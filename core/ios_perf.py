# -*- coding: utf-8 -*-
"""iOS 应用性能实时流（非越狱，基于 pymobiledevice3 DVT instruments）。

能力与实现通道：
- 整机/应用 CPU、内存    sysmontap（整机 CPUUsage + 进程 CPUUsage/PhysFootprint）
- 屏幕帧率               graphicsopengl（FPS）
- 网络上下行             networking（ConnectionUpdate 按 pid 累计 rx/tx 字节，差分出速率）
- 电量/温度/电压/电流    lockdown com.apple.mobile.battery + diagnostics IORegistry(IOPMPowerSource)

架构：DvtProvider 一条 DTX 连接开多个 channel；常驻事件循环上跑一个主协程，
内部若干消费者 task 把最新值写进 self.latest（线程安全），PerfMonitor 的
采样线程按节拍读取快照组装样本。停止 = cancel 主协程，async with 逐层收链。

限制（与前端提示一致）：
- 采样模式，瞬时峰值可能丢失；采样间隔过小设备会断连（sysmontap 建议 >=500ms）。
- 非越狱拿不到 GPU 硬件 counter / 内核线程调度细节。
- FPS 通道与网络通道随 iOS 版本行为有差异，失败时对应指标置空，不影响其它指标。
- iOS 17+ 在 Windows 无 tunneld 隧道，不可用（调用方负责拦截）。
"""

import os
import re
import threading
import time


def _ci(d, *names):
    """大小写不敏感地取 dict 键。"""
    if not isinstance(d, dict):
        return None
    low = {str(k).lower(): v for k, v in d.items()}
    for n in names:
        if n.lower() in low:
            return low[n.lower()]
    return None


def _find_key(obj, *names):
    """递归在嵌套 dict/list/tuple 里找第一个含指定键的 dict，返回该 dict。"""
    if isinstance(obj, dict):
        low = {str(k).lower() for k in obj}
        for n in names:
            if n.lower() in low:
                return obj
        for v in obj.values():
            r = _find_key(v, *names)
            if r is not None:
                return r
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            r = _find_key(v, *names)
            if r is not None:
                return r
    return None


