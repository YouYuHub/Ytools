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

  // ---------- 溢出标题 hover 跑马灯 ----------
  // 溢出标题 hover 250ms 后开始滚动：在裁剪窗口内让内层文本整体左移（尾部
  // 文字从右缘滚入），终点为"末尾文字 + 右边距"对齐，留出边距避免尾字贴边
  // 被右缘渐隐/操作按钮压住。
  // 动画载体为 transform: translateX（合成层属性，每帧只走 GPU 合成、不触
  // 发整行 reflow），配合 ease-out 缓动（先快后慢）——旧实现逐帧写
  // text-indent（布局属性）每帧 reflow，掉帧/重渲染时表现为跳变不平滑。
  // 移出后 80ms 才复位（防抖），期间回到同一标题则继续滚动（保留已滚动位
  // 移），吸收真实鼠标在行内掠动产生的边界事件抖动；复位也走 rAF 动画回滑，
  // 不再瞬间归零。
  // .is-marquee 类负责移除右缘渐隐遮罩 + will-change 提升合成层。
  let marqueeTimer = null;       // 启动延迟 timer
  let marqueeResetTimer = null;  // 移出复位防抖 timer
  let marqueeName = null;        // 当前滚动的标题元素
  let marqueeSession = null;     // 当前滚动的会话 id（行被重渲染替换后凭此接管新元素）
  let marqueeRaf = 0;            // rAF 句柄（滚动/复位共用）
  let marqueeShift = 0;          // 本次滚动的总位移（溢出 + 右边距）
  let marqueeMoved = 0;          // 已滚动位移（px）
  let marqueeLast = 0;           // 上一帧时间戳
  let marqueeAnimatingReset = false; // 复位动画进行中标记

  const MARQUEE_DELAY = 250;         // hover 触发延迟（ms），快速划过不触发
  const MARQUEE_SPEED_MAX = 160;     // 起步最高速度（px/s）
  const MARQUEE_SPEED_MIN = 36;      // 收尾最低速度（px/s），避免无限逼近终点
  const MARQUEE_DECEL = 1.6;         // 减速系数：speed = MIN + 剩余距离×该值
  const MARQUEE_RIGHT_INSET = 10;    // 终点时末尾文字与右缘的间距（px）
  const MARQUEE_RESET_DEBOUNCE = 80; // 移出复位防抖（ms）
  const MARQUEE_MAX_DT = 64;         // 单帧最大时间步长（ms），防切页回来跳变
  const MARQUEE_RESET_SPEED = 320;   // 复位回滚速度（px/s），快速但可见

  function clearMarqueeRaf() {
    if (marqueeRaf) {
      cancelAnimationFrame(marqueeRaf);
      marqueeRaf = 0;
    }
  }

  function marqueeSessionId(name) {
    const item = name.closest(".session-item");
    return item ? item.dataset.session || "" : "";
  }

  // 统一的标题文本写入点：兼容双层结构（内层 .session-name-text 承载文本，
  // 跑马灯 transform 作用在内层；外层 .session-name 保持裁剪窗口），旧
  // node.textContent = title 会把内层结构连根替换导致 transform 失效——
  // 所有运行期改标题的路径都必须走本函数。返回是否发生了变更（false=同值）
  function setSessionNameText(nameNode, title) {
    if (!nameNode) return false;
    const inner = nameNode.querySelector(".session-name-text");
    const target = inner || nameNode; // 兜底：旧结构（无双层）直接写
    if (target.textContent === title) return false;
    target.textContent = title;
    return true;
  }

  // 行被列表重渲染替换后，凭会话 id 找到接任的同名标题元素
  function marqueeSuccessor() {
    if (!marqueeSession) return null;
    const item = sessionList.querySelector(
      '.session-item[data-session="' + (window.CSS && CSS.escape ? CSS.escape(marqueeSession) : marqueeSession) + '"]');
    return item ? item.querySelector(".session-name") : null;
  }

  // 把元素摆到"已滚动 moved px"的位置（transform 载体：合成层属性，
  // 每帧只走 GPU 合成，不再触发整行 reflow——跳变感的旧根因）
  function applyMarqueeTransform(node, moved) {
    node.style.transform = "translateX(" + (-moved) + "px)";
  }

  // transform 动画载体解析：双层结构时取内层 .session-name-text（外层
  // .session-name 是 overflow:hidden 的裁剪窗口，整体平移会把窗口移出可视
  // 区；平移内层文本 = 旧 text-indent 语义的合成层等价实现），旧单层结构
  // 直接用外层自身兜底
  function marqueeAnimTarget(nameNode) {
    return nameNode.querySelector(".session-name-text") || nameNode;
  }

  function resetMarqueeDom() {
    if (marqueeName) {
      marqueeName.classList.remove("is-marquee");
      applyMarqueeTransform(marqueeAnimTarget(marqueeName), 0);
      marqueeAnimTarget(marqueeName).style.removeProperty("transform");
    }
    marqueeName = null;
    marqueeSession = null;
    marqueeMoved = 0;
    marqueeAnimatingReset = false;
  }

  function stopTitleMarquee() {
    if (marqueeTimer) {
      clearTimeout(marqueeTimer);
      marqueeTimer = null;
    }
    if (marqueeResetTimer) {
      clearTimeout(marqueeResetTimer);
      marqueeResetTimer = null;
    }
    clearMarqueeRaf();
    resetMarqueeDom();
  }

  // 复位回滚动画：从当前位移平滑归零（移出 hover 时不再瞬间跳回句首）
  function runMarqueeReset() {
    marqueeAnimatingReset = true;
    clearMarqueeRaf();
    marqueeLast = 0;
    const step = function (now) {
      if (!marqueeName || !marqueeName.isConnected || !marqueeAnimatingReset) {
        clearMarqueeRaf();
        return;
      }
      const dt = Math.min(now - (marqueeLast || now), MARQUEE_MAX_DT);
      marqueeLast = now;
      const next = marqueeMoved - (dt / 1000) * MARQUEE_RESET_SPEED;
      if (next <= 0) {
        resetMarqueeDom();
        marqueeRaf = 0;
        return;
      }
      marqueeMoved = next;
      applyMarqueeTransform(marqueeAnimTarget(marqueeName), marqueeMoved);
      marqueeRaf = requestAnimationFrame(step);
    };
    marqueeRaf = requestAnimationFrame(step);
  }

  function runMarqueeFrame() {
    clearMarqueeRaf();
    const step = function (now) {
      // 行被列表重渲染替换（元素脱离文档）：接管同会话的新元素，滚动无缝继续
      if (!marqueeName || !marqueeName.isConnected) {
        const next = marqueeSuccessor();
        if (!next) {
          clearMarqueeRaf();
          return;
        }
        if (next !== marqueeName) {
          next.classList.add("is-marquee");
          applyMarqueeTransform(marqueeAnimTarget(next), marqueeMoved);
          marqueeName = next;
        }
      }
      const name = marqueeName;
      const dt = Math.min(now - (marqueeLast || now), MARQUEE_MAX_DT);
      marqueeLast = now;
      const remaining = marqueeShift - marqueeMoved;
      // 按剩余距离减速：起步顶速快速滚入，接近终点线性减速到收尾速度
      // （速度连续，无突兀停止；中断续滚天然兼容——每帧按当前状态重算）
      const speed = Math.min(
        MARQUEE_SPEED_MAX,
        MARQUEE_SPEED_MIN + remaining * MARQUEE_DECEL
      );
      marqueeMoved = Math.min(marqueeShift, marqueeMoved + (dt / 1000) * speed);
      applyMarqueeTransform(marqueeAnimTarget(name), marqueeMoved);
      if (marqueeMoved >= marqueeShift) {
        marqueeRaf = 0;   // 到头：hover 期间保持终点（类在，遮罩仍移除）
        return;
      }
      marqueeRaf = requestAnimationFrame(step);
    };
    marqueeRaf = requestAnimationFrame(step);
  }

  sessionList.addEventListener("mouseover", function (e) {
    const name = e.target.closest(".session-name");
    if (!name) return;
    // 防抖窗口内回到同一标题，或复位回滚动画进行中重新进入：
    // 停止回滚、继续正向滚动（保留已滚动位移，不从头重放）
    if ((marqueeResetTimer || marqueeAnimatingReset) && name === marqueeName) {
      clearTimeout(marqueeResetTimer);
      marqueeResetTimer = null;
      marqueeAnimatingReset = false;
      marqueeLast = 0;
      runMarqueeFrame();
      return;
    }
    if (name === marqueeName) return;
    stopTitleMarquee();
    marqueeTimer = setTimeout(function () {
      marqueeTimer = null;
      if (!name.isConnected || marqueeName) return;
      const overflow = name.scrollWidth - name.clientWidth;
      if (overflow <= 2) return;
      marqueeName = name;
      marqueeSession = marqueeSessionId(name);
      marqueeShift = overflow + MARQUEE_RIGHT_INSET;
      marqueeMoved = 0;
      marqueeLast = 0;
      name.classList.add("is-marquee");
      runMarqueeFrame();
    }, MARQUEE_DELAY);
  });

  sessionList.addEventListener("mouseout", function (e) {
    const name = e.target.closest(".session-name");
    if (!name) return;
    // 启动延迟期内移出：直接取消
    if (marqueeTimer) {
      clearTimeout(marqueeTimer);
      marqueeTimer = null;
      return;
    }
    if (name !== marqueeName) return;
    // 复位回滚已在进行：无需再防抖，等它自然归零
    if (marqueeAnimatingReset) return;
    // 滚动中移出：防抖后平滑回滚；80ms 内回到同一标题则继续正向滚动
    if (marqueeResetTimer) clearTimeout(marqueeResetTimer);
    clearMarqueeRaf();
    marqueeResetTimer = setTimeout(function () {
      marqueeResetTimer = null;
      if (name !== marqueeName || marqueeAnimatingReset) return;
      runMarqueeReset();
    }, MARQUEE_RESET_DEBOUNCE);
  });

  function buildSessionItem(id, title) {
    const item = el("div", "session-item" + (id === state.sessionId ? " active" : ""));
    item.dataset.session = id;    // 多选模式的选中气泡：bulk-mode 下显示，点击切换选中（自身是死区元素，必须绑事件）
    const check = el("span", "session-check");
    check.innerHTML = '<svg class="icon" viewBox="0 0 24 24"><path d="M20 6 9 17l-5-5"/></svg>';
    check.addEventListener("click", function (event) {
      event.stopPropagation();
      if (bulkMode) toggleBulkSelect(id, item);
    });
    item.appendChild(check);
    if (bulkMode && bulkSelected.has(id)) item.classList.add("bulk-selected");
    const select = el("button", "session-select");
    select.type = "button";
    // 标题双层结构：外层 .session-name 固定裁剪窗口（overflow:hidden），
    // 内层 .session-name-text 承载文本并作为跑马灯 transform 载体——
    // transform 平移的是内层文本，外层窗口不动（旧 text-indent 语义的
    // transform 等价实现）；写文本统一走 setSessionNameText 保结构
    const nameNode = el("span", "session-name");
    nameNode.appendChild(el("span", "session-name-text", title));
    select.appendChild(nameNode);
    select.addEventListener("click", function () {
      if (bulkMode) {
        toggleBulkSelect(id, item);
        return;
      }
      closeSessionMenus();
      openSession(id);
    });
    item.appendChild(select);

    const actions = el("button", "session-actions");
    actions.type = "button";
    actions.title = "会话操作";
    actions.innerHTML = '<svg class="icon" viewBox="0 0 24 24"><circle cx="5" cy="12" r="2" fill="currentColor"/><circle cx="12" cy="12" r="2" fill="currentColor"/><circle cx="19" cy="12" r="2" fill="currentColor"/></svg>';
    actions.addEventListener("click", function (event) {
      event.stopPropagation();
      if (bulkMode) {
        toggleBulkSelect(id, item);
        return;
      }
      toggleSessionMenu(id, item, actions);
    });
    item.appendChild(actions);
    return item;
  }

  // ---------- 会话多选（批量分享 / 批量删除） ----------
  let bulkMode = false;
  const bulkSelected = new Set();

  function selectedVisibleIds() {
    // 只统计当前可见（未被搜索过滤；filterSessions 用 style.display 隐藏）的选中项
    return Array.from(sessionList.querySelectorAll(".session-item"))
      .filter(function (node) { return node.style.display !== "none"; })
      .map(function (node) { return node.dataset.session; })
      .filter(function (id) { return bulkSelected.has(id); });
  }

  function toggleBulkSelect(id, item) {
    if (bulkSelected.has(id)) {
      bulkSelected.delete(id);
      item.classList.remove("bulk-selected");
    } else {
      bulkSelected.add(id);
      item.classList.add("bulk-selected");
    }
    updateBulkBarState();
  }

  function updateBulkBarState() {
    const count = bulkSelected.size;
    const shareBtn = $("#bulkShareBtn");
    const delBtn = $("#bulkDeleteBtn");
    if (shareBtn) shareBtn.disabled = count === 0;
    if (delBtn) delBtn.disabled = count === 0;
    const label = $("#bulkCount");
    if (label) label.textContent = count ? "已选 " + count : "";
  }

  /** 多选模式切换按钮：进入后自身变为「取消多选」，并展开下方分享/删除操作条。 */
  function setBulkToggle(active) {
    const btn = $("#bulkModeBtn");
    if (!btn) return;
    const label = btn.querySelector("span");
    if (active) {
      btn.classList.add("bulk-mode-active");
      if (label) label.textContent = "取消多选";
      btn.title = "退出多选";
    } else {
      btn.classList.remove("bulk-mode-active");
      if (label) label.textContent = "多选";
      btn.title = "多选会话，批量分享/删除；再次点击退出多选";
    }
  }

  function enterBulkMode() {
    bulkMode = true;
    bulkSelected.clear();
    document.body.classList.add("bulk-mode");
    // 多选按钮变为「取消多选」，并展开操作条（分享/删除/已选计数）
    setBulkToggle(true);
    $("#sessionBulkBar").classList.remove("hidden");
    sessionList.querySelectorAll(".session-item").forEach(function (item) {
      item.classList.remove("bulk-selected");
    });
    updateBulkBarState();
  }

  function exitBulkMode() {
    bulkMode = false;
    bulkSelected.clear();
    document.body.classList.remove("bulk-mode");
    setBulkToggle(false);
    $("#sessionBulkBar").classList.add("hidden");
    sessionList.querySelectorAll(".session-item").forEach(function (item) {
      item.classList.remove("bulk-selected");
    });
    updateBulkBarState();
  }

  /** 批量分享：所选会话打包 zip 下载（复用后端 export_zip 接口）。 */
  async function bulkShareSessions() {
    const ids = selectedVisibleIds();
    if (!ids.length) {
      toast("请先选择要分享的会话");
      return;
    }
    toast("正在打包 " + ids.length + " 个会话…");
    try {
      const result = await API.exportSessionsZip(ids);
      const skippedNote = result.skipped && result.skipped.length
        ? "，" + result.skipped.length + " 个不存在已跳过"
        : "";
      toast("已下载 " + result.name + skippedNote);
    } catch (err) {
      toast("分享失败：" + err.message);
    } finally {
      // 分享动作结束（成功/失败一致）后自动退出多选，回到普通浏览状态
      exitBulkMode();
    }
  }

  /** 批量删除：复用删除确认弹窗（标题动态显示数量）。 */
  function openBulkDeleteModal() {
    const ids = selectedVisibleIds();
    if (!ids.length) {
      toast("请先选择要删除的会话");
      return;
    }
    const streaming = ids.filter(function (id) {
      return state.activeStream && state.activeStream.sessionId === id;
    });
    if (streaming.length === ids.length) {
      toast("生成中的会话请先停止后再删除");
      return;
    }
    state.pendingBulkDelete = ids.filter(function (id) {
      return !state.activeStream || state.activeStream.sessionId !== id;
    });
    if (!state.pendingBulkDelete.length) return;
    if (streaming.length) toast("生成中的会话已跳过");
    $("#deleteModalTitle").textContent = "删除 " + state.pendingBulkDelete.length + " 个会话？";
    deleteModal.querySelector("p").textContent =
      "删除后将无法恢复这些会话及其历史记录（含上传数据目录），确定继续吗？";
    deleteModal.classList.remove("hidden");
    deleteModal.setAttribute("aria-hidden", "false");
  }

  async function confirmBulkDelete() {
    const ids = state.pendingBulkDelete;
    deleteConfirm.disabled = true;
    try {
      const errors = [];
      let removed = 0;
      for (const id of ids) {
        try {
          await API.deleteSession(id);
          delete state.sessionRecency[id];
          const overrides = readTitleOverrides();
          delete overrides[id];
          localStorage.setItem(SESSION_TITLE_KEY, JSON.stringify(overrides));
          const node = sessionList.querySelector('[data-session="' + CSS.escape(id) + '"]');
          if (node) node.remove();
          removed++;
        } catch (err) {
          errors.push(id + "：" + err.message);
        }
      }
      // 恢复删除弹窗的默认文案（单会话删除共用该弹窗）
      $("#deleteModalTitle").textContent = "删除会话？";
      deleteModal.querySelector("p").textContent =
        "删除后将无法恢复该会话及其历史记录，确定继续吗？";
      state.pendingBulkDelete = null;
      closeDeleteModal();
      if (removed) toast("已删除 " + removed + " 个会话");
      if (errors.length) toast(errors.length + " 个删除失败：" + errors[0]);
      if (ids.includes(state.sessionId)) startNewChat();
      await loadSessions();
    } finally {
      // 无论删除完成还是中途异常，都退出多选并恢复确认按钮
      exitBulkMode();
      deleteConfirm.disabled = false;
    }
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
    stopTitleMarquee(); // 进入编辑态会替换 .session-select：先停掉该行跑马灯，避免回滚状态残留
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
    // 恢复弹窗默认文案（批量删除共用弹窗后，取消/确认都要还原标题与说明）
    const titleNode = $("#deleteModalTitle");
    if (titleNode) titleNode.textContent = "删除会话？";
    const descNode = deleteModal.querySelector("p");
    if (descNode) descNode.textContent = "删除后将无法恢复该会话及其历史记录，确定继续吗？";
    state.pendingBulkDelete = null;
    state.pendingDelete = null;
    deleteModal.classList.add("hidden");
    deleteModal.setAttribute("aria-hidden", "true");
  }

  async function confirmDeleteSession() {
    // 批量删除：pendingBulkDelete 非空时走批量流程（与单会话共用同一弹窗）
    if (state.pendingBulkDelete) {
      await confirmBulkDelete();
      return;
    }
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
      setSessionNameText(name, title);
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
      // 文件变更徽标事件驱动同步（替代旧 2s 轮询看门狗；未变化时零请求）
      if (App.fileHistory && App.fileHistory.noteSessionChanged) App.fileHistory.noteSessionChanged();
    }
    return state.sessionId;
  }

  function startNewChat() {
    sessionOpenSeq++;
    // 保存当前会话的输入草稿（文本+附件），再进入"待开始"状态
    App.saveSessionDraft();
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
    // 恢复"新对话"的草稿（通常为空；未发送就切走的内容不会丢）
    App.restoreSessionDraft(null);
    App.resetContextTokenStats();
    App.refreshChatModelLabel(); // 新对话回到全局默认（服务端返回全局选择）
    if (App.fileHistory && App.fileHistory.noteSessionChanged) App.fileHistory.noteSessionChanged();
    state.hasConversation = false;
    state.importedHistoryText = null;
    enhancePanel.classList.add("hidden");
    if (App.dockEnhancePanel) App.dockEnhancePanel(); // body 级浮层挂回 .composer 原位
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

  // ---------- 多选工具条事件 ----------
  const bulkModeBtn = $("#bulkModeBtn");
  const bulkShareBtn = $("#bulkShareBtn");
  const bulkDeleteBtn = $("#bulkDeleteBtn");
  if (bulkModeBtn) {
    // 同一按钮：未进入时→进入多选；进入后按钮文案为「取消多选」→退出
    bulkModeBtn.addEventListener("click", function () {
      if (bulkMode) exitBulkMode();
      else enterBulkMode();
    });
  }
  if (bulkShareBtn) bulkShareBtn.addEventListener("click", bulkShareSessions);
  if (bulkDeleteBtn) bulkDeleteBtn.addEventListener("click", openBulkDeleteModal);

  async function openSession(id) {
    const seq = ++sessionOpenSeq;
    // 保存当前会话的输入草稿（文本+附件），再切换到目标会话
    App.saveSessionDraft();
    // 切换会话时结束进行中的语音输入（识别文本不能串写到另一个会话的输入框）
    if (App.stopVoiceInput) App.stopVoiceInput();
    id = SessionUtils.sanitizeSessionId(id);
    state.sessionId = id;
    // 文件变更徽标事件驱动同步（会话切换即刷新一次；未变化时零请求）
    if (App.fileHistory && App.fileHistory.noteSessionChanged) App.fileHistory.noteSessionChanged();
    App.loadWorkDir();
    App.loadToolSelection(); // 按会话加载独立工具选择（未覆盖则应用全局默认）
    // 模型面板打开中时按新会话的生效选择刷新
    if (!enhancePanel.classList.contains("hidden")) App.openModelPanel();
    // 恢复目标会话的输入草稿（文本+附件；无草稿则清空）
    App.restoreSessionDraft(id);
    App.resetContextTokenStats();
    // 会话切换时同步状态条模型名：显式传 id 强制按目标会话拉取生效选择
    //（会话独立选择 → 全局默认），避免沿用上一会话的模型缓存显示
    App.refreshChatModelLabel(id);
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
    // 刷新引导/队列暂存指示器：只显示属于当前会话的条目
    App.renderPendingOutbox();
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
        // 不传 replayState：初始打开走分段渲染（末尾 240 条先行，向上滚动
        // 加载更早），flushRenderedTail 内部负责贴底
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
    // 模型名刷新已提升到 openSession 主流程（此处不再调用，避免同一次打开重复请求）
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
  App.setSessionNameText = setSessionNameText;
  App.ensureSessionId = ensureSessionId;
  App.startNewChat = startNewChat;
  App.openSession = openSession;
  App.restoreActiveStream = restoreActiveStream;
  // 多选批量操作（分享 zip / 删除）
  App.enterBulkMode = enterBulkMode;
  App.exitBulkMode = exitBulkMode;
  App.bulkShareSessions = bulkShareSessions;
  App.openBulkDeleteModal = openBulkDeleteModal;
})(window.App);
