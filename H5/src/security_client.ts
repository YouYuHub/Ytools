/** 账号校验所需的响应子集（真实 Response 与 XHR 伪装响应通用）。 */
interface AccountResponseLike {
  status: number;
  headers: { get: (name: string) => string | null };
}

interface YtoolsAuthApi {
  readonly user: any;
  readonly csrf: any;
  readonly accountPanelAllowed: boolean;
  readonly historyPagingAllowed: boolean;
  readonly preferences: any;
  readonly executionAllowed: boolean;
  update: (data: any) => void;
  refresh: (signal?: AbortSignal | null) => Promise<any>;
  upload: (input: string | URL, file: Blob, onProgress?: (progress: { phase: string; percent: number }) => void, signal?: AbortSignal | null) => Promise<any>;
  logout: () => Promise<void>;
  storageKey: (key: string) => string;
  clearLocal: () => void;
  lost: (reason?: string) => void;
  rawStorage: any;
}

/* Runs before app modules; no login secrets are persisted in Web Storage. */
(function (global: Window) {
  "use strict";
  const bootstrap = document.getElementById("account-context");
  if (!bootstrap) {
    // Real entry pages must be served by the authenticated backend. Independent
    // component fixtures omit this marker and keep their existing test setup.
    if (document.documentElement.hasAttribute("data-ytools-auth-required")) {
      const sameBackend = /^https?:$/.test(location.protocol) && location.pathname.startsWith("/ui/");
      const target = sameBackend ? location.origin + "/ui/login.html"
        : "http://127.0.0.1:48621/ui/" + location.search + location.hash;
      (global as unknown as { YtoolsAuthPending: boolean }).YtoolsAuthPending = true;
      location.replace(target);
    }
    return;
  }
  let config: any;
  try { config = JSON.parse(bootstrap.textContent as string); } catch (_) { location.replace("/ui/login.html"); return; }
  bootstrap.textContent = "";
  const prefix = "ytools:user:" + config.user.id + ":";
  function storageKey(key: string): string { return prefix + key; }
  const StorageCtor = (global as unknown as { Storage?: typeof Storage }).Storage;
  const raw = StorageCtor && { get: Storage.prototype.getItem, set: Storage.prototype.setItem,
    remove: Storage.prototype.removeItem, key: Storage.prototype.key, clear: Storage.prototype.clear };
  // 账户命名空间隔离是唯一的 Storage.prototype 补丁点：key/length 保持原生语义
  // （返回实际键/全局长度），get/set/remove/clear 按账户隔离。rawStorage 供偏好、
  // 文件标记等模块显式读写，避免隐式依赖包装链。
  // 注：raw 方法为原型裸引用，调用处一律 .call(thisArg) 显式绑定 this。
  if (raw) {
    Storage.prototype.getItem = function (key) { return raw!.get.call(this, storageKey(key)); };
    Storage.prototype.setItem = function (key, value) { return raw!.set.call(this, storageKey(key), value); };
    Storage.prototype.removeItem = function (key) { return raw!.remove.call(this, storageKey(key)); };
    Storage.prototype.clear = function () {
      const keys: string[] = []; for (let i = 0; i < this.length; i++) { const key = raw!.key.call(this, i); if (key && key.startsWith(prefix)) keys.push(key); }
      keys.forEach(key => raw!.remove.call(this, key));
    };
  }
  function clearLocal(): void {
    if (!raw) return;
    [global.localStorage, global.sessionStorage].forEach(storage => {
      if (!storage) return;
      const keys: string[] = []; for (let i = 0; i < storage.length; i++) { const key = raw!.key.call(storage, i); if (key && key.startsWith(prefix)) keys.push(key); }
      keys.forEach(key => {
        const retained = storage === global.localStorage && (global as unknown as { AccountPreferences?: { retain: (key: string, text: string | null) => boolean } }).AccountPreferences &&
          (global as unknown as { AccountPreferences: { retain: (key: string, text: string | null) => boolean } }).AccountPreferences.retain(key.slice(prefix.length), raw!.get.call(storage, key));
        if (!retained) raw!.remove.call(storage, key);
      });
    });
  }
  let redirecting = false;
  function lost(reason?: string): void {
    if (redirecting) return; redirecting = true; clearLocal();
    const event = new CustomEvent("ytools-auth-lost", { cancelable: true, detail: { reason } });
    global.dispatchEvent(event);
    if (event.defaultPrevented) return;
    location.replace("/ui/login.html" + (reason === "username_changed" ? "?username_changed=1" : ""));
  }
  const nativeFetch = global.fetch.bind(global);
  function sameAccount(response: AccountResponseLike): void {
    const account = response.headers.get("X-Ytools-Account");
    if (account && account !== config.user.id) { lost(); throw new Error("当前登录账号已变化，请重新打开页面"); }
  }
  function checked(response: AccountResponseLike): void {
    sameAccount(response);
    if (response.status === 409 && response.headers.get("X-Ytools-Account-Changed")) lost();
    if (response.status === 401 || (response.status === 403 && response.headers.get("X-MFA-Required"))) lost();
    if (response.status === 403 && response.headers.get("X-Password-Change-Required") && location.pathname !== "/ui/account.html") location.replace("/ui/account.html");
  }
  async function refreshIdentity(signal?: AbortSignal | null): Promise<any> {
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 8000);
    const abort = () => controller.abort();
    if (signal) { signal.addEventListener("abort", abort, { once: true }); if (signal.aborted) abort(); }
    try {
      const response = await nativeFetch("/auth/me", { credentials: "same-origin", cache: "no-store", signal: controller.signal, headers: { "X-Ytools-Expected-Account": config.user.id } });
      checked(response);
      if (!response.ok) throw new Error("登录状态暂不可用");
      const data = await response.json();
      if (!data.user || data.user.id !== config.user.id || data.mfa_verified !== true) { lost(); throw new Error("登录身份已变化，请重新登录"); }
      config = Object.assign({}, config, data); return data;
    } finally { clearTimeout(timer); if (signal) signal.removeEventListener("abort", abort); }
  }
  function registering(method: string, url: URL): boolean {
    return !["GET", "HEAD", "OPTIONS"].includes(method) && !url.pathname.startsWith("/auth/") &&
      !["/chat_history/import_preview", "/chat_history/import_package", "/chat_history/upload_chat_file"].includes(url.pathname);
  }
  const registeredSessions = new Map<string, Promise<void>>();
  async function ensureSession(identifier: string): Promise<void> {
    if (!registeredSessions.has(identifier)) {
      const pending = global.fetch("/chat_history/ensure_session", { method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": config.csrf }, body: JSON.stringify({ session_id: identifier })
      }).then(async response => { sameAccount(response); if (!response.ok) { if (response.status === 401) lost(); const data = await response.json(); throw new Error(data.detail || "会话授权失败"); } });
      registeredSessions.set(identifier, pending);
      pending.catch(() => registeredSessions.delete(identifier));
    }
    await registeredSessions.get(identifier);
  }
  global.fetch = async function (input: RequestInfo | URL, options?: RequestInit): Promise<Response> {
    const url = new URL(typeof input === "string" || input instanceof URL ? String(input) : input.url, location.href);
    if (url.origin !== location.origin) return nativeFetch(input, options);
    options = Object.assign({}, options || {});
    const inputReq = input as Request;
    const method = String(options.method || (input && inputReq.method) || "GET").toUpperCase();
    options.credentials = "same-origin";
    options.headers = new Headers(options.headers || (input && inputReq.headers) || {});
    (options.headers as Headers).set("X-Ytools-Expected-Account", config.user.id);
    if (!["GET", "HEAD", "OPTIONS"].includes(method)) (options.headers as Headers).set("X-CSRF-Token", config.csrf);
    const identifier = url.searchParams.get("session_id");
    // Reads never claim an ID. Imports resolve source labels to owned copies
    // server-side, so they must not pre-register the source ID either.
    if (identifier && registering(method, url)) await ensureSession(identifier);
    const replay = input && typeof inputReq.clone === "function" ? inputReq.clone() : input;
    let response = await nativeFetch(input, options);
    sameAccount(response);
    if (response.status === 403 && response.headers.get("X-CSRF-Required") && !(options.body && (options.body as ReadableStream).getReader)) {
      await refreshIdentity(options.signal);
      (options.headers as Headers).set("X-CSRF-Token", config.csrf); response = await nativeFetch(replay, options);
    }
    checked(response);
    return response;
  };
  async function upload(input: string | URL, file: Blob, onProgress?: (progress: { phase: string; percent: number }) => void, signal?: AbortSignal | null): Promise<any> {
    const url = new URL(input, location.href);
    if (url.origin !== location.origin) throw new Error("账号文件只能上传到当前服务");
    if (["/chat_history/import_preview", "/chat_history/import_package", "/chat_history/upload_chat_file"].includes(url.pathname) && config.account_sharing !== true) throw new Error("分享加载服务正在升级，请刷新后重试");
    const identifier = url.searchParams.get("session_id");
    if (signal && signal.aborted) { const error = new Error("上传已取消"); error.name = "AbortError"; throw error; }
    if (identifier && registering("POST", url)) await ensureSession(identifier);
    for (let attempt = 0; attempt < 2; attempt++) {
      const response = await new Promise<{ status: number; ok: boolean; headers: { get: (name: string) => string | null }; json: () => Promise<any> }>((resolve, reject) => {
        const xhr = new XMLHttpRequest(); let settled = false;
        function finish(error?: Error): void {
          if (settled) return; settled = true;
          if (signal) signal.removeEventListener("abort", abort);
          if (error) reject(error); else resolve({ status: xhr.status, ok: xhr.status >= 200 && xhr.status < 300,
            headers: { get: (name: string) => xhr.getResponseHeader(name) }, json: async () => { try { return JSON.parse(xhr.responseText); } catch (_) { return {}; } } });
        }
        function abort(): void { xhr.abort(); }
        function progress(phase: string, percent: number): void { if (onProgress) { try { onProgress({ phase, percent }); } catch (_) { /* Detached progress UI must not break persistence. */ } } }
        xhr.open("POST", url.href); xhr.withCredentials = true;
        xhr.setRequestHeader("Content-Type", "application/octet-stream");
        xhr.setRequestHeader("X-CSRF-Token", config.csrf);
        xhr.setRequestHeader("X-Ytools-Expected-Account", config.user.id);
        xhr.upload.onprogress = event => { if (event.lengthComputable) progress("upload", Math.min(100, Math.round(event.loaded / event.total * 100))); };
        xhr.upload.onload = () => progress("processing", 100);
        xhr.onload = () => finish();
        xhr.onerror = () => finish(new Error("网络连接中断"));
        xhr.onabort = () => { const error = new Error("上传已取消"); error.name = "AbortError"; finish(error); };
        if (signal) { signal.addEventListener("abort", abort, { once: true }); if (signal.aborted) { abort(); if (!settled) { const error = new Error("上传已取消"); error.name = "AbortError"; finish(error); } return; } }
        progress("upload", 0); xhr.send(file);
      });
      sameAccount(response);
      if (!attempt && response.status === 403 && response.headers.get("X-CSRF-Required")) { await refreshIdentity(signal); continue; }
      checked(response);
      const body = await response.json();
      if (!response.ok) { const error = new Error(typeof body.detail === "string" ? body.detail : body.message || ("上传失败 " + response.status)) as Error & { status?: number }; error.status = response.status; throw error; }
      if (onProgress) { try { onProgress({ phase: "done", percent: 100 }); } catch (_) { } }
      return body;
    }
  }
  (global as unknown as { YtoolsAuth: YtoolsAuthApi }).YtoolsAuth = { get user() { return config.user; }, get csrf() { return config.csrf; },
    get accountPanelAllowed() { return config.account_panel === true; },
    get historyPagingAllowed() { return config.history_paging === true; },
    get preferences() { return config.preferences || null; },
    get executionAllowed() { return !!config.execution_allowed; },
    update(data: any) { config = Object.assign({}, config, data); },
    refresh: refreshIdentity, upload,
    async logout() { if ((global as unknown as { AccountPreferences?: { flush: () => Promise<void> } }).AccountPreferences) await (global as unknown as { AccountPreferences: { flush: () => Promise<void> } }).AccountPreferences.flush(); const response = await global.fetch("/auth/logout", { method: "POST" }); if (!response.ok) throw new Error("退出失败，请稍后重试"); lost(); },
    storageKey, clearLocal, lost, rawStorage: raw || null };
  if (document.documentElement) document.documentElement.dataset.accountRole = config.user.role;
})(window);
