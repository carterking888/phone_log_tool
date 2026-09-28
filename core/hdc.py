# -*- coding: utf-8 -*-
"""hdc 命令封装（纯血鸿蒙 HarmonyOS NEXT）：接口风格对齐 core.adb.Adb。

hdc 是 OpenHarmony 的设备连接工具（DevEco Studio 自带，或 OpenHarmony SDK
的 toolchains 目录），纯血鸿蒙（HarmonyOS 5+/NEXT）不再兼容 ADB，只能走 hdc。

常用命令对照：
    adb devices          ->  hdc list targets
    adb shell <cmd>      ->  hdc -t <key> shell <cmd>
    adb install xx.apk   ->  hdc -t <key> install -r xx.hap
    adb uninstall xx     ->  hdc -t <key> uninstall xx
    pm list packages     ->  bm dump -a
    dumpsys package xx   ->  bm dump -n xx
    getprop xx           ->  param get xx

所有输出统一 utf-8 + errors='replace'，避免 Windows GBK 中文乱码。
"""
import os
import re
import shutil
import subprocess
import threading

from .adb import IS_WIN, _no_window, _app_base

HDC_EXE = "hdc.exe" if IS_WIN else "hdc"

# 常见安装位置（找不到时兜底探测）。
# {} 占位 USERNAME / ~ 占位家目录，find_hdc 里展开。
COMMON_PATHS = [
    r"C:\software\hdc\hdc.exe",
    r"C:\OpenHarmony\Sdk\toolchains\hdc.exe",
    r"C:\Program Files\Huawei\DevEco Studio\sdk\default\openharmony\toolchains\hdc.exe",
    r"C:\Users\{}\AppData\Local\OpenHarmony\Sdk\toolchains\hdc.exe",
    r"C:\Users\{}\AppData\Local\Huawei\Sdk\openharmony\toolchains\hdc.exe",
    "~/Library/OpenHarmony/Sdk/toolchains/hdc",
    "/opt/homebrew/bin/hdc",
    "/usr/local/bin/hdc",
    "/usr/bin/hdc",
]


def portable_hdc_dirs():
    """随包分发的便携 hdc 目录候选（与 adb 的 public_settings/adb 同一约定）。

        <分发根>/public_settings/hdc/hdc.exe   # 与程序目录同级（开发时的形态）
        <程序目录>/public_settings/hdc/hdc.exe # 拷进程序目录里
        <程序目录>/hdc/hdc.exe
    """
    base = _app_base()
    dirs = []
    for d in (base, os.path.dirname(base)):
        if not d:
            continue
        dirs.append(os.path.join(d, "public_settings", "hdc"))
        dirs.append(os.path.join(d, "hdc"))
    return dirs


def find_hdc(configured=None):
    """定位 hdc 可执行文件，返回 (路径, 来源)。

    优先级：配置 > PATH > **便携目录** > 常见路径 > "hdc"
    来源：config | path | portable | common | fallback
    """
    if configured and os.path.exists(configured):
        return configured, "config"
    p = shutil.which("hdc")
    if p:
        return p, "path"
    for d in portable_hdc_dirs():
        cand = os.path.join(d, HDC_EXE)
        if os.path.isfile(cand) and (IS_WIN or os.access(cand, os.X_OK)):
            return cand, "portable"
    for c in COMMON_PATHS:
        c = os.path.expanduser(c)
        try:
            c = c.format(os.environ.get("USERNAME", ""))
        except (KeyError, IndexError):
            continue
        if os.path.exists(c) and (IS_WIN or os.access(c, os.X_OK)):
            return c, "common"
    return "hdc", "fallback"


