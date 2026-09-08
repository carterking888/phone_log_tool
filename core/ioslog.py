# -*- coding: utf-8 -*-
"""iOS 实时日志会话：接口对齐 core.logcat.LogcatSession（前端轮询逻辑零改动）。

三条取流路径（按序尝试，先成功的留下）：

1. **os_trace usbmux 直连（首选）**：`OsTraceService(lockdown).syslog()`——
   unified logging 流，**应用/JS 日志只在这条流里**（syslog_relay 不含应用日志）。
   iOS 26 实测无需 tunnel 即可用；流量较大（数百行/秒）。
2. **syslog_relay（老 iOS 兜底）**：`SyslogService(lockdown).watch()`——只有
   系统级日志，新 iOS 上不含应用输出。
3. **tunnel + os_trace（最后兜底）**：iOS 17+ 官方路径，先
   `get_core_device_tunnel_services()` → `start_tunnel()` 拿到 RSD 端点，
   再用 `OsTraceService(rsd).syslog()`。**这一步在 Windows 上要管理员权限**，
   失败时给出明确 hint（未配对 / 无管理员 / Bonjour 不可用），不静默降级。

产出统一成 Android 的行结构 `{time,pid,tid,level,tag,msg}`，
其中 tag 取 `进程名` 或 `subsystem:category`，这样日志页的过滤/高亮/导出全都能直接复用。
"""
import asyncio
import contextlib
import json
import logging
import os
import re
import threading
import time
import urllib.parse
import urllib.request

from .ios import Ios, IosError, ensure_loop, _wrap, _norm_int, _as_text
from .logcat import LogcatSession, MAX_LINES

# iOS 级别 -> Android 单字母级别（前端过滤只有 V/D/I/W/E/F/S）
LEVEL_MAP = {
    "debug": "D",
    "info": "I",
    "notice": "I",
    "default": "I",
    "user_action": "I",
    "warning": "W",
    "error": "E",
    "fault": "F",
    "critical": "F",
    "alert": "F",
    "emergency": "F",
}

# Mar 15 14:23:01 iPhone SpringBoard[123] <Notice>: message
# 注意 iOS 行常带子系统后缀：`wifid(IO80211)[48]` —— proc 必须在 `(` 前截断，
# 否则 tag 变成 `wifid(IO80211)`，和进程表/包名过滤（纯进程名）永远对不上。
RE_SYSLOG = re.compile(
    r"^(?P<mon>[A-Z][a-z]{2})\s+(?P<day>\d{1,2})\s+(?P<time>\d{2}:\d{2}:\d{2})\s+"
    r"(?P<host>\S+)\s+(?P<proc>[^\s\[(]+)(?:\([^)]*\))?(?:\[(?P<pid>\d+)\])?\s*"
    r"(?:<(?P<level>[A-Za-z]+)>)?\s*:?\s*(?P<msg>.*)$"
)

MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


def level_char(name):
    return LEVEL_MAP.get(str(name or "").lower(), "I")


# Cocos V8 Inspector 常见端口（按命中概率排序）
COCOS_PORTS = (6086, 6080, 6081, 6082, 6083, 6084, 6085, 6087, 6088, 6089, 6090, 9229, 9222)


