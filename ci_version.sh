#!/usr/bin/env bash
# 版本号解析脚本（CI 与本地通用）
#
# 作用：算出「本次构建」要用的版本号，并把 core/api.py 的 APP_VERSION 改成它。
#   - 应用内显示的版本、产物 zip 的文件名、GitHub tag、Release 标题 —— 全部同源
#   - 只改工作区文件，不回写仓库；仓库里的 APP_VERSION 只是「一个 tag 都没有」时的兜底基准
#
# 规则（按优先级）：
#   1) 传了参数           -> 用参数（CI 的 version job 先算一次，其余 job 复用它，
#                            保证同一次运行内 mac / win / publish 看到的版本完全一致）
#   2) 本次是 push tag    -> 用 tag 本身（正式发版不递增）
#   3) 存在 v* tag        -> 取最新 tag，patch +1    例：v2.7.0 -> 2.7.1
#   4) 一个 tag 都没有    -> core/api.py 现有 APP_VERSION，patch +1
#
# 用法：
#   VER=$(bash ci_version.sh)        # 自动递增
#   VER=$(bash ci_version.sh 2.8.0)  # 指定版本
set -euo pipefail

EXPLICIT="${1:-}"

[ -f core/api.py ] || { echo "[错误] 找不到 core/api.py，请在项目根目录执行" >&2; exit 1; }

read_app_version() {
  sed -nE 's/^APP_VERSION[[:space:]]*=[[:space:]]*"([^"]+)".*/\1/p' core/api.py | head -1
}
is_semver() { echo "$1" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+$'; }
# 结果校验放宽一些：允许 2.8.0-rc1 / 2.8.0+build 这类正式 tag 形态
is_version() { echo "$1" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+([.+-][0-9A-Za-z.+-]+)?$'; }

BASE=""
if [ -n "$EXPLICIT" ]; then
  VER="$EXPLICIT"
  echo "  版本来源: 外部指定（同一次运行内复用）" >&2
elif [ "${GITHUB_REF_TYPE:-}" = "tag" ] && [ -n "${GITHUB_REF_NAME:-}" ]; then
  VER="${GITHUB_REF_NAME#v}"
  echo "  版本来源: 正式发版 tag $GITHUB_REF_NAME（不递增）" >&2
else
  LATEST=$(git tag -l 'v[0-9]*' --sort=-v:refname 2>/dev/null | head -1 || true)
  BASE="${LATEST#v}"
  BASE="${BASE%%-*}"                       # 剥离历史遗留的 -b<run号> 后缀
  if ! is_semver "$BASE"; then
    BASE="$(read_app_version)"             # 没有 tag / tag 形态异常 -> 回落仓库里的值
    [ -n "$BASE" ] || BASE="0.0.0"
  fi
  is_semver "$BASE" || BASE="0.0.0"
  VER=$(echo "$BASE" | awk -F. '{printf "%s.%s.%d", $1, $2, $3 + 1}')
  echo "  版本来源: 最新 tag ${LATEST:-（无，用 core/api.py 兜底）} -> patch +1" >&2
fi

is_version "$VER" || { echo "[错误] 版本号形态异常：$VER（应形如 2.7.1）" >&2; exit 1; }

# 写回 core/api.py。用「临时文件 + mv」而不是 sed -i：macOS 的 BSD sed 与
# Linux 的 GNU sed 对 -i 的参数要求不同，这种写法两边都通用。
sed -E "s/^(APP_VERSION[[:space:]]*=[[:space:]]*)\"[^\"]+\"/\1\"${VER}\"/" core/api.py > core/api.py.versiontmp
mv core/api.py.versiontmp core/api.py
grep -qE "^APP_VERSION[[:space:]]*=[[:space:]]*\"${VER}\"" core/api.py \
  || { echo "[错误] 改写 core/api.py 的 APP_VERSION 失败" >&2; exit 1; }

echo "  本次构建版本: $VER" >&2
echo "$VER"
