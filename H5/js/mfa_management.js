/* Manage already-enrolled accounts without exporting an active TOTP secret. */
(function (global) {
  "use strict";
  function create(auth, api, refreshDevices) {
    const el = id => document.getElementById(id), section = el("mfaManage"), form = el("mfaManageForm");
    const proof = el("mfaManageProof"), submit = el("mfaManageSubmit"), cancel = el("mfaManageCancel"), list = el("mfaDeviceList");
    let pending = null, busy = false;
    function say(text, error) { el("mfaManageStatus").textContent = text; el("mfaManageStatus").classList.toggle("is-error", !!error); }
    const binding = global.YtoolsMfaEnrollment.create(el("mfaManageEnrollment"), reason => reset(reason, true));
    function syncHint() {
      const replace = form.elements.mode.value === "replace";
      el("mfaManageHint").textContent = replace ? "确认新验证器后，全部旧验证器和旧恢复码失效，其他登录会被撤销。确认前原有验证器仍有效。" : "新验证器使用独立密钥，确认后现有验证器继续有效。以后可单独移除不再使用的验证器。";
      if (!pending) submit.textContent = replace ? "生成更换二维码" : "生成添加二维码";
    }
    function setBusy(value) {
      busy = value; submit.disabled = value; proof.disabled = value || !!pending; form.elements.new_code.disabled = value;
      form.querySelectorAll('button[type="button"]').forEach(button => { button.disabled = value; });
      list.querySelectorAll("button").forEach(button => { button.disabled = value || !!pending || button.dataset.last === "true"; });
      el("mfaRecoveryRegenerate").disabled = value || !!pending;
    }
    function reset(reason, abandon) {
      const old = pending, mode = form.elements.mode.value, label = form.elements.label.value;
      pending = null; binding.clear(); form.reset();
      if (reason) { form.elements.mode.value = mode; form.elements.label.value = label; }
      proof.hidden = false; proof.disabled = false; form.elements.new_code.required = false;
      el("mfaManageNewCodeLabel").hidden = true; cancel.hidden = true; syncHint(); setBusy(busy);
      if (abandon && old) api("/auth/mfa/manage/cancel", "POST", { operation_id: old.operation_id }).catch(error => { if (!pending) say(error.message, true); });
      if (reason) say(reason === "expired" ? "二维码已过期，原有验证器仍有效；请重新验证身份生成。" : "本次二维码已关闭，原有验证器保持有效。重新生成需再次验证身份。");
    }
    async function load() {
      section.hidden = !auth.user.mfa_enabled || !!auth.user.password_change_required;
      if (section.hidden) return;
      const data = await api("/auth/mfa/devices"), devices = data.devices || [];
      el("mfaRecoveryRegenerate").hidden = !Number.isInteger(data.recovery_codes_remaining);
      el("mfaRecoveryCount").textContent = Number.isInteger(data.recovery_codes_remaining)
        ? "剩余 " + data.recovery_codes_remaining + " 条未使用恢复码；仅显示数量，无法重新查看旧码。" : "请将恢复码与手机分开保管；设备丢失时可用它恢复登录。";
      list.replaceChildren();
      devices.forEach(device => {
        const row = document.createElement("div"), text = document.createElement("span"), button = document.createElement("button");
        row.className = "mfa-device-row"; text.textContent = device.label; button.textContent = "移除"; button.type = "button";
        button.dataset.last = String(devices.length <= 1); button.disabled = devices.length <= 1 || busy || !!pending;
        if (devices.length <= 1) button.title = "至少保留一个验证器；需要切换时使用更换";
        button.addEventListener("click", async () => {
          if (busy || pending) return;
          const password = form.elements.password.value, code = form.elements.code.value;
          if (!password || !code) { say("移除前，请先在下方填写当前密码和当前动态码或恢复码。", true); form.elements.password.focus(); return; }
          if (!global.confirm("移除“" + device.label + "”？该验证器将不能登录，其他登录将被撤销。")) return;
          setBusy(true);
          try {
            await api("/auth/mfa/devices/" + encodeURIComponent(device.id), "DELETE", { password, code });
            reset(); await load(); await refreshDevices(); say("验证器已移除，其他登录已撤销。");
          } catch (error) { say(error.message, true); }
          finally { setBusy(false); }
        });
        row.append(text, button); list.append(row);
      });
    }
    form.elements.mode.addEventListener("change", syncHint);
    cancel.addEventListener("click", () => { if (!busy) reset("cancel", true); });
    form.addEventListener("submit", async event => {
      event.preventDefault(); if (busy) return; setBusy(true);
      try {
        if (!pending) {
          const response = await api("/auth/mfa/manage/start", "POST", { mode: form.elements.mode.value, label: form.elements.label.value,
            password: form.elements.password.value, code: form.elements.code.value });
          pending = { operation_id: response.binding.operation_id, mode: response.binding.mode };
          try { binding.show(response.binding); } catch (error) { reset("invalid", true); throw error; }
          form.elements.password.value = ""; form.elements.code.value = ""; proof.hidden = true;
          el("mfaManageNewCodeLabel").hidden = false; form.elements.new_code.required = true; cancel.hidden = false;
          submit.textContent = pending.mode === "replace" ? "确认更换验证器" : "确认添加验证器";
          say("请用新的验证器扫描二维码，并输入新条目的动态码确认。"); await refreshDevices(); form.elements.new_code.focus();
        } else {
          const response = await api("/auth/mfa/manage/confirm", "POST", { operation_id: pending.operation_id, code: form.elements.new_code.value });
          reset();
          if (response.recovery_codes && response.recovery_codes.length) {
            el("mfaManageCodes").textContent = response.recovery_codes.join("\n"); el("mfaManageRecovery").hidden = false;
          }
          await load(); await refreshDevices();
          say(response.mode === "replace" ? "更换成功，旧验证器已停用。请保存新的恢复码。" : "添加成功，新旧验证器都可登录；如需停用旧的，可在列表中移除。");
        }
      } catch (error) { say(error.message, true); }
      finally { setBusy(false); }
    });
    el("mfaManageCodesSaved").addEventListener("click", () => { el("mfaManageCodes").textContent = ""; el("mfaManageRecovery").hidden = true; });
    global.YtoolsRecoveryCodes.bindDownload(el("mfaManageCodesDownload"), el("mfaManageCodes"));
    el("mfaRecoveryRegenerate").addEventListener("click", async () => {
      if (busy || pending) return;
      const password = form.elements.password.value, code = form.elements.code.value;
      if (!password || !code) { say("请先在上方填写当前密码和有效动态码或恢复码。", true); form.elements.password.focus(); return; }
      if (!global.confirm("重新生成恢复码？全部旧恢复码将失效，其他登录会被撤销；现有验证器保留。")) return;
      setBusy(true);
      try {
        const response = await api("/auth/mfa/recovery/regenerate", "POST", { password, code });
        reset(); el("mfaManageCodes").textContent = response.recovery_codes.join("\n"); el("mfaManageRecovery").hidden = false;
        await load(); await refreshDevices(); say("恢复码已重新生成，请下载或安全保存。旧恢复码已失效，现有验证器保持有效。");
      } catch (error) { say(error.message, true); }
      finally { setBusy(false); }
    });
    global.addEventListener("pagehide", () => { el("mfaManageCodes").textContent = ""; el("mfaManageRecovery").hidden = true; });
    syncHint();
    return { load, reset: () => { if (pending) reset("reauth", true); } };
  }
  global.YtoolsMfaManagement = { create };
})(window);
