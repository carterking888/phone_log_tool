# -*- coding: utf-8 -*-
"""ADB 日志与设备调试工具 - 入口。

保持 .py 不编译（作为 PyInstaller 启动点，无业务逻辑），
真实逻辑在 core/app.py（构建时由 Cython 编译为 pyd）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.app import main  # noqa: E402

if __name__ == "__main__":
    main()