class Hdc:
    def __init__(self, hdc_path=None):
        self.path, self.source = find_hdc(hdc_path)
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ 基础
    def build_cmd(self, args, target=None):
        """hdc 用 -t <key> 指定目标设备（等价 adb -s <serial>）。"""
        cmd = [self.path]
        if target:
            cmd += ["-t", target]
        cmd += [str(a) for a in args]
        return cmd

    def run(self, args, target=None, timeout=30):
        cmd = self.build_cmd(args, target)
        try:
            p = subprocess.run(
                cmd,
                capture_output=True,
                timeout=timeout,
                encoding="utf-8",
                errors="replace",
                creationflags=_no_window(),
            )
            out = (p.stdout or "").replace("\r\n", "\n")
            err = (p.stderr or "").replace("\r\n", "\n")
            ok = p.returncode == 0
            # hdc 的毛病：失败也返回 exit code 0，错误文本 "[Fail]E000002]..."
            # 打在 stdout 上。不拦在这里，param get 会把错误串当属性值
            # 一路带到设备型号/详情页。设备侧输出极少以 [Fail] 开头，误伤可忽略。
            if ok and out.lstrip().startswith("[Fail]"):
                ok = False
            # 不存在的子命令（如旧版没有 tdisconnect）会打印整屏帮助后
            # exit 0，同样视为失败，避免把帮助文本当成功输出
            if ok and "Unknown operation command" in out:
                ok = False
            return dict(ok=ok, exitCode=p.returncode, stdout=out, stderr=err,
                        cmd=" ".join(cmd))
        except subprocess.TimeoutExpired:
            return dict(ok=False, exitCode=-1, stdout="", stderr="timeout after %ss" % timeout,
                        cmd=" ".join(cmd))
        except FileNotFoundError:
            return dict(ok=False, exitCode=-2, stdout="", stderr="hdc 未找到: %s" % self.path,
                        cmd=" ".join(cmd))
        except Exception as e:  # noqa
            return dict(ok=False, exitCode=-3, stdout="", stderr="%s: %s" % (type(e).__name__, e),
                        cmd=" ".join(cmd))

    def shell(self, sh_cmd, target=None, timeout=30):
        # hdc shell 后面的内容必须作为一个整体参数传（hdc 自己会拼），不能按空格拆：
        # 拆开会丢引号语义，`ls "Program Files"` 这类命令必错。
        return self.run(["shell", sh_cmd], target=target, timeout=timeout)

    def exists(self):
        r = self.run(["list", "targets"], timeout=10)
        return r["ok"], r

    def version(self):
        r = self.run(["version"], timeout=10)
        if not r["ok"]:
            r = self.run(["-v"], timeout=10)
        if not r["ok"]:
            return ""
        m = re.search(r"([\w.\-]+)", r["stdout"].strip())
        return r["stdout"].strip().splitlines()[0] if r["stdout"].strip() else (
            m.group(1) if m else "")

    # ------------------------------------------------------------------ 设备
    def list_targets_v(self):
        """解析 `list targets -v`：返回 [{key, type, state}]。

        输出列（tab 分隔，不同版本列序可能不同）：
            <key>  USB|TCP|UART  Connected|Offline|Ready  <addr>  hdc
        UART 是宿主机串口噪声（COM1/COM9...），不是设备，直接过滤。
        """
        r = self.run(["list", "targets", "-v"], timeout=15)
        out = []
        if not r["ok"]:
            return out
        for line in r["stdout"].splitlines():
            line = line.strip()
            # 有些版本空列表会打印 [Empty] / Empty
            if not line or line.startswith("[") or line.lower() == "empty":
                continue
            cols = [c.strip() for c in line.split("\t") if c.strip()]
            if not cols:
                continue
            key = cols[0]
            typ, state = "", ""
            for c in cols[1:]:
                cu = c.upper()
                if cu in ("USB", "TCP", "UART"):
                    typ = cu
                elif cu in ("CONNECTED", "OFFLINE", "READY"):
                    state = cu.lower()
            if typ == "UART" or re.match(r"^COM\d+$", key, re.I):
                continue
            out.append({"key": key, "type": typ or "USB", "state": state})
        return out

    def list_targets(self):
        """已连接的设备 key 列表（无设备时输出为空）。"""
        return [t["key"] for t in self.list_targets_v()
                if t["state"] not in ("offline",)]

    def devices(self):
        """设备列表，字段结构与 Adb.devices() 对齐。

        state 通过 `param get` 实测：能取到属性才是真在线，
        Offline / 未授权 / 半连接一律如实标 offline（前端会显示"离线"）。
        """
        out = []
        for t in self.list_targets_v():
            r = self.shell("param get const.product.model",
                           target=t["key"], timeout=8)
            ok = r["ok"] and r["stdout"].strip()
            model = r["stdout"].strip() if ok else ""
            blob = ((r["stdout"] or "") + (r["stderr"] or "")).lower()
            if ok:
                state = "device"
            elif "unauthorized" in blob:
                state = "unauthorized"
            else:
                state = "offline"
            out.append({
                "serial": t["key"],
                "state": state,
                "model": model or t["key"],
                "device": "",
                "product": "",
                "transportId": "",
                "transport": "wifi" if (t["type"] == "TCP"
                                        or re.match(r"^\d+\.\d+\.\d+\.\d+:\d+$", t["key"])) else "usb",
            })
        return out

    # ------------------------------------------------------------------ 无线
    def tconn(self, addr, timeout=15):
        """hdc tconn <ip:port>：连接无线设备（手机开发者选项开「无线调试」）。"""
        return self.run(["tconn", addr], timeout=timeout)

    def tdisconnect(self, addr, timeout=10):
        """hdc tdisconnect <ip:port>：断开无线连接。"""
        return self.run(["tdisconnect", addr], timeout=timeout)

    def tmode(self, target, port=10123, timeout=15):
        """hdc tmode <port>：USB 连接时把设备切到 TCP 监听（之后拔线可 tconn）。"""
        return self.run(["tmode", str(port)], target=target, timeout=timeout)

    def param(self, name, target=None, timeout=10):
        """param get <name>：鸿蒙的系统属性（等价 Android getprop）。"""
        r = self.shell("param get %s" % name, target=target, timeout=timeout)
        return r["stdout"].strip() if r["ok"] else ""

    # ------------------------------------------------------------------ 进程
    def pid_map(self, target):
        """PID -> 进程名。鸿蒙 ps -ef 输出列：UID PID PPID ... CMD。"""
        r = self.shell("ps -ef", target=target, timeout=15)
        mapping = {}
        if not r["ok"]:
            return mapping
        for line in r["stdout"].splitlines()[1:]:
            parts = line.split()
            if len(parts) < 2:
                continue
            try:
                pid = int(parts[1])
            except ValueError:
                continue
            name = parts[-1].split(":")[0]
            mapping[pid] = name
        return mapping

    def pidof(self, target, name):
        r = self.shell("pidof -s %s" % name, target=target, timeout=10)
        if r["ok"]:
            s = r["stdout"].strip()
            if s.isdigit():
                return int(s)
        return None

    # ------------------------------------------------------------------ 包管理
    def bm_list(self, target, timeout=60):
        """bm dump -a：全部 bundle 名列表（每行一个）。"""
        r = self.shell("bm dump -a", target=target, timeout=timeout)
        if not r["ok"]:
            return [], r
        pkgs = []
        for line in r["stdout"].splitlines():
            line = line.strip()
            if line and not line.startswith("[") and " " not in line:
                pkgs.append(line)
        return pkgs, r

    def bm_dump(self, target, bundle, timeout=25):
        """bm dump -n <bundle>：bundle 详情。新版输出 JSON，老版 key: value。"""
        r = self.shell("bm dump -n %s" % bundle, target=target, timeout=timeout)
        if not r["ok"]:
            return None, r
        txt = r["stdout"]
        try:
            start = txt.index("{")
            end = txt.rindex("}")
            import json
            return json.loads(txt[start:end + 1]), r
        except (ValueError, Exception):  # noqa: BLE001 - 非 JSON（老版本/未安装）
            return None, r

    # ------------------------------------------------------------------ 文件
    @staticmethod
    def _q(path):
        return "'%s'" % path.replace("'", "'\\''")

    def list_dir(self, target, path, timeout=20):
        """ls -la 解析，条目结构与 Adb.list_dir 对齐（复用其解析器）。

        路径带尾斜杠：符号链接目录不带 / 只会列出链接本身。
        """
        target_path = path.rstrip("/") + "/"
        r = self.shell("ls -la %s" % self._q(target_path), target=target, timeout=timeout)
        if not r["ok"]:
            return [], r
        # hdc 的 shell 失败也返回 exit 0，`ls: ...: No such file or directory`
        # 错误行打在 stdout 上，先拦掉再解析（复用 Adb 的检测）
        from .adb import Adb, Result
        err_line = Adb.ls_error(r["stdout"])
        if err_line:
            return [], Result(False, 0, r["stdout"], err_line, r["cmd"])
        return Adb._parse_ls(r["stdout"], path), r

    def storage_stats(self, target):
        """df 拿用户区容量。NEXT 的 shell 可见用户目录是 /storage/data/local。"""
        info = {"total": 0, "used": 0, "free": 0, "ok": False}
        for mnt in ("/storage/data/local", "/data", "/"):
            r = self.shell("df %s" % mnt, target=target, timeout=15)
            if not r["ok"]:
                continue
            lines = [l for l in r["stdout"].splitlines() if l.strip()]
            if len(lines) < 2:
                continue
            p = lines[1].split()
            try:
                total = int(p[1])
                used = int(p[2])
                free = int(p[3])
            except (ValueError, IndexError):
                continue
            unit = 1024 if total > 100000 else 1
            info.update(total=total * unit, used=used * unit, free=free * unit, ok=True)
            break
        return info

    def stat_size(self, target, path, timeout=15):
        """远端文件字节数（上传进度用）。stat 不可用时退回 du。"""
        q = self._q(path)
        r = self.shell("stat -c %%s %s" % q, target=target, timeout=timeout)
        if r["ok"] and r["stdout"].strip().isdigit():
            return int(r["stdout"].strip())
        r2 = self.shell("du -sk %s" % q, target=target, timeout=timeout)
        m = re.match(r"^(\d+)", (r2["stdout"] or "").strip())
        return int(m.group(1)) * 1024 if m else 0
