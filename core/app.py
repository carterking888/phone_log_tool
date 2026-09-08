# -*- coding: utf-8 -*-
"""应用启动逻辑（会被 Cython 编译为 pyd，源码不入包）。

main.py 只做入口转发，保持 .py；真正的启动流程在这里。
"""
import functools
import http.server
import json
import logging
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.parse

try:
    import webview
except ModuleNotFoundError:
    sys.stderr.write(
        "[错误] 缺少依赖 pywebview\n"
        '  pip install "pywebview>=5.0,<6.0"\n'
        "  注意：包名是 pywebview，不是 webview\n"
    )
    sys.exit(1)

from core.api import Api, APP_VERSION  # noqa: E402

APP_TITLE = "ADB 日志与设备调试工具 v" + APP_VERSION


def resource_path(rel):
    base = getattr(sys, "_MEIPASS", os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(base, rel)


def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# 文件拖放（Windows / EdgeChromium 专属能力）
#
# WebView2（Chromium 安全模型）不给 JS File 对象真实磁盘路径，HTML5 drop 事件
# 拿到的只有文件名和大小——安装 2GB 的 APK 不可能走 JS 读内容，所以"拖 APK 进
# 弹窗"在纯前端实现不了。
#
# 原理：Chromium 对未被页面消费的文件拖放，默认行为是"导航到该文件"（可渲染
# 类型，如 .html）或"触发下载"（二进制类型，如 .apk/.ipa）。两个方向都在
# CoreWebView2 事件里暴露本地路径：
#   NavigationStarting -> args.Uri = file:///D:/xx.apk
#   DownloadStarting   -> args.DownloadOperation.Uri = file:///D:/xx.apk
# 这里把两条路都拦下（Cancel），把真实路径回传给前端 AdbApp.onNativeDrop()。
# 因此前端**不能**对 drop/dragover preventDefault（那会吃掉默认行为导致
# 两个事件都不触发）——拖放提示高亮之类也不要做。
# ---------------------------------------------------------------------------
DROP_EXTS = (".apk", ".xapk", ".apks", ".ipa")


def _file_uri_to_path(uri):
    try:
        path = urllib.parse.unquote(urllib.parse.urlparse(uri).path or "")
    except Exception:  # noqa: BLE001
        return ""
    path = path.lstrip("/")
    if not path or path.startswith("<"):  # 排除 file:///<非法>
        return ""
    return os.path.normpath(path)


def install_drop_hook(window):
    """GUI 就绪后调用：拦截 WebView2 的文件拖放，路径回传前端。

    线程纪律（见 core/api.py 的 _on_ui_thread 注释）：WinForms/WebView2 的
    属性与事件挂接必须回 UI 线程 —— 一律走 `native.Invoke(Func[...](fn))`，
    后台线程只做轮询等待，不直接碰 CLR 对象。
    """

    def _ui(native, fn):
        from System import Func, Type  # pythonnet（pywebview 已加载 CLR）

        box = {}

        def run():
            try:
                box["value"] = fn()
            except Exception as e:  # noqa: BLE001
                box["error"] = e

        native.Invoke(Func[Type](run))
        if "error" in box:
            raise box["error"]
        return box.get("value")

    def _push(uri):
        path = _file_uri_to_path(uri)
        if not path:
            return
        ext = os.path.splitext(path)[1].lower()
        if ext in DROP_EXTS:
            window.evaluate_js("AdbApp && AdbApp.onNativeDrop(%s)" % json.dumps(path))
        else:
            window.evaluate_js(
                "AdbApp && AdbApp.onNativeDropRejected(%s)" % json.dumps(os.path.basename(path)))

    def _on_file_drop(args_get_uri, cancel):
        try:
            uri = args_get_uri()
        except Exception:  # noqa: BLE001
            return
        if not uri.startswith("file:"):
            return
        cancel()
        _push(uri)

    def _attach_on_ui(native):
        """UI 线程内执行：取 WebView2 控件并挂接事件。成功返回 True。"""
        def job():
            wv = native.browser.webview
            core = wv.CoreWebView2
            if core is None:
                return False

            def on_nav(_sender, args):
                _on_file_drop(lambda: str(args.Uri), lambda: setattr(args, "Cancel", True))

            def on_download(_sender, args):
                _on_file_drop(
                    lambda: str(args.DownloadOperation.Uri),
                    lambda: setattr(args, "Cancel", True))

            core.NavigationStarting += on_nav
            dl_ok = True
            try:
                core.DownloadStarting += on_download
            except Exception as e:  # noqa: BLE001 - 老WebView2运行时缺该事件：.apk/.ipa拖入会弹下载/打开方式提示
                dl_ok = False
                print("[drop-hook][WARN] DownloadStarting 挂接失败: %r" % e)
            return True, dl_ok
        return _ui(native, job)

    def _wait_and_attach():
        # 后台线程只轮询 window.native（纯 Python 属性，安全），CLR 操作全走 Invoke
        native = None
        for _ in range(300):  # 最多等 ~30s
            native = getattr(window, "native", None)
            if native is not None:
                break
            time.sleep(0.1)
        if native is None:
            print("[drop-hook] 未取到原生窗口，拖放不可用（不影响其他功能）")
            return
        for _ in range(300):
            try:
                res = _attach_on_ui(native)
                if res:
                    dl_ok = bool(res[1]) if isinstance(res, tuple) else False
                    print("[drop-hook] 拖放拦截已挂接（导航+下载双拦截=%s，apk/ipa 拖入会回传给前端）"
                          % ("开" if dl_ok else "关"))
                    return
            except Exception as e:  # noqa: BLE001 - 挂接失败只影响拖放
                print("[drop-hook] 挂接失败（不影响其他功能）: %r" % e)
                return
            time.sleep(0.1)
        print("[drop-hook] 等待 CoreWebView2 超时，拖放不可用（不影响其他功能）")

    threading.Thread(target=_wait_and_attach, daemon=True, name="webview-drop-hook").start()


# ---------------------------------------------------------------------------
# pywebview 5.x + pythonnet 在 Windows 上的跨线程 COM 噪音
#
# 根因（已定位到 pywebview 5.4 源码，非本项目 bug）：
#   1. webview/util.py 的 js_bridge_call() 把 js_api 调用放进独立 Thread 执行
#   2. 该线程的 _call() 末尾用 window.evaluate_js(code) 回传结果 —— 仍在后台线程
#   3. platforms/edgechromium.py 的 evaluate_js() 里 self.webview.Invoke(...)
#      跨线程访问 WebView2 COM，pythonnet 对每个属性抛
#      E_NOINTERFACE (0x80004002) / "can only be accessed from the UI thread"
#   4. pythonnet 打印 "Error while processing window.native.<属性链>" 后吞掉异常。
#      evaluate_js 本身是成功的（失败会打印 "Error occurred in script"），
#      所以功能不受影响，纯粹是刷屏。
#
# 对策：过滤这类噪音（这里）+ 前端自适应降频（web/js/app.js 的日志轮询）。
# 需要排查 pywebview 真实错误时用 `adb_tool.exe --verbose` 关掉过滤。
# ---------------------------------------------------------------------------
class _EdgeComNoiseFilter(logging.Filter):
    """只拦 pythonnet 对 window.native.* 的反射噪音，其余 pywebview 日志照常放行。"""

    MARK = "Error while processing window.native."

    def __init__(self):
        super().__init__()
        self.suppressed = 0

    def filter(self, record):
        try:
            msg = record.getMessage()
        except Exception:  # noqa
            return True
        if self.MARK in msg:
            self.suppressed += 1
            return False
        return True


def install_noise_filter():
    f = _EdgeComNoiseFilter()
    logging.getLogger("pywebview").addFilter(f)
    logging.getLogger().addFilter(f)  # 兜底：pythonnet 若改走 root logger
    return f


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


def start_server(directory):
    port = find_free_port()
    handler = functools.partial(QuietHandler, directory=directory)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


# ---------------------------------------------------------------------------
# 诊断模式：ADB_TOOL_DIAG=1 时启用（正常启动零开销）
#
# 窗口模式(--noconsole)下 sys.stderr 是 None，pywebview 里 generate_func()
# 的异常经 logger.exception 写 stderr 时直接丢失 —— 前端表现为 pywebview.api
# 是空对象、所有接口报 "api not found: xxx"。这里把日志落盘并主动枚举 api，
# 用于定位"桥接层为什么是空的"这类问题。
# ---------------------------------------------------------------------------
def setup_diag():
    if os.environ.get("ADB_TOOL_DIAG") != "1":
        return None
    path = os.environ.get("ADB_TOOL_DIAG_OUT") or os.path.join(
        tempfile.gettempdir(), "adb_tool_diag.log")
    logging.basicConfig(
        filename=path, level=logging.DEBUG,
        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger().debug("[diag] logging to %s", path)
    return path


def start_diag_probe(window, log_path):
    def _probe():
        time.sleep(10)
        try:
            keys = window.evaluate_js(
                "JSON.stringify(Object.keys(window.pywebview.api || {}))")
        except Exception as e:  # noqa
            keys = "evaluate_js failed: %r" % e
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write("\n[diag] pywebview.api keys: %s\n" % keys)
        except Exception:
            pass

    threading.Thread(target=_probe, daemon=True).start()


def main():
    verbose = "--verbose" in sys.argv
    noise_filter = None if verbose else install_noise_filter()
    diag_path = setup_diag()

    web_dir = resource_path("web")
    if not os.path.isdir(web_dir):
        sys.stderr.write("[错误] 未找到 web 目录: %s\n" % web_dir)
        sys.exit(1)

    api = Api()
    try:
        api.refresh_env()
    except Exception as e:  # noqa
        print("[warn] 环境检测失败: %s" % e)

    httpd, port = start_server(web_dir)
    url = "http://127.0.0.1:%d/index.html" % port

    window = webview.create_window(
        APP_TITLE,
        url=url,
        width=1440,
        height=900,
        min_size=(1100, 700),
        js_api=api,
        background_color="#0d1117",
    )
    # 保持私有，避免 pywebview 把 Window 当作 js_api 的公开子对象递归扫描。
    api._window = window
    if diag_path:
        start_diag_probe(window, diag_path)

    try:
        if sys.platform == "win32":
            install_drop_hook(window)
        webview.start(debug=False)
    finally:
        try:
            httpd.shutdown()
        except Exception:
            pass
        try:
            api.stop_logcat()
        except Exception:
            pass
        if noise_filter and noise_filter.suppressed:
            print(
                "[info] 已抑制 %d 条 pywebview 跨线程 COM 噪音（无害，"
                "排查 pywebview 自身问题用 adb_tool.exe --verbose）"
                % noise_filter.suppressed
            )
