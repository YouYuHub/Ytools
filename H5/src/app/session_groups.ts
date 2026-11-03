/**
 * 会话分组（分组区 UI + 会话归组交互）
 * - 分组区位于「最近」上方：悬停 / 点击标题展开分组列表，右上角 + 新建分组
 * - 每个分组可折叠/展开（展开显示组内会话标题），支持重命名 / 删除
 * - 会话菜单「分组」入口：选择分组 / 移出分组 / 新建分组并移入
 * - 归属与分组定义的真源在后端（_meta.group_id + session_groups.json）
 * 依赖：app/core.js、app/sessions.js（App.buildSessionItem）、API、SessionGroupUtils
 */
(function (App: any) {
  "use strict";
  const { state, $, el, toast } = App;

  const OPEN_KEY = "ytools-session-groups-open";

  const host = $("#sessionGroups");
  if (!host) return; // HTML 未接线时静默停用（不影响会话列表）
  const head = $("#sessionGroupsHead");
  const body = $("#sessionGroupsBody");
  const countNode = $("#sessionGroupsCount");
  const addBtn = $("#sessionGroupAddBtn");

  const deleteModal = $("#groupDeleteModal");
  const deleteConfirmBtn = $("#groupDeleteConfirm");

  let groups: any = [];        // 规整后的分组列表 [{id,name,collapsed,order,createdAt}]
  let assignments: any = {};   // 会话归属 {sessionId: groupId}
  let rows: any = [];          // 最近一次会话行 [{id,title,...}]（由 loadSessions 注入）
  let pinned = readPinned();  // 点击固定展开
  let hoverOpen = false;      // 悬停临时展开
  let pendingDeleteGroup: any = null;

  // ---------- 展开状态（点击固定 + 悬停临时） ----------
  function readPinned(): boolean {
    try { return localStorage.getItem(OPEN_KEY) === "1"; } catch (_) { return false; }
  }
  function writePinned(value: any): void {
    try { localStorage.setItem(OPEN_KEY, value ? "1" : "0"); } catch (_) { /* 忽略 */ }
  }
  window.addEventListener("ytools-preferences-change", () => { pinned = readPinned(); applyOpenState(); });
  function isOpen(): boolean { return pinned || hoverOpen; }

  function applyOpenState(): void {
    host.classList.toggle("open", isOpen());
    // 钉住态视觉标识（图钉常显高亮；未钉住时悬停淡显提示）
    host.classList.toggle("pinned", pinned);
    updateHeadHint();
    refreshFades();
    // 展开瞬间元素刚从 display:none 变为可见，scrollWidth 尚未完成布局计算；
    // 下一帧再测一次，保证长标题的右缘渐隐遮罩正确挂上
    if (isOpen() && typeof requestAnimationFrame === "function") {
      requestAnimationFrame(refreshFades);
    }
  }

  /** 标题行提示文案随钉住状态更新（悬停 tooltip + 无障碍标签）。 */
  function updateHeadHint(): void {
    if (!head) return;
    head.title = pinned
      ? "已固定展开：点击标题取消固定"
      : "点击标题固定展开；仅悬停时临时展开";
    head.setAttribute("aria-label", pinned
      ? "分组列表已固定展开，点击取消固定"
      : "展开或收起分组列表");
  }

  function ensureOpen(): void {
    if (!pinned) {
      pinned = true;
      writePinned(true);
    }
    hoverOpen = false;
    applyOpenState();
  }

  function togglePinned(): void {
    pinned = !pinned;
    writePinned(pinned);
    hoverOpen = false;
    applyOpenState();
  }

  // ---------- 渲染 ----------
  function updateCount(): void {
    if (!countNode) return;
    countNode.textContent = groups.length ? String(groups.length) : "";
  }

  /** 溢出淡出标记（分组区独立实现：会话列表的 refreshSessionFades 只作用于 #sessionList）。 */
  function refreshFades(): void {
    body.querySelectorAll(".session-name").forEach(function (node: any) {
      node.classList.toggle("truncated", node.scrollWidth > node.clientWidth + 1);
    });
  }

  function escapeSel(value: any): string {
    return (window as unknown as { CSS: any }).CSS && CSS.escape ? CSS.escape(String(value)) : String(value).replace(/["\\]/g, "\\$&");
  }

  function memberRows(groupId: any): any {
    return rows.filter(function (row: any) { return assignments[row.id] === groupId; });
  }

  function buildGroupBlock(group: any): any {
    const block = el("div", "session-group" + (group.collapsed ? "" : " expanded"));
    block.dataset.group = group.id;

    const headRow = el("div", "session-group-head");
    const toggle = el("button", "session-group-toggle");
    toggle.type = "button";
    toggle.title = group.collapsed ? "展开分组" : "折叠分组";
    toggle.innerHTML =
      '<svg class="icon session-group-caret" viewBox="0 0 24 24"><path d="m9 6 6 6-6 6"/></svg>' +
      '<span class="session-group-name"></span>' +
      '<span class="session-group-count"></span>';
    toggle.querySelector(".session-group-name").textContent = group.name;
    const members = memberRows(group.id);
    toggle.querySelector(".session-group-count").textContent = members.length ? String(members.length) : "";
    toggle.addEventListener("click", function () { toggleGroupCollapsed(group); });
    headRow.appendChild(toggle);

    const actions = el("button", "session-group-actions");
    actions.type = "button";
    actions.title = "分组操作";
    actions.innerHTML = '<svg class="icon" viewBox="0 0 24 24"><circle cx="5" cy="12" r="2" fill="currentColor"/><circle cx="12" cy="12" r="2" fill="currentColor"/><circle cx="19" cy="12" r="2" fill="currentColor"/></svg>';
    actions.addEventListener("click", function (event: any) {
      event.stopPropagation();
      toggleGroupMenu(group, actions);
    });
    headRow.appendChild(actions);
    block.appendChild(headRow);

    if (!group.collapsed) {
      // 复用 .session-list 容器类：组内会话行直接获得与会话列表一致的样式与交互
      const listBox = el("div", "session-group-sessions session-list");
      if (!members.length) {
        listBox.appendChild(el("div", "session-group-empty", "分组内暂无会话"));
      } else {
        members.forEach(function (row: any) {
          const item = App.buildSessionItem(row.id, row.title);
          listBox.appendChild(item);
        });
        // 组内行标题接入 hover 跑马灯（复用会话列表的实现：溢出才滚动、
        // 250ms 延迟触发、移出平滑回滚）；监听按容器去重，块重建后不重复绑定
        if (App.bindTitleMarquee) App.bindTitleMarquee(listBox);
      }
      block.appendChild(listBox);
    }
    return block;
  }

  function render(newRows?: any): void {
    if (Array.isArray(newRows)) rows = newRows;
    // 重建前收尾组内跑马灯：旧 DOM 将被整体替换，滚动载体脱离文档后
    // 接任查找会串到「最近」列表中的同名行
    if (App.stopTitleMarqueeIn) App.stopTitleMarqueeIn(body);
    body.innerHTML = "";
    if (!groups.length) {
      body.appendChild(el("div", "session-groups-empty", "暂无分组，点右上角 + 新建"));
    } else {
      groups.forEach(function (group: any) { body.appendChild(buildGroupBlock(group)); });
    }
    updateCount();
    applyOpenState();
    // 多选模式的分组视图依赖分组数据：数据变化时同步重建（未接线时无操作）
    if (App.refreshBulkGroupedView) App.refreshBulkGroupedView();
  }

  // ---------- 数据 ----------
  async function refresh(): Promise<void> {
    try {
      const data = await API.listSessionGroups();
      groups = SessionGroupUtils.normalizeGroups(data && data.groups);
      assignments = SessionGroupUtils.normalizeAssignments(data && data.assignments);
      render();
    } catch (_) { /* 分组接口失败不阻塞会话列表 */ }
  }

  function titleOf(sessionId: any): any {
    const row = rows.find(function (item: any) { return item.id === sessionId; });
    if (row && row.title) return row.title;
    const node = App.sessionList && App.sessionList.querySelector(
      '[data-session="' + escapeSel(sessionId) + '"] .session-name');
    return node ? node.textContent : sessionId;
  }

  function upsertRow(sessionId: any, title: any): void {
    const row = rows.find(function (item: any) { return item.id === sessionId; });
    if (row) {
      if (title) row.title = title;
    } else {
      rows.push({ id: sessionId, title: title || sessionId });
    }
    if (assignments[sessionId]) render();
  }

  /** 标题变化同步（不新增行）：标题模型生成结果回填时刷新组内显示。 */
  function updateRowTitle(sessionId: any, title: any): void {
    const text = (title == null ? "" : String(title)).trim();
    if (!text) return;
    const row = rows.find(function (item: any) { return item.id === sessionId; });
    if (!row || row.title === text) return;
    row.title = text;
    if (assignments[sessionId]) render();
  }

  function removeSession(sessionId: any): void {
    rows = rows.filter(function (item: any) { return item.id !== sessionId; });
    delete assignments[sessionId];
    render();
  }

  function markActive(sessionId: any): void {
    body.querySelectorAll(".session-item").forEach(function (node: any) {
      node.classList.toggle("active", node.dataset.session === sessionId);
    });
  }

  function applyFilter(keyword: any): void {
    host.style.display = keyword ? "none" : "";
  }

  // ---------- 分组折叠 / 重命名 / 删除 ----------
  function toggleGroupCollapsed(group: any): void {
    group.collapsed = !group.collapsed;
    render();
    API.updateSessionGroup(group.id, { collapsed: group.collapsed }).catch(function (err: any) {
      group.collapsed = !group.collapsed;
      render();
      toast("分组状态保存失败：" + err.message);
    });
  }

  function closeGroupMenus(): void {
    document.querySelectorAll(".session-group-menu").forEach(function (menu: any) { menu.remove(); });
    body.querySelectorAll(".session-group-head.menu-open").forEach(function (node: any) {
      node.classList.remove("menu-open");
    });
  }

  function menuButton(label: any, icon: any, handler: any, className?: any): any {
    const button = el("button", "menu-item" + (className ? " " + className : ""));
    button.type = "button";
    button.innerHTML = icon + '<span class="menu-item-label"></span>';
    button.querySelector(".menu-item-label").textContent = label;
    button.addEventListener("click", function (event: any) {
      event.stopPropagation();
      closeGroupMenus();
      handler();
    });
    return button;
  }

  function positionMenu(menu: any, anchor: any): void {
    if (!anchor) return;
    const rect = anchor.getBoundingClientRect();
    menu.style.top = Math.min(rect.bottom + 4, window.innerHeight - menu.offsetHeight - 8) + "px";
    menu.style.left = Math.max(8, rect.right - menu.offsetWidth) + "px";
  }

  const RENAME_ICON = '<svg class="icon" viewBox="0 0 24 24"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>';
  const DELETE_ICON = '<svg class="icon" viewBox="0 0 24 24"><path d="M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2m2 0v14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2V6"/></svg>';

  function toggleGroupMenu(group: any, anchor: any): void {
    const existing: any = document.querySelector(".session-group-menu");
    if (existing && existing.dataset.group === group.id) {
      closeGroupMenus();
      return;
    }
    closeGroupMenus();
    const headRow = anchor.closest(".session-group-head");
    if (headRow) headRow.classList.add("menu-open");
    const menu = el("div", "menu session-group-menu");
    menu.dataset.group = group.id;
    menu.appendChild(menuButton("重命名", RENAME_ICON, function () { startGroupRename(group); }));
    menu.appendChild(menuButton("删除", DELETE_ICON, function () { openGroupDeleteModal(group); }, "danger"));
    document.body.appendChild(menu);
    positionMenu(menu, anchor);
  }

  function startGroupRename(group: any): void {
    ensureOpen();
    const block = body.querySelector('[data-group="' + escapeSel(group.id) + '"]');
    if (!block) return;
    const toggle = block.querySelector(".session-group-toggle");
    if (!toggle) return;
    const editor = document.createElement("input");
    editor.className = "session-group-name-editor";
    editor.type = "text";
    editor.value = group.name;
    editor.maxLength = SessionGroupUtils.GROUP_NAME_MAX;
    editor.setAttribute("aria-label", "编辑分组名称");
    const wrap = el("div", "session-group-edit");
    wrap.appendChild(editor);
    toggle.replaceWith(wrap);
    editor.focus();
    editor.select();

    let done = false;
    async function finish(save: any): Promise<void> {
      if (done) return;
      done = true;
      const res = SessionGroupUtils.validateGroupName(editor.value);
      if (!save || !res.ok || res.name === group.name) {
        if (save && !res.ok && String(editor.value).trim()) toast(res.error);
        render();
        return;
      }
      try {
        await API.updateSessionGroup(group.id, { name: res.name });
        group.name = res.name;
        toast("分组已重命名");
      } catch (err) {
        toast("重命名失败：" + (err as any).message);
      }
      render();
    }
    editor.addEventListener("keydown", function (event: any) {
      if (event.key === "Enter") { event.preventDefault(); finish(true); }
      if (event.key === "Escape") { event.preventDefault(); finish(false); }
    });
    editor.addEventListener("blur", function () { finish(true); });
    editor.addEventListener("click", function (event: any) { event.stopPropagation(); });
  }

  function openGroupDeleteModal(group: any): void {
    pendingDeleteGroup = group;
    if (!deleteModal) {
      doDeleteGroup(group);
      return;
    }
    const titleNode = $("#groupDeleteTitle");
    if (titleNode) titleNode.textContent = "删除分组「" + group.name + "」？";
    deleteModal.classList.remove("hidden");
    deleteModal.setAttribute("aria-hidden", "false");
  }

  function closeGroupDeleteModal(): void {
    pendingDeleteGroup = null;
    if (!deleteModal) return;
    deleteModal.classList.add("hidden");
    deleteModal.setAttribute("aria-hidden", "true");
  }

  async function doDeleteGroup(group: any): Promise<void> {
    try {
      const result = await API.deleteSessionGroup(group.id);
      groups = groups.filter(function (item: any) { return item.id !== group.id; });
      Object.keys(assignments).forEach(function (sid: any) {
        if (assignments[sid] === group.id) delete assignments[sid];
      });
      render();
      const released = result && result.released_sessions ? result.released_sessions : 0;
      toast(released ? "分组已删除，" + released + " 个会话回到未分组" : "分组已删除");
    } catch (err) {
      toast("删除分组失败：" + (err as any).message);
    }
  }

  // ---------- 新建分组 ----------
  function startNewGroup(): void {
    ensureOpen();
    const existing = body.querySelector(".session-group-new");
    if (existing) {
      const input = existing.querySelector("input");
      if (input) input.focus();
      return;
    }
    const row = el("div", "session-group-new");
    const editor = document.createElement("input");
    editor.className = "session-group-name-editor";
    editor.type = "text";
    editor.placeholder = "新分组名称，回车创建";
    editor.maxLength = SessionGroupUtils.GROUP_NAME_MAX;
    editor.setAttribute("aria-label", "新分组名称");
    row.appendChild(editor);
    body.insertBefore(row, body.firstChild);
    editor.focus();

    let done = false;
    async function finish(save: any): Promise<void> {
      if (done) return;
      done = true;
      const res = SessionGroupUtils.validateGroupName(editor.value);
      if (!save || !res.ok) {
        row.remove();
        if (save && !res.ok && String(editor.value).trim()) toast(res.error);
        return;
      }
      try {
        await API.createSessionGroup(res.name);
        toast("分组「" + res.name + "」已创建");
        await refresh();
      } catch (err) {
        row.remove();
        toast("新建分组失败：" + (err as any).message);
      }
    }
    editor.addEventListener("keydown", function (event: any) {
      if (event.key === "Enter") { event.preventDefault(); finish(true); }
      if (event.key === "Escape") { event.preventDefault(); finish(false); }
    });
    editor.addEventListener("blur", function () { finish(true); });
  }

  // ---------- 会话菜单「分组」选择器 ----------
  function pickerItem(label: any, checked: any, handler: any, extraClass?: any): any {
    const button = el("button", "menu-item session-picker-item" + (extraClass ? " " + extraClass : ""));
    button.type = "button";
    button.innerHTML = '<svg class="menu-check" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M20 6 9 17l-5-5"/></svg><span class="menu-item-label"></span>';
    button.querySelector(".menu-item-label").textContent = label;
    button.classList.toggle("selected", !!checked);
    button.addEventListener("click", function (event: any) {
      event.stopPropagation();
      handler();
    });
    return button;
  }

  function closeSessionMenu(): void {
    if (App.closeSessionMenus) App.closeSessionMenus();
  }

  async function assign(sessionId: any, groupId: any, groupName: any): Promise<void> {
    try {
      await API.assignSessionGroup(sessionId, groupId);
      if (groupId) {
        assignments[sessionId] = groupId;
        if (!rows.some(function (row: any) { return row.id === sessionId; })) {
          rows.push({ id: sessionId, title: titleOf(sessionId) });
        }
      } else {
        delete assignments[sessionId];
      }
      render();
      toast(groupId ? "已移动到分组「" + groupName + "」" : "已移出分组");
    } catch (err) {
      toast("分组操作失败：" + (err as any).message);
    }
  }

  /**
   * 会话菜单内的「分组」选择器：重写菜单内容为分组列表。
   * menu 为 sessions.js 创建的 .session-menu（仍挂在 body 上），anchor 为触发按钮。
   */
  function openPicker(sessionId: any, menu: any, anchor: any): void {
    if (!menu) return;
    menu.innerHTML = "";
    menu.classList.add("session-picker");
    menu.appendChild(el("div", "session-picker-title", "移动到分组"));

    const current = assignments[sessionId] || null;
    groups.forEach(function (group: any) {
      menu.appendChild(pickerItem(group.name, current === group.id, function () {
        closeSessionMenu();
        assign(sessionId, group.id, group.name);
      }));
    });
    if (!groups.length) {
      menu.appendChild(el("div", "session-picker-empty", "暂无分组"));
    }
    if (current) {
      menu.appendChild(pickerItem("移出分组", false, function () {
        closeSessionMenu();
        assign(sessionId, null, null);
      }, "session-picker-remove"));
    }
    menu.appendChild(el("div", "menu-divider"));

    menu.appendChild(pickerItem("新建分组…", false, function () {
      menu.innerHTML = "";
      menu.appendChild(el("div", "session-picker-title", "新建分组并移入"));
      const row = el("div", "session-picker-input-row");
      const editor = document.createElement("input");
      editor.className = "session-group-name-editor";
      editor.type = "text";
      editor.placeholder = "分组名称，回车创建";
      editor.maxLength = SessionGroupUtils.GROUP_NAME_MAX;
      editor.setAttribute("aria-label", "新分组名称");
      row.appendChild(editor);
      menu.appendChild(row);
      editor.focus();
      let done = false;
      async function finish(save: any): Promise<void> {
        if (done) return;
        done = true;
        const res = SessionGroupUtils.validateGroupName(editor.value);
        if (!save || !res.ok) {
          closeSessionMenu();
          if (save && !res.ok && String(editor.value).trim()) toast(res.error);
          return;
        }
        try {
          const out = await API.createSessionGroup(res.name);
          const group = out && out.group;
          if (group) {
            await assign(sessionId, group.id, group.name);
          } else {
            await refresh();
          }
        } catch (err) {
          toast("新建分组失败：" + (err as any).message);
        } finally {
          closeSessionMenu();
        }
      }
      editor.addEventListener("keydown", function (event: any) {
        if (event.key === "Enter") { event.preventDefault(); finish(true); }
        if (event.key === "Escape") { event.preventDefault(); finish(false); }
      });
      editor.addEventListener("blur", function () { finish(true); });
      editor.addEventListener("click", function (event: any) { event.stopPropagation(); });
    }));

    positionMenu(menu, anchor);
  }

  // ---------- 事件接线 ----------
  head.addEventListener("click", function (event: any) {
    if (event.target.closest(".session-groups-add")) return;
    togglePinned();
  });
  head.addEventListener("keydown", function (event: any) {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      togglePinned();
    }
  });
  addBtn.addEventListener("click", function (event: any) {
    event.stopPropagation();
    startNewGroup();
  });
  host.addEventListener("mouseenter", function () {
    hoverOpen = true;
    applyOpenState();
  });
  host.addEventListener("mouseleave", function () {
    hoverOpen = false;
    applyOpenState();
  });

  if (deleteModal) {
    const backdrop = $("#groupDeleteBackdrop");
    const cancelBtn = $("#groupDeleteCancel");
    if (backdrop) backdrop.addEventListener("click", closeGroupDeleteModal);
    if (cancelBtn) cancelBtn.addEventListener("click", closeGroupDeleteModal);
    if (deleteConfirmBtn) {
      deleteConfirmBtn.addEventListener("click", async function () {
        const group = pendingDeleteGroup;
        closeGroupDeleteModal();
        if (group) await doDeleteGroup(group);
      });
    }
  }

  document.addEventListener("click", function (event: any) {
    if (event.target.closest(".session-group-menu") || event.target.closest(".session-group-actions")) return;
    closeGroupMenus();
  });
  document.addEventListener("keydown", function (event: any) {
    if (event.key !== "Escape") return;
    closeGroupMenus();
    if (deleteModal && !deleteModal.classList.contains("hidden")) closeGroupDeleteModal();
  });

  // ---------- 导出 ----------
  App.sessionGroups = {
    render: render,
    refresh: refresh,
    removeSession: removeSession,
    upsertRow: upsertRow,
    updateRowTitle: updateRowTitle,
    markActive: markActive,
    applyFilter: applyFilter,
    openPicker: openPicker,
    closeMenus: closeGroupMenus,
    getGroups: function () { return groups.slice(); },
    getAssignments: function () { return Object.assign({}, assignments); },
    getAssignment: function (sessionId: any) { return assignments[sessionId] || null; },
  };

  // 初始渲染 + 拉取分组数据（会话行由 sessions.js loadSessions 注入）
  render();
  refresh();
})((window as unknown as { App: any }).App);
