/**
 * jsdom 渲染 + 交互冒烟（无需 GUI / 无需真机）
 *   NODE_PATH=<workspace>/node_modules node _dom_check.js
 *
 * 覆盖：4 个路由渲染、日志流、多层过滤、包名过滤、保存弹窗、
 *       应用列表/详情/安装、文件浏览/视图切换/删除确认
 */
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const ROOT = path.join(__dirname, "web");
const results = [];
const notes = [];
let lastSavePath = null;

function check(name, ok, extra) {
  results.push({ name, ok: !!ok, extra: extra === undefined ? "" : String(extra) });
}
const tick = (ms) => new Promise((r) => setTimeout(r, ms));

/* ------------------------------ pywebview 桥桩 ------------------------------ */
function makeStub(win) {
  let seq = 0;
  const LINES = [];
  const TEMPLATES = [
    ["D", "MainActivity", "onCreate: savedInstanceState=null", 2345, 2360],
    ["I", "OkHttp", "--> GET https://api.example.com/v1/user/profile", 2345, 2371],
    ["W", "OpenGLRenderer", "Bitmap too large to be uploaded into a texture", 2345, 2360],
    ["E", "AndroidRuntime", "FATAL EXCEPTION: main", 2345, 2345],
    ["I", "SystemServer", "StartWatching for /system/etc/protolog.conf.json", 1532, 1544],
    ["D", "RecyclerView", "onBindViewHolder position=12 type=1", 2345, 2399],
    ["E", "AndroidRuntime", "java.lang.NullPointerException: null object reference", 2345, 2345],
    ["I", "ActivityTaskManager", "Displayed com.example.sampleapp/.MainActivity: +412ms", 812, 812]
  ];
  const gen = setInterval(() => {
    for (let i = 0; i < 4; i++) {
      const t = TEMPLATES[seq % TEMPLATES.length];
      seq += 1;
      const d = new Date();
      const pad = (n, w) => String(n).padStart(w, "0");
      LINES.push({
        id: seq,
        time: `${pad(d.getMonth() + 1, 2)}-${pad(d.getDate(), 2)} ${pad(d.getHours(), 2)}:${pad(d.getMinutes(), 2)}:${pad(d.getSeconds(), 2)}.${pad(d.getMilliseconds(), 3)}`,
        pid: t[3],
        tid: t[4],
        level: t[0],
        tag: t[1],
        msg: t[2],
        raw: false
      });
    }
    if (LINES.length > 5000) LINES.splice(0, LINES.length - 5000);
  }, 120);
  win.__stubGen = gen;

  const PACKAGES = [
    ["微信", "com.tencent.mm", "8.0.49", "user", 486145228, true, 1234],
    ["Chrome", "com.android.chrome", "126.0.6478", "system", 312475648, true, 4521],
    ["抖音", "com.ss.android.ugc.aweme", "28.6.0", "third", 524288000, true, 3312],
    ["支付宝", "com.eg.android.AlipayGphone", "10.5.96", "third", 391774208, true, 2871],
    ["开发者调试工具", "com.example.sampleapp", "2.4.1", "third", 52428800, true, 2345]
  ];

  const FS = {
    "/sdcard": [["Android", true, 4096], ["DCIM", true, 4096], ["Download", true, 4096]],
    "/sdcard/Android": [["adb_logs", true, 4096], ["data", true, 4096]],
    "/sdcard/Android/adb_logs": [
      ["logcat_2024-06-24_15-32.log", false, 5033164],
      ["crash_stacktrace.txt", false, 217088],
      ["screen_20240624_1532.png", false, 1258291],
      ["app_debug_logs.zip", false, 16357785]
    ]
  };

  const tasks = {};
  let tid = 0;
  const newTask = (kind, title) => {
    tid += 1;
    const id = "T" + tid;
    tasks[id] = { id, kind, title, status: "running", phase: "处理中…", percent: 0, done: 0, total: 1, speed: "1.2 MB/s", logs: ["[00:00:01] adb push demo.apk /data/local/tmp/"], error: "", result: null };
    let p = 0;
    const iv = setInterval(() => {
      p += 25;
      tasks[id].percent = Math.min(100, p);
      tasks[id].phase = p >= 100 ? "已完成" : "处理中…";
      if (p >= 100) {
        tasks[id].status = "success";
        clearInterval(iv);
      }
    }, 120);
    return id;
  };

  return {
    refresh_env: () => ({
      appVersion: "2.4.0", adbPath: "C:\\platform-tools\\adb.exe", adbVersion: "35.0.2",
      adbFound: true, serverRunning: true, serverPort: 5037, demo: true, reason: "演示模式（冒烟测试）"
    }),
    list_devices: () => [
      { serial: "adb-R3CN10ABCDE-hG9kQ", state: "device", model: "Pixel 8 Pro", device: "husky", product: "husky", transport: "usb", transportId: "1" },
      { serial: "192.168.1.20:5555", state: "device", model: "Redmi K60", device: "mondrian", product: "mondrian", transport: "wifi", transportId: "2" }
    ],
    get_device_detail: (serial) => ({
      serial: serial || "adb-R3CN10ABCDE-hG9kQ",
      model: serial === "192.168.1.20:5555" ? "Redmi K60" : "Pixel 8 Pro", device: "husky",
      androidVersion: "14", apiLevel: "34", displayId: "AP2A.240605.024", abi: "arm64-v8a",
      kernel: "6.1.0-android14-11 #1 SMP PREEMPT", rooted: false,
      battery: { level: 87, status: "放电中" }, state: "device", props: {}
    }),
    select_device: (s) => ({ ok: true, serial: s }),
    connect_wireless: () => ({ ok: true, output: "connected" }),
    start_logcat: () => ({ ok: true, error: "" }),
    stop_logcat: () => ({ ok: true }),
    clear_logcat: () => ({ ok: true }),
    get_log_batch: (cursor, limit) => {
      const start = Math.max(0, LINES.length - (seq - cursor));
      const chunk = LINES.slice(start, start + (limit || 1500));
      return { lines: chunk, cursor: cursor + chunk.length, total: LINES.length, seq, running: true, error: "" };
    },
    get_pid_map: () => ({
      map: { 2345: "com.example.sampleapp", 1234: "com.tencent.mm", 3312: "com.ss.android.ugc.aweme", 4400: "[qdss]", 4500: "spddaemon" },
      labels: { "com.tencent.mm": "\u5fae\u4fe1", "com.ss.android.ugc.aweme": "\u6296\u97f3", "[qdss]": null, "spddaemon": null }
    }),
    pidof: (p) => ({ pid: p === "com.tencent.mm" ? 1234 : 2345 }),
    list_packages: (s, kind) => ({
      ok: true,
      packages: PACKAGES.filter((p) => kind !== "system" || p[3] === "system")
        .map((p) => ({ packageName: p[1], label: "", codePath: "/data/app/" + p[1] + "/base.apk",
                       versionName: p[2], type: p[3], sizeBytes: p[4], running: p[5], pid: p[6] }))
    }),
    get_label_status: () => ({ available: true, tool: "aapt2", kind: "aapt2", hint: "" }),
    resolve_labels: (items) => {
      const map = {};
      (items || []).forEach((it) => {
        const hit = PACKAGES.filter((p) => p[1] === it.package)[0];
        map[it.package] = hit ? hit[0] : null;
      });
      return { ok: true, labels: map };
    },
    get_config: () => ({ adbPath: "", detectedAdbPath: "C:////platform-tools////adb.exe", pageSize: 20 }),
    set_config: (patch) => ({ ok: true, config: Object.assign({ adbPath: "", detectedAdbPath: "C:////platform-tools////adb.exe" }, patch) }),
    get_app_detail: (pkg) => ({
      ok: true,
      detail: {
        packageName: pkg, label: "微信", versionName: "8.0.49", versionCode: "2400", type: "user",
        uid: "10182", targetSdk: "34", running: true, pid: 1234, enabled: true,
        appSize: 268435456, dataSize: 153092096, cacheSize: 75497472,
        granted: 8, requested: 12,
        permissions: [
          { name: "READ_CONTACTS", full: "android.permission.READ_CONTACTS", label: "读取通讯录", group: "dangerous", granted: true },
          { name: "CAMERA", full: "android.permission.CAMERA", label: "相机", group: "dangerous", granted: true },
          { name: "RECORD_AUDIO", full: "android.permission.RECORD_AUDIO", label: "录音", group: "dangerous", granted: true },
          { name: "ACCESS_BACKGROUND_LOCATION", full: "android.permission.ACCESS_BACKGROUND_LOCATION", label: "后台位置", group: "dangerous", granted: false },
          { name: "SOME_VENDOR_PERM", full: "com.samsung.SOME_VENDOR_PERM", label: "", group: "", granted: false }
        ]
      }
    }),
    get_zh_stats: () => ({
      loaded: true, dir: "D:////proj", errors: [],
      perms: { total: 368, groups: { dangerous: 41, normal: 74, signature: 259 } },
      pkgs: { total: 359, prefixFallback: 22 }
    }),
    app_action: () => ({ ok: true, output: "ok" }),
    uninstall: () => ({ taskId: newTask("uninstall", "卸载") }),
    install_apk: () => ({ taskId: newTask("install", "安装") }),
    list_dir: (p) => {
      if (p === "/cache" || p === "/data") {
        return { ok: false, path: p, entries: [], readable: false, needRoot: true,
                 error: "\u8be5\u76ee\u5f55\u9700\u8981 Root \u6743\u9650\uff0c\u666e\u901a adb \u65e0\u6cd5\u8bbf\u95ee\uff08" + p + "\uff09" };
      }
      return {
      ok: true, path: p, readable: true,
      entries: (FS[p] || []).map((e) => ({
        name: e[0], path: (p === "/" ? "" : p) + "/" + e[0], isDir: e[1], size: e[2], mtime: "2024-06-24 15:32", perm: "drwxrwx---"
      }))
      };
    },
    storage_stats: () => ({
      ok: true, categoriesReady: true, total: 137438953472, used: 127345780326, free: 10093173145,
      categories: [
        { label: "应用", bytes: 49499498086, cls: "blue" },
        { label: "图片视频", bytes: 41339060224, cls: "purple" }
      ]
    }),
    mkdir: () => ({ ok: true, path: "/sdcard/new" }),
    rename: () => ({ ok: true, path: "/sdcard/x" }),
    move: () => ({ ok: true }),
    remove: () => ({ taskId: newTask("remove", "删除") }),
    push: () => ({ taskId: newTask("push", "上传") }),
    pull: () => ({ taskId: newTask("pull", "下载") }),
    get_task: (id) => tasks[id] || { status: "not_found" },
    cancel_task: () => ({ ok: true }),
    get_action_logs: () => ({ items: [{ ts: "1", time: "15:40:12", action: "install", cmd: "adb install demo.apk" }] }),
    choose_file: () => ["C:\\tmp\\sampleapp.apk"],
    choose_files: () => ["C:\\tmp\\a.log", "C:\\tmp\\b.png"],
    choose_folder: () => "C:\\tmp\\out",
    save_file_dialog: () => ["C:\\tmp\\logcat_winforms.txt"], // WinForms SAVE_DIALOG returns array
    save_logs: (lines, p2) => { lastSavePath = p2; return { ok: true, path: p2, count: (lines || []).length }; },
    expand_local: (paths) => (paths || []).map((p2) => ({ path: p2, name: path.basename(p2), size: 1024 }))
  };
}

