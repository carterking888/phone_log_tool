# -*- coding: utf-8 -*-
"""PyInstaller 启动钩子：解除下载 ZIP 传递给托管 DLL 的 MOTW。

Windows 会把 Zone.Identifier 从下载的 ZIP 传播到解压文件。.NET Framework
会拒绝加载带该标记的 Python.Runtime.dll，表现为：
Failed to resolve Python.Runtime.Loader.Initialize。

此钩子在应用代码和 pythonnet 导入前运行，只处理当前应用目录内需要加载的
二进制文件，不调用 PowerShell，也不触碰目录外文件。
"""
import ctypes
import os
import sys


def _unblock_bundled_binaries():
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return

    root = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    delete_file = ctypes.windll.kernel32.DeleteFileW
    delete_file.argtypes = [ctypes.c_wchar_p]
    delete_file.restype = ctypes.c_int

    for dirpath, _dirs, filenames in os.walk(root):
        for name in filenames:
            if name.lower().endswith((".dll", ".pyd", ".exe")):
                # 不存在该 ADS 时 DeleteFileW 返回 0；无需区分，继续启动即可。
                delete_file(os.path.join(dirpath, name) + ":Zone.Identifier")


_unblock_bundled_binaries()
