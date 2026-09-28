# -*- coding: utf-8 -*-
"""性能采样：CPU/内存/网络/FPS + 每核占用/电池/磁盘IO/温度，Android 与 Harmony。

设计：
- 采样线程常驻，interval 可配（默认 1s）；package 为空 = 整机性能，
  指定包名 = 追加应用维度（/proc/<pid>/stat 差值算 CPU，dumpsys meminfo 取 PSS）
- 每核占用：/proc/stat 的 cpuN 行差值（双平台可读）
- 电池：Android 用 dumpsys battery；Harmony 用 hidumper -s BatteryService
- 磁盘 I/O：Android /proc/diskstats 差值；鸿蒙 shell 无权限，如实不支持
- 温度：Android /sys/class/thermal（按类型匹配）；鸿蒙仅电池温度（电池服务），
  CPU/GPU 温度无权限读取
- FPS：Android 用 dumpsys gfxinfo 的 Total frames rendered 差值；整机/Harmony 暂无
- 导出 JSON（meta + samples）；导入走 pywebview 文件对话框，只读回放
- iOS：IosPerfStream（pmd3 DVT instruments：sysmontap/graphics/networking + 电池），
  见 core/ios_perf.py；iOS 17+ 在 Windows 无隧道不可用

差值类指标（CPU/网络/FPS/每核/磁盘）依赖上一次采样，首帧里它们为 None，前端跳过即可。
"""
import json
import os
import re
import threading
import time

from .adb import _app_base
from . import action_log

MAX_SAMPLES = 7200  # 1s 间隔下 2 小时


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _percent(part, total):
    try:
        if total > 0:
            return round(part / total * 100.0, 1)
    except (TypeError, ZeroDivisionError):
        pass
    return None


def _parse_cpu_lines(out):
    """解析 /proc/stat 的 cpu* 行 -> [(idle, total), ...]，第一项是整机聚合。"""
    stats = []
    for ln in (out or "").splitlines():
        mm = re.match(r"cpu\d*\s+((?:\d+\s*)+)", ln.strip())
        if not mm:
            continue
        vals = [int(x) for x in mm.group(1).split()]
        idle = (vals[3] if len(vals) > 3 else 0) + (vals[4] if len(vals) > 4 else 0)
        stats.append((idle, sum(vals)))
    return stats


def _cores_pct(cur_stats, prev_stats):
    """每核占用率（%）：当前/上一次的 (idle,total) 差值。"""
    out = []
    if not prev_stats:
        return out
    for idx, cur in enumerate(cur_stats[1:]):  # 跳过聚合行
        p = prev_stats[idx + 1] if idx + 1 < len(prev_stats) else None
        if not p:
            continue
        dt = cur[1] - p[1]
        if dt > 0:
            out.append(round((1 - (cur[0] - p[0]) / dt) * 100, 1))
    return out


def _bat_text(charging_status):
    """充电状态码 -> 文案（OpenHarmony / Android dumpsys 语义基本一致）。"""
    return {1: "充电中", 2: "放电中", 3: "未充电", 4: "已充满", 5: "已充满"}.get(
        charging_status)


