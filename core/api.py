# -*- coding: utf-8 -*-
"""pywebview 暴露给前端的 API（核心业务层）。

约定：
- 所有方法返回 dict / list，JSON 可序列化；
- 长任务返回 taskId，前端用 get_task(id) 轮询进度；
- 演示模式不再自动回退：无 adb/无设备时列表为空，用户在 UI 里显式进入后走 core.demo 数据
  （env.demo 明确标记，接上真机自动退出）。
"""
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
import threading
import time
import zipfile
import subprocess

from .adb import Adb, IS_WIN, _no_window
from .logcat import LogcatSession, DemoLogcatSession
from .hdc import Hdc
from .hilog import HilogSession
from .ios import Ios, IosError, IosUnavailable, _as_text
from .ioslog import IosLogSession
from . import demo, action_log
from . import labels as labels_mod
from . import zhdict

# 版本号的唯一来源：应用内显示（appVersion）与窗口标题都从这里取。
# 自动发版时 CI 会在打包前用 ci_version.sh 把它改写成「最新 tag 的 patch +1」，
# 所以平时不用手动改这里；仓库里的值只在「一个 tag 都没有」时作为递增基准兜底。
APP_VERSION = "2.7.0"


def _repo_display_version():
    """源码运行时（run.bat / python main.py）界面上显示的版本号。

    仓库里的 APP_VERSION 只由 CI 在 runner 上改写、**不回写仓库**，所以本地会一直
    停在旧值（2.7.0）。这里直接问 git 要「最近一次发布的 tag」，再按 CI 的同款
    规则 **patch +1**——显示的是「本次开发的版本」，与 ci_version.sh / 本地
    pyd_pack 打出来的包一致：上一个发布是 v2.7.4 时，本地就显示 2.7.5。
    取不到（没装 git / 不在仓库里 / 超时）就退回 APP_VERSION。
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        p = subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0", "--match", "v[0-9]*"],
            cwd=root, capture_output=True, text=True, timeout=3,
            creationflags=_no_window(),
        )
        tag = (p.stdout or "").strip()
        if p.returncode == 0 and tag.startswith("v") and tag[1:2].isdigit():
            base = tag[1:].split("-", 1)[0]  # 剥离历史遗留的 -b<run号> 后缀
            parts = base.split(".")
            if len(parts) == 3 and all(x.isdigit() for x in parts):
                parts[2] = str(int(parts[2]) + 1)
                return ".".join(parts)
    except Exception:  # noqa: BLE001 - 版本号拿不到不该影响启动
        pass
    return APP_VERSION


# 打包态（PyInstaller）直接用编译进去的 APP_VERSION：CI 打包前已写成本次构建版本，
# 和 zip 名 / Release 同源，比去猜更准（包里也没有 .git 可问）。
DISPLAY_VERSION = (APP_VERSION if getattr(sys, "frozen", False)
                   else _repo_display_version())

# iOS UDID 形态（40 位 hex / 24 位 hex / 8-16 分段）。platform 缓存没命中时用它兜底判定
UDID_RE = re.compile(r"^(?:[0-9a-fA-F]{8}-[0-9a-fA-F]{16}|[0-9a-fA-F]{40}|[0-9a-fA-F]{24})$")

CONFIG_FILE = os.path.join(os.path.expanduser("~"), ".adb_tool", "config.json")
DEFAULT_CONFIG = {
    "adbPath": "",
    "hdcPath": "",
    "logBufferDefault": "main",
    "pageSize": 20,
    # Cocos Creator 原生应用的 V8 Inspector 地址，如 172.17.224.90:6086。
    # 留空时只抓 iOS Unified Logging。
    "cocosInspector": "",
    # UI 主题：dark | light（右上角按钮切换）
    "theme": "dark",
    # 连接过的设备历史（左侧"历史设备"），最多保留 50 条：
    # [{serial, model, platform, transport, lastSeen}]
    "deviceHistory": [],
    # 被 ✕ 移除的设备 serial：持久化，重启后仍隐藏；
    # 设备重新上线（state=device）时自动移出该表恢复显示
    "hiddenDevices": [],
}

# 纯血鸿蒙 shell 可见的用户存储根（没有 /sdcard，文件页把 /sdcard 映射到这里）
HAR_ROOT = "/storage/data/local"


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    return cfg


def save_config(cfg):
    try:
        os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


class Task:
    def __init__(self, tid, kind, title):
        self.id = tid
        self.kind = kind
        self.title = title
        self.status = "running"  # running / success / failed / cancelled
        self.phase = ""
        self.percent = 0
        self.done = 0
        self.total = 0
        self.speed = ""
        self.logs = []
        self.error = ""
        self.result = None
        self.cancel = False
        self.started = time.time()

    def log(self, text):
        self.logs.append("[%s] %s" % (time.strftime("%H:%M:%S"), text))
        if len(self.logs) > 500:
            self.logs = self.logs[-300:]

    def to_dict(self):
        return {
            "id": self.id, "kind": self.kind, "title": self.title, "status": self.status,
            "phase": self.phase, "percent": self.percent, "done": self.done,
            "total": self.total, "speed": self.speed, "logs": self.logs,
            "error": self.error, "result": self.result,
            "costMs": int((time.time() - self.started) * 1000),
        }


class Api:
    def __init__(self):
        self.config = load_config()
        self.adb = Adb(self.config.get("adbPath") or None)
        self.labels = labels_mod.LabelResolver(self.adb)
        # iOS 后端（pymobiledevice3，可选依赖）。没装时 available() 返回 False，
        # 所有 iOS 分支都会给出安装提示，不会静默失败。
        self.ios = Ios()
        # 纯血鸿蒙（HarmonyOS NEXT）后端：hdc 命令封装，找不到 hdc 时设备列表
        # 只是少了鸿蒙设备，不影响 Android / iOS。
        self.hdc = Hdc(self.config.get("hdcPath") or None)
        # 媒体工具（截屏/录屏/媒体库）与性能采样，平台能力见各自模块 docstring
        from .media import MediaManager
        from .perf import PerfMonitor
        self.media = MediaManager(self)
        self.perf = PerfMonitor(self)
        # serial -> 'android' | 'ios' | 'harmony'，由 refresh_env / list_devices 刷新
        self._platforms = {}
        self._ios_devices = []
        self._harmony_devices = []
        self.session = None
        self._storage_cache = None
        self._storage_scanning = False
        self.demo = False
        self.current_serial = None
        # 被 ✕ 移除的设备（持久化到 config.json，重启仍生效）；
        # 设备重新上线自动恢复显示
        self._hidden = set(self.config.get("hiddenDevices") or [])
        self.tasks = {}
        self._tid = 0
        self._lock = threading.RLock()
        # 必须是私有属性。pywebview 会递归扫描 js_api 的所有公开对象；
        # 若把 Window 放在公开的 self.window 上，它会钻进 Window 内部，
        # 扫描失败后导致整个 window.pywebview.api 为空。
        self._window = None  # core/app.py 注入
        self.env = {
            # 显示用版本号：源码运行取最近发布的 tag，打包态取编译进来的值（见上方
            # DISPLAY_VERSION 注释）。界面上看到的版本号永远等于最新发布的包。
            "appVersion": DISPLAY_VERSION,
            "adbPath": self.adb.path,
            "adbVersion": "",
            "adbFound": False,
            "serverRunning": False,
            "serverPort": 5037,
            "demo": False,
            "reason": "",
            # 打包形态信息：前端据此调整提示文案（frozen 包内没有 pip，
            # "pip install pymobiledevice3" 这类建议只对源码运行有意义）
            "frozen": bool(getattr(sys, "frozen", False)),
            "platform": sys.platform,
            # iOS 侧：pymobiledevice3 是否可用、版本、不可用原因、已连接台数
            "iosAvailable": False,
            "iosLibVersion": "",
            "iosReason": "",
            "iosCount": 0,
            # 鸿蒙侧：hdc 是否找到、版本、路径来源
            "hdcFound": False,
            "hdcPath": "",
            "hdcVersion": "",
            "hdcSource": "",
            "harmonyCount": 0,
        }

    # =================================================================== 环境
    # ------------------------------------------------------- 平台判定与守卫
    def _platform_of(self, serial=None):
        """判定设备平台。优先用探测时缓存的映射，未命中再查设备缓存 / 主动补探。"""
        serial = serial or self.current_serial
        if not serial:
            return "android"
        p = self._platforms.get(serial)
        if p in ("android", "ios", "harmony"):
            return p
        # 映射缺失（如设备在 refresh_env 之后才接入）时查实时设备缓存兜底
        for d in self._harmony_devices:
            if d.get("serial") == serial:
                self._platforms[serial] = "harmony"
                return "harmony"
        for d in self._ios_devices:
            if d.get("serial") == serial:
                self._platforms[serial] = "ios"
                return "ios"
        # 缓存里也没有：主动补探一次 hdc / adb（hdc 很快；iOS 靠 UDID 形态兜底，
        # 不主动跑 pmd3 探测——最坏要等 25s）
        if not self._harmony_devices and self.hdc.exists()[0]:
            try:
                self._harmony_devices = self.hdc.devices() or []
            except Exception:  # noqa: BLE001
                self._harmony_devices = []
            for d in self._harmony_devices:
                if d.get("serial") == serial:
                    self._platforms[serial] = "harmony"
                    return "harmony"
        if serial not in self._platforms and self.adb.exists()[0]:
            try:
                for d in self.adb.devices():
                    if d.get("serial") == serial:
                        self._platforms[serial] = "android"
                        return "android"
            except Exception:  # noqa: BLE001
                pass
        return "ios" if UDID_RE.match(serial) else "android"

    def _harmony_guard(self):
        """hdc 不可用时的统一错误（前端直接展示 hint）。"""
        ok, _ = self.hdc.exists()
        if not ok:
            return {"ok": False,
                    "error": "未检测到 hdc（纯血鸿蒙设备需要 hdc 连接）",
                    "hint": "安装 DevEco Studio，或把 hdc 放到 public_settings/hdc/ 目录"}
        return None

    # ---------------------------------------------------------- 文件操作辅助
    @staticmethod
    def _har_path(path):
        """鸿蒙没有 /sdcard：把 /sdcard 前缀映射到 shell 可见的用户存储根。"""
        p = path or "/sdcard"
        if p == "/sdcard":
            return HAR_ROOT
        if p.startswith("/sdcard/"):
            return HAR_ROOT + p[len("/sdcard"):]
        return p

    def _sh(self, cmd, serial=None, timeout=30):
        """文件类 shell 统一入口：鸿蒙走 hdc，其余走 adb。命令字符串两边同构。"""
        if self._platform_of(serial) == "harmony":
            return self.hdc.shell(cmd, target=serial, timeout=timeout)
        return self.adb.shell(cmd, serial=serial, timeout=timeout)

    def _stat_size(self, serial, path):
        if self._platform_of(serial) == "harmony":
            return self.hdc.stat_size(serial, path)
        return self.adb.stat_size(serial, path)

    def _ios_guard(self):
        """pymobiledevice3 不可用时的统一错误（前端直接展示 hint）。"""
        ok, reason = self.ios.available()
        if not ok:
            return {"ok": False, "error": "iOS 支持未启用：%s" % reason,
                    "hint": "pip install pymobiledevice3"}
        return None

    def _ios_err(self, where, e):
        hint = getattr(e, "hint", "") or ""
        msg = getattr(e, "msg", "") or str(e)
        action_log.record(where, "ios %s" % where, ok=False, output=msg)
        return {"ok": False, "error": msg, "hint": hint}

    @staticmethod
    def _ios_err_text(e):
        """异常 -> 一行可直接展示的文本（含可操作提示）。"""
        msg = getattr(e, "msg", "") or str(e)
        hint = getattr(e, "hint", "")
        return msg + ("；" + hint if hint else "")

    def refresh_env(self):
        found, r = self.adb.exists()
        ver, detail = self.adb.version() if found else ("", "adb 未找到")
        devices = self.adb.devices() if found else []
        online = [d for d in devices if d["state"] == "device"]

        # --- iOS 探测：依赖缺失或没设备都只是"没有 iOS 设备"，不影响 Android
        ios_ok, ios_reason = self.ios.available()
        ios_ver = ""
        ios_devs = []
        if ios_ok:
            ios_ver = self.ios.version()[0]
            try:
                ios_devs = self.ios.devices(timeout=25) or []
            except Exception as e:  # noqa: BLE001 - 探测失败不阻断 Android
                ios_reason = getattr(e, "msg", str(e))
        ios_online = [d for d in ios_devs if d["state"] == "device"]

        # --- 鸿蒙（hdc）探测：hdc 缺失只是"没有鸿蒙设备"，不影响 Android / iOS
        hdc_ok, _ = self.hdc.exists()
        hdc_ver = ""
        harmony_devs = []
        if hdc_ok:
            hdc_ver = self.hdc.version()
            try:
                harmony_devs = self.hdc.devices() or []
            except Exception as e:  # noqa: BLE001 - 探测失败不阻断其他平台
                harmony_devs = []
        harmony_online = [d for d in harmony_devs if d["state"] == "device"]

        self._platforms = {d["serial"]: "android" for d in devices}
        self._platforms.update({d["serial"]: "ios" for d in ios_devs})
        self._platforms.update({d["serial"]: "harmony" for d in harmony_devs})
        self._ios_devices = ios_devs
        self._harmony_devices = harmony_devs

        # 演示模式**只在用户显式开启时进入**（enable_demo），不再自动回退——
        # 没接手机就显示假设备会误导排查。有真机上线时自动退出演示模式。
        if online or ios_online or harmony_online:
            self.demo = False
        all_online = online + ios_online + harmony_online
        if not (found or ios_ok or hdc_ok):
            reason = "未检测到 adb / hdc 与 iOS 支持：可在设置中指定 adb、hdc 路径（macOS 安装包已内置 adb）"
        elif not all_online:
            reason = ("未检测到已授权设备：Android 请检查 USB 调试开关 / 数据线 / 「信任此电脑」；"
                      "鸿蒙请打开「开发者模式 - USB 调试」")
        else:
            reason = ""
        self.env.update(
            # adbSource: config | path | sdk | portable | common | fallback
            # 设置页用来告诉用户"当前用的是哪个 adb"（手动指定 / PATH / 便携包）
            adbPath=self.adb.path, adbVersion=ver, adbFound=found,
            adbSource=getattr(self.adb, "source", "unknown"),
            serverRunning=self.adb.server_status() if found else False,
            demo=self.demo,
            iosAvailable=ios_ok, iosLibVersion=ios_ver, iosReason=ios_reason,
            iosCount=len(ios_online),
            hdcFound=hdc_ok, hdcPath=self.hdc.path, hdcVersion=hdc_ver,
            hdcSource=getattr(self.hdc, "source", "unknown"),
            harmonyCount=len(harmony_online),
            reason=reason,
        )
        if all_online:
            if not self.current_serial or self.current_serial not in [d["serial"] for d in all_online]:
                self.current_serial = all_online[0]["serial"]
        elif self.demo:
            self.current_serial = demo.DEMO_DEVICE["serial"]
        return self.env

    def enable_demo(self):
        """显式进入演示模式（设备列表为空时预览 UI 用）。

        历史约定是"无 adb/无设备自动进演示模式"，实测会误导：没接手机也显示
        Pixel 8 Pro / iPhone 假数据，用户分不清真假。改为空列表 + 手动入口。
        """
        self.demo = True
        self.current_serial = demo.DEMO_DEVICE["serial"]
        return self.refresh_env()

    def disable_demo(self):
        """退出演示模式，回到真实设备列表（可能为空）。"""
        self.demo = False
        self.current_serial = None
        return self.refresh_env()

    def get_env(self):
        if not self.env.get("adbVersion") and not self.adb.exists()[0]:
            return self.refresh_env()
        return self.env

    # =================================================================== 设备
    def list_devices(self):
        if self.demo:
            return demo.demo_devices()
        out = []
        try:
            if self.adb.exists()[0]:
                for d in self.adb.devices():
                    d["platform"] = "android"
                    out.append(d)
        except Exception:  # noqa: BLE001 - adb 侧炸了也要把 iOS 设备列出来
            pass
        if not self._ios_devices and self.ios.available()[0]:
            try:
                self._ios_devices = self.ios.devices(timeout=25) or []
            except Exception:  # noqa: BLE001
                self._ios_devices = []
        for d in self._ios_devices:
            d["platform"] = "ios"
            out.append(d)
        # 鸿蒙：缓存为空且 hdc 可用时重探一次（与 iOS 同策略）
        if not self._harmony_devices and self.hdc.exists()[0]:
            try:
                self._harmony_devices = self.hdc.devices() or []
            except Exception:  # noqa: BLE001
                self._harmony_devices = []
        for d in self._harmony_devices:
            d["platform"] = "harmony"
            out.append(d)
        # 平台映射必须在这里同步回写：设备可能是程序启动后才插上的，
        # refresh_env 只在启动/手动刷新时跑一次；性能采样等处靠
        # _platform_of() 判平台，漏写会把鸿蒙设备当 Android 采样（全空）。
        self._platforms = {d["serial"]: d["platform"] for d in out}
        # ✕ 移除过的设备：不在线就继续隐藏；重新上线（重新授权）恢复显示
        visible = []
        unhidden = False
        for d in out:
            if d["serial"] in self._hidden:
                if d.get("state") == "device":
                    self._hidden.discard(d["serial"])   # 回归在线，取消隐藏
                    unhidden = True
                else:
                    continue
            visible.append(d)
        if unhidden:
            self.config["hiddenDevices"] = sorted(self._hidden)
            save_config(self.config)
        self._record_history(visible)
        return visible

    # ------------------------------------------------------------- 设备历史
    HISTORY_MAX = 50

    def _record_history(self, devices):
        """把本次枚举到的设备并入历史（按 lastSeen 倒序，去重、封顶）。

        历史落在 config.json（~/.adb_tool/config.json），重启后仍在。
        只有内容变化才写盘，避免高频刷新带来无谓 IO。
        """
        try:
            hist = list(self.config.get("deviceHistory") or [])
            by_serial = {h.get("serial"): h for h in hist}
            now = time.strftime("%Y-%m-%d %H:%M:%S")
            changed = False
            for d in devices:
                serial = d.get("serial")
                if not serial or d.get("platform") == "demo":
                    continue
                h = by_serial.get(serial)
                if h is None:
                    h = {"serial": serial, "model": "", "platform": "android",
                         "transport": "usb", "lastSeen": now}
                    hist.insert(0, h)
                    by_serial[serial] = h
                    changed = True
                if d.get("model") and d["model"] != h.get("model"):
                    h["model"] = d["model"]
                    changed = True
                if d.get("platform") and d["platform"] != h.get("platform"):
                    h["platform"] = d["platform"]
                    changed = True
                if d.get("transport") and d["transport"] != h.get("transport"):
                    h["transport"] = d["transport"]
                    changed = True
                if h.get("lastSeen") != now:
                    h["lastSeen"] = now
                    changed = True
            if len(hist) > self.HISTORY_MAX:
                hist = hist[:self.HISTORY_MAX]
                changed = True
            if changed:
                self.config["deviceHistory"] = hist
                save_config(self.config)
        except Exception:  # noqa: BLE001 - 历史记录失败不影响设备列表
            pass

    def device_history(self):
        """历史设备列表（不含当前仍在线的，前端会把在线的过滤掉）。"""
        return list(self.config.get("deviceHistory") or [])

    def remove_device_history(self, serial):
        """删除一条历史记录；serial 传 "*" 清空全部。"""
        hist = list(self.config.get("deviceHistory") or [])
        if serial == "*":
            hist = []
        else:
            hist = [h for h in hist if h.get("serial") != serial]
        self.config["deviceHistory"] = hist
        save_config(self.config)
        return {"ok": True, "count": len(hist)}

    def remove_device(self, serial):
        """从左侧列表移除设备。

        - 无线地址（ip:port）：hdc tdisconnect + adb disconnect 双通道都试，
          哪个通道连的用哪个断，另一个失败无副作用（hdc 部分版本没有
          tdisconnect 命令，断不开也无妨，靠下面的隐藏表兜底）
        - 离线/断不开的设备（hdc/adb 仍会枚举）：加入隐藏表并持久化到
          config.json，重启后仍隐藏；设备重新上线（state=device）时自动
          恢复显示
        - 同步删除历史记录；若是当前选中设备则清空选中
        """
        if not serial:
            return {"ok": False, "error": "缺少 serial"}
        outputs = []
        if re.match(r"^\d{1,3}(\.\d{1,3}){3}:\d+$", serial):
            hok, _ = self.hdc.exists()
            if hok:
                r = self.hdc.tdisconnect(serial)
                outputs.append("[hdc] " + ((r["stdout"] or r["stderr"]).strip() or
                                 ("已断开" if r["ok"] else "无此连接")))
            try:
                r = self.adb.run(["disconnect", serial], timeout=10)
                outputs.append("[adb] " + ((r["stdout"] or r["stderr"]).strip() or
                               ("已断开" if r["ok"] else "无此连接")))
            except Exception as e:  # noqa: BLE001 - adb 侧失败不阻断移除
                outputs.append("[adb] %s" % e)
        self._hidden.add(serial)
        self.config["hiddenDevices"] = sorted(self._hidden)
        save_config(self.config)
        self._harmony_devices = [d for d in self._harmony_devices
                                 if d.get("serial") != serial]
        self._ios_devices = [d for d in self._ios_devices
                             if d.get("serial") != serial]
        self._platforms.pop(serial, None)
        if self.current_serial == serial:
            self.current_serial = None
        self.remove_device_history(serial)
        action_log.record("remove_device", "remove %s" % serial,
                          serial=serial, ok=True, output="; ".join(outputs))
        return {"ok": True, "output": "\n".join(outputs)}

    def get_device_detail(self, serial=None):
        serial = serial or self.current_serial
        if self.demo:
            return demo.demo_detail(serial)
        if self._platform_of(serial) == "ios":
            guard = self._ios_guard()
            if guard:
                return guard
            try:
                return self.ios.device_info(serial)
            except Exception as e:  # noqa: BLE001
                return self._ios_err("device_info", e)
        if self._platform_of(serial) == "harmony":
            guard = self._harmony_guard()
            if guard:
                return guard
            return self._harmony_device_detail(serial)
        props = self.adb.getprops(serial) or {}
        bat = self.adb.battery(serial)
        kernel = self.adb.kernel(serial)
        rooted = self.adb.is_rooted(serial)
        sdk = props.get("ro.build.version.sdk", "")
        device = {
            "serial": serial,
            "model": props.get("ro.product.model", ""),
            "device": props.get("ro.product.device", ""),
            "androidVersion": props.get("ro.build.version.release", ""),
            "apiLevel": sdk,
            "displayId": props.get("ro.build.display.id", ""),
            "abi": props.get("ro.product.cpu.abi", ""),
            "kernel": kernel,
            "rooted": rooted,
            "battery": bat,
            "state": "device",
            "props": props,
        }
        return device

    def _harmony_device_detail(self, serial):
        """鸿蒙设备详情：param get 系统属性（等价 Android getprops）。

        未授权/离线时 param 全会失败（hdc exit code 0 + [Fail] 文本），
        先探测一把，直接给出人话错误，不让错误串污染型号等字段。
        """
        probe = self.hdc.shell("param get const.product.model",
                               target=serial, timeout=8)
        if not (probe["ok"] and probe["stdout"].strip()):
            blob = ((probe["stdout"] or "") + (probe["stderr"] or "")).lower()
            if "unauthorized" in blob:
                return {"serial": serial, "platform": "harmony",
                        "state": "unauthorized",
                        "error": "设备未授权：请解锁手机，在弹出的「允许 hdc 调试」"
                                 "对话框点允许（建议勾选始终允许）；"
                                 "没看到弹窗就拔插一次 USB 线"}
            if "e001005" in blob or "not found or connected" in blob:
                return {"serial": serial, "platform": "harmony",
                        "state": "offline",
                        "error": "设备已枚举但通信未建立：请在手机上关闭再打开「USB 调试」，"
                                 "拔插 USB 线并允许 hdc 调试弹窗；无效则重启手机后重试"}
            return {"serial": serial, "platform": "harmony",
                    "state": "offline",
                    "error": "设备通信失败：" +
                             ((probe["stdout"] or probe["stderr"] or "").strip()[:160] or "未知错误")}
        model = self.hdc.param("const.product.model", target=serial)
        name = self.hdc.param("const.product.name", target=serial)
        brand = self.hdc.param("const.product.brand", target=serial)
        os_ver = self.hdc.param("const.product.software.version", target=serial)
        api_level = self.hdc.param("const.ohos.apiversion", target=serial)
        display = self.hdc.param("const.product.devicetype", target=serial)
        return {
            "serial": serial,
            "platform": "harmony",
            "model": model or name or serial,
            "device": display,
            "brand": brand,
            "osVersion": os_ver,
            "apiLevel": api_level,
            "battery": {"level": None, "status": "", "powered": False, "charging": False},
            "state": "device",
            "props": {},
        }

    def select_device(self, serial):
        self.current_serial = serial
        self.stop_logcat()
        action_log.record("select_device", "adb -s %s ..." % serial, serial=serial, ok=True)
        return {"ok": True, "serial": serial}

    # ------------------------------------------------------------------ 媒体
    def media_capabilities(self):
        return self.media.capabilities()

    def media_screenshot(self, save_to_device=False, copy_clipboard=False):
        return self.media.screenshot(save_to_device, copy_clipboard)

    def media_record_start(self, opts=None):
        return self.media.record_start(opts)

    def media_record_stop(self):
        return self.media.record_stop()

    def media_record_status(self):
        return self.media.record_status()

    def media_list(self, keyword=""):
        return self.media.list_files(keyword)

    def media_open(self, name):
        return self.media.open_file(name)

    def media_delete(self, name):
        return self.media.delete_file(name)

    def media_rename(self, old, new):
        return self.media.rename_file(old, new)

    def media_save_as(self, name):
        return self.media.save_as(name)

    # ------------------------------------------------------------------ 性能
    def perf_start(self, opts=None):
        return self.perf.start(opts)

    def perf_stop(self):
        return self.perf.stop()

    def perf_status(self, after=0):
        return self.perf.status(after or 0)

    def perf_export(self):
        return self.perf.export_data()

    def perf_import(self):
        return self.perf.import_data()

    def perf_trace_start(self):
        return self.perf.trace_start()

    def perf_trace_stop(self):
        return self.perf.trace_stop()

    def connect_wireless(self, addr):
        if self.demo:
            action_log.record("connect", "adb connect %s" % addr, ok=True, output="演示模式")
            return {"ok": True, "output": "演示模式：模拟连接 %s" % addr}
        # 鸿蒙当前设备 / adb 连不上且 hdc 可用 -> 走 hdc tconn（无线调试）
        if self._platform_of() == "harmony":
            r = self.hdc.tconn(addr)
            action_log.record("connect", r["cmd"], exit_code=r["exitCode"],
                              output=r["stdout"] or r["stderr"], ok=r["ok"])
            return {"ok": r["ok"], "output": (r["stdout"] or r["stderr"]).strip()}
        r = self.adb.run(["connect", addr], timeout=20)
        ok, out = r["ok"], (r["stdout"] or r["stderr"]).strip()
        # adb 连不上时兜底试 hdc：用户不知道手里的鸿蒙机该走哪个通道
        if not ok:
            hok, _ = self.hdc.exists()
            if hok:
                hr = self.hdc.tconn(addr)
                hout = (hr["stdout"] or hr["stderr"]).strip()
                action_log.record("connect", hr["cmd"], exit_code=hr["exitCode"],
                                  output=hout, ok=hr["ok"])
                return {"ok": hr["ok"], "output": out + "\n[hdc] " + hout if out else hout}
        action_log.record("connect", r["cmd"], exit_code=r["exitCode"], output=out, ok=ok)
        return {"ok": ok, "output": out}

    def disconnect_wireless(self, addr):
        if self.demo:
            return {"ok": True, "output": "演示模式"}
        if self._platform_of() == "harmony":
            r = self.hdc.tdisconnect(addr)
            return {"ok": r["ok"], "output": (r["stdout"] or r["stderr"]).strip()}
        r = self.adb.run(["disconnect", addr], timeout=20)
        return {"ok": r["ok"], "output": (r["stdout"] or r["stderr"]).strip()}

    @staticmethod
    def _parse_wlan_ip(text):
        m = re.search(r"inet (?:addr:)?(\d+\.\d+\.\d+\.\d+)", text or "")
        if not m or m.group(1).startswith("127."):
            return ""
        return m.group(1)

    def enable_wireless(self, port=None):
        """USB 在线时把设备切到无线监听（鸿蒙 tmode / Android tcpip），并探测设备 IP。"""
        serial = self.current_serial
        if not serial:
            return {"ok": False, "output": "未选择设备"}
        if self.demo:
            return {"ok": True, "ip": "192.168.1.100", "port": 10123,
                    "output": "演示模式：已切换无线监听"}
        platform = self._platform_of(serial)
        if platform == "ios":
            return {"ok": False, "output": "iOS 不支持 ADB/hdc 无线切换（走 AirPlay 局域网镜像）"}
        try:
            port = int(port) if port else 0
        except (TypeError, ValueError):
            port = 0
        if platform == "harmony":
            port = port or 10123
            # 先查 IP 再 tmode：tmode 生效后 USB 会话会断开
            ip = self._parse_wlan_ip(
                (self.hdc.shell("ifconfig wlan0", target=serial, timeout=10)["stdout"]))
            if not ip:
                ip = self._parse_wlan_ip(
                    (self.hdc.shell("ip addr show wlan0", target=serial, timeout=10)["stdout"]))
            r = self.hdc.tmode(serial, port)
        else:
            port = port or 5555
            r = self.adb.run(["tcpip", str(port)], serial=serial, timeout=15)
            ip = self._parse_wlan_ip(
                (self.adb.run(["shell", "ip -f inet addr show wlan0"],
                              serial=serial, timeout=10)["stdout"]))
        out = (r["stdout"] or r["stderr"]).strip()
        return {"ok": r["ok"], "ip": ip, "port": port, "output": out}

    def get_pid_map(self, serial=None):
        serial = serial or self.current_serial
        if self.demo:
            # 未连接设备：不伪造进程表（日志页本来就应为空）
            m = {}
        elif self._platform_of(serial) == "ios":
            m = self.ios.pid_map(serial) or {}
        elif self._platform_of(serial) == "harmony":
            guard = self._harmony_guard()
            m = self.hdc.pid_map(serial) if not guard else {}
        else:
            m = self.adb.pid_map(serial)
        # 顺带给出中文映射（android_pkg_names 命中的才有值，native 进程回落 None）
        labels = {}
        for name in set(m.values()):
            labels[name] = zhdict.pkg_zh(name)
        return {"map": m, "labels": labels}

    def pidof(self, package, serial=None, aliases=None):
        serial = serial or self.current_serial
        # 防串位：前端漏传 serial 占位时，别名数组会落到 serial 上，纠正回来
        if isinstance(serial, (list, tuple)):
            if aliases is None:
                aliases = serial
            serial = self.current_serial
        if self.demo:
            for name, pkg, _v, _t, _s, _r, pid in demo.DEMO_APPS:
                if pkg == package or pkg in (aliases or []):
                    return {"pid": pid}
            return {"pid": None}
        if self._platform_of(serial) == "ios":
            # iOS 进程名可能被应用改成显示名（如「老子有錢」），exe/包名/显示名都试
            m = self.ios.pid_map(serial) or {}
            cands = [package] + [a for a in (aliases or []) if a]
            for pid, name in m.items():
                if name in cands:
                    return {"pid": pid}
            return {"pid": None}
        if self._platform_of(serial) == "harmony":
            return {"pid": self.hdc.pidof(serial, package)}
        return {"pid": self.adb.pidof(serial, package)}

    # =================================================================== 日志
    def start_logcat(self, serial=None, buffer="main", js_port=None, pkg=None):
        serial = serial or self.current_serial
        if self.demo:
            self.session = DemoLogcatSession(self.adb)
        elif self._platform_of(serial) == "ios":
            guard = self._ios_guard()
            if guard:
                return {"ok": False, "error": guard["error"], "hint": guard.get("hint", ""),
                        "serial": serial, "buffer": buffer}
            # Cocos JS 捕获：日志页临时指定 > 全局设置。纯数字 = 设备端口（usbmux 转发）
            inspector = str(js_port or "").strip() or str(self.config.get("cocosInspector") or "").strip()
            # iOS 系统日志洪流（每秒数百条 com.apple.* 噪声）会把应用/JS 日志顶出缓冲，
            # 选中应用过滤时在后端源头就只注入该进程的行（JS 行不受过滤影响）
            self.session = IosLogSession(
                self.ios, cocos_inspector=inspector, pkg=pkg
            )
        elif self._platform_of(serial) == "harmony":
            guard = self._harmony_guard()
            if guard:
                return {"ok": False, "error": guard["error"], "hint": guard.get("hint", ""),
                        "serial": serial, "buffer": buffer}
            # 鸿蒙走 hilog（buffer 参数不适用，hilog 是单一流）
            self.session = HilogSession(self.hdc)
        else:
            self.session = LogcatSession(self.adb)
        ok = self.session.start(serial, buffer)
        plat = self._platform_of(serial)
        is_ios = plat == "ios"
        if is_ios:
            cmd = "ios syslog -s %s" % serial
        elif plat == "harmony":
            cmd = "hdc -t %s shell hilog" % serial
        else:
            cmd = "adb -s %s logcat -v threadtime -b %s" % (serial, buffer)
        action_log.record("logcat_start", cmd, serial=serial, ok=ok,
                          output=("" if ok else self.session.error))
        out = {"ok": ok, "error": self.session.error, "serial": serial, "buffer": buffer}
        if hasattr(self.session, "info"):
            info = self.session.info()
            out["mode"] = info.get("mode", "")
            out["hint"] = info.get("hint", "")
            out["cdpConfigured"] = bool(info.get("cocosInspector"))
        return out

    def stop_logcat(self):
        if self.session:
            self.session.stop()
            action_log.record("logcat_stop", "stop logcat", serial=self.session.serial, ok=True)
        return {"ok": True}

    def detect_js_port(self, serial=None):
        """扫描 iOS 设备端口找 Cocos V8 Inspector（usbmux 直连，纯 USB）。"""
        serial = serial or self.current_serial
        if self._platform_of(serial) != "ios":
            return {"ok": False, "error": "仅 iOS 设备支持端口扫描"}
        try:
            from .ioslog import detect_cocos_port_async
            from .ios import run_async

            port = run_async(detect_cocos_port_async(serial), timeout=60)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": _as_text(e)}
        if port:
            return {"ok": True, "port": port}
        return {"ok": False, "error": "未发现 Cocos Inspector（游戏需开启 JS 调试端口）"}

    # Cocos 引擎日志级别（cc.debug.DebugMode）：游戏默认级别可能吞掉 console.log，
    # 经 CDP 下发 _resetDebugSetting 动态打开，无需重打包。
    COCOS_DEBUG_EXPR = {
        "verbose": "cc.debug._resetDebugSetting(cc.debug.DebugMode.VERBOSE)",
        "info": "cc.debug._resetDebugSetting(cc.debug.DebugMode.INFO)",
        "warn": "cc.debug._resetDebugSetting(cc.debug.DebugMode.WARN)",
        "error": "cc.debug._resetDebugSetting(cc.debug.DebugMode.ERROR)",
    }

    def cocos_debug_mode(self, mode="info"):
        """在游戏里执行 cc.debug._resetDebugSetting(DebugMode.<mode>)。

        JS 调试口未连接时自动等待：iOS 会话的读线程会自动发现端口并重连
        （游戏在前台运行即可接上），最多等 12 秒——点击按钮即可生效，
        无需先手动重启动捕获。
        """
        expr = self.COCOS_DEBUG_EXPR.get(str(mode or "").lower())
        if expr is None:
            return {"ok": False, "error": "未知级别 %r（可选 info/verbose/warn/error）" % mode}
        sess = self.session
        if not hasattr(sess, "cdp_eval"):
            return {"ok": False, "error": "当前会话不支持 Cocos JS 调试（仅 iOS 捕获会话可用）"}
        if not getattr(sess, "running", False):
            return {"ok": False, "error": "请先开始捕获日志"}
        deadline = time.time() + 12
        while (not getattr(sess, "cdp_running", False) and time.time() < deadline):
            time.sleep(0.3)
        ok, output = sess.cdp_eval(expr)
        action_log.record("cocos_debug_mode", expr, serial=sess.serial, ok=ok, output=output)
        return {"ok": ok, "mode": mode, "output": output}

    def clear_logcat(self):
        if self.session:
            self.session.clear()
        else:
            self.adb.run(["logcat", "-c"], serial=self.current_serial, timeout=10)
        return {"ok": True}

    def get_log_batch(self, cursor=0, limit=2000):
        if not self.session:
            return {"lines": [], "cursor": 0, "total": 0, "seq": 0, "running": False, "error": ""}
        return self.session.get_batch(int(cursor), int(limit))

    def save_logs(self, lines, path, fmt="txt", with_ts=True, with_ids=True, zip_it=False):
        """lines: 前端传来的日志行数组（前端已完成过滤）。"""
        import csv
        import io
        import zipfile

        # 兜底：对话框返回值若未在前端归一化，这里再收一次
        if isinstance(path, (list, tuple)):
            path = path[0] if path else ""
        path = str(path or "").strip()
        if not path:
            return {"ok": False, "error": "保存路径为空"}

        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            if fmt == "csv":
                with open(path, "w", encoding="utf-8-sig", newline="") as f:
                    w = csv.writer(f)
                    w.writerow(["time", "level", "pid", "tid", "tag", "message"])
                    for l in lines:
                        w.writerow([
                            l.get("time", ""), l.get("level", ""), l.get("pid", ""),
                            l.get("tid", ""), l.get("tag", ""), l.get("msg", ""),
                        ])
            elif fmt == "log":
                with open(path, "w", encoding="utf-8") as f:
                    for l in lines:
                        f.write(l.get("msg", "") + "\n")
            else:
                with open(path, "w", encoding="utf-8") as f:
                    for l in lines:
                        parts = []
                        if with_ts and l.get("time"):
                            parts.append(l["time"])
                        if with_ids:
                            parts.append("%s/%s" % (l.get("pid", ""), l.get("tid", "")))
                        if l.get("level"):
                            parts.append(l["level"])
                        if l.get("tag"):
                            parts.append(l["tag"] + ":")
                        parts.append(l.get("msg", ""))
                        f.write(" ".join(p for p in parts if p) + "\n")
            final = path
            if zip_it:
                final = os.path.splitext(path)[0] + ".zip"
                with zipfile.ZipFile(final, "w", zipfile.ZIP_DEFLATED) as z:
                    z.write(path, os.path.basename(path))
                try:
                    os.remove(path)
                except OSError:
                    pass
            action_log.record("save_logs", "save %d lines -> %s" % (len(lines), final), ok=True)
            return {"ok": True, "path": final, "count": len(lines)}
        except Exception as e:  # noqa
            return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}

    # =================================================================== 应用
    def list_packages(self, serial=None, kind="all"):
        serial = serial or self.current_serial
        if self.demo:
            return {"ok": True, "packages": demo.demo_packages(kind, serial), "demo": True}
        if self._platform_of(serial) == "ios":
            guard = self._ios_guard()
            if guard:
                return dict(guard, packages=[])
            try:
                # iOS 的应用名由设备直接给出（CFBundleDisplayName），不需要 aapt/词库
                return {"ok": True, "packages": self.ios.list_packages(serial, kind)}
            except Exception as e:  # noqa: BLE001
                return dict(self._ios_err("list_packages", e), packages=[])
        if self._platform_of(serial) == "harmony":
            guard = self._harmony_guard()
            if guard:
                return dict(guard, packages=[])
            # bm dump -a 只给 bundle 名列表；应用名/体积要逐个 bm dump -n（慢），
            # 列表页先出包名，详情弹窗再查具体信息
            pkgs, r = self.hdc.bm_list(serial)
            if not r["ok"]:
                return {"ok": False, "error": r["stderr"] or r["stdout"], "packages": []}
            pmap = self.hdc.pid_map(serial)
            running = {}
            for pid, name in pmap.items():
                running[name] = pid
            out = []
            for pkg in pkgs:
                pid = running.get(pkg, 0)
                out.append({
                    "packageName": pkg,
                    "label": "",
                    "codePath": "",
                    "versionName": "",
                    "type": "",
                    "sizeBytes": 0,
                    "running": bool(pid),
                    "pid": pid,
                })
            return {"ok": True, "packages": out}
        # with_path=True 一次拿到 base.apk 路径，供应用名解析（避免 per-app dumpsys）
        pkgs, r = self.adb.list_packages(serial, kind, with_path=True)
        if not r["ok"]:
            return {"ok": False, "error": r["stderr"] or r["stdout"], "packages": []}
        pmap = self.adb.pid_map(serial)
        running = {}
        for pid, name in pmap.items():
            running[name] = pid
        out = []
        for pkg, apk_path in pkgs:
            out.append({
                "packageName": pkg,
                # 静态词库命中即显示中文，列表秒出；未命中的由 resolve_labels 走 aapt 补
                "label": zhdict.pkg_zh(pkg) or "",
                "codePath": apk_path,
                "versionName": "",
                "type": "third" if kind == "third" else ("system" if kind == "system" else ""),
                "sizeBytes": 0,
                "running": pkg in running,
                "pid": running.get(pkg, 0),
            })
        return {"ok": True, "packages": out}

    # ------------------------------------------------------- 应用名（可选 aapt）
    def get_label_status(self):
        return self.labels.status()

    def resolve_labels(self, items, serial=None):
        """items: [{package, codePath}]，只为传入的这些包解析应用名。

        优先级：静态词库（即时） > aapt 解析（准确但慢，需 pull apk）。
        词库已命中的不再走 aapt，避免无谓的 pull。
        """
        serial = serial or self.current_serial
        items = items or []
        if self.demo:
            out = {}
            for it in items:
                d = demo.demo_app_detail(it.get("package", ""))
                out[it.get("package", "")] = d.get("label")
            return {"ok": True, "labels": out, "demo": True}
        try:
            labels_map = zhdict.pkg_batch([it.get("package", "") for it in items])
            pending = [it for it in items if it.get("package", "") not in labels_map]
            if pending and self.labels.available:
                labels_map.update(
                    {k: v for k, v in self.labels.resolve_batch(serial, pending).items() if v}
                )
            return {"ok": True, "labels": labels_map}
        except Exception as e:  # noqa
            return {"ok": False, "error": "%s: %s" % (type(e).__name__, e), "labels": {}}

    def get_zh_stats(self):
        """静态中文词库加载情况，供设置页展示。"""
        return zhdict.stats()

    @staticmethod
    def _zh_perms(perms):
        """给权限列表补中文字段。没有映射的 label 为空，前端回退显示原名。"""
        out = []
        for p in perms or []:
            name = p.get("name", "")
            zh = zhdict.perm_zh(name) or zhdict.perm_zh(p.get("full", "")) or ""
            group = zhdict.perm_group(name) or zhdict.perm_group(p.get("full", "")) or ""
            item = dict(p)
            item["label"] = zh
            item["group"] = group
            out.append(item)
        return out

    # ------------------------------------------------------- 配置
    def get_config(self):
        cfg = dict(self.config)
        cfg["detectedAdbPath"] = self.adb.path
        return cfg

    def set_config(self, patch):
        self.config.update(patch or {})
        ok = save_config(self.config)
        if "adbPath" in (patch or {}):
            self.adb = Adb(self.config.get("adbPath") or None)
            self.labels = labels_mod.LabelResolver(self.adb)
            self.refresh_env()
        if "hdcPath" in (patch or {}):
            self.hdc = Hdc(self.config.get("hdcPath") or None)
            self.refresh_env()
        return {"ok": ok, "config": self.get_config()}

    def get_app_detail(self, package, serial=None):
        serial = serial or self.current_serial
        if self.demo:
            d = demo.demo_app_detail(package, serial)
            d["permissions"] = self._zh_perms(d.get("permissions"))
            return {"ok": True, "detail": d}
        if self._platform_of(serial) == "ios":
            guard = self._ios_guard()
            if guard:
                return guard
            try:
                return {"ok": True, "detail": self.ios.app_detail(serial, package)}
            except Exception as e:  # noqa: BLE001
                return self._ios_err("app_detail", e)
        if self._platform_of(serial) == "harmony":
            guard = self._harmony_guard()
            if guard:
                return guard
            return self._harmony_app_detail(serial, package)
        info, r = self.adb.dump_package(serial, package)
        if not r["ok"]:
            return {"ok": False, "error": r["stderr"] or r["stdout"]}
        pid = self.adb.pidof(serial, package)
        typ = "system" if info["system"] else "third"
        size = self.adb.app_size(serial, package, info["codePath"], info["dataDir"])
        detail = {
            "packageName": package,
            # 只取静态词库（即时、无 IO）；aapt 的准确结果由列表页 resolve_labels 异步回填。
            # 详情页不做 aapt，避免 pull 大体积 base.apk 把弹窗卡住。
            "label": zhdict.pkg_zh(package) or "",
            "versionName": info["versionName"],
            "versionCode": info["versionCode"],
            "type": typ,
            "uid": info["uid"],
            "targetSdk": info["targetSdk"],
            "running": pid is not None,
            "pid": pid or 0,
            "enabled": info["enabled"],
            "codePath": info["codePath"],
            "dataDir": info["dataDir"],
            "firstInstallTime": info["firstInstallTime"],
            "lastUpdateTime": info["lastUpdateTime"],
            "appSize": size["appSize"],
            "dataSize": size["dataSize"],
            "cacheSize": size["cacheSize"],
            "permissions": self._zh_perms(info["permissions"]),
            "granted": info["granted"],
            "requested": info["requested"],
        }
        detail["sizeBytes"] = detail["appSize"] + detail["dataSize"] + detail["cacheSize"]
        return {"ok": True, "detail": detail}

    def _harmony_app_detail(self, serial, package):
        """鸿蒙应用详情：bm dump -n 解析（新版 JSON / 老版 key-value 都兜）。"""
        dump, r = self.hdc.bm_dump(serial, package)
        if r["ok"] and not dump:
            # 非 JSON 输出：未安装或老版本，报错给前端
            if "Failure" in r["stdout"] or "fail" in r["stdout"].lower():
                return {"ok": False, "error": "未找到应用 %s" % package}
        if not dump:
            dump = {}
        app = dump.get("application") or {}
        version_name = app.get("appVersionName", "")
        version_code = app.get("appVersionCode", "")
        code_path = app.get("codePath", "") or dump.get("appIdentifier", "")
        is_pre = app.get("isPreInstallApp")
        if is_pre is None:
            is_pre = dump.get("isPreInstallApp")
        typ = "system" if is_pre else "third"
        pid = self.hdc.pidof(serial, package)
        abilities = [a.get("name", "") for a in (dump.get("abilityInfos") or [])
                     if a.get("name")]
        install_time = dump.get("installTime") or ""
        update_time = dump.get("updateTime") or ""
        fmt_ms = lambda v: (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(v) / 1000))
                            if str(v).isdigit() and int(v) > 0 else str(v or ""))
        detail = {
            "packageName": package,
            "label": "",
            "versionName": str(version_name or ""),
            "versionCode": str(version_code or ""),
            "type": typ,
            "uid": dump.get("uid", "") or "",
            "targetSdk": "",
            "running": pid is not None,
            "pid": pid or 0,
            "enabled": True,
            "codePath": code_path,
            "dataDir": app.get("dataBaseDir", ""),
            "firstInstallTime": fmt_ms(install_time),
            "lastUpdateTime": fmt_ms(update_time),
            "appSize": 0,
            "dataSize": 0,
            "cacheSize": 0,
            "sizeBytes": 0,
            "entryAbility": abilities[0] if abilities else "",
            "abilities": abilities,
            # 鸿蒙的运行时授权状态在系统设置里，bm dump 只有申请列表，
            # 展示"未授予"会误导，前端按 unsupported 走说明文案
            "permissions": [],
            "granted": 0,
            "requested": 0,
            "permissionsUnsupported": True,
        }
        return {"ok": True, "detail": detail}

    def _harmony_entry_ability(self, serial, package):
        """启动应用需要的 ability 名：bm dump 解析，失败退回常见默认名。"""
        try:
            d = self._harmony_app_detail(serial, package)
            if d.get("ok") and d["detail"].get("entryAbility"):
                return d["detail"]["entryAbility"]
        except Exception:  # noqa: BLE001
            pass
        return "EntryAbility"

    def app_action(self, package, action, serial=None):
        serial = serial or self.current_serial
        cmds = {
            "start": "monkey -p %s -c android.intent.category.LAUNCHER 1" % package,
            "stop": "am force-stop %s" % package,
            "clear": "pm clear %s" % package,
            "disable": "pm disable-user %s" % package,
            "enable": "pm enable %s" % package,
        }
        if action not in cmds:
            return {"ok": False, "error": "未知操作: %s" % action}
        sh = cmds[action]
        # 鸿蒙：启动/强制停止走 aa（Ability Manager），清除数据/停用没有公开能力
        if self._platform_of(serial) == "harmony":
            if action == "start":
                ability = self._harmony_entry_ability(serial, package)
                sh = "aa start -b %s -a %s" % (package, ability)
            elif action == "stop":
                sh = "aa force-stop %s" % package
            else:
                return {"ok": False, "unsupported": True,
                        "error": "鸿蒙设备暂不支持「%s」（无对应 bm/aa 能力）"
                                 % {"clear": "清除数据", "disable": "停用",
                                    "enable": "启用"}.get(action, action)}
            guard = self._harmony_guard()
            if guard:
                return guard
            r = self.hdc.shell(sh, target=serial, timeout=40)
            action_log.record(action, r["cmd"], serial=serial, exit_code=r["exitCode"],
                              output=(r["stdout"] or r["stderr"]).strip(), ok=r["ok"])
            return {"ok": r["ok"], "output": (r["stdout"] or r["stderr"]).strip(), "cmd": r["cmd"]}
        # iOS 非越狱没有 am/pm，启动/停用/清数据都没有对应能力（清数据只能卸载重装）
        if self._platform_of(serial) == "ios":
            return {"ok": False, "unsupported": True,
                    "error": "iOS 非越狱设备不支持「%s」（无 am/pm 能力）"
                             % {"start": "启动", "stop": "强制停止", "clear": "清除数据",
                                "disable": "停用", "enable": "启用"}.get(action, action)}
        if self.demo:
            action_log.record(action, "adb shell %s" % sh, serial=serial, ok=True, output="演示模式")
            return {"ok": True, "output": "演示模式：模拟执行 %s" % sh, "cmd": "adb shell " + sh}
        r = self.adb.shell(sh, serial=serial, timeout=40)
        action_log.record(action, r["cmd"], serial=serial, exit_code=r["exitCode"],
                          output=(r["stdout"] or r["stderr"]).strip(), ok=r["ok"])
        return {"ok": r["ok"], "output": (r["stdout"] or r["stderr"]).strip(), "cmd": r["cmd"]}

    def uninstall(self, package, keep_data=False, serial=None):
        serial = serial or self.current_serial
        tid = self._new_task("uninstall", "卸载 %s" % package)
        t = self.tasks[tid]

        def work():
            if self.demo:
                for i in range(0, 101, 20):
                    if t.cancel:
                        t.status = "cancelled"
                        return
                    t.percent = i
                    t.phase = "正在卸载…"
                    time.sleep(0.25)
                t.status = "success"
                t.percent = 100
                t.phase = "卸载完成"
                return
            if self._platform_of(serial) == "harmony":
                guard = self._harmony_guard()
                if guard:
                    t.status = "failed"
                    t.error = guard["error"] + "（" + guard.get("hint", "") + "）"
                    t.phase = "hdc 不可用"
                    return
                if keep_data:
                    t.log("鸿蒙不支持保留数据卸载，忽略 -k")
                t.phase = "正在卸载…"
                t.log("hdc -t %s uninstall %s" % (serial, package))
                r = self.hdc.run(["uninstall", package], target=serial, timeout=120)
                out = (r["stdout"] or r["stderr"]).strip()
                action_log.record("uninstall", r["cmd"], serial=serial,
                                  exit_code=r["exitCode"], output=out, ok=r["ok"])
                t.percent = 100
                t.phase = "卸载完成" if r["ok"] else "卸载失败"
                t.status = "success" if r["ok"] else "failed"
                t.error = "" if r["ok"] else (out or "卸载失败（命令无输出）")
                t.result = {"output": out}
                return
            if self._platform_of(serial) == "ios":
                guard = self._ios_guard()
                if guard:
                    t.status = "failed"
                    t.error = guard["error"] + "（" + guard.get("hint", "") + "）"
                    t.phase = "iOS 支持未启用"
                    return
                try:
                    t.phase = "正在卸载…"
                    t.log("ios uninstall %s" % package)
                    self.ios.uninstall(serial, package)
                    action_log.record("uninstall", "ios uninstall %s" % package, serial=serial,
                                      ok=True, output="已卸载")
                    t.percent = 100
                    t.phase = "卸载完成"
                    t.status = "success"
                    t.result = {"output": "已卸载 %s" % package}
                except Exception as e:  # noqa: BLE001
                    t.status = "failed"
                    t.phase = "卸载失败"
                    t.error = self._ios_err_text(e)
                return
            args = ["uninstall"]
            if keep_data:
                args.append("-k")
            args.append(package)
            r = self.adb.run(args, serial=serial, timeout=120)
            action_log.record("uninstall", r["cmd"], serial=serial, exit_code=r["exitCode"],
                              output=(r["stdout"] or r["stderr"]).strip(), ok=r["ok"])
            t.percent = 100
            t.phase = "卸载完成" if r["ok"] else "卸载失败"
            t.status = "success" if r["ok"] else "failed"
            t.error = "" if r["ok"] else (r["stdout"] or r["stderr"]).strip()
            t.result = {"output": (r["stdout"] or r["stderr"]).strip()}

        threading.Thread(target=work, daemon=True).start()
        return {"taskId": tid}

    def install_apk(self, path, options=None, serial=None):
        serial = serial or self.current_serial
        options = options or {}
        tid = self._new_task("install", "安装 %s" % os.path.basename(path))
        t = self.tasks[tid]
        t.total = os.path.getsize(path) if os.path.exists(path) else 0
        # 同一个入口按平台分发：Android 装 apk / xapk，iOS 装 ipa，鸿蒙装 hap
        if self._platform_of(serial) == "harmony":
            threading.Thread(target=self._install_harmony_worker,
                             args=(t, serial, path), daemon=True).start()
            return {"taskId": tid}
        if self._platform_of(serial) == "ios":
            threading.Thread(target=self._install_ios_worker,
                             args=(t, serial, path), daemon=True).start()
            return {"taskId": tid}
        threading.Thread(target=self._install_worker,
                         args=(t, serial, path, options), daemon=True).start()
        return {"taskId": tid}

    # installd 回传的 Status -> 中文阶段。ipa 安装本身就慢（上传 staging +
    # 设备端解包/验签/安装），必须把阶段显示出来，否则看着像卡死在 0%。
    IOS_INSTALL_PHASES = {
        "CreatingStagingDirectory": "创建暂存目录…",
        "CopyingFile": "正在上传到设备…",
        "ExtractingPackage": "设备端解包…",
        "InspectingPackage": "检查包内容…",
        "PreflightingApplication": "预检应用…",
        "VerifyingApplication": "校验签名…",
        "CreatingContainer": "创建应用容器…",
        "InstallingApplication": "安装到设备…",
        "PostflightingApplication": "安装收尾…",
        "SandboxingApplication": "配置沙盒…",
        "GeneratingApplicationMap": "生成应用映射…",
        "Complete": "安装完成",
    }

    def _install_ios_worker(self, t, serial, path):
        guard = self._ios_guard()
        if guard:
            t.status = "failed"
            t.phase = "iOS 支持未启用"
            t.error = guard["error"] + "（" + guard.get("hint", "") + "）"
            return
        t.phase = "正在安装 ipa…"
        t.log("ios install %s" % path)
        try:
            last = {"pct": -1, "status": ""}

            def _cb(pct=None, status="", *_args, **_kwargs):
                if isinstance(pct, int) and pct != last["pct"]:
                    last["pct"] = pct
                    t.percent = max(1, min(99, pct))
                s = str(status or "")
                if s and s != last["status"]:
                    last["status"] = s
                    t.phase = self.IOS_INSTALL_PHASES.get(s, "正在安装 ipa…（%s）" % s)
                    t.log("installd: %s%s" % (
                        s, ("  %d%%" % pct) if isinstance(pct, int) else ""))

            self.ios.install(serial, path, on_progress=_cb)
            action_log.record("install", "ios install %s" % path, serial=serial, ok=True,
                              output="安装完成")
            t.percent = 100
            t.phase = "安装完成"
            t.status = "success"
            t.result = {"output": "已安装 %s" % os.path.basename(path)}
        except Exception as e:  # noqa: BLE001
            action_log.record("install", "ios install %s" % path, serial=serial, ok=False,
                              output=str(e))
            t.status = "failed"
            t.phase = "安装失败"
            t.error = self._ios_err_text(e)

    def _install_harmony_worker(self, t, serial, path):
        """鸿蒙 hap 安装：hdc install 会自己完成「发文件 + bm install」两步。

        hdc 不给可解析的进度输出，进度条按时间爬到 90% 表示"进行中"，
        结束后以退出码 + 输出内容判成败。
        """
        guard = self._harmony_guard()
        if guard:
            t.status = "failed"
            t.phase = "hdc 不可用"
            t.error = guard["error"] + "（" + guard.get("hint", "") + "）"
            return
        name = os.path.basename(path)
        t.phase = "正在传输并安装 hap…"
        t.log("hdc -t %s install -r %s" % (serial, name))
        cmd = self.hdc.build_cmd(["install", "-r", path], target=serial)
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                encoding="utf-8", errors="replace",
                creationflags=_no_window() if IS_WIN else 0,
            )
        except Exception as e:  # noqa
            t.status = "failed"
            t.phase = "安装失败"
            t.error = str(e)
            return
        start = time.time()
        while proc.poll() is None:
            if t.cancel:
                proc.kill()
                t.status = "cancelled"
                return
            # 时间爬坡：前 60 秒从 0 爬到 90%（大包安装不至于显示 0% 卡死）
            t.percent = min(90, int(time.time() - start) * 3)
            time.sleep(0.5)
        out = (proc.stdout.read() if proc.stdout else "") or ""
        out = out.strip()
        failed = proc.returncode != 0 or "fail" in out.lower()
        action_log.record("install", " ".join(cmd), serial=serial,
                          exit_code=proc.returncode, output=out, ok=not failed)
        t.percent = 100
        t.phase = "安装完成" if not failed else "安装失败"
        t.status = "success" if not failed else "failed"
        if failed:
            lines = [l for l in out.splitlines() if l.strip()]
            t.error = lines[-1] if lines else "安装失败（命令无输出）"
        t.result = {"output": out}
        t.log(out.replace("\n", " | ") if out else "Success")

    @staticmethod
    def _opt_flags(options):
        flags = []
        for k in ("r", "d", "g", "t"):
            if options.get(k):
                flags.append("-" + k)
        return flags

    @staticmethod
    def _merge_out(r):
        """stdout + stderr 合并。

        adb/pm 把进度行（Performing Streamed Install）和失败详情
        （Failure [INSTALL_FAILED_*] / adb: error: ...）都写 stderr，
        用 `stdout or stderr` 会在 stdout 非空时把真正的错误整个丢掉。
        """
        parts = [(r.get("stdout") or "").strip(), (r.get("stderr") or "").strip()]
        return "\n".join(p for p in parts if p)

    @staticmethod
    def _install_verdict(r, output):
        """以输出里**最后**一个结论为准，拿不到结论才退回 returncode。

        adb 增量安装失败会自动回退流式安装，输出形如
            Performing Incremental Install / Failure / Performing Streamed Install / Success
        中间那个 Failure 是正常的，只要见 Failure 就判失败会误报。
        """
        verdict = None
        for line in output.splitlines():
            s = line.strip()
            if not s:
                continue
            if s.startswith("Success"):
                verdict = True
            elif (s.startswith("Failure") or "INSTALL_FAILED" in s
                  or "INSTALL_PARSE_FAILED" in s or s.lower().startswith("adb: error")
                  or s.lower().startswith("adb: failed")):
                verdict = False
        if verdict is None:
            return bool(r["ok"])
        return verdict

    @staticmethod
    def _install_error(output):
        """从输出里挑出真正有用的那一行，别把进度行当错误展示。"""
        keys = ("INSTALL_FAILED", "INSTALL_PARSE_FAILED", "Failure",
                "adb: error", "adb: failed")
        for line in reversed(output.splitlines()):
            s = line.strip()
            if s and any(k in s for k in keys):
                return s
        return output.strip() or "安装失败（命令无输出）"

    def _install_worker(self, t, serial, path, options):
        ext = os.path.splitext(path)[1].lower()
        if ext in (".xapk", ".apks", ".zip"):
            return self._install_split_worker(t, serial, path, options)
        flags = self._opt_flags(options)
        name = os.path.basename(path)
        remote = "/data/local/tmp/%s" % name
        if self.demo:
            t.log("adb install %s %s" % (" ".join(flags), name))
            total = t.total or 20000000
            sent = 0
            start = time.time()
            while sent < total and not t.cancel:
                time.sleep(0.2)
                sent = min(total, sent + total * 0.08)
                t.done = int(sent)
                t.percent = min(90, int(sent / total * 90))
                t.phase = "正在推送文件到设备…"
                t.speed = "%.1f MB/s" % (sent / max(0.2, time.time() - start) / 1024 / 1024)
            if t.cancel:
                t.status = "cancelled"
                return
            for p in (94, 97, 99):
                if t.cancel:
                    t.status = "cancelled"
                    return
                t.percent = p
                t.phase = "正在安装到设备…"
                t.log("pm install %s" % name)
                time.sleep(0.3)
            t.percent = 100
            t.phase = "安装完成"
            t.status = "success"
            t.log("Success")
            action_log.record("install", "adb install %s %s" % (" ".join(flags), name),
                              serial=serial, ok=True, output="Success（演示模式）")
            return
        # 真实流程：先 push 拿速率与进度，再 pm install
        t.phase = "正在推送文件到设备…"
        t.log("adb -s %s push %s %s" % (serial, name, remote))
        cmd = self.adb.build_cmd(["push", path, remote], serial=serial)
        size = t.total or 1
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                encoding="utf-8", errors="replace",
                creationflags=_no_window() if IS_WIN else 0,
            )
        except Exception as e:  # noqa
            t.status = "failed"
            t.error = str(e)
            return
        last = 0
        start = time.time()
        while proc.poll() is None:
            if t.cancel:
                proc.kill()
                t.status = "cancelled"
                return
            got = self.adb.stat_size(serial, remote)
            if got != last:
                t.done = got
                t.percent = min(90, int(got / size * 90))
                t.speed = "%.1f MB/s" % (got / max(0.3, time.time() - start) / 1024 / 1024)
                last = got
            time.sleep(0.25)
        out = proc.stdout.read() if proc.stdout else ""
        out = (out or "").strip()
        if proc.returncode != 0:
            t.status = "failed"
            t.error = out or "push 失败"
            action_log.record("install", " ".join(cmd), serial=serial, exit_code=proc.returncode,
                              output=out, ok=False)
            return
        t.percent = 92
        t.phase = "正在安装到设备…"
        # 文件已经 push 到设备上了，所以必须走 `shell pm install <设备路径>`。
        # 写成 `adb install <设备路径>` 是错的：adb install 的参数是**电脑本地路径**，
        # 它会去 PC 上找 /data/local/tmp/xxx.apk，必然 cannot stat。
        t.log("adb -s %s shell pm install %s %s" % (serial, " ".join(flags), remote))
        r = self.adb.run(["shell", "pm", "install"] + flags + [remote],
                         serial=serial, timeout=300)
        output = self._merge_out(r)
        t.log(output.replace("\n", " | "))
        ok = self._install_verdict(r, output)
        action_log.record("install", r["cmd"], serial=serial, exit_code=r["exitCode"],
                          output=output, ok=ok)
        try:
            self.adb.shell("rm %s" % remote, serial=serial, timeout=15)
        except Exception:
            pass
        t.percent = 100
        t.phase = "安装完成" if ok else "安装失败"
        t.status = "success" if ok else "failed"
        t.error = "" if ok else self._install_error(output)
        t.result = {"output": output}

    # ---------------------------------------------- .xapk / .apks 会话安装
    def _install_split_worker(self, t, serial, path, options):
        """解包 xapk/apks，用 pm install-create/write/commit 会话安装。"""
        flags = self._opt_flags(options)
        name = os.path.basename(path)
        t.log("解包 %s" % name)
        tmpdir = tempfile.mkdtemp(prefix="adbtool_split_")
        apks = []
        try:
            try:
                with zipfile.ZipFile(path) as z:
                    z.extractall(tmpdir)
            except Exception as e:  # noqa
                t.status = "failed"
                t.error = "解包失败：%s" % e
                t.log(t.error)
                return
            for root, _dirs, files in os.walk(tmpdir):
                for f in files:
                    if f.lower().endswith(".apk"):
                        apks.append(os.path.join(root, f))
            if not apks:
                t.status = "failed"
                t.error = "包内未找到 .apk 文件"
                t.log(t.error)
                return
            # base.apk 排最前，其余按 split_config 顺序
            apks.sort(key=lambda p: (0 if os.path.basename(p).lower() == "base.apk" else 1, p))
            total = sum(os.path.getsize(p) for p in apks)
            t.total = total
            t.log("共 %d 个 split，合计 %d 字节" % (len(apks), total))

            if self.demo:
                sent = 0
                for p in apks:
                    if t.cancel:
                        t.status = "cancelled"
                        return
                    size = os.path.getsize(p)
                    t.phase = "正在推送 %s…" % os.path.basename(p)
                    t.log("adb push %s /data/local/tmp/" % os.path.basename(p))
                    while sent < total and not t.cancel:
                        time.sleep(0.15)
                        sent = min(total, sent + max(total * 0.1, 1024))
                        t.done = int(sent)
                        t.percent = min(85, int(sent / max(1, total) * 85))
                t.log("pm install-create -r -d -g -S %d" % total)
                t.percent = 90
                t.phase = "创建安装会话…"
                time.sleep(0.3)
                t.percent = 96
                t.phase = "写入 split 并提交…"
                time.sleep(0.3)
                t.percent = 100
                t.phase = "安装完成"
                t.status = "success"
                t.log("Success")
                action_log.record("install", "pm install-create/write/commit %s" % name,
                                  serial=serial, ok=True, output="Success（演示模式）")
                return

            # 1) 创建会话
            t.phase = "创建安装会话…"
            create_cmd = ["shell", "pm", "install-create"] + flags + ["-S", str(total)]
            r = self.adb.run(create_cmd, serial=serial, timeout=120)
            out = (r["stdout"] or r["stderr"]).strip()
            t.log((r["cmd"] + " -> " + out)[:200])
            m = re.search(r"\[(\d+)\]", out)
            if not m:
                t.status = "failed"
                t.error = out or "pm install-create 失败"
                action_log.record("install", r["cmd"], serial=serial, exit_code=r["exitCode"],
                                  output=out, ok=False)
                return
            session = m.group(1)
            t.percent = 10

            # 2) 逐个 push + install-write
            sent = 0
            remote_dir = "/data/local/tmp/adbtool_%s" % session
            self.adb.shell("mkdir -p %s" % remote_dir, serial=serial, timeout=20)
            for idx, apk in enumerate(apks):
                if t.cancel:
                    t.status = "cancelled"
                    self.adb.shell("pm install-abandon %s" % session, serial=serial, timeout=30)
                    return
                aname = os.path.basename(apk)
                remote = remote_dir + "/" + aname
                size = os.path.getsize(apk)
                t.phase = "正在推送 %s…" % aname
                r = self.adb.run(["push", apk, remote], serial=serial, timeout=900)
                if not r["ok"]:
                    t.status = "failed"
                    t.error = (r["stdout"] or r["stderr"]).strip()[:200]
                    self.adb.shell("pm install-abandon %s" % session, serial=serial, timeout=30)
                    action_log.record("install", r["cmd"], serial=serial, exit_code=r["exitCode"],
                                      output=t.error, ok=False)
                    return
                sent += size
                t.done = sent
                t.percent = 10 + int(sent / max(1, total) * 70)
                wr = self.adb.run(
                    ["shell", "pm", "install-write", "-S", str(size), session, str(idx), remote],
                    serial=serial, timeout=300)
                if not wr["ok"]:
                    t.status = "failed"
                    t.error = (wr["stdout"] or wr["stderr"]).strip()[:200]
                    self.adb.shell("pm install-abandon %s" % session, serial=serial, timeout=30)
                    action_log.record("install", wr["cmd"], serial=serial, exit_code=wr["exitCode"],
                                      output=t.error, ok=False)
                    return
                t.log("install-write %d %s" % (idx, aname))

            # 3) 提交
            t.percent = 92
            t.phase = "正在提交安装…"
            cr = self.adb.run(["shell", "pm", "install-commit", session], serial=serial, timeout=600)
            output = self._merge_out(cr)
            t.log(output.replace("\n", " | ")[:200])
            ok = self._install_verdict(cr, output)
            action_log.record("install", cr["cmd"], serial=serial, exit_code=cr["exitCode"],
                              output=output, ok=ok)
            self.adb.shell("rm -rf %s" % remote_dir, serial=serial, timeout=30)
            t.percent = 100
            t.phase = "安装完成" if ok else "安装失败"
            t.status = "success" if ok else "failed"
            t.error = "" if ok else self._install_error(output)
            t.result = {"output": output}
        finally:
            try:
                shutil.rmtree(tmpdir, ignore_errors=True)
            except Exception:
                pass

    # =================================================================== 文件
    def list_dir(self, path="/sdcard", serial=None, bundleId=None):
        serial = serial or self.current_serial
        if self.demo:
            # 未连接设备：不返回演示目录，直接给空列表
            return {"ok": True, "path": path, "entries": [], "readable": True}
        if self._platform_of(serial) == "ios":
            return self._ios_list_dir(serial, path, bundleId)
        if self._platform_of(serial) == "harmony":
            hp = self._har_path(path)
            entries, r = self.hdc.list_dir(serial, hp)
            if not r["ok"]:
                raw = (r["stderr"] or r["stdout"]).strip()[:300]
                low = raw.lower()
                if "no such file" in low:
                    error = ("目录不存在或已被删除（%s），鸿蒙下 hdc 可见目录一般是 "
                             "/storage/data/local/tmp" % hp)
                elif "permission" in low:
                    error = "该目录需要更高权限，鸿蒙 shell 无法访问（%s）" % hp
                else:
                    error = raw or "无法读取目录"
                return {"ok": False, "path": hp, "entries": [], "readable": False,
                        "error": error}
            return {"ok": True, "path": hp, "entries": entries, "readable": True}
        entries, r = self.adb.list_dir(serial, path)
        if not r["ok"]:
            raw = (r["stderr"] or r["stdout"]).strip()[:300]
            low = raw.lower()
            # 非 root 设备访问 /data、/cache 等系统目录时 adb 只会报 Permission denied，
            # 转成可读提示，前端直接展示
            if "permission denied" in low:
                # debuggable 应用（测试包）私有目录可以走 run-as，先试一把再放弃
                ra = self._run_as_of(path)
                if ra:
                    return self._list_dir_runas(serial, path, ra, raw)
                return {"ok": False, "path": path, "entries": [], "readable": False,
                        "needRoot": True,
                        "error": ("该目录需要 Root 权限，普通 adb 无法访问（%s）。"
                                  "debug 包可在地址栏直接输入 /data/data/<包名>/... 进入应用私有目录"
                                  % path)}
            if "no such file or directory" in low:
                return {"ok": False, "path": path, "entries": [], "readable": False,
                        "error": "目录不存在或已被删除（%s）" % path}
            return {"ok": False, "path": path, "entries": [], "readable": False, "error": raw}
        return {"ok": True, "path": path, "entries": entries, "readable": True}

    def _ios_file_op(self, serial, where, bundle_id, fn, app_fn):
        """iOS 写操作统一入口：带 bundleId 走应用容器，否则走 AFC 媒体域。"""
        guard = self._ios_guard()
        if guard:
            return guard
        try:
            (app_fn if bundle_id else fn)()
            action_log.record(where, "ios %s%s" % (where, "（应用容器）" if bundle_id else ""),
                              serial=serial, ok=True)
            return {"ok": True}
        except Exception as e:  # noqa: BLE001
            return self._ios_err(where, e)

    def _ios_list_dir(self, serial, path, bundle_id=None):
        """iOS 列目录：不带 bundleId 走 AFC 媒体域，带则走应用容器（HouseArrest）。"""
        guard = self._ios_guard()
        if guard:
            return dict(guard, path=path, entries=[], readable=False)
        # iOS 没有 /sdcard，AFC 的根就是 "/"
        if not path or path == "/sdcard":
            path = "/"
        try:
            entries = (self.ios.list_app_dir(serial, bundle_id, path) if bundle_id
                       else self.ios.list_dir(serial, path))
            return {"ok": True, "path": path, "entries": entries, "readable": True}
        except Exception as e:  # noqa: BLE001
            return dict(self._ios_err("list_dir", e), path=path, entries=[], readable=False)

    def storage_stats(self, serial=None):
        serial = serial or self.current_serial
        if self.demo:
            # 未连接设备：容量显示 "—"（ok=False），不出演示数据
            return {"total": 0, "used": 0, "free": 0, "categories": [],
                    "categoriesReady": True, "ok": False}
        if self._platform_of(serial) == "ios":
            guard = self._ios_guard()
            if guard:
                return dict(guard, ok=False, total=0, used=0, free=0)
            try:
                info = self.ios.storage_stats(serial)
            except Exception as e:  # noqa: BLE001
                return dict(self._ios_err("storage_stats", e), total=0, used=0, free=0)
            # iOS 没有 du 可跑，不做分类占用扫描（前端按 categoriesReady 不再等）
            info["categories"] = []
            info["categoriesReady"] = True
            return info
        if self._platform_of(serial) == "harmony":
            info = self.hdc.storage_stats(serial)
            cache = self._storage_cache or {}
            if cache.get("serial") == serial:
                info["categories"] = cache.get("categories", [])
                info["categoriesReady"] = cache.get("ready", False)
            else:
                info["categories"] = []
                info["categoriesReady"] = False
                self._scan_storage(serial)
            return info
        info = self.adb.storage_stats(serial)
        cache = self._storage_cache or {}
        if cache.get("serial") == serial:
            info["categories"] = cache.get("categories", [])
            info["categoriesReady"] = cache.get("ready", False)
        else:
            info["categories"] = []
            info["categoriesReady"] = False
            self._scan_storage(serial)
        return info

    def _scan_storage(self, serial):
        """分类占用靠 du 统计，较慢，放后台跑；前端按 categoriesReady 决定是否再拉一次。"""
        if self._storage_scanning:
            return
        self._storage_scanning = True

        def work():
            groups = [
                ("应用", "blue", ["/sdcard/Android"]),
                ("图片视频", "purple", ["/sdcard/DCIM", "/sdcard/Pictures", "/sdcard/Movies"]),
                ("音频", "orange", ["/sdcard/Music", "/sdcard/Podcasts", "/sdcard/Ringtones"]),
                ("下载文档", "cyan", ["/sdcard/Download", "/sdcard/Documents"]),
            ]
            is_har = self._platform_of(serial) == "harmony"
            if is_har:
                # 鸿蒙没有 /sdcard：目录前缀换到 shell 可见的用户存储根
                groups = [(l, c, [self._har_path(d) for d in ds]) for l, c, ds in groups]
            cats = []
            used_sum = 0
            try:
                for label, cls, dirs in groups:
                    size = 0
                    for d in dirs:
                        r = self._sh("du -sk %s" % d, serial=serial, timeout=90)
                        m = re.match(r"^(\d+)", (r["stdout"] or "").strip())
                        if m:
                            size += int(m.group(1)) * 1024
                    cats.append({"label": label, "bytes": size, "cls": cls})
                    used_sum += size
                info = self.hdc.storage_stats(serial) if is_har \
                    else self.adb.storage_stats(serial)
                used = info.get("used", 0)
                cats.append({"label": "其他", "bytes": max(0, used - used_sum), "cls": "gray"})
                self._storage_cache = {"serial": serial, "categories": cats, "ready": True}
            except Exception as e:  # noqa
                self._storage_cache = {"serial": serial, "categories": [], "ready": True,
                                       "error": str(e)}
            finally:
                self._storage_scanning = False

        threading.Thread(target=work, daemon=True).start()

    def mkdir(self, path, name, serial=None, bundleId=None):
        serial = serial or self.current_serial
        target = (path.rstrip("/") + "/" + name)
        if self.demo:
            action_log.record("mkdir", "adb shell mkdir -p %s" % target, serial=serial, ok=True)
            return {"ok": True, "path": target}
        if self._platform_of(serial) == "ios":
            r = self._ios_file_op(
                serial, "mkdir", bundleId,
                lambda: self.ios.mkdir(serial, target),
                lambda: self.ios.app_mkdir(serial, bundleId, target))
            if r.get("ok"):
                r["path"] = target
            return r
        if self._platform_of(serial) == "harmony":
            target = self._har_path(target)
        r = self._sh("mkdir -p '%s'" % target, serial=serial, timeout=20)
        action_log.record("mkdir", r["cmd"], serial=serial, exit_code=r["exitCode"], ok=r["ok"])
        return {"ok": r["ok"], "path": target, "error": "" if r["ok"] else r["stderr"]}

    def rename(self, path, new_name, serial=None, bundleId=None):
        serial = serial or self.current_serial
        parent = path.rsplit("/", 1)[0]
        target = parent + "/" + new_name
        if self.demo:
            action_log.record("rename", "adb shell mv %s %s" % (path, target), serial=serial, ok=True)
            return {"ok": True, "path": target}
        if self._platform_of(serial) == "ios":
            r = self._ios_file_op(
                serial, "rename", bundleId,
                lambda: self.ios.rename(serial, path, target),
                lambda: self.ios.app_rename(serial, bundleId, path, target))
            if r.get("ok"):
                r["path"] = target
            return r
        if self._platform_of(serial) == "harmony":
            path = self._har_path(path)
            target = self._har_path(target)
        r = self._sh("mv '%s' '%s'" % (path, target), serial=serial, timeout=30)
        action_log.record("rename", r["cmd"], serial=serial, exit_code=r["exitCode"], ok=r["ok"])
        return {"ok": r["ok"], "path": target, "error": "" if r["ok"] else r["stderr"]}

    def move(self, paths, target_dir, serial=None, bundleId=None):
        serial = serial or self.current_serial
        ok_all = True
        errs = []
        for p in paths:
            name = p.rstrip("/").rsplit("/", 1)[-1]
            dst = target_dir.rstrip("/") + "/" + name
            if self.demo:
                action_log.record("move", "adb shell mv %s %s" % (p, dst), serial=serial, ok=True)
                continue
            if self._platform_of(serial) == "ios":
                # AFC 没有 mv，rename 即移动（同分区内有效）
                r = self._ios_file_op(
                    serial, "move", bundleId,
                    lambda: self.ios.rename(serial, p, dst),
                    lambda: self.ios.app_rename(serial, bundleId, p, dst))
                if not r.get("ok"):
                    ok_all = False
                    errs.append((r.get("error") or "")[:120])
                continue
            if self._platform_of(serial) == "harmony":
                p = self._har_path(p)
                dst = self._har_path(dst)
                r = self._sh("mv '%s' '%s'" % (p, dst), serial=serial, timeout=60)
                action_log.record("move", r["cmd"], serial=serial, exit_code=r["exitCode"], ok=r["ok"])
                if not r["ok"]:
                    ok_all = False
                    errs.append((r["stderr"] or "").strip()[:120])
                continue
            r = self.adb.shell("mv '%s' '%s'" % (p, dst), serial=serial, timeout=60)
            action_log.record("move", r["cmd"], serial=serial, exit_code=r["exitCode"], ok=r["ok"])
            if not r["ok"]:
                ok_all = False
                errs.append((r["stderr"] or "").strip()[:120])
        return {"ok": ok_all, "error": "; ".join(errs)}

    def remove(self, paths, secure=False, serial=None, bundleId=None):
        serial = serial or self.current_serial
        tid = self._new_task("remove", "安全擦除 %d 项" % len(paths) if secure else "删除 %d 项" % len(paths))
        t = self.tasks[tid]
        t.total = len(paths)
        if self._platform_of(serial) == "ios":
            threading.Thread(target=self._remove_ios_worker,
                             args=(t, serial, list(paths), secure, bundleId),
                             daemon=True).start()
        else:
            threading.Thread(target=self._remove_worker,
                             args=(t, serial, list(paths), secure),
                             daemon=True).start()
        return {"taskId": tid}

    def _remove_ios_worker(self, t, serial, paths, secure, bundle_id=None):
        """iOS 删除：AFC rm（force 递归）。安全擦除在 iOS 上做不到，明确提示。"""
        if secure:
            t.status = "failed"
            t.phase = "iOS 不支持安全擦除"
            t.error = "iOS 的文件域由系统管理，无法随机覆写；请改用普通删除"
            return
        guard = self._ios_guard()
        if guard:
            t.status = "failed"
            t.phase = "iOS 支持未启用"
            t.error = guard["error"] + "（" + guard.get("hint", "") + "）"
            return
        done = 0
        errs = []
        for p in paths:
            if t.cancel:
                t.status = "cancelled"
                return
            t.phase = "正在删除… %s" % p.rsplit("/", 1)[-1]
            t.log("ios rm %s" % p)
            try:
                if bundle_id:
                    self.ios.app_remove(serial, bundle_id, p)
                else:
                    self.ios.remove(serial, p)
                action_log.record("remove", "ios rm %s" % p, serial=serial, ok=True)
            except Exception as e:  # noqa: BLE001
                action_log.record("remove", "ios rm %s" % p, serial=serial, ok=False,
                                  output=str(e))
                errs.append(self._ios_err_text(e)[:120])
            done += 1
            t.done = done
            t.percent = int(done / max(1, len(paths)) * 100)
        t.status = "success" if not errs else "failed"
        t.error = "; ".join(errs)
        t.phase = "已完成 %d/%d" % (done, len(paths))

    def _remove_worker(self, t, serial, paths, secure):
        if self._platform_of(serial) == "harmony":
            paths = [self._har_path(p) for p in paths]
        done = 0
        errs = []
        for p in paths:
            if t.cancel:
                t.status = "cancelled"
                return
            if self.demo:
                steps = 6 if secure else 3
                for i in range(1, steps + 1):
                    if t.cancel:
                        t.status = "cancelled"
                        return
                    t.phase = ("正在覆写随机数据…" if secure else "正在删除…") + " %s" % p.rsplit("/", 1)[-1]
                    t.percent = int((done + i / steps) / len(paths) * 100)
                    t.log("adb shell %s %s" % ("shred/rm" if secure else "rm -rf", p))
                    time.sleep(0.2)
                done += 1
                t.done = done
                action_log.record("erase" if secure else "remove",
                                  "adb shell %s %s" % ("dd+rm" if secure else "rm -rf", p),
                                  serial=serial, ok=True, output="演示模式")
                t.status = "success"
                t.phase = "已完成 %d/%d" % (done, len(paths))
                return
            if secure:
                size = self._stat_size(serial, p)
                blocks = max(1, min(3, int(size / (1024 * 1024)) + 1))
                for i in range(blocks):
                    if t.cancel:
                        t.status = "cancelled"
                        return
                    t.phase = "正在覆写随机数据… (%d/%d) %s" % (i + 1, blocks, p.rsplit("/", 1)[-1])
                    self._sh("dd if=/dev/urandom of='%s' bs=1M count=1" % p,
                             serial=serial, timeout=120)
                    t.percent = int((done + (i + 1) / blocks) / len(paths) * 100)
            r = self._sh("rm -rf '%s'" % p, serial=serial, timeout=60)
            action_log.record("erase" if secure else "remove", r["cmd"], serial=serial,
                              exit_code=r["exitCode"], output=(r["stdout"] or r["stderr"])[:200],
                              ok=r["ok"])
            if not r["ok"]:
                errs.append((r["stderr"] or r["stdout"]).strip()[:120])
            done += 1
            t.done = done
            t.percent = int(done / len(paths) * 100)
        t.status = "success" if not errs else "failed"
        t.error = "; ".join(errs)
        t.phase = "已完成 %d/%d" % (done, len(paths))

    def push(self, local_paths, remote_dir, overwrite=True, serial=None, bundleId=None):
        serial = serial or self.current_serial
        tid = self._new_task("push", "上传 %d 个文件" % len(local_paths))
        t = self.tasks[tid]
        t.total = sum(self._local_size(p) for p in local_paths)
        if self._platform_of(serial) == "ios":
            threading.Thread(target=self._push_ios_worker,
                             args=(t, serial, list(local_paths), remote_dir, bundleId),
                             daemon=True).start()
            return {"taskId": tid}
        threading.Thread(target=self._push_worker,
                         args=(t, serial, list(local_paths), remote_dir, overwrite),
                         daemon=True).start()
        return {"taskId": tid}

    def _push_ios_worker(self, t, serial, paths, remote_dir, bundle_id=None):
        guard = self._ios_guard()
        if guard:
            t.status = "failed"
            t.phase = "iOS 支持未启用"
            t.error = guard["error"] + "（" + guard.get("hint", "") + "）"
            return
        total = t.total or 1
        sent = 0
        errs = []
        for p in paths:
            name = os.path.basename(p.rstrip("\\/"))
            t.phase = "正在上传 %s…" % name
            t.log("ios push %s -> %s" % (name, remote_dir))

            def _cb(src, dst, _n=name):
                t.log("  %s -> %s" % (src, dst))

            try:
                if bundle_id:
                    self.ios.app_push(serial, bundle_id, p, remote_dir)
                else:
                    self.ios.push(serial, p, remote_dir, on_progress=_cb)
                action_log.record("push", "ios push %s %s" % (name, remote_dir),
                                  serial=serial, ok=True)
            except Exception as e:  # noqa: BLE001
                action_log.record("push", "ios push %s %s" % (name, remote_dir),
                                  serial=serial, ok=False, output=str(e))
                errs.append(self._ios_err_text(e)[:120])
            sent += self._local_size(p)
            t.done = int(sent)
            t.percent = min(100, int(sent / total * 100))
        t.status = "success" if not errs else "failed"
        t.error = "; ".join(errs)
        t.percent = 100
        t.phase = "上传完成" if not errs else "有文件失败"

    def _push_worker(self, t, serial, paths, remote_dir, overwrite):
        is_har = self._platform_of(serial) == "harmony"
        if is_har:
            remote_dir = self._har_path(remote_dir)
        total = t.total or 1
        sent = 0
        errs = []
        for i, p in enumerate(paths):
            if t.cancel:
                t.status = "cancelled"
                return
            name = os.path.basename(p.rstrip("\\/"))
            dst = remote_dir.rstrip("/") + "/" + name
            size = self._local_size(p)
            if self.demo:
                t.phase = "正在上传 %s…" % name
                t.log("adb push %s %s" % (os.path.basename(p), dst))
                acc = 0
                start = time.time()
                while acc < size and not t.cancel:
                    time.sleep(0.15)
                    acc = min(size, acc + max(size * 0.12, 1024))
                    t.done = int(sent + acc)
                    t.percent = min(100, int((sent + acc) / total * 100))
                    t.speed = "%.1f MB/s" % (acc / max(0.15, time.time() - start) / 1024 / 1024)
                if t.cancel:
                    t.status = "cancelled"
                    return
                sent += size
                t.status = "success"
                t.phase = "上传完成"
                continue
            if not overwrite:
                r = self._sh("ls '%s'" % dst, serial=serial, timeout=10)
                if r["ok"] and name in r["stdout"]:
                    base, ext = os.path.splitext(name)
                    dst = remote_dir.rstrip("/") + "/" + base + "_1" + ext
            t.phase = "正在上传 %s…" % name
            t.log(("hdc file send" if is_har else "adb push") + " %s %s" % (os.path.basename(p), dst))
            if is_har:
                cmd = self.hdc.build_cmd(["file", "send", p, dst], target=serial)
            else:
                cmd = self.adb.build_cmd(["push", p, dst], serial=serial)
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        text=True, encoding="utf-8", errors="replace",
                                        creationflags=_no_window() if IS_WIN else 0)
            except Exception as e:  # noqa
                errs.append(str(e))
                continue
            start = time.time()
            last = 0
            while proc.poll() is None:
                if t.cancel:
                    proc.kill()
                    t.status = "cancelled"
                    return
                got = self._stat_size(serial, dst)
                if got != last:
                    t.done = int(sent + got)
                    t.percent = min(100, int((sent + got) / total * 100))
                    t.speed = "%.1f MB/s" % (got / max(0.3, time.time() - start) / 1024 / 1024)
                    last = got
                time.sleep(0.25)
            out = (proc.stdout.read() if proc.stdout else "") or ""
            action_log.record("push", " ".join(cmd), serial=serial, exit_code=proc.returncode,
                              output=out.strip()[:200], ok=proc.returncode == 0)
            t.log(out.strip().replace("\n", " | ")[:200])
            if proc.returncode != 0:
                errs.append(out.strip()[:120])
            sent += size
            t.done = int(sent)
        t.status = "cancelled" if t.cancel else ("success" if not errs else "failed")
        t.error = "; ".join(errs)
        t.percent = 100
        t.phase = "上传完成" if not errs else "有文件失败"

    def pull(self, remote_paths, local_dir, serial=None, bundleId=None):
        serial = serial or self.current_serial
        tid = self._new_task("pull", "下载 %d 项" % len(remote_paths))
        t = self.tasks[tid]
        plat = self._platform_of(serial)
        worker = (self._pull_ios_worker if plat == "ios"
                  else self._pull_harmony_worker if plat == "harmony"
                  else self._pull_worker)
        threading.Thread(target=worker,
                         args=(t, serial, list(remote_paths), local_dir, bundleId),
                         daemon=True).start()
        return {"taskId": tid}

    def _pull_harmony_worker(self, t, serial, paths, local_dir, bundle_id=None):
        """鸿蒙下载：`hdc file recv`（文件/目录都支持），目录落地后照旧打 zip。

        hdc 没有 exec-out，无法像 Android 那样 tar 流式下载。
        """
        os.makedirs(local_dir, exist_ok=True)
        errs = []
        har_paths = [self._har_path(p) for p in paths]
        for i, hp in enumerate(har_paths):
            if t.cancel:
                t.status = "cancelled"
                return
            name = hp.rstrip("/").rsplit("/", 1)[-1]
            t.phase = "正在下载 %s…" % name
            t.log("hdc file recv %s -> %s" % (name, local_dir))
            is_dir = self._remote_is_dir(serial, hp)
            total = self.hdc.stat_size(serial, hp)
            t.total = max(0, total)
            local_path = os.path.join(local_dir, name)
            cmd = self.hdc.build_cmd(["file", "recv", hp, local_dir], target=serial)
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        text=True, encoding="utf-8", errors="replace",
                                        creationflags=_no_window() if IS_WIN else 0)
            except Exception as e:  # noqa
                errs.append(str(e))
                continue
            start = time.time()
            last = -1
            while proc.poll() is None:
                if t.cancel:
                    proc.kill()
                    t.status = "cancelled"
                    return
                try:
                    got = (os.path.getsize(local_path) if os.path.isfile(local_path)
                           else self._tree_size(local_path))
                    if got != last:
                        t.done = got
                        t.percent = min(99, int(got / max(1, total) * 99))
                        t.speed = "%.1f MB/s" % (got / max(0.3, time.time() - start) / 1024 / 1024)
                        last = got
                except OSError:
                    pass
                time.sleep(0.25)
            out = (proc.stdout.read() if proc.stdout else "") or ""
            action_log.record("pull", " ".join(cmd), serial=serial, exit_code=proc.returncode,
                              output=out.strip()[:200], ok=proc.returncode == 0)
            t.log(("下载完成：" + name) if proc.returncode == 0 else ("失败：" + out.strip()[:120]))
            if proc.returncode != 0:
                errs.append(out.strip()[:120])
            t.percent = int((i + 1) / len(paths) * 100)

        zipped = self._zip_folders(local_dir, har_paths, t)
        t.status = "cancelled" if t.cancel else ("success" if not errs else "failed")
        t.error = "; ".join(errs)
        t.percent = 100
        t.phase = "下载完成" if not errs else "有文件失败"
        t.result = {"localDir": local_dir, "zipped": zipped}

    @staticmethod
    def _tree_size(root):
        """目录本地落地大小（文件夹 recv 的进度用）。"""
        total = 0
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                try:
                    total += os.path.getsize(os.path.join(dirpath, fn))
                except OSError:
                    pass
        return total

    def _pull_ios_worker(self, t, serial, paths, local_dir, bundle_id=None):
        guard = self._ios_guard()
        if guard:
            t.status = "failed"
            t.phase = "iOS 支持未启用"
            t.error = guard["error"] + "（" + guard.get("hint", "") + "）"
            return
        errs = []
        done = 0
        for p in paths:
            name = p.rstrip("/").rsplit("/", 1)[-1]
            t.phase = "正在下载 %s…" % name
            t.log("ios pull %s -> %s" % (name, local_dir))

            def _cb(src, dst, _n=name):
                t.log("  %s" % os.path.basename(str(dst)))

            try:
                if bundle_id:
                    self.ios.app_pull(serial, bundle_id, p, local_dir)
                else:
                    self.ios.pull(serial, p, local_dir, on_progress=_cb)
                action_log.record("pull", "ios pull %s %s" % (p, local_dir),
                                  serial=serial, ok=True)
            except Exception as e:  # noqa: BLE001
                action_log.record("pull", "ios pull %s %s" % (p, local_dir),
                                  serial=serial, ok=False, output=str(e))
                errs.append(self._ios_err_text(e)[:120])
            done += 1
            t.done = done
            t.percent = int(done / max(1, len(paths)) * 100)
        t.status = "success" if not errs else "failed"
        t.error = "; ".join(errs)
        t.percent = 100
        t.phase = "下载完成" if not errs else "有文件失败"

    @staticmethod
    def _run_as_of(path):
        """识别应用私有目录路径，返回 (包名, 相对路径)。

        匹配 /data/data/<pkg>[/...] 与 /data/user/0/<pkg>[/...]；
        rel 相对应用数据目录（run-as 启动后 cwd 即该目录），根目录为 ""。
        非 run-as 可达路径返回 None。
        """
        m = re.match(r"^/data/(?:data|user/\d+)/([A-Za-z0-9_.]+)(/(.*))?$", path.rstrip("/"))
        if not m:
            return None
        return m.group(1), m.group(3) or ""

    def _list_dir_runas(self, serial, path, ra, plain_err):
        """用 run-as 列应用私有目录；失败时给出可操作提示。"""
        pkg, rel = ra
        entries, r = self.adb.list_dir_runas(serial, pkg, rel or ".", path)
        if r["ok"]:
            return {"ok": True, "path": path, "entries": entries,
                    "readable": True, "viaRunAs": True}
        raw = (r["stderr"] or r["stdout"]).strip()[:200]
        low = raw.lower()
        if "not debuggable" in low:
            return {"ok": False, "path": path, "entries": [], "readable": False,
                    "needRoot": True,
                    "error": ("%s 是 release 包（非 debuggable），run-as 不可用，"
                              "需 Root 或换 debug 包（%s）" % (pkg, path))}
        if "no such file or directory" in low:
            return {"ok": False, "path": path, "entries": [], "readable": False,
                    "error": "目录不存在或已被删除（%s）" % path}
        return {"ok": False, "path": path, "entries": [], "readable": False,
                "needRoot": True, "error": raw or plain_err[:150]}

    def _remote_is_dir(self, serial, path):
        if self._platform_of(serial) == "harmony":
            path = self._har_path(path)
            r = self.hdc.shell("ls -ld %s" % self.adb._q(path), target=serial, timeout=15)
            return bool(r["ok"]) and r["stdout"].lstrip().startswith("d")
        ra = self._run_as_of(path)
        if ra:
            pkg, rel = ra
            r = self.adb.shell("run-as %s ls -ld %s" % (pkg, self.adb._q(rel or ".")),
                               serial=serial, timeout=15)
            return bool(r["ok"]) and r["stdout"].lstrip().startswith("d")
        r = self.adb.shell("ls -ld '%s'" % path, serial=serial, timeout=15)
        return bool(r["ok"]) and r["stdout"].lstrip().startswith("d")

    def _remote_size(self, serial, path):
        if self._platform_of(serial) == "harmony":
            return self.hdc.stat_size(serial, self._har_path(path))
        ra = self._run_as_of(path)
        if ra:
            pkg, rel = ra
            r = self.adb.shell("run-as %s du -sk %s" % (pkg, self.adb._q(rel or ".")),
                               serial=serial, timeout=60)
        else:
            r = self.adb.shell("du -sk '%s'" % path, serial=serial, timeout=60)
        m = re.match(r"^(\d+)", (r["stdout"] or "").strip())
        return int(m.group(1)) * 1024 if m else 0

    def _stream_pull(self, serial, remote, local_path, is_dir, task=None, total=0, runas=None):
        """用 `adb exec-out` 流式下载。

        Windows 版 adb 的 `adb pull` 对中文远程路径有编码 bug：
        落盘文件名会丢扩展名（`中文.txt` -> `中文`），带空格时直接失败。
        exec-out 原样透传字节流，本地文件名由我们自己决定，绕开该问题。

        runas=(包名, 相对路径) 时走 `run-as`（debuggable 应用私有目录，
        run-as 的 cwd 即应用数据目录，tar/cat 用相对路径）。
        """
        base = self.adb.build_cmd([], serial=serial)
        if runas:
            pkg, rel = runas
            rel = rel or "."
            # 注意：参数必须拆成独立 argv、不能手动加引号——
            # adb 客户端会对含空格的参数自行转义，手动引号会被 run-as
            # 当成可执行文件名的一部分（"exec failed for tar -cf - '...'"）。
            if is_dir:
                args = base + ["exec-out", "run-as", pkg, "tar", "-cf", "-", rel]
            else:
                args = base + ["exec-out", "run-as", pkg, "cat", rel]
        elif is_dir:
            args = base + ["exec-out", "tar", "-cf", "-", "-C", remote, "."]
        else:
            args = base + ["exec-out", "cat", remote]
        if is_dir:
            tmp_out = local_path + ".tar"
        else:
            tmp_out = local_path
        started = time.time()
        err = ""
        with open(tmp_out, "wb") as f:
            try:
                proc = subprocess.Popen(args, stdout=f, stderr=subprocess.PIPE,
                                        creationflags=_no_window() if IS_WIN else 0)
            except Exception as e:  # noqa
                return False, "启动失败：%s" % e
            while proc.poll() is None:
                if task and task.cancel:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    return False, "已取消"
                if task and total:
                    try:
                        got = os.path.getsize(tmp_out)
                        task.done = got
                        task.percent = min(99, int(got / max(1, total) * 99))
                        task.speed = "%.1f MB/s" % (got / max(0.3, time.time() - started) / 1024 / 1024)
                    except OSError:
                        pass
                time.sleep(0.2)
            err = ""
            if proc.stderr:
                try:
                    err = (proc.stderr.read() or b"").decode("utf-8", "replace")[:200]
                except Exception:
                    err = ""
        if proc.returncode != 0:
            return False, err or ("exec-out 失败，退出码 %s" % proc.returncode)
        if is_dir:
            try:
                os.makedirs(local_path, exist_ok=True)
                with tarfile.open(tmp_out, "r:") as tf:
                    tf.extractall(local_path)
                os.remove(tmp_out)
            except Exception as e:  # noqa
                return False, "解包失败：%s" % e
        return True, ""

    def _pull_worker(self, t, serial, paths, local_dir, bundle_id=None):
        # bundle_id 仅 iOS 沙盒用；Android 下载走 AFC 之外的 exec-out，忽略该参数。
        # 不能省：pull 分发器统一按 6 参传（iOS/Android 同形），否则 TypeError 卡 running。
        os.makedirs(local_dir, exist_ok=True)
        errs = []
        for i, p in enumerate(paths):
            if t.cancel:
                t.status = "cancelled"
                return
            name = p.rstrip("/").rsplit("/", 1)[-1]
            t.phase = "正在下载 %s…" % name
            t.log("adb exec-out cat %s > %s" % (p, os.path.join(local_dir, name)))
            if self.demo:
                for j in range(1, 6):
                    if t.cancel:
                        t.status = "cancelled"
                        return
                    t.percent = int((i + j / 5) / len(paths) * 100)
                    time.sleep(0.18)
                t.status = "success"
                t.phase = "下载完成"
                continue
            is_dir = self._remote_is_dir(serial, p)
            total = self._remote_size(serial, p)
            ra = self._run_as_of(p.rstrip("/"))
            t.total = total
            local_path = os.path.join(local_dir, name)
            ok, err = self._stream_pull(serial, p.rstrip("/"), local_path, is_dir,
                                        t, total, runas=ra)
            action_log.record("pull", "adb exec-out %s %s%s" % (
                "tar" if is_dir else "cat", p, "（run-as %s）" % ra[0] if ra else ""),
                serial=serial, exit_code=0 if ok else 1, output=err[:200], ok=ok)
            t.log(("下载完成：" + name) if ok else ("失败：" + err))
            if not ok and not ra:
                # 兜底：老设备没有 exec-out / tar 时退回 adb pull（run-as 路径 pull 也读不到，不回退）
                t.log("回退 adb pull %s" % p)
                r = self.adb.run(["pull", p, local_dir], serial=serial, timeout=900)
                if not r["ok"]:
                    errs.append(((r["stderr"] or "").strip() or err)[:120])
            t.percent = int((i + 1) / len(paths) * 100)

        # 文件夹自动打包成 zip（adb pull 会把整个目录拉下来）
        zipped = self._zip_folders(local_dir, paths, t)
        t.status = "cancelled" if t.cancel else ("success" if not errs else "failed")
        t.error = "; ".join(errs)
        t.percent = 100
        t.phase = "下载完成" if not errs else "有文件失败"
        t.result = {"localDir": local_dir, "zipped": zipped}

    @staticmethod
    def _zip_folders(local_dir, paths, task=None):
        """把刚 pull 下来的文件夹打包成 zip，返回打包文件名列表。"""
        zipped = []
        for p in paths or []:
            name = p.rstrip("/").rsplit("/", 1)[-1]
            folder = os.path.join(local_dir, name)
            if not os.path.isdir(folder):
                continue
            if task:
                task.phase = "正在打包 %s.zip …" % name
            try:
                zip_path = shutil.make_archive(folder, "zip", root_dir=folder)
                zipped.append(os.path.basename(zip_path))
                if task:
                    task.log("打包完成：%s" % os.path.basename(zip_path))
            except Exception as e:  # noqa
                if task:
                    task.log("打包失败：%s" % e)
        return zipped

    # =================================================================== 任务/工具
    def _new_task(self, kind, title):
        with self._lock:
            self._tid += 1
            tid = "T%d" % self._tid
            self.tasks[tid] = Task(tid, kind, title)
            return tid

    def get_task(self, task_id):
        t = self.tasks.get(task_id)
        if not t:
            return {"status": "not_found"}
        return t.to_dict()

    def cancel_task(self, task_id):
        t = self.tasks.get(task_id)
        if t:
            t.cancel = True
            return {"ok": True}
        return {"ok": False}

    def get_action_logs(self, limit=100):
        return {"items": action_log.recent(int(limit))}

    # ---------------------------------------------------- 本地文件/目录选择
    def _on_ui_thread(self, fn):
        """把调用调度回 pywebview 的 UI 线程执行。

        pywebview 把 js_api 调用放进独立后台线程（webview/util.py 的 js_bridge_call），
        而 `Window.create_file_dialog` 一路到 platforms/winforms.py 的模块级实现都
        **没有 Invoke**，直接操作 WinForms Form。跨线程弹系统对话框属未定义行为：
        轻则刷 E_NOINTERFACE 噪音，重则对话框不置顶、不显示或卡死。

        调度失败（非 pythonnet 环境、窗口已销毁等）一律退回直接调用，
        保证最差情况退化为改造前的行为，不会把路堵死。
        """
        native = getattr(self._window, "native", None)
        if native is None:
            return fn()
        try:
            from System import Func, Type  # pythonnet（pywebview 已加载 CLR）
        except Exception:  # noqa
            return fn()

        box = {}

        def run():
            try:
                box["value"] = fn()
            except Exception as e:  # noqa
                box["error"] = e

        try:
            native.Invoke(Func[Type](run))
        except Exception:  # noqa
            return fn()
        if "error" in box:
            raise box["error"]
        return box.get("value")

    def choose_file(self, title="选择文件", file_types=None, multiple=False):
        if not self._window:
            return "" if not multiple else []
        import webview
        # pywebview 要求 file_types 是 (str, ...) 元组；前端可能传单个字符串，包一层
        if isinstance(file_types, str):
            file_types = (file_types,)
        mode = webview.OPEN_DIALOG
        if multiple:
            mode |= webview.FileDialog.MULTI if hasattr(webview, "FileDialog") else 0
        res = self._on_ui_thread(
            lambda: self._window.create_file_dialog(
                mode,
                allow_multiple=multiple,
                file_types=file_types or ("APK 文件 (*.apk;*.xapk;*.apks)",),
            )
        )
        # WinForms 的 OPEN_DIALOG 即使单选也返回 tuple/list，如 ('D:\\xx.apk',)；
        # 单选归一化为字符串，多选保持数组
        if not res:
            return [] if multiple else ""
        if not multiple:
            return res[0] if isinstance(res, (list, tuple)) else res
        return list(res)

    def choose_files(self, title="选择文件", multiple=True):
        if not self._window:
            return []
        import webview
        res = self._on_ui_thread(
            lambda: self._window.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=multiple)
        )
        return res or []

    def choose_folder(self, title="选择文件夹"):
        if not self._window:
            return None
        import webview
        res = self._on_ui_thread(
            lambda: self._window.create_file_dialog(webview.FOLDER_DIALOG)
        )
        if isinstance(res, (list, tuple)):
            return res[0] if res else None
        return res

    def save_file_dialog(self, default_name="logcat.txt", file_types=None):
        if not self._window:
            return None
        import webview
        if isinstance(file_types, str):
            file_types = (file_types,)
        res = self._on_ui_thread(
            lambda: self._window.create_file_dialog(
                webview.SAVE_DIALOG,
                save_filename=default_name,
                file_types=file_types or ("文本文件 (*.txt)",),
            )
        )
        # WinForms 的 SAVE_DIALOG 返回 tuple/list（如 ('D:\\xx.txt',)），取消时为 None/空
        if isinstance(res, (list, tuple)):
            return res[0] if res else None
        return res or None

    @staticmethod
    def _local_size(path):
        if os.path.isdir(path):
            s = 0
            for root, _d, files in os.walk(path):
                for f in files:
                    fp = os.path.join(root, f)
                    try:
                        s += os.path.getsize(fp)
                    except OSError:
                        pass
            return s
        try:
            return os.path.getsize(path)
        except OSError:
            return 0

    def expand_local(self, paths):
        """前端拖入文件夹时展开为文件清单。"""
        out = []
        for p in paths or []:
            if os.path.isdir(p):
                for root, _d, files in os.walk(p):
                    for f in files:
                        out.append(os.path.join(root, f))
            else:
                out.append(p)
        return [{"path": p, "name": os.path.basename(p), "size": self._local_size(p)} for p in out]
