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
    /* 连接过的设备历史（后端 config.json 持久化），刷新后仍在 */
    history: [],
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
    /* 当前选中设备的平台 / 传输方式（详情优先，列表兜底） */
    get curDevice() {
      var d = this.detail;
      if (d && d.platform) return d;
      var list = this.list || [];
      for (var i = 0; i < list.length; i++) {
        if (list[i].serial === this.current) return list[i];
      }
      return null;
    },
    get curPlatform() {
      var d = this.curDevice;
      return d ? (d.platform || "android") : "android";
    },
    get curTransport() {
      var d = this.curDevice;
      return d ? (d.transport || "usb") : "usb";
    },
    get detailFields() {
      var d = this.detail;
      if (!d) return [];
      if (d.platform === "ios") return this.iosFields;
      if (d.platform === "harmony") return this.harmonyFields;
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
    /* 鸿蒙详情字段：没有 root / kernel 概念，属性来自 param get */
    get harmonyFields() {
      var d = this.detail || {};
      if (d.error) {
        return [
          { label: "设备序列号", value: d.serial, wide: true },
          { label: "状态", value: d.error, cls: "v-warn", wide: true }
        ];
      }
      return [
        { label: "设备型号", value: d.model || "—" },
        { label: "系统版本", value: "HarmonyOS " + (d.osVersion || "—") },
        { label: "API 版本", value: d.apiLevel ? "API " + d.apiLevel : "—" },
        { label: "设备名称", value: d.device || d.brand || "—" },
        { label: "设备序列号", value: d.serial, wide: true }
      ];
    },
    get tipCls() {
      if (!this.env.adbFound) return "bad";
      if (this.onlineList.length === 0) return "warn";
      return "ok";
    },
    get tipText() {
      var cur = this.current;
      var harmony = this.list.some(function (d) {
        return d.serial === cur && d.platform === "harmony";
      });
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
      var curDev = this.curDevice;
      if (curDev && curDev.platform === "harmony" && curDev.state === "unauthorized") {
        return "鸿蒙设备未授权：请解锁手机屏幕，在弹出的「是否允许 hdc 调试」对话框点允许"
          + "（建议勾选「始终允许使用 hdc 调试」）；没看到弹窗就拔插一次 USB 线，"
          + "或执行 hdc kill -r 后重插。";
      }
      if (this.onlineList.length === 0) {
        return "未检测到已授权设备：Android 请检查 USB 调试开关与数据线；"
          + "iPhone 请在手机上点「信任此电脑」并输入锁屏密码；"
          + "鸿蒙请在设置里打开「开发者模式 - USB 调试」并在手机上允许 hdc 调试。";
      }
      if (harmony) {
        return "鸿蒙设备已连接（hdc）：日志走 hilog（已尽力关闭隐私掩码，部分系统版本仍会显示 <private>）；"
          + "应用管理支持查看 / 启动 / 强制停止 / 卸载 / 安装 .hap。";
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
              // 鸿蒙设备用 config/log.png 作为卡片图标（本地服务映射 /config/）
              iconImg: d.platform === "harmony" ? "/config/log.png" : "",
              platform: d.platform || "android",
              platformText: d.platform === "ios" ? "iOS"
                : (d.platform === "harmony" ? "HarmonyOS" : "Android"),
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
        .then(function () { return self.loadHistory(); })
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
          if (r && r.ok) {
            app.toast("连接成功", "ok");
            self.wifiModal = false;
            self.wifiAddr = "";
            return self.refresh(app);
          }
          var out = (r && r.output) || "未知错误";
          var hint = out.indexOf("Connect failed") >= 0 || out.indexOf("Fail") >= 0
            ? "（请确认手机与电脑同一 Wi-Fi、IP:端口 与手机「无线调试」页面显示一致、手机上已点允许）"
            : "";
          app.toast("连接失败：" + out + " " + hint, "bad");
        })
        .catch(function (e) {
          app.toast("连接异常：" + e.message, "bad");
        })
        .then(function () { self.wifiBusy = false; });
    },
    /* USB 在线时一键切无线监听（鸿蒙 tmode / Android tcpip），成功后预填地址 */
    enableWireless: function (app) {
      var self = this;
      this.wifiBusy = true;
      return Util.call("enable_wireless", null)
        .then(function (r) {
          if (!r || !r.ok) {
            app.toast("切换失败：" + ((r && r.output) || "未知错误"), "bad");
            return null;
          }
          var addr = r.ip ? (r.ip + ":" + r.port) : "";
          if (addr) self.wifiAddr = addr;
          self.wifiModal = true;
          app.toast("已切换为无线监听（端口 " + r.port + "）。拔掉 USB 线后点「连接」"
            + (addr ? "，地址已填好" : "；获取 IP 失败，请在手机 WLAN 详情里查看 IP"), "ok");
          return null;
        })
        .catch(function (e) {
          app.toast("切换异常：" + e.message, "bad");
        })
        .then(function () { self.wifiBusy = false; });
    },
    /* 断开当前无线设备（鸿蒙 tdisconnect / adb disconnect） */
    disconnectWireless: function (app) {
      var self = this;
      var addr = this.curDevice && this.curDevice.serial;
      if (!addr) return;
      return Util.call("disconnect_wireless", addr)
        .then(function (r) {
          app.toast(r && r.ok ? "已断开无线连接" : "断开失败：" + ((r && r.output) || ""),
            r && r.ok ? "ok" : "bad");
          return self.refresh(app);
        })
        .catch(function (e) {
          app.toast("断开异常：" + e.message, "bad");
        });
    },
    /* ---- 历史设备：加载（过滤掉当前仍在线的）、删除、点击回连 ---- */
    loadHistory: function () {
      var self = this;
      return Util.call("device_history")
        .then(function (hist) {
          var curSerials = {};
          (self.list || []).forEach(function (d) { curSerials[d.serial] = true; });
          self.history = (hist || [])
            .filter(function (h) { return h.serial && !curSerials[h.serial]; })
            .map(function (h) {
              var ic = Util.iconMeta(h.model || h.serial);
              return Object.assign({}, h, {
                iconText: ic.text, iconColor: ic.color,
                iconImg: h.platform === "harmony" ? "/config/log.png" : "",
                platformText: h.platform === "ios" ? "iOS"
                  : (h.platform === "harmony" ? "HarmonyOS" : "Android"),
                isWifi: /^\d{1,3}(\.\d{1,3}){3}:\d+$/.test(h.serial || "")
              });
            });
        })
        .catch(function () { self.history = []; });
    },
    removeHistory: function (app, serial) {
      var self = this;
      return Util.call("remove_device_history", serial)
        .then(function () {
          self.history = self.history.filter(function (h) { return h.serial !== serial; });
          app.toast("已删除历史记录", "ok");
        })
        .catch(function (e) {
          app.toast("删除失败：" + (e && e.message ? e.message : e), "bad");
        });
    },
    /* 从列表移除设备：无线设备先断开连接；离线设备隐藏；历史记录一并删除 */
    removeDevice: function (app, serial) {
      var self = this;
      return Util.call("remove_device", serial)
        .then(function () {
          app.toast("已移除设备 " + serial, "ok");
          return self.refresh(app);
        })
        .catch(function (e) {
          app.toast("移除失败：" + (e && e.message ? e.message : e), "bad");
        });
    },
    clickHistory: function (app, h) {
      if (!h) return;
      /* 无线地址（ip:port）直接预填到连接弹窗，一键回连 */
      if (h.isWifi) {
        this.wifiAddr = h.serial;
        this.wifiModal = true;
        return;
      }
      app.toast("「" + (h.model || h.serial) + "」请重新插入 USB 后刷新检测", "warn");
    },
    copySerial: function (app) {
      if (!this.detail) return;
      Util.copyText(this.detail.serial);
      app.toast("已复制序列号", "ok");
    }
  };
};