class PerfMonitor:
    def __init__(self, api):
        self.api = api
        self._lock = threading.Lock()
        self._thread = None
        self._stop_evt = threading.Event()
        self.running = False
        self.package = ""
        self.interval = 1.0
        self.samples = []          # [{t, cpu, cpuApp, memUsed, memTotal, memApp, rx, tx, fps}]
        self.thresholds = {"cpu": 80, "mem": 90}
        self.alarms = []           # [{t, text}]
        self._ios_stream = None    # IosPerfStream（iOS 采样时持有）
        self._trace_rec = None     # IosTraceRecorder（系统 trace 录制）

    # ---------------------------------------------------------------- 生命周期
    def start(self, opts=None):
        opts = opts or {}
        if self.running:
            self.stop()
        plat = self.api._platform_of()
        if self.api.demo:
            return {"ok": False, "error": "演示模式无真实设备"}
        serial = self.api.current_serial
        if plat == "ios":
            # iOS 17+ 需要 TUN 隧道，Windows 不支持；≤16 走 usbmux 直连。
            # 版本号用枚举缓存（零服务调用），拿不到就不拦，让真实通道报准确错误。
            major = None
            for d in (getattr(self.api, "_ios_devices", None) or []):
                if d.get("serial") == serial:
                    m = re.match(r"(\d+)", str(d.get("iosVersion") or ""))
                    major = int(m.group(1)) if m else None
                    break
            if major is not None and major >= 17:
                from .adb import IS_WIN
                if IS_WIN:
                    return {"ok": False,
                            "error": "Windows 平台不支持 iOS 17+ 性能采样（iOS 17+ 需要 TUN 隧道）",
                            "hint": "备选：在 Mac 上以 root 运行 tunneld 后使用本工具"}
        elif plat not in ("android", "harmony"):
            return {"ok": False, "error": "未知平台"}
        try:
            self.interval = max(0.5, min(10.0, float(opts.get("interval") or 1)))
        except (TypeError, ValueError):
            self.interval = 1.0
        self.package = str(opts.get("package") or "").strip()
        th = opts.get("thresholds") or {}
        self.thresholds = {
            "cpu": max(1, min(100, int(th.get("cpu") or 80))),
            "mem": max(1, min(100, int(th.get("mem") or 90))),
        }
        self.samples = []
        self.alarms = []
        self._ios_stream = None
        if plat == "ios":
            from .ios_perf import IosPerfStream
            stream = IosPerfStream(serial, self.package,
                                   interval_ms=int(self.interval * 1000))
            try:
                stream.start()
            except Exception as e:  # noqa: BLE001 - 启动失败立刻回给前端
                msg = getattr(e, "msg", None) or str(e)
                hint = getattr(e, "hint", "")
                out = {"ok": False, "error": "iOS 采样启动失败: %s" % msg}
                if hint:
                    out["hint"] = hint
                return out
            self._ios_stream = stream
        self._stop_evt.clear()
        self.running = True
        self._thread = threading.Thread(target=self._worker,
                                        args=(plat,), daemon=True)
        self._thread.start()
        action_log.record("perf", "start interval=%s pkg=%s" % (self.interval, self.package or "整机"),
                          serial=serial, ok=True)
        return {"ok": True, "interval": self.interval, "package": self.package}

    def stop(self):
        was = self.running
        self.running = False
        self._stop_evt.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=self.interval + 5)
        self._thread = None
        st = self._ios_stream
        if st:
            try:
                st.stop()
            except Exception:  # noqa: BLE001
                pass
            self._ios_stream = None
        if was:
            action_log.record("perf", "stop (%d samples)" % len(self.samples),
                              serial=self.api.current_serial, ok=True)
        return {"ok": True, "count": len(self.samples)}

    def status(self, after=0):
        with self._lock:
            latest = self.samples[-1] if self.samples else None
            tail = [s for s in self.samples if s.get("i", 0) >= after][-600:]
        return {
            "running": self.running,
            "package": self.package,
            "interval": self.interval,
            "count": len(self.samples),
            "latest": latest,
            "samples": tail,
            "alarms": self.alarms[-20:],
            "thresholds": self.thresholds,
            "iosError": (self._ios_stream.error if self._ios_stream else "") or "",
        }

    # ---------------------------------------------------------------- 采样线程
    def _worker(self, plat):
        prev = None  # 差值基准 {"cpu": (idle,total), "app": (utime,stime), "rx": n, "tx": n, "frames": n}
        i = 0
        while not self._stop_evt.is_set():
            t0 = time.time()
            try:
                fn = self._sample_ios if plat == "ios" else (
                    self._sample_android if plat == "android" else self._sample_harmony)
                sample, prev = fn(prev, i)
            except Exception:  # noqa - 单次采样失败不断流
                sample, prev = {"t": time.time(), "i": i}, prev
            with self._lock:
                self.samples.append(sample)
                if len(self.samples) > MAX_SAMPLES:
                    self.samples = self.samples[-MAX_SAMPLES:]
                self._check_alarm(sample)
            i += 1
            wait = self.interval - (time.time() - t0)
            if wait > 0:
                self._stop_evt.wait(wait)

    def _check_alarm(self, s):
        hits = []
        if s.get("cpu") is not None and s["cpu"] >= self.thresholds["cpu"]:
            hits.append("CPU %s%%" % s["cpu"])
        mem_pct = _percent(s.get("memUsed"), s.get("memTotal"))
        if mem_pct is not None and mem_pct >= self.thresholds["mem"]:
            hits.append("内存 %s%%" % mem_pct)
        if hits:
            self.alarms.append({"t": s["t"], "text": "、".join(hits)})
            if len(self.alarms) > 100:
                self.alarms = self.alarms[-100:]

    # ---------------------------------------------------------------- iOS
    def _sample_ios(self, prev, i):
        """从 IosPerfStream 快照组装样本；网络为累计字节差分出 KB/s。"""
        stream = self._ios_stream
        st = stream.snapshot() if stream else {}
        s = {"t": time.time(), "i": i}
        s["cpu"] = st.get("sys_cpu")
        s["cpuApp"] = st.get("app_cpu")
        total = st.get("sys_mem_total")
        used = st.get("sys_mem_used")
        s["memTotal"] = round(total / (1024 * 1024)) if total else None
        s["memUsed"] = round(used / (1024 * 1024)) if used else None
        am = st.get("app_mem")
        s["memApp"] = round(am / (1024 * 1024), 1) if am else None

        rx, tx = st.get("net_rx"), st.get("net_tx")
        pt = (prev or {}).get("t")
        prx = (prev or {}).get("rx_bytes")
        ptx = (prev or {}).get("tx_bytes")
        if None not in (rx, tx) and pt and None not in (prx, ptx) \
                and rx >= prx and tx >= ptx:
            dt = time.time() - pt
            if dt > 0:
                s["rx"] = round((rx - prx) / dt / 1024.0, 1)
                s["tx"] = round((tx - ptx) / dt / 1024.0, 1)
        prev_out = {"t": s["t"], "rx_bytes": rx, "tx_bytes": tx}

        s["fps"] = st.get("fps")
        s["batLevel"] = st.get("bat_level")
        s["batTemp"] = st.get("bat_temp")
        s["batVolt"] = st.get("bat_volt")
        s["batCurrent"] = st.get("bat_current")
        s["batPower"] = st.get("bat_power")
        s["batCharging"] = st.get("bat_charging") is True
        s["batText"] = st.get("bat_text") or ""
        return s, prev_out

    # ---------------------------------------------------------------- Android
    def _sample_android(self, prev, i):
        adb = self.api.adb
        serial = self.api.current_serial
        # 上一次采样时刻（差值用）。prev 里显式带 t，见下方各 prev 重赋值
        pt = (prev or {}).get("t")

        def sh(cmd, timeout=10):
            return adb.run(["shell", cmd], serial=serial, timeout=timeout)["stdout"]

        s = {"t": time.time(), "i": i}

        # CPU：/proc/stat 差值（第一行整机聚合，cpuN 行每核）
        stats = _parse_cpu_lines(sh("grep ^cpu /proc/stat"))
        if stats:
            agg = stats[0]
            if prev and prev.get("cpu"):
                di = agg[0] - prev["cpu"][0]
                dt = agg[1] - prev["cpu"][1]
                s["cpu"] = round((1 - di / dt) * 100, 1) if dt > 0 else None
            prev = dict(prev or {}, cpu=agg)
            cores = _cores_pct(stats, prev.get("coreStats"))
            if cores:
                s["cores"] = cores
            if stats:
                prev = dict(prev, coreStats=stats)

        # 内存
        mi = sh("cat /proc/meminfo")
        mt = re.search(r"MemTotal:\s+(\d+)", mi or "")
        ma = re.search(r"MemAvailable:\s+(\d+)", mi or "")
        total = _f(mt.group(1)) if mt else None
        avail = _f(ma.group(1)) if ma else None
        if total:
            s["memTotal"] = round(total / 1024)  # MB
            if avail:
                s["memUsed"] = round((total - avail) / 1024)

        # 网络：wlan + 蜂窝聚合差值 -> KB/s
        rx = tx = 0
        out = sh("cat /sys/class/net/*/statistics/rx_bytes 2>/dev/null; "
                 "echo ===; cat /sys/class/net/*/statistics/tx_bytes 2>/dev/null")
        if out:
            parts = out.split("===")
            rx = sum(int(x) for x in re.findall(r"\d+", parts[0] or "")) if len(parts) > 0 else 0
            tx = sum(int(x) for x in re.findall(r"\d+", parts[1] or "")) if len(parts) > 1 else 0
        if prev and prev.get("net") and (rx or tx):
            dt = s["t"] - pt if pt else 0
            if dt > 0:
                s["rx"] = round(max(0, rx - prev["net"][0]) / dt / 1024, 1)
                s["tx"] = round(max(0, tx - prev["net"][1]) / dt / 1024, 1)
        prev = dict(prev or {}, t=s["t"], net=(rx, tx))

        # 电池：dumpsys battery（较重，隔 2 个采样读一次，前端保留最近值）
        if i % 2 == 0:
            b = sh("dumpsys battery", timeout=10)
            if b:
                mm = re.search(r"level:\s*(\d+)", b)
                if mm:
                    s["batLevel"] = int(mm.group(1))
                mm = re.search(r"temperature:\s*(\d+)", b)
                if mm:
                    s["batTemp"] = round(int(mm.group(1)) / 10.0, 1)
                mm = re.search(r"voltage:\s*(\d+)", b)
                if mm:
                    s["batVolt"] = round(int(mm.group(1)) / 1000.0, 2)
                mm = re.search(r"current now:\s*(-?\d+)", b)
                if mm:
                    ma = int(mm.group(1))
                    s["batCurrent"] = abs(ma) / 1000.0
                    if s.get("batVolt"):
                        s["batPower"] = round(abs(ma) / 1000.0 * s["batVolt"], 1)
                mm = re.search(r"status:\s*(\d+)", b)
                if mm:
                    s["batCharging"] = mm.group(1) in ("2", "5")
                    s["batText"] = _bat_text(int(mm.group(1))) or "未知"

        # 磁盘 I/O：/proc/diskstats 扇区差值（512B/扇区）；每 2 个采样读一次
        if i % 2 == 1:
            ds = sh("cat /proc/diskstats 2>/dev/null", timeout=10)
            rd = wr = 0
            for ln in (ds or "").splitlines():
                cols = ln.split()
                if len(cols) < 14 or not re.match(r"(sd[a-z]+|mmcblk\d+.*|nvme\d+n\d+|dm-\d+)$", cols[2]):
                    continue  # 只统计物理/块设备，排除 loop/ram
                rd += int(cols[5])   # 读扇区
                wr += int(cols[9])   # 写扇区
            if rd or wr:
                if prev.get("disk") and pt:
                    dt = s["t"] - pt
                    if dt > 0:
                        s["diskRead"] = round(max(0, rd - prev["disk"][0]) * 512 / dt / 1048576, 2)
                        s["diskWrite"] = round(max(0, wr - prev["disk"][1]) * 512 / dt / 1048576, 2)
                prev = dict(prev or {}, disk=(rd, wr))

        # 温度：/sys/class/thermal 按 type 匹配 CPU/GPU（较重且变化慢，每 5 个采样一次）
        if i % 5 == 2:
            tz = sh("for z in /sys/class/thermal/thermal_zone*; do "
                    "echo \"$(cat $z/type 2>/dev/null)=$(cat $z/temp 2>/dev/null)\"; done", timeout=12)
            best = {}
            for ln in (tz or "").splitlines():
                mm = re.match(r"([\w\-]+)=(-?\d+)", ln.strip())
                if not mm or mm.group(2) == "":
                    continue
                name, val = mm.group(1).lower(), int(mm.group(2))
                if name not in best:  # 第一个命中的 zone 通常最贴近 SoC
                    best[name] = val
            for name, val in best.items():
                c = val / 1000.0
                if -40 < c < 125:
                    if ("cpu" in name or "soc" in name) and "cpuTemp" not in s:
                        s["cpuTemp"] = round(c, 1)
                    elif "gpu" in name and "gpuTemp" not in s:
                        s["gpuTemp"] = round(c, 1)

        # 应用维度
        pkg = self.package
        if pkg:
            pid = None
            r = adb.run(["shell", "pidof -s %s" % pkg], serial=serial, timeout=8)
            if (r["stdout"] or "").strip().isdigit():
                pid = int(r["stdout"].strip())
            if pid:
                st = sh("cat /proc/%d/stat" % pid)
                mm = re.search(r"\) \S+ (-?\d+).*?(\d+) (\d+)", st)
                if mm:
                    utime, stime = int(mm.group(2)), int(mm.group(3))
                    cur = (utime, stime)
                    if prev and prev.get("app"):
                        dt = s["t"] - pt if pt else 0
                        ticks = (cur[0] - prev["app"][0]) + (cur[1] - prev["app"][1])
                        if dt > 0:
                            hz = 100  # USER_HZ
                            ncpu = os.cpu_count() or 1
                            s["cpuApp"] = round(ticks / (dt * hz) * 100, 1)
                            _ = ncpu
                    prev = dict(prev or {}, t=s["t"], app=cur)
                mi2 = sh("dumpsys meminfo %s | grep -E 'TOTAL PSS|TOTAL:' | head -2" % pkg, timeout=15)
                mm2 = re.search(r"TOTAL\s+(\d+)", mi2 or "")
                if mm2:
                    s["memApp"] = round(int(mm2.group(1)) / 1024, 1)
                gi = sh("dumpsys gfxinfo %s | grep 'Total frames rendered'" % pkg, timeout=15)
                gm = re.search(r"Total frames rendered:\s+(\d+)", gi or "")
                if gm:
                    frames = int(gm.group(1))
                    if prev and prev.get("frames") is not None:
                        dt = s["t"] - pt if pt else 0
                        df = frames - prev["frames"]
                        if dt > 0 and df >= 0:
                            s["fps"] = round(df / dt, 1)
                    prev = dict(prev or {}, t=s["t"], frames=frames)
        return s, prev

    # ---------------------------------------------------------------- Harmony
    def _sample_harmony(self, prev, i):
        hdc = self.api.hdc
        serial = self.api.current_serial
        # 上一次采样时刻（差值用），见下方各 prev 重赋值
        pt = (prev or {}).get("t")

        def sh(cmd, timeout=10):
            return hdc.shell(cmd, target=serial, timeout=timeout)["stdout"]

        s = {"t": time.time(), "i": i}
        # CPU：/proc/stat 差值（整机聚合 + 每核）
        stats = _parse_cpu_lines(sh("grep ^cpu /proc/stat"))
        if stats:
            agg = stats[0]
            if prev and prev.get("cpu"):
                di = agg[0] - prev["cpu"][0]
                dt = agg[1] - prev["cpu"][1]
                s["cpu"] = round((1 - di / dt) * 100, 1) if dt > 0 else None
            prev = dict(prev or {}, cpu=agg)
            cores = _cores_pct(stats, prev.get("coreStats"))
            if cores:
                s["cores"] = cores
            if stats:
                prev = dict(prev, coreStats=stats)
        mi = sh("cat /proc/meminfo")
        mt = re.search(r"MemTotal:\s+(\d+)", mi or "")
        ma = re.search(r"MemAvailable:\s+(\d+)", mi or "")
        total = _f(mt.group(1)) if mt else None
        avail = _f(ma.group(1)) if ma else None
        if total:
            s["memTotal"] = round(total / 1024)
            if avail:
                s["memUsed"] = round((total - avail) / 1024)

        # 网络：/proc/net/dev 聚合差值 -> KB/s（/sys/class/net 在鸿蒙 shell 下无权限）
        rx = tx = 0
        nd = sh("cat /proc/net/dev")
        for ln in (nd or "").splitlines():
            mm = re.match(r"\s*([A-Za-z][\w.@:-]*):\s*(.*)", ln)
            if not mm or mm.group(1) == "lo":
                continue  # 回环不计入流量
            nums = re.findall(r"\d+", mm.group(2))
            if len(nums) >= 9:
                rx += int(nums[0])
                tx += int(nums[8])  # 8 个 Receive 字段后是 Transmit bytes
        if prev and prev.get("net") and (rx or tx):
            dt = s["t"] - pt if pt else 0
            if dt > 0:
                s["rx"] = round(max(0, rx - prev["net"][0]) / dt / 1024, 1)
                s["tx"] = round(max(0, tx - prev["net"][1]) / dt / 1024, 1)
        prev = dict(prev or {}, t=s["t"], net=(rx, tx))

        # 电池：hidumper BatteryService（-a -i 才输出数据，鸿蒙 shell 无 power_supply 权限）
        if i % 2 == 0:
            b = sh("hidumper -s BatteryService -a -i", timeout=12)
            if b and "capacity" in b:
                mm = re.search(r"capacity:\s*(\d+)", b)
                if mm:
                    s["batLevel"] = int(mm.group(1))
                mm = re.search(r"temperature:\s*(\d+)", b)
                if mm:
                    s["batTemp"] = round(int(mm.group(1)) / 10.0, 1)
                mm = re.search(r"voltage:\s*(\d+)", b)
                if mm:
                    s["batVolt"] = round(int(mm.group(1)) / 1e6, 2)
                mm = re.search(r"nowCurrent:\s*(-?\d+)", b)
                if mm:
                    ma = int(mm.group(1))
                    s["batCurrent"] = abs(ma) / 1000.0
                    if s.get("batVolt"):
                        s["batPower"] = round(abs(ma) / 1000.0 * s["batVolt"], 1)
                mm = re.search(r"chargingStatus:\s*(\d+)", b)
                if mm:
                    code = int(mm.group(1))
                    s["batCharging"] = code == 1
                    s["batText"] = _bat_text(code) or "未知"

        # 温度：thermal_zone / /sys/block 均无 shell 权限，只有电池温度（上面已带）
        # CPU/GPU 温度如实不支持，前端显示 —

        pkg = self.package
        if pkg:
            r = sh("pidof -s %s" % pkg)
            if r and r.strip().isdigit():
                pid = int(r.strip())
                st = sh("cat /proc/%d/stat" % pid)
                mm = re.search(r"\) \S+ (-?\d+).*?(\d+) (\d+)", st)
                if mm:
                    cur = (int(mm.group(2)), int(mm.group(3)))
                    if prev and prev.get("app"):
                        dt = s["t"] - pt if pt else 0
                        ticks = sum(c - p for c, p in zip(cur, prev["app"]))
                        if dt > 0:
                            s["cpuApp"] = round(ticks / (dt * 100) * 100, 1)
                    prev = dict(prev or {}, t=s["t"], app=cur)
                status = sh("cat /proc/%d/status" % pid)
                vm = _f(re.search(r"VmRSS:\s+(\d+)", status or ""))
                if vm:
                    s["memApp"] = round(vm / 1024, 1)
        return s, prev

    # ---------------------------------------------------------------- 导入导出
    # ---------------------------------------------------------- 系统 trace 录制
    def trace_start(self):
        """录制系统内核 trace（kdebug 流）。iOS 采样页使用，Android 不支持。"""
        if self._trace_rec is not None:
            return {"ok": False, "error": "已有 trace 录制进行中"}
        if self.api._platform_of() != "ios":
            return {"ok": False, "error": "trace 录制仅支持 iOS 设备"}
        if self.api.demo:
            return {"ok": False, "error": "演示模式无真实设备"}
        from .ios_perf import IosTraceRecorder, trace_dir
        path = os.path.join(trace_dir(), "ios_trace_%s.trace" % time.strftime("%Y%m%d_%H%M%S"))
        rec = IosTraceRecorder(self.api.current_serial)
        try:
            rec.start(path)
        except Exception as e:  # noqa: BLE001
            msg = getattr(e, "msg", None) or str(e)
            out = {"ok": False, "error": "trace 启动失败: %s" % msg}
            hint = getattr(e, "hint", "")
            if hint:
                out["hint"] = hint
            return out
        self._trace_rec = rec
        action_log.record("perf", "trace start %s" % path,
                          serial=self.api.current_serial, ok=True)
        return {"ok": True, "name": os.path.basename(path)}

    def trace_stop(self):
        rec = self._trace_rec
        if rec is None:
            return {"ok": False, "error": "没有进行中的 trace 录制"}
        self._trace_rec = None
        try:
            rec.stop()
        except Exception:  # noqa: BLE001
            pass
        size = os.path.getsize(rec.out_path) if os.path.isfile(rec.out_path) else 0
        ok = size > 0
        action_log.record("perf", "trace stop (%dB)" % size,
                          serial=self.api.current_serial, ok=ok)
        return {"ok": ok, "name": os.path.basename(rec.out_path), "size": size,
                "error": "" if ok else "未录制到数据"}

    def export_data(self):
        """导出当前样本到 media/exports/，返回文件路径。"""
        d = os.path.join(_app_base(), "media", "exports")
        os.makedirs(d, exist_ok=True)
        name = "perf_%s.json" % time.strftime("%Y%m%d_%H%M%S")
        path = os.path.join(d, name)
        serial = self.api.current_serial
        dev = {}
        try:
            dev = self.api.get_device_detail(serial) or {}
        except Exception:  # noqa
            pass
        with self._lock:
            payload = {
                "tool": "adb-tool-perf", "version": 1,
                "device": {"serial": serial,
                           "model": dev.get("model") or serial,
                           "platform": self.api._platform_of(serial)},
                "package": self.package, "interval": self.interval,
                "started": self.samples[0]["t"] if self.samples else None,
                "thresholds": self.thresholds,
                "alarms": self.alarms,
                "samples": list(self.samples),
            }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        action_log.record("perf", "export %s (%d samples)" % (name, len(payload["samples"])), ok=True)
        return {"ok": True, "file": path, "name": name, "count": len(payload["samples"])}

    def import_data(self):
        """文件对话框选 JSON，返回数据集给前端只读回放。

        对话框统一走 api.choose_file()：UI 线程安全，且不使用
        pywebview 不支持的 initial_value 参数（旧实现直接 TypeError）。
        """
        start_dir = os.path.join(_app_base(), "media", "exports")
        os.makedirs(start_dir, exist_ok=True)
        path = self.api.choose_file(
            "选择性能数据",
            file_types=("性能数据 (*.json)",),
        )
        if not path:
            return {"ok": False, "error": ""}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("tool") != "adb-tool-perf":
                return {"ok": False, "error": "不是本工具导出的性能数据"}
        except Exception as e:  # noqa
            return {"ok": False, "error": "解析失败: %s" % e}
        action_log.record("perf", "import %s" % os.path.basename(path), ok=True)
        return {"ok": True, "name": os.path.basename(path), "data": data}
