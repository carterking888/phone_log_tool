/* 模块四：文件管理 */
window.filesComponent = function () {
  return {
    loading: false,
    loadError: "",
    currentPath: "/sdcard",
    pathInput: "/sdcard", // 地址栏（可直接输入 /data/data/<包名>/... 进应用私有目录）
    // iOS 应用容器：非空时所有文件操作都带 bundleId 走 HouseArrest
    appBundle: "",
    appList: [],
    appLoadError: "",
    entries: [],
    search: "",
    sortBy: "name",
    view: "list",
    storage: { total: 0, used: 0, free: 0, categories: [], ok: false },

    uploadOpen: false,
    uploadFiles: [],
    uploadTarget: "/sdcard/Android/adb_logs/2024-06",
    uploadOverwrite: true,
    uploadLog: true,
    uploadTask: null,

    deleteOpen: false,
    deleteTargets: [],
    deleteSecure: false,
    deleteConfirm: "",
    deleteLog: true,
    deleteTask: null,

    renameOpen: false,
    renamePath: "",
    renameValue: "",

    moveOpen: false,
    movePaths: [],
    moveTarget: "",

    pullTask: null,
    mkdirOpen: false,
    mkdirValue: "",

    // ---------------------------------------------------------- 派生
    get isIos() {
      return !!(window.AdbApp && window.AdbApp.isIos);
    },
    /* iOS 走 AFC：根就是 "/"，能访问的只有媒体域，没有 /sdcard、/data */
    get roots() {
      return this.isIos
        ? [{ path: "/", label: "/（媒体域）", locked: false }]
        : [
            { path: "/sdcard", label: "/sdcard", locked: false },
            { path: "/data", label: "/data", locked: true },
            { path: "/storage/emulated", label: "/storage/emulated", locked: false }
          ];
    },
    get quickDirs() {
      return this.isIos
        ? ["/DCIM", "/Books", "/Downloads", "/Photos"]
        : ["/sdcard/Download", "/sdcard/DCIM", "/sdcard/Pictures", "/sdcard/Android/adb_logs"];
    },
    get iosTip() {
      return this.isIos && !this.inApp
        ? "iPhone 可访问媒体域（DCIM / Books / Downloads 等）；在下方选择开启「文件共享」的应用即可浏览其沙盒。"
        : "";
    },
    get inApp() {
      return !!this.appBundle;
    },
    get appLabel() {
      var list = this.appList || [];
      for (var i = 0; i < list.length; i++) {
        if (list[i].packageName === this.appBundle) {
          return list[i].label || list[i].packageName;
        }
      }
      return this.appBundle;
    },
    get breadcrumbs() {
      var parts = String(this.currentPath || "/").split("/").filter(function (p) { return p; });
      var out = [{ label: this.inApp ? "应用容器根" : "根目录", path: "/" }];
      var acc = "";
      for (var i = 0; i < parts.length; i++) {
        acc += "/" + parts[i];
        out.push({ label: parts[i], path: acc });
      }
      return out;
    },
    get filteredEntries() {
      var kw = String(this.search || "").toLowerCase().trim();
      var by = this.sortBy;
      var list = this.entries.filter(function (e) {
        return !kw || e.name.toLowerCase().indexOf(kw) >= 0;
      });
      list.sort(function (a, b) {
        if (a.isDir !== b.isDir) return a.isDir ? -1 : 1;
        if (by === "size") return (b.size || 0) - (a.size || 0);
        return a.name.localeCompare(b.name);
      });
      return list;
    },
    get selectedEntries() {
      return this.entries.filter(function (e) { return e.selected; });
    },
    get selectedCount() {
      return this.selectedEntries.length;
    },
    get selectedSizeText() {
      var s = 0;
      this.selectedEntries.forEach(function (e) { s += e.size || 0; });
      return Util.fmtBytes(s);
    },
    get dirSizeText() {
      var s = 0;
      this.entries.forEach(function (e) { s += e.size || 0; });
      return Util.fmtBytes(s);
    },
    get usedPercent() {
      if (!this.storage.total) return 0;
      return Math.min(100, Math.round(this.storage.used / this.storage.total * 100));
    },
    get storageText() {
      if (!this.storage.ok) return "—";
      return Util.fmtBytes(this.storage.used) + " / " + Util.fmtBytes(this.storage.total);
    },
    get deleteName() {
      var p = this.deleteTargets[0] || "";
      var i = p.lastIndexOf("/");
      return i >= 0 ? p.slice(i + 1) : p;
    },
    get canDelete() {
      return this.deleteConfirm.trim() === "DELETE" && this.deleteTargets.length > 0;
    },
    get allSelected() {
      return this.entries.length > 0 && this.selectedEntries.length === this.entries.length;
    },
    get parentPath() {
      var p = String(this.currentPath || "/");
      if (p === "/") return "/";
      var i = p.lastIndexOf("/");
      return i <= 0 ? "/" : p.slice(0, i);
    },
    get deleteCommand() {
      var p = this.deleteTargets[0] || "";
      if (this.deleteSecure) {
        return "adb shell dd if=/dev/urandom of=" + p + " bs=1M && adb shell rm -rf " + p;
      }
      return "adb shell rm -rf " + p;
    },
    get uploadTotalText() {
      var s = 0;
      this.uploadFiles.forEach(function (f) { s += f.size || 0; });
      return Util.fmtBytes(s);
    },

    // ---------------------------------------------------------- 行为
    openDir: function (app, path) {
      var self = this;
      if (!path) return;
      this.currentPath = path;
      this.pathInput = path;
      this.search = "";
      return this.reload(app);
    },
    goToPath: function (app) {
      var p = String(this.pathInput || "").trim();
      if (!p) return;
      if (p.indexOf("/") !== 0) p = "/" + p; // 容错：漏写开头的 /
      // 清理重复斜杠与尾部斜杠（根目录除外）
      p = p.replace(/\/{2,}/g, "/");
      if (p.length > 1) p = p.replace(/\/+$/, "");
      this.pathInput = p;
      return this.openDir(app, p);
    },
    resetStorage: function () {
      this._storageTries = 0;
      this.storage = { total: 0, used: 0, free: 0, categories: [], ok: false };
    },
    // ---------------------------------------------------------- iOS 应用容器
    loadAppList: function (app) {
      var self = this;
      if (!this.isIos) {
        this.appList = [];
        this.appLoadError = "";
        return Promise.resolve();
      }
      this.appLoadError = "";
      return Util.call("list_packages", "user")
        .then(function (r) {
          if (r && r.ok) {
            self.appList = (r.packages || []).slice().sort(function (a, b) {
              return (a.label || a.packageName).localeCompare(b.label || b.packageName);
            });
          } else {
            self.appList = [];
            self.appLoadError = (r && r.error) || "无法加载应用列表";
          }
        })
        .catch(function (e) {
          self.appList = [];
          self.appLoadError = e && e.message ? e.message : String(e);
        });
    },
    enterApp: function (app, bundleId) {
      if (!bundleId) return;
      this.appBundle = bundleId;
      this.currentPath = "/";
      this.search = "";
      this.reload(app);
    },
    exitApp: function (app) {
      this.appBundle = "";
      this.currentPath = "/";
      this.search = "";
      this.reload(app);
    },
    resetApp: function () {
      this.appBundle = "";
      this.appList = [];
      this.appLoadError = "";
    },
    reload: function (app) {
      var self = this;
      if (!app.currentSerial) {
        app.toast("请先选择设备", "warn");
        return Promise.resolve();
      }
      // 换平台时旧路径一定不通：Android↔iOS 各自回到自己的根
      if (this.isIos && this.currentPath !== "/" &&
          (this.currentPath === "/data" ||
           this.currentPath.indexOf("/sdcard") === 0 ||
           this.currentPath.indexOf("/storage") === 0)) {
        this.currentPath = "/";
        this.uploadTarget = "/";
        this.appBundle = "";
      } else if (!this.isIos && this.currentPath === "/") {
        this.currentPath = "/sdcard";
        this.appBundle = "";
      }
      this.loading = true;
      this.loadError = "";
      // bundleId 占位：进入应用沙盒后所有文件操作都带它走 HouseArrest
      var bid = this.inApp ? this.appBundle : null;
      return Util.call("list_dir", this.currentPath, null, bid)
        .then(function (r) {
          if (r && r.ok) {
            self.entries = (r.entries || []).map(Util.enrichEntry);
          } else {
            self.entries = [];
            self.loadError = (r && r.error) || "无法读取目录";
          }
        })
        .catch(function (e) {
          self.loadError = e && e.message ? e.message : String(e);
          self.entries = [];
        })
        .then(function () {
          self.loading = false;
          self.pathInput = self.currentPath; // 面包屑/上级导航后同步地址栏
        });
    },
    _storageTries: 0,
    loadStorage: function (app) {
      var self = this;
      return Util.call("storage_stats")
        .then(function (r) {
          if (!r) return;
          r.categories = (r.categories || []).map(function (c) {
            return Object.assign({}, c, {
              text: Util.fmtBytes(c.bytes),
              width: r.total ? Math.max(3, Math.round(c.bytes / r.total * 100)) : 0
            });
          });
          self.storage = r;
          // 真机分类占用靠 du 后台统计，未就绪时再拉几次
          if (r.categoriesReady === false && self._storageTries < 8) {
            self._storageTries += 1;
            window.setTimeout(function () { self.loadStorage(app); }, 2500);
          }
        })
        .catch(function () {});
    },
    up: function (app) {
      return this.openDir(app, this.parentPath);
    },
    isCurrent: function (path) {
      return this.currentPath === path;
    },
    toggle: function (e) {
      e.selected = !e.selected;
    },
    toggleAll: function () {
      var v = !this.allSelected;
      this.entries.forEach(function (e) { e.selected = v; });
    },
    clearSel: function () {
      this.entries.forEach(function (e) { e.selected = false; });
    },
    enter: function (app, e) {
      if (e.isDir) return this.openDir(app, e.path);
    },

    // 新建文件夹
    openMkdir: function () {
      this.mkdirOpen = true;
      this.mkdirValue = "";
    },
    doMkdir: function (app) {
      var self = this;
      if (!this.mkdirValue) {
        app.toast("请输入文件夹名称", "warn");
        return;
      }
      return Util.call("mkdir", this.currentPath, this.mkdirValue, null,
        this.inApp ? this.appBundle : null)
        .then(function (r) {
          app.toast(r && r.ok ? "已创建" : ("创建失败：" + ((r && r.error) || "")), r && r.ok ? "ok" : "bad");
          self.mkdirOpen = false;
          return self.reload(app);
        })
        .catch(function (e) {
          app.toast("异常：" + e.message, "bad");
        });
    },

    // 重命名
    openRename: function (e) {
      this.renamePath = e.path;
      this.renameValue = e.name;
      this.renameOpen = true;
    },
    doRename: function (app) {
      var self = this;
      return Util.call("rename", this.renamePath, this.renameValue, null,
        this.inApp ? this.appBundle : null)
        .then(function (r) {
          app.toast(r && r.ok ? "已重命名" : ("失败：" + ((r && r.error) || "")), r && r.ok ? "ok" : "bad");
          self.renameOpen = false;
          return self.reload(app);
        })
        .catch(function (e) {
          app.toast("异常：" + e.message, "bad");
        });
    },

    // 移动
    openMove: function (app) {
      var sel = this.selectedEntries;
      if (!sel.length) {
        app.toast("请先选择文件", "warn");
        return;
      }
      this.movePaths = sel.map(function (e) { return e.path; });
      this.moveTarget = this.currentPath;
      this.moveOpen = true;
    },
    doMove: function (app) {
      var self = this;
      return Util.call("move", this.movePaths, this.moveTarget, null,
        this.inApp ? this.appBundle : null)
        .then(function (r) {
          app.toast(r && r.ok ? "移动完成" : ("移动失败：" + ((r && r.error) || "")), r && r.ok ? "ok" : "bad");
          self.moveOpen = false;
          self.clearSel();
          return self.reload(app);
        })
        .catch(function (e) {
          app.toast("异常：" + e.message, "bad");
        });
    },

    // 上传
    openUpload: function (app) {
      this.uploadOpen = true;
      this.uploadTarget = this.currentPath;
      this.uploadTask = null;
    },
    pickLocalFiles: function (app) {
      var self = this;
      return Util.call("choose_files", "选择要上传的文件", true)
        .then(function (res) {
          var paths = Array.isArray(res) ? res : (res ? [res] : []);
          if (!paths.length) return;
          return Util.call("expand_local", paths).then(function (files) {
            self.uploadFiles = files.map(function (f) {
              return { path: f.path, name: f.name, size: f.size, sizeText: Util.fmtBytes(f.size) };
            });
          });
        })
        .catch(function (e) {
          app.toast("选择文件失败：" + e.message, "bad");
        });
    },
    removeUploadFile: function (i) {
      this.uploadFiles.splice(i, 1);
    },
    doUpload: function (app) {
      var self = this;
      if (!this.uploadFiles.length) {
        app.toast("请先选择文件", "warn");
        return;
      }
      var paths = this.uploadFiles.map(function (f) { return f.path; });
      return Util.call("push", paths, this.uploadTarget, this.uploadOverwrite, null,
        this.inApp ? this.appBundle : null)
        .then(function (r) {
          self.uploadTask = {
            status: "running", percent: 0, phase: "准备中…", speed: "",
            logs: [], done: 0, total: 0
          };
          app.watchTask(r.taskId, function (t) {
            self.uploadTask = t;
          }, function (t) {
            app.toast(t.status === "success" ? "上传完成" : (t.status === "cancelled" ? "已取消" : ("上传失败：" + (t.error || ""))),
              t.status === "success" ? "ok" : (t.status === "cancelled" ? "warn" : "bad"));
            if (t.status === "success") {
              self.uploadOpen = false;
              self.uploadFiles = [];
              self.reload(app);
            }
          });
        })
        .catch(function (e) {
          app.toast("上传异常：" + e.message, "bad");
        });
    },
    cancelUpload: function (app) {
      var self = this;
      if (!this.uploadTask || !this.uploadTask.id) return;
      Util.call("cancel_task", this.uploadTask.id).then(function () {
        app.toast("已取消上传", "warn");
      }).catch(function () {});
    },

    // 下载
    download: function (app, paths) {
      var self = this;
      if (!paths || !paths.length) {
        app.toast("请先选择文件", "warn");
        return;
      }
      return Util.call("choose_folder", "选择保存位置")
        .then(function (dir) {
          if (!dir) return;
          return Util.call("pull", paths, dir, null,
            self.inApp ? self.appBundle : null).then(function (r) {
            self.pullTask = { status: "running", percent: 0, phase: "准备中…", logs: [] };
            app.watchTask(r.taskId, function (t) {
              self.pullTask = t;
            }, function (t) {
              app.toast(t.status === "success" ? "下载完成：" + dir : ("下载失败：" + (t.error || "")),
                t.status === "success" ? "ok" : "bad");
            });
          });
        })
        .catch(function (e) {
          app.toast("下载异常：" + e.message, "bad");
        });
    },

    // 删除 / 安全擦除
    openDelete: function (app, paths) {
      if (!paths || !paths.length) {
        app.toast("请先选择文件", "warn");
        return;
      }
      this.deleteTargets = paths;
      this.deleteSecure = false;
      this.deleteConfirm = "";
      this.deleteTask = null;
      this.deleteOpen = true;
    },
    doDelete: function (app) {
      var self = this;
      if (!this.canDelete) return;
      return Util.call("remove", this.deleteTargets, this.deleteSecure, null,
        this.inApp ? this.appBundle : null)
        .then(function (r) {
          self.deleteTask = { status: "running", percent: 0, phase: "准备中…", logs: [] };
          app.watchTask(r.taskId, function (t) {
            self.deleteTask = t;
          }, function (t) {
            app.toast(t.status === "success" ? "删除完成" : ("删除失败：" + (t.error || "")),
              t.status === "success" ? "ok" : "bad");
            if (t.status === "success") {
              self.deleteOpen = false;
              self.clearSel();
              self.reload(app);
            }
          });
        })
        .catch(function (e) {
          app.toast("删除异常：" + e.message, "bad");
        });
    }
  };
};
