# -*- coding: utf-8 -*-
"""adb 命令封装：一次性命令 + 设备信息解析。

所有输出统一 utf-8 + errors='replace'，避免 Windows GBK 中文乱码。
"""
import os
import re
import shutil
import subprocess
import sys
import threading

IS_WIN = os.name == "nt"

# adb 可执行文件名
ADB_EXE = "adb.exe" if IS_WIN else "adb"

# 常见安装位置（找不到时兜底探测）
# Windows 项用 {} 占位 USERNAME；macOS/Linux 项用 ~ 占位家目录（find_adb 里 expanduser）
COMMON_PATHS = [
    r"C:\software\platform-tools\adb.exe",
    r"C:\Android\platform-tools\adb.exe",
    r"C:\Users\{}\AppData\Local\Android\Sdk\platform-tools\adb.exe",
    # macOS：Android Studio 默认装到 ~/Library/Android/sdk，brew 版在 /opt/homebrew
    "~/Library/Android/sdk/platform-tools/adb",
    "~/Android/Sdk/platform-tools/adb",
    "/opt/homebrew/bin/adb",
    "/opt/android-sdk/platform-tools/adb",
    "/usr/local/bin/adb",
    "/usr/bin/adb",
]


def _app_base():
    """应用基准目录：frozen 是 exe 所在目录，源码模式是项目根（core 的上一级）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def portable_adb_dirs():
    """随包分发的便携 adb 目录候选（给没装 Android SDK 的电脑用）。

    支持的摆放方式（两处都会找，程序目录本身和它的上一层）：
        <分发根>/public_settings/adb/adb.exe   # 与程序目录同级（开发时的形态）
        <程序目录>/public_settings/adb/adb.exe # 拷进程序目录里
        <程序目录>/adb/adb.exe
    adb.exe 依赖同目录的 AdbWinApi.dll / AdbWinUsbApi.dll，**必须整目录带**，
    只拷 exe 会出现"能启动但连不上设备"。
    """
    base = _app_base()
    dirs = []
    for d in (base, os.path.dirname(base)):
        if not d:
            continue
        dirs.append(os.path.join(d, "public_settings", "adb"))
        dirs.append(os.path.join(d, "adb"))
    return dirs


def find_adb(configured=None):
    """定位 adb 可执行文件，返回 (路径, 来源)。

    优先级：配置 > PATH > ANDROID_HOME/SDK > **便携目录** > 常见路径 > "adb"
    来源：config | path | sdk | portable | common | fallback
    """
    if configured and os.path.exists(configured):
        return configured, "config"
    p = shutil.which("adb")
    if p:
        return p, "path"
    for env_key in ("ANDROID_HOME", "ANDROID_SDK_ROOT", "ANDROID_PATH"):
        base = os.environ.get(env_key)
        if base:
            cand = os.path.join(base, "platform-tools", ADB_EXE)
            if os.path.exists(cand) and (IS_WIN or os.access(cand, os.X_OK)):
                return cand, "sdk"
    for d in portable_adb_dirs():
        cand = os.path.join(d, ADB_EXE)
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
    return "adb", "fallback"


def _no_window():
    if IS_WIN:
        return subprocess.CREATE_NO_WINDOW
    return 0


class Result(dict):
    """统一结果结构：{ok, exitCode, stdout, stderr, cmd}"""

    def __init__(self, ok, exit_code, stdout="", stderr="", cmd=""):
        super().__init__(
            ok=ok, exitCode=exit_code, stdout=stdout or "", stderr=stderr or "", cmd=cmd
        )


class Adb:
    def __init__(self, adb_path=None):
        self.path, self.source = find_adb(adb_path)
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ 基础
    def build_cmd(self, args, serial=None):
        cmd = [self.path]
        if serial:
            cmd += ["-s", serial]
        cmd += [str(a) for a in args]
        return cmd

    def run(self, args, serial=None, timeout=30, check=False):
        cmd = self.build_cmd(args, serial)
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
            ok = p.returncode == 0 and (not check or not err.strip().lower().startswith("error"))
            return Result(ok, p.returncode, out, err, " ".join(cmd))
        except subprocess.TimeoutExpired:
            return Result(False, -1, "", "timeout after %ss" % timeout, " ".join(cmd))
        except FileNotFoundError:
            return Result(False, -2, "", "adb 未找到: %s" % self.path, " ".join(cmd))
        except Exception as e:  # noqa
            return Result(False, -3, "", "%s: %s" % (type(e).__name__, e), " ".join(cmd))

    def shell(self, sh_cmd, serial=None, timeout=30):
        return self.run(["shell"] + sh_cmd.split(), serial=serial, timeout=timeout)

    def exists(self):
        r = self.run(["version"], timeout=10)
        return r["ok"], r

    def version(self):
        """返回 (版本串, 明细)。"""
        r = self.run(["version"], timeout=10)
        if not r["ok"]:
            return "", r["stderr"] or r["stdout"]
        lines = [l.strip() for l in r["stdout"].strip().splitlines() if l.strip()]
        first = lines[0] if lines else ""
        # 第二行形如 "Version 35.0.2-12147458"；第一行是协议版本，仅作兜底
        for line in lines:
            m = re.match(r"^Version\s+([\w.\-]+)", line, re.I)
            if m:
                return m.group(1), line
        m = re.search(r"version\s+([\w.\-]+)", first, re.I)
        return (m.group(1) if m else first), first

    # ------------------------------------------------------------------ 设备
    def devices(self):
        """解析 adb devices -l。"""
        r = self.run(["devices", "-l"], timeout=15)
        if not r["ok"]:
            return []
        out = []
        started = False
        for line in r["stdout"].splitlines():
            line = line.strip()
            if not line:
                continue
            if line.lower().startswith("list of devices"):
                started = True
                continue
            if not started:
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            serial, state = parts[0], parts[1]
            props = {}
            for p in parts[2:]:
                if ":" in p:
                    k, v = p.split(":", 1)
                    props[k] = v
            transport = "wifi" if re.match(r"^\d+\.\d+\.\d+\.\d+:\d+$", serial) else "usb"
            out.append(
                {
                    "serial": serial,
                    "state": state,  # device / offline / unauthorized / no permissions
                    "model": props.get("model", "").replace("_", " "),
                    "device": props.get("device", ""),
                    "product": props.get("product", ""),
                    "transportId": props.get("transport_id", ""),
                    "transport": transport,
                }
            )
        return out

    def getprops(self, serial, timeout=20):
        """一次性取回全部 getprop，返回 dict。"""
        r = self.shell("getprop", serial=serial, timeout=timeout)
        props = {}
        if not r["ok"]:
            return props
        for line in r["stdout"].splitlines():
            m = re.match(r"^\[([^\]]+)\]:\s*\[(.*)\]$", line.strip())
            if m:
                props[m.group(1)] = m.group(2)
        return props

    def battery(self, serial):
        r = self.shell("dumpsys battery", serial=serial, timeout=15)
        info = {"level": None, "status": "", "powered": False, "charging": False}
        if not r["ok"]:
            return info
        for line in r["stdout"].splitlines():
            line = line.strip()
            if line.startswith("level:"):
                try:
                    info["level"] = int(line.split(":", 1)[1].strip())
                except ValueError:
                    pass
            elif line.startswith("status:"):
                try:
                    info["status"] = {2: "充电中", 3: "放电中", 4: "未充电", 5: "已充满"}.get(
                        int(line.split(":", 1)[1].strip()), ""
                    )
                except ValueError:
                    pass
            elif line.startswith("AC powered:") and line.endswith("true"):
                info["powered"] = True
            elif line.startswith("USB powered:") and line.endswith("true"):
                info["powered"] = True
        info["charging"] = info["status"] in ("充电中",)
        return info

    def kernel(self, serial):
        r = self.shell("cat /proc/version", serial=serial, timeout=10)
        return r["stdout"].strip() if r["ok"] else ""

    def is_rooted(self, serial):
        r = self.shell("which su", serial=serial, timeout=10)
        if r["ok"] and "su" in r["stdout"] and "/su" in r["stdout"]:
            return True
        r2 = self.shell("id -u", serial=serial, timeout=10)
        return r2["ok"] and r2["stdout"].strip() == "0"

    def pid_map(self, serial):
        """PID -> 进程名（多数情况即包名）。"""
        r = self.shell("ps -A", serial=serial, timeout=15)
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

    def pidof(self, serial, package):
        r = self.shell("pidof -s %s" % package, serial=serial, timeout=10)
        if r["ok"]:
            s = r["stdout"].strip()
            if s.isdigit():
                return int(s)
        return None

    def server_status(self):
        """探测 5037 端口是否有监听。"""
        import socket

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.4)
        try:
            s.connect(("127.0.0.1", 5037))
            return True
        except OSError:
            return False
        finally:
            s.close()

    # ------------------------------------------------------------------ 包管理
    def list_packages(self, serial, kind="all", with_path=False):
        """kind: all / third / system / disabled / enabled

        with_path=True 时使用 -f，返回 [(包名, apk 路径)]，供应用名解析取 base.apk。
        """
        flag = {"all": "", "third": "-3", "system": "-s", "disabled": "-d", "enabled": "-e"}.get(
            kind, ""
        )
        args = ["shell", "pm", "list", "packages"]
        if with_path:
            args.append("-f")
        if flag:
            args.append(flag)
        r = self.run(args, serial=serial, timeout=60)
        pkgs = []
        if r["ok"]:
            for line in r["stdout"].splitlines():
                line = line.strip()
                if not line.startswith("package:"):
                    continue
                body = line[len("package:"):].strip()
                if "=" in body:  # -f 输出：package:/data/app/.../base.apk=com.xxx
                    apk, _, pkg = body.rpartition("=")
                    pkgs.append((pkg.strip(), apk.strip()))
                else:
                    pkgs.append((body, ""))
        return pkgs, r

    def dump_package(self, serial, package, timeout=25):
        """dumpsys package <pkg>：解析版本 / UID / targetSdk / 目录 / 权限。"""
        r = self.shell("dumpsys package %s" % package, serial=serial, timeout=timeout)
        info = {"versionName": "", "versionCode": "", "uid": "", "targetSdk": "",
                "codePath": "", "dataDir": "", "firstInstallTime": "", "lastUpdateTime": "",
                "permissions": [], "granted": 0, "requested": 0, "system": False,
                "enabled": True, "installer": ""}
        if not r["ok"] or "Unable to find package" in r["stdout"]:
            return info, r
        txt = r["stdout"]
        m = re.search(r"versionCode=(\d+)", txt)
        if m:
            info["versionCode"] = m.group(1)
        m = re.search(r"versionName=([^\s]+)", txt)
        if m:
            info["versionName"] = m.group(1)
        m = re.search(r"targetSdk=(\d+)", txt)
        if m:
            info["targetSdk"] = m.group(1)
        m = re.search(r"userId=(\d+)", txt)
        if m:
            info["uid"] = m.group(1)
        m = re.search(r"codePath=(\S+)", txt)
        if m:
            info["codePath"] = m.group(1)
        m = re.search(r"dataDir=(\S+)", txt)
        if m:
            info["dataDir"] = m.group(1)
        m = re.search(r"firstInstallTime=(\S+)", txt)
        if m:
            info["firstInstallTime"] = m.group(1)
        m = re.search(r"lastUpdateTime=(\S+)", txt)
        if m:
            info["lastUpdateTime"] = m.group(1)
        m = re.search(r"pkgFlags=\[\s*([^\]]*)\]", txt)
        if m:
            flags = m.group(1)
            info["system"] = "SYSTEM" in flags
            info["enabled"] = "STOPPED" not in flags
        # 权限：requested permissions 区块
        req = []
        m = re.search(r"requested permissions:\s*\n((?:\s+\S.*\n)+)", txt)
        if m:
            for line in m.group(1).splitlines():
                name = line.strip().split(":")[0].strip()
                if name.startswith("android.permission"):
                    req.append(name)
        # 授权状态：runtime permissions 区块
        granted_map = {}
        m = re.search(r"runtime permissions:\s*\n((?:\s+\S.*\n)+)", txt)
        if m:
            for line in m.group(1).splitlines():
                line = line.strip()
                if ":" not in line:
                    continue
                name, rest = line.split(":", 1)
                granted_map[name.strip()] = "granted=true" in rest
        info["requested"] = len(req)
        perms = []
        for name in req:
            short = name.replace("android.permission.", "")
            g = granted_map.get(name, "(SYSTEM|HAS_SYSTEM)" in txt and "SYSTEM" in txt and False)
            perms.append({"name": short, "full": name, "granted": bool(g)})
        info["permissions"] = perms
        info["granted"] = sum(1 for p in perms if p["granted"])
        return info, r

    def app_size(self, serial, package, code_path="", data_dir=""):
        """估算应用占用：代码 + 数据 + 缓存。失败返回 None 字段。"""
        out = {"appSize": 0, "dataSize": 0, "cacheSize": 0, "ok": False}
        try:
            if code_path:
                r = self.shell("du -sk %s" % code_path, serial=serial, timeout=20)
                m = re.match(r"^(\d+)", r["stdout"].strip())
                if m:
                    out["appSize"] = int(m.group(1)) * 1024
                    out["ok"] = True
            if data_dir:
                r = self.shell("du -sk %s" % data_dir, serial=serial, timeout=20)
                m = re.match(r"^(\d+)", r["stdout"].strip())
                if m:
                    out["dataSize"] = int(m.group(1)) * 1024
                    out["ok"] = True
            r = self.shell("du -sk /data/data/%s/cache" % package, serial=serial, timeout=15)
            m = re.match(r"^(\d+)", r["stdout"].strip())
            if m:
                out["cacheSize"] = int(m.group(1)) * 1024
        except Exception:
            pass
        return out

    # ------------------------------------------------------------------ 文件
    def list_dir(self, serial, path, timeout=20):
        """ls -la 解析。

        路径必须带尾斜杠：`/sdcard` 是符号链接，`ls -la /sdcard` 只会列出链接本身
        （输出形如 `/sdcard -> /storage/self/primary`），带 `/` 才会列出目录内容。
        """
        target = path.rstrip("/") + "/"
        cmd = "ls -la %s" % self._q(target)
        r = self.shell(cmd, serial=serial, timeout=timeout)
        if not r["ok"]:
            return [], r
        return self._parse_ls(r["stdout"], path), r

    def list_dir_runas(self, serial, package, rel, base_path, timeout=20):
        """run-as 列 debuggable 应用私有目录（非 root 设备访问 /data/data/<pkg>）。

        rel 是相对应用数据目录的路径（run-as 会把 cwd 切到 /data/data/<pkg>），
        根目录传 "."；base_path 是对应的绝对路径，用于生成条目的完整 path 字段。
        """
        cmd = "run-as %s ls -la %s" % (package, self._q(rel))
        r = self.shell(cmd, serial=serial, timeout=timeout)
        if not r["ok"]:
            return [], r
        return self._parse_ls(r["stdout"], base_path), r

    @staticmethod
    def _parse_ls(stdout, base_path):
        """解析 `ls -la` 输出为条目列表；base_path 为条目 path 的绝对前缀。"""
        entries = []
        for line in stdout.splitlines():
            line = line.rstrip()
            if not line or line.startswith("total"):
                continue
            parts = line.split(None, 7)
            if len(parts) < 7:
                continue
            perm, _, owner, group, size, date1, date2 = parts[0], parts[1], parts[2], parts[3], parts[4], parts[5], parts[6]
            name = parts[7] if len(parts) > 7 else ""
            if name in (".", ".."):
                continue
            if " -> " in name:
                name = name.split(" -> ")[0].strip()
            try:
                size_i = int(size)
            except ValueError:
                size_i = 0
            mtime = "%s %s" % (date1, date2.split(".")[0])
            entries.append(
                {
                    "name": name,
                    "path": (base_path.rstrip("/") + "/" + name),
                    "isDir": perm.startswith("d"),
                    "size": size_i,
                    "mtime": mtime,
                    "perm": perm,
                }
            )
        return entries

    def storage_stats(self, serial):
        r = self.shell("df /sdcard", serial=serial, timeout=15)
        info = {"total": 0, "used": 0, "free": 0, "ok": False}
        if r["ok"]:
            lines = [l for l in r["stdout"].splitlines() if l.strip()]
            if len(lines) >= 2:
                p = lines[1].split()
                try:
                    # 多数设备输出 KB 块
                    total = int(p[1])
                    used = int(p[2])
                    free = int(p[3])
                    unit = 1024 if total > 100000 else 1
                    info.update(total=total * 1024, used=used * 1024, free=free * 1024, ok=True)
                except (ValueError, IndexError):
                    pass
        return info

    def stat_size(self, serial, path):
        r = self.shell("stat -c %s %s" % ("%s", self._q(path)), serial=serial, timeout=15)
        if r["ok"] and r["stdout"].strip().isdigit():
            return int(r["stdout"].strip())
        return 0

    @staticmethod
    def _q(path):
        return "'%s'" % path.replace("'", "'\\''")