class IosPerfStream:
    """一条设备上的常驻 DTX 性能流。PerfMonitor 持有并按节拍读 snapshot()。"""

    def __init__(self, udid, bundle_id, interval_ms=1000):
        self.udid = udid
        self.bundle_id = bundle_id or ""
        self.interval_ms = max(500, min(5000, int(interval_ms)))
        self.latest = {
            "sys_cpu": None,          # 整机 CPU %
            "sys_mem_used": None,     # bytes
            "sys_mem_total": None,    # bytes
            "app_cpu": None,          # 目标进程 CPU %
            "app_mem": None,          # PhysFootprint bytes
            "fps": None,
            "net_rx": None,           # 累计字节（差分出速率）
            "net_tx": None,
            "bat_level": None, "bat_temp": None, "bat_volt": None,
            "bat_current": None, "bat_charging": None,
            "cores": None,            # iOS 拿不到每核占用，恒 None
        }
        self.pid = None
        self.error = ""
        self.started_at = 0.0
        self._lock = threading.Lock()
        self._fut = None            # concurrent Future（ios 常驻 loop 上的主协程）
        self._thread = None

    # ------------------------------------------------------------ 对外
    def start(self):
        from .ios import ensure_loop, IosError
        loop = ensure_loop()
        self.started_at = time.time()
        self._thread = threading.Thread(target=self._run_thread, args=(loop,),
                                        name="ios-perf-stream", daemon=True)
        self._thread.start()
        # 等 pid 解析结果（应用没开/不在线要立刻报错给用户）
        deadline = time.time() + 30
        while time.time() < deadline:
            if self.error:
                self.stop()
                raise IosError(self.error)
            if self._fut is not None and self._fut.done():
                err = self.error or ("性能流异常退出" )
                self.stop()
                raise IosError(err)
            with self._lock:
                if self.pid is not None or not self.bundle_id:
                    return
            time.sleep(0.2)
        # pid 没拿到但流还在跑：可能是设备慢，让它继续，采样侧自然无应用数据
        if self.error:
            self.stop()
            raise IosError(self.error)

    def stop(self):
        if self._fut is not None and not self._fut.done():
            self._fut.cancel()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=8)
        self._fut = None
        self._thread = None

    def snapshot(self):
        with self._lock:
            return dict(self.latest)

    # ------------------------------------------------------------ 内部
    def _run_thread(self, loop):
        import asyncio
        fut = asyncio.run_coroutine_threadsafe(self._run(), loop)
        with self._lock:
            self._fut = fut
        try:
            fut.result()
        except Exception as e:  # noqa: BLE001 - 挂到 error 供 start/采样侧读取
            with self._lock:
                self.error = str(e) or type(e).__name__

    async def _run(self):
        import asyncio
        import logging
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider

        try:
            ld = await create_using_usbmux(serial=self.udid)
        except Exception as e:  # noqa: BLE001
            with self._lock:
                self.error = "设备未连接或未信任: %s" % e
            return
        async with ld:
            try:
                async with DvtProvider(ld) as dvt:
                    await self._with_dvt(ld, dvt)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                with self._lock:
                    self.error = "instruments 通道失败: %s" % (getattr(e, "msg", None) or e)

    async def _with_dvt(self, ld, dvt):
        import asyncio
        # 1) 定位目标进程（要求应用已在手机上打开）
        if self.bundle_id:
            from pymobiledevice3.services.dvt.instruments.process_control import ProcessControl
            async with ProcessControl(dvt) as pc:
                pid = await pc.process_identifier_for_bundle_identifier(self.bundle_id)
            if not pid:
                with self._lock:
                    self.error = "目标应用未运行，请先在 iPhone 上打开它再开始采集"
                return
            with self._lock:
                self.pid = pid

        # 2) 三个消费者 + 电池轮询并行跑
        tasks = [asyncio.create_task(self._battery_loop(ld))]
        tasks.append(asyncio.create_task(self._sysmon_loop(dvt)))
        for maker in (self._fps_loop, self._net_loop):
            try:
                tasks.append(asyncio.create_task(maker(dvt)))
            except Exception:  # noqa: BLE001 - 单通道失败不影响其它指标
                pass
        # 单通道崩溃只记录、不带走其它通道
        results = await asyncio.gather(*tasks, return_exceptions=True)
        errs = [r for r in results if isinstance(r, BaseException)
                and not isinstance(r, asyncio.CancelledError)]
        if errs:
            with self._lock:
                if not self.error:
                    self.error = "; ".join("%s: %s" % (type(e).__name__, str(e)[:120])
                                           for e in errs)

    # -------- sysmontap：整机 + 目标进程 CPU/内存
    async def _sysmon_loop(self, dvt):
        """直连 TapChannel 消费 sysmontap。

        不用 pmd3 的 Sysmontap 包装：它在 __aenter__ 里把首条消息当 ack 吃掉，
        老 iOS（11.x）没有 ack，首条就是数据，包装后整条流全部错位成 None。
        """
        import asyncio
        from pymobiledevice3.services.dvt.instruments.device_info import DeviceInfo
        from pymobiledevice3.services.dvt.instruments.tap import TapChannel, TapMessageChannel

        async with DeviceInfo(dvt) as di:
            try:
                hw = await di.hardware_information()
                total = _ci(hw or {}, "totalMemoryBytes", "totalMemory")
                if total:
                    with self._lock:
                        self.latest["sys_mem_total"] = int(total)
            except Exception:  # noqa: BLE001
                pass
            proc_attrs = list(await di.sysmon_process_attributes())
            sys_attrs = list(await di.sysmon_system_attributes())

        ch = TapChannel(dvt, "com.apple.instruments.server.services.sysmontap")
        await ch.connect()
        svc = ch.service
        await svc.set_config_({
            "ur": self.interval_ms,          # 输出频率 ms
            "bm": 0,
            "procAttrs": proc_attrs,
            "sysAttrs": sys_attrs,
            "cpuUsage": True,
            "physFootprint": True,
            "sampleInterval": self.interval_ms * 1_000_000,
        })
        await svc.start()
        mc = TapMessageChannel(svc)
        while True:
            row = await mc.receive_plist()
            if row is None:          # 老系统的空通知，跳过
                continue
            self._absorb_row(row, proc_attrs, sys_attrs)

    def _absorb_row(self, row, proc_attrs, sys_attrs):
        """兼容两种行格式：老 iOS 行是 list[dict]，新 iOS 是单个 dict。"""
        items = row if isinstance(row, list) else [row]
        for item in items:
            if not isinstance(item, dict):
                continue
            l = self.latest
            # ---- 系统 CPU
            scu = item.get("SystemCPUUsage")
            if isinstance(scu, dict):
                v = scu.get("CPUUsage")
                if v is None:
                    v = scu.get("CPU_TotalLoad")
                if isinstance(v, (int, float)):
                    with self._lock:
                        l["sys_cpu"] = round(float(v), 1)
            # ---- 系统 / 进程数组（值按 attrs 列表下标对齐）
            sysvals = item.get("System")
            procs = item.get("Processes")

            def _val(vals, attrs, name):
                if not isinstance(vals, (list, tuple)) or name not in attrs:
                    return None
                v = vals[attrs.index(name)]
                return v if isinstance(v, (int, float)) else None

            if isinstance(sysvals, (list, tuple)) and sys_attrs:
                phys = _val(sysvals, sys_attrs, "physMemSize")
                free = _val(sysvals, sys_attrs, "vmFreeCount")
                with self._lock:
                    # 老 iOS（11.x）physMemSize/vmFreeCount 按"页"计数，字节值不可能小于 1e8
                    if phys and 0 < phys < 10 ** 8:
                        phys *= 16384
                        if free is not None:
                            free *= 16384
                    if phys and phys > 0:
                        l["sys_mem_total"] = int(phys)
                    if phys and free is not None:
                        l["sys_mem_used"] = max(0, int(phys - free))
                # 整机网络累计字节（没选目标应用时用它差分出速率）
                if self.pid is None:
                    nin = _val(sysvals, sys_attrs, "netBytesIn")
                    nout = _val(sysvals, sys_attrs, "netBytesOut")
                    with self._lock:
                        if nin is not None:
                            l["net_rx"] = int(nin)
                        if nout is not None:
                            l["net_tx"] = int(nout)
            if isinstance(procs, dict) and self.pid is not None:
                info = procs.get(self.pid)
                if info is not None:
                    cpu = _val(info, proc_attrs, "CPUUsage")
                    fp = _val(info, proc_attrs, "PhysFootprint")
                    if cpu is None and isinstance(info, dict):
                        cpu = info.get("CPUUsage")
                    if fp is None and isinstance(info, dict):
                        fp = info.get("PhysFootprint")
                    with self._lock:
                        if cpu is not None:
                            l["app_cpu"] = round(float(cpu), 1)
                        if fp is not None:
                            l["app_mem"] = int(fp)

    # -------- graphics：FPS
    async def _fps_loop(self, dvt):
        from pymobiledevice3.services.dvt.instruments.graphics import Graphics
        async with Graphics(dvt) as g:
            async for ev in g:
                hit = _find_key(ev, "FPS")
                if hit is not None:
                    v = _ci(hit, "FPS")
                    if v:
                        with self._lock:
                            self.latest["fps"] = round(float(v), 1)

    # -------- network：目标进程（或整机）累计 rx/tx 字节
    async def _net_loop(self, dvt):
        from pymobiledevice3.services.dvt.instruments.network_monitor import NetworkMonitor
        serial2pid = {}
        rx = 0
        tx = 0
        async with NetworkMonitor(dvt) as nm:
            async for ev in nm:
                kind = getattr(ev, "__class__", None)
                name = kind.__name__ if kind else ""
                if name == "ConnectionDetectionEvent":
                    serial2pid[ev.serial_number] = ev.pid
                elif name == "ConnectionUpdateEvent":
                    # 该版本事件字段是 connection_serial（DetectionEvent 才叫 serial_number）
                    pid = serial2pid.get(ev.connection_serial) \
                        or serial2pid.get(getattr(ev, "serial_number", None))
                    if self.pid is not None and pid != self.pid:
                        continue
                    rx += int(ev.rx_bytes or 0)
                    tx += int(ev.tx_bytes or 0)
                    with self._lock:
                        self.latest["net_rx"] = rx
                        self.latest["net_tx"] = tx

    # -------- 电池：lockdown 电量 + IORegistry 温度/电压/电流
    async def _battery_loop(self, ld):
        import asyncio
        from pymobiledevice3.services.diagnostics import DiagnosticsService
        while True:
            try:
                v = await ld.get_value(domain="com.apple.mobile.battery",
                                       key="BatteryCurrentCapacity")
                if isinstance(v, (int, float)):
                    with self._lock:
                        self.latest["bat_level"] = int(v)
            except Exception:  # noqa: BLE001
                pass
            try:
                async with DiagnosticsService(ld) as diag:
                    bat = await diag.get_battery() or {}
                temp = _ci(bat, "Temperature")
                volt = _ci(bat, "Voltage")
                cur = _ci(bat, "Current")
                if not isinstance(cur, (int, float)):
                    # 老 iOS 没有 Current 字段，Amperage（mA）等价
                    cur = _ci(bat, "Amperage")
                charging = _ci(bat, "IsCharging")
                with self._lock:
                    if isinstance(temp, (int, float)):
                        # IOPMPowerSource Temperature 为百分之一度
                        self.latest["bat_temp"] = round(temp / 100.0, 1)
                    if isinstance(volt, (int, float)) and volt > 0:
                        self.latest["bat_volt"] = round(volt / 1000.0, 2)
                    if isinstance(cur, (int, float)):
                        self.latest["bat_current"] = abs(cur) / 1000.0
                        if self.latest.get("bat_volt"):
                            self.latest["bat_power"] = round(
                                abs(cur) / 1000.0 * self.latest["bat_volt"], 1)
                    if isinstance(charging, bool):
                        self.latest["bat_charging"] = charging
                        self.latest["bat_text"] = "充电中" if charging else "放电中"
            except Exception:  # noqa: BLE001 - 旧系统 IORegistry 可能拒绝，拿不到就置空
                pass
            await asyncio.sleep(10)


