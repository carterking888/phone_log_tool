# -*- coding: utf-8 -*-
"""媒体工具：截屏 / 录屏 / 本地媒体库（Android / HarmonyOS / iOS）。

平台能力矩阵（如实声明，不装能）：
- Android: 截屏 screencap；录屏 screenrecord（声音需 Android 11+ --audio-source）
- Harmony: 截屏 snapshot_display + file recv；录屏无公开 hdc 命令，如实提示
- iOS:     截屏 pmd3 ScreenshotService（DVT）——iOS<=16 走 usbmux 直连；
           iOS 17+ Windows 无 tunnel 不可用，Mac 需 root tunneld；
           录屏 pmd3 无公开持续帧流接口，采用"DVT 连续截屏帧 -> ffmpeg 合成
           MP4"方案：录制期间只落帧到临时目录，点停止后才一次性编码
           （ffmpeg 正常退出 = moov 原子必然写完，杜绝 mp4 半途损坏）。
           录屏流只采集画面，不含触摸事件/点击坐标，也无声音。

本地媒体库统一放 <程序根>/media/，文件名时间戳，前端列表预览/下载/重命名/删除。
录屏停止不能强杀 ffmpeg/录屏进程：Android 用 pkill -l2(SIGINT) 让 screenrecord
自己写完封装，再轮询文件大小稳定后 pull，防止 mp4 损坏。
"""
import base64
import os
import re
import subprocess
import threading
import time

from .adb import IS_WIN, _no_window, _app_base
from . import action_log

MEDIA_DIR = os.path.join(_app_base(), "media")

# 录制会话（同一次一台设备）：serial -> {proc, meta, platform, remote, local, ...}
_RECORDS = {}
_LOCK = threading.Lock()


def media_dir():
    os.makedirs(MEDIA_DIR, exist_ok=True)
    return MEDIA_DIR


def _stamp():
    return time.strftime("%Y%m%d_%H%M%S")


def _safe_name(name, default_ext=""):
    """文件名清洗：去路径分隔与控制字符，空则给时间戳默认名。"""
    s = re.sub(r"[\\/:*?\"<>|\r\n\t]", "_", (name or "").strip())
    if not s:
        s = "media_" + _stamp() + default_ext
    if default_ext and not re.search(r"\.[A-Za-z0-9]{2,5}$", s):
        s += default_ext
    return s


def _bin_out(cmd, timeout=30):
    """二进制安全的命令执行（screencap 回传 PNG 用，文本 run 会破坏字节流）。"""
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout,
                           creationflags=_no_window())
        return p.returncode, p.stdout or b"", (p.stderr or b"").decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return -1, b"", "timeout after %ss" % timeout
    except FileNotFoundError:
        return -2, b"", "命令未找到: %s" % cmd[0] if cmd else "empty cmd"
    except Exception as e:  # noqa
        return -3, b"", "%s: %s" % (type(e).__name__, e)


