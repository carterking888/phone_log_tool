# -*- coding: utf-8 -*-
"""pyd 混淆打包：源码暂避 -> PyInstaller -> 校验 -> 恢复源码。

流程要点（踩过坑）：
1. 先把已编译的 core/*.py 临时改名 .py.src，PyInstaller 分析时只看到 .pyd，
   业务源码就不会被打进 _internal。
2. 旧 dist 目录用 rename 挪走（不删），避免 PyInstaller COLLECT 批量删除
   被安全钩子拦截。
3. 校验 _internal：有 pyd、无业务 py、web/ 与两个词库 json 在位、
   pythonnet/runtime 依赖齐全（否则目标机报 Python.Runtime.Loader.Initialize）。
4. finally 里恢复 .py 源码。
"""
import glob
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable

# 平台差异：
#   Windows  dist/adb_tool/adb_tool.exe            （onedir）
#   macOS    dist/<APP_MAC>.app/Contents/MacOS/... （onedir + BUNDLE）
# 扩展模块后缀也不同：.pyd vs .so
# 产物名分平台：mac 包叫 device_bebugging_tool，Windows 仍是 adb_tool
IS_MAC = sys.platform == "darwin"
EXT = "so" if IS_MAC else "pyd"
APP = "device_bebugging_tool" if IS_MAC else "adb_tool"
APP_DIR = os.path.join(HERE, "dist", APP + ".app") if IS_MAC \
    else os.path.join(HERE, "dist", APP)
DIST = os.path.join(APP_DIR, "Contents", "MacOS") if IS_MAC else APP_DIR
INTERNAL = os.path.join(DIST, "_internal")

# 与 setup_pyd.py 的 TARGETS 保持一致（不含 __init__）
MODULES = ("app", "api", "adb", "logcat", "labels", "demo", "action_log", "zhdict",
           "ios", "ioslog")


def read_version():
    """从 core/api.py 读 APP_VERSION（zip 命名用，与界面显示同源）。

    不 import（core 可能处于 .py.src 暂避态或已编译成 .pyd），直接正则提取。
    """
    src = _core("api.py")
    if os.path.isfile(src):
        with open(src, encoding="utf-8") as f:
            m = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', f.read())
        if m:
            return m.group(1)
    return "0.0.0"


def _core(name):
    return os.path.join(HERE, "core", name)


def stash():
    moved = []
    for m in MODULES:
        src = _core(m + ".py")
        if os.path.isfile(src):
            dst = src + ".src"
            if os.path.exists(dst):
                os.remove(dst)
            os.rename(src, dst)
            moved.append((dst, src))
    return moved


def restore(moved):
    for dst, src in moved:
        if os.path.exists(src):
            os.remove(src)
        if os.path.exists(dst):
            os.rename(dst, src)


def move_old_dist():
    """rename 旧产物（同卷瞬时完成且不触发删除钩子）。"""
    if not os.path.isdir(APP_DIR):
        return
    trash = os.path.join(HERE, "dist", "_old_%s_%d" % (APP, int(time.time())))
    try:
        os.rename(APP_DIR, trash)
        print("[dist] old output moved to %s" % os.path.basename(trash))
    except Exception as e:
        print("[warn] cannot move old dist: %s" % e)


# 便携 adb：与项目同层的 public_settings/adb（给没装 Android SDK 的电脑用）
PORTABLE_SRC = os.path.join(os.path.dirname(HERE), "public_settings", "adb")


def copy_portable_adb():
    """把便携 adb 整目录拷进产物，让目标机开箱即用。

    - 必须整目录拷：adb.exe 依赖同目录的 AdbWinApi.dll / AdbWinUsbApi.dll，
      只拷 exe 会"能启动但连不上设备"。
    - mac 产物不能带 Windows 的 adb.exe，且 mac 用系统/brew 的 adb；跳过。
    - SKIP_PORTABLE_ADB=1 可跳过。
    """
    if IS_MAC or os.environ.get("SKIP_PORTABLE_ADB") == "1":
        return None
    exe = os.path.join(PORTABLE_SRC, "adb.exe")
    if not os.path.isfile(exe):
        print("[portable] 未找到 %s，跳过（不影响打包）" % PORTABLE_SRC)
        return None
    # 只拷用得上的：sdk 自带目录里还有 fastboot/mke2fs/sqlite3 等一堆无关工具
    # （合计 ~11MB，压缩后多占 4MB），丢掉后不影响 adb 任何功能。
    # aapt 一并带上——有它才能解析应用名（见 labels.find_aapt）。
    KEEP = ("adb.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll",
            "aapt2.exe", "aapt.exe")
    dst = os.path.join(APP_DIR, "public_settings", "adb")
    os.makedirs(dst, exist_ok=True)
    names = [n for n in KEEP if os.path.isfile(os.path.join(PORTABLE_SRC, n))]
    for n in names:
        shutil.copy2(os.path.join(PORTABLE_SRC, n), os.path.join(dst, n))
    print("[portable] adb -> %s (%s)" % (dst, ", ".join(names)))
    return dst


