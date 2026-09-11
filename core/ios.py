# -*- coding: utf-8 -*-
"""iOS 设备后端（pymobiledevice3，可选依赖）。

两点必须先说清楚，否则照 Android 的写法写必踩：

1. **pymobiledevice3 是纯 async 库**：服务全是 async context manager，
   方法几乎都是 coroutine（`listdir`/`stat`/`pull` 全是 `async def`）。
   这里起一个**常驻后台线程的事件循环**，对外只暴露同步接口
   （js_api 本来就跑在 pywebview 的后台线程，直接阻塞等结果即可）。
2. **iOS 非越狱的能力边界**（决定了哪些功能有、哪些只能提示）：
   - 设备信息 / 应用列表 / 卸载：完整可用（lockdown 服务）
   - 文件：AFC 只能访问媒体域（DCIM、Books、iTunes 等）和app 主动共享的目录，
     **应用沙盒拿不到**（需要挂 DeveloperDiskImage，Windows 上获取麻烦）
   - 实时日志：iOS ≤16 走 syslog_relay；iOS 17+ 必须起 tunnel（管理员权限 + 远程配对）
   权限/配对类失败全部带 `hint`，由 UI 直接展示，禁止静默兜底成假数据。
"""
import asyncio
import atexit
import concurrent.futures
import os
import threading
import time

PACKAGE = "pymobiledevice3"
INSTALL_HINT = "pip install pymobiledevice3"

# macOS 上 iOS 原生可用；Windows 需要装了 iTunes 或 Apple Mobile Device Support
# （提供 usbmux 驱动）。检测不到只是提示，不阻断——也许用户走 WiFi 配对。


class IosUnavailable(Exception):
    """依赖缺失：pymobiledevice3 没装。"""

    def __init__(self, reason="", hint=INSTALL_HINT):
        # 原始 ImportError（No module named ...）是给人看的调试信息，
        # 不进 UI —— 界面只给一句人话 + 安装命令。
        super().__init__(reason or "未安装 %s" % PACKAGE)
        self.reason = reason or "未安装 %s（可选依赖，用于 iOS 设备）" % PACKAGE
        self.hint = hint


class IosError(Exception):
    """设备侧错误：未信任 / 未配对 / 服务不可用等，带可操作提示。"""

    def __init__(self, msg, hint=""):
        super().__init__(msg)
        self.msg = msg
        self.hint = hint


# ------------------------------------------------------------------ 事件循环
_loop = None
_loop_thread = None
_loop_lock = threading.Lock()


def _require():
    """懒加载依赖。缺失时抛 IosUnavailable，不在这里静默返回 None。"""
    try:
        __import__(PACKAGE)
    except ImportError:
        # 不把 "No module named ..." 原文带出去（UI 禁止原始报错噪声）
        raise IosUnavailable()
    return True


def _quiet_mux_loop_handler(loop, context):
    """常驻循环的自定义异常处理器。

    usbmux 端口转发在设备端口未监听（游戏未运行/切后台）时抛 BadDevError /
    ConnectionFailedError，pymobiledevice3 转发器任务未取回异常 → asyncio
    默认处理器把整段栈打印到控制台。这类连接失败属预期行为，CDP 探测已用
    中文提示（「设备 Inspector 端口 xx 未连通…」），这里静默；其余异常照常输出。
    """
    exc = context.get("exception")
    try:
        from pymobiledevice3.exceptions import MuxException
        if isinstance(exc, MuxException):
            return
    except Exception:  # noqa: BLE001 - 未装 pymobiledevice3 时兜底
        pass
    loop.default_exception_handler(context)


def ensure_loop():
    """常驻后台事件循环（daemon 线程）。pymobiledevice3 的服务要在活着的 loop 上跑。"""
    global _loop, _loop_thread
    with _loop_lock:
        if (_loop is not None and not _loop.is_closed() and _loop.is_running()
                and _loop_thread and _loop_thread.is_alive()):
            return _loop
        # Windows 上 proactor 会打断部分阻塞式 socket 操作（pymobiledevice3 官方注释），
        # 官方 utils.get_asyncio_loop 也特判成 SelectorEventLoop，这里保持一致。
        loop = asyncio.SelectorEventLoop() if os.name == "nt" else asyncio.new_event_loop()
        loop.set_exception_handler(_quiet_mux_loop_handler)
        t = threading.Thread(target=loop.run_forever, name="ios-loop", daemon=True)
        t.start()
        _loop, _loop_thread = loop, t
        return loop


