/* 模块五：性能监控（CPU/内存/网络/FPS，整机或指定应用） */
window.perfComponent = function () {
  /* ECharts 实例只能放在闭包里，绝不能挂到组件 this 上——
     petite-vue 会把组件状态包成 Proxy，ECharts 实例经 Proxy 转发后
     setOption 内部 this 错乱，直角坐标系图表静默渲染失败（仪表盘
     恰好因走 CH 缓存而幸存）。 */
  var CH = {}; // chart 实例缓存
  var LAST = { samples: [], isImport: false }; // 最近一次渲染的样本（主题切换时重画用）

  /* 按当前主题取图表配色（dark / light）。
     浅色下 value/text 用足深的颜色，保证白卡片上的可读性。 */
  function palette() {
    var dark = (document.documentElement.dataset.theme || "dark") !== "light";
    return dark
      ? { text: "#8b949e", split: "#21262d", value: "#e6edf3", empty: "#6e7681",
          gauge: "#58a6ff" }
      : { text: "#57606a", split: "#eaeef2", value: "#1f2328", empty: "#8c959f",
          gauge: "#0969da" };
  }

  /* 无数据时在图表中央显示占位；有数据时移除 */
  function emptyNote(ch, hasData, pal) {
    if (!ch) return;
    ch.setOption({
      graphic: hasData ? [] : [{
        type: "text", left: "center", top: "middle", silent: true,
        style: { text: "暂无数据", fontSize: 13, fill: pal.empty }
      }]
    });
  }

  return {
    loading: false,
    running: false,
    viewingImport: "",        // 导入数据集名称；非空 = 只读回放模式
    imported: null,           // 导入的数据集
    // 采样设置
    interval: "1",            // "0.5" | "1" | "2" | "5"
    windowSec: 60,            // 图表时间窗口
    pkg: "",                  // "" = 整机；否则指定应用
    pkgList: [],
    pkgLoading: false,
    // 阈值
    thModal: false,
    thCpu: 80,
    thMem: 90,
    // 状态
    count: 0,
    latest: null,
    alarms: [],
    lastIdx: -1,
    timer: null,
    tracing: false,          // iOS 系统 trace 录制中
    tracingName: "",
    _iosErrShown: false,
    summary: { avgFps: null, maxCpu: null },
    // 扩展模块（各核心占用 / 电池状态 / 磁盘 I/O 与温度），从最近样本回填
    ex: {
      cores: [], coreCount: 0,
      batLevel: null, batTemp: null, batVolt: null, batCurrent: null,
      batPower: null, batCharging: false, batText: "",
      diskRead: null, diskWrite: null, cpuTemp: null, gpuTemp: null,
    },

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
    get isIos() { return this.curPlatform === "ios"; },
    get stateText() {
      if (this.viewingImport) return "查看导入";
      return this.running ? "运行中" : "已停止";
    },
    get alarmCount() { return this.alarms.length; },

    // ---------------- 生命周期
    ensureStarted: function (app) {
      var self = this;
      if (!this._timerStarted) {
        this._timerStarted = true;
        this.loadPkgs(app);
        this.timer = setInterval(function () { self.poll(); }, 1000);
      }
      // 进入页面立即画空态图表（坐标轴 + "暂无数据"占位），而不是等第一条采样。
      // v-show 的 section 可能当帧还未显示（clientWidth=0 时 init 会得到 0 尺寸），
      // 延后一帧再画；已有图表（本次会话停过采样）只纠正尺寸、不清空历史画面。
      setTimeout(function () {
        if (self.running || self.viewingImport) return;
        if (CH["perfCpu"]) {
          self.ensureCharts();
          return;
        }
        self.renderAll([], false);
      }, 80);
      // 主题切换时图表配色是旧主题渲染的（浅色下深色轴底/近白数值几乎不可读），
      // 监听 data-theme 变化，用最近样本按新配色整页重画
      if (!this._themeObs && typeof MutationObserver !== "undefined") {
        this._themeObs = new MutationObserver(function () {
          if (!Object.keys(CH).length) return;
          Object.keys(CH).forEach(function (id) {
            try { CH[id].dispose(); } catch (e) { /* noop */ }
            CH[id] = null;
          });
          self.renderAll(LAST.samples, LAST.isImport);
        });
        this._themeObs.observe(document.documentElement,
          { attributes: true, attributeFilter: ["data-theme"] });
      }
    },
    destroy: function () {
      if (this.timer) clearInterval(this.timer);
      this.timer = null;
      if (this._themeObs) { this._themeObs.disconnect(); this._themeObs = null; }
    },

    // ---------------- 图表
    ensureCharts: function () {
      if (typeof echarts === "undefined") return;
      var mk = function (id) {
        var el = document.getElementById(id);
        if (!el) return;
        // DOM 节点被替换时旧实例已成孤儿，销毁重建
        if (CH[id] && CH[id].getDom() !== el) {
          try { CH[id].dispose(); } catch (e) { /* noop */ }
          CH[id] = null;
        }
        if (!CH[id]) CH[id] = echarts.init(el);
        CH[id].resize();
      };
      ["perfCpu", "perfMem", "perfNet", "perfFps", "gaugeCpu", "gaugeMem"].forEach(mk);
      // 四张趋势图联动：任一图上移动鼠标，其余三张在对应时间点同步显示
      // （同 group 的实例共享十字指示线与 tooltip；gauge 不参与）
      var linked = [];
      ["perfCpu", "perfMem", "perfNet", "perfFps"].forEach(function (id) {
        if (CH[id]) { CH[id].group = "perfTrend"; linked.push(CH[id]); }
      });
      if (linked.length) echarts.connect(linked);
    },
    baseOption: function (title, seriesNames, yMax, empty) {
      var pal = palette();
      // 空态也要有完整坐标轴：y 轴给 0-100 兜底刻度（否则 max:null + 无数据
      // 时 ECharts 画不出任何刻度），x 轴用当前时间窗固定 min/max 出时间刻度
      var win = (this.windowSec || 60) * 1000;
      var now = Date.now();
      return {
        animation: false,
        title: { text: title, textStyle: { fontSize: 12, color: pal.text }, left: 10, top: 8 },
        legend: { data: seriesNames, textStyle: { color: pal.text, fontSize: 11 }, top: 8, right: 10, itemWidth: 14 },
        tooltip: {
          trigger: "axis",
          axisPointer: { type: "line", lineStyle: { color: pal.text, type: "dashed" } }
        },
        grid: { left: 46, right: 14, top: 34, bottom: 22 },
        xAxis: empty
          ? { type: "time", min: now - win, max: now,
              axisLabel: { color: pal.text }, splitLine: { show: false } }
          : { type: "time", axisLabel: { color: pal.text }, splitLine: { show: false } },
        // min 固定 0（yMax 可选），保证没有任何数据时坐标轴也完整可见
        yAxis: { type: "value", min: 0, max: yMax || (empty ? 100 : null),
                 axisLabel: { color: pal.text }, splitLine: { lineStyle: { color: pal.split } } },
        series: seriesNames.map(function (n) {
          return { name: n, type: "line", showSymbol: false, data: [] };
        })
      };
    },
    renderAll: function (samples, isImport) {
      var self = this;
      if (!samples) samples = [];
      LAST.samples = samples;
      LAST.isImport = !!isImport;
      var win = isImport ? 1e12 : (this.windowSec * 1000);
      var cutoff = samples.length ? samples[samples.length - 1].t - win : 0;
      var rows = samples.filter(function (s) { return s.t >= cutoff; });
      var t = function (v) { return v * 1000; };

      this.ensureCharts();
      var pal = palette();
      var chCpu = CH["perfCpu"], chMem = CH["perfMem"], chNet = CH["perfNet"], chFps = CH["perfFps"];
      if (chCpu) {
        chCpu.setOption(this.baseOption("CPU 占用率趋势 (%)", ["整体 CPU", "应用 CPU"], 100));
        chCpu.setOption({
          series: [
            { data: rows.filter(function (s) { return s.cpu != null; }).map(function (s) { return [t(s.t), s.cpu]; }) },
            { data: rows.filter(function (s) { return s.cpuApp != null; }).map(function (s) { return [t(s.t), s.cpuApp]; }) }
          ]
        });
        emptyNote(chCpu, rows.some(function (s) { return s.cpu != null; }), pal);
      }
      if (chMem) {
        var hasMem = rows.some(function (s) { return s.memUsed != null; });
        chMem.setOption(this.baseOption("内存占用趋势 (MB)", ["已用内存", "应用内存"], 0, !hasMem));
        chMem.setOption({
          series: [
            { data: rows.filter(function (s) { return s.memUsed != null; }).map(function (s) { return [t(s.t), s.memUsed]; }) },
            { data: rows.filter(function (s) { return s.memApp != null; }).map(function (s) { return [t(s.t), s.memApp]; }) }
          ]
        });
        emptyNote(chMem, rows.some(function (s) { return s.memUsed != null; }), pal);
      }
      if (chNet) {
        chNet.setOption(this.baseOption("网络流量速率 (KB/s)", ["下行", "上行"]));
        chNet.setOption({
          series: [
            { data: rows.filter(function (s) { return s.rx != null; }).map(function (s) { return [t(s.t), s.rx]; }) },
            { data: rows.filter(function (s) { return s.tx != null; }).map(function (s) { return [t(s.t), s.tx]; }) }
          ]
        });
        emptyNote(chNet, rows.some(function (s) { return s.rx != null || s.tx != null; }), pal);
      }
      if (chFps) {
        var hasFps = rows.some(function (s) { return s.fps != null; });
        chFps.setOption(this.baseOption("实时帧率 FPS", ["FPS"], 120, !hasFps));
        chFps.setOption({
          series: [{ data: rows.filter(function (s) { return s.fps != null; }).map(function (s) { return [t(s.t), s.fps]; }) }]
        });
        emptyNote(chFps, rows.some(function (s) { return s.fps != null; }), pal);
      }
      // 扩展模块数据：每个字段取最近一个非空值（电池/磁盘/温度是隔轮采样）
      var pick = function (key) {
        for (var i = rows.length - 1; i >= 0; i--) {
          if (rows[i][key] != null) return rows[i][key];
        }
        return null;
      };
      var coresArr = pick("cores") || [];
      this.ex = {
        cores: coresArr.map(function (p, i) { return { i: i, pct: p, hot: p >= 80 }; }),
        coreCount: coresArr.length,
        batLevel: pick("batLevel"), batTemp: pick("batTemp"),
        batVolt: pick("batVolt"), batCurrent: pick("batCurrent"),
        batPower: pick("batPower"),
        batCharging: pick("batCharging") === true,
        batText: pick("batText") || "",
        diskRead: pick("diskRead"), diskWrite: pick("diskWrite"),
        cpuTemp: pick("cpuTemp"), gpuTemp: pick("gpuTemp"),
      };
      // 仪表盘
      var last = rows[rows.length - 1] || {};
      var memPct = (last.memUsed != null && last.memTotal) ? Math.round(last.memUsed / last.memTotal * 100) : null;
      this.setGauge("gaugeCpu", last.cpu != null ? last.cpu : 0, "%");
      this.setGauge("gaugeMem", memPct != null ? memPct : 0, "%");
      // 汇总
      var fpsArr = rows.filter(function (s) { return s.fps != null; }).map(function (s) { return s.fps; });
      var cpuArr = rows.filter(function (s) { return s.cpu != null; }).map(function (s) { return s.cpu; });
      this.summary.avgFps = fpsArr.length ? Math.round(fpsArr.reduce(function (a, b) { return a + b; }, 0) / fpsArr.length * 10) / 10 : null;
      this.summary.maxCpu = cpuArr.length ? Math.max.apply(null, cpuArr) : null;
    },
    setGauge: function (id, val, unit) {
      var el = document.getElementById(id);
      if (!el || typeof echarts === "undefined") return;
      if (!CH[id]) CH[id] = echarts.init(el);
      var pal = palette();
      CH[id].setOption({
        animation: false,
        series: [{
          type: "gauge", startAngle: 210, endAngle: -30, min: 0, max: 100,
          radius: "92%", center: ["50%", "58%"],
          progress: { show: true, width: 10, itemStyle: { color: pal.gauge } },
          axisLine: { lineStyle: { width: 10, color: [[1, pal.split]] } },
          axisTick: { show: false }, splitLine: { show: false }, axisLabel: { show: false },
          pointer: { show: false },
          detail: { valueAnimation: false, fontSize: 22, color: pal.value, offsetCenter: [0, 0],
                    formatter: function (v) { return v.toFixed(0) + (unit || ""); } },
          data: [{ value: val || 0 }]
        }]
      });
    },

    // ---------------- 行为
    poll: function () {
      if (!this.running || this.viewingImport) return;
      var self = this;
      Util.call("perf_status", this.lastIdx + 1)
        .then(function (st) {
          if (!st) return;
          self.running = st.running;
          self.count = st.count;
          self.alarms = st.alarms || [];
          self.thCpu = (st.thresholds || {}).cpu || self.thCpu;
          self.thMem = (st.thresholds || {}).mem || self.thMem;
          // iOS 采集流异常（设备断开等）：提示一次并同步停止状态
          if (st.iosError && self.running && !self._iosErrShown) {
            self._iosErrShown = true;
            app.toast("iOS 采集流异常: " + st.iosError, "bad");
          }
          if (!st.iosError) self._iosErrShown = false;
          if (st.samples && st.samples.length) {
            self.lastIdx = st.samples[st.samples.length - 1].i;
            self.renderAll(st.samples, false);
            self.latest = st.samples[st.samples.length - 1];
          }
        }).catch(function () {});
    },
    startMon: function (app) {
      var self = this;
      this.viewingImport = "";
      this.lastIdx = -1;
      Util.call("perf_start", {
        interval: parseFloat(this.interval),
        package: this.pkg,
        thresholds: { cpu: this.thCpu, mem: this.thMem }
      }).then(function (r) {
        if (r && r.ok) {
          self.running = true;
          app.toast("性能采样已开始" + (self.pkg ? "（应用: " + self.pkg + "）" : "（整机）"), "ok");
        } else {
          app.toast((r && r.error) || "启动失败", "bad");
        }
      }).catch(function (e) { app.toast("启动异常: " + e.message, "bad"); });
    },
    stopMon: function (app) {
      var self = this;
      return Util.call("perf_stop").then(function (r) {
        self.running = false;
        self._iosErrShown = false;
        app.toast("已停止，共 " + ((r && r.count) || 0) + " 个采样点", "ok");
      });
    },
    // iOS 系统 trace 录制（kdebug 流，存到媒体库目录）
    toggleTrace: function (app) {
      var self = this;
      if (this.tracing) {
        Util.call("perf_trace_stop").then(function (r) {
          self.tracing = false;
          if (r && r.ok) app.toast("trace 已保存: " + r.name, "ok");
          else app.toast((r && r.error) || "停止失败", "bad");
        }).catch(function (e) { self.tracing = false; app.toast("停止异常: " + e.message, "bad"); });
      } else {
        Util.call("perf_trace_start").then(function (r) {
          if (r && r.ok) {
            self.tracing = true;
            self.tracingName = r.name || "";
            app.toast("系统 trace 录制中: " + r.name, "ok");
          } else {
            app.toast((r && r.error) || "启动失败", "bad");
          }
        }).catch(function (e) { app.toast("启动异常: " + e.message, "bad"); });
      }
    },
    reset: function (app) {
      if (this.running) { this.stopMon(app); }
      this.lastIdx = -1;
      this.count = 0;
      this.alarms = [];
      this.latest = null;
      this.summary = { avgFps: null, maxCpu: null };
      this.ex = {
        cores: [], coreCount: 0,
        batLevel: null, batTemp: null, batVolt: null, batCurrent: null,
        batPower: null, batCharging: false, batText: "",
        diskRead: null, diskWrite: null, cpuTemp: null, gpuTemp: null,
      };
      this.renderAll([], false);
      app.toast("已重置", "ok");
    },
    loadPkgs: function (app) {
      var self = this;
      this.pkgLoading = true;
      Util.call("list_packages", null, "third")
        .then(function (r) {
          self.pkgList = ((r && r.packages) || []).map(function (p) {
            return p.packageName;
          });
        })
        .catch(function () {})
        .then(function () { self.pkgLoading = false; });
    },
    saveThresholds: function (app) {
      this.thModal = false;
      if (this.running) {
        // 运行中改阈值：重启采样线程以应用新阈值
        this.startMon(app);
      }
      app.toast("阈值已保存: CPU " + this.thCpu + "% / 内存 " + this.thMem + "%", "ok");
    },
    doExport: function (app) {
      if (!this.count) { app.toast("没有可导出的采样数据", "warn"); return; }
      Util.call("perf_export").then(function (r) {
        if (r && r.ok) app.toast("已导出 " + r.count + " 条: " + r.name, "ok");
        else app.toast((r && r.error) || "导出失败", "bad");
      }).catch(function (e) { app.toast("导出异常: " + e.message, "bad"); });
    },
    doImport: function (app) {
      var self = this;
      if (this.running) this.stopMon(app);
      Util.call("perf_import").then(function (r) {
        if (!r) return;
        if (!r.ok) { if (r.error) app.toast(r.error, "bad"); return; }
        var d = r.data || {};
        var samples = (d.samples || []).map(function (s, i) {
          return Object.assign({ i: i }, s);
        });
        self.imported = r.name;
        self.viewingImport = r.name;
        self.count = samples.length;
        self.pkg = d.package || "";
        self.alarms = (d.alarms || []).map(function (a) { return a; });
        self.renderAll(samples, true);
        self.latest = samples[samples.length - 1] || null;
        app.toast("已导入 " + samples.length + " 条采样（只读回放）", "ok");
      }).catch(function (e) { app.toast("导入异常: " + e.message, "bad"); });
    },
    exitImport: function (app) {
      this.viewingImport = "";
      this.imported = null;
      this.renderAll([], false);
    }
  };
};