class MediaManager:
    """Api 持有的媒体管理器；platform 判断复用 api._platform_of。"""

    def __init__(self, api):
        self.api = api
        self._auto_finished = {}   # serial -> 自动收尾结果（中断/到时），供前端轮询取走
        self._recovered = {}       # "*" -> 上次异常退出遗留帧目录恢复出的文件名列表
        self._recover_started = False
        self._dur_cache = {}       # (name, mtime, size) -> 视频时长秒数（ffprobe 探测缓存）

    # -------------------------------------------------------------- 能力声明
    def _ios_major(self, serial):
        """iOS 主版本号：优先用 refresh_env 枚举时已缓存的设备清单（零服务调用）。

        之前 capabilities() 里直接 ios.device_info(timeout=40)：它会开
        Diagnostics/AFC 两个 lockdown 服务，设备锁屏/休眠/半掉线时这两个
        服务会挂住，把截屏和录屏入口一起拖到 40 秒超时（用户实际踩到）。
        缓存里没有时兜底做一次 15 秒的轻量查询，再拿不到返回 None（此时
        不拦截，让真正的截屏/录屏自己去报准确错误）。
        """
        for d in (getattr(self.api, "_ios_devices", None) or []):
            if d.get("serial") == serial:
                m = re.match(r"(\d+)", str(d.get("iosVersion") or ""))
                return int(m.group(1)) if m else None
        try:
            d = self.api.ios.device_info(serial, timeout=15)
            m = re.match(r"(\d+)", str(d.get("iosVersion") or ""))
            return int(m.group(1)) if m else None
        except Exception:  # noqa: BLE001
            return None

    def capabilities(self, serial=None):
        plat = self.api._platform_of(serial)
        caps = {"platform": plat, "screenshot": True, "record": False,
                "screenshotHint": "", "recordHint": "", "saveDir": media_dir()}
        if plat == "android":
            caps["record"] = True
            caps["recordHint"] = "录制的声音需 Android 11+；单段最长 3 分钟（到时自动保存）"
        elif plat == "harmony":
            caps["screenshotHint"] = "走 snapshot_display，部分系统版本受安全策略限制"
            caps["recordHint"] = "纯血鸿蒙没有公开的 hdc 录屏命令，暂不支持（可用手机自带录屏后从文件页取）"
        else:  # ios
            caps["screenshot"] = True
            major = self._ios_major(serial) or 0
            if major >= 17 and IS_WIN:
                caps["screenshot"] = False
                caps["screenshotHint"] = ("iOS 17+ 的 DVT 服务需要 TUN 隧道，Windows 不支持；"
                                          "Mac 需 root 运行 tunneld。可用手机自带截屏后从文件页取")
                caps["recordHint"] = ("Windows 平台不支持 iOS 17+ USB 录屏（iOS 17+ 需要 TUN 隧道）。"
                                      "备选：AirPlay 无线录屏（后续版本考虑），"
                                      "或手机自带录屏后从文件页拉取")
            elif major >= 17:
                caps["screenshotHint"] = "iOS 17+：需先以 root 启动 tunneld（sudo pymobiledevice3 remote tunneld）"
                caps["recordHint"] = ("iOS 17+ 的录屏链路需要 TUN 隧道，本工具当前 USB 直连不支持。"
                                      "备选：手机自带录屏后从文件页拉取")
            else:
                caps["screenshotHint"] = ("首次截屏需自动下载并挂载开发者镜像（DDI，约 6-16 MB，需联网）；"
                                          "设备重启后会自动重新挂载")
                caps["record"] = True
                caps["recordHint"] = ("iOS 录屏基于连续截屏帧合成 MP4（帧率可在下方选择，"
                                      "建议 5-8 fps）；只采集画面：不含触摸事件/点击坐标，无声音。"
                                      "首次使用需联网挂载开发者镜像")
        return caps

    # -------------------------------------------------------------- 截屏
    def screenshot(self, save_to_device=False, copy_clipboard=False):
        api = self.api
        serial = api.current_serial
        if not serial:
            return {"ok": False, "error": "未选择设备"}
        if api.demo:
            return {"ok": True, "file": "", "preview": self._demo_png(), "name": "demo_screenshot.png"}
        plat = api._platform_of(serial)
        if plat == "android":
            return self._shot_android(serial, save_to_device, copy_clipboard)
        if plat == "harmony":
            return self._shot_harmony(serial, save_to_device, copy_clipboard)
        if plat == "ios":
            return self._shot_ios(serial, copy_clipboard)
        return {"ok": False, "error": "未知平台"}

    def _shot_android(self, serial, save_to_device, copy_clipboard):
        cmd = self.api.adb.build_cmd(["exec-out", "screencap", "-p"], serial=serial)
        code, data, err = _bin_out(cmd, timeout=30)
        if code != 0 or not data:
            return {"ok": False, "error": "screencap 失败: %s" % (err or "无输出")}
        name = "screenshot_%s.png" % _stamp()
        path = os.path.join(media_dir(), name)
        with open(path, "wb") as f:
            f.write(data)
        if save_to_device:
            # 设备端保存：screencap 直接写 DCIM 再广播扫描
            remote = "/sdcard/DCIM/Screenshots/%s" % name
            self.api.adb.run(["shell", "mkdir -p /sdcard/DCIM/Screenshots"], serial=serial, timeout=10)
            r2 = self.api.adb.run(["shell", "screencap -p %s" % remote], serial=serial, timeout=30)
            if r2["ok"]:
                self.api.adb.run(["shell",
                                  "am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d file://%s"
                                  % remote], serial=serial, timeout=10)
                action_log.record("screenshot", "screencap -> %s" % remote, serial=serial, ok=True)
            else:
                action_log.record("screenshot", "screencap %s" % remote, serial=serial,
                                  ok=False, output=r2["stderr"])
        if copy_clipboard:
            self._copy_image_clipboard(path)
        action_log.record("screenshot", "screencap -p (%d KB)" % (len(data) // 1024),
                          serial=serial, ok=True)
        return {"ok": True, "file": path, "name": name,
                "preview": "data:image/png;base64," + base64.b64encode(data).decode()}

    def _shot_harmony(self, serial, save_to_device, copy_clipboard):
        remote = "/data/local/tmp/hdc_shot_%s.jpeg" % _stamp()
        r = self.api.hdc.shell("snapshot_display -f %s" % remote, target=serial, timeout=20)
        if not r["ok"]:
            blob = r["stdout"] or r["stderr"]
            hint = "snapshot_display 受限（部分版本需开放权限）" if "perm" in blob.lower() else blob.strip()[:120]
            return {"ok": False, "error": "截屏失败：%s" % hint}
        name = "screenshot_%s.jpeg" % _stamp()
        local = os.path.join(media_dir(), name)
        rr = self.api.hdc.run(["file", "recv", remote, local], target=serial, timeout=30)
        self.api.hdc.shell("rm -f %s" % remote, target=serial, timeout=10)
        if not rr["ok"] or not os.path.isfile(local) or os.path.getsize(local) == 0:
            return {"ok": False, "error": "拉取截屏失败: %s" % (rr["stdout"] or rr["stderr"]).strip()[:120]}
        if save_to_device:
            pic = "/storage/data/local/Pictures/%s" % name
            self.api.hdc.shell("mkdir -p /storage/data/local/Pictures", target=serial, timeout=10)
            self.api.hdc.shell("cp %s %s" % (remote, pic), target=serial, timeout=10)
        with open(local, "rb") as f:
            data = f.read()
        if copy_clipboard:
            self._copy_image_clipboard(local)
        action_log.record("screenshot", "snapshot_display -> %s" % name, serial=serial, ok=True)
        return {"ok": True, "file": local, "name": name,
                "preview": "data:image/jpeg;base64," + base64.b64encode(data).decode()}

    def _shot_ios(self, serial, copy_clipboard):
        guard = self.api._ios_guard()
        if guard:
            return {"ok": False, "error": guard["error"], "hint": guard.get("hint", "")}
        # 只做版本闸门判断，不走 capabilities()（避免任何多余的服务调用）。
        major = self._ios_major(serial) or 0
        if major >= 17:
            if IS_WIN:
                return {"ok": False,
                        "error": "iOS 17+ 的 DVT 服务需要 TUN 隧道，Windows 不支持",
                        "hint": "可用手机自带截屏后从文件页取"}
            return {"ok": False, "error": "iOS 17+ 需先启动 tunneld 隧道",
                    "hint": "以 root 运行: sudo pymobiledevice3 remote tunneld"}
        try:
            # 超时放宽：首次使用要自动下载并挂载开发者镜像（DDI）
            png = self.api.ios.screenshot(serial, timeout=300)  # bytes (PNG)
        except Exception as e:  # noqa
            msg = getattr(e, "msg", None) or str(e)
            out = {"ok": False, "error": "iOS 截屏失败: %s" % msg}
            hint = getattr(e, "hint", "")
            if hint:
                out["hint"] = hint
            return out
        name = "screenshot_%s.png" % _stamp()
        path = os.path.join(media_dir(), name)
        with open(path, "wb") as f:
            f.write(png)
        if copy_clipboard:
            self._copy_image_clipboard(path)
        action_log.record("screenshot", "pmd3 screenshot (%d KB)" % (len(png) // 1024),
                          serial=serial, ok=True)
        return {"ok": True, "file": path, "name": name,
                "preview": "data:image/png;base64," + base64.b64encode(png).decode()}

    @staticmethod
    def _copy_image_clipboard(path):
        """把图片放进系统剪贴板（尽力而为，失败不阻断主流程）。"""
        try:
            if IS_WIN:
                ps = ("powershell -NoProfile -STA -Command "
                      "\"Add-Type -AssemblyName System.WindowsForms;\" "
                      "Add-Type -AssemblyName System.Windows.Forms;")
                # 上面拼接易错，直接一条命令内联完成
                ps = ("powershell -NoProfile -STA -Command "
                      "\"Add-Type -AssemblyName System.Windows.Forms; "
                      "Add-Type -AssemblyName System.Drawing; "
                      "[System.Windows.Forms.Clipboard]::SetImage("
                      "[System.Drawing.Image]::FromFile('%s'))\"" % path.replace("'", "''"))
                subprocess.run(ps, timeout=15, creationflags=_no_window())
            else:
                osa = ("set the clipboard to (read (POSIX file \"%s\") as «class PNGf»)" % path)
                subprocess.run(["osascript", "-e", osa], timeout=15)
        except Exception:  # noqa
            pass

    @staticmethod
    def _demo_png():
        # 演示模式：1x1 透明 PNG
        b64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
               "AAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
        return "data:image/png;base64," + b64

    # -------------------------------------------------------------- 录屏
    def record_start(self, opts=None):
        api = self.api
        serial = api.current_serial
        if not serial:
            return {"ok": False, "error": "未选择设备"}
        opts = opts or {}
        plat = api._platform_of(serial)
        with _LOCK:
            if serial in _RECORDS:
                return {"ok": False, "error": "该设备已在录制中"}
            caps = self.capabilities(serial)
            if not caps["record"]:
                return {"ok": False, "error": caps["recordHint"], "hint": "record-unsupported"}
            if plat == "android":
                return self._rec_start_android(serial, opts)
            if plat == "ios":
                return self._rec_start_ios(serial, opts)
        return {"ok": False, "error": "未知平台"}

    # ------------------------------------------------------ iOS 录屏（连续截屏帧）
    @staticmethod
    def _find_ffmpeg():
        """ffmpeg 可执行文件路径：PATH 优先，常见安装目录兜底。"""
        import shutil

        p = shutil.which("ffmpeg")
        if p:
            return p
        if IS_WIN:
            import glob

            for pat in (r"C:\software\ffmpeg*\bin\ffmpeg.exe",
                        r"C:\ffmpeg*\bin\ffmpeg.exe",
                        r"C:\Program Files\ffmpeg*\bin\ffmpeg.exe"):
                hits = sorted(glob.glob(pat))
                if hits:
                    return hits[0]
        return ""

    def _rec_start_ios(self, serial, opts):
        api = self.api
        ffmpeg = self._find_ffmpeg()
        if not ffmpeg:
            return {"ok": False,
                    "error": "未找到 ffmpeg，无法合成录屏视频",
                    "hint": "请安装 ffmpeg 并加入 PATH 后重启工具"}
        major = self._ios_major(serial) or 0
        if major >= 17:
            if IS_WIN:
                return {"ok": False,
                        "error": "Windows 平台不支持 iOS 17+ USB 录屏（iOS 17+ 需要 TUN 隧道）",
                        "hint": "备选：AirPlay 无线录屏（后续版本考虑），或手机自带录屏后从文件页拉取",
                        "hint_tag": "record-unsupported"}
            return {"ok": False, "error": "iOS 17+ 的录屏链路需要 TUN 隧道，本工具当前 USB 直连不支持",
                    "hint": "备选：手机自带录屏后从文件页拉取",
                    "hint_tag": "record-unsupported"}
        try:
            fps = float(opts.get("fps") or 8)
        except (TypeError, ValueError):
            fps = 8.0
        fps = max(2.0, min(15.0, fps))
        try:
            limit = int(opts.get("duration") or 60)
        except (TypeError, ValueError):
            limit = 60
        # 上限 5000 分钟（用户自定义上限）；帧实时落盘，长录注意磁盘余量
        limit = max(5, min(300000, limit))
        tmpdir = os.path.join(media_dir(), ".ios_rec_%s" % _stamp())
        os.makedirs(tmpdir, exist_ok=True)
        stop_event = threading.Event()
        rec = {
            "platform": "ios", "ffmpeg": ffmpeg, "tmpdir": tmpdir,
            "stop_event": stop_event, "times": [], "count": 0,
            "error": "", "started": time.time(), "limit": limit,
            "fps": fps, "opts": dict(opts or {}),
            "name": _safe_name(opts.get("name"), ".mp4"),
            "thread": None,
            # 收尾互斥：用户点停止 / 中断自动保存 / 到时自动收尾可能并发，
            # 谁先拿到 fin_lock 谁编码，另一个等 finished 结果，防止双份编码或死锁
            "fin_lock": threading.Lock(), "finished": None,
        }
        # 先同步截一帧：DDI 未挂载/设备未信任等问题在点开始时就暴露，
        # 而不是录了半天后停止时才发现一帧都没有。
        try:
            png = api.ios.screenshot(serial, timeout=300)
        except Exception as e:  # noqa: BLE001
            import shutil as _sh
            _sh.rmtree(tmpdir, ignore_errors=True)
            msg = getattr(e, "msg", None) or str(e)
            out = {"ok": False, "error": "iOS 录屏启动失败: %s" % msg}
            hint = getattr(e, "hint", "")
            if hint:
                out["hint"] = hint
            return out

        def _capture():
            def write_frame(idx, data, t):
                with open(os.path.join(tmpdir, "%06d.png" % idx), "wb") as f:
                    f.write(data)
                rec["times"].append(t)
                rec["count"] = idx + 1

            write_frame(0, png, time.time())
            interval = 1.0 / rec["fps"]
            fails = 0
            while not stop_event.is_set():
                if time.time() - rec["started"] >= limit:
                    rec["auto"] = True  # 到时自动收尾
                    break
                t0 = time.time()
                try:
                    data = api.ios.screenshot(serial, timeout=60)
                    write_frame(rec["count"], data, time.time())
                    fails = 0
                except Exception as e:  # noqa: BLE001
                    fails += 1
                    rec["last_err"] = getattr(e, "msg", None) or str(e)
                    if fails >= 3:
                        rec["error"] = ("连续 3 次截屏失败，录制中止: %s" % rec["last_err"])
                        break
                stop_event.wait(max(0.0, interval - (time.time() - t0)))

            # 用户点停止（stop_event 已置位）由 record_stop 负责收尾；
            # 其余退出路径（设备断开/连续失败/到时）都在这里自动保存已录帧，
            # 一帧都不丢，再也不出现"断开就直接没了"。
            if stop_event.is_set():
                return
            if not rec.get("error"):
                rec["error"] = "到达时长上限" if rec.get("auto") else "录制中断（设备断开或采样异常）"
            self._auto_finalize(rec, serial)

        th = threading.Thread(target=_capture, name="ios-recorder", daemon=True)
        rec["thread"] = th
        _RECORDS[serial] = rec
        th.start()
        action_log.record("record", "ios screencast %s @ %dfps" % (tmpdir, int(fps)),
                          serial=serial, ok=True)
        return {"ok": True, "limit": limit, "fps": fps}

    @staticmethod
    def _unique_path(dir_, name):
        base, ext = os.path.splitext(name)
        cand = os.path.join(dir_, name)
        n = 1
        while os.path.exists(cand):
            cand = os.path.join(dir_, "%s_%d%s" % (base, n, ext))
            n += 1
        return cand

    def _auto_finalize(self, rec, serial):
        """中断/到时后的自动收尾：把已录帧合成 MP4，结果留给 record_status 轮询取走。

        在采集线程里被调用；若用户恰好同时点了停止（record_stop 已持锁），
        这里直接放弃，由那条路负责编码并返回结果。
        """
        with _LOCK:
            if _RECORDS.get(serial) is rec:
                _RECORDS.pop(serial, None)
        if self._rec_stop_ios(rec, serial) is None:
            return  # record_stop 的收尾已在进行
        with _LOCK:
            self._auto_finished[serial] = rec.get("finished")
        action_log.record("record", "auto save %s（%s）"
                          % (rec.get("name"), rec.get("error")), serial=serial, ok=True)

    def _rec_stop_ios(self, rec, serial):
        """停止 iOS 录制并编码。返回 None 表示收尾已由其他线程接管。"""
        if not rec["fin_lock"].acquire(blocking=False):
            return None
        result = None
        try:
            rec["stop_event"].set()
            # 自动收尾是从采集线程自己调进来的，join 自己会死锁，必须跳过
            if rec["thread"] is not None and rec["thread"] is not threading.current_thread():
                rec["thread"].join(timeout=120)
            result = self._encode_ios_frames(rec, serial)
            return result
        finally:
            rec["finished"] = result or {"ok": False,
                                         "error": rec.get("error") or "录制收尾失败"}
            rec["fin_lock"].release()

    @staticmethod
    def _wait_finished(rec, timeout=600):
        """收尾被别的线程持有时，等它的结果。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            f = rec.get("finished")
            if f is not None:
                return f
            time.sleep(0.3)
        return {"ok": False, "error": "录制收尾超时"}

    def _encode_ios_frames(self, rec, serial):
        count = rec["count"]
        times = rec["times"]
        tmpdir = rec["tmpdir"]
        try:
            if count < 2 or len(times) < 2:
                return {"ok": False,
                        "error": rec.get("error") or "有效帧不足，未能生成视频"}
            # 采集阶段手动点了停止（或自动到时）后一般还有最后半帧间隔，
            # 全程实际帧率按时间戳计算，逐帧 duration 写进 concat 列表（VFR），
            # 保证播放速度与真实操作一致。
            durations = [max(0.001, times[i + 1] - times[i]) for i in range(len(times) - 1)]
            list_path = os.path.join(tmpdir, "list.txt")
            with open(list_path, "w", encoding="ascii") as f:
                f.write("ffconcat version 1.0\n")
                for i in range(count):
                    f.write("file '%06d.png'\n" % i)
                    f.write("duration %.3f\n" % durations[min(i, len(durations) - 1)])
                # concat demuxer 的已知行为：最后一帧必须重复列出才会被读到
                f.write("file '%06d.png'\n" % (count - 1))
            name = rec.get("name") or ("record_%s.mp4" % _stamp())
            out_path = self._unique_path(media_dir(), name)
            # 等待 ffmpeg 正常退出（moov 写完）再继续；绝不能 kill 半路进程
            proc = subprocess.run(
                [rec["ffmpeg"], "-y", "-hide_banner", "-loglevel", "error",
                 "-f", "concat", "-safe", "0", "-i", list_path,
                 "-vsync", "vfr", "-an",
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
                 "-pix_fmt", "yuv420p",
                 "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2",
                 "-movflags", "+faststart", out_path],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                timeout=900, creationflags=_no_window())
            if proc.returncode != 0 or not os.path.isfile(out_path) \
                    or os.path.getsize(out_path) == 0:
                err = (proc.stderr or b"").decode("utf-8", "replace").strip()[:200]
                action_log.record("record", "ffmpeg encode", serial=serial,
                                  ok=False, output=err)
                return {"ok": False, "error": "视频合成失败: %s" % (err or "未知错误")}
            action_log.record("record", "ios mp4 %s（%d 帧）" % (out_path, count),
                              serial=serial, ok=True)
            return {"ok": True, "file": out_path, "name": os.path.basename(out_path),
                    "seconds": int(time.time() - rec["started"]), "frames": count}
        finally:
            import shutil as _sh
            _sh.rmtree(tmpdir, ignore_errors=True)

    def _rec_start_android(self, serial, opts):
        adb = self.api.adb
        remote = "/sdcard/adb_record_%s.mp4" % _stamp()
        args = ["shell", "screenrecord"]
        size = str(opts.get("resolution") or "").strip()  # "1080p"/"720p"/""
        if size == "1080p":
            args += ["--size", "1080x2400"]
        elif size == "720p":
            args += ["--size", "720x1600"]
        try:
            mbps = float(opts.get("bitrate") or 8)
        except (TypeError, ValueError):
            mbps = 8.0
        args += ["--bit-rate", str(int(mbps * 1000000))]
        try:
            limit = int(opts.get("duration") or 60)
        except (TypeError, ValueError):
            limit = 60
        limit = max(5, min(180, limit))
        args += ["--time-limit", str(limit)]
        if opts.get("audio"):
            args += ["--audio-source", "system"]
        args += [remote]
        # 录制前开关
        if opts.get("touch"):
            adb.run(["shell", "settings put system pointer_location 1"],
                    serial=serial, timeout=10)
        if opts.get("stayAwake"):
            adb.run(["shell", "svc power stayon usb"], serial=serial, timeout=10)
        cmd = adb.build_cmd(args, serial=serial)
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                creationflags=_no_window())
        _RECORDS[serial] = {
            "proc": proc, "platform": "android", "remote": remote,
            "started": time.time(), "limit": limit,
            "opts": dict(opts or {}), "name": _safe_name(opts.get("name"), ".mp4"),
        }
        action_log.record("record", "screenrecord %s" % remote, serial=serial, ok=True)
        return {"ok": True, "remote": remote, "limit": limit}

    def record_stop(self):
        api = self.api
        serial = api.current_serial
        if not serial:
            return {"ok": False, "error": "未选择设备"}
        with _LOCK:
            rec = _RECORDS.pop(serial, None)
        if not rec:
            # 中断/到时的自动收尾可能刚出结果：直接把它当作本次停止的返回
            fin = self._auto_finished.pop(serial, None)
            if fin:
                return fin
            return {"ok": False, "error": "当前设备没有进行中的录制"}
        if rec["platform"] == "android":
            return self._rec_stop_android(rec, serial)
        if rec["platform"] == "ios":
            r = self._rec_stop_ios(rec, serial)
            return r if r is not None else self._wait_finished(rec)
        return {"ok": False, "error": "暂不支持的录制类型"}

    def _rec_stop_android(self, rec, serial):
        adb = self.api.adb
        # SIGINT 让 screenrecord 写完封装退出，绝不能强杀
        adb.run(["shell", "pkill -l2 -f screenrecord"], serial=serial, timeout=10)
        try:
            rec["proc"].wait(timeout=10)
        except Exception:  # noqa
            pass
        if rec["opts"].get("touch"):
            adb.run(["shell", "settings put system pointer_location 0"],
                    serial=serial, timeout=10)
        if rec["opts"].get("stayAwake"):
            adb.run(["shell", "svc power stayon false"], serial=serial, timeout=10)
        # 轮询远端文件大小稳定（封装收尾），超时 20s
        stable = 0
        last = -1
        for _ in range(20):
            size = self._remote_size(rec["remote"], serial)
            if 0 < size == last:
                stable += 1
                if stable >= 2:
                    break
            else:
                stable = 0
            last = size
            time.sleep(1)
        name = rec.get("name") or ("record_%s.mp4" % _stamp())
        local = os.path.join(media_dir(), name)
        r = adb.run(["pull", rec["remote"], local], serial=serial, timeout=120)
        adb.run(["shell", "rm -f %s" % rec["remote"]], serial=serial, timeout=10)
        ok = r["ok"] and os.path.isfile(local) and os.path.getsize(local) > 0
        action_log.record("record", "pull %s" % name, serial=serial, ok=ok,
                          output="" if ok else (r["stdout"] or r["stderr"])[:120])
        if not ok:
            return {"ok": False, "error": "拉取录制文件失败: %s" % (r["stdout"] or r["stderr"]).strip()[:120]}
        return {"ok": True, "file": local, "name": name,
                "seconds": int(time.time() - rec["started"])}

    def _remote_size(self, remote, serial):
        r = self.api.adb.run(["shell", "stat -c %%s %s" % remote], serial=serial, timeout=10)
        s = (r["stdout"] or "").strip()
        return int(s) if s.isdigit() else -1

    def record_status(self):
        """前端录制期间轮询：后端是否还在录、中断自动保存的结果、恢复出的历史文件。

        三类场景都从这里出口：
        1. recording=True  —— 采集线程仍在工作；
        2. finished        —— 中断/到时后 _auto_finalize 已把已录帧合成 MP4；
        3. recovered       —— 上次进程异常退出遗留的 .ios_rec_* 帧目录已补合成。
        """
        serial = self.api.current_serial
        with _LOCK:
            rec = _RECORDS.get(serial) if serial else None
            recovered = self._recovered.pop("*", [])
        out = {"ok": True, "recovered": recovered}
        if rec:
            out["recording"] = True
            out["limit"] = rec.get("limit", 0)
            out["elapsed"] = int(time.time() - rec.get("started", time.time()))
        else:
            out["recording"] = False
            out["finished"] = self._auto_finished.pop(serial, None) if serial else None
        return out

    def _recover_orphans_async(self):
        """启动/进页面时扫描上次异常退出遗留的帧目录，后台补合成 MP4。"""
        if self._recover_started:
            return
        self._recover_started = True

        def work():
            import glob as _glob
            import shutil as _sh
            if not self._find_ffmpeg():
                return
            for d in _glob.glob(os.path.join(media_dir(), ".ios_rec_*")):
                if not os.path.isdir(d):
                    continue
                frames = sorted(_glob.glob(os.path.join(d, "*.png")))
                if len(frames) < 2:
                    _sh.rmtree(d, ignore_errors=True)
                    continue
                mtimes = [os.path.getmtime(p) for p in frames]
                m = re.search(r"\.ios_rec_(\d+)", d)
                rec = {
                    "platform": "ios", "ffmpeg": self._find_ffmpeg(), "tmpdir": d,
                    "stop_event": threading.Event(), "times": mtimes,
                    "count": len(frames), "error": "",
                    "started": mtimes[0], "limit": 0, "fps": 0, "opts": {},
                    "name": "record_recover_%s.mp4" % (m.group(1) if m else _stamp()),
                    "thread": None, "fin_lock": threading.Lock(), "finished": None,
                }
                r = self._encode_ios_frames(rec, "RECOVER")
                if r.get("ok"):
                    with _LOCK:
                        self._recovered.setdefault("*", []).append(r["name"])
                    action_log.record("record", "恢复中断录屏 %s（%d 帧）"
                                      % (r["name"], r.get("frames", 0)), ok=True)

        threading.Thread(target=work, name="rec-recover", daemon=True).start()

    # -------------------------------------------------------------- 媒体库
    def _probe_duration(self, path):
        """ffprobe 探测视频时长（秒）；失败返回 None。按 (名字, mtime, 大小) 缓存。"""
        try:
            st = os.stat(path)
        except OSError:
            return None
        key = (os.path.basename(path), st.st_mtime, st.st_size)
        if key in self._dur_cache:
            d = self._dur_cache[key]
            return d if d and d > 0 else None
        ff = self._find_ffmpeg()
        if not ff:
            return None
        probe = os.path.join(os.path.dirname(ff),
                             "ffprobe" + (".exe" if IS_WIN else ""))
        if not os.path.isfile(probe):
            self._dur_cache[key] = -1.0
            return None
        try:
            r = subprocess.run(
                [probe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", path],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=10, creationflags=_no_window())
            d = float(r.stdout.decode("utf-8", "replace").strip())
        except Exception:  # noqa: BLE001 - 探测失败不阻塞列表
            d = -1.0
        self._dur_cache[key] = d
        return d if d > 0 else None

    def list_files(self, keyword=""):
        self._recover_orphans_async()
        out = []
        d = media_dir()
        for n in sorted(os.listdir(d), reverse=True):
            p = os.path.join(d, n)
            if not os.path.isfile(p):
                continue
            if keyword and keyword.lower() not in n.lower():
                continue
            ext = os.path.splitext(n)[1].lower()
            kind = "video" if ext == ".mp4" else ("image" if ext in (".png", ".jpeg", ".jpg") else "other")
            try:
                st = os.stat(p)
            except OSError:
                continue
            out.append({"name": n, "kind": kind, "size": st.st_size,
                        "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
                        "duration": self._probe_duration(p) if kind == "video" else None,
                        "path": p})
        return out

    def open_file(self, name):
        p = os.path.join(media_dir(), _safe_name(name))
        if not os.path.isfile(p):
            return {"ok": False, "error": "文件不存在"}
        try:
            if IS_WIN:
                os.startfile(p)  # noqa
            elif hasattr(os, "system"):
                subprocess.Popen(["open" if hasattr(os, "uname") and os.uname().sysname == "Darwin"
                                  else "xdg-open", p])
            return {"ok": True}
        except Exception as e:  # noqa
            return {"ok": False, "error": str(e)}

    def delete_file(self, name):
        p = os.path.join(media_dir(), _safe_name(name))
        if not os.path.isfile(p):
            return {"ok": False, "error": "文件不存在"}
        try:
            os.remove(p)
            action_log.record("media_delete", name, ok=True)
            return {"ok": True}
        except OSError as e:
            return {"ok": False, "error": str(e)}

    def rename_file(self, old, new):
        src = os.path.join(media_dir(), _safe_name(old))
        dst = os.path.join(media_dir(), _safe_name(new))
        if not os.path.isfile(src):
            return {"ok": False, "error": "文件不存在"}
        if os.path.exists(dst):
            return {"ok": False, "error": "目标文件名已存在"}
        try:
            os.rename(src, dst)
            return {"ok": True, "name": os.path.basename(dst)}
        except OSError as e:
            return {"ok": False, "error": str(e)}

    def save_as(self, name):
        """另存为：弹出系统保存对话框，把媒体库文件复制到用户指定位置。"""
        src = os.path.join(media_dir(), _safe_name(name))
        if not os.path.isfile(src):
            return {"ok": False, "error": "文件不存在"}
        win = getattr(self.api, "_window", None)
        if win is None:
            return {"ok": False, "error": "窗口未就绪"}
        import webview

        dest = self.api._on_ui_thread(
            lambda: win.create_file_dialog(webview.SAVE_DIALOG,
                                           save_filename=os.path.basename(src)))
        if not dest:
            return {"ok": True, "canceled": True}
        if isinstance(dest, (list, tuple)):
            dest = dest[0] if dest else ""
        if not dest:
            return {"ok": True, "canceled": True}
        try:
            import shutil
            shutil.copyfile(src, dest)
            action_log.record("media_save_as", "%s -> %s" % (name, dest), ok=True)
            return {"ok": True, "dest": dest}
        except OSError as e:
            return {"ok": False, "error": "保存失败: %s" % e}