def run_async(coro, timeout=None):
    """把 coroutine 丢到常驻 loop 执行，同步拿结果。"""
    loop = ensure_loop()
    fut = asyncio.run_coroutine_threadsafe(coro, loop)
    try:
        return fut.result(timeout)
    except concurrent.futures.TimeoutError:
        raise IosError("操作超时（%s 秒）" % timeout, "设备可能未响应，重插数据线后重试")
    except IosError:
        raise
    except Exception as e:  # noqa: BLE001 - 统一转成带 hint 的错误
        raise _wrap(e)


def _drain_loop_at_exit():
    """进程退出前排空常驻事件循环。

    不排空的话，daemon 线程随进程死亡时 pending 任务直接被销毁，
    刷 'Task was destroyed but it is pending!' 噪声。
    """
    global _loop, _loop_thread
    if _loop is None or _loop.is_closed():
        return

    async def _cancel_all():
        cur = asyncio.current_task()
        tasks = [t for t in asyncio.all_tasks(_loop) if t is not cur]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    try:
        fut = asyncio.run_coroutine_threadsafe(_cancel_all(), _loop)
        try:
            fut.result(5)
        except Exception:  # noqa: BLE001 - 排空尽力而为
            pass
        _loop.call_soon_threadsafe(_loop.stop)
        if _loop_thread is not None:
            _loop_thread.join(timeout=1)
    except Exception:  # noqa: BLE001
        pass


atexit.register(_drain_loop_at_exit)


def _wrap(e):
    """把 pymobiledevice3 的异常翻译成人话 + 可操作提示。"""
    name = type(e).__name__
    text = str(e) or name
    low = (name + " " + text).lower()
    if "notpaired" in low or "pair" in low or "trust" in low:
        return IosError(
            "设备未信任此电脑（%s）" % text,
            "在 iPhone 上点「信任此电脑」并输入锁屏密码，然后重新插拔数据线",
        )
    if "passwordrequired" in low or "locked" in low:
        return IosError("设备已锁屏（%s）" % text, "解锁 iPhone 屏幕后重试")
    if "invalidhostid" in low:
        return IosError("配对记录失效（%s）" % text, "重新插拔并在 iPhone 上点「信任」以重建配对")
    if "connectionfailed" in low or "connectionaborted" in low or "无法连接" in text:
        return IosError(
            "连接失败（%s）" % text,
            "确认已安装 iTunes 或 Apple Mobile Device Support（Windows 需要它提供 usbmux 驱动）",
        )
    if "mdns" in low or "bonjour" in low:
        return IosError(
            "未能通过 Bonjour 发现设备（%s）" % text,
            "iOS 17+ 走 tunnel 依赖 Bonjour：确认已安装 iTunes/Bonjour 服务；WiFi 连接需与电脑同网段",
        )
    if "permission" in low or "denied" in low or "winerror 5" in low:
        return IosError(
            "权限不足（%s）" % text,
            "iOS 17+ 起 tunnel 需要管理员权限：用管理员身份运行本工具",
        )
    return IosError("%s：%s" % (name, text))


def _house_err(e, bundle_id):
    """house_arrest 的失败原因要能让用户判断"是不是这个应用不给看"。"""
    name = type(e).__name__
    text = str(e) or name
    if "ApplicationLookupFailed" in text or "AppNotInstalled" in name:
        return IosError("设备上没有这个应用：%s" % bundle_id, "它可能已被卸载，刷新应用列表后重试")
    if "Permission" in text or "permission" in text.lower():
        return IosError(
            "该应用拒绝访问容器（%s）" % text,
            "非越狱设备只有开启「文件共享」的应用（或开发签名的包）才能被访问",
        )
    return IosError(
        "%s：%s" % (name, text),
        "这个应用可能未开启文件共享；非越狱设备无法进入未共享的应用沙盒",
    )


