/* 模块一：设备管理 */
window.devicesComponent = function () {
  return {
    loading: false,
    loadError: "",
    env: {
      appVersion: "", adbPath: "", adbVersion: "", adbFound: false,
      serverRunning: false, serverPort: 5037, demo: false, reason: ""
    },
    list: [],
    current: "",
    detail: null,
    wifiAddr: "",
    wifiModal: false,
    wifiBusy: false,

    // ---------------- 派生
    get onlineList() {
      return this.list.filter(function (d) { return d.state === "device"; });
    },
    get usbCount() {
      return this.list.filter(function (d) { return d.transport === "usb" && d.state === "device"; }).length;
    },
    get wifiCount() {
      return this.list.filter(function (d) { return d.transport === "wifi" && d.state === "device"; }).length;
    },
    get badCount() {
      return this.list.filter(function (d) { return d.state !== "device"; }).length;
    },
    get authText() {
      if (this.badCount === 0) return "全部已授权";
      return this.badCount + " 台待处理";
    },
    get authCls() {
      return this.badCount === 0 ? "ok" : "warn";
    },
    get stateText() {
      if (this.onlineList.length === 0) return "未检测到设备";
      return "在线 " + this.onlineList.length + " 台";
    },
    get stateCls() {
      return this.onlineList.length === 0 ? "bad" : "ok";
    },
    get adbCls() {
      return this.env.adbFound ? "ok" : "bad";
    },
    get adbStateText() {
      if (!this.env.adbFound) return "未检测到";
      return this.env.serverRunning ? "已启动" : "未启动";
    },
    get detailFields() {
      var d = this.detail;
      if (!d) return [];
      if (d.platform === "ios") return this.iosFields;
      var rooted = d.rooted;
      var bat = d.battery || {};
      return [
        { label: "设备型号", value: d.model + (d.device ? " (" + d.device + ")" : "") },
        { label: "系统版本", value: "Android " + d.androidVersion + " (API " + d.apiLevel + ")" },
        { label: "设备序列号", value: d.serial },
        { label: "Android 版本", value: d.androidVersion + (d.displayId ? " (" + d.displayId + ")" : "") },
        { label: "内核版本", value: d.kernel || "—", wide: true },
        { label: "架构", value: d.abi || "—" },
        { label: "Root 权限", value: rooted ? "已获取" : "未获取", cls: rooted ? "v-ok" : "v-warn" },
        { label: "电量", value: bat.level != null ? bat.level + "%" : "—" }
      ];
    },
    /* iOS 详情字段：iOS 没有 kernel / root / apiLevel 概念，别硬套 Android 字段 */
    get iosFields() {
      var d = this.detail || {};
      var bat = d.battery || {};
      var st = d.storage || {};
      var used = st.total ? Util.fmtBytes(st.total - st.free) : "—";
      var total = st.total ? Util.fmtBytes(st.total) : "—";
      return [
        { label: "设备型号", value: d.model + (d.device ? " (" + d.device + ")" : "") },
        { label: "系统版本", value: "iOS " + (d.iosVersion || "—") + (d.buildVersion ? " (" + d.buildVersion + ")" : "") },
        { label: "设备名称", value: d.name || "—" },
        { label: "UDID", value: d.udid || d.serial || "—", wide: true },
        { label: "序列号", value: d.serialNumber || "—" },
        { label: "架构", value: d.abi || "—" },
        { label: "电量", value: bat.level != null ? bat.level + "%" : "—" },
        { label: "存储占用", value: st.ok ? (used + " / " + total) : "—" }
      ];
    },
    get tipCls() {
      if (!this.env.adbFound) return "bad";
      if (this.onlineList.length === 0) return "warn";
      return "ok";
    },
    get tipText() {
      var cur = this.current;
      var ios = this.list.some(function (d) {
        return d.serial === cur && d.platform === "ios";
      });
      if (!this.env.adbFound && !this.env.iosAvailable) {
        /* frozen 包里没有 pip，"pip install ..." 只对源码运行有意义 */
        if (this.env.frozen) {
          return "未检测到 adb：请在设置中指定 adb 路径（macOS 安装包已内置 adb，"
            + "Windows 包随附 public_settings/adb）；iOS 支持需以 WITH_IOS=1 重新打包。";
        }
        return "未检测到 adb，也未提供 iOS 支持：请在设置中指定 platform-tools 路径，或执行 pip install pymobiledevice3 启用 iOS。";
      }
      if (this.onlineList.length === 0) {
        return "未检测到已授权设备：Android 请检查 USB 调试开关与数据线；"
          + "iPhone 请在手机上点「信任此电脑」并输入锁屏密码。";
      }
      if (ios) {
        return "iOS 设备已连接：日志走系统 syslog（实测 iOS 17+ 也可直连，取不到时才需要 tunnel）；"
          + "文件管理可访问媒体域（DCIM / Books / Downloads 等），开启「文件共享」的应用可在文件页浏览沙盒。";
      }
      return "设备连接正常，可以开始查看实时日志，将自动获取 main / system / crash 缓冲区日志，支持级别过滤与关键字高亮。";
    },

    // ---------------- 行为
    init: function (app) {
      var self = this;
      this.refresh(app);
    },
    refresh: function (app) {
      var self = this;
      this.loading = true;
      this.loadError = "";
      return Util.call("refresh_env")
        .then(function (env) {
          self.env = Object.assign({}, self.env, env);
          app.env = self.env;
          return Util.call("list_devices");
        })
        .then(function (list) {
          self.list = (list || []).map(function (d) {
            var ic = Util.iconMeta(d.model || d.serial);
            return Object.assign({}, d, {
              iconText: ic.text, iconColor: ic.color,
              platform: d.platform || "android",
              platformText: d.platform === "ios" ? "iOS" : "Android",
              stateText: { device: "在线", offline: "离线", unauthorized: "未授权",
                "no permissions": "无权限" }[d.state] || d.state,
              stateCls: d.state === "device" ? "ok" : (d.state === "unauthorized" ? "bad" : "warn"),
              transportText: d.transport === "wifi" ? "无线" : "USB",
              batteryText: "—"
            });
          });
          var cur = app.currentSerial;
          if (!cur || !self.list.some(function (d) { return d.serial === cur; })) {
            cur = self.onlineList.length ? self.onlineList[0].serial : (self.list[0] || {}).serial || "";
          }
          app.currentSerial = cur;
          self.current = cur;
          return self.loadDetail(app);
        })
        .catch(function (e) {
          self.loadError = e && e.message ? e.message : String(e);
        })
        .then(function () {
          self.loading = false;
        });
    },
    loadDetail: function (app) {
      var self = this;
      if (!this.current) {
        this.detail = null;
        return Promise.resolve();
      }
      return Util.call("get_device_detail", this.current)
        .then(function (d) {
          self.detail = d;
        })
        .catch(function (e) {
          self.loadError = e && e.message ? e.message : String(e);
        });
    },
    select: function (app, serial) {
      var self = this;
      this.current = serial;
      app.currentSerial = serial;
      return Util.call("select_device", serial)
        .then(function () { return self.loadDetail(app); })
        .then(function () {
          if (app.logs.running) app.logs.restart(app);
        });
    },
    connectWireless: function (app) {
      var self = this;
      if (!this.wifiAddr) {
        app.toast("请输入设备 IP 与端口，如 192.168.1.20:5555", "warn");
        return;
      }
      this.wifiBusy = true;
      return Util.call("connect_wireless", this.wifiAddr)
        .then(function (r) {
          app.toast(r && r.ok ? "连接成功" : ("连接失败：" + ((r && r.output) || "未知错误")),
            r && r.ok ? "ok" : "bad");
          self.wifiModal = false;
          self.wifiAddr = "";
          return self.refresh(app);
        })
        .catch(function (e) {
          app.toast("连接异常：" + e.message, "bad");
        })
        .then(function () { self.wifiBusy = false; });
    },
    copySerial: function (app) {
      if (!this.detail) return;
      Util.copyText(this.detail.serial);
      app.toast("已复制序列号", "ok");
    }
  };
};
