/* The account view is disposable; the chat document and streams stay mounted. */
(function (App) {
  "use strict";
  const auth = window.YtoolsAuth, stateKey = "ytoolsAccountPanel";
  if (!auth) return;
  let panel = null, frame = null, token = null, previousFocus = null, timeout = null, closing = false;
  function marker() { return history.state && history.state[stateKey]; }
  function refreshAccount() {
    if (auth.refresh) auth.refresh().then(() => {
      if (App.loadUserProfile) App.loadUserProfile();
      if (window.AccountPreferences && AccountPreferences.refresh) AccountPreferences.refresh();
    }).catch(() => {});
    else if (App.loadUserProfile) App.loadUserProfile();
  }
  function dispose() {
    if (!panel) return;
    clearTimeout(timeout);
    const current = panel;
    panel = null; frame = null; token = null; closing = false;
    current.close(); current.remove();
    if (previousFocus && previousFocus.isConnected) previousFocus.focus({preventScroll:true});
    previousFocus = null;
    refreshAccount();
  }
  function close() {
    if (!panel || closing) return;
    const value = marker();
    if (value && value.token === token && value.userId === auth.user.id) { closing = true; history.back(); }
    else dispose();
  }
  function show(value) {
    if (panel) return true;
    previousFocus = document.activeElement; token = value.token;
    panel = document.createElement("dialog"); panel.className = "account-panel"; panel.id = "accountPanel";
    panel.setAttribute("aria-label", "账号与安全");
    const button = document.createElement("button"); button.type = "button"; button.className = "account-panel-close";
    button.textContent = "×"; button.setAttribute("aria-label", "关闭账号与安全"); button.addEventListener("click", close);
    const status = document.createElement("div"); status.className = "account-panel-loading";
    status.setAttribute("role", "status"); status.textContent = "正在打开账号设置…";
    const fallback = document.createElement("a"); fallback.textContent = "在独立页面打开";
    const url = new URL("/ui/account.html", location.origin);
    const sid = App.state.sessionId || new URL(location.href).searchParams.get("session");
    if (sid) url.searchParams.set("session", sid);
    fallback.href = url.pathname + url.search; fallback.hidden = true;
    status.append(document.createElement("br"), fallback);
    frame = document.createElement("iframe"); frame.title = "账号与安全"; frame.hidden = true;
    url.searchParams.set("panel", token); frame.src = url.pathname + url.search;
    panel.append(button, status, frame);
    panel.addEventListener("cancel", event => { event.preventDefault(); close(); });
    panel.addEventListener("close", event => { if (panel === event.currentTarget) close(); });
    document.body.append(panel); panel.showModal();
    timeout = setTimeout(() => {
      if (panel) {
        status.firstChild.textContent = "账号页面暂时无法打开，请重试或使用独立页面。"; fallback.hidden = false;
        if (auth.refresh) auth.refresh().catch(() => {});
      }
    }, 8000);
    return true;
  }
  function open() {
    if (!auth.accountPanelAllowed || typeof HTMLDialogElement === "undefined" || !HTMLDialogElement.prototype.showModal || !crypto.randomUUID) return false;
    if (panel) return true;
    const value = {token:crypto.randomUUID().replace(/-/g,""), userId:auth.user.id};
    const url = new URL(location.href); url.hash = "account";
    try { history.pushState({...history.state, [stateKey]:value}, "", url.href); } catch (_) { return false; }
    return show(value);
  }
  window.addEventListener("popstate", () => {
    const value = marker();
    if (auth.accountPanelAllowed && value && value.userId === auth.user.id && /^[a-f0-9]{32}$/.test(value.token)) show(value);
    else dispose();
  });
  window.addEventListener("message", event => {
    const data = event.data;
    if (!frame || event.origin !== location.origin || event.source !== frame.contentWindow || !data || data.channel !== "ytools-account-panel" || data.token !== token) return;
    if (data.userId !== auth.user.id) { auth.lost(); return; }
    if (data.type === "ready") { clearTimeout(timeout); frame.hidden = false; panel.querySelector(".account-panel-loading").hidden = true; }
    else if (data.type === "close") close();
    else if (data.type === "auth-lost") auth.lost(data.reason === "username_changed" ? data.reason : undefined);
  });
  App.accountPanel = {open, close, isOpen:() => !!panel};
  const value = marker();
  if (auth.accountPanelAllowed && value && value.userId === auth.user.id && /^[a-f0-9]{32}$/.test(value.token)) show(value);
})(window.App);
