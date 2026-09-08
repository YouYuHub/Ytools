/**
 * 应用共享核心：全局状态、常量、DOM 引用与通用工具
 * - 所有 app/ 模块共享的 state（会话/流式/面板/附件等运行时状态）
 * - 页面 DOM 引用一次性收集，挂到 window.App 供各模块解构
 * - 通用工具：el/toast/滚动定位/空态切换/菜单开关/侧边栏/会话搜索/悬浮提示
 * 依赖：theme.js（ThemeManager）；被 app/ 其余模块与 app.js 入口依赖，须最先加载
 */
window.App = window.App || {};
(function (App) {
  "use strict";
  const state = {
    // 当前会话 id；null 表示"尚未开始的新对话"（未产生任何内容）。
    // 会话 ID 只在真正产生内容的动作（发送消息 / 上传文件）时才惰性生成，
    // 选工具、调参数等操作不触发 ID 分配，避免后端凭空创建空会话文件。
    sessionId: null,
    streaming: false,
    // 当前正在监听/流式的会话 id（同一时刻前端只监听一个会话的流，后端任务可多会话并行）
    streamingSession: null,
    abort: null,
    tools: [],
    servers: [],
    failedServers: [],
    selectedTools: new Set(),
    draftTools: new Set(),
    showToolChips: false,
    boost: false,
    sessionTotalTokens: 0,
    sessionUsage: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 },
    contextTokenStats: null,
    workDir: "",
    workDirOverridden: false,
    // 会话级工具选择：当前会话是否设置了独立选择（false 表示跟随全局默认）
    toolSelectionOverridden: false,
    chatSettingsDefaults: null,
    activeStream: null,
    hasConversation: false,
    importedHistoryText: null,
    pendingDelete: null,
    // 本地“最近触碰”记录：{ sessionId: { title, ts } }
    // 发送消息 / 重命名时写入，用于后端落盘/元数据尚未生效时保持正确的置顶顺序
    sessionRecency: {},
    // 模型配置：role -> GET /chat_config/models 响应（chat_model / compaction_model / title_model）
    modelConfigs: {},
    // 模型配置：role -> 会话是否设置了独立模型选择（false 表示跟随全局默认）
    modelOverridden: {},
    // 模型配置面板最近一次加载是否失败（决定面板提示文案）
    modelLoadFailed: false,
    activeModelRole: "chat_model",
    // 面板中当前选中的模型：provider::model（仅面板内草稿，未确定前不生效）
    selectedModelKey: "",
    // 参数面板中生成参数是否被手动修改过（决定提交时是否带 parameter）
    modelParamDirty: false,
    // 待发送的多媒体附件（粘贴/选择的图片音频视频）：[{id,file,name,kind,dataUrl}]
    pendingMedia: [],
    // 会话内已上传解析的文档（file_memory）：解析文本由后端注入系统提示词，跨消息生效
    sessionDocs: [],
    // 任务计划（模型自我规划）：SSE todo 事件驱动更新
    todoTodos: [],
    todoPanelOpen: false,
    // 当前待回答的提问（ask_user 事件携带的问题列表；null=无待回答提问）
    pendingAskQuestions: null,
    // 待回答提问对应的聊天流内卡片（覆盖式重答时用于清除其后的旧轮次）
    pendingAskBlock: null,
    // 手动压缩进行中标记：compaction 模块置位，发送守卫与发送按钮据此拦截
    manualCompactRunning: false,
    // 手动压缩的中止控制器：终止按钮点击时 abort 断开压缩 SSE 连接
    manualCompactAbort: null,
    // 流式期间的消息暂存：
    // steerMessage = 消息引导（当前 SSE 流结束后立即作为新一轮发送，单条后设覆盖前设）
    // pendingQueue = 队列消息（当前任务完成后 FIFO 逐条自动发送，可累积多条）
    // 均为 { text, media } 结构；media 为待上传附件快照，空数组表示纯文本
    steerMessage: null,
    pendingQueue: [],
    // 运行中已注入的引导消息提示（在消息框上方展示，下一轮模型输出开始后清除）：
    // { sessionId, text }；消息本身由后端落盘，历史回放仍显示为用户消息
    injectedNotice: null,
    // 引导/队列按钮组当前展开的菜单归属："steer" | "queue" | ""（未展开）
    composerSendMode: "",
    // 单次抑制自动派发：用户主动停止/切换会话放弃监听时置位，
    // 该轮 send 结束后不触发引导/队列 flush（消费后自动复位）
    suppressFlushOnce: false,
    // 每会话独立的输入草稿：{ text, media } —— 切换会话时保存/恢复，
    // 文本与待发附件都不丢失；key 为会话 ID，"" 对应"新对话"未建号状态
    sessionDrafts: {},
  };

  // 待发送附件上限：数量与单文件大小按类别区分（与后端 MEDIA_SIZE_LIMITS 一致：视频 500MB）
  const MAX_PENDING_MEDIA = 6;
  const MEDIA_SIZE_LIMITS = { image: 20 * 1024 * 1024, audio: 20 * 1024 * 1024, video: 500 * 1024 * 1024 };
  // 与后端 memory.file_memory 的媒体扩展名白名单保持一致
  // （ico/tif/tiff 上传后由后端自动转为 png，模型始终收到原生支持的格式）
  const MEDIA_EXTENSIONS = {
    image: ["png", "jpg", "jpeg", "gif", "webp", "bmp", "ico", "tif", "tiff"],
    audio: ["wav", "mp3", "m4a", "ogg", "flac"],
    video: ["mp4", "webm", "mov", "mkv"],
  };
  // 与后端 file_factory.PARSER_BY_EXT 一致的可解析文档类型
  // （解析文本写入 file_memory，由后端注入系统提示词，与二进制媒体走不同通道）
  const DOC_EXTENSIONS = ["txt", "md", "pdf", "docx", "doc", "csv", "xls", "xlsx"];
  const MAX_DOC_FILE_SIZE = 10 * 1024 * 1024;

  function docKindOf(filename) {
    const ext = (filename.split(".").pop() || "").toLowerCase();
    return DOC_EXTENSIONS.indexOf(ext) >= 0;
  }

  const QNAV_MAX_DASHES = 40;

  // ---------- DOM ----------
  const $ = function (sel) { return document.querySelector(sel); };
  const app = $("#app");
  const sidebar = $("#sidebar");
  const scrim = $("#scrim");
  const sessionList = $("#sessionList");
  const searchBox = $("#searchBox");
  const searchInput = $("#searchInput");
  const tokenTotal = $("#tokenTotal");
  const sessionActionsBtn = $("#sessionActionsBtn");
  const sessionActionsMenu = $("#sessionActionsMenu");
  const shareSessionItem = $("#shareSessionItem");
  const compactSessionItem = $("#compactSessionItem");
  const main = $(".main");
  const chatScroll = $("#chatScroll");
  const chatInner = $("#chatInner");
  const input = $("#input");
  const composer = $(".composer");
  const composerWrap = $(".composer-wrap");
  const contextTokenStatus = $("#contextTokenStatus");
  const contextTokenSummary = $("#contextTokenSummary");
  const contextTokenWorkdir = $("#contextTokenWorkdir");
  const sendBtn = $("#sendBtn");
  const stopBtn = $("#stopBtn");
  const voiceBtn = $("#voiceBtn");
  const stopGroup = $("#stopGroup");
  const stopMenuBtn = $("#stopMenuBtn");
  const queueMenu = $("#queueMenu");
  const composerAttachments = $("#composerAttachments");
  const pendingOutbox = $("#pendingOutbox");
  const contextTokenTodoSlot = $("#contextTokenTodoSlot");
  const todoPanelHost = $("#todoPanel");
  const askModal = $("#askModal");
  const askQuestions = $("#askQuestions");
  const askSubmit = $("#askSubmit");
  const mediaPreviewModal = $("#mediaPreviewModal");
  const mediaPreviewBody = $("#mediaPreviewBody");
  const mediaPreviewTitle = $("#mediaPreviewTitle");
  const boostBtn = $("#boostBtn");
  const enhancePanel = $("#enhancePanel");
  const enhanceClose = $("#enhanceClose");
  const modelTabs = $("#modelTabs");
  const modelPicker = $("#modelPicker");
  const modelPickerLabel = $("#modelPickerLabel");
  const modelList = $("#modelList");
  const reasoningEffort = $("#reasoningEffort");
  const enableThinking = $("#enableThinking");
  const temperature = $("#temperature");
  const temperatureValue = $("#temperatureValue");
  const maxTokens = $("#maxTokens");
  const maxTokensValue = $("#maxTokensValue");
  const topP = $("#topP");
  const topPValue = $("#topPValue");
  const presencePenalty = $("#presencePenalty");
  const presencePenaltyValue = $("#presencePenaltyValue");
  const enhanceCancel = $("#enhanceCancel");
  const enhanceConfirm = $("#enhanceConfirm");
  const enhanceReset = $("#enhanceReset");
  const enhanceClearOverride = $("#enhanceClearOverride");
  const enhanceHint = $("#enhanceHint");
  const plusBtn = $("#plusBtn");
  const plusMenu = $("#plusMenu");
  const toolModal = $("#toolModal");
  const toolGroups = $("#toolGroups");
  const toolSearchInput = $("#toolSearchInput");
  const toolSelected = $("#toolSelected");
  const toolCollapseAll = $("#toolCollapseAll");
  const toolRefresh = $("#toolRefresh");
  const fileInput = $("#fileInput");
  const historyFileInput = $("#historyFileInput");
  const fileChips = $("#fileChips");
  const toolChips = $("#toolChips");
  const themeMenu = $("#themeMenu");
  const profileSub = $("#profileSub");
  const scrollBottomBtn = $("#scrollBottomBtn");
  const qnav = $("#qnav");
  const qnavRail = $("#qnavRail");
  const qnavPanel = $("#qnavPanel");
  const toastWrap = $("#toastWrap");
  const deleteModal = $("#deleteModal");
  const deleteModalBackdrop = $("#deleteModalBackdrop");
  const deleteCancel = $("#deleteCancel");
  const deleteConfirm = $("#deleteConfirm");
  const chatSettingsModal = $("#chatSettingsModal");
  const chatSettingsBackdrop = $("#chatSettingsBackdrop");
  const chatSettingsClose = $("#chatSettingsClose");
  const chatSettingsCancel = $("#chatSettingsCancel");
  const chatSettingsConfirm = $("#chatSettingsConfirm");
  const chatSettingsReset = $("#chatSettingsReset");
  const reasoningMaxLength = $("#reasoningMaxLength");
  const toolResultMaxLength = $("#toolResultMaxLength");
  const toolCallTimeoutSeconds = $("#toolCallTimeoutSeconds");
  const networkRetryMaxAttempts = $("#networkRetryMaxAttempts");
  const keepRounds = $("#keepRounds");
  const triggerRatio = $("#triggerRatio");
  const summaryBudgetRatio = $("#summaryBudgetRatio");
  const oversizedRejectFactor = $("#oversizedRejectFactor");
  const maxOversizedRejections = $("#maxOversizedRejections");
  const effectiveThresholdHint = $("#effectiveThresholdHint");

  const THEME_LABEL = { system: "跟随系统", light: "浅色", dark: "深色" };
  const SESSION_TITLE_KEY = "ytools-session-title-overrides";

  // 三个模型角色的配置选项卡
  const MODEL_ROLES = [
    { role: "chat_model", label: "聊天模型" },
    { role: "compaction_model", label: "压缩模型" },
    { role: "title_model", label: "标题模型" },
  ];

  // 各角色生成参数的内置默认值（服务端未配置/解析不到时使用）
  const ROLE_DEFAULT_PARAMS = {
    chat_model: { temperature: 0.7, top_p: 1, presence_penalty: 2, reasoning_effort: "medium", enable_thinking: true },
    compaction_model: { temperature: 0.2, top_p: 1, presence_penalty: 0, reasoning_effort: "low", enable_thinking: false },
    title_model: { temperature: 0.7, top_p: 1, presence_penalty: 2, reasoning_effort: "medium", enable_thinking: true },
  };

  // 各 api_type 的“恢复默认”参数预设。后端暂未按协议细分适配，先按常见默认占位，
  // 后续协议设计变化时只需调整对应协议桶（角色默认会覆盖协议预设）
  const API_TYPE_DEFAULT_PARAMS = {
    chat_completions: { temperature: 0.7, top_p: 1.0, presence_penalty: 2.0, reasoning_effort: "medium", enable_thinking: true },
    responses: { temperature: 1.0, top_p: 1.0, presence_penalty: 0.0, reasoning_effort: "medium", enable_thinking: true },
    messages: { temperature: 1.0, top_p: 1.0, presence_penalty: 0.0, reasoning_effort: "medium", enable_thinking: false },
  };

  // 后端接口会返回同一份 defaults；这里仅作为后端暂不可用时的离线回退。
  const CHAT_SETTINGS_DEFAULTS = {
    reasoning_max_length: -1,
    tool_result_max_length: -1,
    call_timeout_seconds: 300,
    network_retry_max_attempts: 3,
    keep_rounds: 20,
    trigger_ratio: 0.8,
    summary_budget_ratio: 0.2,
    oversized_reject_factor: 1.5,
    max_oversized_rejections: 3,
  };

  function readTitleOverrides() {
    try {
      const saved = JSON.parse(localStorage.getItem(SESSION_TITLE_KEY) || "{}");
      return saved && typeof saved === "object" ? saved : {};
    } catch (_) {
      return {};
    }
  }

  // ---------- 工具函数 ----------
  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function toast(message) {
    const node = el("div", "toast", message);
    toastWrap.appendChild(node);
    setTimeout(function () {
      node.classList.add("fade");
      setTimeout(function () { node.remove(); }, 350);
    }, 2600);
  }

  function isMobile() {
    return window.matchMedia("(max-width: 768px)").matches;
  }

  function scrollToBottom(instant) {
    // 瞬跳：.chat-scroll 的 CSS 是 scroll-behavior: smooth，scrollTop 赋值会继承该属性触发动画，
    // 临时覆盖为 auto 再恢复，保证切换历史会话时瞬间跳到底部
    const prev = instant ? chatScroll.style.scrollBehavior : null;
    if (instant) chatScroll.style.scrollBehavior = "auto";
    updateScrollBottomOffset();
    chatScroll.scrollTop = chatScroll.scrollHeight;
    if (instant) chatScroll.style.scrollBehavior = prev;
  }

  function nearBottom() {
    return chatScroll.scrollHeight - chatScroll.scrollTop - chatScroll.clientHeight < 120;
  }

  function setEmpty(empty) {
    app.classList.toggle("empty", Boolean(empty));
    sessionActionsBtn.title = empty ? "加载" : "分享 / 加载 / 压缩对话";
    App.refreshComposerButtons();
    qnav.classList.add("hidden");
    requestAnimationFrame(updateScrollBottomOffset);
    if (!enhancePanel.classList.contains("hidden")) App.fitEnhancePanel();
  }

  function updateScrollBottomOffset() {
    if (!main || !composerWrap) return;
    const keepBottom = !app.classList.contains("empty") &&
      chatScroll.scrollHeight - chatScroll.scrollTop - chatScroll.clientHeight < 24;
    const mainRect = main.getBoundingClientRect();
    const composerRect = composerWrap.getBoundingClientRect();
    const offset = Math.max(16, mainRect.bottom - composerRect.top + 12);
    main.style.setProperty("--scroll-bottom-offset", Math.ceil(offset) + "px");
    const chatBottomPadding = Math.max(56, mainRect.bottom - composerRect.top + 16);
    chatInner.style.setProperty("--chat-bottom-padding", Math.ceil(chatBottomPadding) + "px");
    if (keepBottom) chatScroll.scrollTop = chatScroll.scrollHeight;
  }

  // ---------- 主题 ----------
  function refreshThemeUI() {
    const pref = ThemeManager.getPreference();
    profileSub.textContent = THEME_LABEL[pref];
    themeMenu.querySelectorAll(".menu-item").forEach(function (item) {
      item.classList.toggle("selected", item.dataset.value === pref);
    });
  }

  themeMenu.querySelectorAll(".menu-item").forEach(function (item) {
    item.addEventListener("click", function () {
      ThemeManager.setPreference(item.dataset.value);
      closeMenus();
    });
  });
  document.addEventListener("themechange", refreshThemeUI);

  // ---------- 菜单开关 ----------
  function closeMenus() {
    themeMenu.classList.add("hidden");
    plusMenu.classList.add("hidden");
    enhancePanel.classList.add("hidden");
    sessionActionsMenu.classList.add("hidden");
    queueMenu.classList.add("hidden");
    state.composerSendMode = "";
  }

  function closeToolModal() {
    toolModal.classList.add("hidden");
    toolModal.setAttribute("aria-hidden", "true");
    hideToolTip();
  }

  $("#profileCard").addEventListener("click", function (e) {
    e.stopPropagation();
    plusMenu.classList.add("hidden");
    themeMenu.classList.toggle("hidden");
  });
  document.addEventListener("click", function (e) {
    if (!themeMenu.contains(e.target) && !plusMenu.contains(e.target) && !enhancePanel.contains(e.target) &&
      !sessionActionsMenu.contains(e.target) && !queueMenu.contains(e.target) &&
      e.target !== plusBtn && e.target !== boostBtn &&
      !sessionActionsBtn.contains(e.target)) {
      closeMenus();
    }
    if (!e.target.closest(".session-menu") && !e.target.closest(".session-actions")) {
      App.closeSessionMenus();
    }
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") {
      closeMenus();
      if (!toolModal.classList.contains("hidden")) closeToolModal();
      if (!deleteModal.classList.contains("hidden")) App.closeDeleteModal();
      if (!chatSettingsModal.classList.contains("hidden")) App.closeChatSettings();
      if (!askModal.classList.contains("hidden")) App.closeAskModal();
    }
  });

  $("#toolModalClose").addEventListener("click", closeToolModal);
  $("#toolModalBackdrop").addEventListener("click", closeToolModal);
  enhanceClose.addEventListener("click", function () { enhancePanel.classList.add("hidden"); });
  // 跨模块的 App.* 方法必须经包装函数在点击时再解引用：core.js 先于行为
  // 模块（sessions.js 等）加载，此刻 App.closeDeleteModal 还是 undefined，
  // 直接注册 addEventListener(type, undefined) 会被静默忽略（拆分单文件
  // app.js 之前不存在此问题），表现为弹窗按钮点击无响应。
  deleteModalBackdrop.addEventListener("click", function () { App.closeDeleteModal(); });
  deleteCancel.addEventListener("click", function () { App.closeDeleteModal(); });
  deleteConfirm.addEventListener("click", function () { App.confirmDeleteSession(); });

  // ---------- 侧边栏 ----------
  function setSidebarCollapsed(collapsed) {
    sidebar.classList.toggle("collapsed", collapsed);
    scrim.classList.toggle("show", isMobile() && !collapsed);
  }

  $("#sidebarToggle").addEventListener("click", function () { setSidebarCollapsed(true); });
  $("#railExpand").addEventListener("click", function () { setSidebarCollapsed(false); });
  $("#railChats").addEventListener("click", function () { setSidebarCollapsed(false); });
  $("#menuToggle").addEventListener("click", function () { setSidebarCollapsed(false); });
  scrim.addEventListener("click", function () { setSidebarCollapsed(true); });

  $("#railAvatar").addEventListener("click", function () {
    setSidebarCollapsed(false);
    themeMenu.classList.remove("hidden");
  });

  // ---------- 会话搜索 ----------
  $("#searchBtn").addEventListener("click", function () {
    searchBox.classList.toggle("show");
    if (searchBox.classList.contains("show")) {
      searchInput.value = "";
      filterSessions("");
      searchInput.focus();
    }
  });

  searchInput.addEventListener("input", function () {
    filterSessions(searchInput.value.trim().toLowerCase());
  });

  function filterSessions(keyword) {
    sessionList.querySelectorAll(".session-item").forEach(function (node) {
      const name = node.querySelector(".session-name").textContent.toLowerCase();
      node.style.display = !keyword || name.includes(keyword) ? "" : "none";
    });
  }

  // ---------- 工具行描述悬浮提示 ----------
  const toolTip = el("div", "tool-tip");
  toolTip.style.display = "none";
  document.body.appendChild(toolTip);

  function hideToolTip() {
    toolTip.style.display = "none";
  }


  // ---------- 导出到共享命名空间（各模块按需解构） ----------
  Object.assign(App, {
  MAX_PENDING_MEDIA, MEDIA_SIZE_LIMITS, MEDIA_EXTENSIONS,
  DOC_EXTENSIONS, MAX_DOC_FILE_SIZE, QNAV_MAX_DASHES,
  SESSION_TITLE_KEY, MODEL_ROLES, ROLE_DEFAULT_PARAMS,
  API_TYPE_DEFAULT_PARAMS, CHAT_SETTINGS_DEFAULTS, app,
  sidebar, scrim, sessionList,
  searchBox, searchInput, tokenTotal,
  sessionActionsBtn, sessionActionsMenu, shareSessionItem,
  compactSessionItem, main, chatScroll,
  chatInner, input, composer,
  composerWrap, contextTokenStatus, contextTokenSummary,
  contextTokenWorkdir, sendBtn, stopBtn,
  voiceBtn, stopGroup, stopMenuBtn, queueMenu,
  composerAttachments, pendingOutbox, contextTokenTodoSlot,
  todoPanelHost, askModal, askQuestions,
  askSubmit, mediaPreviewModal, mediaPreviewBody,
  mediaPreviewTitle, boostBtn, enhancePanel,
  enhanceClose, modelTabs, modelPicker,
  modelPickerLabel, modelList, reasoningEffort,
  enableThinking, temperature, temperatureValue,
  maxTokens, maxTokensValue, topP,
  topPValue, presencePenalty, presencePenaltyValue,
  enhanceCancel, enhanceConfirm, enhanceReset,
  enhanceClearOverride, enhanceHint, plusBtn,
  plusMenu, toolModal, toolGroups,
  toolSearchInput, toolSelected, toolCollapseAll,
  toolRefresh, fileInput, historyFileInput,
  fileChips, toolChips, themeMenu,
  profileSub, scrollBottomBtn, qnav,
  qnavRail, qnavPanel, toastWrap,
  deleteModal, deleteModalBackdrop, deleteCancel,
  deleteConfirm, chatSettingsModal, chatSettingsBackdrop,
  chatSettingsClose, chatSettingsCancel, chatSettingsConfirm,
  chatSettingsReset, reasoningMaxLength, toolResultMaxLength,
  toolCallTimeoutSeconds, networkRetryMaxAttempts, keepRounds,
  triggerRatio, summaryBudgetRatio, oversizedRejectFactor,
  maxOversizedRejections, effectiveThresholdHint, state, $,
  el, toast, isMobile,
  scrollToBottom, nearBottom, setEmpty,
  updateScrollBottomOffset, refreshThemeUI, closeMenus,
  closeToolModal, setSidebarCollapsed, filterSessions,
  readTitleOverrides, docKindOf, hideToolTip,
  toolTip
  });
})(window.App);
