/**
 * 手动压缩：顶栏“压缩对话”入口
 * - 读取上下文统计与摘要预算后二次确认，SSE 流式渲染压缩进度
 * - openConfirmDialog：动态构建确认弹窗（复用 confirm-modal 样式）
 * 依赖：app/core.js、API、FormatUtils；App.*：messages/composer/stats 模块
 */
(function (App) {
  "use strict";
  const {
    state, el, toast, setEmpty,
    scrollToBottom, nearBottom, chatInner
  } = App;

  // ---------- 手动压缩 ----------

  /**
   * 动态构建确认弹窗（复用 .confirm-modal 样式；删除确认是静态专用弹窗）。
   * opts: { title, message, confirmText, cancelText, onConfirm, danger }
   */
  function openConfirmDialog(opts) {
    opts = opts || {};
    const wrap = el("div", "confirm-modal");
    wrap.setAttribute("aria-hidden", "false");
    const backdrop = el("div", "confirm-modal-backdrop");
    const dialog = el("section", "confirm-modal-dialog");
    dialog.setAttribute("role", "dialog");
    dialog.setAttribute("aria-modal", "true");
    dialog.appendChild(el("h2", "", opts.title || "确认操作"));
    dialog.appendChild(el("p", "", opts.message || ""));
    const actions = el("div", "confirm-modal-actions");
    const cancelBtn = el("button", "confirm-cancel", opts.cancelText || "取消");
    const okBtn = el("button", opts.danger === false ? "confirm-primary" : "confirm-danger",
      opts.confirmText || "确认");
    actions.appendChild(cancelBtn);
    actions.appendChild(okBtn);
    dialog.appendChild(actions);
    wrap.appendChild(backdrop);
    wrap.appendChild(dialog);
    document.body.appendChild(wrap);

    function close() { wrap.remove(); }
    cancelBtn.addEventListener("click", close);
    backdrop.addEventListener("click", close);
    okBtn.addEventListener("click", function () {
      close();
      if (typeof opts.onConfirm === "function") opts.onConfirm();
    });
  }

  /**
   * 手动压缩入口：读取上下文信息后用二次确认防止误点击；确认后统一把
   * 所有已完成历史纳入累计摘要，不再把“是否超过阈值”作为执行条件。
   */
  async function startManualCompaction() {
    if (state.manualCompactRunning) {
      toast("已有压缩任务在进行中");
      return;
    }
    if (!state.sessionId) {
      toast("请先发送消息开始会话");
      return;
    }
    if (state.streaming && state.streamingSession === state.sessionId) {
      toast("当前会话正在生成回复，请稍后再试");
      return;
    }
    let threshold = 0;
    let used = 0;
    try {
      const results = await Promise.all([
        API.getContextTokenStats(state.sessionId, null, Array.from(state.selectedTools)),
        API.getHistoryCompactionConfig(),
      ]);
      const stats = results[0] || {};
      const config = results[1] || {};
      const limit = Number(stats.context_token_limit) || 0;
      used = Number(stats.request_context_tokens) || 0;
      // 仅展示参考预算 = 摘要总预算上下文（聊天窗口 × 摘要预算比例，聊天设置中可配）；
      // 手动确认后无论当前占用是否达到该预算，都统一纳入累计摘要
      threshold = Number(config.summary_total_budget);
      if (!Number.isFinite(threshold) || threshold <= 0) {
        const ratio = Number(config.summary_budget_ratio);
        threshold = Number.isFinite(ratio) && ratio > 0 && limit > 0
          ? Math.max(1024, Math.floor(limit * ratio))
          : 0;
      }
      // 上下文消息估算低于摘要预算时压缩无收益：直接提示，不进入确认流程
      // （口径与后端下限守卫一致：仅统计消息部分，工具定义/系统提示不参与压缩）
      const messagesTokens = Number(stats.messages_tokens) || 0;
      if (threshold > 0 && messagesTokens < threshold) {
        toast("当前上下文约 " + FormatUtils.fmtNum(messagesTokens) + " tokens，低于摘要预算约 " +
          FormatUtils.fmtNum(threshold) + " tokens，无需压缩");
        return;
      }
    } catch (err) {
      toast("获取上下文统计失败：" + err.message);
      return;
    }
    openConfirmDialog({
      title: "压缩对话上下文？",
      message: "将使用压缩模型把已完成的会话轮次折叠为累计摘要（原始消息仍保留在历史文件中），" +
        "并建立覆盖全部历史的累计摘要，同时保留最近用户问题索引。当前上下文约 " +
        FormatUtils.fmtNum(used) + " tokens，摘要预算约 " +
        FormatUtils.fmtNum(threshold) + " tokens。确定开始吗？",
      confirmText: "开始压缩",
      danger: false,
      onConfirm: runManualCompactionStream,
    });
  }

  /**
   * 执行手动压缩：POST /chat_context/compact_manual?stream=true 的 SSE 流。
   * 事件结构与自动压缩完全一致（context_compaction start/delta/done，
   * done 带 summary_text），渲染复用 buildCompactionBlock；
   * 结尾的 compaction_manual_result 帧携带 compressed_rounds / stats / error。
   */
  async function runManualCompactionStream() {
    if (state.manualCompactRunning) return;
    state.manualCompactRunning = true;
    // 压缩期间隐藏发送按钮，结束后恢复（send() 内有对应的键盘发送守卫）
    App.refreshComposerButtons();
    let ui = null;
    let sawStart = false;
    scrollToBottom();
    try {
      await API.compactContextStream(state.sessionId, function (evt) {
        if (!evt || evt.type === "done") return;
        const data = evt.data || {};
        if (data.event === "compaction_manual_result") {
          const rounds = Number(data.compressed_rounds) || 0;
          if (data.error) {
            toast("手动压缩失败：" + data.error);
          } else if (rounds > 0) {
            toast("已将 " + rounds + " 个历史轮次纳入累计摘要");
          } else if (!sawStart) {
            toast("没有新的历史轮次需要压缩（当前已是摘要模式）");
          }
          App.refreshSessionUsage(state.sessionId);
          App.scheduleContextTokenStatsRefresh(App.CONTEXT_STATS_EVENT_DEBOUNCE_MS, state.sessionId);
          return;
        }
        if (data.event !== "context_compaction") return;
        if (data.phase === "start") {
          sawStart = true;
          ui = App.buildCompactionBlock({
            scope: "session",
            phase: "start",
            context_summary: data.context_summary || "",
          });
          chatInner.appendChild(ui.wrap);
          setEmpty(false);
        } else if (data.phase === "delta" && ui) {
          ui.appendDelta(data);
        } else if (data.phase === "done") {
          if (ui) ui.update(data);
          else {
            ui = App.buildCompactionBlock(Object.assign({ scope: "session" }, data));
            chatInner.appendChild(ui.wrap);
            sawStart = true;
          }
        }
        if (state.sessionId && nearBottom()) scrollToBottom();
      });
    } catch (err) {
      if (err.name !== "AbortError") toast("手动压缩失败：" + err.message);
      if (ui) ui.update({ phase: "done" });
    } finally {
      state.manualCompactRunning = false;
      App.refreshComposerButtons();
      App.refreshSessionUsage(state.sessionId);
      App.scheduleContextTokenStatsRefresh(App.CONTEXT_STATS_EVENT_DEBOUNCE_MS, state.sessionId);
    }
  }


  // ---------- 导出（供其它模块经 App.* 调用） ----------
  App.startManualCompaction = startManualCompaction;
})(window.App);
