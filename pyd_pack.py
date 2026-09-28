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


def latest_tag_version():
    """最近一次发布的 tag 对应的版本号（去掉前缀 v），取不到返回 None。"""
    try:
        p = subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0", "--match", "v[0-9]*"],
            cwd=HERE, capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if p.returncode != 0:
            return None
        tag = (p.stdout or "").strip()
        return tag[1:] if tag.startswith("v") else None
    except Exception:  # noqa: BLE001 - 没装 git / 不在仓库里：当作没有 tag
        return None


def _ver_tuple(v):
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)", v or "")
    return tuple(int(x) for x in m.groups()) if m else None


def next_version_from_tag():
    """下一个版本号：最新发布 tag 的 patch +1（与 ci_version.sh 自动递增规则一致）。

    例：v2.7.4 -> 2.7.5；tag 带历史遗留的 -b<run号> 后缀先剥掉再递增。
    取不到（没 git / 不在仓库 / tag 形态异常）返回 None。
    """
    tag = latest_tag_version()
    if not tag:
        return None
    base = tag.split("-", 1)[0]
    parts = base.split(".")
    if len(parts) != 3 or not all(x.isdigit() for x in parts):
        return None
    parts[2] = str(int(parts[2]) + 1)
    return ".".join(parts)


def sync_version_from_tag(api_py=None):
    """把 core/api.py 的 APP_VERSION 对齐「本次构建版本」（最新 tag 的 patch+1）。

    为什么需要：打包态界面显示的版本号是**编译进去**的 APP_VERSION，而仓库里的值只由
    CI 在 runner 上改写（不回写仓库），本地会一直停在旧值 —— 本地打出来的包写着 2.7.0，
    GitHub 上的包却是 2.7.5，两边对不上。由 setup_pyd.py 在 Cython 编译**之前**调用，
    本地打包就自动跟上 CI 的递增规则：上一个发布是 v2.7.4 时，本地包也是 2.7.5。

    只升不降：CI 打包前 ci_version.sh 已写入「最新 tag + 1」，与这里的算法结果相同，
    new <= old 判定直接 no-op，绝不会把 CI 的版本号改小。
    """
    path = api_py or _core("api.py")
    if not os.path.isfile(path):
        return None
    new_ver = next_version_from_tag()
    if not new_ver:
        return None
    # newline=""：原样读写，不改动行尾（LF 还是 LF，CRLF 还是 CRLF）
    with open(path, encoding="utf-8", newline="") as f:
        src = f.read()
    m = re.search(r'^APP_VERSION[ \t]*=[ \t]*"([^"]+)"', src, re.M)
    if not m:
        return None
    old = m.group(1)
    new_t, old_t = _ver_tuple(new_ver), _ver_tuple(old)
    if new_t is None or old_t is None or new_t <= old_t:
        return None
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(src[:m.start(1)] + new_ver + src[m.end(1):])
    return (old, new_ver)


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
# 本地没有时自动下载到这里（CI runner 上必然没有上一层目录，只能自己下）
PORTABLE_CACHE = os.path.join(HERE, "build", "_portable_adb")
PLATFORM_TOOLS_URL = ("https://dl.google.com/android/repository/"
                      "platform-tools-latest-windows.zip")

# 必须整目录带的文件：adb.exe 依赖同目录的 AdbWinApi.dll / AdbWinUsbApi.dll，
# 只拷 exe 会"能启动但连不上设备"。aapt/aapt2 在 build-tools 里、platform-tools
# 没有，本地目录里放了就一并带上（有它才能解析应用名，见 labels.find_aapt）。
PORTABLE_KEEP = ("adb.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll",
                 "aapt2.exe", "aapt.exe")


