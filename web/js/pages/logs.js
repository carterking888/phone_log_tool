/* 模块二：日志查看（核心） */

window.logsComponent = function () {
  // 点击空白处关闭多选下拉（全局只绑定一次）
  if (typeof document !== "undefined" && !window.__logsDdBound) {
    window.__logsDdBound = true;
    document.addEventListener("click", function () {
      var app = window.AdbApp;
      if (app && app.logs) app.logs.closeDd();
    });
  }
  return {
    running: false,
    paused: false,
    autoscroll: true,
    buffer: "main",
    buffers: [
      { key: "main", label: "main", desc: "主缓冲区" },
      { key: "system", label: "system", desc: "系统" },
      { key: "crash", label: "crash", desc: "崩溃" },
      { key: "events", label: "events", desc: "事件" }
    ],
    cursor: 0,
    lines: [],
    maxLines: 50000,   // 前端缓冲：后端有 10 万，洪流下（iOS 全量 ≈900 条/秒）8000 只够 9 秒
    displayLimit: 600,

    frozenRows: null,  // 取消自动滚动时的阅读快照（映射行副本，保证正在看的日志不消失）

    levels: [],          // 多选级别（命中任一）
    pids: [],            // 多选 PID
    tags: [],            // 多选标签
    levelOptions: [
      { k: "V", label: "Verbose" }, { k: "D", label: "Debug" }, { k: "I", label: "Info" },
      { k: "W", label: "Warn" }, { k: "E", label: "Error" }, { k: "F", label: "Fatal" }
    ],
    dd: { level: false, pid: false, tag: false },  // 多选下拉展开状态
    keyword: "",
    mode: "include",

    pkg: { package: "", label: "", pid: 0 },
    pkgPidTimer: null,

    selected: null,
    detailOpen: false,

    pkgModal: false,
    appTab: "running",
    appSearch: "",
    appSel: "",
    appsRunning: [],
    appsAll: [],
    appsLoading: false,

    saveModal: false,
    save: { scope: "filtered", format: "txt", path: "", withTs: true, withIds: true, zip: false },
    saving: false,
    loadError: "",

    // ---------------------------------------------------------- 派生数据
    get filtered() {
      var self = this;
      var lvSel = this.levels;
      var pidSel = this.pids;
      var tagSel = this.tags;
      var kw = String(this.keyword || "").trim();
      var mode = this.mode;
      // 非正则模式：空格分隔多关键字，预先转小写（OR 语义）
      var kws = mode === "regex" ? [] : kw.split(/\s+/).filter(Boolean).map(function (k) { return k.toLowerCase(); });
      var pkgPid = this.pkg.package ? Number(this.pkg.pid) : null;
      var pkgNames = this.pkg.names && this.pkg.names.length ? this.pkg.names : (this.pkg.package ? [this.pkg.package] : []);
      var re = null;
      if (kw && mode === "regex") {
        try {
          re = new RegExp(kw, "i");
        } catch (e) {
          re = null;
        }
      }
      var out = [];
      for (var i = 0; i < this.lines.length; i++) {
        var l = this.lines[i];
        if (pkgPid != null) {
          // PID 命中 或 Tag 命中任一候选名：应用重启换 PID（或 PID 未跟上）时仍能连续过滤
          // Cocos Inspector 只暴露 JS 上下文、不提供原生 PID；该调试端点本身已绑定一个游戏进程。
          if (l.source !== "cocos-cdp" && l.pid !== pkgPid && pkgNames.indexOf(l.tag) < 0) continue;
        }
        if (lvSel.length && lvSel.indexOf(l.level) < 0) continue;
        if (pidSel.length && pidSel.indexOf(Number(l.pid)) < 0) continue;
        if (tagSel.length && tagSel.indexOf(l.tag) < 0) continue;
        if (kw) {
          var text = l.msg + " " + l.tag;
          var hit;
          if (mode === "regex") {
            hit = re ? re.test(text) : false;
          } else {
            // 多关键字（空格分隔）任一命中即算命中（OR）；exclude 模式任一命中即排除
            var low = text.toLowerCase();
            hit = false;
            for (var k = 0; k < kws.length; k++) {
              if (low.indexOf(kws[k]) >= 0) { hit = true; break; }
            }
          }
          if (mode === "exclude" ? hit : !hit) continue;
        }
        out.push(l);
      }
      return out;
    },
    get displayed() {
      // 冻结阅读：取消自动滚动后展示快照副本，新日志继续入缓冲但表格内容不再变化
      if (this.frozenRows) return this.frozenRows;
      var f = this.filtered;
      var start = f.length > this.displayLimit ? f.length - this.displayLimit : 0;
      var out = [];
      for (var i = start; i < f.length; i++) {
        var l = f[i];
        out.push({
          id: l.id,
          time: l.time || "",
          level: l.level || "-",
          levelCls: "lv-" + (l.level || "raw").toLowerCase(),
          pid: l.pid,
          tid: l.tid,
          tag: l.tag || "",
          msgHtml: Util.hl(l.msg, this.keyword, this.mode),
          rowCls: (this.selected && this.selected.id === l.id ? "is-sel " : "") + (l.raw ? "is-raw" : ""),
          raw: l.raw
        });
      }
      return out;
    },
    get total() {
      return this.lines.length;
    },
    get filteredTotal() {
      return this.filtered.length;
    },
    get countText() {
      if (this.pkg.package) {
        return "已按包名过滤 · " + Util.fmtNum(this.filteredTotal) + " 条";
      }
      if (this.levels.length || this.pids.length || this.tags.length || this.keyword) {
        return "显示 " + Util.fmtNum(this.filteredTotal) + " / 共 " + Util.fmtNum(this.total) + " 条";
      }
      return "显示所有包日志 · " + Util.fmtNum(this.total) + " 条";
    },
    get levelLabel() {
      return this.levels.length ? "级别：" + this.levels.join("/") : "级别：全部";
    },
    get pidLabel() {
      if (!this.pids.length) return "PID：全部";
      return this.pids.length <= 3 ? "PID：" + this.pids.join(",") : "PID：已选 " + this.pids.length + " 项";
    },
    get tagLabel() {
      if (!this.tags.length) return "标签：全部";
      var j = this.tags.join(",");
      return "标签：" + (j.length > 18 ? "已选 " + this.tags.length + " 项" : j);
    },
    get pidOptions() {
      var seen = {};
      var out = [];
      for (var i = this.lines.length - 1; i >= 0 && out.length < 60; i--) {
        var p = this.lines[i].pid;
        if (p && !seen[p]) {
          seen[p] = 1;
          out.push(p);
        }
      }
      out.sort(function (a, b) { return a - b; });
      return out;
    },
    get tagOptions() {
      var seen = {};
      var out = [];
      for (var i = this.lines.length - 1; i >= 0 && out.length < 80; i--) {
        var t = this.lines[i].tag;
        if (t && !seen[t]) {
          seen[t] = 1;
          out.push(t);
        }
      }
      out.sort();
      return out;
    },
    get chips() {
      var out = [];
      if (this.pkg.package) {
        out.push({ key: "pkg", label: "包名", value: this.pkg.package });
      }
      if (this.levels.length) out.push({ key: "level", label: "级别", value: this.levels.join(",") });
      if (this.pids.length) out.push({ key: "pid", label: "PID", value: this.pids.join(",") });
      if (this.tags.length) {
        var tj = this.tags.join(",");
        out.push({ key: "tag", label: "标签", value: tj.length > 18 ? "已选 " + this.tags.length + " 项" : tj });
      }
      if (this.keyword) {
        out.push({
          key: "kw",
          label: { include: "包含", exclude: "不包含", regex: "正则" }[this.mode],
          value: this.keyword
        });
      }
      return out;
    },
    get filteredApps() {
      var kw = String(this.appSearch || "").toLowerCase().trim();
      var src = this.appTab === "running" ? this.appsRunning : this.appsAll;
      if (!kw) return src;
      return src.filter(function (a) {
        // 模糊包含：中文名 / 包名 / 展示名任一命中即可
        return (a.label || "").toLowerCase().indexOf(kw) >= 0 ||
          (a.packageName || "").toLowerCase().indexOf(kw) >= 0 ||
          (a.displayName || "").toLowerCase().indexOf(kw) >= 0;
      });
    },
    get stateText() {
      if (!this.running) return "未连接";
      return this.paused ? "暂停中" : "实时捕获中";
    },
    get stateCls() {
      if (!this.running) return "bad";
      return this.paused ? "warn" : "ok";
    },
    get selectedFields() {
      var l = this.selected;
      if (!l) return [];
      return [
        { label: "时间", value: l.time || "—" },
        { label: "级别", value: l.level || "—", cls: "lv-" + (l.level || "raw").toLowerCase() },
        { label: "PID", value: l.pid || "—" },
        { label: "TID", value: l.tid || "—" },
        { label: "进程", value: (this.pidNames && this.pidNames[l.pid]) || "—" },
        { label: "Tag", value: l.tag || "—" }
      ];
    },

    pidNames: {},
    iosMode: "",
    cdpErrorShown: "",
    cdpRunning: false,
    cdpCount: 0,

    // 卡片状态文案：连上但 0 条时明确告诉用户"不是没连上，是游戏没吐 console.log"
    get cdpStatusText() {
      if (!this.cdpRunning) return "";
      if (this.cdpCount > 0) return "● JS 捕获中（已捕获 " + this.cdpCount + " 条）";
      return "● JS 调试口已连接，等待游戏 console.log…（重启游戏可见启动日志）";
    },
    detecting: false,

    // iOS Cocos JS 捕获端口：输入框直接绑 AdbApp.settings.cocosInspector，
    // 启动时取当前输入值（保存到配置后全局生效）
    jsPortArg: function (app) {
      if (!app || !app.isIos) return null;
      var v = String((app.settings && app.settings.cocosInspector) || "").trim();
      return v || null;
    },
    // iOS 后端预过滤参数：oslog 洪流必须在前端选包时就地掐断，否则应用/JS 日志被顶走
    iosPkgArg: function (app) {
      if (!app || !app.isIos) return null;
      if (!this.pkg.package) return null;
      return {
        package: this.pkg.package,
        pid: this.pkg.pid || 0,
        names: (this.pkg.names && this.pkg.names.length) ? this.pkg.names : [this.pkg.package]
      };
    },
    detectJsPort: function (app) {
      var self = this;
      if (!app.currentSerial) {
        app.toast("请先选择设备", "warn");
        return;
      }
      this.detecting = true;
      Util.call("detect_js_port", app.currentSerial)
        .then(function (r) {
          self.detecting = false;
          if (r && r.ok) {
            app.settings.cocosInspector = String(r.port);
            app.toast("已发现 Cocos Inspector，设备端口 " + r.port + "，点击开始捕获即可", "ok");
          } else {
            app.toast((r && r.error) || "未发现调试端口", "warn");
          }
        })
        .catch(function (e) {
          self.detecting = false;
          app.toast("探测失败：" + e.message, "bad");
        });
    },
    saveJsPort: function (app) {
      var self = this;
      var v = String((app.settings && app.settings.cocosInspector) || "").trim();
      return Util.call("set_config", {
        adbPath: app.settings.adbPath || "",
        cocosInspector: v
      }).then(function () {
        app.toast("已保存（端口 " + (v || "未设置") + "）", "ok");
      }).catch(function (e) {
        app.toast("保存失败：" + e.message, "bad");
      });
    },

    // ---------------------------------------------------------- 生命周期
    start: function (app) {
      var self = this;
      if (!app.currentSerial) {
        app.toast("请先选择设备", "warn");
        return Promise.resolve();
      }
      return Util.call("start_logcat", app.currentSerial, this.buffer, this.jsPortArg(app), this.iosPkgArg(app))
        .then(function (r) {
          if (!r || !r.ok) {
            // iOS 失败时后端会带 hint（未信任 / 需管理员权限起 tunnel / 未装依赖）
            var msg = (r && r.error) || "未知错误";
            if (r && r.hint) msg += "｜" + r.hint;
            app.toast("启动失败：" + msg, "bad");
            return;
          }
          self.running = true;
          self.paused = false;
          self.cursor = 0;
          self.lines = [];
          self.frozenRows = null;
          self.cdpErrorShown = "";
          self.cdpRunning = false;
          // iOS 记录取流方式（syslog 直连 / oslog+tunnel），状态区会显示
          self.iosMode = (r && r.mode) || "";
          self.startPidWatch(app);
          app.toast("已开始捕获" + (self.iosMode
            ? " iOS 日志（" + self.iosMode + (r.cdpConfigured ? " + Cocos JS" : "") + "）"
            : (" " + self.buffer + " 缓冲区日志")), "ok");
        })
        .catch(function (e) {
          app.toast("启动异常：" + e.message, "bad");
        });
    },
    stop: function (app) {
      var self = this;
      this.running = false;
      this.stopPidWatch();
      return Util.call("stop_logcat").catch(function () {});
    },
    restart: function (app) {
      var self = this;
      this.stop(app).then(function () {
        self.start(app);
      });
    },
    togglePause: function (app) {
      this.paused = !this.paused;
      if (!this.paused) {
        this.flush(app);
      }
    },
    setBuffer: function (app, key) {
      if (this.buffer === key) return;
      this.buffer = key;
      if (this.running) this.restart(app);
    },
    clearLogs: function (app) {
      var self = this;
      this.lines = [];
      this.cursor = 0;
      this.frozenRows = null;
      this.selected = null;
      return Util.call("clear_logcat").catch(function () {});
    },

    /* 轮询拉取（后台继续入库，暂停时不渲染） */
    /* 返回 Promise<boolean>：本轮是否拉到新日志。调用方据此自适应降频。 */
    poll: function (app) {
      if (!this.running || this.paused) return Promise.resolve(false);
      return this.flush(app);
    },
    flush: function (app) {
      var self = this;
      var got = false;
      return Util.call("get_log_batch", this.cursor, 1500)
        .then(function (r) {
          if (!r) return false;
          if (r.error) {
            app.toast("日志中断：" + r.error, "bad");
            self.running = false;
            return false;
          }
          if (r.cdpError && r.cdpError !== self.cdpErrorShown) {
            self.cdpErrorShown = r.cdpError;
            app.toast("Cocos JS 日志连接失败：" + r.cdpError + "｜请检查端口或重启游戏", "warn");
          }
          if (!r.cdpError) self.cdpErrorShown = "";
          self.cdpRunning = !!r.cdpRunning;
          self.cdpCount = r.cdpCount || 0;
          if (r.lines && r.lines.length) {
            got = true;
            for (var i = 0; i < r.lines.length; i++) self.lines.push(r.lines[i]);
            if (self.lines.length > self.maxLines) {
              self.lines = self.lines.slice(self.lines.length - self.maxLines);
            }
          }
          self.cursor = r.cursor;
          if (r.running === false && self.running) {
            self.running = false;
            app.toast("日志流已结束（设备可能已断开）", "warn");
          }
          if (self.autoscroll) self.scrollToBottom();
          return got;
        })
        .catch(function (e) {
          self.loadError = e && e.message ? e.message : String(e);
          return false;
        });
    },
    scrollToBottom: function () {
      window.PetiteVue.nextTick(function () {
        var el = document.getElementById("logTableWrap");
        if (el) el.scrollTop = el.scrollHeight;
      });
    },
    /* 自动滚动开关：取消 = 冻结当前表格内容安心阅读（新日志照常入缓冲）；
       重新勾选 = 丢弃快照恢复跟随并跳到底部。 */
    onAutoscrollChange: function () {
      if (this.autoscroll) {
        this.frozenRows = null;
        this.scrollToBottom();
      } else {
        this.frozenRows = this.displayed.slice(); // displayed 内部已是新建的映射行数组
      }
    },

    /* PID 跟踪：应用重启后 PID 变化需自动更新过滤 */
    startPidWatch: function (app) {
      var self = this;
      this.stopPidWatch();
      var tick = function () {
        if (!self.pkg.package || !self.running) return;
        var names = (self.pkg.names && self.pkg.names.length ? self.pkg.names : [self.pkg.package]);
        Util.call("pidof", names[0], null, names.slice(1)).then(function (r) {
          var pid = r && r.pid ? r.pid : 0;
          if (pid && pid !== self.pkg.pid) {
            var old = self.pkg.pid;
            self.pkg = Object.assign({}, self.pkg, { pid: pid });
            if (old) app.toast("进程已重启：PID " + old + " → " + pid, "warn");
          }
        }).catch(function () {});
        Util.call("get_pid_map").then(function (r) {
          if (r && r.map) self.pidNames = r.map;
        }).catch(function () {});
      };
      tick();
      this.pkgPidTimer = window.setInterval(tick, 5000);
    },
    stopPidWatch: function () {
      if (this.pkgPidTimer) {
        window.clearInterval(this.pkgPidTimer);
        this.pkgPidTimer = null;
      }
    },

    // ---------------------------------------------------------- 过滤操作
    removeChip: function (key) {
      if (key === "pkg") {
        this.pkg = { package: "", label: "", pid: 0 };
      } else if (key === "level") {
        this.levels = [];
      } else if (key === "pid") {
        this.pids = [];
      } else if (key === "tag") {
        this.tags = [];
      } else if (key === "kw") {
        this.keyword = "";
      }
    },
    clearAllFilters: function () {
      this.pkg = { package: "", label: "", pid: 0 };
      this.levels = [];
      this.pids = [];
      this.tags = [];
      this.keyword = "";
      this.closeDd();
    },

    // ---------------------------------------------------------- 多选下拉
    toggleDd: function (k) {
      for (var key in this.dd) this.dd[key] = key === k ? !this.dd[key] : false;
    },
    closeDd: function () {
      this.dd.level = false;
      this.dd.pid = false;
      this.dd.tag = false;
    },
    isChecked: function (arrName, val) {
      return this[arrName].indexOf(val) >= 0;
    },
    toggleIn: function (arrName, val) {
      var arr = this[arrName];
      var i = arr.indexOf(val);
      if (i >= 0) arr.splice(i, 1); else arr.push(val);
    },
    clearSel: function (arrName) {
      this[arrName] = [];
    },

    // 包名过滤弹窗
    openPkgModal: function (app) {
      var self = this;
      this.pkgModal = true;
      this.appsLoading = true;
      this.appTab = "running";
      this.appSearch = "";
      this.appSel = this.pkg.package;
      Util.call("get_pid_map")
        .then(function (r) {
          var map = (r && r.map) || {};
          var labels = (r && r.labels) || {}; // 词库中文映射，未命中的为空
          self.pidNames = map;
          self.appsRunning = Object.keys(map).map(function (pid) {
            var name = map[pid];
            var label = labels[name] || name; // 有映射显示中文，没有则原进程名
            var ic = Util.iconMeta(label);
            return {
              packageName: name, label: label, pid: Number(pid), running: true,
              iconText: ic.text, iconColor: ic.color,
              displayName: label
            };
          });
          return Util.call("list_packages", null, "all");
        })
        .then(function (r) {
          var pkgs = (r && r.packages) || [];
          self.appsAll = pkgs.map(function (p) {
            var ic = Util.iconMeta(p.label || p.packageName);
            return Object.assign({}, p, {
              iconText: ic.text, iconColor: ic.color,
              displayName: p.label || p.packageName
            });
          });
          self.appsRunning = self.appsRunning.map(function (a) {
            var isIos = window.AdbApp && window.AdbApp.isIos;
            // iOS 进程名可能被应用设成显示名（如「老子有錢」），exe/显示名/包名都能对上同一应用
            var hit = self.appsAll.filter(function (x) {
              return x.packageName === a.packageName ||
                (isIos && ((x.exe && x.exe === a.packageName) || (x.label && x.label === a.packageName)));
            })[0];
            // 已装应用以列表 label（词库/aapt）为准，native 进程保持 get_pid_map 的结果
            var name = (hit && hit.label) || a.displayName;
            return Object.assign({}, a, { displayName: name, label: name, exe: hit && hit.exe });
          });
        })
        .catch(function (e) {
          app.toast("获取应用列表失败：" + e.message, "bad");
        })
        .then(function () {
          self.appsLoading = false;
        });
    },
    pickApp: function (pkg) {
      this.appSel = pkg;
    },
    applyPkg: function (app) {
      var self = this;
      var p = this.appSel;
      if (!p) {
        app.toast("请先选择一个应用", "warn");
        return;
      }
      var hit = this.appsRunning.filter(function (a) { return a.packageName === p; })[0];
      // iOS：进程名 ≠ bundle id（如 com.tencent.xin 的进程叫 WeChat）。
      // 过滤/PID 查找一律用可执行名（CFBundleExecutable），显示仍用 label。
      var hitAll = this.appsAll.filter(function (a) { return a.packageName === p; })[0];
      var isIos = window.AdbApp && window.AdbApp.isIos;
      var match = (isIos && hitAll && hitAll.exe) ? hitAll.exe : p;
      var label = hit ? hit.displayName : ((hitAll && hitAll.label) || p);
      // iOS 进程名可能被应用设成显示名（如「老子有錢」），exe/显示名/包名都作为候选
      var names = isIos ? [match, label, p] : [match];
      var pid = hit ? hit.pid : 0;
      if (!pid) {
        Util.call("pidof", match, null, names.slice(1))
          .then(function (r) {
            self.pkg = { package: match, label: label, pid: (r && r.pid) || 0, names: names };
            if (!self.pkg.pid) app.toast("该应用当前未运行，已按进程名记录（无 PID 日志）", "warn");
          })
          .catch(function () {
            self.pkg = { package: match, label: label, pid: 0, names: names };
          });
      } else {
        this.pkg = { package: match, label: label, pid: pid, names: names };
      }
      this.pkgModal = false;
      if (this.running) this.startPidWatch(app);
      app.toast("已按包名过滤：" + p, "ok");
      // iOS：过滤要在后端源头生效（掐掉系统日志洪流），必须重启会话
      if (app.isIos && this.running) this.restart(app);
      this.scrollToBottom();
    },
    clearPkg: function (app) {
      this.pkg = { package: "", label: "", pid: 0 };
      if (app && app.isIos && this.running) this.restart(app);
    },

    // 行操作
    selectLine: function (app, id) {
      var l = null;
      for (var i = this.lines.length - 1; i >= 0; i--) {
        if (this.lines[i].id === id) {
          l = this.lines[i];
          break;
        }
      }
      this.selected = l;
      this.detailOpen = !!l;
    },
    closeDetail: function () {
      this.detailOpen = false;
      this.selected = null;
    },
    filterByPid: function (app, pid) {
      this.pids = [Number(pid)];
      this.closeDd();
      app.toast("已按 PID " + pid + " 过滤", "ok");
    },
    filterByTag: function (app, tag) {
      this.tags = [tag];
      this.closeDd();
      app.toast("已按 Tag " + tag + " 过滤", "ok");
    },
    copyLine: function (app) {
      if (!this.selected) return;
      Util.copyText(
        this.selected.time + " " + this.selected.pid + " " + this.selected.tid + " " +
        this.selected.level + " " + this.selected.tag + ": " + this.selected.msg
      );
      app.toast("已复制日志行", "ok");
    },
    fullText: function () {
      var s = this.selected;
      if (!s) return "";
      return s.time + "  " + s.pid + "  " + s.tid + "  " + s.level + "  " + s.tag + ": " + s.msg;
    },

    // 保存
    openSave: function (app) {
      this.saveModal = true;
      this.save.path = "";
    },
    pickSavePath: function () {
      var self = this;
      var name = "logcat_" + new Date().toISOString().replace(/[-:T]/g, "").slice(0, 14) + "." + this.save.format;
      return Util.call("save_file_dialog", name)
        .then(function (p) {
          // 对话框返回值可能是数组（如 ["D:\\xx.txt"]），输入框显示时 join 成了正常路径的假象
          if (Array.isArray(p)) p = p[0] || "";
          if (p) self.save.path = p;
        })
        .catch(function () {});
    },
    doSave: function (app) {
      var self = this;
      if (!this.save.path) {
        app.toast("请选择保存位置", "warn");
        return;
      }
      var data = this.save.scope === "filtered" ? this.filtered : this.lines;
      var plain = data.map(function (l) {
        return { time: l.time, level: l.level, pid: l.pid, tid: l.tid, tag: l.tag, msg: l.msg };
      });
      this.saving = true;
      return Util.call("save_logs", plain, this.save.path, this.save.format,
        this.save.withTs, this.save.withIds, this.save.zip)
        .then(function (r) {
          if (r && r.ok) {
            app.toast("已保存 " + r.count + " 条 -> " + r.path, "ok");
            self.saveModal = false;
          } else {
            app.toast("保存失败：" + ((r && r.error) || "未知错误"), "bad");
          }
        })
        .catch(function (e) {
          app.toast("保存异常：" + e.message, "bad");
        })
        .then(function () {
          self.saving = false;
        });
    },
    get saveCountText() {
      return this.save.scope === "filtered" ? Util.fmtNum(this.filteredTotal) : Util.fmtNum(this.total);
    }
  };
};
