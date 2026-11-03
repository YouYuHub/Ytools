/**
 * 历史记录回放与分段渲染（原 messages.js 16-977 行拆出）
 * 依赖：app/core.js；跨文件调用一律经 App.*（运行时解析，defer 顺序加载保证可用）。
 */
(function (App: any) {
  "use strict";
  const {
    el, state, scrollToBottom, chatScroll, chatInner
  } = App;

  // ---------- 分段渲染 ----------
  // 初始渲染只画末尾 RENDER_TAIL_RECORDS 条记录（最近对话），完整历史的
  // 滚动坐标和可视窗口切换由 history_scroll.js 管理；不逐段回填全部 DOM。
  // 未启用该模块时保留边缘加载条作为回退。分段边界不允许落在
  // 「欠账记录」上（见 isDependentRecord），保证工具调用与结果、压缩
  // start/done 的配对不被切开；record.round 非空时新节点统一打 data-round。
  const RENDER_TAIL_RECORDS = 120;
  const RENDER_OLDER_BATCH = 80;
  // 完整记录保存在内存，DOM 只按需构建。跳转直接切换到目标附近的窗口，
  // 不再从尾部一路回填；滚动、搜索及问题导航共用同一窗口构建入口。
  const loadOlderState: any = { records: null, start: 0, end: 0, loading: false,
    timer: null, button: null, newerButton: null, observer: null };
  // 压缩块展开选择：历史窗口滚动重建 DOM 时保持用户手动折叠/展开，
  // 避免“滚动后自动展开”。key 为“会话:起始记录下标”（start 与其配对的
  // done 共用同一 key），value 为 open 布尔值，上限 500 条。
  const compactionOpenChoices = new Map();
  function compactionChoiceKey(historyIndex: any) {
    if (!Number.isInteger(historyIndex)) return "";
    return (state.sessionId || "") + ":compaction:" + historyIndex;
  }
  function rememberCompactionChoice(historyIndex: any, open: any) {
    const key = compactionChoiceKey(historyIndex);
    if (!key) return;
    compactionOpenChoices.set(key, !!open);
    if (compactionOpenChoices.size > 500) {
      compactionOpenChoices.delete(compactionOpenChoices.keys().next().value);
    }
  }
  function readCompactionChoice(historyIndex: any) {
    const key = compactionChoiceKey(historyIndex);
    if (!key || !compactionOpenChoices.has(key)) return null;
    return compactionOpenChoices.get(key);
  }
  // 同一逻辑压缩的 start/done 共用 key：done 记录向前查找最近的同 scope
  // start（压缩按 scope 顺序配对，与渲染队列一致），找不到则用自身下标。
  function compactionPairStartIndex(records: any, index: any) {
    if (!Array.isArray(records) || !Number.isInteger(index)) return index;
    const rec = records[index];
    if (!rec || rec.kind !== "compaction" || rec.phase === "start" || rec.interrupted) return index;
    const scope = rec.scope === "session" ? "session" : "round";
    for (let i = index - 1; i >= 0; i -= 1) {
      const prev = records[i];
      if (!prev || prev.kind !== "compaction" || prev.interrupted) continue;
      const prevScope = prev.scope === "session" ? "session" : "round";
      if (prevScope !== scope) continue;
      if (prev.phase === "start") return i;
      // 遇到同 scope 的 done/aborted 说明中间已闭合，停止向前查找。
      if (prev.phase === "done" || prev.phase === "aborted") return index;
    }
    return index;
  }

  function resetHistoryPaging() {
    if (loadOlderState.timer != null) window.clearTimeout(loadOlderState.timer);
    if (loadOlderState.observer) loadOlderState.observer.disconnect();
    loadOlderState.timer = null;
    loadOlderState.observer = null;
    loadOlderState.button = null;
    loadOlderState.newerButton = null;
  }

  function historyMatchIndex(records: any, match: any) {
    if (!match) return -1;
    const round = Number(match.round_no || match.round);
    const event = match.event_index;
    const exact = event == null ? -1 : records.findIndex(function(rec: any) {
      return Number(rec.round) === round && (rec.source_event_index === Number(event)
        || (match.agent_id && rec.agent_id === match.agent_id));
    });
    return exact >= 0 ? exact : records.findIndex(function(rec: any) { return Number(rec.round) === round; });
  }

  function renderHistoryWindowAt(index: any, options?: any) {
    const records = loadOlderState.records;
    if (!records || index < 0) return false;
    if (App.historyScroll) App.historyScroll.beforeRender();
    chatInner.replaceChildren();
    renderRecords(records, { historyIndex: index, noScroll: true,
      historyWindowSize:options && options.size, historyLookbehind:options && options.before });
    if (loadOlderState.end === records.length && App.restoreActiveStream) App.restoreActiveStream(state.sessionId);
    App.rebuildQnav();
    return true;
  }

  // 完整任务回放已确认后，撤掉检查点中的当前轮（包括尚未渲染的分页
  // 数据），由 SSE 从任务起点重建。只撤当前轮区间、保留后续轮（插入/重发
  // 等中部轮场景：一刀裁会连后续轮一起删掉），后续节点 data-history-index
  // 整体前移，与拼接后的 records 对齐。
  function prepareActiveRoundReplay(active: any) {
    if (!active || !active.replayComplete || active.sessionId !== state.sessionId) return;
    const records = loadOlderState.records;
    if (!records) return;
    const split = HistoryParser.splitActiveRound(records, active.userText, active.round, active.quotes);
    if (!split) return;
    if (loadOlderState.timer != null) window.clearTimeout(loadOlderState.timer);
    loadOlderState.timer = null;
    const removed = split.cutEnd - split.cutStart;
    loadOlderState.records = split.records;
    // 窗口下标前移：删除区间 [cutStart, cutEnd)，之后的位置整体前移 removed
    function shift(pos: any) { return pos <= split.cutStart ? pos : Math.max(split.cutStart, pos - removed); }
    loadOlderState.start = shift(loadOlderState.start);
    loadOlderState.end = shift(loadOlderState.end);
    Array.from(chatInner.children).forEach(function(node: any) {
      if (node === active.messageNode) return;
      const index = node.getAttribute("data-history-index");
      if (index == null) return;
      const i = Number(index);
      if (i >= split.cutStart && i < split.cutEnd) node.remove();
      else if (i >= split.cutEnd) node.setAttribute("data-history-index", String(i - removed));
    });
    if (loadOlderState.newerButton && loadOlderState.end >= split.records.length) {
      loadOlderState.newerButton.remove();
      loadOlderState.newerButton = null;
    }
    if (loadOlderState.button) loadOlderState.button.classList.toggle("is-hidden", loadOlderState.start === 0);
    active.userNode = null;
  }

  // 欠账记录：渲染它需要其配对记录位于同段更早位置——toolResult 依赖同名
  // tool 调用；压缩 done/aborted 依赖 start（interrupted 中断态独立展示，
  // 不进配对）。段边界切开它们会产生「有结果无调用」的孤儿块
  function isDependentRecord(rec: any) {
    if (!rec) return false;
    if (rec.kind === "toolResult") return true;
    return rec.kind === "compaction" && !rec.interrupted && rec.phase !== "start";
  }

  // 段尾安全消费数量：从 start 起最多 budget 条，段尾若恰好压在欠账记录上
  // 则后移（把欠账记录连着前面的配对拉进本段）。返回本段消费的记录数
  // （注意：此前版本把绝对下标当数量返回，初始切割 start 被算成 0，
  // 表现为打开长会话只显示开头一段、后续内容无加载路径——已修复）
  function pendingQueueSafeEnd(records: any, start: any, budget: any) {
    let end = Math.min(records.length, start + budget);
    while (end < records.length && isDependentRecord(records[end])) end += 1;
    return end - start;
  }

  // 段头安全下标：段头若落在欠账记录上则前移（把欠账记录及其配对调用
  // 拉进本段）。跨段边界的尾部安全由已渲染段的头边界规则保证
  function pendingQueueSafeStart(records: any, start: any) {
    while (start > 0 && isDependentRecord(records[start])) start -= 1;
    return start;
  }

  // 本段渲染后通用收尾：未被结果配对的工具块（分段尾部/历史中断遗留）统一
  // finish 置为完成态；初始渲染把末尾分段固定在屏幕底部（flushRenderedTail
  // 内部处理），并对分段尾与新段头部共同组成的边界闭环 finish
  function finishUnmatchedTools(pendingTools: any) {
    Object.keys(pendingTools).forEach(function(name: any) {
      pendingTools[name].forEach(function(ui: any) { ui.finish(); });
    });
  }

  // 渲染历史记录（工具调用与结果按名称顺序配对）
  // 每条 record 渲染产生的新节点统一打 data-round（1-based 轮次号，
  // 与后端 delete_rounds 的 start_round 同口径），供用户消息编辑/删除定位

  // 单条记录渲染（容器参数化，主渲染与「加载更早消息」分段共用同一实现，
  // 避免双份渲染逻辑漂移）。container 为 chatInner 时行为与历史版本一致；
  // 配对队列 pendingTools/pendingCompactions 由调用方提供（主渲染与分段
  // 回灌各自持有一套，互不串扰）
  function renderOneRecordInto(container: any, rec: any, pendingTools: any, pendingCompactions: any, historyIndex: any) {
    if (rec.kind === "approval") {
      // 审批记录用于审计；可操作按钮由后端当前状态绑定到原工具块。
      // 不再为同一调用创建独立的“执行确认”展示块。
      if (App.permissions && App.permissions.track) App.permissions.track(rec.approval);
      return;
    }
    if (rec.kind === "user") {
      App.appendUserMessage(rec.content, rec.ts, null, null, container, rec.round, rec.quotes,
        rec.source_event_index);
      return;
    }
    if (rec.kind === "think") {
      const think = App.buildThinkBlock({ sessionId: state.sessionId, round: rec.round,
        sourceEvent: rec.source_event_index, historyIndex: historyIndex });
      think.setText(rec.content);
      if (think.done) think.done();
      setStageTimestamp(think.wrap, rec.ts);
      container.appendChild(think.wrap);
      return;
    }
    if (rec.kind === "assistant") {
      appendTimeInto(container, rec.ts);
      const body = el("div", "msg-assistant-body");
      if (Number.isInteger(rec.source_event_index) && rec.source_event_index >= 0) {
        body.dataset.sourceEvent = String(rec.source_event_index);
      }
      body.innerHTML = Markdown.render(rec.content);
      App.highlightCodeBlocks(body);
      container.appendChild(body);
      return;
    }
    if (rec.kind === "tool") {
      const ui = App.buildToolBlock(rec.name, { sessionId: state.sessionId, toolCallId: rec.tool_call_id, round: rec.round, historyIndex: historyIndex }, {deferDetails:true});
      ui.setInput(FormatUtils.prettyJson(rec.args));
      setStageTimestamp(ui.wrap, rec.ts);
      container.appendChild(ui.wrap);
      // ask_user 历史记录附一张可点击的提问卡片：刷新后仍能重新打开回答窗口
      if (rec.name === App.ASK_USER_TOOL_NAME) {
        const askQuestions = App.parseAskQuestionsFromArgs(rec.args);
        if (askQuestions) container.appendChild(App.buildAskBlock(askQuestions));
      }
      (pendingTools[rec.name] = pendingTools[rec.name] || []).push(ui);
      return;
    }
    if (rec.kind === "toolResult") {
      const queue = pendingTools[rec.name];
      const index = queue && rec.tool_call_id ? queue.findIndex((ui: any) => ui.toolCallId === rec.tool_call_id) : 0;
      let ui = queue && index >= 0 ? queue.splice(index, 1)[0] : null;
      if (!ui) {
        ui = App.buildToolBlock(rec.name, { sessionId: state.sessionId, toolCallId: rec.tool_call_id, round: rec.round, historyIndex: historyIndex });
        ui.setInput(FormatUtils.prettyJson(rec.args));
        setStageTimestamp(ui.wrap, rec.ts);
        container.appendChild(ui.wrap);
      }
      App.applyToolResult(ui, rec.name, rec.result, rec.file_diff, rec.args);
      if (Number.isInteger(rec.source_event_index)) ui.wrap.dataset.resultEvent = String(rec.source_event_index);
      ui.finish();
      if (rec.history_detail && App.historyPages) App.historyPages.deferResult(ui,rec);
      return;
    }
    if (rec.kind === "usage") {
      const summary = FormatUtils.roundSummaryText(rec.usage, rec.model);
      if (summary) {
        // 同轮用量行只保留一行：直播行已存在时直接刷新它（落盘数为准），不再另画
        // 一行；同批重复渲染同样幂等（窗口重建/尾部回灌不再堆行）。data-round 立即
        // 打标（后处理也会补打），保证同批内的二次命中能查到。
        if (rec.round != null) {
          const scope: any = container.ownerDocument || document;
          const existing = (container !== scope && container.querySelector
            ? container.querySelector('.round-usage[data-round="' + rec.round + '"]') : null)
            || scope.querySelector('#chatInner .round-usage[data-round="' + rec.round + '"]');
          if (existing) {
            existing._roundUsageSnapshot = rec.usage;
            existing._roundModelSnapshot = rec.model;
            existing.textContent = summary;
            setStageTimestamp(existing, rec.ts);
            return;
          }
        }
        const line = el("div", "round-usage", summary);
        // 附接当前轮 SSE 时可直接收养这行，并保留已落盘的累计值。
        line._roundUsageSnapshot = rec.usage;
        line._roundModelSnapshot = rec.model;
        if (rec.round != null) line.dataset.round = String(rec.round);
        setStageTimestamp(line, rec.ts);
        container.appendChild(line);
      }
      return;
    }
    if (rec.kind === "agentBlock") {
      // 子任务块：任务/计划在头，轨迹按轮次，尾部最终回复引用条
      const agentUi = App.buildSubAgentBlock();
      agentUi.applyEvent({ phase: "start", agent_id: rec.agent_id, round: rec.round, task: rec.task, todo: rec.todo, rounds_limit: rec.rounds_limit,
        timeout_seconds: rec.timeout_seconds });
      if (rec.args) agentUi.setDispatchInput(rec.args);
      agentUi.hydrate(rec);
      if (rec.agent_id) agentUi.wrap.dataset.agentId = rec.agent_id;
      if (rec.done_emitted) agentUi.markReturned(rec);
      setStageTimestamp(agentUi.wrap, rec.started_at);
      container.appendChild(agentUi.wrap);
      return;
    }
    if (rec.kind === "notice") {
      const notice = el("div", "notice-bar", rec.content);
      setStageTimestamp(notice, rec.ts);
      container.appendChild(notice);
      return;
    }
    if (rec.kind === "compaction") {
      // 任务中断遗留的未完成压缩（有 start、无 done）：置为中断态展示，
      // 未覆盖的轮次由后端在下次请求按需重新压缩
      if (rec.interrupted) {
        const interruptedBar = el("div", "compaction-bar compaction-interrupted is-open");
        const head = el("div", "compaction-head");
        head.appendChild(el("span", "compaction-status-icon", "!"));
        head.appendChild(el("span", "compaction-title",
          (rec.scope === "session" ? "跨轮历史" : "本轮工具轨迹") + "压缩已中断，将在下次请求重新执行"));
        interruptedBar.appendChild(head);
        if (rec.content) {
          const body = el("div", "compaction-body");
          const pre = el("pre", "compaction-preview", rec.content);
          body.appendChild(pre);
          interruptedBar.appendChild(body);
        }
        setStageTimestamp(interruptedBar, rec.ts);
        container.appendChild(interruptedBar);
        return;
      }
      const compactionPayload = {
        scope: rec.scope,
        phase: rec.phase,
        invalidated: !!rec.invalidated,
        compress_context: rec.phase === "start" ? rec.content : (rec.compress_context || ""),
        context_summary: rec.phase === "start" ? rec.content : (rec.compress_context || ""),
        // done 记录携带最终摘要全文，刷新后仍可在压缩块中回放
        summary_text: rec.phase === "done" ? (rec.summary_text || "") : "",
        compress_usage: rec.scope === "round" ? rec.usage : null,
        summary_usage: rec.scope === "session" ? rec.usage : null,
        before_tokens: rec.before_tokens,
        after_tokens: rec.after_tokens,
        target_tokens: rec.target_tokens,
        budget_scope: rec.budget_scope,
        token_limit: rec.token_limit,
        trigger_reason: rec.trigger_reason,
        trigger_context_tokens: rec.trigger_context_tokens,
        trigger_threshold: rec.trigger_threshold,
        history_prefix_rounds: rec.history_prefix_rounds,
        batch_source_tokens: rec.batch_source_tokens,
        source_budget: rec.source_budget,
        tail_rounds: rec.tail_rounds,
        output_token_limit: rec.output_token_limit,
        source_was_truncated: rec.source_was_truncated,
        source_was_chunked: rec.source_was_chunked,
        source_chunk_count: rec.source_chunk_count,
        warnings: rec.warnings,
        compress_index: rec.compress_index,
        block_count: rec.block_count,
        error: rec.error || "",
      };
      const scope = compactionPayload.scope === "session" ? "session" : "round";
      const pendingQueue = pendingCompactions[scope];
      const compactionUsage = compactionPayload.compress_usage || compactionPayload.summary_usage;
      const isMergeEvent = compactionUsage && Number(compactionUsage.merge_block_count) > 0;
      // 历史窗口重建保持展开选择：start 与其配对 done 共用起始下标 key。
      const fullRecords = loadOlderState.records;
      const pairIndex = Number.isInteger(historyIndex) && Array.isArray(fullRecords)
        ? compactionPairStartIndex(fullRecords, historyIndex)
        : (Number.isInteger(historyIndex) ? historyIndex : null);
      if (compactionPayload.phase === "aborted") {
        // 失败记录：把此前未闭合的运行块转为失败态（无则直接新建失败块）
        if (pendingQueue && pendingQueue.length) {
          pendingQueue.shift().update({ phase: "aborted", error: compactionPayload.error },
            pairIndex !== null ? { choiceIndex: pairIndex, history: true } : undefined);
          pendingCompactions[scope] = [];
        } else {
          const failedUi = buildCompactionBlock(compactionPayload,
            pairIndex !== null ? { choiceIndex: pairIndex } : undefined);
          setStageTimestamp(failedUi.wrap, rec.ts);
          container.appendChild(failedUi.wrap);
        }
        return;
      }
      if (compactionPayload.phase === "done" && !isMergeEvent && pendingQueue && pendingQueue.length) {
        pendingQueue.shift().update(compactionPayload,
          pairIndex !== null ? { choiceIndex: pairIndex, history: true } : undefined);
        if (!pendingQueue.length) delete pendingCompactions[scope];
        return;
      }
      const compactionUi = buildCompactionBlock(compactionPayload,
        pairIndex !== null ? { choiceIndex: pairIndex } : undefined);
      setStageTimestamp(compactionUi.wrap, rec.ts);
      container.appendChild(compactionUi.wrap);
      if (compactionPayload.phase === "start") {
        (pendingCompactions[scope] = pendingCompactions[scope] || []).push(compactionUi);
      }
      return;
    }
  }

  function renderRecords(records: any, replayState: any) {
    // 渲染上下文（分段渲染状态）：replayState 为 null 表示流式收尾、
    // 编辑重发等对已打开会话的增量回放（记录数少、直接全量渲染，不贴底）；
    // 初始全量渲染走分段并承担贴底/加载更早消息职责
    const ctx: any = replayState === null ? { initial: false } : { initial: true };
    if (replayState) Object.assign(ctx, replayState);
    if (App.processGroups) App.processGroups.indexRecords(ctx.initial ? records : loadOlderState.records || records);
    const pendingTools = {}; // name -> [tool ui]
    const pendingCompactions = {}; // scope -> [尚未完成的压缩 ui]
    function renderOneRecord(rec: any, historyIndex: any) {
      renderOneRecordInto(chatInner, rec, pendingTools, pendingCompactions, historyIndex);
    }

    // 初始全量渲染走分段：只画末尾一段，之前的记录留给「加载更早消息」；
    // 增量渲染（replayState === null）记录数一般很小，直接全量且不贴底。
    const isInitialRender = ctx.initial;
    let slice = records;
    if (isInitialRender) {
      resetHistoryPaging();
      const index = Number.isInteger(ctx.historyIndex) ? ctx.historyIndex : historyMatchIndex(records, ctx.historyMatch);
      let start = pendingQueueSafeStart(records, index >= 0
        ? Math.max(0, index - (Number.isInteger(ctx.historyLookbehind) ? ctx.historyLookbehind : 20)) : Math.max(0, records.length - RENDER_TAIL_RECORDS));
      let end = start + pendingQueueSafeEnd(records, start,
        Math.max(ctx.historyWindowSize || RENDER_TAIL_RECORDS, index >= 0 ? index - start + (ctx.historyWindowSize ? 1 : 40) : 0));
      if (App.processGroups && App.processGroups.windowBounds) {
        const bounds = App.processGroups.windowBounds(records, start, end);
        start = bounds.start; end = bounds.end;
      }
      slice = records.slice(start, end);
      loadOlderState.records = records;
      loadOlderState.start = start;
      loadOlderState.end = end;
      ctx.noScroll = ctx.noScroll || index >= 0;
    }

    let rendered = 0;
    const recordOffset = isInitialRender ? loadOlderState.start : ctx.recordOffset;
    const doRenderBatch = function(batchSize: any) {
      const count = rendered >= slice.length ? 0 : pendingQueueSafeEnd(slice, rendered, batchSize);
      for (let i = 0; i < count; i += 1) {
        const rec = slice[rendered + i];
        const before = chatInner.childNodes.length;
        const historyIndex = Number.isInteger(recordOffset) ? recordOffset + rendered + i : undefined;
        renderOneRecord(rec, historyIndex);
        for (let k = before; k < chatInner.childNodes.length; k += 1) {
          const node = chatInner.childNodes[k];
          if (node.nodeType === 1) {
            if (Number.isInteger(recordOffset)) node.setAttribute("data-history-index", String(recordOffset + rendered + i));
            if (rec.round != null) node.setAttribute("data-round", String(rec.round));
            if (Number.isInteger(rec.source_event_index)) node.setAttribute("data-source-event", String(rec.source_event_index));
            if (App.processGroups) App.processGroups.tagRecord(node, rec);
          }
        }
      }
      rendered += count;
      return count;
    };

    // 段头可能为配对工具结果而向前扩展；选出的尾段必须全部渲染，
    // 否则多出的记录会挤掉末尾的助手回复，刷新后看起来像历史未落盘。
    doRenderBatch(slice.length);

    // 没有等到结果的工具（历史中断）也标记结束
    finishUnmatchedTools(pendingTools);
    // 历史渲染完成后刷新提问卡片可答性（带后续对话的旧提问不可再答）
    App.refreshAskBlockStates();
    if (App.processGroups) App.processGroups.sync(chatInner);
    if (isInitialRender) {
      flushRenderedTail(ctx);
    }
  }

  // 仅在读到边缘或点击时加载，不进行整段历史的后台回填。
  function createLoadOlderButton(newer?: any) {
    const button = el("button", "qnav-load-older");
    button.type = "button";
    button.title = newer ? "加载后续消息" : "加载更早的消息";
    button.setAttribute("aria-label", button.title);
    button.addEventListener("click", function () {
      if (newer) runNewerBatch(); else scheduleBackfill(0);
    });
    if (typeof IntersectionObserver !== "undefined") {
      if (!loadOlderState.observer) loadOlderState.observer = new IntersectionObserver(function(entries: any) {
        entries.forEach(function(entry: any) {
          if (!entry.isIntersecting || !entry.target.isConnected) return;
          if (App.historyScroll && App.historyScroll.active()) return;
          if (entry.target === loadOlderState.newerButton) runNewerBatch();
          else if (entry.target === loadOlderState.button) scheduleBackfill(0);
        });
      }, { root: chatScroll });
      loadOlderState.observer.observe(button);
    }
    return button;
  }

  // 触发一次更早分段的渲染（共享并发锁；细条 loading 态在这里统一管理）。
  // 全部补齐后细条隐藏
  async function runOlderBatch() {
    if (App.isChatScrollbarDragging && App.isChatScrollbarDragging()) return;
    const button = loadOlderState.button;
    if (loadOlderState.loading || !loadOlderState.records) return;
    if (loadOlderState.start <= 0) {
      if (button) button.classList.add("is-hidden");
      return;
    }
    loadOlderState.loading = true;
    if (button) button.classList.add("is-loading");
    try {
      await renderOlderBatch();
    } finally {
      loadOlderState.loading = false;
      if (button) button.classList.remove("is-loading");
      if (loadOlderState.start <= 0 && button) button.classList.add("is-hidden");
    }
  }

  // 合并边缘加载请求；会话切换后丢弃旧调度，不链式回填整段历史。
  function scheduleBackfill(delay: any) {
    if (App.isChatScrollbarDragging && App.isChatScrollbarDragging()) return;
    if (loadOlderState.timer != null) return;
    if (!loadOlderState.records || loadOlderState.start <= 0) {
      const button = loadOlderState.button;
      if (button && loadOlderState.start <= 0) button.classList.add("is-hidden");
      return;
    }
    const myRecords = loadOlderState.records;
    loadOlderState.timer = window.setTimeout(async function () {
      loadOlderState.timer = null;
      if (loadOlderState.records !== myRecords) return; // 已切走
      await runOlderBatch();
    }, delay || 0);
  }

  async function runNewerBatch() {
    if (App.isChatScrollbarDragging && App.isChatScrollbarDragging()) return;
    const paging = loadOlderState;
    const records = paging.records;
    if (paging.loading || !records || paging.end >= records.length) return;
    paging.loading = true;
    try {
      const end = paging.end + pendingQueueSafeEnd(records, paging.end, RENDER_OLDER_BATCH);
      const button = paging.newerButton;
      if (button) button.remove();
      renderRecords(records.slice(paging.end, end), { initial: false, recordOffset: paging.end });
      paging.end = end;
      if (button && end < records.length) chatInner.appendChild(button);
      if (end === records.length && App.restoreActiveStream) App.restoreActiveStream(state.sessionId);
      App.rebuildQnav();
      App.updateCodeblockCopyButtons();
    } finally { paging.loading = false; }
    if (App.historyScroll) App.historyScroll.refresh();
  }

  // 追加更早的一段记录：插到当前聊天流最前面（保持时间顺序），以原首条
  // 消息为锚回拨视口保持阅读位置不跳变；分段边界按 pendingQueueSafeEnd
  // 前置回灌，保证调用/结果配对不被切开
  async function renderOlderBatch() {
    if (!loadOlderState.records) return;
    // 段头回退：落点若是欠账记录（toolResult/压缩 done），连同其配对调用
    // 一起拉进本段（只向更早方向回退，不会越过已渲染区间，无重复渲染）
    const targetStart = pendingQueueSafeStart(
      loadOlderState.records,
      Math.max(0, loadOlderState.start - RENDER_OLDER_BATCH)
    );
    if (targetStart >= loadOlderState.start) return;
    const count = pendingQueueSafeEnd(loadOlderState.records, targetStart, loadOlderState.start - targetStart);
    // 锚点：当前聊天流首条真实消息（跳过顶部的「加载更早消息」细条）
    let anchorNode = chatInner.firstElementChild;
    while (anchorNode && anchorNode.classList.contains("qnav-load-older")) {
      anchorNode = anchorNode.nextElementSibling;
    }
    const anchorTop = anchorNode ? anchorNode.getBoundingClientRect().top : null;
    const pendingTools = {}; // 本段内部配对用（与主渲染队列隔离）
    const pendingCompactions = {};
    const segment = loadOlderState.records.slice(targetStart, targetStart + count);
    const frag = document.createDocumentFragment();
    segment.forEach(function(rec: any, index: any) {
      const before = frag.childNodes.length;
      renderOneRecordInto(frag, rec, pendingTools, pendingCompactions, targetStart + index);
      for (let k = before; k < frag.childNodes.length; k += 1) {
        const node: any = frag.childNodes[k];
        if (node.nodeType === 1) {
          node.setAttribute("data-history-index", String(targetStart + index));
          if (rec.round != null) node.setAttribute("data-round", String(rec.round));
          if (Number.isInteger(rec.source_event_index)) node.setAttribute("data-source-event", String(rec.source_event_index));
          if (App.processGroups) App.processGroups.tagRecord(node, rec);
        }
      }
    });
    // 分段尾部未配对的工具块置完成态
    finishUnmatchedTools(pendingTools);
    chatInner.insertBefore(frag, anchorNode || chatInner.firstChild);
    if (App.processGroups) App.processGroups.sync(chatInner);
    // 分段回灌时用户消息先在离屏 DocumentFragment 中创建，插入后再测量
    // 附件网格宽度，确保引用/媒体/文档方块按当前聊天区宽度排成一到两行。
    if (App.syncMessageAttachmentBlocks) App.syncMessageAttachmentBlocks();
    loadOlderState.start = targetStart;
    if (App.historyScroll) App.historyScroll.refresh({preserve:false});
    // 插入把锚点（及其下方内容）下推：按锚点位移回拨 scrollTop，视口内
    // 阅读位置保持不变；content-visibility 下新内容进入视口附近即真实渲染
    if (anchorNode && anchorNode.parentNode === chatInner) {
      const delta = anchorNode.getBoundingClientRect().top - anchorTop;
      if (Math.abs(delta) > 0.5) {
        const prev = chatScroll.style.scrollBehavior;
        chatScroll.style.scrollBehavior = "auto";
        chatScroll.scrollTop += delta;
        chatScroll.style.scrollBehavior = prev;
      }
    }
    App.rebuildQnav();
    App.updateCodeblockCopyButtons();
    // 回灌段可能包含 ask_user 卡片：与主渲染收尾同口径刷新可答性
    App.refreshAskBlockStates();
  }

  // 初始渲染收尾：插入前后翻阅入口；搜索定位自行滚动，普通打开贴底。
  function flushRenderedTail(ctx: any) {
    if (loadOlderState.start > 0) {
      const button = createLoadOlderButton();
      loadOlderState.button = button;
      chatInner.insertBefore(button, chatInner.firstChild);
    }
    if (loadOlderState.end < loadOlderState.records.length) {
      const button = createLoadOlderButton(true);
      loadOlderState.newerButton = button;
      chatInner.appendChild(button);
    }
    if (App.historyScroll) App.historyScroll.refresh({initial:true});
    if (!ctx.noScroll) scrollToBottom(true);
    if (App.historyPages) App.historyPages.sync();
  }

  function appendTime(ts: any) {
    if (!ts) return;
    chatInner.appendChild(el("div", "msg-time stage-time", FormatUtils.fmtTime(ts)));
  }

  // 时间行（容器参数化版本，renderOneRecordInto 配套）
  function appendTimeInto(container: any, ts: any) {
    if (!ts) return;
    container.appendChild(el("div", "msg-time stage-time", FormatUtils.fmtTime(ts)));
  }

  function setStageTimestamp(node: any, ts: any) {
    if (App.processGroups) App.processGroups.setTimestamp(node, ts);
  }

  // 压缩进度块：与思考/工具块保持一致。开始态可展开查看待压缩内容预览，
  // 并实时追加压缩模型的流式输出（思考过程 / 摘要正文逐帧增量）；
  // 完成态显示最终摘要全文、压缩前后 token、摘要块/轮次和降级信息。
  // SSE delta/done 事件更新同一块；刷新页面后由历史 done 记录的
  // summary_text 重建摘要展示。
  function buildCompactionBlock(compaction: any, opts: any) {
    const scope = compaction.scope === "session" ? "session" : "round";
    const prefixRounds = Number(compaction.history_prefix_rounds);
    const scopeLabel = scope === "session"
      ? (Number.isInteger(prefixRounds) && prefixRounds >= 0 ? "历史前缀（前 " + prefixRounds + " 轮）" : "跨轮历史")
      : "本轮工具轨迹";
    const box = el("div", "compaction-bar");
    const head = el("button", "compaction-head");
    head.type = "button";
    const iconWrap = el("span", "compaction-status-icon");
    const title = el("span", "compaction-title");
    const chevron = el("span", "compaction-chevron");
    chevron.innerHTML = '<svg class="icon" viewBox="0 0 24 24"><path d="m6 9 6 6 6-6"/></svg>';
    head.appendChild(iconWrap);
    head.appendChild(title);
    head.appendChild(chevron);
    const body = el("div", "compaction-body");
    // 历史窗口重建时恢复用户选择，避免滚动后自动展开；实时块无 choiceIndex，
    // 同一 DOM 经 tailNodes 保留，无需持久化。
    let choiceIndex = opts && Number.isInteger(opts.choiceIndex) ? opts.choiceIndex : null;
    let isOpen = compaction.phase !== "done";
    if (choiceIndex !== null) {
      const saved = readCompactionChoice(choiceIndex);
      if (saved !== null) isOpen = saved;
    }
    let latestCompaction = Object.assign({}, compaction);
    let previewText = String(
      latestCompaction.compress_context || latestCompaction.context_summary || ""
    );
    // 运行态累计文本（delta 追加；render 重建 DOM 时据此恢复）
    let reasoningText = "";
    let liveSummaryText = "";
    let latestUsage: any = null;
    // 增量 DOM 引用（render 重建时重置）
    let thinkPre: any = null;
    let summaryPre: any = null;
    head.addEventListener("click", function () {
      if (!body.children.length) return;
      box.classList.toggle("open");
      isOpen = box.classList.contains("open");
      head.setAttribute("aria-expanded", isOpen ? "true" : "false");
      if (choiceIndex !== null) rememberCompactionChoice(choiceIndex, isOpen);
    });
    // 展开态下双击显示区域即可折叠（展开仍走头部按钮）
    body.title = "双击折叠";
    body.addEventListener("dblclick", function () {
      if (!body.children.length) return;
      if (!box.classList.contains("open")) return;
      box.classList.remove("open");
      isOpen = false;
      head.setAttribute("aria-expanded", "false");
      if (choiceIndex !== null) rememberCompactionChoice(choiceIndex, false);
    });
    box.appendChild(head);
    box.appendChild(body);

    function safeNumber(value: any) {
      const number = Number(value);
      return Number.isFinite(number) ? number : null;
    }

    function appendLabeledPre(labelText: any, extraClass: any) {
      body.appendChild(el("div", "compaction-label", labelText));
      const pre = document.createElement("pre");
      pre.className = "compaction-preview" + (extraClass ? " " + extraClass : "");
      body.appendChild(pre);
      return pre;
    }

    // 压缩块内部 pre 的滚动守卫：delta 高频追加时默认贴底，用户上滚阅读即暂停，
    // 滚回底部附近自动恢复。不做会表现为"生成中拖动滚动条立刻被拉回底部"。
    function isNearBottom(pre: any) {
      return pre.scrollHeight - pre.scrollTop - pre.clientHeight < 24;
    }
    function attachScrollGuard(pre: any) {
      if (!pre || pre.__scrollGuarded) return;
      pre.__scrollGuarded = true;
      pre.__stickUser = true;
      pre.addEventListener("wheel", function(e: any) {
        if (e.deltaY < 0) pre.__stickUser = false;
        else if (isNearBottom(pre)) pre.__stickUser = true;
      }, { passive: true });
      pre.addEventListener("scroll", function () {
        // 程序贴底后此事件同样触发，但此时已在底部，判定结果不变（幂等）
        pre.__stickUser = isNearBottom(pre);
      }, { passive: true });
    }
    function scrollToPreBottom(pre: any) {
      if (!pre) return;
      attachScrollGuard(pre);
      if (pre.__stickUser === false) return; // 用户上滚阅读中：不拉回
      pre.scrollTop = pre.scrollHeight;
      pre.scrollLeft = 0;
    }

    /**
     * 追加压缩模型流式增量（phase=delta 事件）：
     * - reasoning_content -> “压缩模型思考”区（弱化色，独立滚动）
     * - content           -> “摘要正文（生成中）”区
     * 增量追加不改变用户设置的展开状态；done 后到达的残留 delta 直接忽略。
     */
    function appendDelta(delta: any) {
      if (!delta || latestCompaction.phase !== "start") return;
      const rc = typeof delta.reasoning_content === "string" ? delta.reasoning_content : "";
      const ct = typeof delta.content === "string" ? delta.content : "";
      if (!rc && !ct) return;
      if (rc) {
        reasoningText += rc;
        if (!thinkPre || !thinkPre.parentNode) {
          thinkPre = appendLabeledPre("压缩模型思考", "compaction-think");
        }
        thinkPre.textContent += rc;
        scrollToPreBottom(thinkPre);
      }
      if (ct) {
        liveSummaryText += ct;
        if (!summaryPre || !summaryPre.parentNode) {
          summaryPre = appendLabeledPre("摘要正文（生成中）", "compaction-summary");
        }
        summaryPre.textContent += ct;
        scrollToPreBottom(summaryPre);
      }
    }

    function render(next: any, updateOpts?: any) {
      // 重连迟到的 start 不能把已完成/中止的同一块重新变成运行态。
      if (next && next.phase === "start" && ["done", "aborted", "ended"].includes(latestCompaction.phase)) return;
      latestCompaction = Object.assign({}, latestCompaction, next || {});
      const isAborted = latestCompaction.phase === "aborted";
      const isEnded = latestCompaction.phase === "ended";
      const nextPhase = ["done", "aborted", "ended"].includes(latestCompaction.phase) ? latestCompaction.phase : "start";
      box.dataset.compactionScope = scope;
      box.dataset.compactionPhase = nextPhase;
      // 历史配对更新（start→done 同段渲染）：默认折叠完成态，避免滚动重建后
      // 自动展开；用户显式选择优先。实时流式更新不传 history，保持当前展开态。
      if (updateOpts && updateOpts.history) {
        if (Number.isInteger(updateOpts.choiceIndex)) choiceIndex = updateOpts.choiceIndex;
        if (choiceIndex !== null) {
          const saved = readCompactionChoice(choiceIndex);
          if (saved !== null) isOpen = saved;
          else if (nextPhase === "done") isOpen = false;
        } else if (nextPhase === "done") {
          isOpen = false;
        }
      }
      const usage = next && (next.compress_usage || next.summary_usage || next.usage);
      const preview = next && (next.compress_context || next.context_summary);
      if (preview) previewText = String(preview);
      if (usage && typeof usage === "object") {
        latestUsage = Object.assign({}, latestUsage || {}, usage);
      }
      thinkPre = null;
      summaryPre = null;
      box.classList.toggle("is-running", nextPhase === "start" && !latestCompaction.invalidated);
      box.classList.toggle("is-done", nextPhase === "done");
      box.classList.toggle("is-failed", isAborted);
      box.classList.toggle("is-invalidated", !!latestCompaction.invalidated);
      iconWrap.innerHTML = nextPhase === "start"
        ? '<svg class="icon compaction-icon" viewBox="0 0 24 24"><path d="M21 12a9 9 0 1 1-6.2-8.56"/></svg>'
        : nextPhase === "aborted"
          ? '<svg class="icon compaction-icon" viewBox="0 0 24 24"><path d="M12 8v5"/><circle cx="12" cy="16.5" r="0.6" fill="currentColor"/><circle cx="12" cy="12" r="9.2"/></svg>'
          : '<svg class="icon compaction-icon" viewBox="0 0 24 24"><path d="M5 4h14M12 4v6m-3-3 3 3 3-3M5 20h14M12 20v-6m-3 3 3-3 3 3"/></svg>';
      title.textContent = latestCompaction.invalidated
        ? "历史压缩记录 · 摘要已失效"
        : nextPhase === "start"
        ? "正在压缩 · " + scopeLabel
        : nextPhase === "aborted"
          ? "压缩失败 · " + scopeLabel
          : isEnded ? "压缩进度已结束 · " + scopeLabel : "上下文压缩完成 · " + scopeLabel;
      body.innerHTML = "";

      if (previewText) {
        body.appendChild(el("div", "compaction-label",
          nextPhase === "start" ? "待压缩内容预览" : "压缩前内容预览"));
        const pre = document.createElement("pre");
        pre.className = "compaction-preview";
        pre.textContent = previewText;
        body.appendChild(pre);
      }

      if (isAborted || isEnded) {
        // 失败态：展示错误信息与已生成部分（若有），原始对话未受影响
        body.appendChild(el("div", "compaction-detail" + (isAborted ? " compaction-error-detail" : ""),
          String(latestCompaction.error || (isEnded ? "未收到本阶段的最终结果，请以已保存的历史记录为准。"
            : "压缩模型调用失败，本次未写入任何摘要；历史对话保持不变，可稍后重试"))));
        if (reasoningText) {
          appendLabeledPre(isEnded ? "已接收的思考" : "已生成的思考（未生效）", "compaction-think").textContent = reasoningText;
        }
        if (liveSummaryText) {
          appendLabeledPre(isEnded ? "已接收的摘要（结果待核实）" : "已生成的摘要（未生效）", "compaction-summary").textContent = liveSummaryText;
        }
        head.setAttribute("aria-expanded", isOpen ? "true" : "false");
        box.classList.toggle("open", isOpen && body.children.length > 0);
        return;
      }

      if (nextPhase === "start") {
        // 运行态：已收到的思考 / 摘要增量原样重建；尚无输出时显示处理中提示
        if (reasoningText) {
          thinkPre = appendLabeledPre("压缩模型思考", "compaction-think");
          thinkPre.textContent = reasoningText;
          scrollToPreBottom(thinkPre);
        }
        if (liveSummaryText) {
          summaryPre = appendLabeledPre("摘要正文（生成中）", "compaction-summary");
          summaryPre.textContent = liveSummaryText;
          scrollToPreBottom(summaryPre);
        }
        if (!reasoningText && !liveSummaryText) {
          body.appendChild(el("div", "compaction-detail", "压缩模型处理中…"));
        }
      } else {
        // 完成态：优先显示 done 事件/历史记录携带的最终摘要全文
        const finalSummary = String(
          latestCompaction.summary_text || liveSummaryText || ""
        ).trim();
        if (finalSummary) {
          appendLabeledPre("摘要正文", "compaction-summary").textContent = finalSummary;
        }
        const usageText = FormatUtils.compactionUsageText(latestUsage);
        if (usageText) body.appendChild(el("div", "compaction-usage", usageText));
        const detail = [];
        const before = safeNumber(latestCompaction.before_tokens != null
          ? latestCompaction.before_tokens : latestUsage && latestUsage.before_tokens);
        const after = safeNumber(latestCompaction.after_tokens != null
          ? latestCompaction.after_tokens : latestUsage && latestUsage.after_tokens);
        if (before !== null && after !== null) {
          // session 压缩的 before/after 为"历史部分"规模（触发判断口径），
          // round 压缩为全量上下文——标签区分，避免与压缩模型输入混淆
          const contextLabel = scope === "session" ? "历史上下文" : "上下文";
          detail.push(contextLabel + " " + FormatUtils.fmtNum(before) + " → " + FormatUtils.fmtNum(after) +
            "（节省 " + FormatUtils.fmtNum(Math.max(0, before - after)) + "）");
        }
        // 触发阈值与触发依据：task/first_call 路径的触发判断是全量上下文
        // （含当前轮轨迹/首调用请求）超阈值，而 before/after 只是历史部分——
        // trigger_context_tokens/trigger_threshold 呈现真实触发比较，消除
        // "历史没到阈值为何压缩"的困惑；auto/manual 路径无这些字段，
        // 此时本批预算即触发阈值，以 before 对照 token_limit 即可
        const tokenLimit = safeNumber(latestCompaction.token_limit);
        const triggerContextTokens = safeNumber(latestCompaction.trigger_context_tokens);
        const triggerThreshold = safeNumber(latestCompaction.trigger_threshold);
        const targetTokens = safeNumber(latestCompaction.target_tokens);
        const budgetScope = latestCompaction.budget_scope
          || (scope === "round" ? "full_request" : "history");
        if (targetTokens !== null && targetTokens > 0) {
          let targetLabel = "历史压缩目标";
          if (budgetScope === "full_request") targetLabel = "完整请求目标";
          else if (budgetScope === "request_history_component") {
            targetLabel = "完整请求目标（本条仅显示历史部分）";
          }
          let targetText = targetLabel + " " + FormatUtils.fmtNum(targetTokens);
          if (budgetScope !== "request_history_component" && after !== null && after > targetTokens) {
            targetText += "（受保留内容或摘要预算下限影响，未完全达到）";
          }
          detail.push(targetText);
        }
        if (triggerThreshold !== null && triggerThreshold > 0) {
          // 强制路径：显示真实触发阈值 + 本批预算（两者不同才有意义）
          detail.push("触发阈值 " + FormatUtils.fmtNum(triggerThreshold));
          if (tokenLimit !== null && tokenLimit > 0 && tokenLimit !== triggerThreshold) {
            detail.push((scope === "session" ? "本批历史预算 " : "本轮压缩预算 ") +
              FormatUtils.fmtNum(tokenLimit));
          }
        } else if (tokenLimit !== null && tokenLimit > 0) {
          const triggered = before !== null && before > tokenLimit;
          const limitLabel = scope === "session"
            ? "历史压缩预算"
            : (triggered ? "触发阈值" : "本轮压缩预算");
          detail.push(limitLabel + " " + FormatUtils.fmtNum(tokenLimit));
        }
        // 触发来源（auto=任务开始检查、task=任务内检查点、
        // first_call=首调用超窗降级、post=任务收尾、manual=手动、
        // history_mutation=历史增删改后重建）：历史回放同样可见
        const triggerReason = latestCompaction.trigger_reason != null
          ? String(latestCompaction.trigger_reason) : "";
        const REASON_LABELS: any = {
          auto: "任务开始历史检查",
          task: "任务内检查点",
          first_call: "首调用超窗降级",
          post: "任务收尾",
          manual: "手动",
          history_mutation: "历史修改后重建",
        };
        if (REASON_LABELS[triggerReason]) {
          let reasonText = "触发来源 " + REASON_LABELS[triggerReason];
          const compareThreshold = triggerThreshold !== null && triggerThreshold > 0
            ? triggerThreshold : tokenLimit;
          if (triggerContextTokens !== null && compareThreshold !== null && compareThreshold > 0) {
            reasonText += "（全量上下文 " + FormatUtils.fmtNum(triggerContextTokens) +
              " > 阈值 " + FormatUtils.fmtNum(compareThreshold) + "）";
          }
          detail.push(reasonText);
        }
        const compressedRounds = safeNumber(latestUsage && latestUsage.compressed_rounds);
        const blockCount = safeNumber(latestCompaction.block_count != null
          ? latestCompaction.block_count : latestUsage && latestUsage.block_count);
        const mergeBlockCount = safeNumber(latestUsage && latestUsage.merge_block_count);
        const compressIndex = safeNumber(latestCompaction.compress_index != null
          ? latestCompaction.compress_index : latestUsage && latestUsage.compress_index);
        if (compressedRounds !== null && compressedRounds > 0) {
          detail.push("压缩旧轮次 " + FormatUtils.fmtNum(compressedRounds));
        }
        // 批次诊断（P2-1）：本批喂入压缩模型的源规模 / 批源预算 / 保留为原始
        // 对话的尾部轮次 / 单段输出上限——用于核对"每批是否吃满、压缩比是否
        // 保真"，而不是只看历史整体 before/after（后者无法反映单批行为）
        const batchSourceTokens = safeNumber(latestCompaction.batch_source_tokens != null
          ? latestCompaction.batch_source_tokens : latestUsage && latestUsage.batch_source_tokens);
        const sourceBudget = safeNumber(latestCompaction.source_budget != null
          ? latestCompaction.source_budget : latestUsage && latestUsage.source_budget);
        const tailRounds = safeNumber(latestCompaction.tail_rounds != null
          ? latestCompaction.tail_rounds : latestUsage && latestUsage.tail_rounds);
        const outputTokenLimit = safeNumber(latestCompaction.output_token_limit != null
          ? latestCompaction.output_token_limit : latestUsage && latestUsage.output_token_limit);
        const sourceChunkCount = safeNumber(latestCompaction.source_chunk_count != null
          ? latestCompaction.source_chunk_count : latestUsage && latestUsage.source_chunk_count);
        const sourceWasChunked = latestCompaction.source_was_chunked
          || (latestUsage && latestUsage.source_was_chunked);
        if (batchSourceTokens !== null && batchSourceTokens > 0) {
          let batchText = "本批压缩源 " + FormatUtils.fmtNum(batchSourceTokens);
          if (sourceBudget !== null && sourceBudget > 0) {
            batchText += " / 预算 " + FormatUtils.fmtNum(sourceBudget);
          }
          if (sourceWasChunked && sourceChunkCount !== null && sourceChunkCount > 1) {
            batchText += " · 完整分段 " + FormatUtils.fmtNum(sourceChunkCount) + " 段";
          }
          if (outputTokenLimit !== null && outputTokenLimit > 0) {
            batchText += " · 摘要上限 " + FormatUtils.fmtNum(outputTokenLimit);
          }
          detail.push(batchText);
        }
        if (tailRounds !== null && tailRounds > 0) {
          detail.push("保留原始对话 " + FormatUtils.fmtNum(tailRounds) + " 轮");
        }
        // 源截断警告：单批源超过压缩模型输入预算时中段被丢弃（保真风险），
        // 显式提示，避免"摘要看起来正常但细节已缺失"的静默降级
        const warnings = latestCompaction.warnings
          || (latestUsage && latestUsage.warnings) || null;
        const truncated = latestCompaction.source_was_truncated
          || (latestUsage && latestUsage.source_was_truncated)
          || (Array.isArray(warnings) && warnings.indexOf("batch_source_truncated") >= 0);
        if (truncated) {
          detail.push("⚠ 本批源超输入预算，已头尾截断（中段未进摘要）");
        }
        if (blockCount !== null && blockCount > 0) {
          detail.push("摘要块 " + FormatUtils.fmtNum(blockCount));
        }
        if (mergeBlockCount !== null && mergeBlockCount > 0) {
          detail.push("合并 " + FormatUtils.fmtNum(mergeBlockCount) + " 个摘要块");
        }
        if (compressIndex !== null && compressIndex > 0) {
          detail.push("已覆盖工具结果 " + FormatUtils.fmtNum(compressIndex) + " 个");
        }
        if (latestUsage && latestUsage.fallback) detail.push("使用降级摘要");
        if (detail.length) body.appendChild(el("div", "compaction-detail", detail.join(" · ")));
        if (!finalSummary && !usageText && !detail.length) {
          body.appendChild(el("div", "compaction-detail", "压缩已完成"));
        }
      }
      head.setAttribute("aria-expanded", isOpen ? "true" : "false");
      box.classList.toggle("open", isOpen && body.children.length > 0);
    }

    render(compaction);
    const ui = {
      wrap: box,
      update: render,
      appendDelta: appendDelta,
      getState: function () { return Object.assign({}, latestCompaction); },
    };
    box._compactionUi = ui;
    return ui;
  }

  // ---------- 导出（供其它模块经 App.* 调用） ----------
  App.renderRecords = renderRecords;
  App.getHistoryWindow = function () { return loadOlderState; };
  App.renderHistoryWindowAt = renderHistoryWindowAt;
  App.prepareActiveRoundReplay = prepareActiveRoundReplay;
  // regen DOM 手术后同步 records：去掉旧轮区间，后续节点 data-history-index
  // 前移。不同步的话，后续任何窗口重建都会按旧 records 把已删旧轮回放回来
  // （暂停后编辑重发的“前端残留”即此因，刷新重拉后消失）。
  App.dropRoundRecords = function (round: any) {
    const records = loadOlderState.records;
    if (!records) return;
    const split = HistoryParser.removeRound(records, round);
    if (!split) return;
    const removed = split.cutEnd - split.cutStart;
    loadOlderState.records = split.records;
    function shift(pos: any) { return pos <= split.cutStart ? pos : Math.max(split.cutStart, pos - removed); }
    loadOlderState.start = shift(loadOlderState.start);
    loadOlderState.end = shift(loadOlderState.end);
    Array.from(chatInner.children).forEach(function (node: any) {
      const index = node.getAttribute("data-history-index");
      if (index == null) return;
      const i = Number(index);
      if (i >= split.cutEnd) node.setAttribute("data-history-index", String(i - removed));
    });
  };
  // 插入 DOM 手术后同步 records：后续轮 round +1（与屏幕重编一致）。
  // records 长度不变，data-history-index 无需动。
  App.renumberRecordsAfter = function (insertAfterRound: any) {
    const records = loadOlderState.records;
    if (!records) return;
    const base = Number(insertAfterRound);
    if (!Number.isFinite(base)) return;
    records.forEach(function (rec: any) {
      if (rec && Number(rec.round) > base) rec.round = Number(rec.round) + 1;
    });
  };
  App.resetHistoryView = function () {
    if (App.historyPages) App.historyPages.reset();
    if (App.historyScroll) App.historyScroll.reset();
    resetHistoryPaging();
    loadOlderState.records = null;
    loadOlderState.start = loadOlderState.end = 0;
  };
  App.historyQuestionAfterRound = function(round: any) {
    const rec = loadOlderState.records && loadOlderState.records.find(function(item: any) {
      return item.kind === "user" && Number(item.round) > round;
    });
    return rec ? { round: rec.round, text: App.contentToPlainText(rec.content) } : null;
  };
  App.showHistoryTail = function () {
    if (loadOlderState.records && loadOlderState.end < loadOlderState.records.length) {
      renderHistoryWindowAt(loadOlderState.records.length - 1);
    }
    if (App.historyScroll && App.historyScroll.revealTail) App.historyScroll.revealTail();
  };
  App.appendTime = appendTime;
  App.buildCompactionBlock = buildCompactionBlock;
  // 以下为 messages.js 拆分新增导出（原模块内直接调用，现跨文件经 App.* 调用）
  // loadOlderState 为 const 对象（仅属性变更）：跨文件共享同一引用
  App.loadOlderState = loadOlderState;
  App.setStageTimestamp = setStageTimestamp;
  App.runOlderBatch = runOlderBatch;
  App.historyMatchIndex = historyMatchIndex;

})((window as unknown as { App: any }).App);
