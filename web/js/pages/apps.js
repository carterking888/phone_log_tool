/* 模块三：应用包管理 */
window.appsComponent = function () {
  return {
    loading: false,
    loadError: "",
    packages: [],
    typeFilter: "all",
    keyword: "",
    sortBy: "name",
    onlyRunning: false,
    page: 1,
    pageSize: 20,

    detailOpen: false,
    detailLoading: false,
    detail: null,

    uninstallOpen: false,
    uninstallTarget: null,
    uninstallKeep: false,
    uninstallTask: null,

    installOpen: false,
    installFile: "",
    installOpts: { r: true, d: true, g: true, t: false },
    installTask: null,

    labelStatus: { available: false, tool: "", hint: "" },
    labelsLoading: false,
    _labelTimer: null,

    types: [
      { key: "all", label: "全部应用" },
      { key: "user", label: "用户应用" },
      { key: "system", label: "系统应用" },
      { key: "third", label: "三方应用" },
      { key: "disabled", label: "已禁用应用" }
    ],

    // ---------------------------------------------------------- 派生
    get typeCounts() {
      var c = { all: this.packages.length, user: 0, system: 0, third: 0, disabled: 0 };
      for (var i = 0; i < this.packages.length; i++) {
        var t = this.packages[i].type;
        if (c[t] != null) c[t] += 1;
        if (t !== "system") c.user += 1;
      }
      return c;
    },
    get typeItems() {
      var c = this.typeCounts;
      return this.types.map(function (t) {
        return { key: t.key, label: t.label, count: c[t.key] };
      });
    },
    get filtered() {
      var kw = String(this.keyword || "").toLowerCase().trim();
      var type = this.typeFilter;
      var only = this.onlyRunning;
      var out = this.packages.filter(function (p) {
        if (only && !p.running) return false;
        if (type === "all") {
          // all
        } else if (type === "user") {
          if (p.type === "system") return false;
        } else if (type === "disabled") {
          if (p.type !== "disabled") return false;
        } else if (p.type !== type) {
          return false;
        }
        if (!kw) return true;
        return (p.label || "").toLowerCase().indexOf(kw) >= 0 ||
          (p.packageName || "").toLowerCase().indexOf(kw) >= 0;
      });
      var by = this.sortBy;
      out.sort(function (a, b) {
        if (by === "size") return (b.sizeBytes || 0) - (a.sizeBytes || 0);
        if (by === "package") return (a.packageName || "").localeCompare(b.packageName || "");
        return (a.label || a.packageName || "").localeCompare(b.label || b.packageName || "");
      });
      return out;
    },
    get total() {
      return this.filtered.length;
    },
    get totalPages() {
      return Math.max(1, Math.ceil(this.total / this.pageSize));
    },
    get pageNumbers() {
      var out = [];
      var last = this.totalPages;
      var cur = this.page;
      var start = Math.max(1, cur - 2);
      var end = Math.min(last, start + 4);
      start = Math.max(1, end - 4);
      for (var i = start; i <= end; i++) out.push(i);
      return out;
    },
    get paged() {
      var s = (this.page - 1) * this.pageSize;
      return this.filtered.slice(s, s + this.pageSize);
    },
    get rangeText() {
      if (!this.total) return "共 0 个应用";
      var s = (this.page - 1) * this.pageSize + 1;
      var e = Math.min(this.total, s + this.pageSize - 1);
      return "显示 " + s + "-" + e + " 共 " + this.total + " 个应用";
    },
    get totalSize() {
      var s = 0;
      for (var i = 0; i < this.packages.length; i++) s += this.packages[i].sizeBytes || 0;
      return s;
    },
    get totalSizeText() {
      return this.totalSize ? Util.fmtBytes(this.totalSize) : "—";
    },
    get runningCount() {
      return this.packages.filter(function (p) { return p.running; }).length;
    },
    get topApps() {
      // 只统计有体积数据的应用：列表接口不再为每个应用现算体积（太慢），
      // 没数据时交给"暂无大小数据"空态，而不是画一排 0 B 的假条形
      var arr = this.packages.filter(function (a) { return (a.sizeBytes || 0) > 0; })
        .sort(function (a, b) {
          return (b.sizeBytes || 0) - (a.sizeBytes || 0);
        }).slice(0, 3);
      if (!arr.length) return [];
      var max = arr[0].sizeBytes || 1;
      return arr.map(function (a) {
        return {
          name: a.label || a.packageName,
          text: Util.fmtBytes(a.sizeBytes),
          width: Math.max(4, Math.round((a.sizeBytes || 0) / max * 100))
        };
      });
    },
    get pendingLabelPkgs() {
      return this.paged.filter(function (p) { return !p.label; })
        .map(function (p) { return { package: p.packageName, codePath: p.codePath || "" }; });
    },
    get labelHint() {
      if (this.labelsLoading) return "正在解析应用名…";
      if (!this.labelStatus.available) return this.labelStatus.hint || "";
      return "";
    },
    get detailFields() {
      var d = this.detail;
      if (!d) return [];
      // iOS 没有 UID / targetSdk 概念，换成 Bundle 路径等有意义的信息
      if (d.permissionsUnsupported) {
        return [
          { label: "类型", value: { user: "用户应用", system: "系统应用" }[d.type] || d.type || "—" },
          { label: "版本", value: (d.versionName || "—") + (d.versionCode ? " (" + d.versionCode + ")" : "") },
          { label: "Bundle 路径", value: d.codePath || "—", wide: true }
        ];
      }
      return [
        { label: "PID", value: d.running ? d.pid : "—" },
        { label: "UID", value: d.uid || "—" },
        { label: "类型", value: { user: "用户应用", system: "系统应用", third: "三方应用" }[d.type] || d.type || "—" },
        { label: "targetSdk", value: d.targetSdk || "—" },
        { label: "版本", value: (d.versionName || "—") + (d.versionCode ? " (" + d.versionCode + ")" : "") }
      ];
    },
    /* iOS 非越狱读不到权限列表（需要解析 entitlements），如实说明而不是留空白让人以为是加载失败 */
    get permsUnsupported() {
      return !!(this.detail && this.detail.permissionsUnsupported);
    },

    // ---------------------------------------------- 安装弹窗（按平台切换文案）
    get isIos() {
      return !!(window.AdbApp && window.AdbApp.isIos);
    },
    get installTitle() {
      return this.isIos ? "安装 IPA" : "安装 APK";
    },
    get installDropText() {
      return this.isIos ? "将 IPA 文件拖放到此处" : "将 APK 文件拖放到此处";
    },
    get installAccept() {
      return this.isIos
        ? "支持 .ipa（未签名 / 签名不匹配的安装包会被系统拒绝）"
        : "支持 .apk / .xapk / .apks，单个文件最大 2GB";
    },
    get targetText() {
      var app = window.AdbApp;
      var d = app && app.devices ? app.devices.detail : null;
      if (d) {
        if (d.platform === "ios") return d.model + " · iOS " + (d.iosVersion || "—");
        return d.model + " · " + (d.displayId || "") + " · Android " + (d.androidVersion || "—");
      }
      // 详情未加载时从设备列表兜底；当前序列号匹配不上（未选/刚插上/列表过期）
      // 就取第一台在线设备——安装弹窗不应显示 "—"
      var list = (app && app.devices ? app.devices.list : []) || [];
      var hit = null;
      for (var i = 0; i < list.length; i++) {
        var it = list[i];
        if (app && it.serial === app.currentSerial) { hit = it; break; }
        if (!hit && it.state === "device") hit = it;
      }
      if (hit) {
        if (hit.platform === "ios") return (hit.model || "iPhone") + " · iOS " + (hit.iosVersion || "—");
        return (hit.model || "Android") + " · " + (hit.displayId || hit.serial);
      }
      return "—";
    },
    get detailStorages() {
      var d = this.detail;
      if (!d) return [];
      return [
        { label: "应用大小", desc: "代码与资源", text: d.appSize ? Util.fmtBytes(d.appSize) : "—" },
        { label: "用户数据", desc: "数据库与文件", text: d.dataSize ? Util.fmtBytes(d.dataSize) : "—" },
        { label: "缓存", desc: "临时文件", text: d.cacheSize ? Util.fmtBytes(d.cacheSize) : "—" }
      ];
    },
    /* 权限：有中文映射显示中文（原名放 title，调试时仍需原始权限名），没有则显示原名 */
    get detailPerms() {
      var d = this.detail;
      if (!d || !d.permissions) return [];
      return d.permissions.map(function (p) {
        return {
          name: p.label || p.name,
          raw: p.name,
          zh: !!p.label,
          group: p.group || "",
          tip: p.label ? p.name + (p.group ? " · " + p.group : "") : (p.group || p.name),
          state: p.granted ? "已授予" : "未授予",
          cls: p.granted ? "ok" : "no"
        };
      });
    },
    get dangerPermCount() {
      return this.detailPerms.filter(function (p) { return p.group === "dangerous"; }).length;
    },

    // ---------------------------------------------------------- 行为
    reload: function (app) {
      var self = this;
      if (!app.currentSerial) {
        app.toast("请先选择设备", "warn");
        return Promise.resolve();
      }
      this.loading = true;
      this.loadError = "";
      // iOS 的设备只查一次：InstallationProxy 每次 lookup 都要新建一个 lockdown
      // 会话，而返回的字典里 application_type=Any 已经覆盖全部应用且自带 system/user
      // 类型 —— 拆成 all/system/disabled 三次调用只是把等待时间翻三倍
      // （之前"同步包列表"转很久最后还失败，列表就一直是旧的）。
      var calls = app.isIos
        ? [Util.call("list_packages", app.currentSerial, "all")]
        : [
          Util.call("list_packages", null, "all"),
          Util.call("list_packages", null, "system"),
          Util.call("list_packages", null, "disabled")
        ];
      return Promise.all(calls.map(function (p) {
        // 单个 kind 失败不拖垮整次刷新：Promise.all 会直接 reject，
        // 旧列表原样留在界面上，表现就是"点了同步没反应"
        return p.then(function (r) { return r; }, function (e) {
          self.loadError = (e && e.message) ? e.message : String(e);
          return null;
        });
      }))
        .then(function (res) {
          var all = (res[0] && res[0].packages) || [];
          // 一条都没拿到（设备掉线/未信任）：保留旧列表，报错由下面统一提示
          if (!all.length && self.loadError) return;
          var sysSet = {};
          var disSet = {};
          if (!app.isIos) {
            ((res[1] && res[1].packages) || []).forEach(function (p) { sysSet[p.packageName] = 1; });
            ((res[2] && res[2].packages) || []).forEach(function (p) { disSet[p.packageName] = 1; });
          }
          self.packages = all.map(function (p) {
            // iOS 非越狱拿不到"已禁用"状态，类型直接用设备返回的 system/user
            var type = app.isIos
              ? (p.type === "system" ? "system" : "third")
              : (disSet[p.packageName] ? "disabled"
                : (sysSet[p.packageName] || p.type === "system" ? "system" : "third"));
            return Util.enrichApp(Object.assign({}, p, { type: type }));
          });
          self.page = 1;
          return self.loadLabelStatus();
        })
        .catch(function (e) {
          self.loadError = e && e.message ? e.message : String(e);
        })
        .then(function () {
          self.loading = false;
          if (self.loadError) app.toast("获取应用列表失败：" + self.loadError, "bad");
          return self.loadLabels(app);
        });
    },

    /* 应用名：aapt 可选，缺少时降级为包名（不阻塞列表） */
    loadLabelStatus: function () {
      var self = this;
      return Util.call("get_label_status")
        .then(function (st) { self.labelStatus = st || self.labelStatus; })
        .catch(function () {});
    },
    loadLabels: function (app) {
      var self = this;
      var pend = this.pendingLabelPkgs;
      if (!pend.length) return Promise.resolve();
      this.labelsLoading = true;
      return Util.call("resolve_labels", pend)
        .then(function (r) {
          var map = (r && r.labels) || {};
          var keys = Object.keys(map);
          if (!keys.length) return;
          for (var i = 0; i < self.packages.length; i++) {
            var p = self.packages[i];
            if (map[p.packageName]) {
              self.packages[i] = Util.enrichApp(Object.assign({}, p, { label: map[p.packageName] }));
            }
          }
        })
        .catch(function () {})
        .then(function () { self.labelsLoading = false; });
    },
    scheduleLabels: function (app) {
      var self = this;
      if (this._labelTimer) window.clearTimeout(this._labelTimer);
      this._labelTimer = window.setTimeout(function () { self.loadLabels(app); }, 500);
    },
    setType: function (key) {
      this.typeFilter = key;
      this.page = 1;
      this.scheduleLabels(AdbApp);
    },
    gotoPage: function (n) {
      if (n < 1 || n > this.totalPages) return;
      this.page = n;
      this.scheduleLabels(AdbApp);
    },
    toggleSelect: function (pkg) {
      for (var i = 0; i < this.packages.length; i++) {
        if (this.packages[i].packageName === pkg) {
          this.packages[i].selected = !this.packages[i].selected;
          break;
        }
      }
    },
    get selectedPackages() {
      return this.packages.filter(function (p) { return p.selected; });
    },

    // 详情
    openDetail: function (app, pkg) {
      var self = this;
      this.detailOpen = true;
      this.detailLoading = true;
      // 不清空 detail：v-if 移除区块与文本 effect 存在一帧竞争，置 null 会触发空对象解引用
      return Util.call("get_app_detail", pkg)
        .then(function (r) {
          if (r && r.ok) {
            var d = r.detail;
            d.sizeBytes = (d.appSize || 0) + (d.dataSize || 0) + (d.cacheSize || 0);
            self.detail = d;
          } else {
            app.toast("获取详情失败：" + ((r && r.error) || "未知错误"), "bad");
            self.detailOpen = false;
          }
        })
        .catch(function (e) {
          app.toast("获取详情异常：" + e.message, "bad");
          self.detailOpen = false;
        })
        .then(function () {
          self.detailLoading = false;
        });
    },
    closeDetail: function () {
      this.detailOpen = false;
    },
    appAction: function (app, action) {
      var self = this;
      if (!this.detail) return;
      var pkg = this.detail.packageName;
      var names = { start: "启动", stop: "强制停止", clear: "清除数据", disable: "停用", enable: "启用" };
      if ((action === "clear" || action === "disable") &&
        !window.confirm("确认" + names[action] + "：" + pkg + " ？")) {
        return;
      }
      return Util.call("app_action", pkg, action)
        .then(function (r) {
          app.toast(names[action] + (r && r.ok ? "成功" : "失败"), r && r.ok ? "ok" : "bad");
          if (r && r.ok) self.reload(app);
        })
        .catch(function (e) {
          app.toast("操作异常：" + e.message, "bad");
        });
    },

    // 卸载
    confirmUninstall: function (pkg) {
      this.uninstallTarget = pkg;
      this.uninstallKeep = false;
      this.uninstallTask = null;
      this.uninstallOpen = true;
      this.detailOpen = false;
    },
    doUninstall: function (app) {
      var self = this;
      if (!this.uninstallTarget) return;
      // 显式带上序列号：后端 current_serial 可能与前端选中不一致（与安装同理）
      return Util.call("uninstall", this.uninstallTarget, this.uninstallKeep, app.currentSerial)
        .then(function (r) {
          self.uninstallTask = { status: "running", percent: 0, phase: "准备中…", logs: [] };
          app.watchTask(r.taskId, function (t) {
            self.uninstallTask = t;
          }, function (t) {
            app.toast(t.status === "success" ? "卸载完成" : ("卸载失败：" + (t.error || "未知错误")),
              t.status === "success" ? "ok" : "bad");
            self.uninstallOpen = false;
            self.reload(app);
          });
        })
        .catch(function (e) {
          app.toast("卸载异常：" + e.message, "bad");
        });
    },

    // 安装
    openInstall: function (app) {
      this.installOpen = true;
      this.installFile = "";
      this.installTask = null;
      // 刷新设备列表：手机可能是打开工具后才插上的，列表过期会让目标设备显示 "—"
      if (app && app.devices && app.devices.refresh) {
        app.devices.refresh(app);
      }
    },
    pickApk: function (app) {
      var self = this;
      var ios = this.isIos;
      var title = ios ? "选择 IPA 文件" : "选择 APK 文件";
      var types = ios
        ? ["IPA 文件 (*.ipa)"]
        : ["APK 文件 (*.apk;*.xapk;*.apks)"];
      return Util.call("choose_file", title, types, false)
        .then(function (res) {
          var p = Array.isArray(res) ? res[0] : res;
          if (p) self.installFile = p;
        })
        .catch(function (e) {
          app.toast("选择文件失败：" + e.message, "bad");
        });
    },
    clearInstallFile: function () {
      this.installFile = "";
      if (this.installTask && this.installTask.status !== "running") this.installTask = null;
    },
    doInstall: function (app) {
      var self = this;
      if (!this.installFile) {
        app.toast(this.isIos ? "请先选择 IPA 文件" : "请先选择 APK 文件", "warn");
        return;
      }
      // 显式带上序列号：后端 current_serial 可能与前端选中不一致
      return Util.call("install_apk", this.installFile, this.installOpts, app.currentSerial)
        .then(function (r) {
          self.installTask = { status: "running", percent: 0, phase: "准备中…", logs: [], speed: "" };
          app.watchTask(r.taskId, function (t) {
            self.installTask = t;
          }, function (t) {
            app.toast(t.status === "success" ? "安装成功" : ("安装失败：" + (t.error || "未知错误")),
              t.status === "success" ? "ok" : "bad");
            if (t.status === "success") {
              self.installOpen = false;
              self.reload(app);
            }
          });
        })
        .catch(function (e) {
          app.toast("安装异常：" + e.message, "bad");
        });
    },
    cancelInstall: function (app) {
      var self = this;
      if (!this.installTask) return;
      Util.call("cancel_task", this.installTask.id)
        .then(function () {
          app.toast("已取消安装", "warn");
          self.installTask = null;
        })
        .catch(function () {});
    }
  };
};