/* --------------------------------- 主流程 --------------------------------- */
(async () => {
  const html = fs.readFileSync(path.join(ROOT, "index.html"), "utf8");
  const scripts = [...html.matchAll(/<script src="([^"]+)"><\/script>/g)].map((m) => m[1]);
  const clean = html.replace(/<script src="[^"]+"><\/script>/g, "");
  check("index.html 声明了全部脚本", scripts.length === 8, scripts.length);

  const dom = new JSDOM(clean, {
    runScripts: "dangerously",
    url: "http://127.0.0.1:8931/index.html",
    pretendToBeVisual: true
  });
  const win = dom.window;
  const doc = win.document;

  // jsdom 无排版引擎：补尺寸桩
  const vis = (el) => {
    let e = el;
    while (e && e.style) {
      if (e.style.display === "none") return false;
      e = e.parentElement;
    }
    return true;
  };
  Object.defineProperty(win.HTMLElement.prototype, "offsetWidth", { configurable: true, get() { return vis(this) ? 800 : 0; } });
  Object.defineProperty(win.HTMLElement.prototype, "offsetHeight", { configurable: true, get() { return vis(this) ? 400 : 0; } });
  Object.defineProperty(win.HTMLElement.prototype, "clientWidth", { configurable: true, get() { return vis(this) ? 800 : 0; } });
  Object.defineProperty(win.HTMLElement.prototype, "clientHeight", { configurable: true, get() { return vis(this) ? 400 : 0; } });
  win.HTMLElement.prototype.getBoundingClientRect = function () {
    const w = vis(this) ? 800 : 0;
    return { width: w, height: vis(this) ? 400 : 0, top: 0, left: 0, right: w, bottom: 400, x: 0, y: 0 };
  };

  // 桥桩必须在脚本注入前就绪
  win.pywebview = { api: makeStub(win) };

  for (const src of scripts) {
    const code = fs.readFileSync(path.join(ROOT, src), "utf8");
    try {
      win.eval(code);
    } catch (e) {
      if (src.indexOf("layui") >= 0) notes.push("layui.js 在 jsdom 下注入失败（可选依赖，UI 只用其图标字体）");
      else check("脚本注入 " + src, false, e.message);
    }
  }
  await tick(300);

  const $ = (s) => doc.querySelector(s);
  const $$ = (s) => [...doc.querySelectorAll(s)];
  const visiblePages = () => $$("section.page").filter((p) => p.style.display !== "none");
  // 页面级查询必须限定在"当前可见 section"内，否则会跨页面选到隐藏 DOM（假 FAIL 来源）
  const q = (s) => { const p = visiblePages()[0]; return p ? p.querySelector(s) : null; };
  const qq = (s) => { const p = visiblePages()[0]; return p ? [...p.querySelectorAll(s)] : []; };
  const rowsOf = (s) => $$(s).filter((r) => !r.classList.contains("row-empty"));
  const app = () => win.AdbApp;
  const click = (el) => { el && el.click(); };
  const setInput = (el, v) => { el.value = v; el.dispatchEvent(new win.Event("input", { bubbles: true })); };
  const setSelect = (el, v) => { el.value = v; el.dispatchEvent(new win.Event("change", { bubbles: true })); };
  /* --------- 1. 冷启动渲染 --------- */
  check("根状态已挂载（window.AdbApp）", !!app());
  check("顶栏 4 个导航项", $$(".nav__item").length === 4, $$(".nav__item").length);
  check("模板无 {{ }} 残留", (doc.body.innerHTML.match(/\{\{[^}]{0,40}\}\}/g) || []).length === 0,
    JSON.stringify((doc.body.innerHTML.match(/\{\{[^}]{0,40}\}\}/g) || []).slice(0, 3)));

  await tick(700);
  check("设备列表渲染", $$(".devcard").length >= 1, $$(".devcard").length);
  check("设备详情字段 >= 6", $$(".detailgrid .dfield").length >= 6, $$(".detailgrid .dfield").length);
  check("提示条有文本", ($(".tipbar__text") || {}).textContent && $(".tipbar__text").textContent.length > 10);
  check("演示模式横幅可见", $(".demobar").style.display !== "none");
  check("当前仅 1 个可见页面", visiblePages().length === 1, visiblePages().length);
  /* --------- 2. 四个 Tab 逐个遍历 --------- */
  const navKeys = ["devices", "logs", "apps", "files"];
  for (const key of navKeys) {
    const nav = $$(".nav__item").filter((n) => n.textContent.trim() ===
      ({ devices: "设备", logs: "日志查看", apps: "应用包管理", files: "文件管理" })[key])[0];
    click(nav);
    const hashNow = win.location.hash; // jsdom 会在下一 tick 回滚 fragment 导航，必须同步读取
    await tick(900);
    const vp = visiblePages();
    const active = $$(".nav__item.is-active");
    check(`导航[${key}] hash/route/唯一可见/激活唯一`,
      hashNow === "#/" + key && app().route === key && vp.length === 1 && active.length === 1,
      `hash=${hashNow} route=${app().route} vis=${vp.length} act=${active.length}`);
  }
  /* --------- 3. 日志模块 --------- */
  click($$(".nav__item")[1]);
  await tick(1800);
  const logRows = () => rowsOf(".logtable tbody tr");
  check("日志自动开始捕获", app().logs.running === true);
  check("日志行已渲染", logRows().length > 0, logRows().length);
  const before = logRows().length;
  await tick(700);
  check("日志持续增长", logRows().length > before, `${before} -> ${logRows().length}`);

  // iOS：isIos 派生 + Cocos JS 卡片显隐（回归：打包漏 web 资源 / 平台误判时在此报警）
  check("源码含 Cocos JS 卡片且由 isIos 门控（渲染期不得引用 AdbApp 全局）",
    /Cocos JS 日志（USB）/.test(html) && html.indexOf('v-if="isIos"') >= 0);
  const savedIosList = app().devices.list;
  const savedIosSerial = app().currentSerial;
  const savedIosDetail = app().devices.detail;
  app().devices.detail = null;
  app().devices.list = [{ serial: "ios-udid-1", platform: "ios", state: "device", model: "iPhone SE" }];
  app().currentSerial = "ios-udid-1";
  await tick(400);
  check("iOS 设备时 isIos 为 true", app().isIos === true, String(app().isIos));
  check("日志页出现 Cocos JS 卡片",
    ((q(".side") || {}).textContent || "").indexOf("Cocos JS 日志（USB）") >= 0);
  app().devices.list = [{ serial: "and-1", platform: "android", state: "device", model: "Pixel" }];
  app().currentSerial = "and-1";
  await tick(400);
  check("Android 设备时 Cocos 卡片隐藏", app().isIos === false &&
    ((q(".side") || {}).textContent || "").indexOf("Cocos JS 日志（USB）") < 0, String(app().isIos));
  app().devices.list = savedIosList;
  app().currentSerial = savedIosSerial;
  app().devices.detail = savedIosDetail;
  await tick(300);

  // 级别过滤（多选下拉）
  const mddBtns = qq(".toolbar .mdd__btn");
  check("三个多选筛选按钮渲染", mddBtns.length === 3, mddBtns.length);
  click(mddBtns[0]);
  await tick(150);
  check("级别下拉展开", app().logs.dd.level === true, String(app().logs.dd.level));
  const pickLv = (txt) =>
    $$(".mdd.is-open .mdd__item").filter((i) => i.textContent.indexOf(txt) >= 0)[0];
  click(pickLv("Error").querySelector("input"));
  click(pickLv("Fatal").querySelector("input"));
  await tick(400);
  check("级别多选命中 E+F", app().logs.levels.join(",") === "E,F", app().logs.levels.join(","));
  const errRows = logRows();
  const allEF = errRows.every((r) => ["E", "F"].indexOf(r.children[1].textContent.trim()) >= 0);
  check("级别多选过滤生效且结果全为 E/F", errRows.length > 0 && errRows.length <= before && allEF,
    `rows=${errRows.length} allEF=${allEF}`);
  check("过滤计数文案出现 / 共", $(".counttext").textContent.indexOf("/ 共") >= 0, $(".counttext").textContent);
  check("过滤芯片出现", $$(".chip").length >= 1, $$(".chip").length);

  // 移除芯片
  click($$(".chip i")[0]);
  await tick(500);
  check("移除芯片后行数恢复", logRows().length >= errRows.length, logRows().length);

  // PID 多选（状态驱动，验证数字多值匹配）
  const p2 = app().logs.pidOptions.slice(0, 2);
  app().logs.pids = p2;
  await tick(400);
  const expectP = app().logs.lines.filter((l) => p2.indexOf(Number(l.pid)) >= 0).length;
  check("PID 多选过滤生效", app().logs.filteredTotal === expectP && expectP > 0,
    `${app().logs.filteredTotal}/${expectP}`);
  app().logs.pids = [];
  await tick(300);

  // 关键字
  const kwInput = q(".search input");
  setInput(kwInput, "NullPointer");
  await tick(500);
  check("关键字过滤生效", logRows().length > 0 && logRows().length < before, logRows().length);
  check("命中关键字高亮", $$("mark.hl").length > 0, $$("mark.hl").length);

  // 多关键字（空格分隔，任一命中）
  setInput(kwInput, "NullPointer FATAL");
  await tick(500);
  const expKw = app().logs.lines.filter((l) => {
    const t = (l.msg + " " + l.tag).toLowerCase();
    return t.indexOf("nullpointer") >= 0 || t.indexOf("fatal") >= 0;
  }).length;
  check("多关键字任一命中过滤", app().logs.filteredTotal === expKw && expKw > 0,
    `${app().logs.filteredTotal}/${expKw}`);
  check("多关键字高亮生效", $$("mark.hl").length > 0, $$("mark.hl").length);
  setInput(kwInput, "");
  await tick(400);

  // 冻结阅读视图：取消自动滚动后表格内容不随新日志变化
  const asCb = q(".content__hd--tight .switch input");
  click(asCb);
  await tick(300);
  const frozenLen = app().logs.frozenRows ? app().logs.frozenRows.length : -1;
  check("取消自动滚动冻结视图", frozenLen > 0, String(frozenLen));
  await tick(800); // 期间 fixture 持续产生新日志
  check("冻结期间表格不变化", logRows().length === frozenLen,
    `${logRows().length}/${frozenLen}`);
  click(asCb);
  await tick(300);
  check("恢复自动滚动丢弃快照", app().logs.frozenRows === null && app().logs.autoscroll === true);

  // 包名过滤弹窗
  const pkgBtn = qq(".toolbar .btn").filter((b) => b.textContent.indexOf("按包名过滤") >= 0)[0];
  click(pkgBtn);
  await tick(800);
  check("包名过滤弹窗打开", $(".modal--lg").style.display !== "none");
  check("运行中的应用列表非空", rowsOf(".applist .approw").length > 0, rowsOf(".applist .approw").length);
  const wxRow = rowsOf(".applist .approw").filter((r) => r.textContent.indexOf("微信") >= 0);
  check("运行中应用显示中文（词库命中）", wxRow.length === 1, wxRow.map((r) => r.textContent.trim().slice(0, 20)).join("|"));
  check("中文行保留原包名", wxRow.length === 1 && wxRow[0].textContent.indexOf("com.tencent.mm") >= 0);
  const nativeRow = rowsOf(".applist .approw").filter((r) => r.textContent.indexOf("spddaemon") >= 0);
  check("native 进程无映射回落原名", nativeRow.length === 1);
  setInput($(".modal--lg input[type=text]"), "微信");
  await tick(300);
  const kwRows = rowsOf(".applist .approw");
  check("模糊搜索命中中文名", kwRows.length === 1 && kwRows[0].textContent.indexOf("com.tencent.mm") >= 0, kwRows.length);
  setInput($(".modal--lg input[type=text]"), "");
  await tick(300);
  click($$(".applist .approw")[0]);
  await tick(200);
  const applyBtn = $$(".modal--lg .modal__ft .btn").filter((b) => b.textContent.indexOf("应用过滤") >= 0)[0];
  click(applyBtn);
  await tick(700);
  check("应用包名过滤后出现芯片", app().logs.pkg.package !== "", app().logs.pkg.package);
  click($$(".main .chip i")[0]);
  await tick(300);
  check("清除包名过滤", app().logs.pkg.package === "");

  // 保存弹窗
  const saveBtn = qq(".toolbar .btn").filter((b) => b.textContent.indexOf("保存日志") >= 0)[0];
  click(saveBtn);
  await tick(400);
  const saveModal = $$(".modal").filter((m) => m.style.display !== "none" && m.textContent.indexOf("保存日志") >= 0)[0];
  check("保存日志弹窗打开", !!saveModal);
  if (saveModal) {
    click($$(".modal .segbtn").filter((b) => b.textContent.indexOf("csv") >= 0)[0]);
    await tick(200);
    check("格式切换为 csv", app().logs.save.format === "csv", app().logs.save.format);
    const browse = $$(".modal .btn").filter((b) => b.textContent.trim() === "浏览")[0];
    click(browse);
    await tick(300);
    check("选择保存路径", app().logs.save.path !== "", app().logs.save.path);
    check("对话框数组路径归一化为字符串",
      typeof app().logs.save.path === "string" && app().logs.save.path.indexOf(".txt") > 0,
      typeof app().logs.save.path + " " + app().logs.save.path);
    click($$(".modal .btn").filter((b) => b.textContent.indexOf("保存文件") >= 0)[0]);
    await tick(600);
    check("保存完成并 toast", $(".toast").style.display !== "none" && !!$(".toast").textContent);
    check("保存请求收到字符串路径", lastSavePath !== null && typeof lastSavePath === "string", String(lastSavePath));
  }
  /* --------- 4. 应用模块 --------- */
  click($$(".nav__item")[2]);
  await tick(900);
  const appRows = () => qq(".tablewrap .table tbody tr").filter((r) => !r.classList.contains("row-empty"));
  check("应用列表渲染", appRows().length > 0, appRows().length);
  check("分类计数渲染", qq(".typelist .typeitem").length === 5, qq(".typelist .typeitem").length);
  const cnt0 = appRows().length;
  const searchApp = q(".toolbar .search input");
  setInput(searchApp, "抖音");
  await tick(500);
  check("应用搜索生效", appRows().length === 1 && appRows().length !== cnt0, appRows().length);
  setInput(searchApp, "");
  await tick(400);
  click(qq(".typelist .typeitem")[2]);
  await tick(400);
  check("类型切换（系统应用）生效", app().apps.typeFilter === "system", app().apps.typeFilter);
  click(qq(".typelist .typeitem")[0]);
  await tick(400);

  click(rowsOf(".table tbody tr")[0].querySelectorAll(".ops .btn")[1]);
  await tick(800);
  const detailModal = $$(".modal").filter((m) => m.style.display !== "none" && m.textContent.indexOf("应用详情") >= 0)[0];
  check("应用详情弹窗打开", !!detailModal);
  check("详情字段渲染", $$(".detailgrid--3 .dfield").length >= 4, $$(".detailgrid--3 .dfield").length);
  check("权限列表渲染", $$(".permgrid .perm").length >= 4, $$(".permgrid .perm").length);
  const permNames = $$(".permgrid .perm").map((e) => e.querySelector("span").textContent.trim());
  check("权限显示中文（有映射）", permNames.indexOf("读取通讯录") >= 0, permNames.join("/"));
  check("权限无映射时回落原名", permNames.indexOf("SOME_VENDOR_PERM") >= 0, permNames.join("/"));
  check("危险权限计数", app().apps.dangerPermCount === 4, app().apps.dangerPermCount);
  check("危险权限加红点标记", $$(".permgrid .perm--danger").length === 4, $$(".permgrid .perm--danger").length);
  const permTitles = $$(".permgrid .perm").map((e) => e.getAttribute("title") || "");
  check("中文权限保留原名（title）", permTitles.some((t) => t.indexOf("READ_CONTACTS") >= 0), permTitles.join("|"));
  if (detailModal) click(detailModal.querySelector(".modal__hd i"));

  const installBtn = $$(".content .btn").filter((b) => b.textContent.indexOf("安装 APK") >= 0)[0];
  click(installBtn);
  await tick(400);
  check("安装弹窗打开", app().apps.installOpen === true);
  click($$(".modal .dropzone")[0]);
  await tick(400);
  check("已选择 APK 文件", app().apps.installFile !== "", app().apps.installFile);
  click($$(".modal .btn").filter((b) => b.textContent.indexOf("开始安装") >= 0)[0]);
  await tick(1600);
  check("安装任务完成", app().apps.installTask && app().apps.installTask.status === "success",
    app().apps.installTask && app().apps.installTask.status);
  /* --------- 5. 文件模块 --------- */
  click($$(".nav__item")[3]);
  await tick(1000);
  const fileRows = () => qq(".tablewrap .table tbody tr").filter((r) => !r.classList.contains("row-empty"));
  check("文件列表渲染", fileRows().length > 0, fileRows().length);
  check("面包屑渲染", $$(".crumb").length >= 2, $$(".crumb").length);

  // 无权限目录：友好中文提示（needRoot）
  app().files.currentPath = "/cache";
  await app().files.reload(app());
  await tick(400);
  check("/cache 显示需 Root 提示", app().files.loadError.indexOf("Root") >= 0, app().files.loadError);
  app().files.currentPath = "/sdcard";
  await app().files.reload(app());
  await tick(500);
  check("返回可读目录恢复", app().files.loadError === "" && fileRows().length > 0, app().files.loadError);
  check("存储容量文本", $(".ring__num").textContent.indexOf("/") > 0, $(".ring__num").textContent);

  // 进入子目录
  click(q(".filecell"));
  await tick(700);
  check("目录跳转（面包屑变长）", $$(".crumb").length >= 3, $$(".crumb").length);

  // 视图切换：两个容器都必须有 DOM
  click(qq(".vbtn")[1]);
  await tick(400);
  check("网格视图有 DOM 且列表隐藏", app().files.view === "grid" && $$(".filegrid").length === 1 && $$(".filegrid .fcard").length > 0,
    $$(".filegrid .fcard").length);
  click(qq(".vbtn")[0]);
  await tick(300);
  check("列表视图恢复", app().files.view === "list" && fileRows().length > 0);

  // 勾选 + 删除确认
  const cb = fileRows()[0].querySelector("input[type=checkbox]");
  cb.checked = true;
  cb.dispatchEvent(new win.Event("change", { bubbles: true }));
  await tick(300);
  check("文件勾选生效", app().files.selectedCount === 1, app().files.selectedCount);
  click(qq(".filebar .btn").filter((b) => b.textContent.indexOf("批量删除") >= 0)[0]);
  await tick(400);
  const delModal = $$(".modal").filter((m) => m.style.display !== "none" && m.textContent.indexOf("擦除") >= 0)[0];
  check("删除弹窗打开", !!delModal);
  check("未输入 DELETE 时按钮禁用", !app().files.canDelete);
  const delInput = $$(".modal input[type=text]").filter((i) => i.placeholder === "DELETE")[0];
  setInput(delInput, "DELETE");
  await tick(300);
  check("输入 DELETE 后可确认", app().files.canDelete === true);
  check("展示 adb 命令", $(".cmdbox").textContent.indexOf("adb shell rm") >= 0, $(".cmdbox").textContent);
  click($$(".optradio")[1]);
  await tick(300);
  check("安全擦除切换后命令变化", app().files.deleteSecure === true && $(".cmdbox").textContent.indexOf("/dev/urandom") >= 0);
  /* --------- 6. 设备切换 --------- */
  const devSel = $(".devsel select");
  check("设备下拉有 2 台设备", devSel.options.length === 2, devSel.options.length);
  setSelect(devSel, "192.168.1.20:5555");
  await tick(800);
  check("切换设备后详情更新", app().devices.detail && app().devices.detail.model === "Redmi K60",
    app().devices.detail && app().devices.detail.model);

  /* --------- 7. 应用名解析（aapt 可选） --------- */
  click($$(".nav__item")[2]);
  await tick(1200);
  check("应用名解析状态可用", app().apps.labelStatus.available === true, app().apps.labelStatus.tool);
  const labeled = app().apps.packages.filter((p) => p.label);
  check("列表应用名已回填（非包名）", labeled.length > 0, labeled.map((p) => p.label).join("/"));
  const shown = qq(".tablewrap .appcell__name").map((e) => e.textContent.trim());
  check("表格展示应用名而非包名", shown.some((n) => n === "微信"), shown.slice(0, 3).join("/"));

  /* --------- 8. 设置弹窗 --------- */
  click($(".topbar .btn--icon"));
  await tick(500);
  const setModal = $$(".modal").filter((m) => m.style.display !== "none" && m.textContent.indexOf("设置") >= 0)[0];
  check("设置弹窗打开", !!setModal);
  if (setModal) {
    check("回显自动探测路径", app().settings.detected !== "", app().settings.detected);
    check("中文词库统计展示",
      app().zhStats.loaded === true && app().zhStats.perms.total === 368 && app().zhStats.pkgs.total === 359,
      `${app().zhStats.perms.total}/${app().zhStats.pkgs.total}`);
    const adbInput = setModal.querySelector('input[type=text]');
    setInput(adbInput, "D:////sdk////platform-tools////adb.exe");
    await tick(200);
    check("修改 adb 路径", app().settings.adbPath.indexOf("D:") === 0, app().settings.adbPath);
    click($$(".modal .btn").filter((b) => b.textContent.indexOf("保存并重新检测") >= 0)[0]);
    await tick(900);
    check("保存后关闭并重新检测", app().settings.open === false && $$(".devcard").length >= 1,
      $$(".devcard").length);
  }

  /* --------- 汇总 --------- */
  clearInterval(win.__stubGen);
  win.close();

  const pass = results.filter((r) => r.ok).length;
  const fail = results.filter((r) => !r.ok);
  console.log("\n================ DOM / 交互冒烟 ================");
  results.forEach((r) => {
    console.log(`${r.ok ? "PASS" : "FAIL"}  ${r.name}${r.extra ? "   -> " + r.extra : ""}`);
  });
  if (notes.length) {
    console.log("\n-- note --");
    notes.forEach((n) => console.log("  " + n));
  }
  console.log(`\n合计 ${results.length} 项，通过 ${pass}，失败 ${fail.length}`);
  process.exit(fail.length ? 1 : 0);
})().catch((e) => {
  console.error("RUN ERROR:", e);
  process.exit(2);
});
