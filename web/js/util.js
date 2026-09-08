/* 通用工具：格式化 / 转义 / 类型判定 / pywebview 桥调用封装 */
(function () {
  "use strict";

  function fmtBytes(n) {
    n = Number(n || 0);
    if (n <= 0) return "0 B";
    var units = ["B", "KB", "MB", "GB", "TB"];
    var i = Math.min(units.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
    var v = n / Math.pow(1024, i);
    return (i === 0 ? v : v.toFixed(v >= 100 ? 0 : 1)) + " " + units[i];
  }

  function fmtNum(n) {
    return String(n || 0).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  /* 关键字高亮：mode = include | exclude | regex；非正则模式支持空格分隔多关键字 */
  function hl(text, kw, mode) {
    var s = esc(text);
    kw = String(kw || "").trim();
    if (!kw) return s;
    try {
      if (mode === "regex") {
        var re = new RegExp("(" + kw + ")", "gi");
        return s.replace(re, '<mark class="hl">$1</mark>');
      }
      var parts = kw.split(/\s+/).filter(Boolean).map(function (k) {
        return esc(k).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      });
      if (!parts.length) return s;
      return s.replace(new RegExp("(" + parts.join("|") + ")", "gi"), '<mark class="hl">$1</mark>');
    } catch (e) {
      return s;
    }
  }

  var FILE_TYPES = [
    { key: "folder", label: "文件夹", ext: [], cls: "ft-folder", isDir: true },
    { key: "log", label: "日志", ext: [".log", ".txt.log", ".logcat"], cls: "ft-log" },
    { key: "text", label: "文本", ext: [".txt", ".json", ".xml", ".csv", ".ini", ".prop"], cls: "ft-text" },
    { key: "image", label: "图片", ext: [".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"], cls: "ft-image" },
    { key: "zip", label: "压缩包", ext: [".zip", ".apk", ".xapk", ".apks", ".tar", ".gz", ".7z", ".rar"], cls: "ft-zip" },
    { key: "video", label: "视频", ext: [".mp4", ".mov", ".mkv", ".avi"], cls: "ft-video" },
    { key: "audio", label: "音频", ext: [".mp3", ".wav", ".flac", ".aac"], cls: "ft-audio" }
  ];

  function fileType(entry) {
    if (entry.isDir) return FILE_TYPES[0];
    var n = String(entry.name || "").toLowerCase();
    for (var i = 1; i < FILE_TYPES.length; i++) {
      var t = FILE_TYPES[i];
      for (var j = 0; j < t.ext.length; j++) {
        if (n.slice(-t.ext[j].length) === t.ext[j]) return t;
      }
    }
    return { key: "other", label: "文件", ext: [], cls: "ft-other" };
  }

  /* 类型图标：内联 SVG，随 CSS 变量着色 */
  var SVG = {
    folder: '<svg viewBox="0 0 16 16" width="16" height="16"><path fill="currentColor" d="M1.6 3.2c0-.4.3-.7.7-.7h3.1l1.3 1.5h7c.4 0 .7.3.7.7v7.6c0 .4-.3.7-.7.7H2.3c-.4 0-.7-.3-.7-.7V3.2z"/></svg>',
    log: '<svg viewBox="0 0 16 16" width="16" height="16"><path fill="currentColor" d="M3 1.4h6.4L13 5v9.6c0 .3-.3.6-.6.6H3.6c-.3 0-.6-.3-.6-.6V2c0-.3.3-.6.6-.6zM9 1.8V5h3.2L9 1.8zM5 7.4h6v1.2H5V7.4zm0 2.6h6v1.2H5V10zm0 2.6h4v1.2H5v-1.2z"/></svg>',
    text: '<svg viewBox="0 0 16 16" width="16" height="16"><path fill="currentColor" d="M3.4 1.4h9.2c.4 0 .7.3.7.7v11.8c0 .4-.3.7-.7.7H3.4c-.4 0-.7-.3-.7-.7V2.1c0-.4.3-.7.7-.7zm1 2.4v1.1h7.2V3.8H4.4zm0 2.8v1.1h7.2V6.6H4.4zm0 2.8v1.1h5V9.4H4.4z"/></svg>',
    image: '<svg viewBox="0 0 16 16" width="16" height="16"><path fill="currentColor" d="M2.4 2h11.2c.3 0 .6.3.6.6v10.8c0 .3-.3.6-.6.6H2.4a.6.6 0 01-.6-.6V2.6c0-.3.3-.6.6-.6zm.8 9.2h9.6L10 7.4l-2.4 3-1.6-1.8L3.2 11.2zM5.6 5.2a1.1 1.1 0 100 2.2 1.1 1.1 0 000-2.2z"/></svg>',
    zip: '<svg viewBox="0 0 16 16" width="16" height="16"><path fill="currentColor" d="M3.4 1.4h9.2c.4 0 .7.3.7.7v11.8c0 .4-.3.7-.7.7H3.4c-.4 0-.7-.3-.7-.7V2.1c0-.4.3-.7.7-.7zM7 2.6v1.6h2V2.6H7zm0 2.4v1.6h2V5H7zm0 2.4v1.6h2V7.4H7zm0 2.4v1.6h2V9.8H7z"/></svg>',
    video: '<svg viewBox="0 0 16 16" width="16" height="16"><path fill="currentColor" d="M2.6 2.4h8c.4 0 .7.3.7.7v2.1l2.4-1.6c.3-.2.7 0 .7.4v6c0 .4-.4.6-.7.4l-2.4-1.6v2.1c0 .4-.3.7-.7.7h-8c-.4 0-.7-.3-.7-.7V3.1c0-.4.3-.7.7-.7z"/></svg>',
    audio: '<svg viewBox="0 0 16 16" width="16" height="16"><path fill="currentColor" d="M12 1.6v9.9a2.6 2.6 0 11-1.4-2.3V4.9L7 5.9v7a2.6 2.6 0 11-1.4-2.3V4.2L12 1.6z"/></svg>',
    other: '<svg viewBox="0 0 16 16" width="16" height="16"><path fill="currentColor" d="M3.4 1.4h9.2c.4 0 .7.3.7.7v11.8c0 .4-.3.7-.7.7H3.4c-.4 0-.7-.3-.7-.7V2.1c0-.4.3-.7.7-.7zm1.7 4.2v1.2h5.8V5.6H5.1zm0 2.6v1.2h5.8V8.2H5.1z"/></svg>'
  };

  function iconSvg(key) {
    return SVG[key] || SVG.other;
  }

  function enrichEntry(e) {
    var t = fileType(e);
    return Object.assign({}, e, {
      typeKey: t.key,
      typeLabel: t.label,
      typeCls: t.cls,
      iconSvg: iconSvg(t.key),
      sizeText: e.isDir ? "—" : fmtBytes(e.size),
      selected: false
    });
  }

  /* adb 来源 -> 中文（来源由后端 env.adbSource 给出） */
  var ADB_SOURCE_TEXT = {
    config: "手动指定",
    path: "系统 PATH",
    sdk: "Android SDK",
    portable: "随包便携版",
    common: "常见路径",
    fallback: "未找到"
  };
  function adbSourceText(src) {
    return ADB_SOURCE_TEXT[String(src || "")] || "";
  }

  /* 应用图标：无真实图标时用首字 + 稳定色 */
  var ICON_COLORS = ["#1f6feb", "#238636", "#8957e5", "#d29922", "#da3633", "#1f7a8c", "#bf4b8a"];
  function iconMeta(name) {
    var s = String(name || "?");
    var ch = s.trim().charAt(0) || "?";
    var sum = 0;
    for (var i = 0; i < s.length; i++) sum = (sum + s.charCodeAt(i)) % 997;
    return { text: ch, color: ICON_COLORS[sum % ICON_COLORS.length] };
  }

  function enrichApp(a) {
    var ic = iconMeta(a.label || a.packageName);
    var typeMap = { user: "用户", system: "系统", third: "三方", disabled: "已禁用" };
    return Object.assign({}, a, {
      iconText: ic.text,
      iconColor: ic.color,
      typeLabel: typeMap[a.type] || a.type || "—",
      typeCls: "ty-" + (a.type || "none"),
      sizeText: a.sizeBytes ? fmtBytes(a.sizeBytes) : "—",
      statusText: a.running ? "运行中" : "—",
      statusCls: a.running ? "run" : "idle",
      displayName: a.label || a.packageName,
      selected: false
    });
  }

  /* pywebview 桥：**必须 await**，未就绪时抛错由调用方处理（不静默回退） */
  function hasApi() {
    return !!(window.pywebview && window.pywebview.api);
  }

  function call(name) {
    var api = window.pywebview && window.pywebview.api;
    if (!api) return Promise.reject(new Error("pywebview bridge not ready"));
    if (typeof api[name] !== "function") {
      return Promise.reject(new Error("api not found: " + name));
    }
    var args = Array.prototype.slice.call(arguments, 1);
    try {
      return Promise.resolve(api[name].apply(api, args));
    } catch (e) {
      return Promise.reject(e);
    }
  }

  function copyText(text) {
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text);
        return true;
      }
    } catch (e) {}
    try {
      var ta = document.createElement("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
      return true;
    } catch (e2) {
      return false;
    }
  }

  window.Util = {
    fmtBytes: fmtBytes,
    fmtNum: fmtNum,
    esc: esc,
    hl: hl,
    fileType: fileType,
    enrichEntry: enrichEntry,
    enrichApp: enrichApp,
    iconMeta: iconMeta,
    adbSourceText: adbSourceText,
    hasApi: hasApi,
    call: call,
    copyText: copyText
  };
})();
