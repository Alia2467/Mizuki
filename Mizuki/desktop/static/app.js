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

  // DOM 引用集中缓存，初始化时一次性查询，避免轮询中重复查找。
  const dom = {};
  const cacheDom = (ids) => {
    for (const id of ids) {
      dom[id] = document.getElementById(id);
    }
  };
  cacheDom([
    "auth-card", "auth-form", "auth-msg", "auth-status",
    "card-export", "cfg-clear-token", "cfg-edit-computer-enabled",
    "cfg-edit-interval", "cfg-edit-poll", "cfg-edit-timeout",
    "cfg-edit-token", "cfg-port", "cfg-token-note", "config-status",
    "dashboard", "export-csv", "export-json", "export-msg",
    "export-toggle-btn", "history-bars", "history-empty", "history-labels",
    "history-status", "hw-cpu-pct", "hw-cpu-sub", "hw-mem-pct",
    "hw-mem-sub", "hw-disk-pct", "hw-disk-sub", "login-submit",
    "login-token", "logs-body", "logs-count", "logs-status",
    "logout-btn", "plugin-pill", "plugin-text", "settings-btn",
    "settings-card", "settings-close", "settings-msg", "settings-save",
    "status-pill", "status-text", "state-notice", "theme-btn",
    "conn-device", "conn-last-seen", "loc-city", "loc-lat", "loc-lng",
    "wth-condition", "wth-temp", "wth-humidity", "hlt-heart", "hlt-steps",
    "hlt-sleep", "use-app", "use-nav", "use-call", "diag-version",
    "diag-uptime", "diag-send", "diag-error", "diag-perms", "diag-warnings",
    "pc-ip", "pc-window", "pc-process", "pc-flags",
    "srv-version", "srv-uptime", "srv-refresh",
  ]);

  const dashboardLoop = { run: pollDashboard, delay: () => pollMs, timer: null, isRunning: false };
  const pluginLoop = { run: pollPluginStatus, delay: () => PLUGIN_POLL_MS, timer: null, isRunning: false };
  const configLoop = { run: pollConfig, delay: () => CONFIG_POLL_MS, timer: null, isRunning: false };
  const loops = [dashboardLoop, pluginLoop, configLoop];

  // 工具函数

  const isObject = (value) => value !== null && typeof value === "object" && !Array.isArray(value);

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
    const node = dom[id];
    if (!node) return;
    const next = "kv-val" + (cls ? " " + cls : "");
    if (node.textContent !== text || node.className !== next) {
      node.textContent = text;
      node.className = next;
    }
  }

  function setClass(id, cls) {
    const node = dom[id];
    if (node && node.className !== cls) node.className = cls;
  }

  function setTextIfChanged(id, text) {
    const node = dom[id];
    if (node && node.textContent !== text) node.textContent = text;
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

  function date(iso) {
    if (iso === undefined || iso === null || iso === "") return null;
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? null : d;
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

  // 渲染函数

  function renderHardwareGauge(suffix, pct, sub) {
    const pctEl = dom["hw-" + suffix + "-pct"];
    const ringEl = document.querySelector(".hw-ring-" + suffix);
    const subEl = dom["hw-" + suffix + "-sub"];
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
    setClass("status-pill", "status-pill " + (online ? "online" : "offline"));
    setTextIfChanged("status-text", online ? "手机在线" : "手机离线");
    setTextIfChanged("state-notice", "");
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
    setText("pc-flags", pick(state.computer, "is_gaming") === true ? "🎮 游戏中" : "正常",
      pick(state.computer, "is_gaming") === true ? "yes" : "");
    renderHardwareGauge("cpu", pick(state.computer, "cpu_percent"));
    renderHardwareGauge("mem", pick(state.computer, "memory_percent"),
      fmtNumber(pick(state.computer, "memory_used_gb")) + " / " + fmtNumber(pick(state.computer, "memory_total_gb")) + " GB");
    renderHardwareGauge("disk", pick(state.computer, "disk_percent"),
      fmtNumber(pick(state.computer, "disk_used_gb")) + " / " + fmtNumber(pick(state.computer, "disk_total_gb")) + " GB");
    setText("srv-version", pick(state.server, "version", "—"));
    setText("srv-uptime", fmtUptime(pick(state.server, "uptime_seconds")));
    setText("srv-refresh", fmtClock(new Date().toISOString()));
  }

  function renderPluginStatus(plugins) {
    const online = plugins.filter((p) => p.online === true).length;
    setClass("plugin-pill", "status-pill " + (online ? "online" : "offline"));
    setTextIfChanged("plugin-text", online ? `插件 ${online}/${plugins.length} 在线` : "插件 离线");
  }

  function renderPluginUnknown(text) {
    setClass("plugin-pill", "status-pill unknown");
    setTextIfChanged("plugin-text", text);
  }

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
        type,
        timestamp: pick(data, "received_at", pick(row, "received_at", pick(row, "timestamp", data.timestamp))),
        data,
      });
    }
    return records;
  }

  function renderLogs(records) {
    dom["logs-body"].replaceChildren();
    setTextIfChanged("logs-count", records.length + " 条");
    if (!records.length) {
      dom["logs-body"].appendChild(textElement("div", "logs-empty", "暂无记录"));
      return;
    }
    for (const record of records.slice(0, 30)) {
      const data = record.data;
      const isPhone = record.type === "phone";
      const main = isPhone ? [
        pick(data, "device_id", "—"),
        pick(data, "location.city", "—"),
        fmtNumber(pick(data, "health.steps")) + " 步",
        pick(data, "usage.foreground_app", "—"),
      ].join(" · ") : pick(data, "foreground_window", "—");
      const row = document.createElement("div");
      row.className = "log-item";
      row.appendChild(textElement("span", "log-time", fmtLogTime(record.timestamp)));
      row.appendChild(textElement("span", "log-kind " + (isPhone ? "phone" : "pc"), isPhone ? "手机" : "电脑"));
      row.appendChild(textElement("span", "log-main", main));
      dom["logs-body"].appendChild(row);
    }
  }

  function renderHistory() {
    const points = [];
    for (let i = historyData.length - 1; i >= 0; i--) {
      const record = historyData[i];
      if (record.type !== "phone") continue;
      const value = number(pick(record.data, "health." + historyMetric));
      if (value !== null && value >= 0) points.push({ value, time: fmtShortTime(record.timestamp) });
    }
    dom["history-bars"].replaceChildren();
    dom["history-labels"].replaceChildren();
    dom["history-empty"].classList.toggle("show", !points.length);
    const maxValue = Math.max(1, ...points.map((p) => p.value));
    for (const point of points) {
      const bar = document.createElement("div");
      bar.className = "history-bar" + (point.value === 0 ? " is-zero" : "");
      bar.style.height = point.value === 0 ? "2px" : Math.max(5, (point.value / maxValue) * 100) + "%";
      bar.dataset.value = String(point.value);
      bar.title = point.time + " · " + point.value;
      bar.setAttribute("aria-label", bar.title);
      dom["history-bars"].appendChild(bar);
      dom["history-labels"].appendChild(textElement("div", "history-label", point.time));
    }
  }

  // 请求管理

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
        method: opts.method || "GET",
        headers,
        body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
        cache: "no-store",
        credentials: "omit",
        redirect: "error",
        referrerPolicy: "no-referrer",
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

  // 轮询调度

  function canPoll() {
    return !isLocked && !isConnecting && !isSaving && !document.hidden && currentConfig !== null;
  }

  function queuePoll(loop, delay) {
    if (!canPoll() || loop.timer !== null || loop.isRunning) return;
    loop.timer = setTimeout(async function () {
      loop.timer = null;
      if (!canPoll()) return;
      loop.isRunning = true;
      try { await loop.run(); } finally {
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

  // 会话状态

  function renderSession() {
    const isReady = !isLocked && currentConfig !== null;
    dom["dashboard"].hidden = !isReady;
    dom["settings-btn"].disabled = !isReady || isSaving;
    dom["export-toggle-btn"].disabled = !isReady || isSaving;
    dom["settings-save"].disabled = !isReady || isSaving;
    dom["logout-btn"].hidden = !isReady || !sessionToken;
    dom["logout-btn"].disabled = isSaving;
    dom["login-submit"].disabled = isConnecting;
    dom["login-token"].disabled = isConnecting;
    if (isReady) {
      setTextIfChanged("auth-status", currentConfig.auth_enabled ? "已通过令牌验证" : "当前未启用鉴权");
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
    dom["auth-card"].hidden = false;
    setTextIfChanged("auth-status", "请登录控制台");
    setTextIfChanged("auth-msg", message);
    if (dom["login-token"]) dom["login-token"].value = "";
    if (dom["cfg-edit-token"]) dom["cfg-edit-token"].value = "";
    if (dom["cfg-clear-token"]) dom["cfg-clear-token"].checked = false;
    dom["settings-card"].style.display = "none";
    dom["card-export"].style.display = "none";
    renderSession();
    if (!isConnecting) dom["login-token"]?.focus();
  }

  function renderTokenOptions() {
    const fromEnv = currentConfig && currentConfig.token_from_env === true;
    const configured = currentConfig && currentConfig.token_configured === true;
    if (dom["cfg-clear-token"]) {
      dom["cfg-clear-token"].disabled = fromEnv || !configured;
      if (fromEnv || !configured) dom["cfg-clear-token"].checked = false;
    }
    if (dom["cfg-edit-token"]) {
      dom["cfg-edit-token"].disabled = fromEnv || dom["cfg-clear-token"]?.checked;
      if ((fromEnv || dom["cfg-clear-token"]?.checked) && dom["cfg-edit-token"]) dom["cfg-edit-token"].value = "";
    }
    setTextIfChanged("cfg-token-note", fromEnv
      ? "当前使用环境变量 MIZUKI_TOKEN，优先于配置文件；此处不能修改或清除，请在服务端修改环境变量。"
      : configured ? "已配置令牌。留空保持不变；替换后本标签页自动使用新令牌。"
        : "尚未配置令牌。留空保持不变；填写后启用鉴权。");
  }

  function renderConfig(cfg, resetForm) {
    if (!isObject(cfg)) throw new Error("Invalid config");
    currentConfig = cfg;
    setText("cfg-port", pick(cfg, "port", "—"));
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
      if (dom["cfg-edit-interval"]) dom["cfg-edit-interval"].value = pick(cfg, "computer_collect_interval", "");
      if (dom["cfg-edit-timeout"]) dom["cfg-edit-timeout"].value = pick(cfg, "phone_timeout_ms", "");
      if (dom["cfg-edit-poll"]) dom["cfg-edit-poll"].value = pollMs;
      if (dom["cfg-edit-computer-enabled"]) dom["cfg-edit-computer-enabled"].checked = pick(cfg, "computer_collect_enabled", true);
    }
    if (resetForm) {
      isSettingsDirty = false;
      if (dom["cfg-edit-token"]) dom["cfg-edit-token"].value = "";
      if (dom["cfg-clear-token"]) dom["cfg-clear-token"].checked = false;
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
      dom["auth-card"].hidden = true;
      setTextIfChanged("auth-msg", "");
      renderPluginUnknown("插件 检查中…");
    } catch (error) {
      if (!isSilentError(error)) showLogin("无法连接服务，请检查地址或网络后重试。");
    } finally {
      isConnecting = false;
      renderSession();
      if (isLocked) dom["login-token"]?.focus();
      resumePolling();
      if (!isLocked && !currentConfig && !document.hidden) connect(false);
    }
  }

  // 轮询任务

  async function pollState() {
    try {
      const state = await fetchApi("/api/state", { background: true });
      if (!isObject(state)) throw new Error("Invalid state");
      render(state);
    } catch (error) {
      if (isSilentError(error)) return;
      setClass("status-pill", "status-pill unknown");
      setTextIfChanged("status-text", "状态获取失败");
      setTextIfChanged("state-notice", "状态刷新失败，以下内容保留上次成功获取的数据。");
    }
  }

  async function pollLogs() {
    try {
      const rows = await fetchApi("/api/logs?limit=30", { background: true });
      renderLogs(normalizeRecords(rows));
      setTextIfChanged("logs-status", "");
    } catch (error) {
      if (!isSilentError(error)) setTextIfChanged("logs-status", "日志刷新失败，保留上次记录。");
    }
  }

  async function pollHistory() {
    try {
      const rows = await fetchApi("/api/logs?record_type=phone&limit=20", { background: true });
      historyData = normalizeRecords(rows);
      renderHistory();
      setTextIfChanged("history-status", "");
    } catch (error) {
      if (!isSilentError(error)) setTextIfChanged("history-status", "历史刷新失败，保留上次记录。");
    }
  }

  async function pollDashboard() {
    await Promise.all([pollState(), pollLogs(), pollHistory()]);
  }

  async function pollConfig() {
    try {
      renderConfig(await fetchApi("/api/config", { background: true }), false);
      setTextIfChanged("config-status", "");
    } catch (error) {
      if (!isSilentError(error)) setTextIfChanged("config-status", "配置刷新失败，暂用上次配置。");
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

  // 设置保存

  function buildConfigUpdate() {
    const body = { computer_collect_enabled: dom["cfg-edit-computer-enabled"].checked };
    const fields = [
      ["computer_collect_interval", dom["cfg-edit-interval"], "采集间隔"],
      ["phone_timeout_ms", dom["cfg-edit-timeout"], "离线判定"],
      ["poll_interval", dom["cfg-edit-poll"], "轮询间隔"],
    ];
    for (const [key, input, label] of fields) {
      const value = number(input.value);
      if (value === null || !Number.isInteger(value) || value < Number(input.min) || value > Number(input.max)) {
        throw new Error(label + "必须为 " + input.min + "～" + input.max + " 毫秒的整数。");
      }
      body[key] = value;
    }
    if (currentConfig.token_from_env !== true) {
      const token = dom["cfg-edit-token"].value.trim();
      if (dom["cfg-clear-token"].checked && currentConfig.token_configured === true) body.shared_token = "";
      else if (token) body.shared_token = token;
    }
    return body;
  }

  async function saveSettings() {
    if (isLocked || isSaving || isConnecting || !currentConfig) return;
    let body;
    try {
      body = buildConfigUpdate();
    } catch (error) {
      setClass("settings-msg", "settings-msg error");
      setTextIfChanged("settings-msg", error.message);
      return;
    }
    isSaving = true;
    pausePolling();
    cancelRequests(false);
    renderSession();
    setClass("settings-msg", "settings-msg");
    setTextIfChanged("settings-msg", "正在保存…");
    let hasSaved = false;
    try {
      await fetchApi("/api/config", { method: "PATCH", body });
      hasSaved = true;
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
      setTextIfChanged("config-status", "");
      setTextIfChanged("settings-msg", "已保存");
    } catch (error) {
      if (!isSilentError(error)) {
        setClass("settings-msg", "settings-msg error");
        setTextIfChanged("settings-msg", hasSaved ? "已保存，配置暂时无法刷新。" : "保存失败，请检查连接与配置后重试。");
      }
    } finally {
      isSaving = false;
      renderSession();
      resumePolling();
    }
  }

  function showExportMsg(text, isError) {
    clearTimeout(exportMessageTimer);
    setTextIfChanged("export-msg", text);
    setClass("export-msg", "export-msg" + (isError ? " error" : ""));
    exportMessageTimer = setTimeout(() => setTextIfChanged("export-msg", ""), 3000);
  }

  async function download(format) {
    if (isLocked || isSaving || isExporting || !currentConfig) return;
    isExporting = true;
    dom["export-json"].disabled = true;
    dom["export-csv"].disabled = true;
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
      dom["export-json"].disabled = false;
      dom["export-csv"].disabled = false;
    }
  }

  // 事件绑定

  dom["auth-form"]?.addEventListener("submit", function (event) {
    event.preventDefault();
    if (isConnecting || isSaving) return;
    setSessionToken(dom["login-token"].value.trim());
    dom["login-token"].value = "";
    setTextIfChanged("auth-msg", "正在验证…");
    connect(true);
  });

  dom["logout-btn"]?.addEventListener("click", function () {
    if (isSaving) return;
    setSessionToken("");
    showLogin("已退出当前标签页；输入令牌可重新登录。");
  });

  dom["settings-save"]?.addEventListener("click", saveSettings);

  for (const input of document.querySelectorAll("#settings-card .settings-form input")) {
    input.addEventListener("input", () => { isSettingsDirty = true; });
    input.addEventListener("change", () => { isSettingsDirty = true; });
  }

  dom["cfg-clear-token"]?.addEventListener("change", renderTokenOptions);

  dom["settings-btn"]?.addEventListener("click", function () {
    dom["settings-card"].style.display = dom["settings-card"].style.display === "none" ? "block" : "none";
  });

  dom["settings-close"]?.addEventListener("click", function () {
    dom["settings-card"].style.display = "none";
  });

  dom["export-toggle-btn"]?.addEventListener("click", function () {
    dom["card-export"].style.display = dom["card-export"].style.display === "none" ? "block" : "none";
  });

  dom["export-json"]?.addEventListener("click", () => download("json"));
  dom["export-csv"]?.addEventListener("click", () => download("csv"));

  for (const tab of document.querySelectorAll(".history-tab")) {
    tab.addEventListener("click", function () {
      document.querySelectorAll(".history-tab").forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");
      historyMetric = tab.dataset.metric;
      renderHistory();
    });
  }

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

  // 夜间模式

  const themeBtn = dom["theme-btn"];
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
  themeBtn?.addEventListener("click", function () {
    const rect = themeBtn.getBoundingClientRect();
    setTheme(!document.documentElement.classList.contains("dark"), rect.left + rect.width / 2, rect.top + rect.height / 2);
  });

  connect(false);
})();
