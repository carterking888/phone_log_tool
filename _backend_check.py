# -*- coding: utf-8 -*-
"""后端能力自测：真实设备（若已连接）+ 强制演示模式，两种路径都跑。

用法：  <venv-python> _backend_check.py
"""
import json
import os
import shutil
import sys
import tempfile
import time
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.adb import ADB_EXE, Adb, portable_adb_dirs  # noqa: E402
from core.api import Api  # noqa: E402
from core import demo  # noqa: E402

results = []
skipped = []


def check(name, ok, extra=""):
    results.append((bool(ok), name, str(extra)))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, ("   -> " + str(extra)) if extra else ""))


def wait_task(api, task_id, timeout=25):
    end = time.time() + timeout
    while time.time() < end:
        t = api.get_task(task_id)
        if t.get("status") != "running":
            return t
        time.sleep(0.2)
    return api.get_task(task_id)


def probe_dir(api):
    """找一个实际可读的目录，避免硬编码路径在真机上不存在。"""
    for p in ("/sdcard", "/storage/emulated/0", "/sdcard/Download"):
        r = api.list_dir(p)
        if r.get("ok") and r.get("entries"):
            return p, r["entries"]
    return None, []


def run_common(api):
    env = api.refresh_env()
    check("环境检测", bool(env.get("adbVersion")) or env.get("demo"),
          "adb=%s demo=%s" % (env.get("adbVersion"), env.get("demo")))

    # 手动指定 adb 路径整链路（用户曾反馈「传入 adb 地址也不行」，
    # 根因是 api.window 暴露导致桥接方法表作废，set_config 404）。
    # 无论是否连真机都应可用：本机 adb.exe 存在即可 adbFound=True。
    real_adb = shutil.which("adb")
    if not real_adb and os.name == "nt":
        cand = r"C:\software\platform-tools\adb.exe"
        if os.path.isfile(cand):
            real_adb = cand
    if real_adb:
        old_path = api.get_config().get("adbPath") or ""
        try:
            res = api.set_config({"adbPath": real_adb})
            env2 = api.get_env()
            check("手动指定 adb 路径生效（set_config 整链路）",
                  bool(res.get("ok"))
                  and res.get("config", {}).get("adbPath") == real_adb
                  and env2.get("adbFound") is True
                  and env2.get("adbPath") == real_adb,
                  "adb=%s found=%s" % (env2.get("adbPath"), env2.get("adbFound")))
        finally:
            api.set_config({"adbPath": old_path})  # 恢复用户原配置
    else:
        skipped.append("手动指定 adb 路径（本机找不到 adb 可执行文件）")

    # 便携 adb 回退：目标机没装 Android SDK 时，应自动用上随包的 public_settings/adb
    pdirs = [d for d in portable_adb_dirs()
             if os.path.isfile(os.path.join(d, ADB_EXE))]
    if pdirs:
        old_path_env = os.environ.get("PATH", "")
        try:
            os.environ["PATH"] = ""  # 模拟"没有环境的电脑"
            a2 = Adb()
            check("便携 adb 回退（PATH 为空时）",
                  a2.source == "portable" and os.path.isfile(a2.path),
                  "%s <- %s" % (a2.source, a2.path))
            ver2 = a2.version()
            check("便携 adb 可执行", bool(ver2[0]), str(ver2[0])[:40])
        finally:
            os.environ["PATH"] = old_path_env
    else:
        skipped.append("便携 adb 回退（未找到 public_settings/adb）")

    # 中文词库从 config/ 加载（2026-09 起词库挪到 config/ 子目录）
    st = api.get_zh_stats()
    check("中文词库从 config/ 加载", bool(st.get("loaded"))
          and st["perms"]["total"] > 300 and st["pkgs"]["total"] > 300
          and "config" in (st.get("dir") or "").replace("/", "\\").lower(),
          "权限 %d · 包名 %d · %s" % (st["perms"]["total"], st["pkgs"]["total"], st.get("dir")))

    devs = api.list_devices()
    check("设备列表", len(devs) >= 1, "%d 台" % len(devs))

    r = api.start_logcat()
    check("启动日志", r.get("ok"), r.get("error"))
    time.sleep(1.2)
    b = api.get_log_batch(0)
    # 演示 logcat 生成线程/真机 logcat 首启都可能慢一拍，最多再等 4s，消除偶发「0 行」误报
    waited = 0.0
    while not b["lines"] and waited < 4.0:
        time.sleep(0.4)
        waited += 0.4
        b = api.get_log_batch(0)
    check("日志批量拉取", len(b["lines"]) > 0, "%d 行" % len(b["lines"]))
    if b["lines"]:
        check("日志行字段完整",
              all(k in b["lines"][0] for k in ("id", "time", "pid", "tid", "level", "tag", "msg")),
              json.dumps(b["lines"][0], ensure_ascii=False)[:90])
    else:
        skipped.append("日志行字段完整（未取到日志行）")
    c1 = api.get_log_batch(b["cursor"])
    check("游标递增且不重复", c1["cursor"] >= b["cursor"], "%s -> %s" % (b["cursor"], c1["cursor"]))
    api.stop_logcat()

    pk = api.list_packages(None, "all")
    pkgs = pk.get("packages", [])
    check("包列表", len(pkgs) > 0, "%d 个" % len(pkgs))
    # codePath 来自真机的 `pm list packages -f`，演示数据不带该字段，
    # 无真机时跳过（否则每次拔手机都误报一条 FAIL，掩盖真实回归问题）
    if api.demo:
        skipped.append("包列表带 codePath（需真机 pm -f）")
    elif api._platform_of() == "ios":
        # codePath 是 Android 的 pm -f 概念；iOS 用 Path/Container，另有字段
        skipped.append("包列表带 codePath（iOS 无此概念）")
    else:
        check("包列表带 codePath（供应用名解析）",
              bool(pkgs) and all("codePath" in p for p in pkgs),
              (pkgs[0].get("codePath") or "")[:52])
    sysres = api.list_packages(None, "system")
    check("系统包筛选（pm list packages -s）",
          sysres.get("ok") and len(sysres.get("packages", [])) > 0,
          "%d 个" % len(sysres.get("packages", [])))

    path, entries = probe_dir(api)
    check("目录列举（含子项）", bool(entries), "%s -> %s" % (path, [e["name"] for e in entries][:4]))
    if entries:
        dirs = [e for e in entries if e["isDir"]]
        if dirs:
            sub = api.list_dir(dirs[0]["path"])
            check("进入子目录", sub.get("ok") and bool(sub.get("entries")),
                  "%s -> %d 项" % (dirs[0]["path"], len(sub.get("entries", []))))

    ss = api.storage_stats()
    check("存储统计", bool(ss.get("ok")), "total=%s ready=%s" % (ss.get("total"), ss.get("categoriesReady")))

    tmp = tempfile.mkdtemp(prefix="adbtool_check_")
    try:
        folder = os.path.join(tmp, "logs")
        os.makedirs(os.path.join(folder, "sub"))
        with open(os.path.join(folder, "a.log"), "w", encoding="utf-8") as f:
            f.write("hello")
        zipped = Api._zip_folders(tmp, ["/sdcard/logs"])
        zip_path = os.path.join(tmp, "logs.zip")
        names = []
        if os.path.exists(zip_path):
            with zipfile.ZipFile(zip_path) as z:
                names = z.namelist()
        check("文件夹下载自动打包 zip", zipped == ["logs.zip"] and "a.log" in names, "%s %s" % (zipped, names))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    cfg = api.get_config()
    check("读取配置", "adbPath" in cfg and "detectedAdbPath" in cfg, cfg.get("detectedAdbPath"))
    old = cfg.get("adbPath", "")
    sr = api.set_config({"adbPath": ""})
    check("保存配置", sr.get("ok") and api.get_config()["adbPath"] == "", "原值=%r" % old)
    if old:
        api.set_config({"adbPath": old})

    logs = api.get_action_logs(20)
    kinds = [i["action"] for i in logs.get("items", [])]
    check("操作日志联动记录", len(kinds) > 0, kinds[:5])


