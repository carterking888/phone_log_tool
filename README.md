# ADB 日志与设备调试工具

桌面端 Android 调试工具：深色开发主题。
四大模块：设备管理 / 日志查看 / 应用包管理 / 文件管理。

## 一、运行

```bash
# 依赖（注意包名是 pywebview，不是 webview）
pip install -r requirements.txt

# 启动
python main.py          # 或双击 run.bat

# 打包（pyd 混淆，onedir；源码不入包）
build_pyd.bat           # 双击即可，build.bat 是它的转发壳
```

打包要求：MSVC（`C:\software\Microsoft Visual Studio18`）+ Windows SDK
（环境由 `setup_pyd.py` 在进程内注入，无需 VS 开发者命令行）。产物在
`dist\adb_tool\`，**分发时必须带整个目录**（依赖在 `_internal`），不能只拷 exe。

环境要求：Python 3.9+ / Windows（WebView2）。adb 按 `PATH → ANDROID_HOME → 常见路径` 自动探测，
未装 adb 也能启动（进入演示模式）。

## 二、目录结构

```
main.py                 入口转发（保持 .py，仅 3 行，无业务逻辑）
core/
  app.py                启动流程（HTTP Server + pywebview 窗口 + COM 噪音过滤）
  adb.py                adb 命令封装（utf-8 解码、超时、设备/包/目录解析）
  logcat.py             logcat 流式会话（子进程 + 读线程 + 游标批量拉取）
  ios.py                iOS 后端（pymobiledevice3 可选依赖 + 常驻 asyncio 线程）
  ioslog.py             iOS 日志会话（syslog / tunnel+oslog，输出对齐 logcat 行结构）
  api.py                前端 API 层（业务方法 + 长任务 Task 机制，按 platform 分发）
  demo.py               演示数据（无设备时的完整替身）
  labels.py             应用名解析（aapt 可选 + 缓存，缺失时降级为包名）
  action_log.py         操作日志联动（~/.adb_tool/action_logs/日期.jsonl）
config/
  android_perms_zh.json   权限中文字库（368 条）
  android_pkg_names.json  包名中文字库（359 条）
