"use strict";

// 无 npm 依赖：执行真实 app.js 和 index.html，以轻量 DOM、时钟和 HTTP stub 验证浏览器行为。
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const staticDir = path.join(__dirname, "..", "Mizuki", "desktop", "static");
const source = fs.readFileSync(path.join(staticDir, "app.js"), "utf8");
const html = fs.readFileSync(path.join(staticDir, "index.html"), "utf8");

class Element {
  constructor(tag, owner) {
    this.tagName = tag.toLowerCase();
    this.ownerDocument = owner;
    this.children = [];
    this.parentNode = null;
    this.attributes = {};
    this.style = {};
    this.dataset = {};
    this.className = "";
    this.hidden = false;
    this.disabled = false;
    this.checked = false;
    this._text = "";
    this._value = "";
    this.events = new Map();
    this.classList = {
      contains: (name) => this.className.split(/\s+/).includes(name),
      add: (...names) => { this.className = [...new Set([...this.className.split(/\s+/).filter(Boolean), ...names])].join(" "); },
      remove: (...names) => { this.className = this.className.split(/\s+/).filter((name) => !names.includes(name)).join(" "); },
      toggle: (name, force) => {
        const enabled = force === undefined ? !this.classList.contains(name) : Boolean(force);
        if (enabled) this.classList.add(name);
        else this.classList.remove(name);
        return enabled;
      },
    };
  }
  set textContent(value) {
    this.replaceChildren();
    this._text = String(value);
  }
  get textContent() { return this._text + this.children.map((child) => child.textContent).join(""); }
  set innerHTML(_) { throw new Error("Dynamic innerHTML is forbidden; use textContent/createElement"); }
  get innerHTML() { throw new Error("The stub intentionally has no HTML rendering shortcut"); }
  set value(value) { this._value = String(value); }
  get value() { return this._value; }
  setAttribute(name, value) {
    value = String(value);
    this.attributes[name] = value;
    if (name === "class") this.className = value;
    else if (["hidden", "disabled", "checked"].includes(name)) this[name] = true;
    else if (name.startsWith("data-")) this.dataset[name.slice(5)] = value;
    else if (name === "style") {
      for (const entry of value.split(";")) {
        const [key, val] = entry.split(":");
        if (key && val) this.style[key.trim()] = val.trim();
      }
    } else this[name] = value;
  }
  getAttribute(name) { return this.attributes[name] ?? null; }
  appendChild(child) {
    if (child.parentNode) child.remove();
    child.parentNode = this;
    this.children.push(child);
    return child;
  }
  replaceChildren(...children) {
    for (const child of this.children) child.parentNode = null;
    this.children = [];
    this._text = "";
    children.forEach((child) => this.appendChild(child));
  }
  remove() {
    if (!this.parentNode) return;
    const siblings = this.parentNode.children;
    siblings.splice(siblings.indexOf(this), 1);
    this.parentNode = null;
  }
  addEventListener(type, callback) {
    if (!this.events.has(type)) this.events.set(type, []);
    this.events.get(type).push(callback);
  }
  dispatch(type) {
    const event = { type, target: this, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
    for (const callback of this.events.get(type) || []) this.ownerDocument.track(callback(event));
    return event;
  }
  click() {
    if (this.disabled) return;
    if (this.tagName === "a") this.ownerDocument.downloads.push({ href: this.href, name: this.download });
    this.dispatch("click");
  }
  focus() { this.ownerDocument.activeElement = this; }
  getBoundingClientRect() { return { left: 0, top: 0, width: 36, height: 36 }; }
}

function descendants(node) {
  return node.children.flatMap((child) => [child, ...descendants(child)]);
}

function matches(el, selector) {
  if (selector.startsWith(".")) return el.classList.contains(selector.slice(1));
  if (selector.startsWith("#")) return el.id === selector.slice(1);
  return el.tagName === selector;
}

function buildDocument(track) {
  const document = {
    hidden: false, downloads: [], track,
    createElement(tag) { return new Element(tag, document); },
    getElementById(id) { return descendants(document.root).find((el) => el.id === id) || null; },
    querySelectorAll(selector) {
      const parts = selector.split(/\s+/);
      return descendants(document.root).filter((el) => {
        if (!matches(el, parts[parts.length - 1])) return false;
        let parent = el.parentNode;
        for (let i = parts.length - 2; i >= 0; i--) {
          while (parent && !matches(parent, parts[i])) parent = parent.parentNode;
          if (!parent) return false;
          parent = parent.parentNode;
        }
        return true;
      });
    },
    querySelector(selector) { return document.querySelectorAll(selector)[0] || null; },
  };
  document.root = new Element("root", document);
  document.addEventListener = document.root.addEventListener.bind(document.root);
  document.dispatch = document.root.dispatch.bind(document.root);
  Object.defineProperty(document, "cookie", {
    get() { throw new Error("Cookie authentication is forbidden"); },
    set() { throw new Error("Cookie authentication is forbidden"); },
  });
  const stack = [document.root];
  const voidTags = new Set(["meta", "link", "input", "br", "hr", "img"]);
  for (const token of html.match(/<!--[\s\S]*?-->|<![^>]*>|<[^>]+>|[^<]+/g)) {
    if (token.startsWith("<!")) continue;
    if (token.startsWith("</")) { stack.pop(); continue; }
    if (token.startsWith("<")) {
      const [, tag, rest] = token.match(/^<([\w-]+)([\s\S]*?)\/?\s*>$/);
      const el = document.createElement(tag);
      for (const [, name, double, single, bare] of rest.matchAll(/([\w:-]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?/g)) {
        el.setAttribute(name, double ?? single ?? bare ?? "");
      }
      stack[stack.length - 1].appendChild(el);
      if (!voidTags.has(tag) && !token.endsWith("/>")) stack.push(el);
    } else if (token.trim()) {
      const text = document.createElement("#text");
      text.textContent = token;
      stack[stack.length - 1].appendChild(text);
    }
  }
  assert.equal(stack.length, 1, "index.html must have balanced element tags");
  document.body = document.querySelector("body");
  document.documentElement = document.querySelector("html");
  return document;
}

function response(status, data, blob) {
  return {
    status, ok: status >= 200 && status < 300,
    async json() { return structuredClone(data); },
    async blob() { return blob || new Blob([JSON.stringify(data)], { type: "application/json" }); },
  };
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function app(options = {}) {
  let now = 0;
  let nextTimer = 1;
  const timers = new Map();
  const errors = [];
  const track = (promise) => {
    if (promise && typeof promise.then === "function") promise.catch((error) => errors.push(error));
  };
  const document = buildDocument(track);
  document.hidden = Boolean(options.hidden);
  const session = new Map(options.sessionToken ? [["mizuki-token", options.sessionToken]] : []);
  const local = new Map();
  const store = (map) => ({
    getItem(key) { if (options.storageThrows) throw new Error("Storage disabled"); return map.get(key) ?? null; },
    setItem(key, val) { if (options.storageThrows) throw new Error("Storage disabled"); map.set(key, String(val)); },
    removeItem(key) { if (options.storageThrows) throw new Error("Storage disabled"); map.delete(key); },
  });
  const runtime = {
    document, timers, session, local, requests: [], urls: [], revoked: [], active: new Map(), maxActive: new Map(),
    config: {
      host: "127.0.0.1", port: 12345, computer_collect_enabled: true,
      computer_collect_interval: 300, phone_timeout_ms: 10000, poll_interval: options.pollInterval ?? 5000,
      db_file: "data/collected.db",
    },
    configuredToken: options.token || "", envToken: options.envToken || "",
    state: options.state || { phone_connected: true, phone: {}, computer: {}, server: {} },
    plugins: options.plugins || [{ plugin_id: "demo", online: true }],
    records: options.records || [],
    respond: options.respond,
    el(id) { const el = document.getElementById(id); assert.ok(el, `Missing #${id} in actual index.html`); return el; },
    count(url) { return runtime.requests.filter((request) => request.url === url).length; },
    defaultResponse(request) {
      const token = runtime.envToken || runtime.configuredToken;
      if (token && request.headers["X-Sensor-Token"] !== token) return response(401, { detail: "Unauthorized" });
      if (request.url === "/api/config") {
        if (request.method === "PATCH") {
          const body = JSON.parse(request.body);
          if (Object.hasOwn(body, "shared_token")) runtime.configuredToken = body.shared_token;
          for (const key of Object.keys(runtime.config)) if (Object.hasOwn(body, key)) runtime.config[key] = body[key];
          return response(200, { status: "ok", updated: Object.keys(body) });
        }
        return response(200, {
          ...runtime.config, auth_enabled: Boolean(token), token_from_env: Boolean(runtime.envToken),
          token_configured: Boolean(runtime.configuredToken),
        });
      }
      if (request.url === "/api/state") return response(200, runtime.state);
      if (request.url === "/api/plugin-status") return response(200, { plugins: runtime.plugins });
      if (request.url.startsWith("/api/logs?")) return response(200, runtime.records);
      if (request.url.startsWith("/api/export/")) return response(200, runtime.records);
      throw new Error("Unexpected endpoint: " + request.url);
    },
    async flush() {
      for (let i = 0; i < 40; i++) await Promise.resolve();
      if (errors.length) throw errors.shift();
    },
    async advance(ms = 0) {
      await runtime.flush();
      const target = now + ms;
      let ticks = 0;
      while (true) {
        const entry = [...timers].filter(([, timer]) => timer.at <= target).sort((a, b) => a[1].at - b[1].at || a[0] - b[0])[0];
        if (!entry) break;
        assert.ok(++ticks < 10000, "Timer storm or polling loop");
        const [id, timer] = entry;
        timers.delete(id);
        now = timer.at;
        track(timer.fn());
        await runtime.flush();
      }
      now = target;
      await runtime.flush();
    },
    async login(token) {
      runtime.el("login-token").value = token;
      const event = runtime.el("auth-form").dispatch("submit");
      assert.ok(event.defaultPrevented, "Login must never navigate/submit a query string");
      await runtime.advance();
    },
    async input(id, value) {
      runtime.el(id).value = value;
      runtime.el(id).dispatch("input");
      await runtime.flush();
    },
    async save() {
      runtime.el("settings-save").click();
      await runtime.advance();
    },
    async visibility(hidden) {
      document.hidden = hidden;
      document.dispatch("visibilitychange");
      await runtime.advance();
    },
  };
  const fetchStub = (url, opts) => {
    const request = { url, ...opts, headers: { ...opts.headers }, at: now };
    runtime.requests.push(request);
    const key = request.method + " " + url;
    runtime.active.set(key, (runtime.active.get(key) || 0) + 1);
    runtime.maxActive.set(key, Math.max(runtime.active.get(key), runtime.maxActive.get(key) || 0));
    return new Promise((resolve, reject) => {
      const abort = () => {
        if (!request.ignoreAbort) reject(Object.assign(new Error("Aborted"), { name: "AbortError" }));
      };
      opts.signal.addEventListener("abort", abort, { once: true });
      if (opts.signal.aborted) abort();
      try {
        const result = runtime.respond ? runtime.respond(request, runtime) : runtime.defaultResponse(request);
        Promise.resolve(result).then(resolve, reject);
      } catch (error) { reject(error); }
    }).finally(() => runtime.active.set(key, runtime.active.get(key) - 1));
  };
  const windowEvents = new Element("window", document);
  const location = {};
  Object.defineProperty(location, "href", {
    get: () => "http://127.0.0.1/",
    set: () => { throw new Error("Authenticated navigation is forbidden; use fetch + Blob"); },
  });
  const context = vm.createContext({
    document,
    window: {
      innerWidth: 1200, innerHeight: 800, location,
      addEventListener: windowEvents.addEventListener.bind(windowEvents),
    },
    sessionStorage: store(session), localStorage: store(local),
    fetch: fetchStub, AbortController, Blob, console,
    URL: {
      createObjectURL(blob) {
        assert.ok(blob instanceof Blob);
        const url = "blob:test-" + runtime.urls.length;
        runtime.urls.push({ url, blob });
        return url;
      },
      revokeObjectURL(url) { runtime.revoked.push(url); },
    },
    setTimeout(fn, delay) {
      const id = nextTimer++;
      timers.set(id, { fn, at: now + Number(delay || 0) });
      return id;
    },
    clearTimeout(id) { timers.delete(id); },
    setInterval() { throw new Error("Polling must await the previous request, not overlap setInterval calls"); },
    clearInterval() {},
  });
  vm.runInContext(source, context, { filename: path.join(staticDir, "app.js") });
  return runtime;
}

function phone(data, extra = {}) {
  return { id: 1, type: "phone", timestamp: "2026-09-09T03:04:05", data: JSON.stringify(data), ...extra };
}

function patches(runtime) {
  return runtime.requests.filter((request) => request.method === "PATCH");
}

function assertNoTokenLeak(runtime, token) {
  assert.ok(!runtime.document.body.textContent.includes(token));
  assert.ok(!JSON.stringify([...runtime.local]).includes(token));
  assert.equal(runtime.el("login-token").value, "");
  assert.equal(runtime.el("cfg-edit-token").value, "");
  for (const request of runtime.requests) {
    assert.ok(!request.url.includes(token));
    assert.ok(!/[?&](?:token|shared_token|access_token)=/i.test(request.url));
    assert.equal(request.credentials, "omit");
    assert.equal(request.redirect, "error");
  }
}

test("SQLite JSON strings and objects share the log/chart parser; malformed rows are isolated", async () => {
  const a = app({ records: [
    phone({ device_id: "phone-one", location: { city: "海边" }, health: { steps: 0, heart_rate: 72 }, usage: { foreground_app: "地图" } }),
    phone(null, { id: 2, data: { device_id: "phone-two", health: { steps: 8, heart_rate: 80 } } }),
    { id: 3, type: "computer", timestamp: "2026-09-09T03:04:05", data: JSON.stringify({ foreground_window: "编辑器", health: { heart_rate: 999 } }) },
    phone(null, { data: "{broken" }), phone(null, { data: null }), phone(null, { data: "[]" }),
    { type: "unknown", data: {} },
  ] });
  await a.advance();
  assert.equal(a.el("logs-count").textContent, "3 条");
  assert.match(a.el("logs-body").textContent, /phone-one · 海边 · 0 步 · 地图/);
  assert.match(a.el("logs-body").textContent, /phone-two/);
  assert.match(a.el("logs-body").textContent, /电脑编辑器/);
  assert.deepEqual(a.el("history-bars").children.map((bar) => bar.dataset.value), ["80", "72"]);
  assert.equal(a.el("history-empty").classList.contains("show"), false);
});

test("zero coordinates, weather, health, uptime and hardware remain valid displayed values", async () => {
  const a = app({ state: {
    phone_connected: true,
    phone: {
      location: { latitude: 0, longitude: 0 }, weather: { temperature: 0, humidity: 0 },
      health: { heart_rate: 0, steps: 0, sleep_hours: 0 }, diagnostics: { running_seconds: 0, send_success: 0, send_failed: 0 },
      usage: { is_navigating: false, is_calling: false },
    },
    computer: { cpu_percent: 0, memory_percent: 0, disk_percent: 0, memory_used_gb: 0, memory_total_gb: 8, disk_used_gb: 0, disk_total_gb: 64 },
    server: { uptime_seconds: 0 },
  }, records: [phone({ health: { heart_rate: 0, steps: 0 } })] });
  await a.advance();
  for (const id of ["loc-lat", "loc-lng", "hlt-steps", "hw-cpu-pct", "hw-mem-pct", "hw-disk-pct"]) assert.equal(a.el(id).textContent, "0");
  assert.equal(a.el("wth-temp").textContent, "0 ℃");
  assert.equal(a.el("wth-humidity").textContent, "0 %");
  assert.equal(a.el("hlt-heart").textContent, "0 bpm");
  assert.equal(a.el("hlt-sleep").textContent, "0 小时");
  assert.equal(a.el("diag-uptime").textContent, "0 分 0 秒");
  assert.equal(a.el("srv-uptime").textContent, "0 分 0 秒");
  assert.equal(a.el("diag-send").textContent, "0 / 0");
  assert.equal(a.el("hw-mem-sub").textContent, "0 / 8 GB");
  assert.equal(a.el("use-nav").textContent, "否");
  assert.equal(a.el("history-bars").children[0].dataset.value, "0");
  assert.equal(a.el("history-bars").children[0].style.height, "2px");
  a.document.querySelectorAll(".history-tab")[1].click();
  assert.equal(a.el("history-bars").children[0].dataset.value, "0");
  assert.equal(a.el("history-empty").classList.contains("show"), false);
});

test("missing, boolean and non-finite metrics stay absent instead of becoming zero", async () => {
  const a = app({ state: { phone: { weather: { temperature: "", humidity: null }, health: { steps: false } }, computer: { cpu_percent: "not-a-number" } },
    records: [phone({ health: { heart_rate: "" } }), phone({ health: { heart_rate: false } }), phone({ health: { heart_rate: "Infinity" } }), phone({})] });
  await a.advance();
  for (const id of ["wth-temp", "wth-humidity", "hlt-steps", "hw-cpu-pct"]) assert.equal(a.el(id).textContent, "—");
  assert.equal(a.el("history-bars").children.length, 0);
  assert.equal(a.el("history-empty").classList.contains("show"), true);
});

test("shared time formatting pads short times and prefers received_at in SQLite data", async () => {
  const a = app({ state: { phone_last_seen: "2026-09-09T03:04:05" }, records: [phone({
    received_at: "2026-09-09T06:07:08", health: { heart_rate: 60 },
  })] });
  await a.advance();
  assert.equal(a.el("conn-last-seen").textContent, "03:04:05");
  assert.equal(a.el("history-labels").textContent, "06:07");
  assert.match(a.el("logs-body").textContent, /09-09 06:07:08/);
});

test("external text, timestamps and window titles cannot create HTML", async () => {
  const attack = '<img src=x onerror="alert(1)"><script>bad()</script>';
  const a = app({ state: { phone: { device_id: attack, location: { city: attack }, usage: { foreground_app: attack }, diagnostics: { last_error: attack } }, computer: { foreground_window: attack } },
    records: [phone({ device_id: attack, location: { city: attack }, usage: { foreground_app: attack }, health: { heart_rate: 12, steps: 0 } }, { timestamp: attack }),
      { type: "computer", timestamp: attack, data: { foreground_window: attack } }] });
  await a.advance();
  assert.equal(a.el("conn-device").textContent, attack);
  assert.equal(a.el("pc-window").textContent, attack);
  assert.ok(a.el("logs-body").textContent.includes(attack));
  assert.equal(a.document.querySelectorAll("img").length, 0);
  assert.equal(a.document.querySelectorAll("script").length, 1, "Only the original static script tag exists");
  assert.equal(a.el("history-labels").textContent, "—");
});

test("state polling never overwrites plugin heartbeat status", async () => {
  const a = app({ pollInterval: 1000 });
  await a.advance();
  assert.equal(a.el("plugin-text").textContent, "插件 1/1 在线");
  a.state = { phone_connected: false, plugins: [] };
  await a.advance(1000);
  assert.equal(a.el("plugin-text").textContent, "插件 1/1 在线");
  assert.equal(a.count("/api/plugin-status"), 1);
});

test("failed or malformed plugin responses are unknown, not cached-online or offline", async () => {
  const a = app({ pollInterval: 1000 });
  await a.advance();
  a.respond = (request, server) => request.url === "/api/plugin-status" ? response(503, {}) : server.defaultResponse(request);
  await a.advance(3000);
  assert.equal(a.el("plugin-pill").className, "status-pill unknown");
  assert.match(a.el("plugin-text").textContent, /获取失败/);
  await a.advance(1000);
  assert.equal(a.el("plugin-pill").className, "status-pill unknown");
  a.respond = (request, server) => request.url === "/api/plugin-status" ? response(200, {}) : server.defaultResponse(request);
  await a.advance(2000);
  assert.equal(a.el("plugin-pill").className, "status-pill unknown");
  a.respond = undefined;
  a.plugins = [];
  await a.advance(3000);
  assert.equal(a.el("plugin-text").textContent, "插件 离线");
});

test("401 exposes token login, pauses private requests and accepts only header authentication", async () => {
  const secret = "tab-secret-only";
  const a = app({ token: secret });
  await a.advance();
  assert.equal(a.el("auth-card").hidden, false);
  assert.equal(a.el("dashboard").hidden, true);
  assert.equal(a.el("login-token").type, "password");
  assert.equal(a.el("login-token").getAttribute("name"), null, "A native form submit cannot leak a token field");
  assert.equal(a.el("auth-form").method, "post");
  await a.advance(120000);
  assert.equal(a.requests.length, 1);
  await a.login("wrong-token");
  assert.equal(a.session.has("mizuki-token"), false);
  assert.match(a.el("auth-msg").textContent, /无效/);
  await a.login(secret);
  assert.equal(a.el("dashboard").hidden, false);
  assert.equal(a.el("auth-card").hidden, true);
  assert.equal(a.session.get("mizuki-token"), secret);
  for (const request of a.requests.slice(2)) assert.equal(request.headers["X-Sensor-Token"], secret);
  assertNoTokenLeak(a, secret);
});

test("restored tab token authenticates startup but never pre-fills the configuration secret", async () => {
  const secret = "restored-secret";
  const a = app({ token: secret, sessionToken: secret });
  await a.advance();
  assert.equal(a.requests[0].headers["X-Sensor-Token"], secret);
  assert.equal(a.el("dashboard").hidden, false);
  assertNoTokenLeak(a, secret);
  a.el("logout-btn").click();
  await a.advance();
  assert.equal(a.session.has("mizuki-token"), false);
  assert.equal(a.el("dashboard").hidden, true);
  assert.equal(a.el("logs-count").textContent, "0 条");
  const count = a.requests.length;
  await a.advance(120000);
  assert.equal(a.requests.length, count);
});

test("a background 401 clears the tab token, hides private data and stops all poll loops", async () => {
  const a = app({ token: "before", sessionToken: "before", pollInterval: 1000 });
  await a.advance();
  a.configuredToken = "after";
  await a.advance(1000);
  assert.equal(a.el("dashboard").hidden, true);
  assert.equal(a.session.has("mizuki-token"), false);
  assert.equal(a.el("auth-card").hidden, false);
  const count = a.requests.length;
  await a.advance(120000);
  assert.equal(a.requests.length, count);
  await a.login("after");
  assert.equal(a.el("dashboard").hidden, false);
});

test("blocked Web Storage falls back to memory without localStorage or cookies", async () => {
  const a = app({ token: "memory-only", storageThrows: true });
  await a.advance();
  await a.login("memory-only");
  assert.equal(a.el("dashboard").hidden, false);
  assert.equal(a.session.size, 0);
  assert.equal(a.local.size, 0);
  await a.advance(5000);
  assert.ok(a.requests.slice(1).every((request) => request.headers["X-Sensor-Token"] === "memory-only"));
  assertNoTokenLeak(a, "memory-only");
});

test("blank configuration token preserves authentication; replacement rotates the current session", async () => {
  const a = app({ token: "old-token", sessionToken: "old-token" });
  await a.advance();
  await a.save();
  assert.equal(Object.hasOwn(JSON.parse(patches(a)[0].body), "shared_token"), false);
  assert.equal(a.session.get("mizuki-token"), "old-token");
  await a.input("cfg-edit-token", "new-token");
  await a.save();
  const patch = patches(a)[1];
  assert.equal(JSON.parse(patch.body).shared_token, "new-token");
  assert.equal(patch.headers["X-Sensor-Token"], "old-token");
  assert.equal(a.session.get("mizuki-token"), "new-token");
  assert.equal(a.el("settings-msg").textContent, "已保存");
  assert.ok(a.requests.slice(a.requests.indexOf(patch) + 1).every((request) => request.headers["X-Sensor-Token"] === "new-token"));
  assertNoTokenLeak(a, "new-token");
  await a.advance(5000);
  assert.equal(a.el("dashboard").hidden, false);
});

test("clearing an existing token is explicit and removes it from subsequent request headers", async () => {
  const a = app({ token: "clear-this", sessionToken: "clear-this" });
  await a.advance();
  assert.equal(a.el("cfg-clear-token").disabled, false);
  a.el("cfg-clear-token").checked = true;
  a.el("cfg-clear-token").dispatch("change");
  assert.equal(a.el("cfg-edit-token").disabled, true);
  await a.save();
  const patch = patches(a)[0];
  assert.equal(JSON.parse(patch.body).shared_token, "");
  assert.equal(patch.headers["X-Sensor-Token"], "clear-this");
  assert.equal(a.session.has("mizuki-token"), false);
  assert.ok(a.requests.slice(a.requests.indexOf(patch) + 1).every((request) => !Object.hasOwn(request.headers, "X-Sensor-Token")));
  assert.equal(a.el("dashboard").hidden, false);
  assert.match(a.el("auth-status").textContent, /未启用/);
});

test("environment token takes priority and token editing/clearing is disabled even with a file token", async () => {
  const a = app({ token: "file-secret", envToken: "environment-secret", sessionToken: "environment-secret" });
  await a.advance();
  assert.equal(a.el("cfg-edit-token").disabled, true);
  assert.equal(a.el("cfg-clear-token").disabled, true);
  assert.match(a.el("cfg-token-note").textContent, /MIZUKI_TOKEN.*优先/);
  a.el("cfg-edit-token").value = "should-not-be-sent";
  a.el("cfg-clear-token").checked = true;
  await a.save();
  assert.equal(Object.hasOwn(JSON.parse(patches(a)[0].body), "shared_token"), false);
  assert.equal(a.configuredToken, "file-secret");
  assert.equal(a.session.get("mizuki-token"), "environment-secret");
  assertNoTokenLeak(a, "environment-secret");
  assertNoTokenLeak(a, "file-secret");
});

test("enabling a token from public mode authenticates the follow-up config and dashboard requests", async () => {
  const a = app();
  await a.advance();
  assert.equal(a.el("cfg-clear-token").disabled, true);
  await a.input("cfg-edit-token", "first-token");
  await a.save();
  const patch = patches(a)[0];
  assert.equal(patch.headers["X-Sensor-Token"], undefined);
  assert.equal(a.session.get("mizuki-token"), "first-token");
  assert.ok(a.requests.slice(a.requests.indexOf(patch) + 1).every((request) => request.headers["X-Sensor-Token"] === "first-token"));
  assert.equal(a.el("dashboard").hidden, false);
  assert.equal(a.el("settings-msg").textContent, "已保存");
});

test("hiding the page during token replacement does not abort the configuration write", async () => {
  const pending = deferred();
  const a = app({ token: "write-old", sessionToken: "write-old", respond(request, server) {
    if (request.method === "PATCH") return pending.promise;
    return server.defaultResponse(request);
  } });
  await a.advance();
  await a.input("cfg-edit-token", "write-new");
  a.el("settings-save").click();
  await a.flush();
  const patch = patches(a)[0];
  await a.visibility(true);
  assert.equal(patch.signal.aborted, false);
  a.configuredToken = "write-new";
  pending.resolve(response(200, { status: "ok", updated: ["shared_token"] }));
  await a.advance();
  assert.equal(a.session.get("mizuki-token"), "write-new");
  const count = a.requests.length;
  await a.advance(120000);
  assert.equal(a.requests.length, count);
  await a.visibility(false);
  assert.equal(a.requests.filter((request) => request.url === "/api/state").at(-1).headers["X-Sensor-Token"], "write-new");
});

test("failed token replacement retains the existing session and invalid settings are not submitted", async () => {
  const a = app({ token: "keep-secret", sessionToken: "keep-secret" });
  await a.advance();
  await a.input("cfg-edit-poll", "0");
  await a.save();
  assert.equal(patches(a).length, 0);
  assert.match(a.el("settings-msg").textContent, /整数/);
  await a.input("cfg-edit-poll", "5000");
  await a.input("cfg-edit-token", "rejected-secret");
  a.respond = (request, server) => request.method === "PATCH" ? response(500, {}) : server.defaultResponse(request);
  await a.save();
  assert.equal(a.session.get("mizuki-token"), "keep-secret");
  assert.match(a.el("settings-msg").textContent, /保存失败/);
  await a.advance(5000);
  assert.equal(a.el("dashboard").hidden, false);
});

test("JSON and CSV exports use authenticated fetch + Blob and revoke temporary URLs", async () => {
  const a = app({ token: "download-secret", sessionToken: "download-secret" });
  await a.advance();
  for (const format of ["json", "csv"]) {
    a.el("export-" + format).click();
    await a.advance();
    const request = a.requests.find((request) => request.url === "/api/export/" + format);
    assert.equal(request.headers["X-Sensor-Token"], "download-secret");
    assert.equal(a.document.downloads.at(-1).name, "mizuki-data." + format);
    assert.match(a.document.downloads.at(-1).href, /^blob:/);
    assert.match(a.el("export-msg").textContent, /已开始下载/);
  }
  await a.advance(1000);
  assert.deepEqual(a.revoked, a.urls.map((entry) => entry.url));
  assertNoTokenLeak(a, "download-secret");
});

test("failed or unauthorized exports never claim success or start a download", async () => {
  const a = app({ token: "export-old", sessionToken: "export-old" });
  await a.advance();
  a.respond = (request, server) => request.url.startsWith("/api/export/") ? response(503, {}) : server.defaultResponse(request);
  a.el("export-json").click();
  await a.advance();
  assert.match(a.el("export-msg").textContent, /失败/);
  assert.equal(a.document.downloads.length, 0);
  a.respond = undefined;
  a.configuredToken = "export-new";
  a.el("export-csv").click();
  await a.advance();
  assert.equal(a.el("auth-card").hidden, false);
  assert.equal(a.document.downloads.length, 0);
  assert.equal(a.urls.length, 0);
});

test("poll_interval milliseconds drive state, logs and history while plugins use 3 seconds", async () => {
  const a = app({ pollInterval: 2000 });
  await a.advance();
  const dashboardUrls = ["/api/state", "/api/logs?limit=30", "/api/logs?record_type=phone&limit=20"];
  for (const url of dashboardUrls) assert.equal(a.count(url), 1);
  await a.advance(1999);
  for (const url of dashboardUrls) assert.equal(a.count(url), 1);
  await a.advance(1);
  for (const url of dashboardUrls) assert.equal(a.count(url), 2);
  assert.equal(a.count("/api/plugin-status"), 1);
  await a.advance(1000);
  assert.equal(a.count("/api/plugin-status"), 2);
});

test("periodic config refresh leaves active edits and unchanged polling timers intact", async () => {
  const a = app({ pollInterval: 2000 });
  await a.advance();
  await a.input("cfg-edit-interval", "1700");
  await a.input("cfg-edit-token", "unsaved-token");
  await a.advance(120000);
  assert.equal(a.count("/api/state"), 61);
  assert.equal(a.count("/api/logs?limit=30"), 61);
  assert.equal(a.count("/api/logs?record_type=phone&limit=20"), 61);
  assert.equal(a.count("/api/plugin-status"), 41);
  assert.equal(a.count("/api/config"), 3);
  assert.equal(a.timers.size, 3, "One waiting timeout per poll group, no timer accumulation");
  assert.equal(a.el("cfg-edit-interval").value, "1700");
  assert.equal(a.el("cfg-edit-token").value, "unsaved-token");
});

test("periodic config changes reschedule only the main poll group", async () => {
  const a = app({ pollInterval: 2000 });
  await a.advance();
  await a.advance(59000);
  assert.equal(a.count("/api/state"), 30);
  a.config.poll_interval = 1000;
  await a.advance(1000);
  assert.equal(a.count("/api/config"), 2);
  assert.equal(a.count("/api/state"), 30);
  assert.equal(a.count("/api/plugin-status"), 21);
  await a.advance(1000);
  assert.equal(a.count("/api/state"), 31);
  assert.equal(a.count("/api/logs?record_type=phone&limit=20"), 31);
  assert.equal(a.timers.size, 3);
});

test("saved poll interval reschedules the main group without adding another loop", async () => {
  const a = app({ pollInterval: 5000 });
  await a.advance();
  await a.input("cfg-edit-poll", "1000");
  await a.save();
  const before = a.count("/api/state");
  await a.advance(1000);
  assert.equal(a.count("/api/state"), before + 1);
  assert.equal(a.count("/api/logs?limit=30"), before + 1);
  assert.equal(a.count("/api/logs?record_type=phone&limit=20"), before + 1);
  assert.equal(a.timers.size, 3);
});

test("a slow state request blocks the next main group rather than overlapping requests", async () => {
  const pending = deferred();
  const a = app({ pollInterval: 1000, respond(request, server) {
    if (request.url === "/api/state" && server.count("/api/state") === 1) return pending.promise;
    return server.defaultResponse(request);
  } });
  await a.advance();
  await a.advance(5000);
  assert.equal(a.count("/api/state"), 1);
  assert.equal(a.count("/api/logs?limit=30"), 1);
  assert.equal(a.count("/api/plugin-status"), 2, "Independent heartbeat polling keeps working");
  pending.resolve(response(200, a.state));
  await a.advance();
  await a.advance(1000);
  assert.equal(a.count("/api/state"), 2);
  assert.ok([...a.maxActive.values()].every((value) => value === 1));
});

test("hidden pages cancel background requests and resume a single main/plugin poll", async () => {
  const pending = deferred();
  const a = app({ respond(request, server) {
    return request.url === "/api/state" && server.count("/api/state") === 1 ? pending.promise : server.defaultResponse(request);
  } });
  await a.advance();
  const old = a.requests.find((request) => request.url === "/api/state");
  await a.visibility(true);
  assert.equal(old.signal.aborted, true);
  const before = a.requests.length;
  await a.advance(120000);
  assert.equal(a.requests.length, before);
  assert.equal(a.timers.size, 0);
  await a.visibility(false);
  assert.equal(a.count("/api/state"), 2);
  assert.equal(a.count("/api/plugin-status"), 2);
  await a.visibility(false);
  assert.equal(a.count("/api/state"), 2);
  assert.equal(a.timers.size, 3);
});

test("an initially hidden page waits for visibility before its first protected request", async () => {
  const a = app({ hidden: true });
  await a.advance(120000);
  assert.equal(a.requests.length, 0);
  await a.visibility(false);
  assert.equal(a.count("/api/config"), 1);
  assert.equal(a.count("/api/state"), 1);
});

test("plugin timeout marks unknown promptly and does not stop other polling", async () => {
  const a = app({ pollInterval: 1000 });
  await a.advance();
  a.respond = (request, server) => request.url === "/api/plugin-status" ? new Promise(() => {}) : server.defaultResponse(request);
  await a.advance(6000);
  assert.equal(a.el("plugin-pill").className, "status-pill unknown");
  assert.match(a.el("plugin-text").textContent, /获取失败/);
  assert.equal(a.count("/api/state"), 7);
});

test("late 401 from a cancelled old session cannot undo successful token replacement", async () => {
  const pending = deferred();
  const a = app({ token: "old-session", sessionToken: "old-session", pollInterval: 1000, respond(request, server) {
    if (request.url === "/api/state" && server.count("/api/state") === 2) {
      request.ignoreAbort = true; // 模拟已在网络中、无法及时取消的响应。
      return pending.promise;
    }
    return server.defaultResponse(request);
  } });
  await a.advance();
  await a.advance(1000);
  await a.input("cfg-edit-token", "new-session");
  await a.save();
  pending.resolve(response(401, {}));
  await a.advance();
  assert.equal(a.session.get("mizuki-token"), "new-session");
  assert.equal(a.el("dashboard").hidden, false);
  assert.equal(a.el("auth-card").hidden, true);
  await a.advance(1000);
  assert.equal(a.requests.filter((request) => request.url === "/api/state").at(-1).headers["X-Sensor-Token"], "new-session");
});
