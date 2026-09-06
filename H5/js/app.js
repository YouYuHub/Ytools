/**
 * 应用入口：一次性迁移旧配置并初始化
 * 模块拆分见 js/app/ 目录；加载顺序见 index.html（core 最先、本文件最后）
 * 依赖：js/app/ 全部模块（经 window.App 共享）
 */
(function (App) {
  "use strict";
  const { refreshThemeUI, updateExportButton, autosize, isMobile,
    sidebar, sessionList, loadTools, loadWorkDir, loadSessions,
    openSession, startNewChat } = App;

  // 旧版内置工具开关（localStorage 全局生效）一次性迁移为全局默认工具选择：
  // 读取旧键后即清除并打标记，迁移结果通过 GET/POST tool_selection 落到 mcp_servers.json；
  // 此后内置工具的开关完全由“配置工具”模态框按会话/全局默认管理
  function migrateLegacyBuiltinFlags() {
    let legacyTodo = false;
    let legacyAsk = false;
    try {
      if (localStorage.getItem("ytools-builtin-migrated")) {
        localStorage.removeItem("ytools-todo-enabled");
        localStorage.removeItem("ytools-ask-enabled");
        return;
      }
      legacyTodo = localStorage.getItem("ytools-todo-enabled") === "1";
      legacyAsk = localStorage.getItem("ytools-ask-enabled") === "1";
      localStorage.setItem("ytools-builtin-migrated", "1");
      localStorage.removeItem("ytools-todo-enabled");
      localStorage.removeItem("ytools-ask-enabled");
    } catch (_) { return; }
    if (!legacyTodo && !legacyAsk) return;
    API.getToolSelection().then(function (data) {
      const inputs = (data && data.inputs) || {};
      // 全局默认已包含内置工具选择时不覆盖，避免吃掉其他端已保存的结果
      if (Array.isArray(inputs[App.BUILTIN_SERVER_KEY]) && inputs[App.BUILTIN_SERVER_KEY].length) return;
      const builtinNames = [];
      if (legacyTodo) builtinNames.push(App.TODO_TOOL_NAME);
      if (legacyAsk) builtinNames.push(App.ASK_USER_TOOL_NAME);
      inputs[App.BUILTIN_SERVER_KEY] = builtinNames;
      return API.updateToolSelection(inputs);
    }).then(function () {
      App.loadToolSelection(); // 让当前页面立即反映迁移后的全局默认
    }).catch(function () { /* 迁移失败不阻塞初始化，可随时在模态框手动勾选 */ });
  }

  function init() {
    refreshThemeUI();
    updateExportButton();
    autosize();
    if (isMobile()) sidebar.classList.add("collapsed");
    loadTools();
    loadWorkDir();
    migrateLegacyBuiltinFlags();
    // 支持 ?session=xxx 直达会话；目标会话不存在时（如默认 default 已删除）直接新建对话
    const target = new URLSearchParams(location.search).get("session") || "default";
    loadSessions().then(function () {
      if (sessionList.querySelector('[data-session="' + target + '"]')) openSession(target);
      else startNewChat();
    });
  }

  init();
})(window.App);
