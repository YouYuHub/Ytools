/**
 * 访客用户（全局显示名）：侧边栏底部名字就地编辑
 * - 点击名字 → 内联输入框（Enter 保存 / Esc 取消 / 失焦保存）
 * - 保存经 API.updateUserProfile 写入项目 .env 的 USER_NAME 键（全局生效，非会话级）
 * - 账号/数据库体系落地前的过渡实现：数据源换成数据库时只需替换后端路由实现
 * 依赖：app/core.js（DOM 引用）、API（js/api.js）
 */
(function (App) {
  "use strict";
  const { toast, profileName } = App;

  // 服务端未返回时的兜底默认名（与后端 config.DEFAULT_USER_NAME 一致）
  const FALLBACK_NAME = "访客用户";

  let profileEditing = false;
  let profileBusy = false;
  let profileMaxLength = 32;
  let currentName = FALLBACK_NAME;

  function renderName(name) {
    currentName = (name || "").trim() || FALLBACK_NAME;
    profileName.textContent = currentName;
    profileName.setAttribute("title", "点击修改显示名");
  }

  /** 首屏拉取显示名（失败静默，保持 HTML 中的默认文案） */
  function loadUserProfile() {
    return API.getUserProfile().then(function (data) {
      if (!data) return;
      if (typeof data.max_length === "number" && data.max_length > 0) {
        profileMaxLength = data.max_length;
      }
      renderName(data.name || data.default_name);
    }).catch(function () { /* 读取失败不打断初始化 */ });
  }

  /** 进入编辑：把名字文本换成内联输入框 */
  function startNameEdit() {
    if (profileEditing || profileBusy) return;
    const original = currentName;
    const editor = document.createElement("input");
    editor.className = "profile-name-editor";
    editor.type = "text";
    editor.value = original;
    editor.maxLength = profileMaxLength;
    editor.setAttribute("aria-label", "修改显示名");
    editor.setAttribute("placeholder", "输入显示名；留空恢复默认");
    // 输入框自身点击不应冒泡到 profileCard（否则会顺带打开主题菜单）
    editor.addEventListener("click", function (event) { event.stopPropagation(); });

    profileName.textContent = "";
    profileName.appendChild(editor);
    profileEditing = true;
    editor.focus();
    editor.select();

    let finished = false;
    async function finish(save) {
      if (finished) return;
      finished = true;
      profileEditing = false;
      const next = editor.value.trim();
      // 取消、或值未变化：仅还原显示，不发请求
      if (!save || next === original) {
        renderName(original);
        return;
      }
      profileBusy = true;
      editor.disabled = true;
      try {
        const data = await API.updateUserProfile(next);
        if (data && data.state === "succeed") {
          renderName(data.name || data.default_name);
          toast(data.message || "显示名已更新");
        } else {
          renderName(original);
          toast("显示名更新失败：" + ((data && data.message) || "未知错误"));
        }
      } catch (err) {
        renderName(original);
        toast("显示名更新失败：" + err.message);
      } finally {
        profileBusy = false;
      }
    }

    editor.addEventListener("keydown", function (event) {
      if (event.key === "Enter") {
        event.preventDefault();
        finish(true);
      }
      if (event.key === "Escape") {
        event.preventDefault();
        finish(false);
      }
    });
    editor.addEventListener("blur", function () { finish(true); });
  }

  profileName.addEventListener("click", function (event) {
    // 阻止冒泡：profileCard 的点击负责弹出主题菜单，本入口只做改名
    event.stopPropagation();
    // 已展开的主题菜单顺手收起（与点击其它浮层外区域的行为一致）
    if (App.closeMenus) App.closeMenus();
    startNameEdit();
  });

  // ---------- 导出（供 app.js 初始化与其它模块经 App.* 调用） ----------
  App.loadUserProfile = loadUserProfile;
  App.renderUserName = renderName;
  App.startUserNameEdit = startNameEdit;
})(window.App);