web/
  index.html            4 页面 SPA + 9 个弹窗
  css/app.css           深色主题（#0d1117 / #161b22 / 主色 #1f6feb / 成功 #238636）
  js/app.js             根状态：路由 / 设备上下文 / 轮询 / 任务监听 / toast
  js/util.js            格式化、类型图标、pywebview 桥封装
  js/pages/*.js         四个页面组件（devices / logs / apps / files）
  vendor/               本地化依赖：petite-vue 0.4.1 + layui 2.9.7（含字体）
_dom_check.js           jsdom 渲染 + 交互冒烟（54 项，无需 GUI）
_render_check.py        真实 WebView2 渲染冒烟（需能弹窗的会话）
```

## 三、关键实现约定

| 主题 | 做法 |
| --- | --- |
| 实时日志 | 后端 `Popen(adb logcat -v threadtime)` + 读线程入库；前端**自适应轮询**游标批量拉取（有新日志 400ms，连续空闲或不在日志页降到 2s）。内存环形上限 8000 行，渲染 tail 600 行 |
| 暂停不丢数据 | 暂停只停渲染，后台继续入库，恢复时一次性补齐 |
| 包名过滤 | 转 `--pid`；**每 5s `pidof` 跟踪 PID**，进程重启自动更新过滤并提示 |
| 安装进度 | `adb install` 无原生进度：先 `push` 到 `/data/local/tmp` 按目标端 `stat` 算速率与百分比，再 `pm install` |
| `.xapk/.apks` | 本地解包 → `pm install-create -S` → 逐 split `push` + `pm install-write` → `pm install-commit`（失败会 `install-abandon`） |
| 长任务 | 所有 push/pull/install/uninstall/remove 返回 `taskId`，前端 300ms 轮询 `get_task`，可取消 |
| 危险操作 | 删除/卸载/清数据/停用：二次确认 + 输入 `DELETE` + 操作日志联动三件套 |
| 多设备 | 所有命令强制 `-s <serial>`；顶栏切换设备后各模块会话重置 |
| 演示模式 | 无 adb 或无已授权设备时 `env.demo=true`，页面顶部橙色横幅明示，数据来自 `core/demo.py` |
| 下载（关键） | **不用 `adb pull`**：Windows adb 对中文远程路径会丢扩展名（`中文.txt`→`中文`）、带空格直接失败。改用 `adb exec-out cat` / `exec-out tar -cf - -C dir .` 流式下载，本地文件名自己定（见下） |
| 目录列举 | `ls -la` 的路径**必须带尾斜杠**，否则 `/sdcard`（符号链接）只会返回链接本身 |
| 无权限目录 | 非 root 访问 `/cache`、`/data` 等会 `Permission denied`，后端转成中文提示「该目录需要 Root 权限…」并标记 `needRoot`，前端直接展示 |
| 应用名 | **静态词库优先**（`android_pkg_names.json`，即时无 IO）→ 未命中由 aapt/aapt2 解析 `application-label` 并按 `serial:pkg` 缓存 → 仍无则显示包名。词库命中的不再走 aapt，省掉 pull |
| 权限中文 | `android_perms_zh.json` 368 条。有映射显示中文（原名放 `title` 悬停），无映射显示原名；`dangerous` 组加红点标记 |
| 存储分类 | 真机靠 `du -sk` 后台统计（较慢），前端按 `categoriesReady` 自动重拉 |
| 文件对话框 | js_api 跑在**后台线程**，而 pywebview 的 `create_file_dialog` 无 Invoke → 统一走 `core/api.py` 的 `_on_ui_thread()` 调度回 UI 线程，全链路兜底（失败退回直接调用） |
| 控制台噪音 | pywebview 跨线程 COM 噪音由 `main.py` 的过滤器精准拦截（详见第六节），`python main.py --verbose` 可关闭过滤 |

## 四、验证

```bash
# 1) jsdom 渲染 + 交互冒烟（推荐，无需 GUI / 无需真机，75 项）
#    jsdom 装在项目外，必须用 NODE_PATH 指过去，否则报 Cannot find module 'jsdom'
NODE_PATH=C:/Users/qifeng.wang/.workbuddy/binaries/node/workspace/node_modules node _dom_check.js

# 2) 真实窗口渲染冒烟（需要能弹窗的桌面会话）
python _render_check.py
#    ⚠ 无桌面会话（SSH / 无头 CI）时 webview.start() 会一直阻塞，
#      脚本自带 90s 看门狗，超时 exit 2 并提示"当前会话无法创建窗口"。
#      这种环境下跑不了，用 1)+3) 代替。

# 3) 后端自测（真机 + 强制演示模式双跑；真机用例按平台分流：
#    Android 跑 dumpsys/pm 那套，iOS 跑 AFC/应用/日志那套，离线 23 项）
python _backend_check.py

# 4) macOS 打包分支预检（在 Windows 上模拟 darwin，16 项，无需真 mac）
python _mac_dryrun_check.py
```

`_dom_check.js` 覆盖：4 个路由逐个切换、日志流增长、级别/PID/Tag/关键字/包名五层过滤、
芯片增删、保存弹窗、应用搜索/分类/详情/安装进度、**应用名解析回填**、**设置弹窗**、
**权限中文显示与无映射回落**、**危险权限计数与标记**、文件浏览/视图切换/勾选/DELETE 确认、设备切换。

## 五、Petite-Vue 落地约束（已在本项目落实）

1. 子组件在 state 里直接实例化，启动逻辑挂 `<body @vue:mounted="init()">`（`created/mounted` 不执行）。
2. `init()` 里 `window.AdbApp = this` 拿**响应式 proxy**，跨组件调用一律用 `AdbApp`（模板里拿不到 `this`）。
3. 过滤/分页/计数全部用 **getter 派生**，禁止"改状态不过滤"。
4. 模板不写复杂表达式，展示字段在 `util.js` 预映射（`levelCls` / `iconSvg` / `sizeText` …）。
5. `init()` 必须等 `pywebviewready`；浏览器预览 3s 兜底提示，不静默回退假数据。
6. **会解引用"可能为 null 的对象"的区块必须用 `v-if` 而非 `v-show`** —— `v-show` 不会阻止子表达式求值，
   且 `v-if` 移除区块与文本 effect 存在一帧竞争，因此对象置 null 前要先关闭容器（见 `apps.closeDetail`）。
7. **WinForms 文件对话框返回的是数组**（`('D:\\xx.apk',)`），不是字符串——`save_file_dialog` /
   `choose_file` 单选已在后端归一化为字符串，前端仍要防一手（数组在 `<input>` 里会 join 成正常路径的
   假象，回传 Python 时变成 list → `os.path.dirname` 抛 `TypeError: expected str, bytes or
   os.PathLike object, not list`）。三层防御：后端归一化 → 前端 `Array.isArray` 收敛 →
   `save_logs` 入口再兜一次底。

## 六、控制台刷屏：`Error while processing window.native.browser.webview.*`

启动后控制台可能刷出大量这类日志（每条都带一长串 `InvalidCastException` / `E_NOINTERFACE`）：

```
[pywebview] Error while processing window.native.browser.webview.CanGoBack:
           CoreWebView2 can only be accessed from the UI thread. --->
           System.InvalidCastException: ... 0x80004002 (E_NOINTERFACE)
[pywebview] Error while processing window.native.AccessibilityObject.Bounds.Empty.Empty...:
           maximum recursion depth exceeded
```

已观测到两族变体，**全部以 `Error while processing window.native.` 开头**：

- `window.native.browser.webview.<Prop>` → `E_NOINTERFACE (0x80004002)` / "UI thread"
- `window.native.AccessibilityObject.Bounds.Empty.Empty...`、
  `window.native.ActiveControl.ModifierKeys.Add.Add...` → `maximum recursion depth exceeded`
  （pythonnet 反射链无限包装属性）

**结论：无害噪音，不是本项目 bug，功能不受影响。** 已定位到 pywebview 5.4 源码：

| 步骤 | 位置 | 行为 |
| --- | --- | --- |
| 1 | `webview/util.py` `js_bridge_call()` | 把 js_api 调用放进**独立 Thread** 执行（源码注释：避免阻塞 UI 线程） |
| 2 | 同函数 `_call()` 末尾 | 用 `window.evaluate_js(code)` 回传结果 —— **仍在后台线程** |
| 3 | `webview/platforms/edgechromium.py` `evaluate_js()` | `self.webview.Invoke(...)` 跨线程访问 WebView2 COM |
| 4 | pythonnet 3.1 | 对控件每个属性抛 `E_NOINTERFACE(0x80004002)`，打印路径后**吞掉异常** |

判断它无害的依据：`evaluate_js` 失败会打印 `Error occurred in script`（`edgechromium.py:132`），
日志里没有这一条，说明脚本执行成功、结果正常回传。

**本项目做了两层处理：**

1. **精准过滤**（`main.py` 的 `_EdgeComNoiseFilter`）：只拦以 `Error while processing window.native.`
   开头的噪音（覆盖上述两族变体），pywebview 的**真实错误照常输出**。
   退出时打印一行 `[info] 已抑制 N 条 …`，不会静默掩盖问题。
   排查 pywebview 自身问题时用 `python main.py --verbose` 关掉过滤。
2. **减少触发次数**：日志轮询改为自适应（有新日志 400ms，空闲/离开日志页降到 2s）。
   每次 js_api 调用都会新建线程 + 跨线程回传，降频顺带减少线程创建。

> 不要试图用 `PYWEBVIEW_LOG=critical` 静音——那会连 pywebview 的真实错误（`Error occurred in script`、
> `Function xxx() does not exist`）一起吞掉，正是排查时最需要的那几条。

## 七、中文词库（权限 / 包名）

两个 JSON 都在项目根目录 `config\` 下，运行时按需懒加载，**缺失也不报错**（查询返回 `None`，调用方回落原值）。
查找顺序：`ADB_TOOL_DATA_DIR` 环境变量 > 打包目录（`_MEIPASS` 及其 `config\`）> 项目根 `config\` > 项目根（老位置兼容）。

**接入点**：应用列表 / 应用详情权限 / **日志"按应用包过滤"弹窗的运行中进程**（`get_pid_map`
顺带返回 `labels` 字典，命中显示中文、native 进程回落原进程名）；弹窗搜索框按
中文名 / 包名 / 展示名模糊包含匹配。

| 文件 | 内容 | 条数 |
| --- | --- | --- |
| `config\android_perms_zh.json` | `Manifest.permission` 权限中文化，分 dangerous/normal/signature | 368 |
| `config\android_pkg_names.json` | 常见包名 → 中文应用名，另含 22 条前缀兜底规则 | 359 |

**显示规则**：有映射 → 中文；无映射 → 原值（权限原名 / 包名）。权限的中文项仍保留原名在 `title` 里，
调试时复制搜索要用原名。

### 关于前缀兜底：默认关闭，别开

`android_pkg_names.json` 的 `prefix_fallback` 会把未命中的包按前缀归类。实测（三星机 444 个包）：

| 方案 | 覆盖率 | 实际效果 |
| --- | --- | --- |
| 仅精确匹配（**当前**） | 44/444 = 10% | 44 个有准确中文名，其余显示包名，**都能区分** |
| 开启前缀兜底 | 389/444 = 88% | 但其中 214 个包全叫"三星系应用"、51 个"安卓系统组件"…… **无法区分** |

覆盖率从 10% 涨到 88% 看着漂亮，实际是把 214 行变成同名。调试工具里包名至少有区分度，
所以默认关闭。真要开：`zhdict.pkg_zh(pkg, use_prefix_fallback=True)`。

### 打包

两个 JSON 靠 `sys._MEIPASS` 定位，PyInstaller 必须显式带上（`adb_tool.spec` 已配 `datas`）。
也支持用环境变量 `ADB_TOOL_DATA_DIR` 指向自定义词库目录。

## 八、打包（pyd 混淆，源码不入包）

双击 `build_pyd.bat`，四步自动完成：

| 步骤 | 文件 | 干什么 |
| --- | --- | --- |
| 1 | `requirements.txt` | 装 pywebview + cython + pyinstaller + setuptools |
| 2 | `setup_pyd.py` | `cythonize` 把 `core/*.py` 编成 `.pyd`（MSVC 环境在脚本内注入） |
| 3 | `pyd_pack.py` | 源码暂避 `.py.src` → PyInstaller → 校验 → 恢复源码 |
| 4 | `fix_and_check.bat` | 拷进产物根目录，供目标机解压后跑一次 |

**产物**：`dist\adb_tool\`（约 32 MB，onedir）：

```
dist\adb_tool\
├── adb_tool.exe          入口（5 MB，无业务逻辑）
├── _internal\            core\*.pyd + web\ + 两个词库 JSON + pythonnet 运行时
└── fix_and_check.bat     目标机首次解压后跑一次
```

### 三个关键点（都踩过坑）

1. **pyd 里的 import PyInstaller 看不见。** 静态分析到 `core/app.pyd` 就断了，
   `webview`、`http.server`、`core.api` 统统不进包，运行即 `ModuleNotFoundError`。
   对策：`adb_tool.spec` 在打包时用 `ast` 扫描源码算出 import 闭包（30 项）
   自动塞进 `hiddenimports`——加新依赖不用手改 spec。
2. **`--clean` 会失败。** 它批量删 `bincache`（>50 个文件）被安全删除钩子拦截，
   exit 1 且没有 traceback。旧产物改成 rename 挪走，不加 `--clean`。
3. **pythonnet 依赖必须补全。** 默认 hook 只收 `Python.Runtime.dll`，
   漏了 `pythonnet/runtime/` 下 97 个 `System.*.dll` 和 `clr_loader` 的
   `ClrLoader.dll`，**本机正常、拷到别的电脑就报**
   `Failed to resolve Python.Runtime.Loader.Initialize`。
   用 `collect_all('pythonnet')` + `collect_all('clr_loader')` 解决。

### 校验（pyd_pack.py 自动跑）

`core pyd 8/8`、`leaked py: none`、`web/index.html OK`、两个 JSON OK、
`pythonnet runtime dll: 97`、`ClrLoader.dll OK`。

启动冒烟：进程存活 + 内置 HTTP 服务起端口 + `GET /index.html` 返回 200/49028 字节。

### 分发注意

- **整目录分发**（zip 打包 `dist\adb_tool`），exe 单独拷走缺 `_internal` 依赖。
- 目标机首次解压后先跑 `fix_and_check.bat`，它做三件事：解除 MOTW（否则 .NET DLL
  被拦，报 `Python.Runtime.Loader.Initialize`）、查 .NET Framework ≥ 4.7.2、
  **查 WebView2 运行时**。
- **WebView2 是硬依赖**：pywebview 走 EdgeChromium，缺它表现为"进程起来了但窗口空白"。
  Win11 自带，Win10 很多机器没有 —— 缺失时按 bat 提示装 Evergreen Runtime 即可。
- UPX 关闭：.NET 托管 DLL 经 UPX 压缩后可能加载失败。

### 想补词库

直接往两个 JSON 的 `all` 里加键值即可，`by_group` 只用于统计和危险权限标记，不参与查询。
改完重启应用生效（`zhdict.reload()` 可热重载）。当前这台三星机缺的主要是
`com.samsung.*`(214)、`com.sec.*`(67)、`com.android.*`(51)、`com.google.*`(38)。

## 九、便携运行时：没装 Android SDK 的电脑也能用

分发包可以自带一份 adb，目标机零安装。目录放在**与 `08_adb_tool` 同层**的
`public_settings\adb\`（也可直接放在程序目录里）：

```
android_tool\
├── 08_adb_tool\          项目/程序
└── public_settings\
    ├── adb\              ★ 本工具需要（adb.exe + 3 个 dll）
    ├── allure-commandline\   ✗ 用不到（其他项目遗留）
    └── jre\                  ✗ 用不到（本工具不依赖 Java）
```

**只有 adb 是必需的。** 应用名解析走 `aapt/aapt2`（Android SDK 工具，不需要 JVM），
adb 本身也不依赖 Java —— 所以 `jre`（Temurin 17）和 `allure-commandline`
对本工具完全无用，分发时可以删掉（能省上百 MB）。

### 查找顺序（改自 `core/adb.py`）

| 优先级 | 来源标记 | 说明 |
| --- | --- | --- |
| 1 | `config` 手动指定 | 设置页填的路径 |
| 2 | `path` 系统 PATH | 用户自己装的环境 |
| 3 | `sdk` Android SDK | `ANDROID_HOME` / `ANDROID_SDK_ROOT` |
| 4 | **`portable` 随包便携版** | `public_settings\adb\`（**没环境时兜底**） |
| 5 | `common` 常见路径 | `C:\software\platform-tools` 等 |
| 6 | `fallback` 未找到 | 回落裸 `adb` |

探测目录：程序目录及其**上一层**的 `public_settings\adb\` 和 `adb\`。
设置页和设备页会显示「来源」，一眼看出用的是哪个 adb。

### 打包时自动带上

`pyd_pack.py` 在 PyInstaller 之后自动把 `..\public_settings\adb` 整目录拷进
`dist\adb_tool\public_settings\adb\`，并校验 dll 齐全。
`SKIP_PORTABLE_ADB=1` 可跳过；mac 产物自动跳过（不能塞 Windows 的 exe）。

> **必须整目录拷**：`adb.exe` 依赖同目录的 `AdbWinApi.dll` / `AdbWinUsbApi.dll`，
> 只拷 exe 会出现"能启动但连不上设备"。

### aapt（可选，决定应用名好不好看）

没有 aapt 时工具照样能用，只是未命中静态词库的应用**显示包名**而不是中文名。
想让没环境的电脑也解析应用名，把 `aapt2.exe`（来自 Android SDK 的 `build-tools`，
约几 MB，通常自包含）丢进 `public_settings\adb\` 即可 —— `find_aapt()` 已经把便携目录
纳入查找（顺序：PATH > 便携目录 > Android SDK）。

> 注意：开发机 `C:\software\platform-tools` 里**没有** aapt —— 它不在 platform-tools 包里，
> 而在 `build-tools\<版本>\aapt2.exe`。

## 十、iOS 支持（可选依赖）

工具同时支持 Android（adb）与 iOS（iPhone）。设备列表会把两端合并展示，
卡片上有 `Android` / `iOS` 平台徽标，切换设备后日志 / 应用 / 文件三个模块自动走对应后端。

### 启用

```bash
pip install pymobiledevice3      # 纯 Python，Windows / macOS / Linux 通用
```

- **Windows 前提**：需要 iTunes 或「Apple Mobile Device Support」驱动提供 usbmux 通信
  （装过 iTunes 的机器都有）。没装的话设备列表里不会出现 iPhone。
- **首次连接**：iPhone 上点「信任此电脑」并输入锁屏密码，配对记录会存到本地。
- **可选依赖**：没装时工具照常跑 Android，设备页会显示「iOS 支持：未启用」，
  不会静默降级成假数据。

### 能力边界（iOS 非越狱，别指望越权）

| 模块 | 支持情况 | 说明 |
| --- | --- | --- |
| 设备信息 | ✅ | 型号 / iOS 版本 / 名称 / UDID / 序列号 / 电量 / 存储 |
| 应用列表 | ✅ | Bundle ID + 应用名 + 版本 + 体积，卸载可用 |
| 安装 ipa | ⚠️ | 未签名或签名不匹配的包会被系统拒绝 |
| 实时日志 | ⚠️ | 系统日志走 os_trace/syslog；Cocos Debug 包可在设置中填写 `IP:6086`，合并捕获 JS console |
| 文件管理 | ⚠️ | AFC 只能访问**媒体域**（`/DCIM`、`/Books`、`/Downloads`、`/Photos` 等），应用沙盒需越狱 |
| 权限列表 | ❌ | 系统不向第三方提供，详情页如实说明而不是留空 |
| 启动 / 强停 / 清数据 | ❌ | iOS 没有 am/pm 能力，按钮会隐藏并给出说明 |
| 安全擦除 | ❌ | 明确拒绝（提示改用普通删除），不假装成功 |

### 实现要点

- `core/ios.py` / `core/ioslog.py` 是新增的 iOS 后端与日志会话；`core/api.py` 按
  `platform` 分发（缓存的 serial→平台映射，兜底按 UDID 形态判定）。
- **pymobiledevice3 是纯 async 库**：服务全是 async context manager，方法几乎都是
  coroutine。`core/ios.py` 起了一个常驻后台线程的事件循环，对外只暴露同步接口。
- **iOS 日志行会统一成 Android 的结构** `{time,pid,tid,level,tag,msg}`
  （tag 取进程名或 `subsystem:category`，级别映射成 V/D/I/W/E/F），
  所以日志页的过滤 / 高亮 / 导出完全复用，前端零改动。
- Cocos Creator/Cocos2d-x 的 JS 日志不进入 Unified Logging；设置页填写
  `chrome://inspect` 显示的 `Cocos2d-x Games` 地址后，工具通过 CDP
  `Runtime.consoleAPICalled` 合并捕获。Inspector 只允许一个客户端，使用工具时需关闭
  Chrome 的 Inspect 窗口。
- 真实能力已在 iPhone 12 / iOS 26 上验证：syslog 直连可用（0.2s 出流，**不需要 tunnel**），
  应用列表 35 个三方包、AFC 根目录 11 项、上传下载往返内容一致。

### 打包

```bat
pip install pymobiledevice3
set WITH_IOS=1
build_pyd.bat
```

不设 `WITH_IOS=1` 时产物只含 Android 能力，体积不变
（`core/ios.py` 里的 pymobiledevice3 全是函数内延迟 import，AST 扫描不会带进来）。

## 十一、macOS 打包（.app）

**能打，但不能在 Windows 上打。** PyInstaller 不支持交叉编译——Windows 只能产出
`.exe`，mac 产物必须在 macOS 上构建（或用 GitHub Actions 的 `macos-latest` runner，
免费额度足够）。

```bash
chmod +x build_mac.sh
./build_mac.sh           # -> dist/adb_tool.app + dist/adb_tool_macos_arm64.zip
./build_mac.sh --dmg     # 额外再出一个 dmg
```

前提：macOS + Xcode CLT（`xcode-select --install`）。脚本自建 `.venv_mac`，
不污染系统 Python。

### 没有 mac 机器怎么办

用 GitHub Actions：`.github/workflows/build-mac.yml`。
Actions 页 → **Build macOS App** → Run workflow，可选架构（`both` / `arm64` / `x86_64`）
和是否出 dmg，跑完在 Artifacts 里下载 `adb_tool-macos-<arch>.zip`。

matrix 用 `macos-14`(arm64) + `macos-13`(x86_64) 两档，一次出两种芯片的包。
workflow 自带冒烟：进程存活 15s + `core/*.so` 齐全 + `web/index.html` 在位。

### 平台差异（同一套 spec，靠 `IS_MAC` 分支）

| | Windows | macOS |
| --- | --- | --- |
| 渲染后端 | EdgeChromium / WebView2 + pythonnet | Cocoa / WKWebView（**不需要 pythonnet，也不需要 .NET**） |
| Cython 产物 | `core/*.pyd` | `core/*.so` |
| 产物形态 | `dist\adb_tool\adb_tool.exe` | `dist/adb_tool.app`（内容在 `Contents/MacOS`） |
| 额外步骤 | 无 | `BUNDLE()` + ad-hoc `codesign` |
| 分发 | zip 整目录 | zip（必须 `-y` 保留符号链接）或 dmg |

业务代码本身已是跨平台的：`_no_window()` 按 `IS_WIN` 返回 `CREATE_NO_WINDOW` 或 0；
`_on_ui_thread()` 里 `from System import ...` 失败会退回直接调用（mac 没 pythonnet）。

### mac 特有的四个坑

1. **架构必须匹配**。M 系列打出来是 `arm64`，Intel 是 `x86_64`，互不兼容
   （对方双击会报"无法打开"）。脚本按本机 `uname -m` 走，产物名带架构后缀。
2. **必须 `BUNDLE()`**。只做 `COLLECT` 出来的裸 unix 可执行文件双击没反应、
   也没有 Dock 图标。spec 末尾在 `IS_MAC` 时多套一层 `.app`。
3. **必须签名**。不签名 macOS 直接拒绝启动。脚本用 ad-hoc 签名
   （`codesign --force --deep -s -`），够本机和内网分发；要做公证
   （让陌生人双击无警告）得申请 Apple Developer ID 并走 notarize。
4. **quarantine 属性**。从网络下载的 app 带 `com.apple.quarantine`，表现为
   "已损坏，无法打开"。对方执行一次 `xattr -cr adb_tool.app` 即可。

### mac 上找不到 adb

`find_adb()` 的兜底路径已补 mac 常见位置（优先级：配置 > PATH > `ANDROID_HOME` >
常见路径）：

```
~/Library/Android/sdk/platform-tools/adb   # Android Studio 默认
~/Android/Sdk/platform-tools/adb
/opt/homebrew/bin/adb                      # brew 安装
/opt/android-sdk/platform-tools/adb
/usr/local/bin/adb
```

`.app` 启动时读不到 shell 的 `PATH` / `ANDROID_HOME`（Dock 启动不经 shell），
所以 brew 装的 adb 常常搜不到——在设置里**手动指定 adb 绝对路径**最稳。

### 校验

`pyd_pack.py` 自动按平台切换：mac 上查 `dist/adb_tool.app/Contents/MacOS/`
下的 `core 8/8 .so`、`leaked py: none`、`web/` 与两个 JSON 在位、`pyobjc OK`
（Windows 则查 pythonnet 97 个 dll + ClrLoader.dll）。

改完 mac 分支，**在 Windows 上就能先预检**（16 项，不用等真 mac）：

```bash
python _mac_dryrun_check.py
```

它把 `sys.platform` 临时改成 `darwin`，然后：exec 一遍 `adb_tool.spec`（stub 掉
`Analysis/EXE/COLLECT/BUNDLE`，能抓出分支里的 NameError 和参数写错）、校验
`pyd_pack` 的产物路径常量、跑一遍 `setup_clang_env()` 看环境变量设对没。
