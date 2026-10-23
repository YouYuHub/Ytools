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
    scrollToBottom, stickToBottom, nearBottom, chatInner
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

    function close(cancelled) {
      wrap.remove();
      if (cancelled && typeof opts.onCancel === "function") opts.onCancel();
    }
    cancelBtn.addEventListener("click", function () { close(true); });
    backdrop.addEventListener("click", function () { close(true); });
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
        API.getHistoryCompactionConfig(state.sessionId),
      ]);
      const stats = results[0] || {};
      const config = results[1] || {};
      const limit = Number(stats.context_token_limit) || 0;
      used = Number(stats.request_context_tokens) || 0;
      // 摘要预算按当前会话聊天/压缩模型的较小窗口计算，与后端手动压缩一致。
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
   * 后台任务与消费端分离：本地 SSE 中断不再停止后台任务，刷新后可经
   * attachManualCompactionStream(sessionId) 重连收看剩余进度。
   */
  async function runManualCompactionStream() {
    await consumeCompactionStream(state.sessionId, false);
  }

  /**
   * 附接到已有的手动压缩任务（刷新后重连，不二次确认、不重复启动）。
   * 后端无运行中任务时返回 false，调用方回退为普通历史加载。
   */
  async function attachManualCompactionStream(sessionId) {
    return consumeCompactionStream(sessionId, true);
  }

  async function consumeCompactionStream(compactSessionId, attach, reconnects) {
    if (!compactSessionId) return false;
    if (state.manualCompactRunning) return false;
    state.manualCompactRunning = true;
    // 记录发起压缩的会话：终止按钮/发送守卫只作用于该会话，切到其他会话不受影响
    state.manualCompactSession = compactSessionId;
    // 压缩期间隐藏发送按钮、终止按钮接管（点击可中止压缩），结束后恢复
    const controller = new AbortController();
    state.manualCompactAbort = controller;
    App.refreshComposerButtons();
    // 会话状态条同步压缩进度：独立建一条任务行，切走会话也不丢失。
    const taskHandle = App.taskStatus && App.taskStatus.compactBegin
      ? App.taskStatus.compactBegin(compactSessionId)
      : null;
    function noteCompactionEvent(payload) {
      if (taskHandle && App.taskStatus && App.taskStatus.compactEvent) {
        App.taskStatus.compactEvent(taskHandle, payload);
      }
    }
    let ui = null;
    let sawStart = false;
    let sawNativeDone = false;
    let compactError = null;
    let compactStopped = false;
    let attachMissing = false;
    let sawResult = false;
    let reconnect = false;
    let disconnected = false;
    function inView() { return state.sessionId === compactSessionId; }
    function runningBars() {
      if (!inView()) return [];
      return Array.from(chatInner.querySelectorAll('.compaction-bar.is-running[data-compaction-scope="session"]'));
    }
    // 历史窗口只含 start 时，SSE 收到 done 后也要更新内存记录，避免重建恢复运行态。
    // 已有配对终态的落盘记录保持原样；这里不改历史文件。
    function settleHistory(node, data) {
      if (!inView() || !node || !node.hasAttribute("data-history-index")) return;
      const view = App.getHistoryWindow && App.getHistoryWindow();
      const records = view && view.records;
      const index = Number(node.dataset.historyIndex), rec = records && records[index];
      if (!rec || rec.kind !== "compaction" || rec.phase !== "start" || rec.scope !== "session") return;
      const shown = node._compactionUi && node._compactionUi.getState();
      const source = shown && (shown.compress_context || shown.context_summary);
      const savedSource = rec.compress_context || rec.content || rec.context_summary;
      if (source && savedSource && source !== savedSource) return; // 历史快照已替换，旧下标不再对应此块。
      for (let i = index + 1; i < records.length; i++) {
        const next = records[i];
        if (next.kind !== "compaction" || next.scope !== rec.scope) continue;
        if (next.phase === "start") break;
        const usage = next.usage || next.summary_usage;
        if (["done", "aborted"].includes(next.phase) && !(usage && Number(usage.merge_block_count) > 0)) return;
      }
      Object.assign(rec, data, {compress_context: savedSource || "",
        usage: data.summary_usage || data.compress_usage || data.usage || rec.usage});
    }
    function removeRunningBars(terminal) {
      const nodes = runningBars().filter(node => !ui || node !== ui.wrap);
      // 切回时移除的历史占位仍要在终态时同步；引用跟随控制器跨重连保留。
      const aliases = ui ? (ui.historyAliases || (ui.historyAliases = new Set())) : new Set();
      nodes.forEach(node => aliases.add(node));
      if (terminal) { aliases.forEach(node => settleHistory(node, terminal)); aliases.clear(); }
      if (App.historyScroll && App.historyScroll.discardNodes) App.historyScroll.discardNodes(nodes);
      else nodes.forEach(node => node.remove());
      if (nodes.length && App.processGroups && inView()) App.processGroups.sync(chatInner);
    }
    function adoptUi() {
      if (!ui && inView()) {
        const existing = runningBars().reverse().find(node => node._compactionUi);
        if (existing) ui = existing._compactionUi;
      }
      return ui;
    }
    function mountUi() {
      if (ui && inView() && !ui.wrap.isConnected) chatInner.appendChild(ui.wrap);
      if (inView()) setEmpty(false);
    }
    function settleUi(data) {
      adoptUi();
      removeRunningBars(data);
      if (ui) {
        settleHistory(ui.wrap, data);
        ui.update(data);
        mountUi();
      } else if (inView()) {
        ui = App.buildCompactionBlock(Object.assign({scope: "session"}, data));
        mountUi();
      }
      if (App.processGroups && inView()) App.processGroups.sync(chatInner);
    }
    function finishPending(data) {
      // 仅给尚无终态的块收尾，已完成摘要绝不被连接错误覆盖。
      const pending = runningBars();
      if (ui && ui.wrap.classList.contains("is-running") && !pending.includes(ui.wrap)) pending.push(ui.wrap);
      pending.forEach(node => {
        if (node._compactionUi) {
          node._compactionUi.update(data);
          if (node._compactionUi.historyAliases) {
            node._compactionUi.historyAliases.forEach(alias => settleHistory(alias, data));
            node._compactionUi.historyAliases.clear();
          }
        }
        settleHistory(node, data);
      });
      if (App.processGroups && inView()) App.processGroups.sync(chatInner);
    }
    if (!attach) scrollToBottom();
    try {
      const result = await API.compactContextStream(compactSessionId, function (evt) {
        if (!evt || evt.type === "done") return;
        const data = evt.data || {};
        if (data.event === "task_usage") {
          if (App.taskCosts && state.sessionId === compactSessionId) App.taskCosts.event(data);
          return;
        }
        if (sawResult) return; // 业务终态之后的迟到进度不能重新打开运行态。
        // 用户切走后的压缩事件不渲染进其他会话视图；切回后由 done/统计刷新呈现
        const inCompactView = state.sessionId === compactSessionId;
        if (data.event === "responses_compaction") {
          noteCompactionEvent(data);
          if (inCompactView && App.refreshResponsesState) App.refreshResponsesState();
          if (data.phase === "done") sawNativeDone = true;
          if (data.phase === "error") toast("官方压缩未完成：" + (data.error || "请稍后重试"));
          return;
        }
        if (data.warning) { toast(data.warning.message || "压缩部分完成"); return; }

        if (data.event === "compaction_manual_result") {
          sawResult = true;
          const rounds = Number(data.compressed_rounds) || 0;
          if (data.error) {
            compactError = data.error;
            toast("手动压缩失败：" + data.error);
          } else if (rounds > 0) {
            toast("已将 " + rounds + " 个历史轮次纳入累计摘要");
          } else if (sawNativeDone) {
            toast("官方压缩已完成，本地保留现有摘要");
          } else if (!sawStart) {
            toast("没有新的历史轮次需要压缩（当前已是摘要模式）");
          }
          App.refreshSessionUsage(compactSessionId);
          App.scheduleContextTokenStatsRefresh(App.CONTEXT_STATS_EVENT_DEBOUNCE_MS, compactSessionId);
          return;
        }
        if (data.event !== "context_compaction") return;
        noteCompactionEvent(data);
        if (data.phase === "aborted" && !compactError) compactError = data.error || "压缩失败";
        if (data.phase === "start") {
          sawStart = true;
          if (!inCompactView) return;
          adoptUi();
          // 重放/重复 start 复用当前运行块；真正下一批在上一批终态后另建。
          if (ui && ui.wrap.classList.contains("is-running")) ui.update(data);
          else ui = App.buildCompactionBlock(Object.assign({scope: "session"}, data));
          mountUi();
          removeRunningBars();
        } else if (data.phase === "delta") {
          if (!adoptUi()) return;
          if (inCompactView && !ui.wrap.isConnected) removeRunningBars();
          mountUi();
          ui.appendDelta(data);
        } else if (data.phase === "aborted") {
          settleUi(data);
        } else if (data.phase === "done") {
          const usage = data.summary_usage || data.compress_usage;
          if (usage && Number(usage.merge_block_count) > 0) {
            // 合并摘要的独立 done 不占用当前批次的 start。
            if (inCompactView) chatInner.appendChild(App.buildCompactionBlock(data).wrap);
          } else settleUi(data);
          sawStart = true;
        }
        if (inCompactView) stickToBottom();
      }, controller.signal, attach);
      if (!sawResult && !(result && result.completed)) throw new Error("压缩连接提前结束，未收到结束标记");
    } catch (err) {
      const aborted = err.name === "AbortError";
      const status = err && err.status;
      if (sawResult) {
        // 最终业务结果已经确认，随后读取器断线不应把成功结果改成失败。
      } else if (!attach && status === 409 && !reconnects) {
        // 后端已有运行中任务（多半是刷新前启动的）：转入附接收看剩余进度。
        compactError = null;
        reconnect = true;
        toast("已有压缩任务在进行中，正在为你接回进度");
      } else if (attach && status === 404) {
        // 附接时任务已结束（或从未启动）：静默回退，历史即为准。
        attachMissing = true;
        compactError = null;
      } else if (aborted) {
        // 本地消费端中止：唯一调用方是显式停止（stopManualCompactionStream 先调
        // 后端 stop 再 abort），刷新/关闭时旧页面直接卸载不会执行到这里。
        compactStopped = true;
        toast("已终止压缩，已保存的结果保留，可刷新查看");
      } else {
        const message = String(err.message || "");
        // 网络抖动导致 SSE 中断，但后台任务仍在继续：自动重连一次，避免误报失败。
        let reattached = false;
        if (!compactError) {
          try {
            const st = await API.manualCompactionStatus(compactSessionId);
            disconnected = !!(st && st.running);
            reattached = disconnected && !reconnects;
          } catch (_) { disconnected = true; }
        }
        if (reattached) {
          reconnect = true;
          toast("压缩连接中断，正在重连收看剩余进度");
        } else {
          if (!compactError) compactError = message || "连接中断";
          toast("压缩连接中断，请重新打开会话查看后台进度及已保存结果");
        }
      }
    } finally {
      if (!reconnect) {
        finishPending(compactStopped || (compactError && !disconnected && sawResult)
          ? {phase: "aborted", error: compactStopped ? "用户手动终止" : compactError}
          : {phase: "ended", error: disconnected ? "连接已中断，后台任务状态请重新打开会话查看。" : "本次压缩进度已结束，请以已保存的历史结果为准。"});
      }
      if (taskHandle && App.taskStatus && App.taskStatus.compactEnd) {
        App.taskStatus.compactEnd(taskHandle, reconnect || disconnected
          ? {disconnected: true} : attachMissing ? {missing: true} : compactStopped
            ? {stopped: true} : compactError ? {error: compactError} : {});
      }
      if (state.manualCompactAbort === controller) {
        state.manualCompactRunning = false;
        state.manualCompactSession = null;
        state.manualCompactAbort = null;
      }
      App.refreshComposerButtons();
      App.refreshSessionUsage(compactSessionId);
      App.scheduleContextTokenStatsRefresh(App.CONTEXT_STATS_EVENT_DEBOUNCE_MS, compactSessionId);
      // 压缩结束后派发暂存的引导/队列消息（压缩期间发送被禁止，此处补发）
      if (!reconnect && !disconnected) setTimeout(function () { App.flushPendingMessages(compactSessionId); }, 0);
    }
    // 必须等旧请求 finally 收尾后再附接，防止旧请求清掉新请求的运行标志/取消器。
    if (reconnect) return consumeCompactionStream(compactSessionId, true, (reconnects || 0) + 1);
    return !compactError && !attachMissing;
  }

  /** 打开会话后：若该会话后台仍在压缩，自动附接续看剩余进度。 */
  function maybeAttachRunningCompaction(sessionId) {
    if (!sessionId || state.manualCompactRunning) return;
    const sid = String(sessionId);
    API.manualCompactionStatus(sid).then(function (status) {
      if (state.sessionId !== sid || state.manualCompactRunning) return;
      if (status && status.running) {
        toast("检测到该会话有压缩任务在进行中，正在接回进度");
        attachManualCompactionStream(sid);
      }
    }).catch(function () { /* 状态查询失败则跳过续接 */ });
  }

  /**
   * 显式停止手动压缩：先通知后端取消后台任务，再断开本地 SSE。
   * 刷新/断线不再隐式取消，必须经此路径停止。
   */
  async function stopManualCompactionStream() {
    const compactSessionId = state.manualCompactSession || state.sessionId;
    if (!state.manualCompactRunning || state.manualCompactSession !== state.sessionId) return false;
    const controller = state.manualCompactAbort;
    try {
      await API.stopManualCompaction(compactSessionId);
    } catch (err) {
      toast("停止压缩失败： " + (err.message || err));
      return false;
    }
    // 标记由消费端收尾：AbortError 分支据此展示“已终止”而非网络错误。
    if (controller && state.manualCompactAbort === controller) controller.abort();
    return true;
  }


  // ---------- 导出（供其它模块经 App.* 调用） ----------
  App.startManualCompaction = startManualCompaction;
  App.attachManualCompaction = attachManualCompactionStream;
  App.stopManualCompaction = stopManualCompactionStream;
  App.maybeAttachRunningCompaction = maybeAttachRunningCompaction;
  App.openConfirmDialog = openConfirmDialog;
})(window.App);
