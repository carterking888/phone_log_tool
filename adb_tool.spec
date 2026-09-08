# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onedir 打包配置（配合 pyd_pack.py 使用，源码不入包）。

关键点：
- collect_all('pythonnet') + collect_all('clr_loader')：默认 hook 只收
  Python.Runtime.dll，缺 ~95 个 System.*.dll 会让目标机启动即报
  "Failed to resolve Python.Runtime.Loader.Initialize"。（仅 Windows）
- hiddenimports 补 _cffi_backend / clr / clr_loader.*：pythonnet -> clr_loader -> cffi
  是动态导入，静态分析会漏。
- UPX 关闭：.NET 托管 DLL 经 UPX 压缩后可能无法加载。

平台差异（IS_MAC 分支）：
- Windows：EdgeChromium + pythonnet，产物 dist\\adb_tool\\adb_tool.exe
- macOS  ：Cocoa/WKWebView，无 pythonnet，末尾多一个 BUNDLE 产出
  dist/adb_tool.app（否则只有裸 unix 可执行文件，双击不起 app）
"""
import ast
import os
import sys

IS_MAC = sys.platform == 'darwin'

# pythonnet / clr_loader 只在 Windows 需要；macOS 没装它们，
# collect_all 只会刷一堆 "package not found" warning。
if IS_MAC:
    clr_bin, clr_dat, clr_hid = [], [], []
    pn_bin, pn_dat, pn_hid = [], [], []
else:
    from PyInstaller.utils.hooks import collect_all

    clr_bin, clr_dat, clr_hid = collect_all('clr_loader')
    pn_bin, pn_dat, pn_hid = collect_all('pythonnet')

# ---------------------------------------------------------------------------
# 自动补齐 pyd 的 import 闭包
#
# PyInstaller 无法分析扩展模块（.pyd）内部的 import，静态分析到 core/app.pyd
# 就断了，webview / http.server / core.api 等统统不会进包，运行即
# ModuleNotFoundError。这里在打包时 AST 扫描源码（此时源码已被 pyd_pack.py
# 改名成 .py.src），把 import 闭包算出来塞进 hiddenimports。
# 好处：加新模块/新依赖不用手改 spec，永远和源码同步。
# 注意：源码只是被"读取"，不会被打进包。
# ---------------------------------------------------------------------------
_HERE = os.path.abspath(globals().get('SPECPATH') or os.getcwd())
_CORE = os.path.join(_HERE, 'core')
_MODULES = ('app', 'api', 'adb', 'logcat', 'labels', 'demo', 'action_log', 'zhdict',
            'ios', 'ioslog')

# ---------------------------------------------------------------------------
# iOS 支持（pymobiledevice3）是**可选**依赖：
# core/ios*.py 里的 pymobiledevice3 import 全在函数内（延迟 import），
# _imports_of 只扫顶层所以默认不进包、体积不变（~34MB）。
# 需要 iOS 时设 WITH_IOS=1 打包（前提：当前环境已 pip install pymobiledevice3），
# 产物约 65MB。qh3 是 pm3 的 QUIC/tunnel 依赖（iOS17+ 远程调试用），
# 本工具的取流走 usbmux 直连，tunnel 路径在本机缺 Apple NCM 驱动本就不可用，
# 排除 qh3 省 ~5MB（excludes 只在 Analysis 末尾统一加）。
# ---------------------------------------------------------------------------
WITH_IOS = os.environ.get('WITH_IOS') == '1'
ios_bin, ios_dat, ios_hid = [], [], []
if WITH_IOS:
    try:
        from PyInstaller.utils.hooks import collect_all as _collect_all

        ios_bin, ios_dat, ios_hid = _collect_all('pymobiledevice3')
        print('[spec] WITH_IOS=1 -> pymobiledevice3 已收集（qh3 已排除）')
    except Exception as _e:  # noqa: BLE001
        print('[spec][WARN] WITH_IOS=1 但收集失败: %s' % _e)


def _source_of(name):
    """编译后源码被暂避为 .py.src，兼容两种状态。"""
    for fn in (name + '.py.src', name + '.py'):
        p = os.path.join(_CORE, fn)
        if os.path.isfile(p):
            return p
    return None


def _imports_of(path, pkg='core'):
    """只收集**模块顶层**的 import（含完整点号路径，如 http.server）。

    坑（60MB 体积事故）：不能用 ast.walk —— 它会递归进函数体，把
    ios.py/ioslog.py 里所有函数内的 `from pymobiledevice3... import` 也扫进来，
    连带 cryptography/qh3 整条依赖链（~30MB）在不开 WITH_IOS 时也被打进包。
    这里只遍历 tree.body 顶层语句（顶层 If/Try/With 的直接 body 也算顶层），
    函数/类内部的延迟 import 一律不收。
    """
    out = set()

    def _scan_body(body):
        for node in body:
            if isinstance(node, ast.Import):
                for al in node.names:
                    out.add(al.name)
            elif isinstance(node, ast.ImportFrom):
                if node.level:  # 相对导入 -> core.xxx
                    if node.module:
                        out.add(pkg + '.' + node.module)
                    else:
                        for al in node.names:
                            out.add(pkg + '.' + al.name)
                elif node.module:
                    out.add(node.module)
            elif isinstance(node, (ast.If, ast.Try, ast.With)):
                _scan_body(node.body)

    try:
        with open(path, 'r', encoding='utf-8') as f:
            tree = ast.parse(f.read())
    except Exception:
        return out
    _scan_body(tree.body)
    return out


_pyd_hidden = set('core.' + m for m in _MODULES)
for _m in _MODULES:
    _src = _source_of(_m)
    if _src:
        _pyd_hidden |= _imports_of(_src)

# main.py 也扫一遍（入口本身是 .py，但保持一份保险）
_main_py = os.path.join(_HERE, 'main.py')
if os.path.isfile(_main_py):
    _pyd_hidden |= {n for n in _imports_of(_main_py) if not n.startswith('core.')}

# 'System' 来自 core/api.py 的 `from System import ...`（pythonnet，仅 Windows）。
# mac 上没这个模块，留在 hiddenimports 里只会刷 "Hidden import not found" 警告。
if IS_MAC:
    _pyd_hidden.discard('System')

pyd_hidden = sorted(_pyd_hidden)
print('[spec] pyd import closure: %d -> %s' % (len(pyd_hidden), pyd_hidden))

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=clr_bin + pn_bin + ios_bin,
    datas=[
        ('web', 'web'),
        ('config/android_perms_zh.json', 'config'),
        ('config/android_pkg_names.json', 'config'),
    ] + clr_dat + pn_dat + ios_dat,
    hiddenimports=(
        # pywebview 按平台动态 import_module('webview.platforms.' + gui)，
        # 静态分析扫不到，必须显式声明（macOS 是 cocoa，Windows 是 edgechromium）
        # objc/AppKit/Foundation/WebKit 是 pyobjc 的顶层模块，PyInstaller 的
        # pyobjc hook 偶尔漏收集，显式补上避免启动时 ModuleNotFoundError。
        ['webview.platforms.cocoa', 'objc', 'AppKit', 'Foundation', 'WebKit', 'websocket']
        if IS_MAC else [
            'webview.platforms.edgechromium',
            'websocket',
            '_cffi_backend',
            'cffi',
            'clr',
            'pythonnet',
            'clr_loader',
            'clr_loader.netfx',
            'clr_loader.ffi',
        ]
    ) + clr_hid + pn_hid + ios_hid + pyd_hidden,
    hookspath=[],
    hooksconfig={},
    # 在 pythonnet 导入前清除下载 ZIP 传播到 DLL 的 Zone.Identifier，
    # 否则目标机上的 .NET Framework 会拒绝 Python.Runtime.dll。（Windows only）
    runtime_hooks=[] if IS_MAC else ['rthook_unblock_dotnet.py'],
    # qh3: pm3 的 QUIC/tunnel 依赖，本工具用不上（见上方 WITH_IOS 注释）。
    # 放 excludes 保证即使被分析到也不会进包（WITH_IOS 时同样生效）。
    excludes=['tkinter', 'qh3'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='adb_tool',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='adb_tool',
)

# macOS：COLLECT 只有裸 unix 可执行文件，必须再套一层 .app bundle，
# 否则双击无反应、也没有 Dock 图标。产物在 dist/adb_tool.app
if IS_MAC:
    app = BUNDLE(
        coll,
        name='adb_tool.app',
        icon=None,
        bundle_identifier='com.local.adbtool',
        info_plist={
            'NSHighResolutionCapable': True,
            # 不声明的话 macOS 可能把窗口当低分屏渲染，字体发虚
        },
    )
