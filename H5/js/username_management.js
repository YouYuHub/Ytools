/* Login names are editable; immutable account IDs keep all owned data in place. */
(function (global) {
  "use strict";
  function create(auth, api, message) {
    const el = id => document.getElementById(id), section = el("usernameSettings"), form = el("usernameForm");
    const button = el("usernameSubmit"), code = form.elements.code;
    let settings = null, busy = false;
    async function load() {
      if (auth.user.password_change_required) return;
      // Hide this entry during a rolling deployment until the backend supports it.
      if (!(await api("/auth/status")).username_change) return;
      settings = await api("/user/username");
      el("currentUsername").textContent = settings.username;
      el("usernameCodeLabel").hidden = !settings.mfa_required;
      code.required = settings.mfa_required;
      button.disabled = !settings.can_change;
      el("usernameStatus").textContent = settings.mfa_setup_required
        ? "管理员和操作员请先绑定验证器，再刷新本页修改用户名。"
        : !settings.can_change ? "下次可修改时间：" + new Date(settings.next_change_at * 1000).toLocaleString()
        : "修改后全部设备需要重新登录，账号数据和已绑定验证器保留。";
      section.hidden = false;
    }
    form.addEventListener("submit", async event => {
      event.preventDefault();
      if (busy || !settings || !settings.can_change) return;
      const username = form.elements.username.value.normalize("NFKC").toLowerCase();
      if (!/^[a-z0-9][a-z0-9_.@+-]{1,63}$/.test(username)) {
        message("用户名需为 2–64 个英文字母、数字或 _.@+-，以字母或数字开头", true); return;
      }
      if (username === settings.username) { message("新用户名与当前用户名相同", true); return; }
      if (!global.confirm("确认将登录用户名从「" + settings.username + "」改为「" + username + "」？旧名停止登录且不能再注册，30 天内不能再次改名。")) return;
      busy = true; button.disabled = true;
      try {
        await api("/user/username", "POST", { username, password: form.elements.password.value, code: code.value.trim() });
        form.reset(); auth.lost("username_changed");
      } catch (error) {
        message(error.message, true);
        if (settings.mfa_required) { code.focus(); code.select(); }
      } finally { busy = false; button.disabled = !settings.can_change; }
    });
    global.addEventListener("pagehide", () => { form.elements.password.value = ""; code.value = ""; });
    return { load };
  }
  global.YtoolsUsernameManagement = { create };
})(window);