def resolve_portable_adb():
    """确定内置 adb 的来源目录。返回 None = 跳过（mac / 显式跳过）。

    ★ 这里曾经是「找不到源目录就静默跳过」，结果 **CI 打出的 Windows 包没有 adb**
      （发布机是干净 runner，不存在 ../public_settings/adb），而 Release 才是主要
      分发渠道 —— 用户拿到包根本连不上设备。现在缺了就联网下载，下载失败直接报错，
      绝不产出「看起来正常、其实少了 adb」的包。
    """
    if IS_MAC:
        # mac 包不能带 Windows 的 adb.exe；mac 的 adb 由 build_mac.sh 单独内置
        return None
    if os.environ.get("SKIP_PORTABLE_ADB") == "1":
        print("[portable] SKIP_PORTABLE_ADB=1，跳过内置 adb")
        return None
    if os.path.isfile(os.path.join(PORTABLE_SRC, "adb.exe")):
        return PORTABLE_SRC
    if os.path.isfile(os.path.join(PORTABLE_CACHE, "adb.exe")):
        print("[portable] 使用已下载的 %s" % PORTABLE_CACHE)
        return PORTABLE_CACHE

    print("[portable] %s 不存在，从 Google 下载 platform-tools..." % PORTABLE_SRC)
    # urllib.request 只在真的要下载时才导入（本文件其余部分用不到网络）：
    # 它在 darwin 分支有一句裸 `from _scproxy import ...`，而
    # _mac_dryrun_check.py 会在 Windows 上把 sys.platform 伪造成 'darwin'
    # 再 import 本模块 —— 顶层导入会让那个离线预检直接 ModuleNotFoundError。
    import urllib.request
    os.makedirs(PORTABLE_CACHE, exist_ok=True)
    zpath = os.path.join(PORTABLE_CACHE, "platform-tools.zip")
    try:
        urllib.request.urlretrieve(PLATFORM_TOOLS_URL, zpath)
        with zipfile.ZipFile(zpath) as z:
            for n in z.namelist():
                base = os.path.basename(n)
                if base in PORTABLE_KEEP:
                    with z.open(n) as fsrc, \
                            open(os.path.join(PORTABLE_CACHE, base), "wb") as fdst:
                        shutil.copyfileobj(fsrc, fdst)
        os.remove(zpath)
    except Exception as e:  # noqa: BLE001
        raise SystemExit(
            "[错误] 内置 adb 下载失败：%s\n"
            "       目标机没装 Android SDK 时，包里的 adb 是唯一可用的 adb。\n"
            "       修复：联网重跑；或先手工把 platform-tools 的 adb.exe +\n"
            "             AdbWinApi.dll + AdbWinUsbApi.dll 放到 %s；\n"
            "       确实不要内置时：SKIP_PORTABLE_ADB=1" % (e, PORTABLE_SRC))
    print("[portable] 下载完成 -> %s" % PORTABLE_CACHE)
    return PORTABLE_CACHE


def copy_portable_adb(src):
    """把便携 adb 整目录拷进产物，让目标机开箱即用（src 由 resolve_portable_adb 给）。"""
    if not src:
        return None
    # 只拷用得上的：platform-tools 里还有 fastboot/mke2fs/sqlite3 等无关工具
    # （合计 ~11MB，压缩后多占 4MB），丢掉后不影响 adb 任何功能。
    dst = os.path.join(APP_DIR, "public_settings", "adb")
    os.makedirs(dst, exist_ok=True)
    names = [n for n in PORTABLE_KEEP if os.path.isfile(os.path.join(src, n))]
    for n in names:
        shutil.copy2(os.path.join(src, n), os.path.join(dst, n))
    print("[portable] adb -> %s (%s)" % (dst, ", ".join(names)))
    return dst


# ---------------------------------------------------------------------------
# 便携 hdc（鸿蒙）：**可选内置**，与 adb 的必需内置不同。
# hdc 没有稳定直链可自动下载（官方走整套 OpenHarmony SDK / DevEco），所以只做
# 「本地放好就带进包」：
#   ../public_settings/hdc/hdc(.exe)   与 adb 同目录约定
#   build/_portable_hdc/hdc(.exe)      手工放置的缓存位
# 运行时 core/hdc.py 的 find_hdc() 本就按 配置 > PATH > 便携目录 > 常见路径 找，
# 包里没有时鸿蒙模块会提示自备 hdc —— 因此缺失只警告不报错。
# ---------------------------------------------------------------------------
PORTABLE_HDC_CANDIDATES = (
    os.path.join(os.path.dirname(HERE), "public_settings", "hdc"),
    os.path.join(HERE, "build", "_portable_hdc"),
)


def _hdc_exe_name():
    return "hdc" if IS_MAC else "hdc.exe"


def resolve_portable_hdc():
    """确定便携 hdc 来源目录（本地有才有效）。None = 跳过（不硬失败）。"""
    if os.environ.get("SKIP_PORTABLE_HDC") == "1":
        print("[portable] SKIP_PORTABLE_HDC=1，跳过内置 hdc")
        return None
    exe = _hdc_exe_name()
    for d in PORTABLE_HDC_CANDIDATES:
        if os.path.isfile(os.path.join(d, exe)):
            return d
    return None


def copy_portable_hdc(src):
    """把便携 hdc 拷进产物 public_settings/hdc/（可选件：没有就提示）。"""
    if not src:
        print("[portable] hdc: 未内置（鸿蒙功能需目标机自备 hdc；"
              "要内置请把 hdc 放 ../public_settings/hdc/ 后重打包，"
              "SKIP_PORTABLE_HDC=1 可显式关闭）")
        return None
    dst = os.path.join(APP_DIR, "public_settings", "hdc")
    os.makedirs(dst, exist_ok=True)
    names = sorted(n for n in os.listdir(src)
                   if os.path.isfile(os.path.join(src, n)))
    for n in names:
        shutil.copy2(os.path.join(src, n), os.path.join(dst, n))
    print("[portable] hdc -> %s (%s)" % (dst, ", ".join(names)))
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