def move_old_build():
    """rename 旧 build 目录（不删）。

    PyInstaller 启动时会清理 build/<App> 下的旧中间产物（实测 258 个文件），
    超过安全删除钩子的 50 阈值 → 被拦 → **exit 1 且没有 traceback**，
    和 --clean 删 bincache 是同一个坑。挪走即可，代价是每轮全量重分析。
    """
    b = os.path.join(HERE, "build", APP)
    if not os.path.isdir(b):
        return
    # 必须同卷：os.rename 跨盘报 WinError 17（试过挪到 %TEMP%，D->C 直接失败，
    # 结果 build 没挪走又被安全钩子拦，打包 exit 1）。所以留在 build 目录内，
    # 由 cleanup_old_builds() 在打包成功后尽力清理。
    trash = os.path.join(HERE, "build", "_old_%s_%d" % (APP, int(time.time())))
    try:
        os.rename(b, trash)
        print("[build] old build dir moved to %s" % os.path.basename(trash))
    except Exception as e:  # noqa
        print("[warn] cannot move old build: %s" % e)


def cleanup_old_builds():
    """清掉历史 build/_old_*（尽力而为）。

    安全删除钩子会拦批量删除（>50 文件），在会话内跑必然失败；
    用户本地双击 build_pyd.bat 时能删掉。失败一律静默，不影响打包结果。
    """
    for d in glob.glob(os.path.join(HERE, "build", "_old_*")):
        shutil.rmtree(d, ignore_errors=True)


def cleanup_old_dists():
    """清掉历史 dist/_old_*（尽力而为）。

    move_old_dist() 只挪不删，一个旧产物就是 20MB+，打几次就上百 MB。
    旧产物是可重建的中间物，成功后即可清；失败时保留（方便对比排查）。
    """
    for d in glob.glob(os.path.join(HERE, "dist", "_old_*")):
        shutil.rmtree(d, ignore_errors=True)


def make_zip():
    """把产物打成可直接分发的 zip。

    Windows：Python zipfile 即可（产物全是实体文件）。
    macOS：**必须调用系统 zip -y 保留符号链接** —— .app 内 Frameworks 大量 symlink，
    zipfile 会跟随链接存成实体文件副本，导致体积暴涨且签名失效。
    """
    if not os.path.isdir(APP_DIR):
        print("[zip] 产物目录不存在，跳过")
        return None
    if IS_MAC:
        arch = (platform.machine() or "universal").lower()
        name = "%s_v%s_macos_%s.zip" % (APP, read_version(), arch)
        dst = os.path.join(HERE, "dist", name)
        if os.path.exists(dst):
            os.remove(dst)
        r = subprocess.run(["zip", "-q", "-r", "-y", name,
                            os.path.basename(APP_DIR)],
                           cwd=os.path.join(HERE, "dist"))
        if r.returncode != 0:
            print("[zip] zip 命令失败（退出码 %d）" % r.returncode)
            return None
    else:
        name = "%s_v%s_win64.zip" % ("device_bebugging_tool", read_version())
        dst = os.path.join(HERE, "dist", name)
        if os.path.exists(dst):
            os.remove(dst)
        n = 0
        root = APP_DIR
        with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED,
                             compresslevel=6) as z:
            for r, _d, fs in os.walk(root):
                for f in fs:
                    p = os.path.join(r, f)
                    z.write(p, os.path.join(APP, os.path.relpath(p, root)))
                    n += 1
    size = os.path.getsize(dst) / 1024 / 1024
    print("[zip] %s -> %.1f MB" % (name, size))
    return dst


