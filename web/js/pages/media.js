/* 模块六：工具与媒体（一键截屏 / 一键录屏 / 媒体文件库） */
window.mediaComponent = function () {
  return {
    loading: false,
    caps: null,             // {platform, screenshot, record, screenshotHint, recordHint, saveDir}
    // 录屏参数
    recSource: "screenrecord",
    recResolution: "",
    recBitrate: "8",
    recDuration: "60",
    recDurationCustom: "10", // 时长上限选"自定义"时的分钟数（1-5000，iOS 生效；Android 受 screenrecord 3 分钟硬限制）
    recFps: "8",            // iOS 截屏帧率
    recName: "",
    recAudio: false,
    recTouch: false,
    recStay: true,
    recording: false,
    recElapsed: 0,
    recLimit: 0,            // 到时自动停止（秒）
    recTimer: null,
    recBusy: false,
    recVideoName: "",       // 最近一次录制完成的视频（内嵌预览）
    recVideoUrl: "",
    recSeconds: "",
    statusTimer: null,      // 录制期间轮询后端状态（中断自动保存感知）
    // 截屏
    shotSave: false,
    shotCopy: true,
    shotBusy: false,
    shotPreview: "",
    shotFile: "",
    // 媒体列表
    files: [],
    filterKind: "all",
    keyword: "",
    page: 1,
    pageSize: 5,            // 每页显示条数
    renameModal: false,
    renameOld: "",
    renameNew: "",

    // ---------------- 派生
    get curPlatform() {
      var d = window.AdbApp && AdbApp.devices.detail;
      if (d && d.platform) return d.platform;
      var list = (window.AdbApp && AdbApp.devices.list) || [];
      for (var i = 0; i < list.length; i++) {
        if (list[i].serial === AdbApp.currentSerial) return list[i].platform || "android";
      }
      return "android";
    },
    get isAndroid() { return this.curPlatform === "android"; },
    get isHarmony() { return this.curPlatform === "harmony"; },
    get isIos() { return this.curPlatform === "ios"; },
    get recordBlocked() {
      return !this.caps || !this.caps.record;
    },
    get shotBlocked() {
      return !this.caps || !this.caps.screenshot;
    },
    get recordBtnText() {
      return this.recording ? "停止录制" : "一键开始录屏";
    },
    get filteredFiles() {
      var k = this.filterKind, kw = (this.keyword || "").toLowerCase();
      return this.files.filter(function (f) {
        if (k === "image" && f.kind !== "image") return false;
        if (k === "video" && f.kind !== "video") return false;
        if (kw && f.name.toLowerCase().indexOf(kw) < 0) return false;
        return true;
      });
    },
    get totalPages() {
      return Math.max(1, Math.ceil(this.filteredFiles.length / this.pageSize));
    },
    get pagedFiles() {
      var start = (this.page - 1) * this.pageSize;
      return this.filteredFiles.slice(start, start + this.pageSize);
    },
    get kindText() {
      return function (k) {
        return { image: "截屏图片", video: "录屏视频", other: "文件" }[k] || k;
      };
    },
    get sizeText() {
      return function (n) {
        if (n > 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + " MB";
        if (n > 1024) return (n / 1024).toFixed(0) + " KB";
        return n + " B";
      };
    },

    // ---------------- 生命周期
    init: function (app) {
      this.loadCaps(app);
      this.refresh(app);
      this.checkStatus(app); // 进页面顺带取一次：上次异常退出恢复出的录屏
    },
    loadCaps: function (app) {
      var self = this;
      Util.call("media_capabilities").then(function (c) {
        self.caps = c || null;
      }).catch(function () {});
    },
    // 换设备后刷新能力与列表
    reload: function (app) {
      this.recording = false;
      this.stopStatusPoll();
      if (this.recTimer) { clearInterval(this.recTimer); this.recTimer = null; }
      this.recLimit = 0;
      this.recVideoName = "";
      this.recVideoUrl = "";
      this.recSeconds = "";
      this.shotPreview = "";
      this.loadCaps(app);
      this.refresh(app);
    },

    // ---------------- 录屏
    toggleRecord: function (app) {
      var self = this;
      if (this.recBusy) return;
      if (this.recording) {
        this.recBusy = true;
        Util.call("media_record_stop").then(function (r) {
          self.recBusy = false;
          if (r && r.ok) {
            self.recording = false;
            self.stopStatusPoll();
            if (self.recTimer) { clearInterval(self.recTimer); self.recTimer = null; }
            self.recLimit = 0;
            self.recSeconds = self.fmtTime(r.seconds || 0);
            self.recVideoName = r.name || "";
            self.recVideoUrl = r.name ? ("/media/" + encodeURIComponent(r.name)) : "";
            app.toast("录制完成: " + r.name + "（" + self.recSeconds
              + (r.frames ? "，" + r.frames + " 帧" : "") + "）", "ok");
            self.refresh(app);
            setTimeout(function () { self.refresh(app); }, 600); // 二次确认列表已含新文件
          } else {
            app.toast((r && r.error) || "停止失败", "bad");
          }
        }).catch(function (e) {
          self.recBusy = false;
          app.toast("停止异常: " + e.message, "bad");
        });
        return;
      }
      this.recBusy = true;
      var dur = this.recDuration === "custom"
        ? (parseInt(this.recDurationCustom, 10) || 1) * 60  // 自定义按分钟
        : parseInt(this.recDuration, 10);
      Util.call("media_record_start", {
        source: this.recSource,
        resolution: this.recResolution,
        bitrate: parseFloat(this.recBitrate),
        duration: dur,
        fps: parseInt(this.recFps, 10),
        name: this.recName,
        audio: this.recAudio,
        touch: this.recTouch,
        stayAwake: this.recStay
      }).then(function (r) {
        self.recBusy = false;
        if (r && r.ok) {
          self.recording = true;
          self.recElapsed = 0;
          self.recLimit = r.limit || 0;
          self.startStatusPoll(app); // 轮询后端：设备断开等中断场景由后端自动保存
          // iOS 上一次的预览指向旧文件，开新录制时先收起
          self.recVideoName = "";
          self.recVideoUrl = "";
          self.recSeconds = "";
          self.recTimer = setInterval(function () {
            self.recElapsed++;
            // 到达时长上限自动停止（Android 设备端到时也会自动收尾，此处统一收口）
            if (self.recLimit && self.recElapsed >= self.recLimit) {
              self.toggleRecord(app);
            }
          }, 1000);
          app.toast("录制中，最长 " + r.limit + " 秒，点「停止录制」保存"
            + (r.fps ? "（" + r.fps + " fps）" : ""), "ok");
        } else {
          app.toast((r && r.error) || "启动失败", "bad");
        }
      }).catch(function (e) {
        self.recBusy = false;
        app.toast("录制异常: " + e.message, "bad");
      });
    },

    // ---------------- 录屏状态轮询（感知设备断开等中断，后端会自动保存已录部分）
    startStatusPoll: function (app) {
      var self = this;
      this.stopStatusPoll();
      this.statusTimer = setInterval(function () {
        Util.call("media_record_status").then(function (r) {
          if (!r || !r.ok) return;
          if (r.recovered && r.recovered.length) {
            app.toast("恢复了 " + r.recovered.length + " 个中断的录屏: "
              + r.recovered.join("、"), "ok");
            self.refresh(app);
          }
          if (!self.recording || r.recording) return;
          // 后端已收尾（设备断开/连续失败/到时），前端同步停下并展示结果
          self.recording = false;
          self.stopStatusPoll();
          if (self.recTimer) { clearInterval(self.recTimer); self.recTimer = null; }
          self.recLimit = 0;
          var fin = r.finished;
          if (fin && fin.ok) {
            self.recSeconds = self.fmtTime(fin.seconds || 0);
            self.recVideoName = fin.name || "";
            self.recVideoUrl = fin.name ? ("/media/" + encodeURIComponent(fin.name)) : "";
            app.toast("录制中断，已自动保存已录制部分: " + fin.name, "ok");
            self.refresh(app);
            setTimeout(function () { self.refresh(app); }, 600);
          } else {
            app.toast((fin && fin.error) || "录制已结束", "bad");
          }
        }).catch(function () { /* 轮询失败忽略，下一轮再试 */ });
      }, 3000);
    },
    stopStatusPoll: function () {
      if (this.statusTimer) { clearInterval(this.statusTimer); this.statusTimer = null; }
    },
    // 进页面时取一次状态（主要为了拿恢复出的历史录屏）
    checkStatus: function (app) {
      var self = this;
      Util.call("media_record_status").then(function (r) {
        if (r && r.recovered && r.recovered.length) {
          app.toast("恢复了 " + r.recovered.length + " 个中断的录屏: "
            + r.recovered.join("、"), "ok");
          self.refresh(app);
        }
      }).catch(function () {});
    },

    // ---------------- 录屏视频
    saveVideo: function (app) {
      var self = this;
      if (!this.recVideoName) return;
      Util.call("media_save_as", this.recVideoName).then(function (r) {
        if (r && r.ok && !r.canceled) app.toast("已保存到: " + r.dest, "ok");
      }).catch(function (e) {
        app.toast("保存异常: " + e.message, "bad");
      });
    },

    // ---------------- 截屏
    doScreenshot: function (app) {
      var self = this;
      if (this.shotBusy) return;
      this.shotBusy = true;
      Util.call("media_screenshot", this.shotSave, this.shotCopy)
        .then(function (r) {
          self.shotBusy = false;
          if (r && r.ok) {
            self.shotPreview = r.preview || "";
            self.shotFile = r.name || "";
            app.toast("截图已保存: " + r.name + (self.shotCopy ? "，已复制到剪贴板" : ""), "ok");
            self.refresh(app);
          } else {
            app.toast((r && r.error) || "截屏失败", "bad");
          }
        })
        .catch(function (e) {
          self.shotBusy = false;
          app.toast("截屏异常: " + e.message, "bad");
        });
    },
    openShot: function (app) {
      if (this.shotFile) this.openFile(app, this.shotFile);
    },

    // ---------------- 媒体库
    setFilter: function (k) {
      this.filterKind = k;
      this.page = 1;
    },
    goPage: function (p) {
      this.page = Math.min(Math.max(1, p), this.totalPages);
    },
    refresh: function (app) {
      var self = this;
      Util.call("media_list", this.keyword).then(function (list) {
        self.files = list || [];
        // 数据变少（删除/过滤）后页码可能越界，收回来
        if (self.page > self.totalPages) self.page = self.totalPages;
      }).catch(function () {});
    },
    openFile: function (app, name) {
      Util.call("media_open", name).then(function (r) {
        if (r && !r.ok) app.toast(r.error || "打开失败", "bad");
      });
    },
    askRename: function (f) {
      this.renameOld = f.name;
      this.renameNew = f.name;
      this.renameModal = true;
    },
    doRename: function (app) {
      var self = this;
      Util.call("media_rename", this.renameOld, this.renameNew).then(function (r) {
        if (r && r.ok) {
          self.renameModal = false;
          app.toast("已重命名", "ok");
          self.refresh(app);
        } else {
          app.toast((r && r.error) || "重命名失败", "bad");
        }
      });
    },
    deleteFile: function (app, f) {
      var self = this;
      // 桌面工具本地图库，删除即移除；确认由前端二次提示承担
      if (!confirm("确定删除 " + f.name + " ？")) return;
      Util.call("media_delete", f.name).then(function (r) {
        if (r && r.ok) {
          app.toast("已删除", "ok");
          self.refresh(app);
        } else {
          app.toast((r && r.error) || "删除失败", "bad");
        }
      });
    },
    fmtTime: function (s) {
      if (s < 60) return s + " 秒";
      return Math.floor(s / 60) + " 分 " + (s % 60) + " 秒";
    },
    fmtDur: function (s) {
      s = Math.round(s);
      var m = Math.floor(s / 60), r = s % 60;
      return m + ":" + (r < 10 ? "0" : "") + r;
    }
  };
};