async def detect_cocos_port_async(serial, ports=COCOS_PORTS):
    """扫描设备端口，找有 CDP /json/list 响应的 Cocos Inspector，返回端口号或 None。

    usbmux connect 对没人听的端口会直接失败，因此可以放心扫。
    """
    import socket as _socket

    from pymobiledevice3 import usbmux

    mux = await usbmux.select_device(serial)
    if mux is None:
        return None
    loop = asyncio.get_event_loop()

    def probe_raw(raw):
        """返回 'http'（CDP 确认）/'tcp'（仅端口可连：游戏后台时 V8 会挂起）/''。"""
        try:
            raw.settimeout(2.5)
            raw.sendall(b"GET /json/list HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
            buf = b""
            while len(buf) < 4096:
                chunk = raw.recv(4096)
                if not chunk:
                    break
                buf += chunk
            if b"200" in buf.split(b"\r\n", 1)[0] or b"webSocketDebuggerUrl" in buf:
                return "http"
            return "tcp"
        except Exception:  # noqa: BLE001
            return "tcp"  # TCP 已连上但无响应：游戏切后台 V8 挂起，仍视为候选
        finally:
            with contextlib.suppress(Exception):
                raw.close()

    fallback = None
    for port in ports:
        try:
            raw = await mux.connect(port)
        except Exception:  # noqa: BLE001
            continue
        try:
            hit = await loop.run_in_executor(None, probe_raw, raw)
        except Exception:  # noqa: BLE001
            hit = ""
        if hit == "http":
            return port
        if hit == "tcp" and fallback is None:
            fallback = port
    return fallback


def parse_syslog_line(line, seq=0):
    """iOS 老式 syslog 行 -> 统一行结构。"""
    if isinstance(line, (bytes, bytearray)):
        line = line.decode("utf-8", "replace")
    line = line.rstrip("\r\n")
    m = RE_SYSLOG.match(line)
    if m:
        d = m.groupdict()
        mon = MONTHS.get(d["mon"], 0)
        ts = "%02d-%02d %s" % (mon, int(d["day"]), d["time"]) if mon else d["time"]
        return {
            "id": seq,
            "time": ts,
            "pid": _norm_int(d.get("pid")),
            "tid": 0,
            "level": level_char(d.get("level")),
            "tag": d.get("proc") or "",
            "msg": d.get("msg") or "",
            "raw": False,
        }
    return {"id": seq, "time": "", "pid": 0, "tid": 0, "level": "",
            "tag": "", "msg": line, "raw": True}


def entry_to_line(entry, seq=0):
    """pymobiledevice3 的 SyslogEntry -> 统一行结构。"""
    ts = getattr(entry, "timestamp", None)
    if ts is not None:
        try:
            ts = ts.strftime("%m-%d %H:%M:%S")
        except Exception:  # noqa: BLE001
            ts = str(ts)
    label = getattr(entry, "label", None)
    if label is not None and (getattr(label, "subsystem", None) or getattr(label, "category", None)):
        tag = "%s:%s" % (_as_text(getattr(label, "subsystem", "")), _as_text(getattr(label, "category", "")))
        tag = tag.strip(":")
    else:
        image = _as_text(getattr(entry, "filename", "")) or _as_text(getattr(entry, "image_name", ""))
        tag = os.path.basename(image)
    level = getattr(entry, "level", None)
    level_name = getattr(level, "name", None) or str(level or "")
    return {
        "id": seq,
        "time": ts or "",
        "pid": _norm_int(getattr(entry, "pid", 0)),
        "tid": _norm_int(getattr(entry, "thread_id", 0)),
        "level": level_char(level_name),
        "tag": tag,
        "msg": _as_text(getattr(entry, "message", "")),
        "raw": False,
    }


class IosLogSession(LogcatSession):
    """复用 LogcatSession 的 get_batch/clear/inject，只重写取流部分。"""

    def __init__(self, ios=None, cocos_inspector="", pkg=None):
        super().__init__(None)  # 不需要 adb
        self.ios = ios or Ios()
        self.mode = ""  # syslog | oslog
        self.hint = ""
        self._fut = None
        self._ready = threading.Event()
        self._pump_done = threading.Event()
        self._pump_done.set()
        self.cocos_inspector = str(cocos_inspector or "").strip()
        self.cdp_running = False
        self.cdp_error = ""
        self.cdp_count = 0  # 已捕获的 JS console 行数（给前端展示"管道是否真在吐日志"）
        self._cdp_thread = None
        self._cdp_ws = None
        self._ws_path = ""  # 最近一次握手成功的 WS path（重连优先复用）
        # CDP 请求-响应配对：读线程 recv 到带 id 的响应时回填，js_api 线程等待
        self._cdp_id = 0
        self._cdp_pend_lock = threading.Lock()
        self._cdp_pending = {}  # id -> (Event, [response])
        # 后端预过滤（选中应用时由前端传入）：oslog 洪流每秒数百条 com.apple.* 噪声，
        # 不在源头掐掉的话，应用/JS 日志会先被缓冲压缩挤掉、再被前端尾窗顶走，永远看不到。
        pf = pkg if isinstance(pkg, dict) else None
        self.pkg_pid = _norm_int(pf.get("pid")) if pf else None
        names = []
        if pf and isinstance(pf.get("names"), list):
            names = [str(n).strip() for n in pf["names"] if str(n).strip()]
        if pf and not names:
            p0 = str(pf.get("package") or "").strip()
            if p0:
                names = [p0]
        self.pkg_names = names
        self.dropped = 0  # 被预过滤丢弃的行数（诊断用）
        # iOS usbmux 端口转发（把设备上的 Cocos Inspector 端口转到本地回环）
        self._forwarder = None
        self._forwarder_fut = None
        self._forward_local = 0
        self._device_cdp_port = 0  # 设备侧 Inspector 端口（提示文案用）

    # -------------------------------------------------------------- 生命周期
    def start(self, serial=None, buffer="main", clear=True, wait=30):
        self.stop()
        self.error = ""
        self.serial = serial
        self.buffer = buffer or "main"
        with self._lock:
            self.lines = []
            self.seq = 0
        self.running = False
        self._ready = threading.Event()
        self._pump_done = threading.Event()
        loop = ensure_loop()
        self._fut = asyncio.run_coroutine_threadsafe(self._pump(serial), loop)
        # 取流建立可能要十几秒（iOS 17+ 要起 tunnel），等一个明确结果再返回
        self._ready.wait(wait)
        if self.error:
            return False
        if not self.running:
            self.error = "日志流建立超时（%ss）" % wait
            return False
        if self.cocos_inspector:
            addr = self.cocos_inspector
            if addr.isdigit():
                # 纯数字 = 设备上的 Inspector 端口 → usbmux 转发到本地回环（纯 USB，免 tunnel）
                self._device_cdp_port = int(addr)
                ok, msg = self._start_forwarder_sync(serial, int(addr))
                if not ok:
                    self.cdp_error = msg
                else:
                    self.cocos_inspector = "127.0.0.1:%d" % self._forward_local
            if self._forward_local or (":" in self.cocos_inspector):
                self._cdp_thread = threading.Thread(
                    target=self._cdp_reader, name="ios-cocos-cdp", daemon=True
                )
                self._cdp_thread.start()
        return True

    def stop(self):
        self.running = False
        ws, self._cdp_ws = self._cdp_ws, None
        if ws is not None:
            with contextlib.suppress(Exception):
                ws.close()
        self.cdp_running = False
        self._cdp_thread = None
        self._stop_forwarder()
        fut, self._fut = self._fut, None
        if fut is not None:
            fut.cancel()
            # 取回任务异常，避免 "Task exception was never retrieved" 噪声
            fut.add_done_callback(self._swallow_result)
            # 不能只发 cancel 就返回：进程退出/立即重启时 loop 会销毁尚未完成
            # SSL read 的任务，产生 pending/aclose already running 警告。
            if threading.current_thread().name != "ios-loop":
                self._pump_done.wait(3)
        self._ready.set()

    @staticmethod
    def _swallow_result(task):
        try:
            task.result()
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 收尾路径，静默即可
            pass

    def clear(self):
        super().clear()

    def get_batch(self, cursor=0, limit=2000):
        out = super().get_batch(cursor, limit)
        out["cdpConfigured"] = bool(self.cocos_inspector)
        out["cdpRunning"] = self.cdp_running
        out["cdpError"] = self.cdp_error
        out["cdpCount"] = self.cdp_count
        return out

    def inject(self, line):
        """覆盖父类：父类用的是 Android threadtime 解析器，这里必须走 syslog 解析器。"""
        with self._lock:
            parsed = parse_syslog_line(line, 0)
            if not self._match_pkg(parsed):
                self.dropped += 1
                return
            self.seq += 1
            parsed["id"] = self.seq
            self.lines.append(parsed)
            if len(self.lines) > MAX_LINES:
                self.lines = self.lines[-MAX_LINES // 2:]

    # -------------------------------------------------- usbmux 端口转发（Inspector）
    def _start_forwarder_sync(self, serial, device_port):
        """把设备上的 device_port 转发到本地随机回环端口，返回 (ok, msg)。

        UsbmuxTcpForwarder.start() 会阻塞到 stopped 置位，因此作为协程丢进
        常驻 iOS 事件循环；listening_event.wait() 在 js_api 线程同步等就绪。
        """
        if self._forwarder is not None:
            self._stop_forwarder()
        try:
            from pymobiledevice3.tcp_forwarder import UsbmuxTcpForwarder
        except ImportError:
            return False, "pymobiledevice3 缺少 tcp_forwarder 模块"
        # 静音转发器内部日志：设备端口未监听时它每 3 秒刷一条
        # "failed to connect to remote endpoint"（CDP 读线程会重连），
        # 连接状态由本类的 cdp_error 以中文提示给出，无需重复英文噪声。
        logging.getLogger("pymobiledevice3.tcp_forwarder").setLevel(logging.CRITICAL)
        try:
            ready = threading.Event()  # 转发器只调 .set()；跨线程同步等就绪必须用 threading 版
            fwd = UsbmuxTcpForwarder(serial, int(device_port), 0, listening_event=ready)
            loop = ensure_loop()
            fut = asyncio.run_coroutine_threadsafe(fwd.start(), loop)
            if not ready.wait(10):
                fwd.stop()
                fut.cancel()
                return False, "USB 转发启动超时（设备未响应端口 %d）" % device_port
            local = fwd.listening_port
            if not local:
                fwd.stop()
                return False, "USB 转发未能获得本地端口"
            self._forwarder = fwd
            self._forwarder_fut = fut
            self._forward_local = local
            return True, ""
        except Exception as e:  # noqa: BLE001
            return False, "USB 转发失败：%s" % _as_text(e)

    def _stop_forwarder(self):
        fwd, self._forwarder = self._forwarder, None
        self._forward_local = 0
        if fwd is not None:
            fwd.stop()
        fut, self._forwarder_fut = self._forwarder_fut, None
        if fut is not None:
            # 取回协程结束状态，避免事件循环刷 "exception was never retrieved"
            with contextlib.suppress(Exception):
                fut.result(5)

    # -------------------------------------------------------------- 取流
    @staticmethod
    async def _drain(gen, consume):
        """迭代取流生成器；取消会自然向 __anext__ 传播，禁止重复 aclose。"""
        async for item in gen:
            if not consume(item):
                break

    def _consume_line(self, line):
        if not self._mark_ready():
            return False
        self.inject(line)
        return True

    def _consume_entry(self, entry):
        if not self._mark_ready():
            return False
        self._inject_entry(entry)
        return True

    async def _pump(self, serial):
        try:
            await self._pump_body(serial)
        finally:
            self._pump_done.set()

    async def _pump_body(self, serial):
        """优先 os_trace 直连（unified logging，含应用/JS 日志）；失败回落 legacy
        syslog_relay（只有系统级日志）；再失败走 tunnel + os_trace（iOS 17+ 官方路径）。"""
        first_err = None
        # 路径 1：os_trace usbmux 直连（iOS 26 实测可用，应用日志只在这条流里）
        try:
            await self._drain(self._iter_oslog_usbmux(serial), self._consume_entry)
            return
        except asyncio.CancelledError:
            return
        except Exception as e:  # noqa: BLE001
            first_err = e

        if self._stopped_by_user():
            return

        # 路径 2：legacy syslog_relay（老 iOS 可用；新 iOS 只吐系统级日志）
        legacy_err = None
        try:
            await self._drain(self._iter_syslog(serial), self._consume_line)
            return
        except asyncio.CancelledError:
            return
        except Exception as e:  # noqa: BLE001
            legacy_err = e

        if self._stopped_by_user():
            return

        # 路径 3：tunnel + os_trace
        try:
            await self._drain(self._iter_oslog(serial), self._consume_entry)
        except asyncio.CancelledError:
            return
        except Exception as e:  # noqa: BLE001
            err = _wrap(e)
            hint = err.hint or "iOS 17+ 需管理员权限起 tunnel；也可先用 macOS 控制台抓日志"
            le = _wrap(first_err) if first_err is not None else None
            if legacy_err is not None and le is not None:
                self.error = "%s（oslog直连失败：%s）" % (err.msg, le.msg)
            else:
                self.error = err.msg
            self.hint = hint
            self.running = False
            self._ready.set()
            return

    def _mark_ready(self):
        """第一帧到达才算真正开始；期间若被 stop 则退出。"""
        if not self.running:
            self.running = True
            self.mode = self.mode or "ios"
            self._ready.set()
        return True

    def _stopped_by_user(self):
        return self._fut is None

    def _match_pkg(self, line):
        """后端预过滤：PID 命中或 Tag 命中任一候选名；未设置过滤时全放行。

        cdp 行（JS 日志）不走这里——_inject_cdp_event 直接入库，永不过滤。
        """
        if self.pkg_pid is None and not self.pkg_names:
            return True
        if self.pkg_pid is not None and line.get("pid") == self.pkg_pid:
            return True
        if self.pkg_names and line.get("tag") in self.pkg_names:
            return True
        return False

    def _inject_entry(self, entry):
        line = entry_to_line(entry, 0)
        with self._lock:
            if not self._match_pkg(line):
                self.dropped += 1
                return
            self.seq += 1
            line["id"] = self.seq
            self.lines.append(line)
            if len(self.lines) > MAX_LINES:
                self.lines = self.lines[-MAX_LINES // 2:]

    # -- Cocos Creator / Cocos2d-x V8 Inspector（Chrome DevTools Protocol）
    def _cdp_ws_candidates(self):
        """发现 Cocos Inspector 的候选 WebSocket 地址列表（按优先级排序）。

        游戏重启/页面刷新都会更换 target id，旧的 webSocketDebuggerUrl 再握手会得到
        400 + text/html 错误页；且 targets[0] 未必是活页面。因此每次重连都重新拉
        /json/list，把「上次成功过的 path > 各目标 URL > 各目标 /devtools/page/<id>
        > 老版本序号 /devtools/page/0」逐个尝试。
        """
        addr = self.cocos_inspector
        if not addr:
            raise ValueError("未配置 Cocos Inspector 地址")
        if "://" in addr and addr.startswith("ws"):
            return [addr]
        if "://" not in addr:
            addr = "http://" + addr
        parsed = urllib.parse.urlparse(addr)
        netloc = parsed.netloc or parsed.path
        scheme = "wss" if parsed.scheme == "https" else "ws"
        cands, seen = [], set()

        def add(path):
            if path and path not in seen:
                seen.add(path)
                cands.append("%s://%s%s" % (scheme, netloc, path))

        if self._ws_path:  # 上次握手成功过的 path 优先复用
            add(self._ws_path)
        try:
            with urllib.request.urlopen("http://%s/json/list" % netloc, timeout=3) as res:
                targets = json.loads(res.read().decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001 - 列表拉不下来时仍可试老版序号地址
            targets = []
        for t in (targets if isinstance(targets, list) else []):
            ws_url = t.get("webSocketDebuggerUrl")
            if ws_url:
                # webSocketDebuggerUrl 里的 host 是目标侧视角（设备内 127.0.0.1:6086），
                # 只保留 path，host 统一改写为本地访问地址（直连或 usbmux 转发端口）。
                add(urllib.parse.urlparse(ws_url).path)
            add("/devtools/page/%s" % t.get("id", ""))
        add("/devtools/page/0")  # 老版本 Cocos Inspector 按序号索引
        if not cands:
            raise RuntimeError("未发现 Cocos 调试目标")
        return cands

    @staticmethod
    def _cdp_arg_text(arg):
        if "value" in arg:
            value = arg.get("value")
            if isinstance(value, (dict, list)):
                return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            if value is None:
                return "null"
            return str(value)
        return str(arg.get("description") or arg.get("unserializableValue") or "")

    def _inject_cdp_event(self, params):
        texts = [self._cdp_arg_text(a) for a in (params.get("args") or [])]
        msg = " ".join(t for t in texts if t)
        if not msg:
            return
        kind = str(params.get("type") or "log").lower()
        level = {
            "debug": "D", "log": "D", "info": "I", "warning": "W",
            "error": "E", "assert": "E",
        }.get(kind, "D")
        ts = params.get("timestamp")
        try:
            stamp = time.strftime("%m-%d %H:%M:%S", time.localtime(float(ts) / 1000.0))
            stamp += ".%03d" % (int(float(ts)) % 1000)
        except Exception:
            stamp = time.strftime("%m-%d %H:%M:%S")
        with self._lock:
            self.seq += 1
            self.cdp_count += 1
            self.lines.append({
                "id": self.seq, "time": stamp, "pid": 0, "tid": 0,
                "level": level, "tag": "jswrapper", "msg": msg,
                "raw": False, "source": "cocos-cdp",
            })
            if len(self.lines) > MAX_LINES:
                self.lines = self.lines[-MAX_LINES // 2:]

    def cdp_eval(self, expression, timeout=5):
        """通过已连接的 CDP 会话在游戏里执行 JS 表达式（如开启 cc.debug 日志级别）。

        发送走 js_api 调用线程，响应由读线程回填（_cdp_pending 配对）。
        返回 (ok, 输出文本)。
        """
        ws = self._cdp_ws
        if ws is None or not self.cdp_running:
            return False, "JS 调试口未连接（需先开始捕获，且游戏在前台运行）"
        with self._cdp_pend_lock:
            self._cdp_id += 1
            rid = self._cdp_id
            ev = threading.Event()
            box = []
            self._cdp_pending[rid] = (ev, box)
        try:
            ws.send(json.dumps({
                "id": rid, "method": "Runtime.evaluate",
                "params": {"expression": expression, "returnByValue": True},
            }))
        except Exception as e:  # noqa: BLE001
            with self._cdp_pend_lock:
                self._cdp_pending.pop(rid, None)
            return False, "发送失败：%s" % _as_text(e)
        if not ev.wait(timeout):
            with self._cdp_pend_lock:
                self._cdp_pending.pop(rid, None)
            return False, "游戏未响应（可能已切后台或刚好重启）"
        resp = box[0] if box else {}
        if resp.get("error"):
            return False, "执行出错：%s" % json.dumps(resp["error"], ensure_ascii=False)[:200]
        result = (resp.get("result") or {}).get("result") or {}
        if result.get("subtype") == "error":
            return False, "JS 异常：" + str(result.get("description") or "")[:200]
        val = result.get("value") if "value" in result else result.get("description")
        return True, "已执行" if val is None else _as_text(val)[:200]

    def _cdp_probe(self):
        """WS 握手前先探测设备侧 Inspector 是否可连（HTTP /json/list 走同一转发链）。

        返回 (True, "") 或 (False, 中文提示)。游戏未运行/V8 未监听时，
        转发器对设备端口的连接会失败——此时给可操作提示而不是原始异常。
        """
        addr = self.cocos_inspector
        if not addr:
            return False, "未配置 Cocos Inspector 地址"
        base = ("http://" + addr) if "://" not in addr else addr.replace("ws", "http", 1)
        try:
            with urllib.request.urlopen(base.rstrip("/") + "/json/list", timeout=2):
                return True, ""
        except Exception:  # noqa: BLE001 - 探测失败统一给可操作提示
            # 提示里展示设备侧端口（cocos_inspector 此时已被改写为本地转发地址）
            port = str(getattr(self, "_device_cdp_port", "") or addr.rsplit(":", 1)[-1])
            return False, (
                "设备 Inspector 端口 %s 未连通（游戏需在前台运行并开启 Cocos JS 调试），自动重试中…" % port
            )

    @staticmethod
    def _cdp_friendly_error(e):
        """把握手/读取异常翻译成可操作的中文提示（原始异常不透传到 UI）。"""
        if isinstance(e, (ValueError, RuntimeError)):
            return str(e)  # 本模块自抛的中文提示，原样展示
        try:
            import websocket
            if isinstance(e, websocket.WebSocketBadStatusException):
                code = getattr(e, "status_code", None)
                if not code:
                    m = re.search(r"\b(\d{3})\b", str(e))
                    code = m.group(1) if m else "4xx"
                return ("Inspector 拒绝握手（HTTP %s）——常见原因：① 页面 id 已失效"
                        "（游戏重启后自动恢复）② 该页面已被其他调试客户端（Safari Web "
                        "Inspector / 爱思助手等）占用，V8 只允许一个客户端，请关闭后重试。"
                        "自动重试中…" % code)
        except Exception:  # noqa: BLE001
            pass
        return "%s: %s" % (type(e).__name__, e)

    def _cdp_reader(self):
        """独立线程读取 JS console；失败不影响 iOS 系统日志主流。"""
        try:
            import websocket
        except ImportError:
            self.cdp_error = "缺少 websocket-client 依赖（pip install websocket-client）"
            return

        # 游戏重启会更换 target id；Chrome 关闭 Inspect 后也应自动接管，因此持续重连。
        while self.running:
            ws = None
            try:
                # 先探测：设备端口未监听（游戏未运行/切后台）时不必发起 WS 握手，
                # 也避免转发器每次尝试连设备端口产生的连接失败噪声。
                ok, probe_err = self._cdp_probe()
                if not ok:
                    self.cdp_running = False
                    self.cdp_error = probe_err
                else:
                    # 逐个候选地址握手（游戏重启后 id 会变，400 的候选直接跳过）
                    last_err = None
                    for url in self._cdp_ws_candidates():
                        try:
                            ws = websocket.create_connection(
                                url, timeout=2, suppress_origin=True
                            )
                            self._ws_path = urllib.parse.urlparse(url).path
                            break
                        except Exception as e:  # noqa: BLE001 - 换下一个候选
                            last_err = e
                            ws = None
                    if ws is None:
                        raise last_err or RuntimeError("未发现可用的 Cocos 调试目标")
                    self._cdp_ws = ws
                    self.cdp_running = True
                    self.cdp_error = ""
                    ws.send(json.dumps({"id": 1, "method": "Runtime.enable"}))
                    while self.running and self._cdp_ws is ws:
                        try:
                            raw = ws.recv()
                        except websocket.WebSocketTimeoutException:
                            continue
                        if not raw:
                            break
                        event = json.loads(raw)
                        if "id" in event:
                            # Runtime.evaluate 等请求的响应 → 交给等待中的 cdp_eval
                            with self._cdp_pend_lock:
                                waiter = self._cdp_pending.pop(event["id"], None)
                            if waiter is not None:
                                waiter[1].append(event)
                                waiter[0].set()
                        elif event.get("method") == "Runtime.consoleAPICalled":
                            self._inject_cdp_event(event.get("params") or {})
            except Exception as e:  # noqa: BLE001
                self.cdp_error = self._cdp_friendly_error(e)
            finally:
                if self._cdp_ws is ws:
                    self._cdp_ws = None
                if ws is not None:
                    with contextlib.suppress(Exception):
                        ws.close()
                self.cdp_running = False
            # 可被 stop 最多延迟 3 秒；daemon 线程不会阻塞应用退出。
            for _ in range(30):
                if not self.running:
                    return
                time.sleep(0.1)

    # -- 路径 1：os_trace usbmux 直连（含应用日志；iOS 26 实测无需 tunnel）
    async def _iter_oslog_usbmux(self, serial):
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.services.os_trace import OsTraceService

        self.mode = "oslog"
        ld = await create_using_usbmux(serial=serial)
        async with ld:
            async with OsTraceService(ld) as svc:
                async for entry in svc.syslog():
                    yield entry

    # -- 路径 2：syslog_relay
    async def _iter_syslog(self, serial):
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.services.syslog import SyslogService

        self.mode = "syslog"
        ld = await create_using_usbmux(serial=serial)
        async with ld:
            async with SyslogService(ld) as svc:
                async for line in svc.watch():
                    yield line

    # -- 路径 3：tunnel + os_trace（iOS 17+ 官方路径，需管理员）
    async def _iter_oslog(self, serial):
        from pymobiledevice3.remote.common import TunnelProtocol
        from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
        from pymobiledevice3.remote.tunnel_service import (
            get_core_device_tunnel_services,
            start_tunnel,
        )
        from pymobiledevice3.services.os_trace import OsTraceService

        self.mode = "oslog-tunnel"
        services = await get_core_device_tunnel_services(udid=serial)
        if not services:
            raise IosError(
                "未发现可建立 tunnel 的 iOS 设备",
                "确认已安装 iTunes/Bonjour；iOS 17+ 抓日志需要先起 tunnel（管理员权限）",
            )
        async with start_tunnel(services[0], protocol=TunnelProtocol.TCP) as tunnel:
            rsd = RemoteServiceDiscoveryService((tunnel.address, tunnel.port))
            try:
                await rsd.connect()
                async with OsTraceService(rsd) as svc:
                    async for entry in svc.syslog():
                        yield entry
            finally:
                try:
                    await rsd.close()
                except Exception:  # noqa: BLE001
                    pass

    # 供前端展示当前用的是哪条路径
    def info(self):
        return {
            "mode": self.mode,
            "running": self.running,
            "error": self.error,
            "hint": getattr(self, "hint", ""),
            "serial": self.serial,
            "cocosInspector": self.cocos_inspector,
            "jsLocalPort": self._forward_local,
            "cdpRunning": self.cdp_running,
            "cdpError": self.cdp_error,
        }
