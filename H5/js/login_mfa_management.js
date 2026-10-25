/* Manage enrollment during a password-verified login; never retain the password. */
(function (global) {
  "use strict";
  function create({ api, message, authenticated, complete, busy: setPageBusy }) {
    const el = id => document.getElementById(id), form = el("loginMfaForm");
    const root = el("loginMfaManage"), proof = el("loginMfaProof"), opener = el("loginMfaOpen");
    const submit = el("loginMfaSubmit"), cancel = el("loginMfaCancel");
    let pending = null, busy = false, restartReason = null;
    const enrollment = global.YtoolsMfaEnrollment.create(el("loginMfaEnrollment"), reason => {
      if (busy) restartReason = reason;
      else stop(reason);
    });
    function hint() {
      el("loginMfaHint").textContent = form.elements.mode.value === "replace"
        ? "新验证器确认成功后才停用全部旧验证器，并更新恢复码。" : "添加后，新旧验证器都可登录。";
    }
    function clear() {
      enrollment.clear(); pending = null; restartReason = null; form.reset();
      root.hidden = true; opener.hidden = true; proof.hidden = false;
      form.elements.code.required = true; form.elements.new_code.required = false;
      el("loginMfaNewCode").hidden = true; submit.textContent = "验证身份并生成二维码";
      cancel.textContent = "返回验证码登录"; hint();
    }
    function setBusy(value) {
      busy = value; submit.disabled = value; cancel.disabled = value;
      proof.disabled = value; form.elements.new_code.disabled = value; setPageBusy(value);
    }
    async function stop(reason) {
      if (busy) return;
      if (!pending) {
        clear(); opener.hidden = false; el("mfaForm").hidden = false; el("mfaForm").elements.code.focus(); return;
      }
      const operation = pending.operation_id; setBusy(true);
      try {
        await api("/auth/mfa/manage/cancel", { operation_id: operation });
        clear();
        complete({}, reason === "expired" ? "二维码已过期，现有验证器仍可使用。可进入账号与安全重新添加。"
          : "已取消本次绑定，现有验证器保持有效。可进入账号与安全重新生成二维码。");
      } catch (error) { message(error.message, true); }
      finally { setBusy(false); }
    }
    opener.addEventListener("click", () => {
      if (busy) return;
      opener.hidden = true; el("mfaForm").hidden = true; root.hidden = false; form.elements.code.focus();
      message("验证当前动态码或恢复码后，即可扫码添加或更换验证器");
    });
    form.elements.mode.addEventListener("change", hint);
    cancel.addEventListener("click", () => stop("cancel"));
    form.addEventListener("submit", async event => {
      event.preventDefault(); if (busy) return;
      setBusy(true);
      try {
        if (!pending) {
          const data = await api("/auth/mfa/manage/login_start", { mode: form.elements.mode.value,
            label: form.elements.label.value, code: form.elements.code.value.trim() });
          authenticated(data);
          pending = { operation_id: data.binding.operation_id, mode: data.binding.mode };
          try { enrollment.show(data.binding); }
          catch (error) { restartReason = "invalid"; throw error; }
          form.elements.code.value = ""; form.elements.code.required = false; proof.hidden = true;
          el("loginMfaNewCode").hidden = false; form.elements.new_code.required = true;
          submit.textContent = pending.mode === "replace" ? "确认更换验证器" : "确认添加验证器";
          cancel.textContent = "取消本次绑定";
          message("请用新验证器扫码，再输入新条目的 6 位动态码确认");
        } else {
          const data = await api("/auth/mfa/manage/confirm", { operation_id: pending.operation_id, code: form.elements.new_code.value.trim() });
          clear(); complete(data, data.mode === "replace" ? "更换成功，全部旧验证器和旧恢复码已停用。请保存新的恢复码。"
            : "添加成功，新旧验证器都可登录。");
        }
      } catch (error) {
        message(error.message, true);
        const field = pending ? form.elements.new_code : form.elements.code; field.select();
      } finally {
        setBusy(false);
        if (pending && restartReason) { const reason = restartReason; restartReason = null; stop(reason); }
        else if (pending) form.elements.new_code.focus();
      }
    });
    global.addEventListener("pagehide", clear);
    clear();
    return { clear, show: user => { opener.hidden = !user.mfa_enabled || !!user.password_change_required; } };
  }
  global.YtoolsLoginMfaManagement = { create };
})(window);
