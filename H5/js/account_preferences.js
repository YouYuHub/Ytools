/* Nonsecret UI preferences: account server state + a small offline outbox. */
(function (global) {
  "use strict";
  const auth = global.YtoolsAuth;
  if (!auth) return;
  const fields = {
    theme: ["ytools-theme-preference", "system", v => ["system", "light", "dark"].includes(v)],
    accent: ["ytools-accent-preference", "blue", v => ["blue", "green", "purple", "pink", "yellow", "none"].includes(v)],
    task_status_visible: ["ytools-task-status-visible", true, v => typeof v === "boolean"],
    sidebar_width: ["ytools-sidebar-width", 240, v => Number.isInteger(v) && v >= 220 && v <= 840],
    groups_expanded: ["ytools-session-groups-open", false, v => typeof v === "boolean"]
  };
  const outboxKey = "ytools-preference-outbox";
  // 显式经账户命名空间读写，不依赖 Storage 包装链的隐式前缀。
  const raw = auth.rawStorage || (global.Storage && {
    get: Storage.prototype.getItem, set: Storage.prototype.setItem, remove: Storage.prototype.removeItem });
  const scoped = key => auth.storageKey ? auth.storageKey(key) : key;
  const rawGet = key => raw.get.call(global.localStorage, scoped(key));
  const rawSet = (key, value) => raw.set.call(global.localStorage, scoped(key), value);
  let pending = {}, version = -1, applying = false, timer = null, sending = null, refreshing = null;
  const enabled = auth.preferences != null;
  function decode(key, text) {
    if (text == null) return undefined;
    const field = Object.keys(fields).find(name => fields[name][0] === key);
    if (!field) return undefined;
    let value = text;
    if (typeof fields[field][1] === "boolean") {
      if (!["0", "1", "false", "true"].includes(text)) return undefined;
      value = text === "1" || text === "true";
    } else if (field === "sidebar_width") value = Number(text);
    return fields[field][2](value) ? { field, value } : undefined;
  }
  function readOutbox(text) {
    try {
      if (!text || text.length > 4096) return {};
      const data = JSON.parse(text), result = {};
      if (!data || typeof data !== "object" || Array.isArray(data)) return {};
      for (const [field, item] of Object.entries(data)) {
        if (Object.hasOwn(fields, field) && item && fields[field][2](item.value) && typeof item.seed === "boolean") result[field] = { value: item.value, seed: item.seed };
      }
      return result;
    } catch (_) { return {}; }
  }
  function cachePending() {
    try {
      const text = JSON.stringify(pending);
      if (rawGet(outboxKey) !== text) rawSet(outboxKey, text);
    } catch (_) { /* Server saving still works without Web Storage. */ }
  }
  function write(field, value) {
    const text = typeof value === "boolean" ? (value ? "1" : "0") : String(value);
    try {
      if (rawGet(fields[field][0]) !== text) rawSet(fields[field][0], text);
    } catch (_) { /* restricted storage */ }
  }
  function adopt(data) {
    if (!data || !Number.isInteger(data.version) || data.version < version) return;
    version = data.version;
    applying = true;
    for (const [field, [, fallback, valid]] of Object.entries(fields)) {
      const saved = data.ui && data.ui[field];
      write(field, pending[field] ? pending[field].value : valid(saved) ? saved : fallback);
    }
    applying = false;
    global.dispatchEvent(new Event("ytools-preferences-change"));
  }
  function schedule() {
    clearTimeout(timer);
    if (enabled) timer = setTimeout(() => { flush(); }, 150);
  }
  function changed(key, text) {
    if (applying || !enabled) return;
    const item = text == null ? Object.entries(fields).find(([, spec]) => spec[0] === key) : null;
    const decoded = item ? { field: item[0], value: item[1][1] } : decode(key, text);
    if (!decoded) return;
    pending[decoded.field] = { value: decoded.value, seed: false };
    cachePending(); schedule();
  }
  // 捕获 UI 偏好变更：链式调用已有 prototype 实现（security_client 的隔离层），只监听 localStorage。
  const prevSet = global.Storage.prototype.setItem, prevRemove = global.Storage.prototype.removeItem;
  global.Storage.prototype.setItem = function (key, value) {
    const result = prevSet.call(this, key, value);
    if (this === global.localStorage) changed(String(key), String(value));
    return result;
  };
  global.Storage.prototype.removeItem = function (key) {
    const result = prevRemove.call(this, key);
    if (this === global.localStorage) changed(String(key), null);
    return result;
  };
  async function request(method, body) {
    const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 5000);
    try {
      const response = await fetch("/user/preferences", { method, cache: "no-store", signal: controller.signal,
        keepalive: method === "PATCH", headers: { "Content-Type": "application/json", "X-Ytools-Expected-Account": auth.user.id },
        ...(body ? { body: JSON.stringify(body) } : {}) });
      if (!response.ok) throw new Error("偏好同步失败");
      return await response.json();
    } finally { clearTimeout(timeout); }
  }
  function flush() {
    clearTimeout(timer);
    if (!enabled) return Promise.resolve();
    if (sending) return sending;
    sending = (async () => {
      while (Object.keys(pending).length) {
        const seed = !Object.values(pending).some(item => !item.seed);
        const batch = Object.fromEntries(Object.entries(pending).filter(([, item]) => item.seed === seed));
        const data = await request("PATCH", { ui: Object.fromEntries(Object.entries(batch).map(([field, item]) => [field, item.value])), only_if_unset: seed });
        for (const [field, item] of Object.entries(batch)) {
          if (JSON.stringify(pending[field]) === JSON.stringify(item)) delete pending[field];
        }
        cachePending(); adopt(data);
      }
    })().catch(() => { /* Keep pending UI values; retry on focus, online or the next edit. */ }).finally(() => { sending = null; });
    return sending;
  }
  function refresh() {
    if (!enabled || refreshing) return;
    refreshing = (async () => { await flush(); adopt(await request("GET")); })().catch(() => {}).finally(() => { refreshing = null; });
  }
  global.AccountPreferences = {
    flush, refresh,
    retain(key, text) { return key === outboxKey ? Object.keys(readOutbox(text)).length > 0 : !!decode(key, text); },
  };
  try { pending = readOutbox(rawGet(outboxKey)); } catch (_) { /* restricted storage */ }
  if (enabled) {
    const initial = auth.preferences;
    for (const [field, [key]] of Object.entries(fields)) {
      if (pending[field] || (initial.ui && Object.hasOwn(initial.ui, field))) continue;
      try {
        const cached = rawGet(key);
        const legacy = decode(key, cached == null && field === "theme" ? rawGet("theme-preference") : cached);
        if (legacy) pending[field] = { value: legacy.value, seed: true };
      } catch (_) { /* Only this account's namespaced cache is eligible for migration. */ }
    }
    cachePending(); adopt(initial); schedule();
  }
  global.addEventListener("online", refresh);
  global.addEventListener("focus", refresh);
  global.addEventListener("pagehide", () => { flush(); });
  global.addEventListener("storage", event => {
    if (event.storageArea === localStorage && event.key && event.key.startsWith(auth.storageKey(""))) refresh();
  });
})(window);
