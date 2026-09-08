# -*- coding: utf-8 -*-
"""操作日志联动：所有设备写操作记录 adb 命令与结果，JSONL 落盘。"""
import json
import os
import threading
import time
from datetime import datetime

_LOCK = threading.Lock()


def log_dir():
    base = os.path.join(os.path.expanduser("~"), ".adb_tool", "action_logs")
    os.makedirs(base, exist_ok=True)
    return base


def record(action, cmd, serial="", exit_code=None, output="", cost_ms=0, ok=True, extra=None):
    item = {
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "time": time.strftime("%H:%M:%S"),
        "serial": serial,
        "action": action,
        "cmd": cmd,
        "exitCode": exit_code,
        "output": (output or "")[:2000],
        "costMs": cost_ms,
        "ok": bool(ok),
    }
    if extra:
        item.update(extra)
    path = os.path.join(log_dir(), "%s.jsonl" % time.strftime("%Y-%m-%d"))
    with _LOCK:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        except Exception:
            pass
    return item


def recent(limit=200):
    path = os.path.join(log_dir(), "%s.jsonl" % time.strftime("%Y-%m-%d"))
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return []
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    out.reverse()
    return out


def tail(n=50):
    return recent(n)
