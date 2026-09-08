# -*- coding: utf-8 -*-
"""logcat 实时会话：后台子进程 + 读线程 + 游标式批量拉取（前端轮询）。"""
import re
import subprocess
import threading
import time

from .adb import IS_WIN, _no_window

# 06-24 15:40:08.121  2345  2360 E AndroidRuntime: FATAL EXCEPTION: main
RE_THREADTIME = re.compile(
    r"^(?P<time>\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d{3})\s+"
    r"(?P<pid>\d+)\s+(?P<tid>\d+)\s+"
    r"(?P<level>[VDIWEFS])\s+"
    r"(?P<tag>[^:]*?)\s*:\s?(?P<msg>.*)$"
)

MAX_LINES = 100000


def parse_line(line, seq=0):
    m = RE_THREADTIME.match(line)
    if m:
        d = m.groupdict()
        return {
            "id": seq,
            "time": d["time"],
            "pid": int(d["pid"]),
            "tid": int(d["tid"]),
            "level": d["level"],
            "tag": d["tag"].strip(),
            "msg": d["msg"],
            "raw": False,
        }
    return {
        "id": seq,
        "time": "",
        "pid": 0,
        "tid": 0,
        "level": "",
        "tag": "",
        "msg": line,
        "raw": True,
    }


class LogcatSession:
    def __init__(self, adb):
        self.adb = adb
        self.proc = None
        self.thread = None
        self.lines = []
        self.seq = 0
        self.cursor = 0
        self.running = False
        self.serial = None
        self.buffer = "main"
        self.error = ""
        self._lock = threading.Lock()

    # -------------------------------------------------------------- 生命周期
    def start(self, serial, buffer="main", clear=True):
        self.stop()
        self.error = ""
        if clear:
            self.adb.run(["logcat", "-c"], serial=serial, timeout=15)
        args = ["logcat", "-v", "threadtime"]
        if buffer and buffer != "all":
            args += ["-b", buffer]
        cmd = self.adb.build_cmd(args, serial=serial)
        try:
            self.proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=_no_window() if IS_WIN else 0,
            )
        except FileNotFoundError:
            self.error = "adb 未找到: %s" % self.adb.path
            return False
        except Exception as e:  # noqa
            self.error = "%s: %s" % (type(e).__name__, e)
            return False
        self.serial = serial
        self.buffer = buffer
        self.running = True
        with self._lock:
            self.lines = []
            self.seq = 0
            self.cursor = 0
        self.thread = threading.Thread(target=self._reader, daemon=True)
        self.thread.start()
        return True

    def stop(self):
        self.running = False
        if self.proc:
            try:
                self.proc.terminate()
            except Exception:
                pass
            try:
                self.proc.wait(timeout=3)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            self.proc = None
        self.thread = None

    def _reader(self):
        try:
            for line in iter(self.proc.stdout.readline, ""):
                if not self.running:
                    break
                line = line.rstrip("\r\n")
                with self._lock:
                    self.seq += 1
                    self.lines.append(parse_line(line, self.seq))
                    if len(self.lines) > MAX_LINES:
                        self.lines = self.lines[-MAX_LINES // 2:]
        except Exception:
            pass
        finally:
            self.running = False

    # -------------------------------------------------------------- 数据接口
    def get_batch(self, cursor=0, limit=2000):
        with self._lock:
            total_seq = self.seq
            if cursor > total_seq:
                cursor = total_seq
            start = max(0, len(self.lines) - (total_seq - cursor))
            chunk = self.lines[start:start + limit]
            new_cursor = cursor + len(chunk)
            return {
                "lines": list(chunk),
                "cursor": new_cursor,
                "total": len(self.lines),
                "seq": total_seq,
                "running": self.running,
                "error": self.error,
            }

    def clear(self):
        with self._lock:
            self.lines = []
            self.seq = 0
            self.cursor = 0

    def inject(self, line):
        """供演示模式注入日志行。"""
        with self._lock:
            self.seq += 1
            self.lines.append(parse_line(line, self.seq))


class DemoLogcatSession(LogcatSession):
    """演示模式（未连接设备）：不产生模拟日志，页面显示空。

    保留独立类是为了让 start/stop 的调用流程与真机一致（running=True、
    游标从 0 开始），只是永远没有新行——不再伪造 logcat 数据。
    """

    def start(self, serial=None, buffer="main", clear=True):
        self.stop()
        self.error = ""
        self.serial = serial or "demo-device"
        self.buffer = buffer
        with self._lock:
            self.lines = []
            self.seq = 0
        self.running = True
        return True
