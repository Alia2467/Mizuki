/* 控制台 — 前端逻辑 */
(function () {
  "use strict";

  const REFRESH_MS = 3000;
  var pollLogsTimer = null;
  var pollTimer = null;

  // 快捷取值：从嵌套对象安全读取
  function pick(obj, path, fallback) {
    const parts = path.split(".");
    let cur = obj;
    for (const p of parts) {
      if (cur == null || typeof cur !== "object") return fallback;
      cur = cur[p];
    }
    return cur === undefined || cur === null || cur === "" ? fallback : cur;
  }

  function fmt(val, fallback) {
    if (val === undefined || val === null || val === "") return fallback ?? "—";
    return String(val);
  }

  function fmtBool(val) {
    if (val === true) return { text: "是", cls: "yes" };
    if (val === false) return { text: "否", cls: "no" };
    return { text: "—", cls: "" };
  }

  function fmtNumber(val) {
    if (val === undefined || val === null || val === "" || val === 0) return "—";
    return String(val);
  }

  // 硬件仪表盘：通过 SVG stroke-dasharray 渲染环形进度
  const HW_CIRC = 264; // 2 * π * 42 ≈ 264

  function renderHardwareGauge(suffix, pct, sub) {
    const pctEl = document.getElementById("hw-" + suffix + "-pct");
    const ringEl = document.querySelector(".hw-ring-" + suffix);
    if (!pctEl) return;
    if (pct === undefined || pct === null || pct === 0) {
      pctEl.textContent = "—";
      if (ringEl) { ringEl.style.strokeDashoffset = HW_CIRC; ringEl.className.baseVal = "hw-ring-fill hw-ring-" + suffix; }
      const subEl = document.getElementById("hw-" + suffix + "-sub");
      if (subEl) subEl.textContent = "—";
      return;
    }
    const val = Math.max(0, Math.min(100, parseInt(pct) || 0));
    pctEl.textContent = val;
    const offset = HW_CIRC - (val / 100) * HW_CIRC;
    if (ringEl) {
      ringEl.style.strokeDashoffset = offset;
      const cls = "hw-ring-fill hw-ring-" + suffix + (val >= 80 ? " high" : val >= 60 ? " mid" : "");
      ringEl.className.baseVal = cls;
    }
    if (sub) {
      const subEl = document.getElementById("hw-" + suffix + "-sub");
      if (subEl) subEl.textContent = sub;
    }
  }

  function setText(id, text, cls) {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = text;
    el.className = "kv-val" + (cls ? " " + cls : "");
  }

  function fmtUptime(seconds) {
    if (!seconds && seconds !== 0) return "—";
    const s = Math.max(0, Math.floor(seconds));
    const d = Math.floor(s / 86400);
    const h = Math.floor((s % 86400) / 3600);
    const m = Math.floor((s % 3600) / 60);
    if (d > 0) return `${d} 天 ${h} 小时`;
    if (h > 0) return `${h} 小时 ${m} 分`;
    return `${m} 分 ${s % 60} 秒`;
  }

  function fmtClock(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    const pad = (n) => String(n).padStart(2, "0");
    return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  }

  function fmtLogTime(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    const pad = (n) => String(n).padStart(2, "0");
    return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  }

  function render(state) {
    // 手机连接
    const online = !!state.phone_connected;
    const pill = document.getElementById("status-pill");
    pill.className = "status-pill " + (online ? "online" : "offline");
    document.getElementById("status-text").textContent = online ? "手机在线" : "手机离线";

    setText("conn-device", pick(state.phone, "device_id", "—"));
    setText("conn-last-seen", state.phone_last_seen ? fmtClock(state.phone_last_seen) : "—");

    // 位置
    setText("loc-city", pick(state.phone, "location.city", "—"));
    setText("loc-lat", fmtNumber(pick(state.phone, "location.latitude")));
    setText("loc-lng", fmtNumber(pick(state.phone, "location.longitude")));

    // 天气
    setText("wth-condition", fmt(pick(state.phone, "weather.condition", "—")));
    setText("wth-temp", pick(state.phone, "weather.temperature") ? pick(state.phone, "weather.temperature") + " ℃" : "—");
    setText("wth-humidity", pick(state.phone, "weather.humidity") ? pick(state.phone, "weather.humidity") + " %" : "—");

    // 健康
    setText("hlt-heart", pick(state.phone, "health.heart_rate") ? pick(state.phone, "health.heart_rate") + " bpm" : "—");
    setText("hlt-steps", fmtNumber(pick(state.phone, "health.steps")));
    setText("hlt-sleep", pick(state.phone, "health.sleep_hours") ? pick(state.phone, "health.sleep_hours") + " 小时" : "—");

    // 手机使用
    setText("use-app", pick(state.phone, "usage.foreground_app", "—"));
    const nav = fmtBool(pick(state.phone, "usage.is_navigating"));
    setText("use-nav", nav.text, nav.cls);
    const call = fmtBool(pick(state.phone, "usage.is_calling"));
    setText("use-call", call.text, call.cls);

    // 手机诊断
    const diag = pick(state.phone, "diagnostics", {}) || {};
    setText("diag-version", pick(diag, "app_version", "—"));
    setText("diag-uptime", diag.running_seconds ? fmtUptime(diag.running_seconds) : "—");
    setText("diag-send", pick(diag, "send_success", 0) + " / " + pick(diag, "send_failed", 0));
    setText("diag-error", pick(diag, "last_error", "无"));
    const permLabels = { location: "定位", phone_state: "通话", usage_access: "使用访问", notification: "通知" };
    const perms = pick(diag, "permissions", {}) || {};
    const permParts = [];
    for (const k in permLabels) {
      if (perms[k] === undefined) continue;
      permParts.push(permLabels[k] + (perms[k] ? "✓" : "✗"));
    }
    setText("diag-perms", permParts.length ? permParts.join("  ") : "—");
    const warnings = pick(diag, "warnings", []) || [];
    setText("diag-warnings", warnings.length ? warnings.join("；") : "无", warnings.length ? "no" : "");

    // 电脑状态
    setText("pc-ip", pick(state.computer, "local_ip", "—"));
    setText("pc-window", pick(state.computer, "foreground_window", "—"));
    setText("pc-process", pick(state.computer, "foreground_process", "—"));
    const flags = [];
    if (pick(state.computer, "is_gaming")) flags.push("🎮 游戏中");
    setText("pc-flags", flags.length ? flags.join(" · ") : "正常", flags.length ? "yes" : "");

    // 系统硬件
    renderHardwareGauge("cpu", pick(state.computer, "cpu_percent"));
    renderHardwareGauge("mem", pick(state.computer, "memory_percent"),
      pick(state.computer, "memory_used_gb") + " / " + pick(state.computer, "memory_total_gb") + " GB");
    renderHardwareGauge("disk", pick(state.computer, "disk_percent"),
      pick(state.computer, "disk_used_gb") + " / " + pick(state.computer, "disk_total_gb") + " GB");

    // 服务
    setText("srv-version", pick(state.server, "version", "—"));
    setText("srv-uptime", fmtUptime(pick(state.server, "uptime_seconds")));
    setText("srv-refresh", fmtClock(new Date().toISOString()));

    // 插件连接状态
    renderPluginStatus(state.plugins);
  }

  function renderPluginStatus(plugins) {
    var pill = document.getElementById("plugin-pill");
    var text = document.getElementById("plugin-text");
    if (!plugins || !plugins.length) {
      pill.className = "status-pill offline";
      text.textContent = "插件 离线";
      return;
    }
    var online = plugins.filter(function (p) { return p.online; }).length;
    var total = plugins.length;
    if (online > 0) {
      pill.className = "status-pill online";
      text.textContent = "插件 " + online + "/" + total + " 在线";
    } else {
      pill.className = "status-pill offline";
      text.textContent = "插件 离线";
    }
  }

  function renderConfig(cfg) {
    setText("cfg-port", pick(cfg, "port", "—"));
    setText("cfg-interval", pick(cfg, "computer_collect_interval", "—") + " 毫秒");
    setText("cfg-timeout", pick(cfg, "phone_timeout_ms", "—") + " 毫秒");
    setText("cfg-auth", pick(cfg, "auth_enabled") ? "已启用" : "未启用");

    // 填充设置表单
    document.getElementById("cfg-edit-interval").value = pick(cfg, "computer_collect_interval", 5000);
    document.getElementById("cfg-edit-timeout").value = pick(cfg, "phone_timeout_ms", 90000);
    document.getElementById("cfg-edit-token").value = pick(cfg, "shared_token", "");
    document.getElementById("cfg-edit-poll").value = pick(cfg, "poll_interval", 5000);
    document.getElementById("cfg-edit-computer-enabled").checked = pick(cfg, "computer_collect_enabled", true);

    // 更新轮询间隔（poll_interval 已为毫秒，无需转换）
    var newInterval = pick(cfg, "poll_interval", 5000);
    if (pollLogsTimer) clearInterval(pollLogsTimer);
    pollLogsTimer = setInterval(pollLogs, newInterval);
  }

  function renderLogs(records) {
    const body = document.getElementById("logs-body");
    const count = document.getElementById("logs-count");
    if (!records || !records.length) {
      body.innerHTML = '<div class="logs-empty">暂无记录</div>';
      count.textContent = "0 条";
      return;
    }
    count.textContent = records.length + " 条";
    body.innerHTML = records.slice(0, 30).map(function (r) {
      const time = fmtLogTime(r.received_at || r.timestamp);
      if (r.type === "phone") {
        const device = pick(r, "device_id", "—");
        const city = pick(r, "location.city", "—");
        const steps = pick(r, "health.steps", 0);
        const app = pick(r, "usage.foreground_app", "—");
        return '<div class="log-item"><span class="log-time">' + time + '</span>' +
          '<span class="log-kind phone">手机</span>' +
          '<span class="log-main">' + device + ' · ' + city + ' · ' + steps + ' 步 · ' + app + '</span></div>';
      }
      const win = pick(r, "foreground_window", "—");
      return '<div class="log-item"><span class="log-time">' + time + '</span>' +
        '<span class="log-kind pc">电脑</span>' +
        '<span class="log-main">' + win + '</span></div>';
    }).join("");
  }

  async function poll() {
    try {
      const resp = await fetch("/api/state", { cache: "no-store" });
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      render(await resp.json());
    } catch (err) {
      const pill = document.getElementById("status-pill");
      pill.className = "status-pill offline";
      document.getElementById("status-text").textContent = "连接服务失败";
    }
  }

  async function pollConfig() {
    try {
      const resp = await fetch("/api/config", { cache: "no-store" });
      if (resp.ok) renderConfig(await resp.json());
    } catch (err) { /* 忽略 */ }
  }

  async function pollLogs() {
    try {
      const resp = await fetch("/api/logs?limit=30", { cache: "no-store" });
      if (resp.ok) renderLogs(await resp.json());
    } catch (err) { /* 忽略 */ }
  }

  // 历史数据图表
  var historyMetric = "heart_rate";
  var historyData = [];

  async function pollHistory() {
    try {
      const resp = await fetch("/api/logs?record_type=phone&limit=20", { cache: "no-store" });
      if (resp.ok) {
        historyData = await resp.json();
        renderHistory();
      }
    } catch (err) { /* 忽略 */ }
  }

  function renderHistory() {
    var barsEl = document.getElementById("history-bars");
    var labelsEl = document.getElementById("history-labels");
    var emptyEl = document.getElementById("history-empty");
    if (!historyData || !historyData.length) {
      barsEl.innerHTML = "";
      labelsEl.innerHTML = "";
      emptyEl.classList.add("show");
      return;
    }
    emptyEl.classList.remove("show");
    // 提取指标值
    var values = [];
    var labels = [];
    for (var i = historyData.length - 1; i >= 0; i--) {
      var d = historyData[i].data || JSON.parse(historyData[i].data);
      var val = 0;
      if (historyMetric === "heart_rate") {
        val = d.health && d.health.heart_rate ? d.health.heart_rate : 0;
      } else if (historyMetric === "steps") {
        val = d.health && d.health.steps ? d.health.steps : 0;
      }
      if (val > 0) {
        values.push(val);
        labels.push(fmtShortTime(historyData[i].received_at || historyData[i].timestamp));
      }
    }
    if (!values.length) {
      barsEl.innerHTML = "";
      labelsEl.innerHTML = "";
      emptyEl.classList.add("show");
      return;
    }
    var maxVal = Math.max.apply(null, values);
    // 渲染柱状图
    barsEl.innerHTML = values.map(function (v) {
      var pct = Math.max(5, (v / maxVal) * 100);
      return '<div class="history-bar" style="height:' + pct + '%" data-value="' + v + '"></div>';
    }).join("");
    labelsEl.innerHTML = labels.map(function (l) {
      return '<div class="history-label">' + l + '</div>';
    }).join("");
  }

  function fmtShortTime(iso) {
    if (!iso) return "";
    var d = new Date(iso);
    if (isNaN(d.getTime())) return "";
    return pad(d.getHours()) + ":" + pad(d.getMinutes());
  }

  // 历史数据标签切换
  document.querySelectorAll(".history-tab").forEach(function (tab) {
    tab.addEventListener("click", function () {
      document.querySelectorAll(".history-tab").forEach(function (t) { t.classList.remove("active"); });
      tab.classList.add("active");
      historyMetric = tab.dataset.metric;
      renderHistory();
    });
  });

  // 数据导出
  document.getElementById("export-json").addEventListener("click", function () {
    window.location.href = "/api/export/json";
    showExportMsg("已开始下载 JSON");
  });
  document.getElementById("export-csv").addEventListener("click", function () {
    window.location.href = "/api/export/csv";
    showExportMsg("已开始下载 CSV");
  });

  // 顶栏导出按钮：切换导出卡片显示
  var exportCard = document.getElementById("card-export");
  document.getElementById("export-toggle-btn").addEventListener("click", function () {
    exportCard.style.display = exportCard.style.display === "none" ? "block" : "none";
  });

  function showExportMsg(text) {
    var msg = document.getElementById("export-msg");
    msg.textContent = text;
    setTimeout(function () { msg.textContent = ""; }, 3000);
  }

  // 设置面板
  var settingsCard = document.getElementById("settings-card");
  document.getElementById("settings-btn").addEventListener("click", function () {
    settingsCard.style.display = settingsCard.style.display === "none" ? "block" : "none";
  });
  document.getElementById("settings-close").addEventListener("click", function () {
    settingsCard.style.display = "none";
  });
  document.getElementById("settings-save").addEventListener("click", async function () {
    var msg = document.getElementById("settings-msg");
    var body = {
      computer_collect_enabled: document.getElementById("cfg-edit-computer-enabled").checked,
      computer_collect_interval: parseInt(document.getElementById("cfg-edit-interval").value) || 5000,
      phone_timeout_ms: parseInt(document.getElementById("cfg-edit-timeout").value) || 90000,
      shared_token: document.getElementById("cfg-edit-token").value.trim(),
      poll_interval: parseInt(document.getElementById("cfg-edit-poll").value) || 5000,
    };
    try {
      var resp = await fetch("/api/config", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (resp.ok) {
        msg.className = "settings-msg";
        msg.textContent = "已保存";
        pollConfig();
      } else {
        msg.className = "settings-msg error";
        msg.textContent = "保存失败";
      }
    } catch (err) {
      msg.className = "settings-msg error";
      msg.textContent = "网络错误";
    }
    setTimeout(function () { msg.textContent = ""; }, 3000);
  });

  // 夜间模式（圆形扩展动画）
  var themeBtn = document.getElementById("theme-btn");
  var isDark = localStorage.getItem("mizuki-dark") === "1";

  function setTheme(dark, x, y) {
    var html = document.documentElement;
    if (x !== undefined) {
      var overlay = document.createElement("div");
      overlay.className = "theme-reveal";
      overlay.style.background = dark ? "#0f1117" : "#f6f7fb";
      var size = Math.sqrt(window.innerWidth * window.innerWidth + window.innerHeight * window.innerHeight) * 2;
      overlay.style.width = size + "px";
      overlay.style.height = size + "px";
      overlay.style.left = (x - size / 2) + "px";
      overlay.style.top = (y - size / 2) + "px";
      document.body.appendChild(overlay);
      void overlay.offsetWidth;
      overlay.classList.add("active");
      // 展开到一半时切换主题，此时覆盖层已遮住整个屏幕
      setTimeout(function () {
        html.classList.toggle("dark", dark);
        localStorage.setItem("mizuki-dark", dark ? "1" : "0");
        themeBtn.textContent = dark ? "☀" : "🌙";
        themeBtn.title = dark ? "日间模式" : "夜间模式";
        // 淡出覆盖层
        overlay.style.transition = "opacity 0.3s ease";
        overlay.style.opacity = "0";
        setTimeout(function () { overlay.remove(); }, 300);
      }, 400);
    } else {
      html.classList.toggle("dark", dark);
      localStorage.setItem("mizuki-dark", dark ? "1" : "0");
      themeBtn.textContent = dark ? "☀" : "🌙";
      themeBtn.title = dark ? "日间模式" : "夜间模式";
    }
  }

  async function pollPluginStatus() {
    try {
      const resp = await fetch("/api/plugin-status", { cache: "no-store" });
      if (resp.ok) renderPluginStatus((await resp.json()).plugins);
    } catch (err) { /* 忽略 */ }
  }

  setTheme(isDark);
  themeBtn.addEventListener("click", function (e) {
    var rect = themeBtn.getBoundingClientRect();
    setTheme(!document.documentElement.classList.contains("dark"),
      rect.left + rect.width / 2, rect.top + rect.height / 2);
  });

  poll();
  pollConfig();
  pollLogs();
  pollPluginStatus();
  pollHistory();
  setInterval(poll, REFRESH_MS);
  pollLogsTimer = setInterval(pollLogs, 5000);
  setInterval(pollConfig, 60000);
  setInterval(pollPluginStatus, 10000);
  setInterval(pollHistory, 30000);
})();
