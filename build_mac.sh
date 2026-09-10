#!/bin/bash
# macOS 一键打包：venv -> Cython 编译 .so -> PyInstaller .app -> 签名 -> 分发包
#
# 用法：
#   ./build_mac.sh            # 产出 dist/device_bebugging_tool.app + zip
#   ./build_mac.sh --dmg      # 额外再打一个 dmg
#
# 前提：
#   1. 必须在 macOS 上跑（PyInstaller 不能交叉编译，Windows 打不出 mac 产物）
#   2. Xcode Command Line Tools：xcode-select --install
#   3. 架构跟随本机 Python：M 系列 -> arm64，Intel -> x86_64
#      给对方发包前先确认对方芯片，架构不匹配会直接 "无法打开"
set -e

cd "$(dirname "$0")"

PY="${PY:-python3}"
VENV=".venv_mac"
ARCH="$(uname -m)"
MAKE_DMG=0
[ "$1" = "--dmg" ] && MAKE_DMG=1

echo "==> 0/6 环境检查"
if [ "$(uname -s)" != "Darwin" ]; then
    echo "[错误] 本脚本只能在 macOS 上运行。" >&2
    echo "       PyInstaller 不支持交叉编译，Windows 无法产出 mac 可执行文件。" >&2
    echo "       替代方案：GitHub Actions 的 macos-latest runner（免费额度足够）。" >&2
    exit 1
fi
command -v "$PY" >/dev/null || { echo "[错误] 找不到 $PY"; exit 1; }
if ! xcode-select -p >/dev/null 2>&1; then
    echo "[错误] 缺少 Xcode Command Line Tools，请先执行：xcode-select --install"
    exit 1
fi
echo "    python : $($PY -V 2>&1)"
echo "    arch   : $ARCH  ($([ "$ARCH" = "arm64" ] && echo 'Apple Silicon' || echo 'Intel'))"

echo "==> 1/6 创建虚拟环境 $VENV"
if [ ! -x "$VENV/bin/python" ]; then
    "$PY" -m venv "$VENV"
fi
VPY="$VENV/bin/python"
"$VPY" -m pip install -q --upgrade pip setuptools wheel

echo "==> 2/6 安装依赖（pywebview 会自动带 pyobjc）"
"$VPY" -m pip install -q -r requirements.txt
if [ "${WITH_IOS:-0}" = "1" ]; then
    "$VPY" -m pip install -q "pymobiledevice3"
fi
"$VPY" -m pip install -q "pyinstaller>=6.0" "cython>=3.0"
# pillow：spec 里 BUNDLE 的 icon 用 config/icon.ico，PyInstaller 需 Pillow 转 .icns
"$VPY" -m pip install -q "pillow>=10.0"

echo "==> 3/6 Cython 编译 core/*.py -> .so（源码不入包）"
"$VPY" setup_pyd.py build_ext --inplace
ls -1 core/*.so 2>/dev/null | wc -l | xargs echo "    编译产物 .so 数量："

echo "==> 4/6 PyInstaller 打包（.app bundle）"
# 产物名（须与 adb_tool.spec 的 APP / pyd_pack.py 的 APP 保持一致）
NAME="device_bebugging_tool"
APP="dist/$NAME.app"
# --no-zip：zip 留到第 6 步 ad-hoc 签名之后再打，否则 zip 里是未签名产物
"$VPY" pyd_pack.py --no-zip

echo "==> 4.5/6 内置 adb（目标机免装 Android SDK / brew adb）"
# 放 Contents/MacOS/public_settings/adb/ —— core/adb.py 的 portable_adb_dirs()
# 会在可执行文件同目录找到它（来源标记 portable）。mac 平台 tools 的 adb 是
# 自包含单文件，不带 dylib 依赖，直接拷贝即可。
ADB_DST="$APP/Contents/MacOS/public_settings/adb/adb"
if [ -x "$ADB_DST" ]; then
    echo "    已存在，跳过"
elif [ "${SKIP_BUNDLED_ADB:-0}" = "1" ]; then
    echo "    [warn] SKIP_BUNDLED_ADB=1，跳过内置（目标机需自备 adb）"
else
    ZIP="/tmp/platform-tools-darwin.zip"
    curl -fL --retry 3 -o "$ZIP" \
        "https://dl.google.com/android/repository/platform-tools-latest-darwin.zip" \
        || { echo "[错误] platform-tools 下载失败（SKIP_BUNDLED_ADB=1 可跳过内置）"; exit 1; }
    rm -rf /tmp/platform-tools
    unzip -q -o "$ZIP" -d /tmp
    mkdir -p "$(dirname "$ADB_DST")"
    cp /tmp/platform-tools/adb "$ADB_DST"
    chmod +x "$ADB_DST"
    echo "    adb -> $ADB_DST"
    "$ADB_DST" version | sed 's/^/    /'
fi

echo "==> 5/6 解除隔离属性 + ad-hoc 签名"
# 本地产物也会被标记 quarantine，导致「已损坏，无法打开」
xattr -cr "$APP" 2>/dev/null || true
# ad-hoc 签名（-s -）：不签名 macOS 会直接拒绝启动（代码签名无效）
codesign --force --deep --timestamp=none -s - "$APP" 2>/dev/null \
    && echo "    codesign: ad-hoc OK" \
    || echo "    [warn] codesign 失败（不影响本机运行，分发给他人会被告警）"

echo "==> 6/6 生成分发包"
# 版本号与 core/api.py 的 APP_VERSION 同源（pyd_pack 跑完源码已恢复，可直接读）
VER=$("$VPY" -c "import re;print(re.search(r'APP_VERSION\s*=\s*\"([^\"]+)\"', open('core/api.py', encoding='utf-8').read()).group(1))")
echo "    版本: v$VER（取自 core/api.py APP_VERSION）"
rm -f "dist/${NAME}_v${VER}_macos_${ARCH}.zip"
# zip -y 保留符号链接：.app 内部 Frameworks 依赖软链，丢了会起不来
(cd dist && zip -qry "${NAME}_v${VER}_macos_${ARCH}.zip" "$NAME.app")
echo "    dist/${NAME}_v${VER}_macos_${ARCH}.zip  ($(du -sh "$APP" | cut -f1) -> $(du -h "dist/${NAME}_v${VER}_macos_${ARCH}.zip" | cut -f1))"

if [ "$MAKE_DMG" = "1" ]; then
    DMG="dist/${NAME}_v${VER}_macos_${ARCH}.dmg"
    rm -f "$DMG"
    hdiutil create -volname "Device Debugging Tool" -srcfolder "$APP" -ov -format UDZO "$DMG" >/dev/null
    echo "    $DMG"
fi

echo
echo "完成。运行：open $APP"
echo "分发：把 dist/${NAME}_v${VER}_macos_${ARCH}.zip 发给同芯片的 mac；"
echo "      对方解压后若提示「已损坏/无法打开」，执行一次："
echo "        xattr -cr $NAME.app"
echo "      若提示「无法验证开发者」：右键 -> 打开（仅首次）"