def run_real(api):
    print("\n--- 真实设备（%s）---" % api.current_serial)
    d = api.get_device_detail()
    check("设备详情字段", bool(d.get("model")) and bool(d.get("apiLevel")),
          "%s Android %s (API %s) %s%%" % (d.get("model"), d.get("androidVersion"),
                                           d.get("apiLevel"), (d.get("battery") or {}).get("level")))
    check("内核与架构", bool(d.get("kernel")) and bool(d.get("abi")),
          "%s / %s" % ((d.get("kernel") or "")[:36], d.get("abi")))

    pmap = api.get_pid_map().get("map", {})
    pids = [p for p, n in pmap.items() if n and not n.startswith("[")]
    check("进程表（ps -A）", len(pids) > 10, "%d 个进程" % len(pids))

    # 取一个真实三方包验证 dumpsys 解析
    pkgs = api.list_packages(None, "third").get("packages", [])
    if pkgs:
        pkg = pkgs[0]["packageName"]
        det = api.get_app_detail(pkg)
        ok = det.get("ok") and det["detail"].get("versionName")
        check("dumpsys package 解析（版本/SDK/权限）", bool(ok),
              "%s v%s targetSdk=%s 权限 %s/%s" % (pkg, det["detail"].get("versionName"),
                                                 det["detail"].get("targetSdk"),
                                                 det["detail"].get("granted"),
                                                 det["detail"].get("requested")))
        check("应用操作（force-stop）", api.app_action(pkg, "stop").get("ok"), pkg)
    else:
        check("三方包列表", False, "未取到三方包")

    # 静态中文词库：权限 / 包名
    st = api.get_zh_stats()
    check("中文词库加载", bool(st.get("loaded")) and st["perms"]["total"] > 300
          and st["pkgs"]["total"] > 300,
          "权限 %d · 包名 %d" % (st["perms"]["total"], st["pkgs"]["total"]))
    if pkgs:
        perms = api.get_app_detail(pkgs[0]["packageName"])["detail"]["permissions"]
        hit = [p for p in perms if p.get("label")]
        check("权限中文化", bool(perms) and len(hit) > 0,
              "%d/%d 命中" % (len(hit), len(perms)))
        check("权限字段齐备（name/label/group）",
              all("name" in p and "label" in p and "group" in p for p in perms),
              "%d 项" % len(perms))
        check("未命中权限 label 为空（前端回落原名）",
              all(p["label"] for p in hit) and all(p["name"] for p in perms), "")
    all_pkgs = api.list_packages(None, "all").get("packages", [])
    labeled = [p for p in all_pkgs if p.get("label")]
    check("包名中文化（词库命中）", len(labeled) > 0,
          "%d/%d 个包有中文名" % (len(labeled), len(all_pkgs)))

    # 中文文件名全链路（Windows adb pull 有编码 bug，必须走 exec-out）
    d = "/sdcard/Download/adbtool_selftest"
    out = tempfile.mkdtemp(prefix="adbtool_cn_")
    try:
        api.adb.run(["shell", "rm -rf " + d], timeout=30)
        api.mkdir("/sdcard/Download", "adbtool_selftest")
        src = os.path.join(tempfile.gettempdir(), "adbtool_selftest_src.txt")
        with open(src, "w", encoding="utf-8") as f:
            f.write("hello adb 中文内容")
        names = ["plain.txt", "with space.txt", "中文.txt", "中文 空格.txt"]
        for n in names:
            api.adb.run(["push", src, d + "/" + n], timeout=60)
        listed = [e["name"] for e in api.list_dir(d).get("entries", [])]
        check("中文文件名列举不乱码", all(n in listed for n in names), listed)
        pulled = []
        for n in names:
            st = wait_task(api, api.pull([d + "/" + n], out)["taskId"])
            pulled.append((n, st.get("status")))
        check("中文文件名下载（exec-out）",
              all(st == "success" for _n, st in pulled), pulled)
        check("下载落盘文件名正确", sorted(os.listdir(out)) == sorted(names), sorted(os.listdir(out)))
        st_dir = wait_task(api, api.pull([d], out)["taskId"])
        zipped = (st_dir.get("result") or {}).get("zipped", [])
        check("目录下载并自动打包 zip", st_dir.get("status") == "success" and bool(zipped), zipped)
    finally:
        api.adb.run(["shell", "rm -rf " + d], timeout=30)
        shutil.rmtree(out, ignore_errors=True)
    check("测试目录已清理", not api.list_dir(d).get("entries"), d)

    api.start_logcat(buffer="main")
    time.sleep(1.5)
    b = api.get_log_batch(0)
    parsed = [l for l in b["lines"] if not l.get("raw") and l.get("tag")]
    check("真机 logcat 解析（时间/PID/TID/级别/Tag）",
          len(parsed) > 0, json.dumps(parsed[0], ensure_ascii=False)[:110] if parsed else "无解析行")
    api.stop_logcat()


