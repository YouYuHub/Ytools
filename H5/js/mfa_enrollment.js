/* Shared, offline TOTP enrollment for Google and Microsoft Authenticator. */
(function (global) {
  "use strict";
  function create(root, onRestart) {
    root.innerHTML = '<div class="mfa-qr-panel"><img class="mfa-qr" alt="使用 Google Authenticator 或 Microsoft Authenticator 扫码绑定" hidden>' +
      '<p class="mfa-qr-status" role="status"></p></div>' +
      '<div class="mfa-app-help"><p><strong>Google Authenticator</strong>：＋ → 扫描二维码</p>' +
      '<p><strong>Microsoft Authenticator</strong>：＋ → 其他帐户 → 扫描二维码</p></div>' +
      '<p class="mfa-expiry" aria-live="off"></p>' +
      '<details class="mfa-manual"><summary>无法扫码？手动输入密钥</summary><code class="auth-secret"></code>' +
      '<p>名称：Ytools；类型：基于时间（TOTP）；6 位，30 秒更新。</p><button type="button" class="mfa-copy">复制密钥</button></details>' +
      '<small>二维码和密钥仅供本人绑定，请勿分享。任选一种验证器即可。</small>' +
      '<button type="button" class="mfa-restart">重新生成二维码</button>';
    const img = root.querySelector("img"), status = root.querySelector(".mfa-qr-status");
    const manual = root.querySelector("details"), key = root.querySelector("code"), expiry = root.querySelector(".mfa-expiry");
    let secret = "", timer = null, resetOnReturn = false;
    function clear() {
      if (timer !== null) global.clearInterval(timer);
      timer = null; secret = ""; key.textContent = ""; expiry.textContent = ""; status.textContent = "";
      img.removeAttribute("src"); img.hidden = true; manual.open = false; root.hidden = true;
    }
    function restart(reason) { clear(); if (onRestart) onRestart(reason); }
    root.querySelector(".mfa-restart").addEventListener("click", () => restart("renew"));
    root.querySelector(".mfa-copy").addEventListener("click", async () => {
      if (!secret) return;
      try { await global.navigator.clipboard.writeText(secret); status.textContent = "密钥已复制"; }
      catch (_) { manual.open = true; status.textContent = "请选中密钥手动复制"; }
    });
    function show(data) {
      clear();
      let uri;
      try { if (typeof data.uri !== "string" || data.uri.length > 1024) throw new Error(); uri = new URL(data.uri); } catch (_) { throw new Error("验证器绑定信息无效，请重新生成"); }
      if (uri.protocol !== "otpauth:" || uri.hostname !== "totp" || uri.searchParams.get("secret") !== data.secret ||
          uri.username || uri.password || uri.hash || !/^[A-Z2-7]{16,128}$/.test(data.secret) || (uri.searchParams.get("issuer") || "") !== "Ytools" ||
          (uri.searchParams.get("algorithm") || "SHA1").toUpperCase() !== "SHA1" ||
          (uri.searchParams.get("digits") || "6") !== "6" || (uri.searchParams.get("period") || "30") !== "30") {
        throw new Error("验证器绑定信息无效，请重新生成");
      }
      const deadline = Number.isFinite(data.expires_at) ? data.expires_at * 1000 : Date.now() + 300000;
      if (deadline <= Date.now()) throw new Error("二维码已过期，请重新生成");
      secret = data.secret; key.textContent = secret; root.hidden = false;
      try {
        const qr = global.qrcode(0, "M"); qr.addData(data.uri, "Byte"); qr.make();
        img.src = qr.createDataURL(6, 24); img.hidden = false;
        status.textContent = "用手机验证器扫码，再输入应用中的 6 位动态码";
      } catch (_) {
        status.textContent = "二维码暂不可用，请展开下方手动密钥添加账号"; manual.open = true;
      }
      function tick() {
        const seconds = Math.max(0, Math.ceil((deadline - Date.now()) / 1000));
        if (!seconds) { restart("expired"); return; }
        expiry.textContent = "绑定剩余时间 " + Math.floor(seconds / 60) + ":" + String(seconds % 60).padStart(2, "0");
      }
      timer = global.setInterval(tick, 1000); tick();
    }
    global.addEventListener("pagehide", () => { resetOnReturn = !!secret; clear(); });
    global.addEventListener("pageshow", event => { if (event.persisted && resetOnReturn) { resetOnReturn = false; restart("return"); } });
    clear();
    return { show, clear };
  }
  global.YtoolsMfaEnrollment = { create };
})(window);
