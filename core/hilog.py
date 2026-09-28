# -*- coding: utf-8 -*-
"""鸿蒙 hilog 实时会话：接口对齐 core.logcat.LogcatSession（前端轮询逻辑零改动）。

数据源：`hdc -t <key> shell hilog`（hilog 不带 -x 时持续阻塞输出，正好当流用）。

hilog 行格式（threadtime 风格）：
    09-27 18:24:54.123  1234  5678 D C01800/HLWF: message
                          pid  tid  级别 域/标签

要点：
- 级别本身就是 D/I/W/E/F 单字母，与前端过滤（V/D/I/W/E/F/S）天然对齐；
- tag 保留「域/标签」整体（如 C01800/HLWF），避免不同域同名标签混在一起过滤；
- 鸿蒙默认开启隐私掩码（内容显示 <private>），启动时先 `hilog -p off` 尽力关闭，
  失败不影响取流，只是内容被掩码；
- 启动时 `hilog -r` 清环形缓冲，保证从当前时间开始看。
"""
import re
import subprocess
import threading

from .adb import IS_WIN, _no_window
from .logcat import MAX_LINES

# 09-27 18:24:54.123  1234  5678 D C01800/HLWF: message
RE_HILOG = re.compile(
    r"^(?P<time>\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d{3})\s+"
    r"(?P<pid>\d+)\s+(?P<tid>\d+)\s+"
    r"(?P<level>[VDIWEF])\s+"
    r"(?P<domain>[0-9A-Fa-f]{4,6})/(?P<tag>[^\s:]+)\s*:\s?(?P<msg>.*)$"
)


def parse_line(line, seq=0):
    m = RE_HILOG.match(line)
    if m:
        d = m.groupdict()
        return {
            "id": seq,
            "time": d["time"],
            "pid": int(d["pid"]),
            "tid": int(d["tid"]),
            "level": d["level"],
            # 保留 域/标签 整体做 tag：不同域同名标签过滤时不串
            "tag": "%s/%s" % (d["domain"], d["tag"]),
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


class HilogSession:
    """hdc hilog 实时会话，生命周期与 LogcatSession 完全一致。"""

    def __init__(self, hdc):
        self.hdc = hdc
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
    def start(self, target, buffer="main", clear=True):
        self.stop()
        self.error = ""
        if not self.hdc.exists()[0]:
            self.error = "hdc 未找到: %s" % self.hdc.path
            return False
        # 清环形缓冲（尽力而为，失败不阻断）
        if clear:
            self.hdc.run(["shell", "hilog -r"], target=target, timeout=15)
        # 关隐私掩码：默认很多字段显示 <private>，关掉才能看到真实内容
        # （部分版本/非 root 会失败，忽略即可）
        self.hdc.run(["shell", "hilog -p off"], target=target, timeout=10)
        cmd = self.hdc.build_cmd(["shell", "hilog"], target=target)
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
            self.error = "hdc 未找到: %s" % self.hdc.path
            return False
        except Exception as e:  # noqa
            self.error = "%s: %s" % (type(e).__name__, e)
            return False
        self.serial = target
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
