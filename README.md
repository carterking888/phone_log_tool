# ADB 日志与设备调试工具

支持 **Android（adb）** 与 **iOS（pymobiledevice3，可选）** 双平台：
设备管理 / 实时日志 / 应用包管理 / 文件管理 四大模块。

## 运行

```bash
pip install -r requirements.txt
python main.py            # 或双击 run.bat
```

- Python 3.9+；Windows 需 WebView2 运行时（Win11 自带）。
- adb 按「设置页手动指定 → PATH → ANDROID_HOME → 随包便携版 → 常见路径」自动探测，
  未装也能启动（手动进入演示模式，不会静默回退假数据）。
- iOS 支持：`pip install pymobiledevice3`（Windows 需 iTunes/Apple 驱动提供 usbmux）。

## 打包

| 平台 | 命令 | 产物 |
| --- | --- | --- |
| Windows | `build_pyd.bat`（pyd 混淆 + onedir，需 MSVC） | `dist\adb_tool\`，**整目录分发** |
| macOS | `./build_mac.sh`（自建 venv，自动内置 adb） | `dist/adb_tool.app` + zip |
| macOS 免机器 | GitHub Actions → Build macOS App（可选 with_ios / dmg） | Artifacts 下载 |

- Windows 产物含 `fix_and_check.bat`：目标机首次解压后跑一次（解 MOTW + 查 .NET 4.7.2 + WebView2）。
- mac 产物首次打开右键 → 打开；网络下载的包若报「已损坏」执行 `xattr -cr adb_tool.app`。
- iOS 支持需在打包时以 `WITH_IOS=1`（脚本会询问）装入 pymobiledevice3，产物内嵌运行时，目标机免 Python 环境。

## 目录结构

```
main.py                 入口转发（3 行，无业务逻辑）
core/
  app.py                启动流程（HTTP Server + 窗口 + 控制台噪音过滤）
  adb.py / ios.py       adb 封装 / iOS 后端（常驻 asyncio 线程）
  logcat.py / ioslog.py 双平台日志会话（输出统一为同一种行结构）
  api.py                js_api 层（业务方法 + 长任务 Task 机制，按 platform 分发）
  demo.py               演示数据（显式开启，不自动回退）
  labels.py / zhdict.py 应用名解析 / 中文词库懒加载
config/                 权限中文（368 条）+ 包名中文（359 条）两个 JSON
web/                    index.html + css + js/pages/*（四个页面组件）+ vendor 本地化依赖
build_pyd.bat / adb_tool.spec / pyd_pack.py          Windows 打包
build_mac.sh / .github/workflows/build-mac.yml       macOS 打包
build_mac_win.bat       源码 zip / 触发云端构建
_dom_check.js 等        离线回归脚本（见下）
```

## 关键约定

- **实时日志**：后端子进程/流式入库，前端自适应轮询（有新日志 400ms，空闲 2s）；
  内存环形上限 5 万行；暂停只停渲染，恢复一次性补齐。
- **Cocos JS 捕获（iOS）**：usbmux 端口转发 + CDP 捕获 console，纯 USB 免管理员；
  游戏重启自动重连（多候选地址依次握手）；「开启 INFO 日志」按钮经调试口执行
  `cc.debug._resetDebugSetting(DebugMode.INFO)`，无需重打包。
- **长任务**：push/pull/install/uninstall 全部返回 taskId，前端 300ms 轮询，可取消；
  安装进度靠 push 后按目标端 stat 计算速率；`.xapk/.apks` 走 install-create 会话。
- **下载**：不用 `adb pull`（Windows 下中文/空格路径会坏），统一 `exec-out cat / tar` 流式下载；
  私有目录自动 run-as 回退（debuggable 包）。
- **目录列举**：`ls -la` 路径必须带尾斜杠（符号链接只返回链接本身）。
- **多设备**：所有命令强制 `-s <serial>`；顶栏切换设备后各模块会话重置。
- **应用名**：静态中文词库优先 → aapt 解析（按 serial:pkg 缓存）→ 包名兜底；
  词库缺失不报错。前缀兜底默认关闭（会把大量包归成同名）。
- **危险操作**：二次确认 + 输入 DELETE + 操作日志联动。

## 验证（无需真机即可跑大部分）

```bash
node _dom_check.js            # jsdom 渲染+交互冒烟，97 项
python _backend_check.py      # 后端自测（真机+演示模式双跑，按平台分流）
python _mac_dryrun_check.py   # Windows 上模拟 darwin 预检 mac 打包分支
python _render_check.py       # 真实窗口渲染冒烟（需桌面会话）
```

## 平台差异速查

| | Windows | macOS |
| --- | --- | --- |
| 渲染后端 | WebView2 + pythonnet（需 .NET 4.7.2+） | Cocoa / WKWebView（无需 .NET） |
| Cython 产物 | `core/*.pyd` | `core/*.so` |
| 打包形态 | onedir（exe + `_internal`） | `.app` bundle + ad-hoc 签名 |
| adb 来源 | 便携目录 / PATH / SDK | 包内 `Contents/MacOS/public_settings/adb`（构建时自动下载） |

PyInstaller 不支持交叉编译：mac 产物必须在 macOS 或 GitHub Actions 上构建。
