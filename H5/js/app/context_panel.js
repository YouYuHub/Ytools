/** Explicit, read-only local-context inspection; no background polling. */
(function (App) {
  "use strict";
  const node = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
  const button = (label, fn) => { const n = node("button", "context-button", label); n.type = "button"; n.addEventListener("click", fn); return n; };
  const number = n => Number.isFinite(Number(n)) ? Number(n).toLocaleString() : "未知";
  const reasonNames = { auto: "任务开始历史检查", task: "任务内上下文检查", first_call: "首次请求超出窗口",
    manual: "用户手动压缩", post: "任务收尾", history_mutation: "历史编辑或删除后重建" };
  const modes = { raw: "原文", summary: "已摘要", supplement: "摘要＋原文补充", repair_pending: "摘要待重建", active: "当前运行轮次" };
  const phases = { start: "开始", delta: "进行中", done: "完成", aborted: "中止", error: "失败" };
  const remoteStates = { new: "尚未建立", ready: "可接续响应链", request_pending: "请求进行中", recovery_required: "需要恢复确认",
    rebuild_pending: "下次发送时重建", ready_to_rebase: "压缩窗口待启用", compacting: "正在压缩" };
  let opened = false, sid = "", version = 0, controller = null, background = [], returnFocus = null;
  const modal = node("div", "context-panel-modal"); modal.hidden = true;
  const backdrop = node("div", "context-panel-backdrop");
  const dialog = node("section", "context-panel-dialog"); dialog.setAttribute("role", "dialog");
  dialog.setAttribute("aria-modal", "true"); dialog.setAttribute("aria-labelledby", "contextPanelTitle");
  const header = node("header", "context-panel-header"), title = node("h2", "", "上下文管理"); title.id = "contextPanelTitle";
  const headerActions = node("div", "context-panel-actions");
  const refresh = button("刷新", () => load(0)), closeButton = button("关闭", close);
  headerActions.append(refresh, closeButton); header.append(title, headerActions);
  const error = node("p", "context-panel-error"); error.setAttribute("role", "alert");
  const content = node("div", "context-panel-content");
  dialog.append(header, error, content); modal.append(backdrop, dialog); document.body.append(modal);
  backdrop.addEventListener("click", close);

  function section(label, intro) {
    const box = node("section", "context-section"); box.append(node("h3", "", label));
    if (intro) box.append(node("p", "context-hint", intro));
    content.append(box); return box;
  }
  function line(parent, label, value) {
    const row = node("div", "context-kv"); row.append(node("span", "context-hint", label), node("span", "", value)); parent.append(row);
  }
  function operation(label, run) { return button(label, () => { close(); run(); }); }
  function render(snapshot) {
    content.replaceChildren();
    if (!snapshot || snapshot.exists === false) {
      content.append(node("p", "context-hint", "当前会话还没有上下文。发送第一条消息后即可查看。")); return;
    }
    if (!snapshot.inspection) {
      content.append(node("p", "context-alert", "后端尚未提供详细上下文信息，请重启更新后的后端再刷新。")); return;
    }
    const info = snapshot.inspection, model = info.model || {}, policy = info.policy || {};
    const protocol = { chat_completions: "Chat Completions", messages: "Messages", responses: "Responses" }[model.protocol] || model.protocol || "未知接口";
    const overview = section("下一次请求预览", "按已保存配置和本地历史估算；运行中任务使用启动时的配置。");
    overview.append(node("p", "context-model", (model.provider ? model.provider + " / " : "") + (model.name || "未配置模型")));
    overview.append(node("span", "context-tag", protocol + " · " + (model.mode === "stateful" ? "有状态续聊" : "无状态请求")));
    if (info.running) overview.append(node("span", "context-tag", "任务运行中 · 点击刷新查看新快照"));
    const cards = node("div", "context-metrics");
    [["本地上下文估算", snapshot.request_context_tokens], ["聊天模型窗口", snapshot.context_token_limit],
      ["已摘要轮次", (snapshot.rounds || {}).summarized], ["待重建摘要块", info.repair_block_count == null ? (info.summary_blocks || []).filter(b => b.repair_required).length : info.repair_block_count]]
      .forEach(([label, value]) => { const card = node("div", "context-metric"); card.append(node("span", "context-hint", label), node("strong", "", number(value))); cards.append(card); });
    overview.append(cards);
    const limit = Number(snapshot.context_token_limit || 0), used = Number(snapshot.request_context_tokens || 0);
    if (limit > 0) {
      const bar = node("progress", "context-progress"); bar.max = limit; bar.value = Math.min(limit, used);
      bar.setAttribute("aria-label", "本地上下文占聊天窗口比例"); overview.append(bar);
      overview.append(node("p", "context-hint", "约 " + (100 * used / limit).toFixed(1) + "% · 此数字不是累计消费 tokens"));
    }
    line(overview, "消息（摘要、问题索引与原文）", number(snapshot.messages_tokens) + " tokens");
    line(overview, "系统提示词", number(snapshot.system_prompt_tokens) + " tokens");
    line(overview, "工具定义", number(snapshot.tool_definition_tokens) + " tokens");
    line(overview, "文件清单", number(snapshot.file_memory_tokens) + " tokens");
    if (info.repair_required) overview.append(node("p", "context-alert", "存在待重建摘要。下一次聊天会先重建受影响块，当前估算不代表重建后的大小；失败时可以重试。"));

    const policyBox = section("压缩策略", "普通自动压缩按较小模型窗口判断；手动、超窗保护或历史修改重建有各自触发原因。");
    line(policyBox, "聊天与压缩模型的较小窗口", number(policy.effective_window) + " tokens");
    line(policyBox, "自动触发比例 / 阈值", (100 * Number(policy.trigger_ratio || 0)).toFixed(0) + "% / " + number(policy.trigger_threshold) + " tokens");
    line(policyBox, "触发后的历史目标", Number(policy.target_tokens) > 0 ? number(policy.target_tokens) + " tokens" : "未单独指定");
    line(policyBox, "累计摘要预算", number(policy.summary_budget) + " tokens");
    const actions = node("div", "context-panel-actions");
    const compact = operation("手动压缩…", () => App.startManualCompaction());
    compact.disabled = Boolean(App.state.streaming || App.state.manualCompactRunning || !App.state.hasConversation);
    if (compact.disabled) compact.title = "请在当前任务结束后压缩已有会话";
    actions.append(compact, operation("聊天设置", () => App.openChatSettings()), operation("模型与参数", () => App.openModelPanel()));
    policyBox.append(actions);

    const summaries = section("活动摘要", "未受影响的摘要保留；待重建块的旧正文不会继续发送。预览较长时会截短。");
    if (!(info.summary_blocks || []).length) summaries.append(node("p", "context-hint", "当前没有摘要正文；历史如何进入上下文可查看下方轮次列表。"));
    (info.summary_blocks || []).forEach(block => {
      const detail = node("details", "context-summary");
      detail.append(node("summary", "", "摘要 " + block.index + " · " + (block.round_start ? "历史第 " + block.round_start + "–" + block.round_end + " 轮" : "旧格式，未标明轮次") +
        (block.repair_required ? " · 待重建" : " · 可用")));
      detail.append(node("pre", "context-preview", block.preview.text + (block.preview.truncated ? "\n…预览已截短" : ""))); summaries.append(detail);
    });
    if (info.summary_blocks_truncated) summaries.append(node("p", "context-hint", "共有 " + info.summary_block_count + " 块，本页仅展示前 50 块。"));

    const history = section("历史轮次", "历史轮次与后端编辑、删除的编号一致；按最新到最旧排列。点击问题可定位原文。");
    const page = info.round_page || { items: [] };
    page.items.forEach(row => {
      const entry = node("div", "context-round");
      const heading = node("div", "context-round-heading"); heading.append(node("strong", "", "历史第 " + row.round + " 轮"), node("span", "context-tag", modes[row.representation] || row.representation));
      entry.append(heading);
      const jump = button(row.question.text || "（无文字问题）", () => {
        const target = sid; close();
        if (App.jumpToQuoteSource) App.jumpToQuoteSource({ session_id: target, round: row.round, role: "user" }, row.question.text);
      }); jump.classList.add("context-question"); entry.append(jump);
      if (row.question_index) entry.append(node("small", "context-hint", "保留原始问题索引"));
      history.append(entry);
    });
    const pager = node("div", "context-panel-actions");
    if (page.offset > 0) pager.append(button("较新轮次", () => load(Math.max(0, page.offset - page.limit))));
    if (page.has_more) pager.append(button("更早轮次", () => load(page.offset + page.limit)));
    history.append(pager);

    const events = section("最近压缩记录", "历史记录仅供查看，不会重新触发压缩。");
    if (!(info.recent_compactions || []).length) events.append(node("p", "context-hint", "暂无压缩记录。"));
    (info.recent_compactions || []).forEach(event => {
      const item = node("div", "context-event");
      item.append(node("strong", "", (event.event === "responses_compaction" ? "远端原生压缩" : event.scope === "round" ? "当前轮工具轨迹压缩" : "历史压缩") + " · " + (phases[event.phase] || event.phase || "记录")));
      item.append(node("p", "context-hint", (event.timestamp || "") + " · " + (reasonNames[event.trigger_reason] || "旧记录未标注原因")));
      if (event.before_tokens != null || event.after_tokens != null) item.append(node("p", "context-hint", number(event.before_tokens) + " → " + number(event.after_tokens) + " tokens"));
      if (event.trigger_context_tokens != null && event.trigger_threshold != null) item.append(node("p", "context-hint", "触发时上下文 " + number(event.trigger_context_tokens) + " / 阈值 " + number(event.trigger_threshold)));
      if (event.error) item.append(node("p", "context-alert", event.error)); events.append(item);
    });
    renderRemote(info);
    const notes = section("统计口径");
    (info.notes || []).forEach(text => notes.append(node("p", "context-hint", text)));
    notes.append(node("p", "context-hint", "快照时间：" + (info.captured_at ? new Date(info.captured_at).toLocaleString() : "未知")));
  }

  function renderRemote(info) {
    const remote = info.remote || {};
    const box = section("Responses 远端状态");
    if (!remote.applicable) { box.append(node("p", "context-hint", "当前使用无状态请求；每次提供本地有效上下文，不接续远端响应链。")); return; }
    box.append(node("p", "context-hint", "本地摘要大小与远端链占用不同。下列 usage 来自最近已完成响应，不能当作下一次请求的精确占用。"));
    if (remote.error) box.append(node("p", "context-alert", remote.error));
    line(box, "状态", remoteStates[remote.status] || remote.status || "未知");
    line(box, "已有响应链 / 压缩窗口", (remote.has_remote_head ? "有" : "无") + " / " + (remote.has_compacted_window ? "有" : "无"));
    if (remote.compatible === false) box.append(node("p", "context-alert", "模型、账户或模式已变化；旧链不会直接用于当前选择，下次请求重新建立链。"));
    if (remote.message) box.append(node("p", "context-hint", remote.message));
    if (remote.last_usage) {
      line(box, "最近响应输入", number(remote.last_usage.input_tokens) + " tokens");
      line(box, "最近响应输出", number(remote.last_usage.output_tokens) + " tokens");
    }
    line(box, "链体积参考值（不含新增输入）", remote.chain_tokens_estimate == null ? "当前无法可靠估算" : number(remote.chain_tokens_estimate) + " tokens");
    const compacted = remote.compaction || {};
    if (compacted.phase) line(box, "原生压缩 / 启用状态", (phases[compacted.phase] || compacted.phase) + " / " +
      ({ active: "已启用", pending_rebase: "待启用" }[compacted.activation] || "未标明"));
    if (remote.age_risk) box.append(node("p", "context-alert", "远端链已使用较长时间，可以在任务结束后准备从本地重新同步。"));
    const rebuild = button("准备从本地重新同步…", () => {
      const target = sid;
      close(); App.openConfirmDialog({ title: "重新同步上下文", danger: false, confirmText: "准备同步",
        message: "将以当前有效本地历史准备新链，下次发送时生效。当前会话内容保留。",
        onConfirm: async () => {
          if (App.state.sessionId !== target || App.state.streaming || App.state.manualCompactRunning) { App.toast("会话或任务状态已变化，请重新查看"); return; }
          try { await API.rebuildResponsesContext(target, "local"); if (App.state.sessionId === target) { App.toast("已准备，下次发送时同步"); open(); } }
          catch (e) { App.toast("准备同步失败：" + e.message); }
        } });
    });
    rebuild.disabled = Boolean(App.state.streaming || App.state.manualCompactRunning || info.repair_required || remote.error);
    if (rebuild.disabled) rebuild.title = "请先结束当前任务或在下次聊天中完成摘要重建";
    box.append(rebuild);
  }

  async function load(offset) {
    if (!opened || !sid) return;
    const current = ++version, session = sid;
    if (controller) controller.abort(); controller = new AbortController();
    error.textContent = ""; refresh.disabled = true; content.setAttribute("aria-busy", "true");
    try {
      const snapshot = await API.getContextOverview(session, Array.from(App.state.selectedTools || []), offset, controller.signal);
      if (!opened || current !== version || sid !== session || App.state.sessionId !== session) return;
      render(snapshot); content.scrollTop = 0;
    } catch (e) {
      if (opened && current === version && e.name !== "AbortError") error.textContent = "上下文读取失败：" + e.message + "。可以点击刷新重试。";
    } finally {
      if (current === version) { refresh.disabled = false; content.removeAttribute("aria-busy"); }
    }
  }
  function open() {
    if (opened) return;
    opened = true; sid = App.state.sessionId || ""; returnFocus = document.activeElement;
    [App.plusMenu, App.sessionActionsMenu].forEach(menu => { if (menu) menu.classList.add("hidden"); });
    background = Array.from(document.body.children).filter(n => n !== modal && !["SCRIPT", "STYLE", "LINK"].includes(n.tagName))
      .map(n => { const old = n.inert; n.inert = true; return [n, old]; });
    modal.hidden = false; error.textContent = ""; content.replaceChildren(node("p", "context-hint", sid ? "正在读取上下文…" : "请先开始会话，再查看上下文。"));
    refresh.disabled = !sid; closeButton.focus({ preventScroll: true }); if (sid) load(0);
  }
  function close() {
    if (!opened) return;
    opened = false; ++version; if (controller) controller.abort(); controller = null;
    modal.hidden = true; background.forEach(([n, old]) => { n.inert = old; }); background = [];
    if (returnFocus && returnFocus.isConnected) returnFocus.focus({ preventScroll: true });
  }
  modal.addEventListener("keydown", e => {
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(); }
    if (e.key === "Tab") {
      const targets = Array.from(dialog.querySelectorAll("button:not(:disabled),summary")).filter(n => !n.closest("[hidden]"));
      const first = targets[0], last = targets[targets.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  });
  ["contextTokenSummary", "contextManagerItem"].forEach(id => {
    const entry = document.getElementById(id); if (entry) entry.addEventListener("click", open);
  });
  App.contextPanel = { open, close, noteSessionChanged: () => { if (opened && sid !== App.state.sessionId) close(); } };
})(window.App);