def _jsonable(obj):
    """递归转成 JSON 可序列化类型。

    pymobiledevice3 的 lockdown all_values 里混着 bytes（如 DevicePublicKey 是
    DER 公钥二进制）、datetime 等 —— pywebview 把 js_api 返回值 json.dumps 回传前端，
    任何一个是 bytes 整个调用就炸 "Object of type bytes is not JSON serializable"。
    规则：bytes 能解码就转 str，否则转 hex；datetime 转 "%Y-%m-%d %H:%M:%S"；
    其余未知对象兜底 str()。
    """
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, (bytes, bytearray)):
        try:
            return obj.decode("utf-8")
        except UnicodeDecodeError:
            return obj.hex()
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(v) for v in obj]
    if hasattr(obj, "strftime"):
        return obj.strftime("%Y-%m-%d %H:%M:%S")
    return str(obj)


def _as_text(v, default=""):
    """单值兜底：bytes/其它非字符串统一转 str，避免日志字段里混入 bytes。"""
    if v is None:
        return default
    if isinstance(v, str):
        return v
    if isinstance(v, (bytes, bytearray)):
        return v.decode("utf-8", "replace")
    return str(v)


def _norm_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _ts(mtime):
    """AFC 的 st_mtime 是 datetime 对象（不是 epoch），老版本也可能是纳秒时间戳。"""
    if mtime is None:
        return ""
    try:
        if hasattr(mtime, "strftime"):
            return mtime.strftime("%Y-%m-%d %H:%M")
        n = float(mtime)
        if n > 1e12:  # 纳秒
            n /= 1e9
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(n))
    except Exception:
        return ""


