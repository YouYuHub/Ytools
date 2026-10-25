(function () {
  "use strict";
  const el = id => document.getElementById(id);
  let csrf = "", enrolling = false, nextPath = "/ui/", loginManagementAvailable = false;
  function message(text, error) { el("authMessage").textContent = text; el("authMessage").classList.toggle("is-error", !!error); }
  const enrollment = window.YtoolsMfaEnrollment.create(el("mfaEnrollment"), resetEnrollment);
  const manager = window.YtoolsLoginMfaManagement.create({ api, message, authenticated, complete,
    busy: setBusy });
  function setBusy(value) {
    el("changeAccount").disabled = value; el("loginMfaOpen").disabled = value;
  }
  function authenticated(data) {
    if (data.user) nextPath = data.user.password_change_required ? "/ui/account.html" : "/ui/";
  }
  function showLogin() {
    resetEnrollment(); manager.clear(); csrf = "";
    ["mfaForm", "enrollForm", "recovery", "loginComplete", "changeAccount"].forEach(id => { el(id).hidden = true; });
    el("recoveryCodes").textContent = ""; el("loginForm").hidden = false;
  }
  function complete(data, notice) {
    authenticated(data); resetEnrollment(); manager.clear();
    ["loginForm", "mfaForm", "enrollForm", "recovery", "loginComplete", "changeAccount"].forEach(id => { el(id).hidden = true; });
    const codes = data.recovery_codes || [];
    el("recoveryCodes").textContent = codes.join("\n");
    el(codes.length ? "recovery" : "loginComplete").hidden = false; message(notice);
  }
  function resetEnrollment(reason) {
    enrollment.clear(); enrolling = false; el("enrollForm").reset();
    el("enrollPassword").hidden = false; el("enrollForm").elements.password.required = true;
    el("enrollCode").hidden = true; el("enrollForm").elements.code.required = false;
    el("enrollButton").textContent = "生成验证二维码";
    if (reason) message(reason === "expired" ? "二维码已过期，请重新登录或重新输入密码生成" : "请重新输入密码生成二维码；上一张未完成绑定的二维码请勿继续使用");
  }
  async function api(path, body) {
    const options = { credentials: "same-origin", cache: "no-store" };
    if (body !== undefined) {
      options.method = "POST"; options.headers = { "Content-Type": "application/json", "X-CSRF-Token": csrf };
      options.body = JSON.stringify(body);
    }
    const response = await fetch(path, options);
    const data = await response.json();
    if (!response.ok) {
      if (response.status === 401 && path.startsWith("/auth/mfa/")) {
        // Older servers also used 401 for an incorrect factor. Only discard the
        // password-verified challenge if the cookie really expired or was revoked.
        try {
          const current = await fetch("/auth/me", { credentials: "same-origin", cache: "no-store" });
          if (current.ok) { const saved = await current.json(); if (saved.csrf) csrf = saved.csrf; }
          else if (current.status === 401) showLogin();
        } catch (_) { /* A network error does not invalidate the pending login. */ }
      }
      const error = new Error(typeof data.detail === "string" ? data.detail : "请求未成功，请检查输入");
      error.status = response.status; throw error;
    }
    if (data.csrf) csrf = data.csrf;
    return data;
  }
  function state(data) {
    authenticated(data); resetEnrollment(); manager.clear();
    if (data.mfa_verified) { location.replace(nextPath); return; }
    el("loginForm").hidden = true;
    el("mfaForm").hidden = !data.user.mfa_enabled;
    el("enrollForm").hidden = !!data.user.mfa_enabled;
    el("mfaCodeLabelText").textContent = "验证器验证码或恢复码"; el("mfaForm").elements.code.placeholder = "";
    el("changeAccount").hidden = false; if (loginManagementAvailable) manager.show(data.user);
    el("replaceLostDevice").hidden = !loginManagementAvailable || !!data.user.password_change_required;
    el("mfaResetCommand").textContent = "python scripts/manage_accounts.py reset-mfa --username " + (data.user.username || "YOUR_ACCOUNT");
    el("enrollIntro").textContent = data.user.mfa_setup_required ? "验证器已由服务器管理员重置。请用新设备扫码绑定，完成后才能进入工作区。"
      : "首次绑定：使用 Google Authenticator 或 Microsoft Authenticator 扫码，再输入 6 位动态码确认。";
    message(data.user.mfa_enabled ? "请输入双重验证信息" : data.user.mfa_setup_required ? "账号恢复：请重新绑定验证器" : "首次登录：设置双重验证");
    if (data.user.mfa_enabled) el("mfaForm").elements.code.focus();
  }
  function bind(id, handler) {
    el(id).addEventListener("submit", async event => {
      event.preventDefault(); const button = event.target.querySelector("button[type=submit]");
      button.disabled = true; setBusy(true);
      try { await handler(new FormData(event.target), event.target); }
      catch (error) {
        message(error.message, true);
        if (!event.target.hidden && event.target.elements.code) { event.target.elements.code.focus(); event.target.elements.code.select(); }
      }
      finally { button.disabled = false; setBusy(false); }
    });
  }
  bind("loginForm", async (fields, form) => {
    const data = await api("/auth/login", { username: fields.get("username"), password: fields.get("password") });
    form.elements.password.value = ""; state(data);
  });
  bind("mfaForm", async fields => state(await api("/auth/mfa/verify", { code: fields.get("code").trim() })));
  bind("enrollForm", async (fields, form) => {
    if (!enrolling) {
      const data = await api("/auth/mfa/start", { password: fields.get("password") });
      enrollment.show(data);
      form.elements.password.value = ""; el("enrollPassword").hidden = true; el("enrollCode").hidden = false;
      form.elements.password.required = false; form.elements.code.required = true;
      el("enrollButton").textContent = "确认验证码"; enrolling = true; form.elements.code.focus();
    } else {
      const data = await api("/auth/mfa/confirm", { code: fields.get("code") });
      complete(data, "双重验证已启用，请保存恢复码");
    }
  });
  el("recoveryCopy").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(el("recoveryCodes").textContent); message("恢复码已复制，请保存到安全的位置"); }
    catch (_) { message("请选中恢复码手动复制"); }
  });
  window.YtoolsRecoveryCodes.bindDownload(el("recoveryDownload"), el("recoveryCodes"));
  el("useRecoveryCode").addEventListener("click", () => {
    el("mfaCodeLabelText").textContent = "恢复码（一次性）";
    el("mfaForm").elements.code.placeholder = "粘贴一条未使用的恢复码"; el("mfaForm").elements.code.focus();
    message("输入一条未使用恢复码即可登录。设备丢失时，建议选择更换验证器以停用旧设备。");
  });
  el("replaceLostDevice").addEventListener("click", () => {
    const code = el("mfaForm").elements.code.value;
    el("loginMfaOpen").click(); const form = el("loginMfaForm"); form.elements.mode.value = "replace";
    form.elements.mode.dispatchEvent(new Event("change")); form.elements.code.value = code;
  });
  function enter() { el("recoveryCodes").textContent = ""; location.replace(nextPath); }
  el("enterApp").addEventListener("click", enter); el("loginCompleteEnter").addEventListener("click", enter);
  el("changeAccount").addEventListener("click", async () => {
    setBusy(true);
    try { await api("/auth/logout", {}); showLogin(); message("请输入账号和密码"); el("loginForm").elements.username.focus(); }
    catch (error) { if (error.status === 401) { showLogin(); message("登录已过期，请重新输入密码"); } else message(error.message, true); }
    finally { setBusy(false); }
  });
  window.addEventListener("pagehide", () => {
    el("recoveryCodes").textContent = "";
    document.querySelectorAll('input[type="password"], input[autocomplete="one-time-code"]').forEach(input => { input.value = ""; });
  });
  window.addEventListener("pageshow", event => {
    if (event.persisted) api("/auth/me").then(state).catch(() => { showLogin(); message("请重新登录"); });
  });
  (async () => {
    try {
      const status = await api("/auth/status");
      loginManagementAvailable = status.login_mfa_management === true;
      if (!status.initialized) { el("setupHelp").hidden = false; message("账号尚未初始化"); return; }
      try { state(await api("/auth/me")); }
      catch (_) { showLogin(); message(new URLSearchParams(location.search).get("username_changed") === "1"
        ? "登录用户名已修改，请使用新用户名和原密码重新登录。验证器继续有效。" : "欢迎回来"); }
    } catch (error) { message(error.message, true); }
  })();
})();
