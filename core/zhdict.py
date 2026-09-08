# -*- coding: utf-8 -*-
"""静态中文字典：权限名 / 包名 -> 中文。

数据来源（项目根目录 config/ 下；打包后从 sys._MEIPASS 取）：
- config/android_perms_zh.json   368 条 Manifest.permission 权限中文化（dangerous/normal/signature）
- config/android_pkg_names.json  359 条常见包名 -> 中文应用名（另含 22 条前缀兜底规则）

设计原则：
- 两个文件都是**可选**的。缺失/损坏时查询一律返回 None，调用方回退到原名，
  不报错、不阻塞主流程（与 labels.py 的 aapt 降级策略一致）。
- 懒加载 + 进程内缓存，只在首次查询时读盘。
- 包名默认**只做精确匹配**：表里没有就显示原包名，不臆造。
  前缀兜底（com.tencent.* -> "腾讯应用"）由 use_prefix_fallback 控制，默认关闭，
  因为用户要求"没有则展示原名称"。
"""
import json
import os
import sys
import threading

PERMS_FILE = "android_perms_zh.json"
PKGS_FILE = "android_pkg_names.json"

_LOCK = threading.Lock()
_PERMS = None      # {UPPER_KEY: zh}
_PERM_GROUPS = None  # {UPPER_KEY: dangerous|normal|signature}
_PKGS = None       # {package: zh}
_PREFIX = None     # [(prefix, zh)]
_LOADED = False
_ERRORS = []


def _data_dir():
    """定位 JSON 所在目录：环境变量 > 打包临时目录 > 项目根 config/ > 项目根。

    config/ 子目录是 2026-09 起的规范位置；旧位置（项目根、_internal 根部）
    仍保留在查找链里，老部署/老产物不用改也能继续用。
    """
    def has(d):
        return d and os.path.isfile(os.path.join(d, PERMS_FILE))

    env = os.environ.get("ADB_TOOL_DATA_DIR")
    if has(env):
        return env
    base = getattr(sys, "_MEIPASS", None)
    if base:
        for cand in (base, os.path.join(base, "config")):
            if has(cand):
                return cand
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for cand in (os.path.join(here, "config"), here):
        if has(cand):
            return cand
    # 都没找到：返回规范位置，让 errors 里体现缺哪个文件
    return os.path.join(here, "config")


def _load():
    """懒加载。任何异常都吞掉，退化为空字典。"""
    global _LOADED, _PERMS, _PERM_GROUPS, _PKGS, _PREFIX
    if _LOADED:
        return
    with _LOCK:
        if _LOADED:
            return
        _PERMS, _PERM_GROUPS, _PKGS, _PREFIX = {}, {}, {}, []
        d = _data_dir()
        try:
            with open(os.path.join(d, PERMS_FILE), "r", encoding="utf-8") as f:
                data = json.load(f)
            for k, v in (data.get("all") or {}).items():
                _PERMS[str(k).strip().upper()] = v
            for grp, mapping in (data.get("by_group") or {}).items():
                for k in (mapping or {}):
                    _PERM_GROUPS[str(k).strip().upper()] = grp
        except Exception as e:  # noqa
            _ERRORS.append("%s: %s" % (PERMS_FILE, e))
        try:
            with open(os.path.join(d, PKGS_FILE), "r", encoding="utf-8") as f:
                data = json.load(f)
            for k, v in (data.get("all") or {}).items():
                _PKGS[str(k).strip()] = v
            for item in (data.get("prefix_fallback") or []):
                if isinstance(item, dict) and item.get("prefix") and item.get("zh"):
                    _PREFIX.append((item["prefix"], item["zh"]))
            # 长前缀优先，保证 com.tencent.mm. 先于 com.tencent. 命中
            _PREFIX.sort(key=lambda x: -len(x[0]))
        except Exception as e:  # noqa
            _ERRORS.append("%s: %s" % (PKGS_FILE, e))
        _LOADED = True


# ------------------------------------------------------------------ 查询
def perm_key(name):
    """归一化权限名：去掉 android.permission. 前缀，转大写。

    'android.permission.CAMERA' / 'CAMERA' / 'camera' -> 'CAMERA'
    """
    if not name:
        return ""
    s = str(name).strip()
    low = s.lower()
    if low.startswith("android.permission."):
        s = s[len("android.permission."):]
    return s.strip().upper()


def perm_zh(name):
    """权限中文名；没有映射返回 None（调用方回退到原名）。"""
    _load()
    return _PERMS.get(perm_key(name))


def perm_group(name):
    """权限分组：dangerous / normal / signature；未知返回 ''。"""
    _load()
    return _PERM_GROUPS.get(perm_key(name), "")


def pkg_zh(package, use_prefix_fallback=False):
    """包名中文名；没有映射返回 None（调用方回退到包名）。

    use_prefix_fallback=True 时，精确匹配失败再按前缀兜底
    （如 com.tencent.xxx -> "腾讯应用"），默认关闭。
    """
    _load()
    if not package:
        return None
    p = str(package).strip()
    if p in _PKGS:
        return _PKGS[p]
    if use_prefix_fallback:
        for prefix, zh in _PREFIX:
            if p.startswith(prefix):
                return zh
    return None


def pkg_batch(packages, use_prefix_fallback=False):
    """批量查包名中文，返回 {package: zh}（只含命中的）。"""
    _load()
    out = {}
    for p in packages or []:
        zh = pkg_zh(p, use_prefix_fallback)
        if zh:
            out[p] = zh
    return out


def perm_batch(names):
    """批量查权限中文，返回 {原名: zh}（只含命中的）。"""
    _load()
    out = {}
    for n in names or []:
        zh = _PERMS.get(perm_key(n))
        if zh:
            out[n] = zh
    return out


# ------------------------------------------------------------------ 元信息
def stats():
    """词典加载情况与统计，供设置页展示。"""
    _load()
    groups = {}
    for g in (_PERM_GROUPS or {}).values():
        groups[g] = groups.get(g, 0) + 1
    return {
        "dir": _data_dir(),
        "loaded": bool(_PERMS or _PKGS),
        "errors": list(_ERRORS),
        "perms": {"total": len(_PERMS or {}), "groups": groups},
        "pkgs": {"total": len(_PKGS or {}), "prefixFallback": len(_PREFIX or [])},
    }


def reload():
    """重新读盘（词库文件更新后调用）。"""
    global _LOADED, _ERRORS
    with _LOCK:
        _LOADED = False
        _ERRORS = []
