# -*- coding: utf-8 -*-
"""真实 WebView2 渲染冒烟：启动 pywebview -> 抓取 DOM -> 关闭。

用法：  <venv-python> _render_check.py
"""
import json
import os
import sys
import threading
import time

sys.path.insert(0, __file__.rsplit("\\", 1)[0].rsplit("/", 1)[0])

import webview  # noqa: E402
# 启动逻辑 2026-09 已迁到 core/app.py（main.py 只剩 3 行转发，供 PyInstaller 当入口）
from core.app import resource_path, start_server  # noqa: E402
from core.api import Api  # noqa: E402

PROBE = """
(function () {
  var out = {};
  var vis = function (el) { var e = el; while (e && e.style) { if (e.style.display === 'none') return false; e = e.parentElement; } return true; };
  out.mustacheLeft = (document.body.innerHTML.match(/\\{\\{[^}]{0,40}\\}\\}/g) || []).length;
  out.libs = { PetiteVue: typeof window.PetiteVue, layui: typeof window.layui };
  out.bridge = !!(window.pywebview && window.pywebview.api);
  out.envDemo = !!(window.AdbApp && window.AdbApp.env && window.AdbApp.env.demo);
  out.devCards = document.querySelectorAll('.devcard').length;
  out.detailFields = document.querySelectorAll('.detailgrid .dfield').length;
  out.visibleSections = [].slice.call(document.querySelectorAll('section.page')).filter(vis).length;
  out.bodyTextLen = (document.body.innerText || '').trim().length;

  // 切到日志页并等 3 秒
  document.querySelectorAll('.nav__item')[1].click();
  return new Promise(function (resolve) {
    setTimeout(function () {
      var rows = [].slice.call(document.querySelectorAll('.logtable tbody tr'))
        .filter(function (r) { return !r.classList.contains('row-empty'); });
      out.logRunning = window.AdbApp.logs.running;
      out.logRows = rows.length;
      var t = document.getElementById('logTableWrap');
      out.logBox = t ? (t.offsetWidth + 'x' + t.offsetHeight) : 'NO_BOX';
      out.statusBar = (document.querySelector('.statusbar') || {}).innerText || '';

      document.querySelectorAll('.nav__item')[2].click();
      setTimeout(function () {
        out.appRows = [].slice.call(document.querySelectorAll('.tablewrap .table tbody tr'))
          .filter(function (r) { return !r.classList.contains('row-empty'); }).length;

        document.querySelectorAll('.nav__item')[3].click();
        setTimeout(function () {
          out.fileRows = [].slice.call(document.querySelectorAll('.tablewrap .table tbody tr'))
            .filter(function (r) { return !r.classList.contains('row-empty'); }).length;
          out.crumbs = document.querySelectorAll('.crumb').length;
          resolve(JSON.stringify(out));
        }, 1200);
      }, 1200);
    }, 3000);
  });
})()
"""


def bootstrap():
    win = webview.windows[0]
    time.sleep(3)
    try:
        raw = win.evaluate_js(PROBE)
        print("PROBE:", raw)
    except Exception as e:  # noqa
        print("PROBE_ERROR:", e)
    try:
        win.destroy()
    except Exception:
        pass


TIMEOUT_SEC = 90


def main():
    # 看门狗：没有可弹窗的桌面会话（CI / 无头 / 远程桌面断开）时，
    # webview.start() 会一直阻塞、脚本永久挂住（实测 3 分半无输出）。
    # 硬超时直接退出，让调用方能拿到明确失败而不是干等。
    def _guard():
        sys.stderr.write(
            "\n[TIMEOUT] %ds 内窗口未完成渲染 —— 多半是当前会话无法创建窗口。\n"
            "          真实渲染冒烟需要在有桌面的会话里跑（不要走 SSH / 无头 CI）。\n" % TIMEOUT_SEC)
        sys.stderr.flush()
        os._exit(2)

    watchdog = threading.Timer(TIMEOUT_SEC, _guard)
    watchdog.daemon = True
    watchdog.start()

    api = Api()
    api.refresh_env()
    server, port = start_server(resource_path("web"))
    url = "http://127.0.0.1:%d/index.html" % port
    print("URL:", url, "demo:", api.env["demo"], "adb:", api.env["adbPath"])
    webview.create_window("render smoke", url=url, width=1440, height=900, js_api=api)
    api._window = webview.windows[0] if webview.windows else None
    try:
        webview.start(bootstrap, debug=False)
    finally:
        watchdog.cancel()
        try:
            server.shutdown()
        except Exception:
            pass


if __name__ == "__main__":
    main()
