(function () {
  "use strict";
  const auth = window.YtoolsAuth, el = id => document.getElementById(id);
  if (!auth) return;
  const returnSession = new URLSearchParams(location.search).get("session");
  if (returnSession) el("backToChat").href = "/ui/?session=" + encodeURIComponent(returnSession);
  let editedUser = null;
  let enrollingMfa = false;
  function message(text, error) { el("accountMessage").textContent = text; el("accountMessage").classList.toggle("is-error", !!error); }
  const enrollment = window.YtoolsMfaEnrollment.create(el("mfaEnrollment"), resetMfa);
  function resetMfa(reason) {
    enrollment.clear(); enrollingMfa = false; const form = el("mfaSetupForm"); form.reset();
    el("mfaPasswordLabel").hidden = false; el("mfaCodeLabel").hidden = true;
    form.elements.password.required = true; form.elements.code.required = false;
    el("mfaSetupButton").textContent = "生成验证二维码";
    if (reason) message(reason === "expired" ? "二维码已过期，请重新输入密码生成" : "请重新输入密码生成二维码；上一张未完成绑定的二维码请勿继续使用");
  }
  async function api(path, method, data) {
    const options = { method: method || "GET", headers: { "Content-Type": "application/json" } };
    if (data !== undefined) options.body = JSON.stringify(data);
    const response = await fetch(path, options), result = await response.json();
    if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "请检查输入");
    if (result.csrf) auth.update(result);
    return result;
  }
  function bind(id, handler) { el(id).addEventListener("submit", async event => {
    event.preventDefault(); const button = event.submitter || event.target.querySelector('button[type="submit"]') || event.target.querySelector("button"); button.disabled = true;
    try { await handler(new FormData(event.target), event.target); } catch (error) { message(error.message, true); }
    finally { button.disabled = false; }
  }); }
  const mfaManager = window.YtoolsMfaManagement.create(auth, api, devices);
  const usernameManager = window.YtoolsUsernameManagement.create(auth, api, message);
  window.YtoolsRecoveryCodes.bindDownload(el("mfaCodesDownload"), el("mfaCodes"));
  async function devices() {
    if (auth.user.password_change_required) return;
    const data = await api("/auth/sessions"); el("devices").replaceChildren();
    data.sessions.forEach(item => { const row = document.createElement("div"), text = document.createElement("span"), button = document.createElement("button");
      text.textContent = (item.id === data.current ? "当前设备 · " : "") + (item.label || "未知设备") + " · " + new Date(item.last_seen * 1000).toLocaleString();
      button.textContent = "撤销"; button.addEventListener("click", async () => { try { await api("/auth/sessions/" + encodeURIComponent(item.id), "DELETE"); if (item.id === data.current) auth.lost(); else await devices(); } catch (error) { message(error.message, true); } });
      row.append(text, button); el("devices").append(row);
    });
  }
  async function users() {
    if (auth.user.role !== "admin" || auth.user.password_change_required) return;
    el("adminUsers").hidden = false; el("adminAudit").hidden = false;
    const data = await api("/admin/users"); el("users").replaceChildren();
    data.users.forEach(user => { const row = document.createElement("tr");
      [user.username + " · " + user.name, user.role, user.enabled ? "启用" : "停用", user.mfa_enabled ? "已设置" : "待设置"].forEach(value => { const cell = document.createElement("td"); cell.textContent = value; row.append(cell); });
      const cell = document.createElement("td"), button = document.createElement("button"); button.textContent = user.enabled ? "停用" : "启用";
      button.addEventListener("click", async () => { if (!confirm("确认" + button.textContent + "此账号？停用将撤销登录并阻止继续执行。")) return;
        try { await api("/admin/users/" + encodeURIComponent(user.id), "PATCH", { enabled: !user.enabled }); await users(); } catch (error) { message(error.message, true); }
      }); cell.append(button);
      const edit = document.createElement("button"); edit.textContent = "管理";
      edit.addEventListener("click", async () => { try {
        const data = await api("/admin/users/" + encodeURIComponent(user.id) + "/limits");
        editedUser = user; const form = el("editUserForm"); form.reset();
        form.elements.role.value = user.role;
        form.elements.daily_calls.value = data.limits.daily_calls; form.elements.daily_tokens.value = data.limits.daily_tokens;
        form.elements.storage_gib.value = data.limits.storage_bytes / (1024 ** 3);
        form.elements.concurrent_models.value = data.limits.concurrent_models;
        el("editUserTitle").textContent = "管理 " + user.username; el("editUser").showModal();
      } catch (error) { message(error.message, true); } });
      cell.append(edit); row.append(cell); el("users").append(row);
    });
    el("audit").textContent = JSON.stringify((await api("/admin/audit")).events, null, 2);
  }
  function renderAccountName() { el("accountName").textContent = auth.user.name + " · " + auth.user.username; }
  bind("profileForm", async (fields, form) => {
    const data = await api("/user/profile", "POST", { name: fields.get("name") });
    auth.update({ user: { ...auth.user, name: data.name } });
    form.elements.name.value = data.name; renderAccountName(); message("显示名已保存");
  });
  bind("passwordForm", async (fields, form) => { await api("/user/password", "POST", { old_password: fields.get("old_password"), password: fields.get("password") }); form.reset(); auth.lost(); });
  bind("reauthForm", async (fields, form) => { await api("/auth/reauthenticate", "POST", { password: fields.get("password"), code: fields.get("code") }); mfaManager.reset(); form.reset(); message("身份验证成功"); await devices(); });
  bind("createForm", async (fields, form) => { await api("/admin/users", "POST", { username: fields.get("username"), password: fields.get("password"), role: fields.get("role"), name: fields.get("name") || null, trusted: fields.get("trusted") === "on" }); form.reset(); message("账号已创建"); await users(); });
  if (!auth.user.mfa_enabled && !auth.user.password_change_required) el("mfaSetup").hidden = false;
  bind("mfaSetupForm", async (fields, form) => {
    if (!enrollingMfa) {
      const data = await api("/auth/mfa/start", "POST", { password: fields.get("password") });
      enrollment.show(data);
      form.elements.password.value = ""; el("mfaPasswordLabel").hidden = true;
      form.elements.password.required = false; form.elements.code.required = true;
      el("mfaCodeLabel").hidden = false; el("mfaSetupButton").textContent = "验证并启用"; enrollingMfa = true;
    } else {
      const data = await api("/auth/mfa/confirm", "POST", { code: fields.get("code") });
      enrollment.clear(); form.reset(); form.hidden = true;
      el("mfaCodes").textContent = data.recovery_codes.join("\n"); el("mfaRecovery").hidden = false;
      message("双重验证已启用，请保存恢复码"); await devices(); await usernameManager.load();
    }
  });
  el("mfaCodesSaved").addEventListener("click", async () => { el("mfaCodes").textContent = ""; el("mfaSetup").hidden = true; try { await mfaManager.load(); } catch (error) { message(error.message, true); } });
  bind("editUserForm", async (fields, form) => {
    if (!editedUser) return; const path = "/admin/users/" + encodeURIComponent(editedUser.id);
    if (fields.get("password") && editedUser.id === auth.user.id) throw new Error("请使用个人修改密码入口");
    if (fields.get("role") !== editedUser.role) await api(path, "PATCH", { role: fields.get("role"), trusted: fields.get("trusted") === "on" });
    await api(path + "/limits", "PUT", { daily_calls: Number(fields.get("daily_calls")), daily_tokens: Number(fields.get("daily_tokens")),
      storage_bytes: Math.round(Number(fields.get("storage_gib")) * (1024 ** 3)), concurrent_models: Number(fields.get("concurrent_models")) });
    if (fields.get("password")) await api(path + "/password", "POST", { password: fields.get("password") });
    form.reset(); el("editUser").close(); message("账号设置已保存"); await users();
  });
  el("cancelEdit").addEventListener("click", () => { el("editUserForm").reset(); el("editUser").close(); });
  el("logout").addEventListener("click", () => auth.logout().catch(error => message(error.message, true)));
  el("logoutAll").addEventListener("click", async () => { try { await api("/auth/logout_all", "POST"); auth.lost(); } catch (error) { message(error.message, true); } });
  renderAccountName();
  window.addEventListener("pagehide", () => { el("mfaCodes").textContent = ""; });
  el("profileForm").elements.name.value = auth.user.name;
  if (auth.user.password_change_required) {
    el("profileForm").hidden = true; el("devices").closest("section").hidden = true;
    message("这是初始或临时密码，请先在下方修改。修改后重新登录即可使用工作区和账号管理。");
  }
  const usage = auth.user.password_change_required ? Promise.resolve() : api("/user/usage").then(data => {
    const storage = data.storage ? " 存储 " + (data.storage.used_bytes / (1024 ** 3)).toFixed(2) + " GiB" + (data.storage.limit_bytes ? " / " + (data.storage.limit_bytes / (1024 ** 3)).toFixed(2) + " GiB。" : "（不限）。") : "";
    el("usage").textContent = "今日已用 " + data.used.daily_calls + " 次模型请求、" + data.used.daily_tokens.toLocaleString() + " tokens（UTC 日统计）。" + storage + (auth.executionAllowed ? "工具执行已授权。" : "当前为无工具聊天账号。");
  });
  Promise.all([devices(), users(), usage, mfaManager.load(), usernameManager.load()]).catch(error => message(error.message, true));
})();
