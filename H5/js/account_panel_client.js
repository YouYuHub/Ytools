/* The parent validates origin, frame, account ID and this per-opening token. */
(function (global) {
  "use strict";
  const token = new URLSearchParams(location.search).get("panel"), auth = global.YtoolsAuth;
  if (global.parent === global || !auth || !/^[a-f0-9]{32}$/.test(token || "")) return;
  function send(type, reason) {
    global.parent.postMessage({channel:"ytools-account-panel", token, userId:auth.user.id, type, reason}, location.origin);
  }
  global.addEventListener("ytools-auth-lost", event => { event.preventDefault(); send("auth-lost", event.detail && event.detail.reason); });
  document.addEventListener("DOMContentLoaded", () => {
    const back = document.getElementById("backToChat");
    if (back) back.addEventListener("click", event => {
      if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      event.preventDefault(); send("close");
    });
    send("ready");
  });
})(window);
