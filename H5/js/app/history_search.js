/** Search persisted histories in an explicit archive scope. */
(function (App) {
  "use strict";
  const { $, el, state } = App;
  const modal = $("#historySearchModal"), input = $("#historySearchInput");
  const results = $("#historySearchResults"), status = $("#historySearchStatus");
  const more = $("#historySearchMore"), retry = $("#historySearchRetry");
  const internal = $("#historySearchInternal"), filenames = $("#historySearchFilenames");
  const archiveScope = $("#historySearchArchiveScope");
  let composing = false, returnFocus = null, navigating = 0, renderedQuery = "";
  const labels = { user: "用户", assistant: "助手", tool: "工具", sub_agent: "子智能体", compaction: "压缩摘要", session: "会话" };
  const fields = { content: "正文", reasoning_content: "思考", arguments: "工具参数", result: "工具结果", quote: "引用", error: "错误", filename: "文件名", title: "标题", task: "任务", final_reply: "最终回复", summary_text: "压缩摘要", compress_content: "压缩原文", todos: "计划" };

  function excerpt(snippet) {
    const wrap = el("div", "history-search-excerpt");
    const chars = Array.from(snippet.text || ""); // Backend offsets use Unicode code points.
    wrap.appendChild(document.createTextNode((snippet.prefix ? "…" : "") + chars.slice(0, snippet.start).join("")));
    wrap.appendChild(el("mark", "", chars.slice(snippet.start, snippet.end).join("")));
    wrap.appendChild(document.createTextNode(chars.slice(snippet.end).join("") + (snippet.suffix ? "…" : "")));
    return wrap;
  }

  function render(data) {
    const scrollTop = data.query === renderedQuery ? results.scrollTop : 0;
    renderedQuery = data.query;
    const busy = data.status === "pending" || data.status === "loading" || data.status === "more";
    results.setAttribute("aria-busy", String(busy));
    const selectedScope = archiveScope && archiveScope.value || "active";
    const scopeLabel = selectedScope === "archived" ? "已归档会话" : selectedScope === "all" ? "全部会话" : "未归档会话";
    if (data.status === "idle") status.textContent = "搜索" + scopeLabel + "的" + (internal.checked ? "聊天内容、工具与内部记录" : "用户问题、主助手回答正文") + "和标题" + (filenames.checked ? "，包含会话文件名" : "");
    else if (data.status === "pending" || data.status === "loading") status.textContent = "正在搜索…首次搜索可能需要稍候";
    else if (data.status === "error") status.textContent = "搜索失败：" + data.error;
    else status.textContent = data.total ? data.total + " 条匹配 · " + data.session_count + " 个会话" : "没有找到匹配的会话记录";
    if (data.skipped_files || data.skipped_lines) status.textContent += " · 部分正在变化或损坏的记录已跳过，可重试";
    results.replaceChildren();
    (data.results || []).forEach(function (match) {
      const button = el("button", "history-search-hit");
      button.type = "button";
      button.disabled = busy;
      button.appendChild(el("div", "history-search-hit-title", (match.archived ? "[已归档] " : "") + (match.title || match.session_id || "未命名会话")));
      button.appendChild(el("div", "history-search-filename", match.filename || match.session_id + "_chat.jsonl"));
      button.appendChild(el("div", "history-search-hit-meta", (match.round_no ? "第 " + match.round_no + " 轮 · " : "")
        + (labels[match.role] || "记录") + (fields[match.field] ? " · " + fields[match.field] : "")
        + (match.timestamp ? " · " + match.timestamp : "")));
      (match.snippets || []).forEach(function (snippet, index) {
        const node = excerpt(snippet); node.dataset.snippetIndex = String(index); button.appendChild(node);
      });
      button.addEventListener("click", function (event) {
        const snippetNode = event.target.closest("[data-snippet-index]");
        const snippet = snippetNode && match.snippets[Number(snippetNode.dataset.snippetIndex)];
        openMatch(snippet ? Object.assign({}, match, { snippets: [snippet] }) : match, data.query);
      });
      results.appendChild(button);
    });
    results.scrollTop = scrollTop;
    more.hidden = data.next_offset == null;
    more.disabled = busy;
    more.textContent = data.status === "more" ? "加载中…" : "加载更多结果";
    retry.hidden = data.status !== "error";
  }
  const controller = HistorySearch.createController(API.searchHistory, render);
  function updateScope() {
    input.placeholder = (internal.checked ? "搜索会话内容、标题" : "搜索问题、回答正文、标题") + (filenames.checked ? "或文件名…" : "…");
    controller.setScope({ includeInternal: internal.checked, includeFilenames: filenames.checked, archiveScope: archiveScope && archiveScope.value || "active" }, composing);
  }
  internal.addEventListener("change", updateScope);
  filenames.addEventListener("change", updateScope);

  if (archiveScope) archiveScope.addEventListener("change", updateScope);

  function open(options) {
    // 常规入口每次回到未归档，不能沿用从归档窗口打开时遗留的范围。
    if (archiveScope) { archiveScope.value = options && options.archiveScope || "active"; updateScope(); }
    if (options && typeof options.query === "string") input.value = options.query;
    returnFocus = document.activeElement;
    modal.classList.remove("hidden");
    modal.setAttribute("aria-hidden", "false");
    controller.open(input.value);
    input.focus();
    input.select();
  }
  function close() {
    controller.close();
    composing = false;
    modal.classList.add("hidden");
    modal.setAttribute("aria-hidden", "true");
    if (returnFocus && returnFocus.isConnected) returnFocus.focus();
  }
  async function openMatch(match, query) {
    const mine = ++navigating;
    close();
    try {
      if (App.noteSessionArchive && typeof match.archived === "boolean") App.noteSessionArchive(match.session_id, match.archived);
      const opened = await App.openSession(match.session_id, { historyMatch: match });
      if (opened === false || mine !== navigating || state.sessionId !== match.session_id) return;
      if (match.round_no && App.jumpToHistoryMatch) {
        await App.jumpToHistoryMatch(match, query, function () { return mine === navigating && state.sessionId === match.session_id; });
      }
    } catch (error) {
      if (mine === navigating && App.toast) App.toast("定位搜索结果失败：" + (error.message || "请重新搜索"));
    }
  }
  $("#searchBtn").addEventListener("click", open);
  $("#railSearch").addEventListener("click", open);
  $("#historySearchClose").addEventListener("click", close);
  $("#historySearchBackdrop").addEventListener("click", close);
  input.addEventListener("compositionstart", function () { composing = true; controller.update(input.value, true); });
  input.addEventListener("compositionend", function () { composing = false; controller.update(input.value); });
  input.addEventListener("input", function (event) { controller.update(input.value, composing || event.isComposing); });
  input.addEventListener("keydown", function (event) {
    if (event.key === "Enter" && !composing && !event.isComposing) { event.preventDefault(); controller.flush(); }
    if (event.key === "ArrowDown") { const first = results.querySelector("button"); if (first && !first.disabled) { event.preventDefault(); first.focus(); } }
  });
  more.addEventListener("click", controller.more);
  retry.addEventListener("click", controller.flush);
  modal.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && !event.isComposing) { event.preventDefault(); event.stopPropagation(); close(); }
    if (event.key === "Tab") {
      const focusable = Array.from(modal.querySelectorAll("button, input, select")).filter(function (node) { return !node.disabled && !node.hidden; });
      const first = focusable[0], last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
  });
  App.historySearch = { open: open, close: close };
})(window.App);
