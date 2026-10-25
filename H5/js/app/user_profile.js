/**
 * 侧边栏只显示当前账号名称；改名统一在“账号与安全”页面完成。
 * 依赖：app/core.js（DOM 引用）、API（js/api.js）
 */
(function (App) {
  "use strict";
  const { profileName } = App;

  // 服务端未返回时的兜底默认名（与后端 config.DEFAULT_USER_NAME 一致）
  const FALLBACK_NAME = "访客用户";

  let requestVersion = 0;

  function renderName(name) {
    const currentName = (name || "").trim() || FALLBACK_NAME;
    profileName.textContent = currentName;
    profileName.setAttribute("title", currentName);
  }

  /** 首屏拉取显示名（失败静默，保持 HTML 中的默认文案） */
  function loadUserProfile() {
    const version = ++requestVersion;
    return API.getUserProfile().then(function (data) {
      if (!data || version !== requestVersion) return;
      renderName(data.name || data.default_name);
      const auth = window.YtoolsAuth;
      if (auth) auth.update({ user: { ...auth.user, name: data.name || data.default_name } });
    }).catch(function () { /* 读取失败不打断初始化 */ });
  }

  const user = window.YtoolsAuth && window.YtoolsAuth.user;
  renderName(user ? user.name || user.username : profileName.textContent);
  // 浏览器后退可能直接恢复页面快照，需要重新读取账号页保存的显示名。
  window.addEventListener("pageshow", function (event) {
    if (event.persisted) loadUserProfile();
  });

  // ---------- 导出（供 app.js 初始化与其它模块经 App.* 调用） ----------
  App.loadUserProfile = loadUserProfile;
  App.renderUserName = renderName;
})(window.App);