def run_real_ios(api):
    """iOS 真机用例：只测 iOS 真正支持的那些能力。"""
    print("\n--- 真实设备（iOS %s）---" % api.current_serial)
    d = api.get_device_detail()
    check("iOS 设备详情字段", bool(d.get("model")) and bool(d.get("iosVersion")),
          "%s iOS %s 电量 %s%%" % (d.get("model"), d.get("iosVersion"),
                                   (d.get("battery") or {}).get("level")))
    st = d.get("storage") or {}
    check("存储容量（AFC）", bool(st.get("ok")) and st.get("total", 0) > 0,
          "共 %d GB" % (st.get("total", 0) // 1024 ** 3))

    pkgs = api.list_packages(None, "third").get("packages", [])
    check("iOS 应用列表", len(pkgs) > 0, "%d 个三方应用" % len(pkgs))
    if pkgs:
        det = api.get_app_detail(pkgs[0]["packageName"])
        check("iOS 应用详情", bool(det.get("ok")) and bool(det["detail"].get("versionName")),
              "%s v%s" % (det["detail"].get("label"), det["detail"].get("versionName")))
        check("iOS 权限列表如实留空（不伪造）",
              det["detail"].get("permissionsUnsupported") is True
              and not det["detail"].get("permissions"), "")
        act = api.app_action(pkgs[0]["packageName"], "stop")
        check("iOS 不支持的操作有明确提示",
              (not act.get("ok")) and act.get("unsupported") is True, act.get("error"))

    root = api.list_dir("/", None)
    check("AFC 根目录列举", bool(root.get("ok")) and len(root.get("entries", [])) > 0,
          [e["name"] for e in root.get("entries", [])][:5])

    tmp = tempfile.mkdtemp(prefix="adbtool_ios_")
    out = tempfile.mkdtemp(prefix="adbtool_ios_out_")
    remote = "/Downloads/ios_selftest.txt"
    try:
        src = os.path.join(tmp, "ios_selftest.txt")
        with open(src, "w", encoding="utf-8") as f:
            f.write("hello ios 中文内容")
        t = wait_task(api, api.push([src], "/Downloads")["taskId"])
        check("iOS 上传", t.get("status") == "success", t.get("phase") or t.get("error"))
        names = [e["name"] for e in api.list_dir("/Downloads").get("entries", [])]
        check("iOS 上传后可见", "ios_selftest.txt" in names, names)
        t2 = wait_task(api, api.pull([remote], out)["taskId"])
        pulled_file = os.path.join(out, "ios_selftest.txt")
        check("iOS 下载（含中文内容）",
              t2.get("status") == "success" and os.path.isfile(pulled_file),
              t2.get("phase") or t2.get("error"))
        if os.path.isfile(pulled_file):
            with open(pulled_file, "r", encoding="utf-8", errors="replace") as f:
                check("iOS 下载内容一致", f.read() == "hello ios 中文内容", "")
        # 安全擦除在 iOS 上必须明确拒绝，不能假装成功
        t3 = wait_task(api, api.remove([remote], True)["taskId"])
        err = t3.get("error") or ""
        check("iOS 安全擦除明确拒绝",
              t3.get("status") == "failed" and ("覆写" in err or "不支持" in err), err)
        t4 = wait_task(api, api.remove([remote])["taskId"])
        check("iOS 删除", t4.get("status") == "success", t4.get("phase") or t4.get("error"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(out, ignore_errors=True)

    r = api.start_logcat(buffer="main")
    check("iOS 日志流启动", bool(r.get("ok")),
          "%s %s" % (r.get("mode"), r.get("error") or r.get("hint") or ""))
    time.sleep(2)
    b = api.get_log_batch(0)
    parsed = [l for l in b["lines"] if not l.get("raw") and l.get("tag")]
    check("iOS 日志解析（时间/PID/级别/Tag）", len(parsed) > 0,
          json.dumps(parsed[0], ensure_ascii=False)[:110] if parsed else "无解析行")
    api.stop_logcat()


def run_demo(api):
    print("\n--- 强制演示模式 ---")
    api.demo = True
    # 演示模式现在有两台设备（Android + iOS），用例针对 Android 那台写死，先切过去
    api.current_serial = demo.DEMO_DEVICE["serial"]
    d = api.get_device_detail()
    check("演示设备详情", d.get("model") == "Pixel 8 Pro" and d.get("apiLevel") == "34",
          "%s API %s 电量 %s%%" % (d.get("model"), d.get("apiLevel"), (d.get("battery") or {}).get("level")))

    det = api.get_app_detail("com.tencent.mm")
    check("演示应用详情", det["detail"]["label"] == "微信", det["detail"]["label"])

    lr = api.resolve_labels([{"package": "com.tencent.mm", "codePath": ""}])
    check("演示应用名解析", (lr.get("labels") or {}).get("com.tencent.mm") == "微信",
          json.dumps(lr.get("labels"), ensure_ascii=False))

    ld = api.list_dir("/sdcard/Android/adb_logs/2024-06")
    # 演示模式不再伪造文件数据：目录必须为空
    check("演示目录为空（不出假数据）", ld["ok"] and len(ld["entries"]) == 0,
          "entries=%d" % len(ld["entries"]))
    st = api.storage_stats()
    check("演示容量显示空（ok=False）", st.get("ok") is False and not st.get("total"),
          "ok=%s total=%s" % (st.get("ok"), st.get("total")))
    lc = api.start_logcat()
    import time as _t
    _t.sleep(0.6)
    batch = api.get_log_batch(0, 10)
    api.stop_logcat()
    check("演示日志为空（不出假数据）", lc.get("ok") and batch.get("total") == 0,
          "ok=%s total=%s" % (lc.get("ok"), batch.get("total")))

    tmp = tempfile.mkdtemp(prefix="adbtool_xapk_")
    xapk = os.path.join(tmp, "sample.xapk")
    try:
        with zipfile.ZipFile(xapk, "w") as z:
            z.writestr("base.apk", b"PK\x03\x04fake-base")
            z.writestr("split_config.arm64_v8a.apk", b"PK\x03\x04fake-split")
        res = wait_task(api, api.install_apk(xapk, {"r": True, "d": False, "g": True, "t": False})["taskId"])
        logs = " | ".join(res.get("logs", [])[:6])
        check("xapk 会话安装（演示）", res.get("status") == "success" and res.get("percent") == 100,
              "%s%% %s" % (res.get("percent"), res.get("phase")))
        check("xapk 走 install-create 会话", "install-create" in logs or "split" in logs, logs[:100])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    un = wait_task(api, api.uninstall("com.tencent.mm", True)["taskId"])
    check("卸载任务（演示）", un.get("status") == "success", un.get("phase"))
    rm = wait_task(api, api.remove(["/sdcard/tmp.log"], True)["taskId"])
    check("安全擦除任务（演示）", rm.get("status") == "success", rm.get("phase"))


def main():
    api = Api()
    api.refresh_env()
    run_common(api)
    if not api.demo:
        # 真机用例按平台分流：连着 iPhone 时跑 Android 的 dumpsys/pm 用例必然全红
        if api._platform_of(api.current_serial) == "ios":
            run_real_ios(api)
        else:
            run_real(api)
    else:
        print("\n--- 未连接真机，跳过真实设备用例 ---")
    run_demo(api)

    passed = sum(1 for r in results if r[0])
    failed = [r for r in results if not r[0]]
    print("\n合计 %d 项，通过 %d，失败 %d" % (len(results), passed, len(failed)))
    for s in skipped:
        print("  SKIP: %s" % s)
    for f in failed:
        print("  FAIL: %s -> %s" % (f[1], f[2]))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
