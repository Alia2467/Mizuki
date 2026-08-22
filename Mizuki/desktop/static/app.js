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
    setText("pc-window", pick(state.computer, "foreground_window", "—"));
    setText("pc-process", pick(state.computer, "foreground_process", "—"));
    const flags = [];
    if (pick(state.computer, "is_gaming")) flags.push("🎮 游戏中");
    if (pick(state.computer, "is_navigating")) flags.push("🧭 导航中");
    setText("pc-flags", flags.length ? flags.join(" · ") : "正常", flags.length ? "yes" : "");

    // 服务
    setText("srv-version", pick(state.server, "version", "—"));
    setText("srv-uptime", fmtUptime(pick(state.server, "uptime_seconds")));
    setText("srv-refresh", fmtClock(new Date().toISOString()));
  }

  function renderConfig(cfg) {
    setText("cfg-port", pick(cfg, "port", "—"));
    setText("cfg-interval", pick(cfg, "computer_collect_interval", "—") + " 秒");
    setText("cfg-timeout", pick(cfg, "phone_timeout_seconds", "—") + " 秒");
    setText("cfg-auth", pick(cfg, "auth_enabled") ? "已启用" : "未启用");

    // 填充设置表单
    document.getElementById("cfg-edit-interval").value = pick(cfg, "computer_collect_interval", 5);
    document.getElementById("cfg-edit-timeout").value = pick(cfg, "phone_timeout_seconds", 90);
    document.getElementById("cfg-edit-token").value = pick(cfg, "shared_token", "");
    document.getElementById("cfg-edit-poll").value = pick(cfg, "poll_interval", 5);

    // 更新轮询间隔
    var newInterval = (pick(cfg, "poll_interval", 5)) * 1000;
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
      computer_collect_interval: parseInt(document.getElementById("cfg-edit-interval").value) || 5,
      phone_timeout_seconds: parseInt(document.getElementById("cfg-edit-timeout").value) || 90,
      shared_token: document.getElementById("cfg-edit-token").value.trim(),
      poll_interval: parseInt(document.getElementById("cfg-edit-poll").value) || 5,
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

  setTheme(isDark);
  themeBtn.addEventListener("click", function (e) {
    var rect = themeBtn.getBoundingClientRect();
    setTheme(!document.documentElement.classList.contains("dark"),
      rect.left + rect.width / 2, rect.top + rect.height / 2);
  });

  poll();
  pollConfig();
  pollLogs();
  setInterval(poll, REFRESH_MS);
  pollLogsTimer = setInterval(pollLogs, 5000);
  setInterval(pollConfig, 60000);
})();