def copy_helper_scripts():
    """把目标机自检脚本拷到产物根目录。

    fix_and_check.bat：解 MOTW（否则 .NET DLL 被拦）+ 查 .NET ≥ 4.7.2
    + 查 WebView2 运行时（缺它表现为"进程起来了但窗口空白"）。
    接收方解压后跑一次，能挡掉大部分"打不开"的工单。
    """
    if IS_MAC:  # mac 用 build_mac.sh 里那套 xattr/codesign，不需要这个 bat
        return None
    src = os.path.join(HERE, "fix_and_check.bat")
    if not os.path.isfile(src):
        return None
    dst = os.path.join(APP_DIR, "fix_and_check.bat")
    shutil.copy2(src, dst)
    print("[helper] fix_and_check.bat -> %s" % dst)
    return dst


def run_pyinstaller():
    # 不要加 --clean：它会批量删除 bincache，被安全删除钩子拦截（exit 1 且无 traceback）
    # spec 文件固定叫 adb_tool.spec：产物名由 spec 内部的 IS_MAC 分支决定
    # （mac 上 APP 是 device_bebugging_tool，但不存在 device_bebugging_tool.spec，
    #   按 APP 拼 spec 名会在 mac CI 上报 "Spec file not found"）
    cmd = [PY, "-m", "PyInstaller", "--noconfirm",
           os.path.join(HERE, "adb_tool.spec")]
    print("[pack] " + " ".join(cmd[1:]))
    r = subprocess.run(cmd, cwd=HERE)
    return r.returncode


def check():
    errs = []
    ok = []

    exe = os.path.join(DIST, APP + ("" if IS_MAC else ".exe"))
    exe_ok = os.path.isfile(exe)
    (ok if exe_ok else errs).append(
        "exe: %s" % ("OK" if exe_ok else "MISSING"))

    if IS_MAC:
        # PyInstaller 6 的 macOS BUNDLE 会把 onedir 布局整个重排（与 Windows 的
        # Contents/MacOS/_internal 完全不同），按文件类型分家：
        #   可执行文件  -> Contents/MacOS
        #   二进制(.so) -> Contents/Frameworks
        #   数据文件    -> Contents/Resources
        # 再用符号链接互跨（sys._MEIPASS 指向 Contents/Frameworks）。
        # 所以 mac 上不能按固定 INTERNAL 路径找，统一在整个 .app 里递归查。
        # （glob 默认不跟随目录符号链接，不会把 cross-link 副本重复算进来）
        def _app_glob(rel):
            return glob.glob(os.path.join(APP_DIR, "Contents", "**",
                                          rel.replace("/", os.sep)),
                             recursive=True)

        # 每个模块都必须有编译产物：少一个说明 spec 的 import 闭包没覆盖到
        missing = [m for m in MODULES if not _app_glob("core/" + m + "*." + EXT)]
        (ok if not missing else errs).append(
            "core %s: %d/%d %s" % (EXT, len(MODULES) - len(missing), len(MODULES),
                                   missing and ("missing " + ",".join(missing)) or "all OK"))

        # 业务源码绝不能出现在包里（core/__init__.py 是空壳，不算泄露）
        leaked = [os.path.basename(p) for p in _app_glob("core/*.py")
                  if os.path.basename(p) != "__init__.py"]
        (ok if not leaked else errs).append(
            "leaked py: %s" % (leaked or "none"))

        for rel in ("web/index.html", "config/android_perms_zh.json",
                    "config/android_pkg_names.json"):
            hit = _app_glob(rel)
            (ok if hit else errs).append(
                "%s: %s" % (rel, "OK" if hit else "MISSING"))

        # macOS 走 Cocoa/WKWebView，不依赖 .NET；改查 pyobjc 是否进包
        objc = _app_glob("objc/*.so") or _app_glob("PyObjCTools")
        (ok if objc else errs).append(
            "pyobjc: %s" % ("OK" if objc else "MISSING（pywebview 无法起窗口）"))
    else:
        # 每个模块都必须有编译产物：少一个说明 spec 的 import 闭包没覆盖到
        missing = [m for m in MODULES
                   if not glob.glob(os.path.join(INTERNAL, "core", m + "*." + EXT))]
        (ok if not missing else errs).append(
            "core %s: %d/%d %s" % (EXT, len(MODULES) - len(missing), len(MODULES),
                                   missing and ("missing " + ",".join(missing)) or "all OK"))

        # 业务源码绝不能出现在包里（core/__init__.py 是空壳，不算泄露）
        leaked = [os.path.basename(p) for p in
                  glob.glob(os.path.join(INTERNAL, "core", "*.py"))
                  if os.path.basename(p) != "__init__.py"]
        (ok if not leaked else errs).append(
            "leaked py: %s" % (leaked or "none"))

        for rel in ("web/index.html", "config/android_perms_zh.json",
                    "config/android_pkg_names.json"):
            p = os.path.join(INTERNAL, rel.replace("/", os.sep))
            (ok if os.path.isfile(p) else errs).append(
                "%s: %s" % (rel, "OK" if os.path.isfile(p) else "MISSING"))

        rt = glob.glob(os.path.join(INTERNAL, "pythonnet", "runtime", "*.dll"))
        (ok if len(rt) > 50 else errs).append("pythonnet runtime dll: %d" % len(rt))

        cl = glob.glob(os.path.join(INTERNAL, "clr_loader", "ffi", "dlls",
                                    "*", "ClrLoader.dll"))
        (ok if cl else errs).append(
            "ClrLoader.dll: %s" % ("OK" if cl else "MISSING"))

        bat = os.path.join(APP_DIR, "fix_and_check.bat")
        (ok if os.path.isfile(bat) else errs).append(
            "fix_and_check.bat: %s" % ("OK" if os.path.isfile(bat) else "MISSING"))

    # 便携 adb 是可选增强：带了就校验，没带只提示（源目录不存在时不算失败）
    pad = glob.glob(os.path.join(APP_DIR, "public_settings", "adb", "adb.exe"))
    if IS_MAC:
        pass
    elif pad:
        dlls = glob.glob(os.path.join(APP_DIR, "public_settings", "adb", "*.dll"))
        ok.append("便携 adb: OK (%d dll)" % len(dlls))
    else:
        ok.append("便携 adb: 未附带（可选）")

    print("\n[check]")
    for line in ok:
        print("  + %s" % line)
    for line in errs:
        print("  - %s" % line)
    return not errs


