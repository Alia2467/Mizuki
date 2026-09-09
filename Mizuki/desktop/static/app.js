/* 控制台 — 前端逻辑 */
(function () {
  "use strict";

  const DEFAULT_POLL_MS = 5000;
  const MIN_POLL_MS = 100;
  const MAX_POLL_MS = 60000;
  const PLUGIN_POLL_MS = 3000;
  const CONFIG_POLL_MS = 60000;
  const REQUEST_TIMEOUT_MS = 10000;
  const TOKEN_KEY = "mizuki-token";
  const HW_CIRC = 264; // 2 * π * 42 ≈ 264
  const requests = new Set();
  let sessionToken = "";
  let requestEpoch = 0;
  let pollEpoch = 0;
  let isLocked = false;
  let isConnecting = false;
  let isSaving = false;
  let isExporting = false;
  let isSettingsDirty = false;
  let currentConfig = null;
  let pollMs = DEFAULT_POLL_MS;
  let historyMetric = "heart_rate";
  let historyData = [];
  let exportMessageTimer = null;

  try { sessionToken = sessionStorage.getItem(TOKEN_KEY) || ""; } catch (_) { /* 内存会话兜底 */ }

  const dashboardLoop = { run: pollDashboard, delay: () => pollMs, timer: null, isRunning: false };
  const pluginLoop = { run: pollPluginStatus, delay: () => PLUGIN_POLL_MS, timer: null, isRunning: false };
  const configLoop = { run: pollConfig, delay: () => CONFIG_POLL_MS, timer: null, isRunning: false };
  const loops = [dashboardLoop, pluginLoop, configLoop];

  function isObject(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
  }

  // 快捷取值：从嵌套对象安全读取，保留合法的 0 和 false。
  function pick(obj, path, fallback) {
    let cur = obj;
    for (const part of path.split(".")) {
      if (!isObject(cur)) return fallback;
      cur = cur[part];
    }
    return cur === undefined || cur === null || cur === "" ? fallback : cur;
  }

  function fmt(value, fallback) {
    return value === undefined || value === null || value === "" ? (fallback ?? "—") : String(value);
  }

  function number(value) {
    if (typeof value !== "number" && typeof value !== "string") return null;
    if (typeof value === "string" && !value.trim()) return null;
    const result = Number(value);
    return Number.isFinite(result) ? result : null;
  }

  function fmtNumber(value, unit) {
    const result = number(value);
    return result === null ? "—" : String(result) + (unit || "");
  }

  function fmtBool(value) {
    if (value === true) return { text: "是", cls: "yes" };
    if (value === false) return { text: "否", cls: "no" };
    return { text: "—", cls: "" };
  }

  function setText(id, text, cls) {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = text;
    el.className = "kv-val" + (cls ? " " + cls : "");
  }

  function textElement(tag, cls, text) {
    const el = document.createElement(tag);
    el.className = cls;
    el.textContent = text;
    return el;
  }

  function fmtUptime(seconds) {
    const value = number(seconds);
    if (value === null) return "—";
    const s = Math.max(0, Math.floor(value));
    const d = Math.floor(s / 86400);
    const h = Math.floor((s % 86400) / 3600);
    const m = Math.floor((s % 3600) / 60);
    if (d > 0) return `${d} 天 ${h} 小时`;
    if (h > 0) return `${h} 小时 ${m} 分`;
    return `${m} 分 ${s % 60} 秒`;
  }

  function pad(value) {
    return String(value).padStart(2, "0");
  }

  function date(value) {
    if (value === undefined || value === null || value === "") return null;
    const result = new Date(value);
    return Number.isNaN(result.getTime()) ? null : result;
  }

  function fmtClock(iso) {
    const d = date(iso);
    return d ? `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}` : fmt(iso);
  }

  function fmtLogTime(iso) {
    const d = date(iso);
    return d ? `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${fmtClock(iso)}` : fmt(iso);
  }

  function fmtShortTime(iso) {
    const d = date(iso);
    return d ? `${pad(d.getHours())}:${pad(d.getMinutes())}` : "—";
  }

  function renderHardwareGauge(suffix, pct, sub) {
    const pctEl = document.getElementById("hw-" + suffix + "-pct");
    const ringEl = document.querySelector(".hw-ring-" + suffix);
    const subEl = document.getElementById("hw-" + suffix + "-sub");
    const value = number(pct);
    const val = value === null ? null : Math.max(0, Math.min(100, Math.round(value)));
    if (pctEl) pctEl.textContent = val === null ? "—" : String(val);
    if (ringEl) {
      ringEl.style.strokeDashoffset = HW_CIRC - ((val ?? 0) / 100) * HW_CIRC;
      ringEl.setAttribute("class", "hw-ring-fill hw-ring-" + suffix + (val >= 80 ? " high" : val >= 60 ? " mid" : ""));
    }
    if (subEl) subEl.textContent = value === null ? "—" : (sub || "—");
  }

  function render(state) {
    const online = state.phone_connected === true;
    document.getElementById("status-pill").className = "status-pill " + (online ? "online" : "offline");
    document.getElementById("status-text").textContent = online ? "手机在线" : "手机离线";
    document.getElementById("state-notice").textContent = "";
    setText("conn-device", pick(state.phone, "device_id", "—"));
    setText("conn-last-seen", fmtClock(state.phone_last_seen));
    setText("loc-city", pick(state.phone, "location.city", "—"));
    setText("loc-lat", fmtNumber(pick(state.phone, "location.latitude")));
    setText("loc-lng", fmtNumber(pick(state.phone, "location.longitude")));
    setText("wth-condition", fmt(pick(state.phone, "weather.condition")));
    setText("wth-temp", fmtNumber(pick(state.phone, "weather.temperature"), " ℃"));
    setText("wth-humidity", fmtNumber(pick(state.phone, "weather.humidity"), " %"));
    setText("hlt-heart", fmtNumber(pick(state.phone, "health.heart_rate"), " bpm"));
    setText("hlt-steps", fmtNumber(pick(state.phone, "health.steps")));
    setText("hlt-sleep", fmtNumber(pick(state.phone, "health.sleep_hours"), " 小时"));
    setText("use-app", pick(state.phone, "usage.foreground_app", "—"));
    const nav = fmtBool(pick(state.phone, "usage.is_navigating"));
    setText("use-nav", nav.text, nav.cls);
    const call = fmtBool(pick(state.phone, "usage.is_calling"));
    setText("use-call", call.text, call.cls);

    const diag = pick(state.phone, "diagnostics", {});
    setText("diag-version", pick(diag, "app_version", "—"));
    setText("diag-uptime", fmtUptime(pick(diag, "running_seconds")));
    setText("diag-send", pick(diag, "send_success", 0) + " / " + pick(diag, "send_failed", 0));
    setText("diag-error", pick(diag, "last_error", "无"));
    const permLabels = { location: "定位", phone_state: "通话", usage_access: "使用访问", notification: "通知" };
    const perms = pick(diag, "permissions", {});
    const permParts = [];
    for (const key in permLabels) {
      if (perms[key] !== undefined) permParts.push(permLabels[key] + (perms[key] ? "✓" : "✗"));
    }
    setText("diag-perms", permParts.length ? permParts.join("  ") : "—");
    const warnings = pick(diag, "warnings", []);
    const hasWarnings = Array.isArray(warnings) && warnings.length > 0;
    setText("diag-warnings", hasWarnings ? warnings.join("；") : "无", hasWarnings ? "no" : "");

    setText("pc-ip", pick(state.computer, "local_ip", "—"));
    setText("pc-window", pick(state.computer, "foreground_window", "—"));
    setText("pc-process", pick(state.computer, "foreground_process", "—"));
    const isGaming = pick(state.computer, "is_gaming") === true;
    setText("pc-flags", isGaming ? "🎮 游戏中" : "正常", isGaming ? "yes" : "");
    renderHardwareGauge("cpu", pick(state.computer, "cpu_percent"));
    renderHardwareGauge("mem", pick(state.computer, "memory_percent"),
      fmtNumber(pick(state.computer, "memory_used_gb")) + " / " + fmtNumber(pick(state.computer, "memory_total_gb")) + " GB");
    renderHardwareGauge("disk", pick(state.computer, "disk_percent"),
      fmtNumber(pick(state.computer, "disk_used_gb")) + " / " + fmtNumber(pick(state.computer, "disk_total_gb")) + " GB");
    setText("srv-version", pick(state.server, "version", "—"));
    setText("srv-uptime", fmtUptime(pick(state.server, "uptime_seconds")));
    setText("srv-refresh", fmtClock(new Date().toISOString()));
    // /api/state 不含插件信息；插件在线指示只能由独立的心跳状态接口驱动。
  }

  function renderPluginStatus(plugins) {
    const online = plugins.filter((plugin) => plugin.online === true).length;
    document.getElementById("plugin-pill").className = "status-pill " + (online ? "online" : "offline");
    document.getElementById("plugin-text").textContent = online ? `插件 ${online}/${plugins.length} 在线` : "插件 离线";
  }

  function renderPluginUnknown(text) {
    document.getElementById("plugin-pill").className = "status-pill unknown";
    document.getElementById("plugin-text").textContent = text;
  }

  // SQLite 行为 {id,type,timestamp,data}，data 可为 JSON 字符串或已解析对象。
  // 同时兼容旧的展平记录；损坏行单独跳过，不影响其他记录或主轮询。
  function normalizeRecords(rows) {
    if (!Array.isArray(rows)) throw new Error("Invalid records");
    const records = [];
    for (const row of rows) {
      if (!isObject(row)) continue;
      let data = row.data === undefined ? row : row.data;
      if (typeof data === "string") {
        try { data = JSON.parse(data); } catch (_) { continue; }
      }
      if (!isObject(data)) continue;
      const type = row.type || data.type;
      if (type !== "phone" && type !== "computer") continue;
      records.push({
        type: type,
        timestamp: pick(data, "received_at", pick(row, "received_at", pick(row, "timestamp", data.timestamp))),
        data: data,
      });
    }
    return records;
  }

  function renderLogs(records) {
    const body = document.getElementById("logs-body");
    body.replaceChildren();
    document.getElementById("logs-count").textContent = records.length + " 条";
    if (!records.length) {
      body.appendChild(textElement("div", "logs-empty", "暂无记录"));
      return;
    }
    for (const record of records.slice(0, 30)) {
      const data = record.data;
      const isPhone = record.type === "phone";
      const main = isPhone ? [pick(data, "device_id", "—"), pick(data, "location.city", "—"),
        fmtNumber(pick(data, "health.steps")) + " 步", pick(data, "usage.foreground_app", "—")].join(" · ")
        : pick(data, "foreground_window", "—");
      const row = document.createElement("div");
      row.className = "log-item";
      row.appendChild(textElement("span", "log-time", fmtLogTime(record.timestamp)));
      row.appendChild(textElement("span", "log-kind " + (isPhone ? "phone" : "pc"), isPhone ? "手机" : "电脑"));
      row.appendChild(textElement("span", "log-main", main));
      body.appendChild(row);
    }
  }

  function renderHistory() {
    const barsEl = document.getElementById("history-bars");
    const labelsEl = document.getElementById("history-labels");
    const points = [];
    for (let i = historyData.length - 1; i >= 0; i--) {
      const record = historyData[i];
      if (record.type !== "phone") continue;
      const value = number(pick(record.data, "health." + historyMetric));
      if (value !== null && value >= 0) points.push({ value: value, time: fmtShortTime(record.timestamp) });
    }
    barsEl.replaceChildren();
    labelsEl.replaceChildren();
    document.getElementById("history-empty").classList.toggle("show", !points.length);
    const maxValue = Math.max(1, ...points.map((point) => point.value));
    for (const point of points) {
      const bar = document.createElement("div");
      bar.className = "history-bar" + (point.value === 0 ? " is-zero" : "");
      bar.style.height = point.value === 0 ? "2px" : Math.max(5, (point.value / maxValue) * 100) + "%";
      bar.dataset.value = String(point.value);
      bar.title = point.time + " · " + point.value;
      bar.setAttribute("aria-label", bar.title);
      barsEl.appendChild(bar);
      labelsEl.appendChild(textElement("div", "history-label", point.time));
    }
  }

  function cancelRequests(backgroundOnly) {
    if (backgroundOnly) pollEpoch++;
    else requestEpoch++;
    for (const request of requests) {
      if (!backgroundOnly || request.background) request.abort.abort();
    }
  }

  function setSessionToken(token) {
    cancelRequests(false);
    sessionToken = token;
    try {
      if (token) sessionStorage.setItem(TOKEN_KEY, token);
      else sessionStorage.removeItem(TOKEN_KEY);
    } catch (_) { /* 禁用 Web Storage 时只保留内存，不改用 localStorage/cookie */ }
  }

  function requestError(name) {
    const error = new Error(name);
    error.name = name;
    return error;
  }

  function isSilentError(error) {
    return error.name === "AuthenticationError" || error.name === "StaleRequest";
  }

  // 所有受保护请求共用此入口。令牌仅出现在 header，禁止 cookie、URL 和重定向传递。
  // 读取完响应体后再次检查会话，避免旧请求的迟到响应覆盖重新登录/改令牌后的界面。
  async function fetchApi(path, options) {
    const opts = options || {};
    const epoch = requestEpoch;
    const backgroundEpoch = pollEpoch;
    const request = { abort: new AbortController(), background: opts.background === true };
    const isStale = () => epoch !== requestEpoch || (request.background && backgroundEpoch !== pollEpoch);
    const headers = {};
    if (sessionToken) headers["X-Sensor-Token"] = sessionToken;
    if (opts.body !== undefined) headers["Content-Type"] = "application/json";
    requests.add(request);
    const timeout = setTimeout(() => request.abort.abort(), opts.timeoutMs || REQUEST_TIMEOUT_MS);
    try {
      const response = await fetch(path, {
        method: opts.method || "GET", headers: headers,
        body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
        cache: "no-store", credentials: "omit", redirect: "error", referrerPolicy: "no-referrer",
        signal: request.abort.signal,
      });
      if (isStale()) throw requestError("StaleRequest");
      if (response.status === 401) {
        const hadToken = Boolean(sessionToken);
        setSessionToken("");
        showLogin(hadToken ? "令牌无效或已失效，请重新登录。" : "此控制台已启用鉴权，请输入共享令牌。");
        throw requestError("AuthenticationError");
      }
      if (!response.ok) throw new Error("HTTP " + response.status);
      const data = opts.responseType === "blob" ? await response.blob() : await response.json();
      if (isStale()) throw requestError("StaleRequest");
      if (request.abort.signal.aborted) throw requestError("AbortError");
      return data;
    } catch (error) {
      if (error.name !== "AuthenticationError" && isStale()) throw requestError("StaleRequest");
      throw error;
    } finally {
      clearTimeout(timeout);
      requests.delete(request);
    }
  }

  function canPoll() {
    return !isLocked && !isConnecting && !isSaving && !document.hidden && currentConfig !== null;
  }

  function queuePoll(loop, delay) {
    if (!canPoll() || loop.timer !== null || loop.isRunning) return;
    loop.timer = setTimeout(async function () {
      loop.timer = null;
      if (!canPoll()) return;
      loop.isRunning = true;
      try { await loop.run(); }
      finally {
        loop.isRunning = false;
        queuePoll(loop, loop.delay());
      }
    }, delay);
  }

  function pausePolling() {
    for (const loop of loops) {
      clearTimeout(loop.timer);
      loop.timer = null;
    }
    cancelRequests(true);
  }

  function resumePolling() {
    queuePoll(dashboardLoop, 0);
    queuePoll(pluginLoop, 0);
    queuePoll(configLoop, CONFIG_POLL_MS);
  }

  function renderSession() {
    const isReady = !isLocked && currentConfig !== null;
    document.getElementById("dashboard").hidden = !isReady;
    document.getElementById("settings-btn").disabled = !isReady || isSaving;
    document.getElementById("export-toggle-btn").disabled = !isReady || isSaving;
    document.getElementById("settings-save").disabled = !isReady || isSaving;
    document.getElementById("logout-btn").hidden = !isReady || !sessionToken;
    document.getElementById("logout-btn").disabled = isSaving;
    document.getElementById("login-submit").disabled = isConnecting;
    document.getElementById("login-token").disabled = isConnecting;
    if (isReady) {
      document.getElementById("auth-status").textContent = currentConfig.auth_enabled ? "已通过令牌验证" : "当前未启用鉴权";
    }
  }

  function showLogin(message) {
    isLocked = true;
    pausePolling();
    currentConfig = null;
    historyData = [];
    render({});
    renderLogs([]);
    renderHistory();
    renderPluginUnknown("插件 状态未知");
    document.getElementById("auth-card").hidden = false;
    document.getElementById("auth-status").textContent = "请登录控制台";
    document.getElementById("auth-msg").textContent = message;
    document.getElementById("login-token").value = "";
    document.getElementById("cfg-edit-token").value = "";
    document.getElementById("cfg-clear-token").checked = false;
    document.getElementById("settings-card").style.display = "none";
    document.getElementById("card-export").style.display = "none";
    renderSession();
    if (!isConnecting) document.getElementById("login-token").focus();
  }

  function renderTokenOptions() {
    const fromEnv = currentConfig && currentConfig.token_from_env === true;
    const configured = currentConfig && currentConfig.token_configured === true;
    const clear = document.getElementById("cfg-clear-token");
    const input = document.getElementById("cfg-edit-token");
    clear.disabled = fromEnv || !configured;
    if (clear.disabled) clear.checked = false;
    input.disabled = fromEnv || clear.checked;
    if (fromEnv || clear.checked) input.value = "";
    document.getElementById("cfg-token-note").textContent = fromEnv
      ? "当前使用环境变量 MIZUKI_TOKEN，优先于配置文件；此处不能修改或清除，请在服务端修改环境变量。"
      : configured ? "已配置令牌。留空保持不变；替换后本标签页自动使用新令牌。"
        : "尚未配置令牌。留空保持不变；填写后启用鉴权。";
  }

  function renderConfig(cfg, resetForm) {
    if (!isObject(cfg)) throw new Error("Invalid config");
    currentConfig = cfg;
    setText("cfg-port", pick(cfg, "port", "—"));
    setText("cfg-interval", pick(cfg, "computer_collect_interval", "—") + " 毫秒");
    setText("cfg-timeout", pick(cfg, "phone_timeout_ms", "—") + " 毫秒");
    setText("cfg-auth", cfg.auth_enabled ? "已启用" : "未启用");
    const interval = number(cfg.poll_interval);
    const newInterval = interval === null ? DEFAULT_POLL_MS : Math.max(MIN_POLL_MS, Math.min(MAX_POLL_MS, interval));
    if (newInterval !== pollMs) {
      pollMs = newInterval;
      if (dashboardLoop.timer !== null) {
        clearTimeout(dashboardLoop.timer);
        dashboardLoop.timer = null;
        queuePoll(dashboardLoop, pollMs);
      }
    }
    if (resetForm || !isSettingsDirty) {
      document.getElementById("cfg-edit-interval").value = pick(cfg, "computer_collect_interval", "");
      document.getElementById("cfg-edit-timeout").value = pick(cfg, "phone_timeout_ms", "");
      document.getElementById("cfg-edit-poll").value = pollMs;
      document.getElementById("cfg-edit-computer-enabled").checked = pick(cfg, "computer_collect_enabled", true);
    }
    if (resetForm) {
      isSettingsDirty = false;
      document.getElementById("cfg-edit-token").value = "";
      document.getElementById("cfg-clear-token").checked = false;
    }
    renderTokenOptions();
    renderSession();
  }

  async function connect(isUserInitiated) {
    if (isConnecting || isSaving || (document.hidden && !isUserInitiated)) return;
    isConnecting = true;
    pausePolling();
    renderSession();
    try {
      const cfg = await fetchApi("/api/config", { background: !isUserInitiated });
      renderConfig(cfg, true);
      if (!cfg.auth_enabled && sessionToken) setSessionToken("");
      isLocked = false;
      document.getElementById("auth-card").hidden = true;
      document.getElementById("auth-msg").textContent = "";
      renderPluginUnknown("插件 检查中…");
    } catch (error) {
      if (!isSilentError(error)) showLogin("无法连接服务，请检查地址或网络后重试。");
    } finally {
      isConnecting = false;
      renderSession();
      if (isLocked) document.getElementById("login-token").focus();
      resumePolling();
      // 初始请求被隐藏页面取消后，快速切回时仍需完成首次配置读取。
      if (!isLocked && !currentConfig && !document.hidden) connect(false);
    }
  }

  async function pollState() {
    try {
      const state = await fetchApi("/api/state", { background: true });
      if (!isObject(state)) throw new Error("Invalid state");
      render(state);
    } catch (error) {
      if (isSilentError(error)) return;
      document.getElementById("status-pill").className = "status-pill unknown";
      document.getElementById("status-text").textContent = "状态获取失败";
      document.getElementById("state-notice").textContent = "状态刷新失败，以下内容保留上次成功获取的数据。";
    }
  }

  async function pollLogs() {
    try {
      const rows = await fetchApi("/api/logs?limit=30", { background: true });
      renderLogs(normalizeRecords(rows));
      document.getElementById("logs-status").textContent = "";
    } catch (error) {
      if (!isSilentError(error)) document.getElementById("logs-status").textContent = "日志刷新失败，保留上次记录。";
    }
  }

  async function pollHistory() {
    try {
      const rows = await fetchApi("/api/logs?record_type=phone&limit=20", { background: true });
      historyData = normalizeRecords(rows);
      renderHistory();
      document.getElementById("history-status").textContent = "";
    } catch (error) {
      if (!isSilentError(error)) document.getElementById("history-status").textContent = "历史刷新失败，保留上次记录。";
    }
  }

  async function pollDashboard() {
    await Promise.all([pollState(), pollLogs(), pollHistory()]);
  }

  async function pollConfig() {
    try {
      renderConfig(await fetchApi("/api/config", { background: true }), false);
      document.getElementById("config-status").textContent = "";
    } catch (error) {
      if (!isSilentError(error)) document.getElementById("config-status").textContent = "配置刷新失败，暂用上次配置。";
    }
  }

  async function pollPluginStatus() {
    try {
      const data = await fetchApi("/api/plugin-status", { background: true, timeoutMs: PLUGIN_POLL_MS });
      if (!isObject(data) || !Array.isArray(data.plugins) || data.plugins.some((p) => !isObject(p) || typeof p.online !== "boolean")) {
        throw new Error("Invalid plugin status");
      }
      renderPluginStatus(data.plugins);
    } catch (error) {
      if (!isSilentError(error)) renderPluginUnknown("插件 状态获取失败");
    }
  }

  function buildConfigUpdate() {
    const body = { computer_collect_enabled: document.getElementById("cfg-edit-computer-enabled").checked };
    const fields = [
      ["computer_collect_interval", "cfg-edit-interval", "采集间隔"],
      ["phone_timeout_ms", "cfg-edit-timeout", "离线判定"],
      ["poll_interval", "cfg-edit-poll", "轮询间隔"],
    ];
    for (const [key, id, label] of fields) {
      const input = document.getElementById(id);
      const value = number(input.value);
      if (value === null || !Number.isInteger(value) || value < Number(input.min) || value > Number(input.max)) {
        throw new Error(label + "必须为 " + input.min + "～" + input.max + " 毫秒的整数。");
      }
      body[key] = value;
    }
    if (currentConfig.token_from_env !== true) {
      const token = document.getElementById("cfg-edit-token").value.trim();
      if (document.getElementById("cfg-clear-token").checked && currentConfig.token_configured === true) body.shared_token = "";
      else if (token) body.shared_token = token;
    }
    return body;
  }

  async function saveSettings() {
    if (isLocked || isSaving || isConnecting || !currentConfig) return;
    const msg = document.getElementById("settings-msg");
    let body;
    try { body = buildConfigUpdate(); }
    catch (error) {
      msg.className = "settings-msg error";
      msg.textContent = error.message;
      return;
    }
    isSaving = true;
    pausePolling();
    cancelRequests(false);
    renderSession();
    msg.className = "settings-msg";
    msg.textContent = "正在保存…";
    let hasSaved = false;
    try {
      await fetchApi("/api/config", { method: "PATCH", body: body });
      hasSaved = true;
      // PATCH 完成后先切换会话，再读取配置，防止新令牌生效后被下一次 GET 登出。
      if (Object.prototype.hasOwnProperty.call(body, "shared_token")) {
        setSessionToken(body.shared_token);
        currentConfig.auth_enabled = Boolean(body.shared_token);
        currentConfig.token_configured = Boolean(body.shared_token);
      }
      for (const key of ["computer_collect_enabled", "computer_collect_interval", "phone_timeout_ms", "poll_interval"]) {
        currentConfig[key] = body[key];
      }
      renderConfig(currentConfig, true);
      renderConfig(await fetchApi("/api/config"), true);
      document.getElementById("config-status").textContent = "";
      msg.textContent = "已保存";
    } catch (error) {
      if (!isSilentError(error)) {
        msg.className = "settings-msg error";
        msg.textContent = hasSaved ? "已保存，配置暂时无法刷新。" : "保存失败，请检查连接与配置后重试。";
      }
    } finally {
      isSaving = false;
      renderSession();
      resumePolling();
    }
  }

  function showExportMsg(text, isError) {
    const msg = document.getElementById("export-msg");
    clearTimeout(exportMessageTimer);
    msg.textContent = text;
    msg.className = "export-msg" + (isError ? " error" : "");
    exportMessageTimer = setTimeout(() => { msg.textContent = ""; }, 3000);
  }

  async function download(format) {
    if (isLocked || isSaving || isExporting || !currentConfig) return;
    isExporting = true;
    const buttons = [document.getElementById("export-json"), document.getElementById("export-csv")];
    buttons.forEach((button) => { button.disabled = true; });
    let url = null;
    try {
      const blob = await fetchApi("/api/export/" + format, { responseType: "blob" });
      url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = "mizuki-data." + format;
      document.body.appendChild(link);
      try { link.click(); } finally { link.remove(); }
      showExportMsg("已开始下载 " + format.toUpperCase(), false);
    } catch (error) {
      if (!isSilentError(error)) showExportMsg("导出失败，请重试。", true);
    } finally {
      if (url) setTimeout(() => URL.revokeObjectURL(url), 1000);
      isExporting = false;
      buttons.forEach((button) => { button.disabled = false; });
    }
  }

  document.getElementById("auth-form").addEventListener("submit", function (event) {
    event.preventDefault();
    if (isConnecting || isSaving) return;
    setSessionToken(document.getElementById("login-token").value.trim());
    document.getElementById("login-token").value = "";
    document.getElementById("auth-msg").textContent = "正在验证…";
    connect(true);
  });
  document.getElementById("logout-btn").addEventListener("click", function () {
    if (isSaving) return;
    setSessionToken("");
    showLogin("已退出当前标签页；输入令牌可重新登录。");
  });
  document.getElementById("settings-save").addEventListener("click", saveSettings);
  document.querySelectorAll(".settings-form input").forEach(function (input) {
    input.addEventListener("input", () => { isSettingsDirty = true; });
    input.addEventListener("change", () => { isSettingsDirty = true; });
  });
  document.getElementById("cfg-clear-token").addEventListener("change", renderTokenOptions);
  document.getElementById("settings-btn").addEventListener("click", function () {
    const card = document.getElementById("settings-card");
    card.style.display = card.style.display === "none" ? "block" : "none";
  });
  document.getElementById("settings-close").addEventListener("click", function () {
    document.getElementById("settings-card").style.display = "none";
  });
  document.getElementById("export-toggle-btn").addEventListener("click", function () {
    const card = document.getElementById("card-export");
    card.style.display = card.style.display === "none" ? "block" : "none";
  });
  document.getElementById("export-json").addEventListener("click", () => download("json"));
  document.getElementById("export-csv").addEventListener("click", () => download("csv"));
  document.querySelectorAll(".history-tab").forEach(function (tab) {
    tab.addEventListener("click", function () {
      document.querySelectorAll(".history-tab").forEach((item) => item.classList.remove("active"));
      tab.classList.add("active");
      historyMetric = tab.dataset.metric;
      renderHistory();
    });
  });
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) pausePolling();
    else if (!isLocked && !currentConfig) connect(false);
    else if (canPoll()) {
      renderPluginUnknown("插件 检查中…");
      resumePolling();
    }
  });
  window.addEventListener("pagehide", pausePolling);
  window.addEventListener("pageshow", function () {
    if (!isLocked && !currentConfig) connect(false);
    else resumePolling();
  });

  // 夜间模式（圆形扩展动画），主题可长期保存，令牌始终仅限当前标签页。
  const themeBtn = document.getElementById("theme-btn");
  let isDark = false;
  try { isDark = localStorage.getItem("mizuki-dark") === "1"; } catch (_) { /* 默认日间模式 */ }

  function saveTheme(dark) {
    document.documentElement.classList.toggle("dark", dark);
    try { localStorage.setItem("mizuki-dark", dark ? "1" : "0"); } catch (_) { /* 仍可切换主题 */ }
    themeBtn.textContent = dark ? "☀" : "🌙";
    themeBtn.title = dark ? "日间模式" : "夜间模式";
  }

  function setTheme(dark, x, y) {
    if (x === undefined) { saveTheme(dark); return; }
    const overlay = document.createElement("div");
    overlay.className = "theme-reveal";
    overlay.style.background = dark ? "#0f1117" : "#f6f7fb";
    const size = Math.hypot(window.innerWidth, window.innerHeight) * 2;
    overlay.style.width = size + "px";
    overlay.style.height = size + "px";
    overlay.style.left = (x - size / 2) + "px";
    overlay.style.top = (y - size / 2) + "px";
    document.body.appendChild(overlay);
    void overlay.offsetWidth;
    overlay.classList.add("active");
    setTimeout(function () {
      saveTheme(dark);
      overlay.style.transition = "opacity 0.3s ease";
      overlay.style.opacity = "0";
      setTimeout(() => overlay.remove(), 300);
    }, 400);
  }

  setTheme(isDark);
  themeBtn.addEventListener("click", function () {
    const rect = themeBtn.getBoundingClientRect();
    setTheme(!document.documentElement.classList.contains("dark"), rect.left + rect.width / 2, rect.top + rect.height / 2);
  });
  connect(false);
})();