class Ios:
    """iOS 后端。所有方法都是同步的（内部走常驻 loop）。"""

    platform = "ios"

    # ---------------------------------------------------------------- 能力
    def available(self):
        """(bool, reason)。依赖缺失时 reason 即安装提示。"""
        try:
            _require()
        except IosUnavailable as e:
            return False, e.reason
        return True, ""

    def version(self):
        """(版本串, 明细)——与 Adb.version() 同形，便于 UI 复用。"""
        try:
            _require()
        except IosUnavailable as e:
            return "", e.reason
        try:
            import importlib.metadata as md

            return md.version(PACKAGE), PACKAGE
        except Exception:
            return "unknown", PACKAGE

    # ---------------------------------------------------------------- 设备
    def devices(self, timeout=30):
        """返回与 adb.devices() 同构的列表，额外带 platform='ios'。"""
        _require()
        return run_async(self._devices(), timeout=timeout)

    async def _devices(self):
        from pymobiledevice3 import usbmux
        from pymobiledevice3.lockdown import create_using_usbmux

        out = []
        try:
            mux = await usbmux.list_devices()
        except Exception as e:  # noqa: BLE001
            raise _wrap(e)
        for d in mux:
            item = {
                "serial": d.serial,
                "state": "device",
                "model": "",
                "device": "",
                "product": "",
                "name": "",
                "iosVersion": "",
                "transport": "usb" if (d.connection_type or "").upper() == "USB" else "wifi",
                "transportId": str(d.devid),
                "platform": "ios",
                "detail": "",
            }
            try:
                ld = await create_using_usbmux(serial=d.serial)
                async with ld:
                    v = ld.all_values or {}
                    # 字段统一过 _as_text：设备信息理论上都是 str，防个别固件给 bytes
                    item["model"] = _as_text(ld.display_name) or _as_text(v.get("ProductType"))
                    item["device"] = _as_text(v.get("ProductType"))
                    item["product"] = _as_text(v.get("HardwareModel"))
                    item["name"] = _as_text(v.get("DeviceName"))
                    item["iosVersion"] = _as_text(v.get("ProductVersion"))
            except Exception as e:  # noqa: BLE001
                err = _wrap(e)
                item["state"] = "unauthorized"
                item["detail"] = err.msg
            out.append(item)
        return out

    def device_info(self, udid, timeout=40):
        return run_async(self._device_info(udid), timeout=timeout)

    async def _device_info(self, udid):
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.services.diagnostics import DiagnosticsService
        from pymobiledevice3.services.afc import AfcService

        ld = await create_using_usbmux(serial=udid)
        async with ld:
            v = dict(ld.all_values or {})
            battery = {"level": None, "status": "", "powered": False, "charging": False}
            try:
                async with DiagnosticsService(ld) as diag:
                    raw = await diag.get_battery() or {}
                level = raw.get("CurrentCapacity", raw.get("Level"))
                if level is not None:
                    battery["level"] = int(level)
                battery["charging"] = bool(raw.get("IsCharging") or raw.get("ExternalConnected"))
                battery["powered"] = bool(raw.get("ExternalConnected"))
                battery["status"] = (
                    "已充满" if raw.get("FullyCharged") else
                    ("充电中" if battery["charging"] else "放电中")
                )
            except Exception:  # noqa: BLE001 - 电量拿不到不影响其它字段
                pass

            storage = {"total": 0, "used": 0, "free": 0, "ok": False}
            try:
                async with AfcService(ld) as afc:
                    info = await afc.get_device_info() or {}
                # 实测真机返回的是 FSTotalBytes / FSFreeBytes（字符串），
                # 老版本/其它实现可能给 TotalBytes / FreeBytes，两种都认。
                total = _norm_int(info.get("FSTotalBytes") or info.get("TotalBytes"))
                free = _norm_int(info.get("FSFreeBytes") or info.get("FreeBytes"))
                storage.update(total=total, free=free, used=max(0, total - free), ok=total > 0)
            except Exception:  # noqa: BLE001
                pass

            return {
                "serial": udid,
                "platform": "ios",
                "model": _as_text(ld.display_name) or _as_text(v.get("ProductType")),
                "device": _as_text(v.get("ProductType")),
                "name": _as_text(v.get("DeviceName")),
                "iosVersion": _as_text(v.get("ProductVersion")),
                "buildVersion": _as_text(v.get("BuildVersion")),
                "udid": _as_text(v.get("UniqueDeviceID")) or udid,
                "serialNumber": _as_text(v.get("SerialNumber")),
                "abi": _as_text(v.get("CPUArchitecture")),
                "kernel": "",
                "rooted": False,
                "battery": battery,
                "storage": storage,
                "state": "device",
                "props": _jsonable(v),  # all_values 含 DevicePublicKey(bytes)，必须先转 JSON 安全类型
            }

    # ---------------------------------------------------------------- 应用
    @staticmethod
    def _app_type(kind):
        return {"third": "User", "user": "User", "system": "System", "disabled": "Any"}.get(
            kind, "Any"
        )

    def list_packages(self, udid, kind="all", timeout=120):
        return run_async(self._list_packages(udid, kind), timeout=timeout)

    async def _list_packages(self, udid, kind):
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.services.installation_proxy import InstallationProxyService

        ld = await create_using_usbmux(serial=udid)
        async with ld:
            async with InstallationProxyService(ld) as ips:
                # **不要开 calculate_sizes**：它会对每个应用额外发一次 lookup 让设备
                # 现场统计 StaticDiskUsage/DynamicDiskUsage，应用一多就要几十秒到几分钟
                # （列表页只显示包名/版本，根本用不到）。体积在详情页单独查（单包，
                # 带 bundle_identifiers 只统计一个应用，很快）。
                apps = await ips.get_apps(
                    application_type=self._app_type(kind), calculate_sizes=False
                )
        return [self._fmt_app(bid, info) for bid, info in (apps or {}).items()]

    @staticmethod
    def _fmt_app(bid, info):
        info = info or {}
        app_type = (info.get("ApplicationType") or "").lower()
        size = _norm_int(info.get("StaticDiskUsage")) + _norm_int(info.get("DynamicDiskUsage"))
        return {
            "packageName": bid,
            "exe": _as_text(info.get("CFBundleExecutable")),  # 可执行名=进程名，日志过滤靠它
            "label": (
                info.get("CFBundleDisplayName")
                or info.get("CFBundleName")
                or bid
            ),
            "versionName": info.get("CFBundleShortVersionString", "") or "",
            "versionCode": str(info.get("CFBundleVersion", "") or ""),
            "type": "system" if app_type == "system" else "user",
            "sizeBytes": size,
            "running": False,
            "pid": 0,
            "enabled": True,
            "platform": "ios",
        }

    def app_detail(self, udid, bundle_id, timeout=60):
        return run_async(self._app_detail(udid, bundle_id), timeout=timeout)

    async def _app_detail(self, udid, bundle_id):
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.services.installation_proxy import InstallationProxyService

        ld = await create_using_usbmux(serial=udid)
        async with ld:
            async with InstallationProxyService(ld) as ips:
                apps = await ips.get_apps(
                    application_type="Any", calculate_sizes=True, bundle_identifiers=[bundle_id]
                )
        info = (apps or {}).get(bundle_id)
        if info is None:
            raise IosError("未找到应用：%s" % bundle_id, "它可能已被卸载，刷新列表后重试")
        base = self._fmt_app(bundle_id, info)
        base.update(
            {
                "uid": "",
                "targetSdk": "",
                "minSdk": "",
                "installer": "",
                "firstInstallTime": "",
                "lastUpdateTime": "",
                "appSize": _norm_int(info.get("StaticDiskUsage")),
                "dataSize": _norm_int(info.get("DynamicDiskUsage")),
                "cacheSize": 0,
                "codePath": info.get("Path", ""),
                "dataDir": info.get("Container", ""),
                # iOS 不给第三方读权限列表（需挂开发者镜像解析 entitlements），
                # 留空并在 UI 明示，不要伪造。
                "permissions": [],
                "granted": 0,
                "requested": 0,
                "permissionsUnsupported": True,
            }
        )
        return base

    def uninstall(self, udid, bundle_id, timeout=180):
        return run_async(self._uninstall(udid, bundle_id), timeout=timeout)

    async def _uninstall(self, udid, bundle_id):
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.services.installation_proxy import InstallationProxyService

        ld = await create_using_usbmux(serial=udid)
        async with ld:
            async with InstallationProxyService(ld) as ips:
                await ips.uninstall(bundle_id)
        return {"ok": True}

    def install(self, udid, path, on_progress=None, timeout=900):
        return run_async(self._install(udid, path, on_progress), timeout=timeout)

    async def _install(self, udid, path, on_progress):
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.services.installation_proxy import InstallationProxyService

        def _cb(percent=None, *args, **kwargs):  # pymobiledevice3 的 handler 签名多变
            if on_progress:
                try:
                    on_progress(percent if isinstance(percent, int) else None, args, kwargs)
                except Exception:  # noqa: BLE001
                    pass

        ld = await create_using_usbmux(serial=udid)
        async with ld:
            async with InstallationProxyService(ld) as ips:
                try:
                    await ips.install_from_local(path, handler=_cb)
                except Exception as first:  # noqa: BLE001
                    # 已存在同名应用时 Install 会失败，回退到 Upgrade
                    try:
                        await ips.upgrade(path, handler=_cb)
                    except Exception:  # noqa: BLE001
                        raise _wrap(first)
        return {"ok": True}

    # ---------------------------------------------------------------- 文件
    def list_dir(self, udid, path="/", timeout=60):
        return run_async(self._list_dir(udid, path), timeout=timeout)

    async def _list_dir(self, udid, path):
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.services.afc import AfcService

        path = path or "/"
        ld = await create_using_usbmux(serial=udid)
        async with ld:
            async with AfcService(ld) as afc:
                names = await afc.listdir(path)
                entries = []
                for name in names:
                    if name in (".", ".."):
                        continue
                    full = (path.rstrip("/") + "/" + name) if path != "/" else ("/" + name)
                    size, mtime, is_dir = 0, "", False
                    try:
                        st = await afc.stat(full)
                        size = _norm_int(st.get("st_size") or st.get("size"))
                        mtime = _ts(st.get("st_mtime") or st.get("mtime"))
                        is_dir = "DIR" in str(st.get("st_ifmt", "")).upper()
                    except Exception:  # noqa: BLE001 - 单个条目 stat 失败不拖垮整页
                        pass
                    entries.append(
                        {
                            "name": name,
                            "path": full,
                            "isDir": is_dir,
                            "size": size,
                            "mtime": mtime,
                            "perm": "drwxr-xr-x" if is_dir else "-rw-r--r--",
                        }
                    )
        return entries

    def storage_stats(self, udid, timeout=30):
        return run_async(self._storage_stats(udid), timeout=timeout)

    async def _storage_stats(self, udid):
        info = await self._device_info(udid)
        return info.get("storage") or {"total": 0, "used": 0, "free": 0, "ok": False}

    def mkdir(self, udid, path, timeout=30):
        return run_async(self._mkdir(udid, path), timeout=timeout)

    async def _mkdir(self, udid, path):
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.services.afc import AfcService

        ld = await create_using_usbmux(serial=udid)
        async with ld:
            async with AfcService(ld) as afc:
                await afc.makedirs(path)
        return {"ok": True}

    def rename(self, udid, src, dst, timeout=30):
        return run_async(self._rename(udid, src, dst), timeout=timeout)

    async def _rename(self, udid, src, dst):
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.services.afc import AfcService

        ld = await create_using_usbmux(serial=udid)
        async with ld:
            async with AfcService(ld) as afc:
                await afc.rename(src, dst)
        return {"ok": True}

    def remove(self, udid, path, timeout=120):
        return run_async(self._remove(udid, path), timeout=timeout)

    async def _remove(self, udid, path):
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.services.afc import AfcService

        ld = await create_using_usbmux(serial=udid)
        async with ld:
            async with AfcService(ld) as afc:
                await afc.rm(path, force=True)
        return {"ok": True}

    def pull(self, udid, remote, local_dir, on_progress=None, timeout=1800):
        """下载 remote（文件或目录）到 local_dir。"""
        return run_async(self._pull(udid, remote, local_dir, on_progress), timeout=timeout)

    async def _pull(self, udid, remote, local_dir, on_progress):
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.services.afc import AfcService

        remote = remote.rstrip("/")
        parent, name = os.path.split(remote)
        parent = parent or "/"

        def _cb(src, dst, *args, **kwargs):
            if on_progress:
                try:
                    on_progress(src, dst)
                except Exception:  # noqa: BLE001
                    pass

        ld = await create_using_usbmux(serial=udid)
        async with ld:
            async with AfcService(ld) as afc:
                await afc.pull(
                    name, local_dir, src_dir=parent, callback=_cb if on_progress else None,
                    ignore_errors=False, progress_bar=False,
                )
        return {"ok": True}

    def push(self, udid, local_path, remote_dir, on_progress=None, timeout=1800):
        return run_async(self._push(udid, local_path, remote_dir, on_progress), timeout=timeout)

    async def _push(self, udid, local_path, remote_dir, on_progress):
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.services.afc import AfcService

        def _cb(src, dst, *args, **kwargs):
            if on_progress:
                try:
                    on_progress(src, dst)
                except Exception:  # noqa: BLE001
                    pass

        ld = await create_using_usbmux(serial=udid)
        async with ld:
            async with AfcService(ld) as afc:
                await afc.push(
                    local_path, remote_dir, callback=_cb if on_progress else None,
                    progress_bar=False,
                )
        return {"ok": True}

    # ------------------------------------------------- 应用容器（HouseArrest）
    #
    # AFC 默认只给媒体域。想看应用自己的目录要过 house_arrest：
    #   VendDocuments -> 只给 Documents（要求应用开启「文件共享」）
    #   VendContainer -> 整个容器（一般只有开发签名的包才行）
    # 失败全部翻译成人话，前端据此提示"这个应用不给你看"，不要假装空目录。
    @staticmethod
    def _join_container(root, path):
        """把容器内的相对路径拼到实际根上（documents_only 时根是 /Documents）。"""
        p = (path or "/").strip() or "/"
        if not p.startswith("/"):
            p = "/" + p
        if root in ("/", ""):
            return p.rstrip("/") or "/"
        if p == "/":
            return root
        return root.rstrip("/") + p

    @staticmethod
    def _strip_root(root, full):
        """还原成容器内的相对路径，保证前端拿到的 path 还能原样传回来。"""
        if root not in ("/", "") and full.startswith(root):
            return full[len(root):] or "/"
        return full

    async def _with_house(self, udid, bundle_id, fn, documents_only=True):
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.services.house_arrest import HouseArrestService

        ld = await create_using_usbmux(serial=udid)
        async with ld:
            try:
                ha = await HouseArrestService.create(
                    ld, bundle_id, documents_only=documents_only
                )
            except Exception as e:  # noqa: BLE001
                raise _house_err(e, bundle_id)
            async with ha:
                return await fn(ha, "/Documents" if documents_only else "/")

    def list_app_dir(self, udid, bundle_id, path="/", documents_only=True, timeout=60):
        return run_async(
            self._list_app_dir(udid, bundle_id, path, documents_only), timeout=timeout
        )

    async def _list_app_dir(self, udid, bundle_id, path, documents_only):
        async def _fn(ha, root):
            full = self._join_container(root, path)
            names = await ha.listdir(full)
            entries = []
            for name in names:
                if name in (".", ".."):
                    continue
                sub = self._join_container(full, name)
                size, mtime, is_dir = 0, "", False
                try:
                    st = await ha.stat(sub)
                    size = _norm_int(st.get("st_size") or st.get("size"))
                    mtime = _ts(st.get("st_mtime") or st.get("mtime"))
                    is_dir = "DIR" in str(st.get("st_ifmt", "")).upper()
                except Exception:  # noqa: BLE001
                    pass
                entries.append({
                    "name": name,
                    "path": self._strip_root(root, sub),
                    "isDir": is_dir,
                    "size": size,
                    "mtime": mtime,
                    "perm": "drwxr-xr-x" if is_dir else "-rw-r--r--",
                })
            return entries

        return await self._with_house(udid, bundle_id, _fn, documents_only)

    def app_pull(self, udid, bundle_id, remote, local_dir, documents_only=True,
                 timeout=1800):
        async def _fn(ha, root):
            full = self._join_container(root, remote).rstrip("/")
            parent, name = os.path.split(full)
            await ha.pull(name, local_dir, src_dir=parent or "/",
                          ignore_errors=False, progress_bar=False)
            return {"ok": True}

        return run_async(
            self._with_house(udid, bundle_id, _fn, documents_only), timeout=timeout
        )

    def app_push(self, udid, bundle_id, local_path, remote_dir, documents_only=True,
                 timeout=1800):
        async def _fn(ha, root):
            await ha.push(local_path, self._join_container(root, remote_dir),
                          progress_bar=False)
            return {"ok": True}

        return run_async(
            self._with_house(udid, bundle_id, _fn, documents_only), timeout=timeout
        )

    def app_remove(self, udid, bundle_id, path, documents_only=True, timeout=120):
        async def _fn(ha, root):
            await ha.rm(self._join_container(root, path), force=True)
            return {"ok": True}

        return run_async(
            self._with_house(udid, bundle_id, _fn, documents_only), timeout=timeout
        )

    def app_mkdir(self, udid, bundle_id, path, documents_only=True, timeout=30):
        async def _fn(ha, root):
            await ha.makedirs(self._join_container(root, path))
            return {"ok": True}

        return run_async(
            self._with_house(udid, bundle_id, _fn, documents_only), timeout=timeout
        )

    def app_rename(self, udid, bundle_id, src, dst, documents_only=True, timeout=30):
        async def _fn(ha, root):
            await ha.rename(self._join_container(root, src),
                            self._join_container(root, dst))
            return {"ok": True}

        return run_async(
            self._with_house(udid, bundle_id, _fn, documents_only), timeout=timeout
        )

    # ---------------------------------------------------------------- 其它
    def pid_map(self, udid, timeout=30):
        """尽力而为：拿不到进程表就返回空 dict（前端不显示进程名而已）。"""
        try:
            return run_async(self._pid_map(udid), timeout=timeout)
        except Exception:  # noqa: BLE001
            return {}

    async def _pid_map(self, udid):
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.services.os_trace import OsTraceService

        ld = await create_using_usbmux(serial=udid)
        async with ld:
            async with OsTraceService(ld) as os_trace:
                pids = await os_trace.get_pid_list()
        # pymobiledevice3 11.x 返回的是原始 plist dict：
        # {'Payload': {pid: {'ProcessName': ...}}, 'Status': ...} —— 不是列表！
        payload = pids.get("Payload") if isinstance(pids, dict) else pids
        out = {}
        if isinstance(payload, dict):
            for pid, meta in payload.items():
                n = _norm_int(pid, 0)
                if not n:
                    continue
                if isinstance(meta, dict):
                    name = _as_text(meta.get("ProcessName") or meta.get("processName")
                                    or meta.get("name"))
                else:
                    name = _as_text(meta)
                out[n] = name
        elif isinstance(payload, list):  # 兼容旧版列表形态
            for p in payload:
                pid = _norm_int(p.get("pid") if isinstance(p, dict) else p, 0)
                name = _as_text(p.get("processName") or p.get("name")) if isinstance(p, dict) else ""
                if pid:
                    out[pid] = name
        return out
