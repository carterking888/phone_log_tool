/* 根状态：路由 / 设备上下文 / 轮询 / 任务监听 / 全局提示 */
(function () {
  "use strict";

  // 日志轮询频率：有新日志时 400ms；连续空闲或不在日志页时降到 2s。
  // 原因：每次 js_api 调用 pywebview 都会新建后台线程回传结果，并触发跨线程 COM 噪音，
  // 空闲时高频轮询没有收益，只会放大噪音、白白建线程。
  var POLL_MS_ACTIVE = 400;
  var POLL_MS_IDLE = 2000;
  var IDLE_ROUNDS = 5;

  var state = {
    route: "devices",
    navs: [
      { key: "devices", label: "设备" },
      { key: "logs", label: "日志查看" },
      { key: "apps", label: "应用包管理" },
      { key: "files", label: "文件管理" }
    ],
    currentSerial: "",
    env: { appVersion: "", adbPath: "", adbVersion: "", adbSource: "", demo: false, reason: "" },
    actionLogs: [],

    devices: window.devicesComponent(),
    logs: window.logsComponent(),
    apps: window.appsComponent(),
    files: window.filesComponent(),

    toastState: { show: false, text: "", cls: "ok" },
    settings: { open: false, adbPath: "", detected: "", cocosInspector: "" },
    // 中文词库统计。初值带完整结构，避免 v-if/effect 竞争时解引用 undefined（见坑 9）
    zhStats: {
      loaded: false, dir: "", errors: [],
      perms: { total: 0, groups: {} },
      pkgs: { total: 0, prefixFallback: 0 }
    },
    _toastTimer: null,
    _taskTimers: {},

    // adb 来源中文（"随包便携版" / "系统 PATH" ...），模板不写复杂表达式
    get adbSourceText() {
      return Util.adbSourceText(this.env.adbSource);
    },

    // 当前选中的是不是 iOS 设备（模板里到处要用，统一在这里派生）
    get isIos() {
      var d = this.devices.detail;
      if (d && d.platform) return d.platform === "ios";
      var list = this.devices.list || [];
      for (var i = 0; i < list.length; i++) {
        if (list[i].serial === this.currentSerial) return list[i].platform === "ios";
      }
      return false;
    },

    // ---------------------------------------------------------- 初始化
    init: function () {
      var self = this;
      window.AdbApp = this; // 响应式 proxy（供跨组件读取，见坑 0.2）

      var boot = function () {
        self.devices.refresh(self).then(function () {
          self.onRoute(self.route);
        });
        self.loadActionLogs();
        // 预读配置：日志页 JS 端口卡片/启动参数都直接读 settings.cocosInspector
        Util.call("get_config").then(function (cfg) {
          if (cfg) {
            self.settings.cocosInspector = cfg.cocosInspector || "";
            self.settings.adbPath = cfg.adbPath || "";
            self.settings.detected = cfg.detectedAdbPath || "";
          }
        }).catch(function () {});
      };

      // 坑 8：必须等 pywebview 桥就绪；浏览器预览模式 3s 后兜底放行
      var started = false;
      var startOnce = function () {
        if (started) return;
        started = true;
        if (!Util.hasApi()) {
          self.toast("未检测到桌面桥接（浏览器预览模式），数据不可用", "warn");
          return;
        }
        boot();
      };
      if (Util.hasApi()) {
        startOnce();
      } else {
        window.addEventListener("pywebviewready", startOnce);
        window.setTimeout(startOnce, 3000);
      }

      // 日志轮询（自适应降频：见文件头 POLL_MS_ACTIVE 注释）
      var idle = 0;
      var pollLoop = function () {
        Promise.resolve(self.logs.poll(self)).then(function (got) {
          idle = got ? 0 : idle + 1;
          var delay =
            self.route !== "logs" || idle >= IDLE_ROUNDS ? POLL_MS_IDLE : POLL_MS_ACTIVE;
          window.setTimeout(pollLoop, delay);
        });
      };
      window.setTimeout(pollLoop, POLL_MS_ACTIVE);

      // 路由（hash）
      var onHash = function () {
        self.onHashChange();
      };
      window.addEventListener("hashchange", onHash);
      var h = (window.location.hash || "").replace(/^#\/?/, "");
      if (h && self.navs.some(function (n) { return n.key === h; })) {
        self.route = h;
      }
    },

    onHashChange: function () {
      var h = (window.location.hash || "").replace(/^#\/?/, "");
      var self = this;
      if (h && self.navs.some(function (n) { return n.key === h; })) {
        self.route = h;
        self.onRoute(h);
      }
    },

    go: function (key) {
      this.route = key;
      window.location.hash = "#/" + key;
      this.onRoute(key);
    },

    onRoute: function (key) {
      var self = this;
      if (key === "apps" && !this.apps.packages.length) {
        this.apps.reload(this);
      } else if (key === "files") {
        if (!this.files.entries.length) this.files.reload(this);
        if (!this.files.storage.ok) this.files.loadStorage(this);
      } else if (key === "logs") {
        if (!this.logs.running) this.logs.start(this);
      }
    },

    // ---------------------------------------------------------- 设备切换
    onDeviceChange: function () {
      var self = this;
      var serial = this.currentSerial;
      this.devices.current = serial;
      Util.call("select_device", serial)
        .then(function () {
          // 新设备：清掉上一台的日志筛选条件（级别/PID/标签/关键字/包名/选中行）
          self.logs.resetFilters();
          return self.devices.loadDetail(self);
        })
        .then(function () {
          if (self.logs.running) self.logs.restart(self);
          self.apps.packages = [];
          self.files.entries = [];
          self.files.resetStorage();
          if (self.route === "apps") self.apps.reload(self);
          if (self.route === "files") {
            self.files.reload(self);
            self.files.loadStorage(self);
          }
          if (self.isIos) self.files.loadAppList(self);
        })
        .catch(function (e) {
          self.toast("切换设备失败：" + e.message, "bad");
        });
    },

    // ---------------------------------------------------------- 任务监听
    watchTask: function (taskId, apply, done) {
      var self = this;
      var timer = window.setInterval(function () {
        Util.call("get_task", taskId)
          .then(function (t) {
            if (apply && t) apply(t);
            if (!t || t.status !== "running") {
              window.clearInterval(timer);
              delete self._taskTimers[taskId];
              if (done) done(t || { status: "failed", error: "任务不存在" });
            }
          })
          .catch(function (e) {
            window.clearInterval(timer);
            delete self._taskTimers[taskId];
            if (done) done({ status: "failed", error: e.message });
          });
      }, 300);
      self._taskTimers[taskId] = timer;
      return timer;
    },

    // ---------------------------------------------------------- 设置
    openSettings: function () {
      var self = this;
      this.settings.open = true;
      this.apps.loadLabelStatus();
      Util.call("get_zh_stats")
        .then(function (s) { if (s) self.zhStats = s; })
        .catch(function () {});
      return Util.call("get_config")
        .then(function (cfg) {
          self.settings.adbPath = (cfg && cfg.adbPath) || "";
          self.settings.detected = (cfg && cfg.detectedAdbPath) || "";
          self.settings.cocosInspector = (cfg && cfg.cocosInspector) || "";
        })
        .catch(function (e) {
          self.toast("读取配置失败：" + e.message, "bad");
        });
    },
    saveSettings: function () {
      var self = this;
      return Util.call("set_config", {
        adbPath: this.settings.adbPath,
        cocosInspector: String(this.settings.cocosInspector || "").trim()
      })
        .then(function (r) {
          self.settings.open = false;
          return self.devices.refresh(self);
        })
        .then(function () {
          self.toast("已保存并重新检测环境", "ok");
        })
        .catch(function (e) {
          self.toast("保存失败：" + e.message, "bad");
        });
    },

    loadActionLogs: function () {
      var self = this;
      return Util.call("get_action_logs", 50)
        .then(function (r) {
          self.actionLogs = (r && r.items) || [];
        })
        .catch(function () {});
    },

    // --------------------------------------- 原生拖放（core/app.py 拦截后回传）
    // WebView2 不给 JS 真实磁盘路径，Python 侧从导航/下载事件截出路径后调这里。
    onNativeDrop: function (path) {
      if (!path) return;
      var apps = this.apps;
      if (!apps) return;
      apps.installOpen = true;
      apps.installFile = path;
      apps.installTask = null;
      // 目标设备信息可能过期（手机是后插的），顺手刷新
      if (this.devices && this.devices.refresh) this.devices.refresh(this);
      this.toast("已接收拖放文件", "ok");
    },
    onNativeDropRejected: function (name) {
      this.toast("已忽略 " + name + "：仅支持 .apk / .xapk / .apks / .ipa", "warn");
    },

    // ---------------------------------------------------------- 全局提示
    toast: function (text, cls) {
      var self = this;
      this.toastState = { show: true, text: text, cls: cls || "ok" };
      if (this._toastTimer) window.clearTimeout(this._toastTimer);
      this._toastTimer = window.setTimeout(function () {
        self.toastState = { show: false, text: "", cls: "ok" };
      }, 3200);
    },

    refreshEnv: function () {
      var self = this;
      return this.devices.refresh(this);
    },

    /* 显式进入/退出演示模式（不再自动回退假数据；接上真机自动退出） */
    setDemo: function (on) {
      var self = this;
      return Util.call(on ? "enable_demo" : "disable_demo")
        .then(function (env) {
          self.env = Object.assign({}, self.env, env);
          return self.devices.refresh(self);
        })
        .catch(function (e) {
          self.toast("演示模式切换失败：" + (e && e.message ? e.message : e), "bad");
        });
    }
  };

  window.PetiteVue.createApp(state).mount();
})();
