# -*- coding: utf-8 -*-
"""macOS 打包路径的离线预检（在 Windows 上模拟 darwin，抓低级故障）。

为什么需要它：PyInstaller 不能交叉编译，mac 分支的代码只有在真 mac 上才会被执行。
低级错误（spec 里拼错变量名、路径写错、分支漏 import）会等到对方机器上才炸，
排查成本极高。这里在 Windows 上把 darwin 分支的**逻辑**先跑一遍：

  1. exec adb_tool.spec（stub 掉 Analysis/EXE/PYZ/COLLECT/BUNDLE）
     -> 验证 mac 分支没有 NameError，且 hiddenimports/runtime_hooks/BUNDLE 参数正确
  2. import pyd_pack（sys.platform='darwin'）
     -> 验证产物路径切到 dist/adb_tool.app/Contents/MacOS、扩展名是 .so
  3. 单抽 setup_pyd.setup_clang_env 执行
     -> 验证 MACOSX_DEPLOYMENT_TARGET / ARCHFLAGS 设对

用法：python _mac_dryrun_check.py
"""
import ast
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
results = []


def check(name, ok, extra=""):
    results.append((bool(ok), name, str(extra)))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name,
                        ("   -> " + str(extra)) if extra else ""))


# ---------------------------------------------------------------- 1. spec
def check_spec():
    spec = os.path.join(HERE, "adb_tool.spec")
    calls = {}

    def stub(kind):
        def _f(*args, **kwargs):
            calls.setdefault(kind, []).append((args, kwargs))
            # 返回值要够真：spec 里有 PYZ(a.pure) / EXE(pyz, a.scripts, ...)，
            # 光返回字符串会在第二次使用时 AttributeError
            return types.SimpleNamespace(kind=kind, pure="pure",
                                         scripts="scripts", binaries="bin",
                                         datas="data")
        return _f

    g = {
        "__file__": spec,
        "SPECPATH": HERE,
        "Analysis": stub("Analysis"),
        "EXE": stub("EXE"),
        "PYZ": stub("PYZ"),
        "COLLECT": stub("COLLECT"),
        "BUNDLE": stub("BUNDLE"),
    }
    try:
        with open(spec, encoding="utf-8") as f:
            code = compile(f.read(), spec, "exec")
        exec(code, g)
    except Exception as e:
        check("spec 在 darwin 分支可执行", False, "%s: %s" % (type(e).__name__, e))
        return

    check("spec 在 darwin 分支可执行", True, "无 NameError / 语法错误")

    a = calls.get("Analysis", [[(), {}]])[0][1]
    hid = a.get("hiddenimports", [])
    check("hiddenimports 含 cocoa 平台", "webview.platforms.cocoa" in hid,
          "有" if "webview.platforms.cocoa" in hid else "缺")
    check("hiddenimports 不含 pythonnet（mac 不需要）",
          not any(m.startswith(("pythonnet", "clr", "cffi", "_cffi")) for m in hid),
          [m for m in hid if m.startswith(("pythonnet", "clr", "cffi", "_cffi"))] or "干净")
    check("runtime_hooks 为空（不挂 dotnet 钩子）",
          a.get("runtime_hooks") == [], a.get("runtime_hooks"))
    check("import 闭包仍完整（含 webview/http.server）",
          "webview" in hid and "http.server" in hid,
          "闭包 %d 项" % len(hid))

    b = calls.get("BUNDLE")
    check("调用了 BUNDLE（否则只有裸可执行文件）", bool(b), "有" if b else "缺")
    if b:
        kw = b[0][1]
        check("BUNDLE name 是 .app", kw.get("name") == "adb_tool.app", kw.get("name"))
        check("BUNDLE 有 bundle_identifier", bool(kw.get("bundle_identifier")),
              kw.get("bundle_identifier"))


# ------------------------------------------------------------ 2. pyd_pack
def check_pyd_pack():
    sys.path.insert(0, HERE)
    for m in ("pyd_pack",):
        sys.modules.pop(m, None)
    try:
        import pyd_pack as pp
    except Exception as e:
        check("pyd_pack 可导入", False, "%s: %s" % (type(e).__name__, e))
        return
    check("扩展名后缀是 .so", pp.EXT == "so", pp.EXT)
    ok_dir = pp.APP_DIR.endswith(os.path.join("dist", "adb_tool.app"))
    check("产物目录是 dist/adb_tool.app", ok_dir, pp.APP_DIR)
    ok_dist = pp.DIST.endswith(os.path.join("adb_tool.app", "Contents", "MacOS"))
    check("DIST 指向 Contents/MacOS", ok_dist, pp.DIST)
    ok_int = pp.INTERNAL.endswith(os.path.join("Contents", "MacOS", "_internal"))
    check("_internal 在 Contents/MacOS 下", ok_int, pp.INTERNAL)


