# -*- coding: utf-8 -*-
"""Cython 编译：core/*.py -> 扩展模块（源码不入包）。

用法：  python setup_pyd.py build_ext --inplace
产物：  Windows  core/api.cp313-win_amd64.pyd
        macOS    core/api.cpython-313-darwin.so
        （ABI 标签随 Python 版本变化）

注意：core/__init__.py 不参与编译（保持 .py，PyInstaller 才能识别为普通包）。
"""
import os
import sys

# ---- MSVC / Windows SDK 环境注入（等价于 vcvars64，免 VS 开发者命令行）----
# MSYS/Git Bash 会把带空格的 SDK 路径拼坏，所以在 Python 进程内设置最可靠。
MSVC_ROOT = r"C:\software\Microsoft Visual Studio18\VC\Tools\MSVC"
SDK_DIR = r"C:\Program Files (x86)\Windows Kits\10"


def setup_clang_env():
    """macOS：指定最低系统版本 + 单一架构。

    - MACOSX_DEPLOYMENT_TARGET 不设的话，Python 的 sysconfig 常带 10.x 目标，
      clang 会警告甚至找不到对应 SDK；arm64 机器统一抬到 11.0。
    - ARCHFLAGS 限定单一架构：brew 的 universal2 Python 不加会同时编两种架构，
      慢一倍且容易在链接期报 "file is universal but does not contain ..."。
    """
    if sys.platform != "darwin":
        return
    try:
        arch = os.uname().machine  # arm64 | x86_64
    except Exception:
        arch = "arm64"
    os.environ.setdefault(
        "MACOSX_DEPLOYMENT_TARGET", "11.0" if arch == "arm64" else "10.13")
    os.environ.setdefault("ARCHFLAGS", "-arch " + arch)
    print("[clang] arch=%s | deployment target=%s"
          % (arch, os.environ["MACOSX_DEPLOYMENT_TARGET"]))


def setup_msvc_env():
    if os.environ.get("ADB_TOOL_MSVC_OK"):
        return
    try:
        ver = sorted(os.listdir(MSVC_ROOT))[-1]
    except Exception:
        return
    msvc = os.path.join(MSVC_ROOT, ver)
    cl = os.path.join(msvc, "bin", "HostX64", "x64", "cl.exe")
    if not os.path.isfile(cl):
        return
    try:
        sdk_ver = sorted(os.listdir(os.path.join(SDK_DIR, "Include")))[-1]
    except Exception:
        return

    inc = ";".join([
        os.path.join(msvc, "include"),
        os.path.join(SDK_DIR, "Include", sdk_ver, "ucrt"),
        os.path.join(SDK_DIR, "Include", sdk_ver, "um"),
        os.path.join(SDK_DIR, "Include", sdk_ver, "shared"),
        os.path.join(SDK_DIR, "Include", sdk_ver, "winrt"),
    ])
    lib = ";".join([
        os.path.join(msvc, "lib", "x64"),
        os.path.join(SDK_DIR, "Lib", sdk_ver, "ucrt", "x64"),
        os.path.join(SDK_DIR, "Lib", sdk_ver, "um", "x64"),
    ])
    bin_dirs = ";".join([
        os.path.join(SDK_DIR, "bin", sdk_ver, "x64"),  # rc.exe
        os.path.join(msvc, "bin", "HostX64", "x64"),
    ])
    os.environ["INCLUDE"] = inc
    os.environ["LIB"] = lib
    os.environ["PATH"] = bin_dirs + ";" + os.environ.get("PATH", "")
    os.environ["ADB_TOOL_MSVC_OK"] = "1"
    print("[msvc] %s | SDK %s" % (ver, sdk_ver))


if sys.platform == "darwin":
    setup_clang_env()
else:
    setup_msvc_env()

from setuptools import setup  # noqa: E402
from Cython.Build import cythonize  # noqa: E402

TARGETS = [
    os.path.join("core", m + ".py") for m in (
        "app", "api", "adb", "logcat", "labels", "demo", "action_log", "zhdict",
        "ios", "ioslog",
    )
]
TARGETS = [t for t in TARGETS if os.path.isfile(t)]

if not TARGETS:
    sys.stderr.write("[错误] 没有找到待编译的 core 模块\n")
    sys.exit(1)

print("[cython] targets: %s\n" % ", ".join(TARGETS))

setup(
    name="adb_tool_core",
    ext_modules=cythonize(
        TARGETS,
        compiler_directives={"language_level": "3"},
    ),
)
