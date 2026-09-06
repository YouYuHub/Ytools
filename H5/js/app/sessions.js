/**
 * 会话列表与会话切换
 * - 侧边栏会话列表：加载/排序/重命名/删除确认/置顶/本地 recency 兜底
 * - 会话切换：新对话惰性分配 ID、打开会话（拉历史/恢复流式/刷新统计）
 * 依赖：app/core.js、API、SessionUtils、SessionListUtils、HistoryParser
 */
(function (App) {
  "use strict";
  const {
    state, $, el, toast,
    isMobile, setEmpty, setSidebarCollapsed, scrollToBottom,
    readTitleOverrides, SESSION_TITLE_KEY, sessionList, deleteModal,
    deleteCancel, deleteConfirm, enhancePanel, chatInner,
    input
  } = App;

  // ---------- 会话列表 ----------
  // 会话标识规整请见 SessionUtils.sanitizeSessionId（js/session_utils.js）
  function clearSessionItems() {
    sessionList.querySelectorAll(".session-item, .empty-tip").forEach(function (n) { n.remove(); });
  }

  async function loadSessions() {
    clearSessionItems();
    let files = [];
    try {
      files = await API.listSessions();
    } catch (err) {
      sessionList.appendChild(el("div", "empty-tip", "无法连接后端服务"));
      return;
    }

    const items = [];
    const fetchedIds = new Set();
    if (Array.isArray(files) && files.length) {
      const rows = await Promise.all(files.map(async function (file) {
        const id = SessionUtils.fileNameToSessionId(file);
        let title = id;
        let updated = "";
        let created = "";
        try {
          const meta = await API.getSessionMeta(id);
          title = SessionUtils.getSessionTitle(id, meta.title || (meta.user_questions && meta.user_questions[0]) || id);
          updated = meta.updated_at || "";
          created = meta.created_at || "";
        } catch (_) { /* 元数据读取失败时用 id 兜底 */ }
        return { id: id, title: title, updated: updated, created: created };
      }));
      rows.forEach(function (row) { fetchedIds.add(row.id); items.push(row); });
    }

    // 本地刚发送/重命名但后端尚未落盘的会话也一并展示，避免列表重建后“消失”
    Object.keys(state.sessionRecency).forEach(function (id) {
      if (fetchedIds.has(id)) return;
      const rec = state.sessionRecency[id] || { title: id };
      items.push({ id: id, title: rec.title || id, updated: "", created: "" });
    });

    if (!items.length) {
      sessionList.appendChild(el("div", "empty-tip", "暂无历史会话"));
      return;
    }

    const recencyMap = {};
    Object.keys(state.sessionRecency).forEach(function (id) {
      recencyMap[id] = state.sessionRecency[id].ts;
    });
    // 按最近更新倒序（后更新的在前）：后端 updated_at 为主，本地 recency 兜底，created_at 再兜底
    SessionListUtils.sortRows(items, recencyMap).forEach(function (item) {
      sessionList.appendChild(buildSessionItem(item.id, item.title));
    });
    refreshSessionFades();
  }

  // 溢出淡出标记：给真正被截断的标题加 .truncated（右缘渐隐遮罩，替代「…」占位）。
  // 短标题不加遮罩，避免文字尾部被无谓淡出
  function refreshSessionFades() {
    sessionList.querySelectorAll(".session-name").forEach(function (node) {
      node.classList.toggle("truncated", node.scrollWidth > node.clientWidth + 1);
    });
  }

  function buildSessionItem(id, title) {
    const item = el("div", "session-item" + (id === state.sessionId ? " active" : ""));
    item.dataset.session = id;
    const select = el("button", "session-select");
    select.type = "button";
    select.appendChild(el("span", "session-name", title));
    select.addEventListener("click", function () {
      closeSessionMenus();
      openSession(id);
    });
    item.appendChild(select);

    const actions = el("button", "session-actions");
    actions.type = "button";
    actions.title = "会话操作";
    actions.innerHTML = '<svg class="icon" viewBox="0 0 24 24"><circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/></svg>';
    actions.addEventListener("click", function (event) {
      event.stopPropagation();
      toggleSessionMenu(id, item, actions);
    });
    item.appendChild(actions);
    return item;
  }

  function closeSessionMenus() {
    document.querySelectorAll(".session-menu").forEach(function (menu) { menu.remove(); });
    sessionList.querySelectorAll(".session-item.menu-open").forEach(function (item) {
      item.classList.remove("menu-open");
    });
  }

  function sessionMenuButton(label, icon, handler, className) {
    const button = el("button", "menu-item" + (className ? " " + className : ""));
    button.innerHTML = icon + '<span class="menu-item-label"></span>';
    button.querySelector(".menu-item-label").textContent = label;
    button.addEventListener("click", function (event) {
      event.stopPropagation();
      closeSessionMenus();
      handler();
    });
    return button;
  }

  function toggleSessionMenu(id, item, anchor) {
    const existing = document.querySelector(".session-menu");
    if (existing && item.classList.contains("menu-open")) {
      closeSessionMenus();
      return;
    }
    closeSessionMenus();
    item.classList.add("menu-open");

    const menu = el("div", "menu session-menu");
    const shareIcon = '<svg class="icon" viewBox="0 0 24 24"><path d="M12 15V3m0 0 4 4m-4-4L8 7"/><path d="M4 15v3a3 3 0 0 0 3 3h10a3 3 0 0 0 3-3v-3"/></svg>';
    const editIcon = '<svg class="icon" viewBox="0 0 24 24"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>';
    const archiveIcon = '<svg class="icon" viewBox="0 0 24 24"><path d="M3 6h18M5 6v14h14V6M8 10h8M9 3h6l1 3H8l1-3Z"/></svg>';
    const deleteIcon = '<svg class="icon" viewBox="0 0 24 24"><path d="M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2m2 0v14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2V6"/></svg>';
    menu.appendChild(sessionMenuButton("分享", shareIcon, function () { App.downloadSession(id); }));
    menu.appendChild(sessionMenuButton("重命名", editIcon, function () { renameSession(id, item); }));
    menu.appendChild(sessionMenuButton("归档", archiveIcon, function () { toast("归档功能暂未接入后端"); }, "disabled"));
    menu.appendChild(sessionMenuButton("删除", deleteIcon, function () { openDeleteModal(id, item); }, "danger"));
    document.body.appendChild(menu);

    const rect = anchor.getBoundingClientRect();
    menu.style.top = Math.min(rect.bottom + 4, window.innerHeight - menu.offsetHeight - 8) + "px";
    menu.style.left = Math.max(8, rect.right - menu.offsetWidth) + "px";
  }

  function renameSession(id, item) {
    const current = item.querySelector(".session-name").textContent;
    const select = item.querySelector(".session-select");
    const wrapper = el("div", "session-edit");
    const editor = document.createElement("input");
    editor.className = "session-name-editor";
    editor.type = "text";
    editor.value = current;
    editor.maxLength = 120;
    editor.setAttribute("aria-label", "编辑会话名称");
    wrapper.appendChild(editor);
    select.replaceWith(wrapper);

    let finished = false;
    async function finish(save) {
      if (finished) return;
      finished = true;
      const title = editor.value.trim();
      if (!save || !title || title === current) {
        wrapper.replaceWith(select);
        if (save && !title) toast("名称不能为空");
        return;
      }
      editor.disabled = true;
      try {
        await API.updateSessionTitle(id, title);
        const overrides = readTitleOverrides();
        delete overrides[id];
        localStorage.setItem(SESSION_TITLE_KEY, JSON.stringify(overrides));
        wrapper.replaceWith(select);
        // 重命名成功：更新标题并把该会话置顶（重命名视为一次“最近更新”）
        upsertLocalSession(id, { title: title });
        toast("会话已重命名");
      } catch (err) {
        wrapper.replaceWith(select);
        toast("重命名失败：" + err.message);
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
    editor.focus();
    editor.select();
  }

  function openDeleteModal(id, node) {
    if (state.activeStream && state.activeStream.sessionId === id) {
      toast("生成中的会话请先停止后再删除");
      return;
    }
    state.pendingDelete = { id: id, node: node };
    deleteModal.classList.remove("hidden");
    deleteModal.setAttribute("aria-hidden", "false");
  }

  function closeDeleteModal() {
    state.pendingDelete = null;
    deleteModal.classList.add("hidden");
    deleteModal.setAttribute("aria-hidden", "true");
  }

  async function confirmDeleteSession() {
    if (!state.pendingDelete) return;
    const pending = state.pendingDelete;
    deleteConfirm.disabled = true;
    try {
      await API.deleteSession(pending.id);
      pending.node.remove();
      delete state.sessionRecency[pending.id];
      const overrides = readTitleOverrides();
      delete overrides[pending.id];
      localStorage.setItem(SESSION_TITLE_KEY, JSON.stringify(overrides));
      toast("会话已删除");
      closeDeleteModal();
      if (pending.id === state.sessionId) startNewChat();
    } catch (err) {
      toast("删除失败：" + err.message);
    } finally {
      deleteConfirm.disabled = false;
    }
  }

  function markActiveSession() {
    sessionList.querySelectorAll(".session-item").forEach(function (node) {
      node.classList.toggle("active", node.dataset.session === state.sessionId);
    });
  }

  /**
   * 发送消息 / 重命名后立即把该会话插入侧边栏列表并置顶。
   * 纯本地操作：不请求后端 _meta，后端落盘慢也不影响立即显示。
   * - { userText }：新会话标题取首条提问前 40 个字符（与后端一致）
   * - { title }：直接使用指定标题（重命名场景，不截断）
   * 同时记录本地 recency，后续 loadSessions 重建列表时仍按此排在最前。
   */
  function upsertLocalSession(sessionId, opts) {
    opts = opts || {};
    const title = opts.userText != null
      ? (SessionListUtils.firstQuestionTitle(opts.userText) || sessionId)
      : (opts.title || sessionId);
    state.sessionRecency[sessionId] = { title: title, ts: Date.now() };

    const tip = sessionList.querySelector(".empty-tip");
    if (tip) tip.remove();
    let item = sessionList.querySelector('[data-session="' + sessionId + '"]');
    if (!item) {
      const label = sessionList.querySelector(".session-label");
      item = buildSessionItem(sessionId, title);
      sessionList.insertBefore(item, label ? label.nextSibling : sessionList.firstChild);
    } else if (opts.title) {
      const name = item.querySelector(".session-name");
      if (name) name.textContent = title;
    }
    // 置顶（本会话刚发生交互，应排在列表最前）
    const label = sessionList.querySelector(".session-label");
    const topRef = label ? label.nextSibling : sessionList.firstChild;
    if (item !== topRef && topRef) sessionList.insertBefore(item, topRef);
    markActiveSession();
    refreshSessionFades();
    return item;
  }

  // ---------- 会话切换 ----------
  // 会话打开请求序号：切会话瞬间旧请求仍可能在途，过期响应直接丢弃
  let sessionOpenSeq = 0;

  /**
   * 惰性生成会话 ID：仅在真正产生会话内容时分配（发送消息 / 上传文件）。
   * "新对话"只把 sessionId 置空，不预生成 ID——否则选工具、改参数等操作
   * 携带该 ID 请求后端（如 token_stats），会导致后端创建只有 _meta 的空会话文件。
   */
  function ensureSessionId() {
    if (!state.sessionId) {
      state.sessionId = SessionUtils.sanitizeSessionId(SessionUtils.generateSessionId());
    }
    return state.sessionId;
  }

  function startNewChat() {
    sessionOpenSeq++;
    // 只进入"待开始"状态，不生成本地 ID；首次发送消息时才由 ensureSessionId() 分配
    state.sessionId = null;
    App.loadWorkDir(); // 新对话未覆盖目录：显示全局默认
    App.loadToolSelection(); // 新对话未覆盖工具：应用全局默认选择
    // 模型面板打开中时按"新对话=全局默认"刷新（否则缓存仍是上一会话的生效选择）
    if (!enhancePanel.classList.contains("hidden")) App.openModelPanel();
    state.sessionDocs = [];
    state.todoTodos = [];
    state.todoPanelOpen = false;
    App.renderTodoWidget();
    App.clearPendingMedia();
    App.resetContextTokenStats();
    state.hasConversation = false;
    state.importedHistoryText = null;
    enhancePanel.classList.add("hidden");
    chatInner.innerHTML = "";
    setEmpty(true);
    App.setSessionTotalTokens(0);
    App.updateExportButton();
    markActiveSession();
    App.loadSessionFiles();
    if (isMobile()) setSidebarCollapsed(true);
    input.focus();
    App.refreshComposerButtons();
  }

  $("#newChatBtn").addEventListener("click", startNewChat);
  $("#railNewChat").addEventListener("click", startNewChat);

  async function openSession(id) {
    const seq = ++sessionOpenSeq;
    id = SessionUtils.sanitizeSessionId(id);
    state.sessionId = id;
    App.loadWorkDir();
    App.loadToolSelection(); // 按会话加载独立工具选择（未覆盖则应用全局默认）
    // 模型面板打开中时按新会话的生效选择刷新
    if (!enhancePanel.classList.contains("hidden")) App.openModelPanel();
    App.clearPendingMedia();
    App.resetContextTokenStats();
    state.hasConversation = false;
    state.importedHistoryText = null;
    App.updateExportButton();
    markActiveSession();
    if (isMobile()) setSidebarCollapsed(true);

    chatInner.innerHTML = "";
    App.setSessionTotalTokens(0);
    // 恢复任务计划（会话级，_meta.todo）
    state.todoTodos = [];
    state.todoPanelOpen = false;
    App.renderTodoWidget();
    API.getSessionMeta(id).then(function (meta) {
      if (state.sessionId !== id) return;
      App.applySessionTodo(meta && meta.todo);
    }).catch(function () { /* ignore */ });
    App.loadSessionFiles();
    // 切换会话后按“当前会话”重新计算发送/停止按钮（其他会话后台流式不影响本会话）
    App.refreshComposerButtons();
    try {
      const text = await API.fetchSessionFile(id);
      // 等待期间用户已切到其他会话，丢弃过期响应，避免误覆盖
      if (seq !== sessionOpenSeq) return;
      const parsed = HistoryParser.parseHistory(text);
      const active = state.activeStream && state.activeStream.sessionId === id ? state.activeStream : null;
      const visibleRecords = active ? HistoryParser.recordsBeforeActiveRound(parsed.records, active.userText) : parsed.records;
      App.setSessionUsage(parsed.metaUsage);
      App.refreshContextTokenStats(id);
      state.hasConversation = visibleRecords.length > 0 || Boolean(active);
      App.updateExportButton();
      if (visibleRecords.length === 0) {
        setEmpty(!restoreActiveStream(id));
      } else {
        setEmpty(false);
        chatInner.innerHTML = "";
        App.renderRecords(visibleRecords);
        restoreActiveStream(id);
        App.rebuildQnav();
        scrollToBottom(true);
      }
    } catch (err) {
      state.hasConversation = false;
      state.importedHistoryText = null;
      App.refreshContextTokenStats(id);
      App.updateExportButton();
      setEmpty(!restoreActiveStream(id));
      if (err.message !== "加载会话失败") toast("历史加载失败：" + err.message);
    }
    // 刷新后若同一会话仍有后台生成任务，自动附接续看
    if (seq === sessionOpenSeq) App.maybeAttachRunningStream(id);
  }

  function restoreActiveStream(sessionId) {
    const active = state.activeStream;
    if (!active || active.sessionId !== sessionId || active.completed) return false;

    const hasUserMessage = Array.from(chatInner.querySelectorAll(".msg-user .msg-bubble"))
      .some(function (node) { return node.textContent === active.userText; });
    if (!hasUserMessage && active.userNode && active.userNode.parentNode !== chatInner) {
      chatInner.appendChild(active.userNode);
    }
    if (active.messageNode && active.messageNode.parentNode !== chatInner) {
      chatInner.appendChild(active.messageNode);
    }
    setEmpty(false);
    App.rebuildQnav();
    return true;
  }


  // ---------- 导出（供其它模块经 App.* 调用） ----------
  App.clearSessionItems = clearSessionItems;
  App.loadSessions = loadSessions;
  App.buildSessionItem = buildSessionItem;
  App.closeSessionMenus = closeSessionMenus;
  App.renameSession = renameSession;
  App.openDeleteModal = openDeleteModal;
  App.closeDeleteModal = closeDeleteModal;
  App.confirmDeleteSession = confirmDeleteSession;
  App.markActiveSession = markActiveSession;
  App.refreshSessionFades = refreshSessionFades;
  App.upsertLocalSession = upsertLocalSession;
  App.ensureSessionId = ensureSessionId;
  App.startNewChat = startNewChat;
  App.openSession = openSession;
  App.restoreActiveStream = restoreActiveStream;
})(window.App);