# ------------------------------------------------------- 3. setup_clang_env
def check_clang_env():
    src = os.path.join(HERE, "setup_pyd.py")
    try:
        with open(src, encoding="utf-8") as f:
            tree = ast.parse(f.read())
    except Exception as e:
        check("setup_pyd.py 可解析", False, e)
        return
    fn = [n for n in tree.body
          if isinstance(n, ast.FunctionDef) and n.name == "setup_clang_env"]
    if not fn:
        check("找到 setup_clang_env", False)
        return
    ns = {"os": os, "sys": sys}
    exec(compile(ast.Module(body=fn, type_ignores=[]), "<clang>", "exec"), ns)

    for key in ("MACOSX_DEPLOYMENT_TARGET", "ARCHFLAGS"):
        os.environ.pop(key, None)

    # arm64
    os.uname = lambda: types.SimpleNamespace(machine="arm64")
    ns["setup_clang_env"]()
    check("arm64 -> deployment target 11.0",
          os.environ.get("MACOSX_DEPLOYMENT_TARGET") == "11.0",
          os.environ.get("MACOSX_DEPLOYMENT_TARGET"))
    check("arm64 -> ARCHFLAGS 限定单架构",
          os.environ.get("ARCHFLAGS") == "-arch arm64", os.environ.get("ARCHFLAGS"))

    # x86_64（先清掉，验证 setdefault 不会复用上一次的值）
    os.environ.pop("MACOSX_DEPLOYMENT_TARGET", None)
    os.environ.pop("ARCHFLAGS", None)
    os.uname = lambda: types.SimpleNamespace(machine="x86_64")
    ns["setup_clang_env"]()
    check("x86_64 -> deployment target 10.13",
          os.environ.get("MACOSX_DEPLOYMENT_TARGET") == "10.13",
          os.environ.get("MACOSX_DEPLOYMENT_TARGET"))
    check("x86_64 -> ARCHFLAGS 限定单架构",
          os.environ.get("ARCHFLAGS") == "-arch x86_64", os.environ.get("ARCHFLAGS"))


# --------------------------------------------------- 4. check() × BUNDLE 布局
def check_check_layout():
    """用 PyInstaller 6 macOS BUNDLE 的真实布局跑一遍 pyd_pack.check()。

    布局依据 PyInstaller/building/osx.py：可执行文件进 Contents/MacOS、
    二进制(.so)进 Contents/Frameworks、数据文件进 Contents/Resources
    （cross-link 符号链接省略——glob 本就不跟随目录链接）。
    曾经的事故：check 按 Windows 的 Contents/MacOS/_internal 找，
    mac 上全部误报 MISSING 导致 CI exit 2。
    """
    sys.path.insert(0, HERE)
    for m in ("pyd_pack",):
        sys.modules.pop(m, None)
    try:
        import pyd_pack as pp
    except Exception as e:
        check("pyd_pack 可导入", False, "%s: %s" % (type(e).__name__, e))
        return

    import shutil
    import tempfile
    app = os.path.join(tempfile.mkdtemp(prefix="_fake_bundle_"), "adb_tool.app")
    c = os.path.join(app, "Contents")
    try:
        os.makedirs(os.path.join(c, "MacOS"))
        os.makedirs(os.path.join(c, "Frameworks", "core"))
        os.makedirs(os.path.join(c, "Frameworks", "objc"))
        os.makedirs(os.path.join(c, "Resources", "web"))
        os.makedirs(os.path.join(c, "Resources", "config"))

        with open(os.path.join(c, "MacOS", "adb_tool"), "wb") as f:
            f.write(b"\xcf\xfa\xed\xfe")  # Mach-O magic
        for m in pp.MODULES:
            fn = os.path.join(c, "Frameworks", "core",
                              "%s.cpython-313-darwin.so" % m)
            with open(fn, "wb") as f:
                f.write(b"\x00")
        # 空壳 __init__.py 不算泄露
        with open(os.path.join(c, "Frameworks", "core", "__init__.py"), "wb") as f:
            f.write(b"")
        with open(os.path.join(c, "Frameworks", "objc",
                               "_objc.cpython-313-darwin.so"), "wb") as f:
            f.write(b"\x00")
        with open(os.path.join(c, "Resources", "web", "index.html"), "wb") as f:
            f.write(b"<html></html>")
        for j in ("android_perms_zh.json", "android_pkg_names.json"):
            with open(os.path.join(c, "Resources", "config", j), "wb") as f:
                f.write(b"{}")

        old_app = pp.APP_DIR
        old_dist = pp.DIST
        pp.APP_DIR = app
        pp.DIST = os.path.join(app, "Contents", "MacOS")
        try:
            ret = pp.check()
        finally:
            pp.APP_DIR = old_app
            pp.DIST = old_dist
        check("check() 在模拟 BUNDLE 布局下通过", ret is True,
              "返回 %r" % ret)
    finally:
        shutil.rmtree(os.path.dirname(app), ignore_errors=True)


def main():
    print("=== macOS 打包路径离线预检（模拟 sys.platform='darwin'）===\n")
    old = sys.platform
    had_uname = hasattr(os, "uname")
    old_uname = getattr(os, "uname", None)
    orig_env = dict(os.environ)
    try:
        sys.platform = "darwin"
        check_spec()
        check_pyd_pack()
        check_clang_env()
        check_check_layout()
    finally:
        sys.platform = old
        if had_uname:
            os.uname = old_uname
        else:
            try:
                delattr(os, "uname")
            except Exception:
                pass
        os.environ.clear()
        os.environ.update(orig_env)

    passed = sum(1 for r in results if r[0])
    failed = [r for r in results if not r[0]]
    print("\n合计 %d 项，通过 %d，失败 %d" % (len(results), passed, len(failed)))
    for f in failed:
        print("  FAIL: %s -> %s" % (f[1], f[2]))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
