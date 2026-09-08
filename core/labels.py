# -*- coding: utf-8 -*-
"""应用名（label）解析。

`pm list packages` 不带应用名，`dumpsys package` 在 label 走资源时也不给。
可行方案是用宿主机的 aapt/aapt2 解析 APK 的 application-label。

设计：
- aapt 是**可选依赖**，找不到就降级为包名（不报错、不阻塞）；
- 结果按 `serial:package` 缓存到 ~/.adb_tool/labels.json，一次解析长期复用；
- 只为当前页（默认 20 个）解析，不全量，避免首次打开卡死。
"""
import json
import os
import re
import shutil
import subprocess
import threading
import time

CACHE_FILE = os.path.join(os.path.expanduser("~"), ".adb_tool", "labels.json")
_LOCK = threading.Lock()
_CACHE = None
_CACHE_DIRTY = False


def _load_cache():
    global _CACHE
    with _LOCK:
        if _CACHE is None:
            try:
                with open(CACHE_FILE, "r", encoding="utf-8") as f:
                    _CACHE = json.load(f)
            except Exception:
                _CACHE = {}
        return _CACHE


def _save_cache():
    global _CACHE_DIRTY
    if not _CACHE_DIRTY:
        return
    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(_CACHE or {}, f, ensure_ascii=False)
        _CACHE_DIRTY = False
    except Exception:
        pass


def find_aapt():
    """定位 aapt / aapt2。返回 (可执行文件路径, 类型 aapt|aapt2) 或 (None, None)。

    查找顺序：PATH > 便携目录（随包分发，给没装 build-tools 的电脑）> Android SDK。
    便携目录与 adb 共用 `public_settings\\adb\\`，把 aapt2.exe 丢进去即可生效。
    """
    for exe, kind in (("aapt2", "aapt2"), ("aapt", "aapt")):
        p = shutil.which(exe)
        if p:
            return p, kind
    try:
        from core.adb import portable_adb_dirs  # 与 adb 共用便携目录
        for d in portable_adb_dirs():
            for name, kind in (("aapt2.exe", "aapt2"), ("aapt.exe", "aapt"),
                               ("aapt2", "aapt2"), ("aapt", "aapt")):
                p = os.path.join(d, name)
                if os.path.isfile(p):
                    return p, kind
    except Exception:  # noqa
        pass
    for env_key in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        base = os.environ.get(env_key)
        if not base:
            continue
        bt = os.path.join(base, "build-tools")
        if not os.path.isdir(bt):
            continue
        # 取版本号最大的目录
        versions = sorted([d for d in os.listdir(bt) if os.path.isdir(os.path.join(bt, d))], reverse=True)
        for v in versions:
            for name, kind in (("aapt2.exe", "aapt2"), ("aapt.exe", "aapt"),
                               ("aapt2", "aapt2"), ("aapt", "aapt")):
                p = os.path.join(bt, v, name)
                if os.path.exists(p):
                    return p, kind
    return None, None


def _run(cmd, timeout=30):
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
        return p.returncode, (p.stdout or "")
    except Exception:
        return -1, ""


def _label_from_aapt(aapt, kind, apk_path):
    """解析 application-label / application-label-zh。"""
    code, out = _run([aapt, "dump", "badging", apk_path], timeout=40)
    if code != 0 or not out:
        return None
    # 优先中文标签
    m = re.search(r"application-label-zh(?:-CN)?:\s*'([^']*)'", out)
    if m and m.group(1).strip():
        return m.group(1).strip()
    m = re.search(r"application-label:\s*'([^']*)'", out)
    if m and m.group(1).strip():
        return m.group(1).strip()
    return None


class LabelResolver:
    def __init__(self, adb, aapt=None, aapt_kind=None):
        self.adb = adb
        self.aapt, self.kind = (aapt, aapt_kind) if aapt else find_aapt()
        self.available = bool(self.aapt)
        self._tmp = os.path.join(os.path.expanduser("~"), ".adb_tool", "tmp")
        os.makedirs(self._tmp, exist_ok=True)

    def resolve(self, serial, package, code_path=""):
        cache = _load_cache()
        key = "%s|%s" % (serial, package)
        if key in cache:
            return cache[key]
        if not self.available or not code_path:
            return None
        # 拉取 base.apk 到临时目录再解析（/data/app 下的 apk 本身世界可读）
        local = os.path.join(self._tmp, "%s_%s.apk" % (serial.replace(":", "_").replace(".", "_"),
                                                       package.replace(".", "_")))
        try:
            r = self.adb.run(["pull", code_path, local], serial=serial, timeout=120)
            if not r["ok"] or not os.path.exists(local):
                return None
            label = _label_from_aapt(self.aapt, self.kind, local)
            if label:
                cache[key] = label
                global _CACHE_DIRTY
                _CACHE_DIRTY = True
                _save_cache()
            return label
        except Exception:
            return None
        finally:
            try:
                if os.path.exists(local):
                    os.remove(local)
            except OSError:
                pass

    def resolve_batch(self, serial, items, workers=4):
        """items: [{package, codePath}]，并发解析，返回 {package: label}。"""
        out = {}
        if not items:
            return out
        try:
            from concurrent.futures import ThreadPoolExecutor
        except ImportError:
            for it in items:
                out[it["package"]] = self.resolve(serial, it["package"], it.get("codePath", ""))
            return out
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {
                ex.submit(self.resolve, serial, it["package"], it.get("codePath", "")): it["package"]
                for it in items
            }
            for f, pkg in futs.items():
                try:
                    out[pkg] = f.result()
                except Exception:
                    out[pkg] = None
        return out

    def status(self):
        return {
            "available": self.available,
            "tool": self.aapt or "",
            "kind": self.kind or "",
            "hint": "" if self.available else
                    "未检测到 aapt/aapt2，应用名将显示为包名；安装 Android build-tools 或把 aapt2 加入 PATH 后可解析真实应用名"
        }


def recent_usage_stamp():
    return time.strftime("%Y-%m-%d %H:%M:%S")