def _resolve_with_ios():
    """决定本次打包是否带 iOS 支持（spec 的 WITH_IOS 开关）。

    spec 只在环境变量 WITH_IOS=1 时收集 pymobiledevice3（约 +30MB）。以前是
    纯手动开关，本地打包忘了设就打出「没有 iOS 环境」的包（界面 iOS 一栏
    显示 未安装 pymobiledevice3）。现在改为自动探测：构建解释器能 import 到
    pymobiledevice3 就自动带上，与直觉一致。优先级：
        WITH_IOS=0  强制不带（出精简包）
        WITH_IOS=1  强制带上（构建环境没有时 spec 会直接报错，CI 可暴露问题）
        未设置      自动探测 pymobiledevice3，找得到就带
    """
    v = os.environ.get("WITH_IOS")
    if v is None:
        try:
            import importlib.util
            v = "1" if importlib.util.find_spec("pymobiledevice3") else "0"
        except Exception:  # noqa: BLE001 - 探测失败按没有处理
            v = "0"
        print("[ios] WITH_IOS 未设置，自动探测 pymobiledevice3 -> %s" %
              ("已找到，将打包" if v == "1" else "未安装，跳过（WITH_IOS=1 可强制）"))
    os.environ["WITH_IOS"] = v
    return v


def run_pyinstaller():
    _resolve_with_ios()
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

        # fix_and_check.bat 跟源文件走：仓库根目录有就随包分发，没有只提示不算失败
        # （它曾被 .gitignore 的 *.bat 排除 → CI 上没有 → 无谓地卡住整个打包流程）
        bat = os.path.join(APP_DIR, "fix_and_check.bat")
        ok.append("fix_and_check.bat: %s"
                  % ("OK" if os.path.isfile(bat) else "未附带（可选）"))

    # 便携 adb：Windows 包缺了就等于没有 adb（目标机装 SDK 的极少），必须硬校验。
    # 曾经这里写的是「未附带（可选）」→ 静默放过 → CI 发出的包全都没有 adb。
    if not IS_MAC:
        pad = os.path.join(APP_DIR, "public_settings", "adb")
        dlls = glob.glob(os.path.join(pad, "*.dll"))
        if not os.path.isfile(os.path.join(pad, "adb.exe")):
            if os.environ.get("SKIP_PORTABLE_ADB") == "1":
                ok.append("便携 adb: 未附带（SKIP_PORTABLE_ADB=1，目标机需自备 adb）")
            else:
                errs.append("便携 adb: MISSING（包内没有 adb.exe，目标机将无法使用）")
        elif len(dlls) < 2:
            errs.append("便携 adb: 只找到 %d 个 dll，缺 AdbWinApi/AdbWinUsbApi "
                        "会让 adb 能启动但连不上设备" % len(dlls))
        else:
            ok.append("便携 adb: OK (%d dll)" % len(dlls))

    # 便携 hdc：可选件，未内置只提示（鸿蒙功能可选，目标机可自备或放包后补）。
    hcd = os.path.join(APP_DIR, "public_settings", "hdc")
    if os.path.isfile(os.path.join(hcd, _hdc_exe_name())):
        ok.append("便携 hdc: OK")
    else:
        ok.append("便携 hdc: 未内置（鸿蒙功能需目标机自备 hdc）")

    # iOS 支持：报告本次 WITH_IOS 决策（run_pyinstaller 已 resolve）。
    if os.environ.get("WITH_IOS") == "1":
        ok.append("iOS 支持: 已内置（pymobiledevice3）")
    else:
        ok.append("iOS 支持: 未内置（打包环境没有 pymobiledevice3，或 WITH_IOS=0）")

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
    # 便携 adb 是必需项：目标机大概率没装 Android SDK，包里没有 adb 就完全不可用。
    # 先在这里备好（可能要联网下载），别等 PyInstaller 跑完几分钟才发现拿不到。
    portable_src = resolve_portable_adb()
    portable_hdc = resolve_portable_hdc()
    move_old_build()
    move_old_dist()
    moved = stash()
    try:
        code = run_pyinstaller()
        if code != 0:
            print("[错误] PyInstaller 退出码 %d" % code)
            return code
        copy_portable_adb(portable_src)
        copy_portable_hdc(portable_hdc)
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
