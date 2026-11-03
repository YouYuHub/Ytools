/** 账户面板客户端依赖的 YtoolsAuth 子集（用户 ID 用于 postMessage 校验）。 */
interface PanelAuthLike {
  user: { id: string };
}

/* The parent validates origin, frame, account ID and this per-opening token. */
(function (global: Window) {
  "use strict";
  const token = new URLSearchParams(location.search).get("panel"),
    auth = (global as unknown as { YtoolsAuth?: PanelAuthLike }).YtoolsAuth;
  if (global.parent === global || !auth || !/^[a-f0-9]{32}$/.test(token || "")) return;
  function send(type: string, reason?: unknown): void {
    global.parent.postMessage({ channel: "ytools-account-panel", token, userId: auth!.user.id, type, reason }, location.origin);
  }
  global.addEventListener("ytools-auth-lost", event => { event.preventDefault(); send("auth-lost", (event as CustomEvent).detail && (event as CustomEvent).detail.reason); });
  document.addEventListener("DOMContentLoaded", () => {
    const back = document.getElementById("backToChat");
    if (back) back.addEventListener("click", event => {
      if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      event.preventDefault(); send("close");
    });
    send("ready");
  });
})(window);
