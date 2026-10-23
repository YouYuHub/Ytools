/** Local archive organization: settings popup, archive browser and current-state controls. */
(function (App) {
  "use strict";
  const { state, el, toast } = App;
  const anchors = [document.getElementById("profileCard"), document.getElementById("railAvatar")].filter(Boolean);
  const menu = el("div", "menu sidebar-settings-menu"); menu.hidden = true; menu.id = "sidebarSettingsMenu"; menu.setAttribute("role", "menu");
  document.body.append(menu);
  let menuAnchor = null, menuFocus = null;
  function hideSettings(restore) {
    menu.hidden = true;
    if (menuAnchor) menuAnchor.setAttribute("aria-expanded", "false");
    if (restore && menuFocus && menuFocus.isConnected) menuFocus.focus({ preventScroll: true });
    menuAnchor = null;
  }
  function menuItem(text, path, action) {
    const item = el("button", "menu-item"); item.type = "button"; item.setAttribute("role", "menuitem");
    item.innerHTML = '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="' + path + '"/></svg>';
    item.append(el("span", "menu-item-label", text));
    item.addEventListener("click", function () { const anchor = menuAnchor; hideSettings(false); if (anchor) anchor.focus({ preventScroll: true }); action(); });
    menu.append(item); return item;
  }
  menuItem("归档会话", "M3 3h18v5H3zM5 8v13h14V8M9 12h6", open);
  menuItem("主题", "M12 3a9 9 0 1 0 9 9c0-2-2-3-4-2s-4 0-3-2 0-5-2-5Z", function () { App.openThemePicker(); });
  menuItem("聊天设置", "M4 4v16M12 4v16M20 4v16M1 8h6M9 16h6M17 10h6", function () { App.openChatSettings(); });
  function statusToggleLabel() {
    const visible = !App.taskStatus || !App.taskStatus.isStatusVisible || App.taskStatus.isStatusVisible();
    return visible ? "隐藏会话状态" : "显示会话状态";
  }
  const statusToggleItem = menuItem("隐藏会话状态", "M3 12h4l3 8 4-16 3 8h4", function () {
    if (App.taskStatus && App.taskStatus.toggleStatusVisible) {
      const visible = App.taskStatus.toggleStatusVisible();
      toast(visible ? "已显示会话状态" : "已隐藏会话状态");
    }
    syncStatusToggleItem();
  });
  function syncStatusToggleItem() {
    const label = statusToggleItem.querySelector(".menu-item-label");
    if (label) label.textContent = statusToggleLabel();
  }
  syncStatusToggleItem();
  anchors.forEach(function (anchor) { anchor.setAttribute("aria-haspopup", "menu"); anchor.setAttribute("aria-controls", menu.id); anchor.setAttribute("aria-expanded", "false"); });
  App.sidebarSettings = { toggle: function (anchor) {
    if (!menu.hidden && menuAnchor === anchor) { hideSettings(true); return; }
    hideSettings(false); App.closeMenus(); if (App.closeSessionMenus) App.closeSessionMenus();
    syncStatusToggleItem();
    menuFocus = anchor; menuAnchor = anchor; menu.hidden = false; anchor.setAttribute("aria-expanded", "true");
    const rect = anchor.getBoundingClientRect(), bounds = menu.getBoundingClientRect();
    menu.style.left = Math.max(8, Math.min(rect.left, window.innerWidth - bounds.width - 8)) + "px";
    menu.style.top = Math.max(8, Math.min(rect.top - bounds.height - 8, window.innerHeight - bounds.height - 8)) + "px";
    menu.querySelector("button").focus({ preventScroll: true });
  } };
  document.addEventListener("click", function (event) { if (!menu.hidden && !menu.contains(event.target) && !anchors.some(anchor => anchor.contains(event.target))) hideSettings(false); });
  window.addEventListener("resize", function () { hideSettings(true); });
  window.addEventListener("scroll", function (event) { if (!menu.hidden && !menu.contains(event.target)) hideSettings(false); }, true);
  menu.addEventListener("keydown", function (event) {
    const items = Array.from(menu.querySelectorAll("button")), at = items.indexOf(document.activeElement);
    if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); hideSettings(true); }
    if (event.key === "ArrowDown" || event.key === "ArrowUp") { event.preventDefault(); items[(at + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length].focus(); }
    if (event.key === "Tab") hideSettings(true);
  });

  const modal = el("div", "archive-modal"); modal.hidden = true; modal.id = "sessionArchivesModal";
  const backdrop = el("div", "archive-backdrop");
  const dialog = el("section", "archive-dialog"); dialog.setAttribute("role", "dialog"); dialog.setAttribute("aria-modal", "true"); dialog.setAttribute("aria-labelledby", "archiveTitle");
  function button(text, action, cls) { const node = el("button", cls || "archive-button", text); node.type = "button"; node.addEventListener("click", action); return node; }
  const heading = el("header", "archive-head"), title = el("h2", "", "归档会话"); title.id = "archiveTitle";
  const closeButton = button("×", close, "archive-close"); closeButton.setAttribute("aria-label", "关闭归档会话"); heading.append(title, closeButton);
  const intro = el("p", "archive-intro", "归档会从侧栏和默认搜索中隐藏。可在这里查看、搜索并继续聊天；取消归档后回到原分组，继续聊天不会自动取消归档。");
  const toolbar = el("div", "archive-toolbar"), query = el("input"); query.type = "search"; query.id = "archiveQuery"; query.maxLength = 200; query.placeholder = "筛选归档标题或会话 ID…"; query.setAttribute("aria-label", "筛选归档标题或会话 ID");
  const searchContent = button("搜索归档内容", function () { const text = query.value; close(); App.historySearch.open({ archiveScope: "archived", query: text }); }); searchContent.id = "archiveSearchContent";
  const refresh = button("刷新", function () { load(false); }); toolbar.append(query, searchContent, refresh);
  const status = el("p", "archive-status"); status.id = "archiveStatus"; status.setAttribute("role", "status"); status.setAttribute("aria-live", "polite");
  const list = el("div", "archive-list"); list.id = "archiveList";
  const footer = el("footer", "archive-footer"), more = button("加载更多", function () { load(true); }), retry = button("重试", function () { load(false); });
  more.hidden = retry.hidden = true; footer.append(retry, more); dialog.append(heading, intro, toolbar, status, list, footer); modal.append(backdrop, dialog); document.body.append(modal);
  let opened = false, seq = 0, controller = null, timer = null, rows = [], next = null, total = 0, busy = false, composing = false, confirming = null, returnFocus = null, background = [];
  const mutations = new Set();
  const archiveItem = button("归档会话", function () { App.closeMenus(); if (state.sessionId) setArchived(state.sessionId, !App.isSessionArchived(state.sessionId)); }, "menu-item hidden"); archiveItem.id = "archiveSessionItem";
  archiveItem.replaceChildren();
  const archiveIcon = document.createElementNS("http://www.w3.org/2000/svg", "svg"); archiveIcon.setAttribute("class", "icon"); archiveIcon.setAttribute("viewBox", "0 0 24 24"); archiveIcon.setAttribute("aria-hidden", "true");
  archiveIcon.innerHTML = '<path d="M3 3h18v5H3zM5 8v13h14V8M9 12h6"/>';
  const archiveLabel = el("span", "menu-item-label", "归档会话"); archiveItem.append(archiveIcon, archiveLabel);
  const actions = document.getElementById("sessionActionsMenu"); if (actions) actions.append(archiveItem);
  const banner = el("div", "archive-current-banner"); banner.id = "archivedSessionBanner"; banner.hidden = true;
  banner.append(el("span", "", "已归档 · 可继续聊天"), button("取消归档", function () { setArchived(state.sessionId, false); }));
  const composer = document.querySelector(".composer-wrap"); if (composer) composer.prepend(banner);
  function syncCurrent() {
    const archived = !!state.sessionId && App.isSessionArchived(state.sessionId);
    banner.hidden = !archived;
    archiveLabel.textContent = archived ? "取消归档" : "归档会话";
    archiveItem.classList.toggle("hidden", !state.sessionId || !state.hasConversation || !!state.importedHistoryText);
    archiveItem.disabled = mutations.has(state.sessionId);
    banner.querySelector("button").disabled = mutations.has(state.sessionId);
  }
  async function setArchived(id, archived) {
    if (!id || mutations.has(id)) return false;
    mutations.add(id); syncCurrent(); render();
    try {
      const result = await API.setSessionArchived(id, archived);
      App.noteSessionArchive(id, result.archived);
      if (!result.archived) rows = rows.filter(row => row.id !== id);
      toast(result.archived ? "会话已归档，可在设置中查看" : "已取消归档");
      await App.loadSessions();
      if (opened) await load(false);
      return true;
    } catch (error) { toast("更改归档状态失败：" + error.message); return false; }
    finally { mutations.delete(id); syncCurrent(); render(); }
  }
  function render() {
    const scroll = list.scrollTop; list.replaceChildren();
    rows.forEach(function (row) {
      const card = el("article", "archive-row"); card.dataset.session = row.id;
      const openButton = button(row.title || row.id, async function () {
        if (mutations.has(row.id)) return;
        App.noteSessionArchive(row.id, true); close();
        try { await App.openSession(row.id); } catch (error) { toast("会话打开失败：" + error.message); }
      }, "archive-row-title"); openButton.title = row.title; openButton.setAttribute("aria-label", "打开并继续聊天：" + (row.title || row.id));
      const meta = el("p", "archive-row-meta", "最后更新 " + (row.updated_at || "未知") + " · " + row.id);
      const rowActions = el("div", "archive-row-actions");
      if (confirming === row.id) {
        card.append(el("p", "archive-delete-warning", "永久删除会话及附件，无法恢复。运行中的任务将停止。"));
        rowActions.append(button("确认删除", function () { remove(row.id); }, "archive-button danger"), button("取消", function () { confirming = null; render(); }));
      } else {
        rowActions.append(button("取消归档", function () { setArchived(row.id, false); }), button("删除", function () {
          if (state.activeStream && state.activeStream.sessionId === row.id) { toast("生成中的会话请先停止后再删除"); return; }
          confirming = row.id; render();
          list.querySelector('[data-session="' + CSS.escape(row.id) + '"] .danger').focus();
        }, "archive-button danger"));
      }
      card.prepend(openButton, meta); card.append(rowActions);
      if (mutations.has(row.id)) card.querySelectorAll("button").forEach(node => { node.disabled = true; });
      list.append(card);
    });
    if (!rows.length && !busy) list.append(el("p", "archive-empty", query.value.trim() ? "没有匹配的归档会话" : "暂无归档会话"));
    list.scrollTop = scroll;
    more.hidden = next == null; more.disabled = busy; refresh.disabled = busy;
  }
  async function remove(id) {
    if (mutations.has(id)) return;
    mutations.add(id); render();
    try {
      await API.deleteSession(id);
      App.forgetSession(id);
      if (state.sessionId === id) App.startNewChat();
      confirming = null; toast("归档会话已永久删除");
      await App.loadSessions(); if (opened) await load(false);
    } catch (error) { toast("删除失败：" + error.message); }
    finally { mutations.delete(id); render(); }
  }
  async function load(append) {
    if (!opened || (append && next == null)) return;
    if (controller) controller.abort();
    const mine = ++seq, requestQuery = query.value.trim(); controller = new AbortController(); busy = true; retry.hidden = true;
    status.textContent = "正在读取归档会话…"; list.setAttribute("aria-busy", "true"); render();
    try {
      const data = await API.listArchivedSessions(requestQuery, append ? next : 0, controller.signal);
      if (!opened || mine !== seq) return;
      if (append) { const seen = new Set(rows.map(row => row.id)); rows = rows.concat(data.sessions.filter(row => !seen.has(row.id))); }
      else rows = data.sessions || [];
      total = data.total; next = data.next_offset; status.textContent = total + " 个匹配 · 共 " + data.archived_count + " 个归档会话";
    } catch (error) { if (opened && mine === seq && error.name !== "AbortError") { status.textContent = "读取失败：" + error.message; retry.hidden = false; } }
    finally { if (mine === seq) { busy = false; controller = null; list.setAttribute("aria-busy", "false"); render(); } }
  }
  function open() {
    if (opened) { query.focus(); return; }
    hideSettings(false); App.closeMenus(); if (App.closeSessionMenus) App.closeSessionMenus();
    returnFocus = document.activeElement; opened = true; modal.hidden = false; confirming = null;
    background = Array.from(document.body.children).filter(node => node !== modal && !["SCRIPT", "STYLE", "LINK"].includes(node.tagName)).map(node => { const old = node.inert; node.inert = true; return [node, old]; });
    query.focus({ preventScroll: true }); load(false);
  }
  function close() {
    if (!opened) return;
    opened = false; ++seq; if (controller) controller.abort(); controller = null; clearTimeout(timer); timer = null;
    busy = false; modal.hidden = true; background.forEach(([node, old]) => { node.inert = old; }); background = [];
    if (returnFocus && returnFocus.isConnected) returnFocus.focus({ preventScroll: true });
  }
  function schedule() {
    ++seq; if (controller) controller.abort(); clearTimeout(timer); next = null;
    if (!composing) timer = setTimeout(function () { load(false); }, 250);
  }
  query.addEventListener("input", schedule);
  query.addEventListener("compositionstart", function () { composing = true; schedule(); });
  query.addEventListener("compositionend", function () { composing = false; schedule(); });
  query.addEventListener("keydown", function (event) { if (event.key === "Enter" && !composing && !event.isComposing) { clearTimeout(timer); load(false); } });
  backdrop.addEventListener("click", close);
  modal.addEventListener("keydown", function (event) {
    if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); close(); }
    if (event.key === "Tab") {
      const focusable = Array.from(dialog.querySelectorAll("button:not(:disabled), input:not(:disabled)")).filter(node => !node.hidden && !node.closest("[hidden]"));
      const first = focusable[0], last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
  });
  App.archives = { open, close, setArchived, syncCurrent, noteDeleted: function (id) { rows = rows.filter(row => row.id !== id); if (opened) render(); } };
  syncCurrent();
})(window.App);