class IosTraceRecorder:
    """系统内核 trace 录制（kdebug 原始流，供 Instruments 工具链分析）。"""

    def __init__(self, udid):
        self.udid = udid
        self.out_path = ""
        self.error = ""
        self._fut = None
        self._thread = None

    def start(self, dest_path):
        from .ios import ensure_loop, IosError
        self.out_path = dest_path
        loop = ensure_loop()
        self._thread = threading.Thread(target=self._run_thread, args=(loop,),
                                        name="ios-trace-rec", daemon=True)
        self._thread.start()
        time.sleep(1.0)  # 给启动失败一个快速暴露窗口
        if self.error:
            self.stop()
            raise IosError(self.error)

    def stop(self):
        if self._fut is not None and not self._fut.done():
            self._fut.cancel()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=8)
        self._fut = None
        self._thread = None

    def _run_thread(self, loop):
        import asyncio
        fut = asyncio.run_coroutine_threadsafe(self._run(), loop)
        self._fut = fut
        try:
            fut.result()
        except Exception as e:  # noqa: BLE001
            self.error = str(e) or type(e).__name__

    async def _run(self):
        import asyncio
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
        from pymobiledevice3.services.dvt.instruments.core_profile_session_tap import (
            CoreProfileSessionTap)

        ld = await create_using_usbmux(serial=self.udid)
        async with ld:
            try:
                async with DvtProvider(ld) as dvt:
                    time_config = await CoreProfileSessionTap.get_time_config(dvt)
                    tap = CoreProfileSessionTap(dvt, time_config)
                    async with tap:
                        with open(self.out_path, "wb") as out:
                            try:
                                await tap.dump(out, timeout=None)
                            except asyncio.CancelledError:
                                pass  # 停止录制：文件已按块落盘
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.error = "trace 录制失败: %s" % (getattr(e, "msg", None) or e)


def trace_dir():
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    d = os.path.join(base, "media")
    os.makedirs(d, exist_ok=True)
    return d


def _stamp():
    return time.strftime("%Y%m%d_%H%M%S")