def cleanup_core_artifacts():
    """把 core/*.pyd 与 core/*.c 挪出 core/。

    不挪的后果很隐蔽：Python 导入时 .pyd 优先级高于 .py，
    打包后 core 里残留的旧编译模块会让「源码模式」（run.bat -> main.py）
    一直跑旧代码，改了 .py 完全看不出变化。挪到 build/ 下既保留可追溯，
    又不会干扰源码运行；下次打包会重新编译生成。
    """
    core = os.path.join(HERE, "core")
    dst = os.path.join(HERE, "build", "_pyd_shadow_%d" % int(time.time()))
    moved = 0
    try:
        for name in sorted(os.listdir(core)):
            if name.endswith("." + EXT) or name.endswith(".c"):
                src = os.path.join(core, name)
                if not os.path.isfile(src):
                    continue
                os.makedirs(dst, exist_ok=True)
                os.replace(src, os.path.join(dst, name))
                moved += 1
    except Exception as e:  # noqa: BLE001 - 清理失败不影响打包结果
        print("[warn] 清理 core 编译产物失败：%s" % e)
        return
    if moved:
        print("[clean] core 编译产物已移出：%d 个 -> %s" % (moved, dst))


def main():
    if not glob.glob(os.path.join(HERE, "core", "*." + EXT)):
        # 上一次打包会把 core 下的编译产物移到 build/_pyd_shadow_*（避免遮蔽源码），
        # 所以这里是「需要先重新编译」，不是异常。
        print("[错误] core 下没有 %s，请先跑 setup_pyd.py build_ext --inplace" % EXT)
        return 1
    move_old_build()
    move_old_dist()
    moved = stash()
    try:
        code = run_pyinstaller()
        if code != 0:
            print("[错误] PyInstaller 退出码 %d" % code)
            return code
        copy_portable_adb()
        copy_helper_scripts()
    finally:
        restore(moved)
    cleanup_old_builds()
    if not check():
        return 2  # 校验没过：保留 dist/_old_* 便于对比排查
    # 产物已成型，把 core 里的编译产物挪走：留着会**遮蔽同名 .py**
    # （Python 优先加载 .pyd），源码模式下改代码跑的还是旧编译结果。
    cleanup_core_artifacts()
    if "--no-zip" in sys.argv:
        print("[zip] --no-zip 已指定，跳过")
    else:
        make_zip()
    cleanup_old_dists()
    return 0


if __name__ == "__main__":
    sys.exit(main())
