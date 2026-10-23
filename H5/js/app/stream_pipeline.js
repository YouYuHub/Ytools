/** Live event rendering shared by send and replay attachment. */
(function (App) {
  "use strict";
  const { state, el, toast, input, chatInner, sessionList, scrollToBottom, nearBottom,
    stickToBottom, setEmpty, sendBtn, stopBtn } = App;

  function createStreamPipeline(msg, sessionId, activeStream, services = {}) {
    const { pickTitlePreview = () => "", requestSessionTitle = () => {},
      scheduleFileChangesBadgeRefresh = () => {}, titlePreviewChars: TITLE_PREVIEW_CHARS = 100,
      fileBadgeTools: FILE_BADGE_TOOLS = [] } = services;
    function ensureLiveContainer() {
      if (state.activeStream !== activeStream || state.sessionId !== sessionId ||
          activeStream.detached || activeStream.completed) return;
      const user = activeStream.userNode;
      if (msg.parentNode === chatInner && !msg.classList.contains("history-live-hidden") &&
          (!user || user.parentNode === chatInner && !user.classList.contains("history-live-hidden"))) return;
      const view = App.getHistoryWindow && App.getHistoryWindow();
      // 阅读旧历史时不抢回视口；尾部窗口被重建后，要把同一个回复容器挂回，
      // 否则 SSE 仍在处理，却只会写入脱离页面的节点，直到刷新才能看到。
      if (view && view.records && view.end < view.records.length) return;
      // 节点仍在 DOM 中也可能残留旧窗口的隐藏标记；补回提问时维持先问后答。
      if (user) {
        user.classList.remove("history-live-hidden");
        if (user.parentNode !== chatInner) chatInner.insertBefore(user, msg.parentNode === chatInner ? msg : null);
      }
      msg.classList.remove("history-live-hidden");
      if (msg.parentNode !== chatInner) chatInner.appendChild(msg);
      App.rebuildQnav();
    }
    let curThink = null;
    let curAnswer = null;
    let stageTimestamp = new Date().toISOString();
    let answerText = "";
    let hasStage = false;
    // 主回答流式渲染节流：deepseek 等高速模型的逐帧 delta 频率远超渲染所需，
    // 每帧全量 Markdown.render + Prism 高亮 + innerHTML 重建是 O(n²) 热路径
    // （长回复+长思考时页面越来越卡）。60ms 合并一次渲染（≈16fps，视觉上
    // 仍是连续打字），收尾/分块 seal 时强制 flush 一次保证内容完整。
    let answerRenderTimer = null;
    function flushAnswerRender() {
      if (!answerRenderTimer) return;
      clearTimeout(answerRenderTimer);
      answerRenderTimer = null;
      if (curAnswer && answerText) {
        App.renderPreservingWidgets(curAnswer, Markdown.render(answerText));
        App.highlightCodeBlocks(curAnswer);
        App.updateCodeblockCopyButtons();
      }
    }
    function scheduleAnswerRender() {
      if (answerRenderTimer) return;
      answerRenderTimer = setTimeout(flushAnswerRender, 60);
    }
    const toolBlocks = [];
    let activeToolBlocks = null;
    // 子任务块（sub_agent）：按父级 tool_call id / agent_id 双键索引，同一块
    // 对象同时登记（tool_start 预建 → start 事件续用 → tool_return 收尾）
    const agentByCall = new Map();
    const agentById = new Map();
    const agentBlocksAll = [];
    const activeCompactions = {}; // scope -> [尚未完成的压缩 ui]
    let usageLine = null;
    // 一个模型 completion 可能在多个 SSE 帧重复携带 usage（甚至是逐步增长的快照）。
    // 按 completion id 保留最新快照，不能按每个 usage 对象直接相加，否则会把同一
    // 次请求重复累计成百万级 token。
    const usageByCompletion = new Map();
    const usageWithoutId = new Map();
    let roundUsageAcc = null;
    let roundUsageBaseline = null;
    let roundUsageAbsolute = null;
    let hasAuthoritativeRoundUsage = false;
    let roundModel = activeStream.model || null;
    // 标题生成（前端驱动）：流内累计思考/正文 delta，达 TITLE_PREVIEW_CHARS
    // 时由 maybeTriggerTitle 发起一次标题请求（聊天主链路不再感知标题任务）
    let titleReasoning = "";
    let titleContent = "";
    let titleTriggered = false;

    // 附接回放去重窗口：replay marker（带 replay_end_supported）打开、
    // replay_end 帧关闭。窗口内的正文/思考先暂存、seal 时与同轮历史节点
    // 比对（命中则丢弃）；工具按 callId 直接收养历史卡。窗口外一切照旧，
    // 绝不误删正常重复内容。无轮次号/缺 helper 时退化为普通路径。
    let replayWindow = false;
    let stagingAnswer = null; // { el, text } 未挂载的暂存正文块
    let stagingThink = null; // 未建块的暂存思考文本
    const replayDedup = typeof StreamReplayDedup !== "undefined" ? StreamReplayDedup : null;
    const replayTextMatcher = replayDedup ? replayDedup.createSequentialMatcher() : null;
    const replayThinkMatcher = replayDedup ? replayDedup.createSequentialMatcher() : null;
    function replayRound() {
      const round = activeStream && activeStream.round;
      return typeof round === "number" && round > 0 ? round : null;
    }
    function stagingActive() {
      return replayWindow && replayRound() != null && replayDedup != null;
    }
    // 同 callId 的历史工具卡收养：复用其 ui 控制器（结果/审批归位到原卡），
    // 不在 DOM 新建重复卡。历史没有（被裁剪过）则返回 null 走正常新建。
    function adoptHistoryToolBlock(callId) {
      if (!callId || !replayDedup) return null;
      const wrap = replayDedup.findHistoryToolWrap(chatInner, msg, callId);
      const ui = wrap && wrap._toolBlockUi;
      if (!wrap || !ui) return null;
      const adopted = toolBlocks.find(t => t.ui === ui);
      if (adopted) return adopted;
      const tb = { ui: ui, name: "", callId: callId, argsText: "", done: false, adopted: true };
      toolBlocks.push(tb);
      return tb;
    }
    function matchHistoryText(source) {
      const round = replayRound();
      if (round == null || !replayTextMatcher) return null;
      const candidates = replayDedup.collectHistoryTexts(chatInner, msg, round);
      return replayTextMatcher.match(candidates, function (node) {
        return replayDedup.renderedSourceEquals(Markdown, el, source, node.textContent);
      });
    }
    function matchHistoryThink(source) {
      const round = replayRound();
      if (round == null || !replayThinkMatcher) return null;
      const candidates = replayDedup.collectHistoryThinks(chatInner, msg, round);
      const wanted = String(source || "").trim();
      return replayThinkMatcher.match(candidates, function (node) {
        return (node.textContent || "").trim() === wanted;
      });
    }
    function finalizeThinkStaging() {
      if (stagingThink == null) return;
      const text = stagingThink;
      stagingThink = null;
      if (!matchHistoryThink(text)) {
        const ui = App.buildThinkBlock();
        ui.setText(text);
        ui.done();
        hasStage = true;
        appendStage(ui.wrap);
      }
    }
    function finalizeAnswerStaging() {
      if (!stagingAnswer) return;
      const staged = stagingAnswer;
      stagingAnswer = null;
      if (!matchHistoryText(staged.text)) {
        hasStage = true;
        appendStage(staged.el);
        staged.el.classList.remove("msg-cursor");
        App.renderPreservingWidgets(staged.el, Markdown.render(staged.text));
        App.highlightCodeBlocks(staged.el);
        App.updateCodeblockCopyButtons();
      }
    }
    function finalizeStagingAsReplay() {
      finalizeThinkStaging();
      finalizeAnswerStaging();
    }
    function flushStagingAsLive() {
      // 窗口关闭时还有未 seal 的暂存（回放中途转实时，如正文流到一半）：
      // 直接挂载并转为实时块继续追加，不丢内容。
      if (stagingThink != null) {
        curThink = App.buildThinkBlock();
        curThink.setText(stagingThink);
        curThink.streaming();
        stagingThink = null;
        hasStage = true;
        appendStage(curThink.wrap);
      }
      if (stagingAnswer) {
        curAnswer = stagingAnswer.el;
        answerText = stagingAnswer.text;
        stagingAnswer = null;
        hasStage = true;
        appendStage(curAnswer);
        scheduleAnswerRender();
      }
    }
    function setReplayWindow(open) {
      const next = Boolean(open);
      if (replayWindow && !next) flushStagingAsLive();
      replayWindow = next;
    }

    function sealTextBlocks() {
      finalizeStagingAsReplay();
      if (curThink) curThink.done();
      if (curAnswer) curAnswer.classList.remove("msg-cursor");
      flushAnswerRender();
      curThink = null;
      curAnswer = null;
    }

    function appendStage(node) {
      if (App.processGroups) {
        const timestamp = node.dataset.stageTimestamp || stageTimestamp;
        if (node.matches(".msg-assistant-body")) {
          const time = el("div", "msg-time stage-time", FormatUtils.fmtTime(timestamp));
          msg.appendChild(time);
        }
        App.processGroups.setTimestamp(node, timestamp);
      }
      msg.appendChild(node);
      if (usageLine && usageLine.textContent) msg.appendChild(usageLine);
      if (App.processGroups) App.processGroups.sync(msg);
    }

    function ensureUsageLine() {
      // 刷新后先回放 JSONL，再附接仍在运行的 SSE。历史回放已为本轮画过
      // usage 行；收养它并继续更新，避免附接流在新 msg 中再画一行。
      const round = activeStream.round;
      if (Number.isInteger(round) && round > 0 && state.sessionId === sessionId && msg.parentNode === chatInner) {
        const historical = chatInner.querySelector('.round-usage[data-round="' + round + '"]');
        if (historical && historical !== usageLine) {
          if (usageLine) usageLine.remove();
          usageLine = historical;
          retainUsageBaseline(historical._roundUsageSnapshot);
          if (!roundModel && historical._roundModelSnapshot) roundModel = historical._roundModelSnapshot;
        }
      }
      if (!usageLine) usageLine = el("div", "round-usage");
      if (Number.isInteger(round) && round > 0) usageLine.dataset.round = String(round);
      return usageLine;
    }

    function renderRoundSummary() {
      const line = ensureUsageLine();
      line._roundUsageSnapshot = displayRoundUsage();
      line._roundModelSnapshot = roundModel;
      const summary = FormatUtils.roundSummaryText(line._roundUsageSnapshot, line._roundModelSnapshot);
      if (!summary) {
        // 编辑重发附接时，JSONL 可能仍保留旧轮的 usage 行；marker 的
        // 空绝对值明确表示新轮尚无用量，不能继续显示旧数字。
        if (hasAuthoritativeRoundUsage) {
          line.textContent = "";
          line.remove();
        }
        return;
      }
      line.textContent = summary;
      msg.appendChild(line);
      if (App.processGroups) App.processGroups.sync(msg);
    }

    function usageTokenCount(usage) {
      if (!usage || typeof usage !== "object") return 0;
      const total = Number(usage.total_tokens);
      if (Number.isFinite(total) && total >= 0) return total;
      return (Number(usage.prompt_tokens) || 0) + (Number(usage.completion_tokens) || 0);
    }

    function retainUsageBaseline(usage) {
      if (!usage || typeof usage !== "object") return;
      if (!roundUsageBaseline || usageTokenCount(usage) >= usageTokenCount(roundUsageBaseline)) {
        roundUsageBaseline = usage;
      }
    }

    function displayRoundUsage() {
      // 新后端发送的是本轮绝对累计值；它比逐条 SSE usage 重算可靠，
      // 尤其是刷新后环形回放缓冲已丢失早期 completion 的时候。
      if (hasAuthoritativeRoundUsage) return roundUsageAbsolute;
      return roundUsageBaseline && usageTokenCount(roundUsageBaseline) > usageTokenCount(roundUsageAcc)
        ? roundUsageBaseline : roundUsageAcc;
    }

    function setRoundUsageBaseline(usage) {
      if (!usage || typeof usage !== "object") return;
      hasAuthoritativeRoundUsage = true;
      // 附接 marker 先给当前快照，回放缓冲随后可能送来更早的绝对值；
      // 只接受不小于当前值的快照。历史行可能属于尚未提交的编辑旧轮，
      // 有服务端绝对值时不再以历史数字作为下限。
      if (!roundUsageAbsolute || usageTokenCount(usage) >= usageTokenCount(roundUsageAbsolute)) {
        roundUsageAbsolute = usage;
      }
      activeStream.usage = displayRoundUsage();
      renderRoundSummary();
    }

    function accumulateUsage(target, delta) {
      ["prompt_tokens", "completion_tokens", "total_tokens"].forEach(function (key) {
        const value = Number(delta[key] || 0);
        if (value) target[key] = (target[key] || 0) + value;
      });
    }

    function rebuildRoundUsage() {
      const total = {};
      usageByCompletion.forEach(function (usage) {
        accumulateUsage(total, usage);
      });
      usageWithoutId.forEach(function (usage) {
        accumulateUsage(total, usage);
      });
      return total;
    }

    function onRoundUsage(usage, completionId) {
      if (!usage || typeof usage !== "object") return false;
      const fingerprint = JSON.stringify(usage);
      const id = completionId != null && String(completionId).trim()
        ? String(completionId)
        : "";
      const usageKey = id || "no_id:" + fingerprint;
      if (id) {
        const previous = usageByCompletion.get(id);
        if (previous && JSON.stringify(previous) === fingerprint) return false;
        // 相同 completion id 的 usage 是累计快照，更新而不是再次相加。
        usageByCompletion.set(id, usage);
      } else {
        if (usageWithoutId.has(usageKey)) return false;
        usageWithoutId.set(usageKey, usage);
      }
      roundUsageAcc = rebuildRoundUsage();
      activeStream.usage = displayRoundUsage();
      renderRoundSummary();
      return true;
    }

    // 更新本轮展示模型：同步写入管线内部与 activeStream（切走再切回会话时
    // 复用），并立即显示/刷新轮次摘要。服务端不提供 usage 时只显示模型名。
    // 唯一下发通道为 round_model 帧（round_started 帧不再携带 model）；
    // 附接回放走 marker.model → 同一入口。
    function setRoundModel(model) {
      if (!model || typeof model.name !== "string" || !model.name.trim()) return false;
      roundModel = model;
      activeStream.model = model;
      renderRoundSummary();
      return true;
    }

    // ---------- 子任务块（sub_agent）事件路由 ----------
    // 同一块对象双键登记：父级 tool_start 只带 tool_call id（预建占位），
    // 子事件带 agent_id（start 到达后绑定）。done 后保留在索引中，
    // tool_return(sub_agent) 到达时据此追加"最终回复已返回父智能体"引用条。
    function bindAgentCall(block, callId) {
      if (callId) {
        block.callId = callId;
        agentByCall.set(callId, block);
      }
    }

    function promoteAgentTool(tb, callId) {
      let block = tb.agentBlock || (callId && agentByCall.get(callId));
      if (!block) {
        block = { ui: App.buildSubAgentBlock(sessionId), callId: "", agentId: "", done: false };
        agentBlocksAll.push(block);
        if (tb.ui.wrap.parentNode) {
          if (App.processGroups) App.processGroups.setTimestamp(block.ui.wrap, tb.ui.wrap.dataset.stageTimestamp || stageTimestamp);
          tb.ui.wrap.parentNode.replaceChild(block.ui.wrap, tb.ui.wrap);
        }
        else appendStage(block.ui.wrap);
      } else if (tb.ui.wrap.parentNode) {
        tb.ui.wrap.parentNode.removeChild(tb.ui.wrap);
      }
      tb.agentBlock = block;
      bindAgentCall(block, callId);
      block.ui.setDispatchInput(tb.argsText || "");
      return block;
    }

    function handleSubAgentEvent(data) {
      let block = null;
      if (data.agent_id && agentById.has(data.agent_id)) block = agentById.get(data.agent_id);
      if (!block && data.parent_tool_call_id && agentByCall.has(data.parent_tool_call_id)) {
        block = agentByCall.get(data.parent_tool_call_id);
      }
      if (!block) {
        block = { ui: App.buildSubAgentBlock(sessionId), callId: "", agentId: "", done: false };
        agentBlocksAll.push(block);
        hasStage = true;
        appendStage(block.ui.wrap);
      }
      if (data.parent_tool_call_id && !block.callId) {
        block.callId = data.parent_tool_call_id;
        agentByCall.set(data.parent_tool_call_id, block);
      } else if (data.parent_tool_call_id && !agentByCall.has(data.parent_tool_call_id)) {
        agentByCall.set(data.parent_tool_call_id, block);
      }
      if (data.agent_id && !block.agentId) {
        block.agentId = data.agent_id;
        agentById.set(data.agent_id, block);
      }
      block.ui.applyEvent(data);
      if (data.phase === "done") block.done = true;
      if (state.sessionId === sessionId && data.phase !== "heartbeat" && data.phase !== "progress") stickToBottom();
    }

    function handle(evt) {
      if (App.taskStatus) App.taskStatus.event(activeStream.taskHandle, evt);
      if (evt.type === "done") return;
      ensureLiveContainer();
      const data = evt.data || {};
      stageTimestamp = data.timestamp || new Date().toISOString();
      if (data.event === "task_usage") {
        if (App.taskCosts && state.sessionId === sessionId) App.taskCosts.event(data);
        return;
      }

      if (data.round_usage_total && typeof data.round_usage_total === "object") {
        setRoundUsageBaseline(data.round_usage_total);
      }

      // 本轮生效聊天模型：唯一下发通道为 round_model 帧（三条发送路径均推送；
      // round_started 帧只带轮次号，不再冗余携带 model）
      if (data.round_model) setRoundModel(data.round_model);

      // 轮次开始：后端任务启动后推送本轮最终轮次号（普通发送），前端就地
      // 补挂本轮提问气泡的编辑/复制/删除入口（此前收尾不重载就没有入口）；
      // 同时同步到 activeStream.round——中途切走再切回会话时，openSession
      // 的历史裁剪/去重按轮次精确匹配（编辑重发路径 send() 已提前打标）
      if (data.round_started && typeof data.round_started.round === "number") {
        if (activeStream.round == null) activeStream.round = data.round_started.round;
        if (usageLine) ensureUsageLine();
        App.attachLiveRoundEntry(activeStream, data.round_started.round);
        if (state.sessionId === sessionId) App.rebuildQnav();
      }

      // 空增量、心跳、签名活动和 usage 快照不构成文本阶段边界。
      // 只在正文、有效工具调用或 completion 收尾时结束思考，避免一段思考
      // 被这些控制帧拆成多个折叠框（两种模型协议共用此管线）。

      // 压缩事件不是普通文本/工具事件，单独渲染为可展开的状态块，
      // 并在完成后刷新一次上下文统计。兼容旧版后端的嵌套 payload。
      if (data.event === "tool_approval") {
        if (App.permissions) App.permissions.receive(data, sessionId);
        const approval = data.approval;
        if (data.phase === "requested" && approval && approval.agent_id !== "preview") {
          sealTextBlocks();
          if (approval.agent_id === "main") {
            let tb = toolBlocks.find(t => t.callId === approval.tool_call_id);
            if (!tb && activeToolBlocks && approval.tool_index != null) {
              const candidate = activeToolBlocks.get(approval.tool_index);
              if (candidate && !candidate.done && (!candidate.callId || candidate.callId === approval.tool_call_id)) tb = candidate;
            }
            // 兼容没有调用 ID 的旧模型增量；认领本轮未绑定的声明块。
            if (!tb) tb = toolBlocks.find(t => !t.done && !t.callId && t.name === approval.tool);
            if (!tb && approval.tool_call_id) tb = adoptHistoryToolBlock(approval.tool_call_id);
            if (!tb) {
              tb = { ui: App.buildToolBlock(approval.tool, { sessionId: sessionId }), name: approval.tool, callId: approval.tool_call_id, done: false };
              toolBlocks.push(tb); hasStage = true; appendStage(tb.ui.wrap);
            }
            tb.callId = approval.tool_call_id;
            tb.name = approval.tool; tb.ui.setName(approval.tool);
            if (tb.ui.bindPermission) tb.ui.bindPermission({ sessionId: sessionId, toolCallId: approval.tool_call_id, round: approval.round, taskId: approval.task_id });
          } else {
            let block = agentById.get(approval.agent_id) || agentByCall.get(approval.parent_tool_call_id);
            if (!block) {
              block = { ui: App.buildSubAgentBlock(sessionId), callId: approval.parent_tool_call_id || "", agentId: approval.agent_id, done: false };
              agentBlocksAll.push(block); hasStage = true; appendStage(block.ui.wrap);
              if (block.callId) agentByCall.set(block.callId, block);
            }
            agentById.set(approval.agent_id, block);
            if (block.ui.applyApproval) block.ui.applyApproval(approval);
          }
        }
        return;
      }
      if (data.event === "todo") {
        // 模型通过 todo_write 更新了任务计划
        App.applySessionTodo(data.todos);
        return;
      }
      if (data.event === "responses_compaction") {
        if (state.sessionId === sessionId && App.refreshResponsesState) App.refreshResponsesState();
        return;
      }
      if (data.event === "message_injected") {
        // 运行中注入的用户消息（消息引导）在检查点被消费：此刻消息节点
        // 末尾恰好在当前轮工具结果之后，在此插入气泡与后端 messages
        // 顺序一致；同时清除消息框上方的待注入提示
        sealTextBlocks();
        App.clearInjectedPending(sessionId, data.message_id);
        App.appendUserMessage(
          data.content || (typeof data.text === "string" ? data.text : ""),
          null,
          sessionId,
          null,
          msg,
          null,
          data.quotes || []
        );
        hasStage = true;
        App.rebuildQnav();
        if (state.sessionId === sessionId) stickToBottom();
        return;
      }
      if (data.event === "ask_user") {
        // 模型通过 ask_user 向用户提问：流内渲染静态卡片，并弹出交互窗口；
        // 仅在当前正在浏览该会话时弹窗（后台会话切换回来后可点击卡片打开）
        sealTextBlocks();
        const questions = Array.isArray(data.questions) ? data.questions : [];
        const askCard = App.buildAskBlock(questions);
        hasStage = true;
        appendStage(askCard);
        App.refreshAskBlockStates();
        if (!replayWindow && state.sessionId === sessionId) App.openAskModal(questions, askCard);
        return;
      }
      if (data.event === "sub_agent") {
        if (data.phase === "tool_result" && App.terminalJobs && App.terminalJobs.noteToolResult) {
          App.terminalJobs.noteToolResult(sessionId, data.tool_name || data.function_name);
        }
        // 子任务事件：按 agent_id/父级 tool_call id 聚合到独立块
        //（思考/正文/工具轨迹/todo 都在块内，不进入父级通用渲染管线）
        handleSubAgentEvent(data);
        return;
      }
      const rawCompaction = data.event === "context_compaction"
        ? data
        : data.context_compaction;
      const compaction = rawCompaction && rawCompaction.phase
        ? rawCompaction
        : rawCompaction
          ? Object.assign({}, rawCompaction, {
            phase: "done",
            compress_usage: rawCompaction.compress_usage || rawCompaction.usage || null,
          })
          : null;
      if (compaction && typeof compaction === "object") {
        sealTextBlocks();
        const scope = compaction.scope === "session" ? "session" : "round";
        const activeQueue = activeCompactions[scope];
        // 压缩模型流式增量（思考/摘要正文）：追加到当前活动的压缩块实时渲染；
        // 无活动块时忽略（如重连回放中 done 已消费、残留 delta 迟到的场景）。
        if (compaction.phase === "delta") {
          const activeUi = activeQueue && activeQueue.length
            ? activeQueue[activeQueue.length - 1]
            : null;
          if (activeUi) {
            activeUi.appendDelta(compaction);
            hasStage = true;
            if (state.sessionId === sessionId) stickToBottom();
          }
          return;
        }
        const compactionUsage = compaction.compress_usage || compaction.summary_usage;
        const isMergeEvent = compactionUsage && Number(compactionUsage.merge_block_count) > 0;
        if (compaction.phase === "aborted") {
          // 压缩失败：把该 scope 队列中最早的运行块转为失败态并清空队列
          // （失败不落盘摘要，原始对话不受影响）
          const queue = activeCompactions[scope] || [];
          if (queue.length) {
            queue.shift().update({ phase: "aborted", error: compaction.error || "" });
            activeCompactions[scope] = [];
          } else {
            const failedUi = App.buildCompactionBlock(compaction);
            hasStage = true;
            appendStage(failedUi.wrap);
          }
          if (state.sessionId === sessionId) stickToBottom();
          return;
        }
        if (compaction.phase === "done" && !isMergeEvent && activeQueue && activeQueue.length) {
          activeQueue.shift().update(compaction);
          if (!activeQueue.length) delete activeCompactions[scope];
        } else {
          if (compaction.phase === "done") {
            // 断线重连时可能只回放到 done，仍需显示一条完整的完成状态块。
            hasStage = true;
          }
          const compactionUi = App.buildCompactionBlock(compaction);
          hasStage = true;
          appendStage(compactionUi.wrap);
          if (compaction.phase === "start") {
            (activeCompactions[scope] = activeCompactions[scope] || []).push(compactionUi);
          }
        }
        if (compaction.phase === "done" && state.sessionId === sessionId) {
          App.scheduleContextTokenStatsRefresh(App.CONTEXT_STATS_EVENT_DEBOUNCE_MS, sessionId);
        }
        if (state.sessionId === sessionId) stickToBottom();
        return;
      }

      if (data.warning) {
        const warnText = typeof data.warning === "string" ? data.warning : (data.warning.message || JSON.stringify(data.warning));
        msg.insertBefore(el("div", "notice-bar", "提示：" + warnText), msg.firstChild);
      }
      // 网络失败重试帧：显示重试进度与可展开的原始错误；重试成功继续时
      // 上方条保留（审计可见），不自动移除。
      if (data.error && data.retrying) {
        if (data.discard_tool_calls && activeToolBlocks) {
          // 参数流中断：这些工具未执行，移除占位，下一次尝试从新参数开始。
          activeToolBlocks.forEach(function (tb) {
            if (tb.done || tb.dispatched || (tb.agentBlock && tb.agentBlock.agentId)) return;
            tb.done = true;
            tb.ui.wrap.remove();
            if (tb.agentBlock && !tb.agentBlock.agentId) {
              tb.agentBlock.done = true;
              tb.agentBlock.ui.wrap.remove();
              agentByCall.forEach(function (block, key) {
                if (block === tb.agentBlock) agentByCall.delete(key);
              });
            }
          });
          activeToolBlocks = null;
        }
        const attempt = Number(data.retry || 0);
        const maxAttempts = data.max_attempts != null ? Number(data.max_attempts) : null;
        const summary = "网络请求失败，正在重试 " + attempt +
          (maxAttempts ? "/" + maxAttempts : "") + " 次：" + data.error;
        const bar = el("div", "notice-bar notice-retry");
        const textLine = el("div", "notice-retry-summary", summary);
        bar.appendChild(textLine);
        if (data.error_detail && data.error_detail !== data.error) {
          const pre = el("pre", "notice-retry-detail", String(data.error_detail));
          pre.hidden = true;
          const toggle = el("button", "notice-retry-toggle", "原始错误");
          toggle.type = "button";
          toggle.addEventListener("click", function () {
            pre.hidden = !pre.hidden;
            toggle.textContent = pre.hidden ? "原始错误" : "收起";
          });
          bar.appendChild(toggle);
          bar.appendChild(pre);
        }
        sealTextBlocks();
        msg.appendChild(bar);
        hasStage = true;
        return;
      }
      if (data.error) {
        sealTextBlocks();
        msg.appendChild(el("div", "notice-bar", "出错：" + data.error));
      }
      // 思考：每段独立一个块
      if (typeof data.reasoning_content === "string" && data.reasoning_content) {
        if (stagingActive()) {
          finalizeAnswerStaging();
          stagingThink = (stagingThink || "") + data.reasoning_content;
          titleReasoning += data.reasoning_content;
        } else {
          if (curAnswer) {
          curAnswer.classList.remove("msg-cursor");
          // 置空前刷出该段挂起的节流渲染帧（answerText 此时仍是本段的，
          // flush 会把最后一批增量落进 DOM；此后 curAnswer=null 跳过后续渲染）
          flushAnswerRender();
          curAnswer = null;
        }
        if (!curThink) {
          curThink = App.buildThinkBlock();
          curThink.streaming();
          hasStage = true;
          appendStage(curThink.wrap);
        }
        curThink.add(data.reasoning_content);
        titleReasoning += data.reasoning_content;
        }
      }
      // 回答：每段独立一个块
      if (typeof data.content === "string" && data.content) {
        // 正文到达即结束思考；兼容同帧 reasoning_content + content。
        if (stagingActive()) {
          finalizeThinkStaging();
          if (!stagingAnswer) {
            stagingAnswer = { el: el("div", "msg-assistant-body msg-cursor"), text: "" };
          }
          stagingAnswer.text += data.content;
          titleContent += data.content;
        } else {
          if (curThink) curThink.done();
          curThink = null;
          if (!curAnswer) {
          curAnswer = el("div", "msg-assistant-body msg-cursor");
          answerText = "";
          // 上一段挂起帧已在 reasoning 分支/sealTextBlocks 的置空前 flush，
          // 此处只需重置计时器锚点（curAnswer/answerText 均已指向新段）
          hasStage = true;
          appendStage(curAnswer);
        }
        answerText += data.content;
        // 节流渲染（flushAnswerRender 顶部定义）：先累积文本，60ms 后统一
        // Markdown.render + 高亮 + 重建，高速模型逐帧 delta 不再逐帧全量重排
        scheduleAnswerRender();
        titleContent += data.content;
        }
      }
      // 工具调用增量：按 index 合并，参数 JSON 逐帧流式展示。
      // 模型生成 arguments（命令文本/文件内容等）的每个增量帧都会实时
      // 追加到工具块输入区并自动滚动；首次收到增量时自动展开该块。
      if (Array.isArray(data.tool_calls) && data.tool_calls.length) {
        sealTextBlocks();
        if (!activeToolBlocks) activeToolBlocks = new Map();
        data.tool_calls.forEach(function (d) {
          const idx = d.index != null ? d.index : 0;
          if (!activeToolBlocks.has(idx)) {
            let toolBlock = d.id ? adoptHistoryToolBlock(d.id) : null;
            if (!toolBlock) {
              toolBlock = { ui: App.buildToolBlock("", { sessionId: sessionId }), argsText: "", done: false };
              toolBlocks.push(toolBlock);
              hasStage = true;
              appendStage(toolBlock.ui.wrap);
            }
            activeToolBlocks.set(idx, toolBlock);
          }
          const tb = activeToolBlocks.get(idx);
          const fn = d.function || {};
          if (d.id) tb.callId = d.id;
          if (tb.ui.bindPermission && tb.callId) tb.ui.bindPermission({ sessionId: sessionId, toolCallId: tb.callId, round: activeStream.round });
          if (fn.name) tb.ui.setName(tb.name = fn.name);
          if (tb.name === "sub_agent") promoteAgentTool(tb, tb.callId);

          // 首个参数数据到达：进入流式展示态（自动展开 + 执行中样式）。
          // 收养的历史卡参数已完整展示，跳过 beginStream（会清空输入/输出）。
          if (!tb.adopted && !tb.agentBlock && !tb.streamStarted && fn.arguments) {
            tb.streamStarted = true;
            tb.ui.beginStream();
          }
          if (typeof fn.arguments === "string" && fn.arguments) {
            tb.argsText += fn.arguments;
            // 收养卡的参数显示以历史为准：重放增量只累计文本用于派发，
            // 不再向输入区追加（否则与已展示的完整参数重复）。
            if (!tb.adopted && !tb.agentBlock) tb.ui.addInputDelta(fn.arguments);
          } else if (fn.arguments && typeof fn.arguments === "object") {
            tb.argsText = JSON.stringify(fn.arguments, null, 2);
            if (!tb.adopted && !tb.agentBlock) tb.ui.setInputText(tb.argsText);
          }
          if (tb.agentBlock) tb.agentBlock.ui.setDispatchInput(tb.argsText);
        });
      }
      // 工具开始执行：参数已完整、后端正在调用（等待结果阶段的可感知状态）
      if (data.tool_start && data.tool_start.function_name) {
        sealTextBlocks();
        const ts = data.tool_start;
        // 子任务派发：预建子任务块（占位），后续 sub_agent 事件续用同一块；
        // 不进入普通工具气泡渲染
        if (ts.function_name === "sub_agent") {
          const key = ts.tool_call_id || "";
          const activeBlocks = activeToolBlocks ? Array.from(activeToolBlocks.values()) : [];
          const tb = activeBlocks.find(t => t.callId === key && key)
            || activeBlocks.find(t => t.name === "sub_agent" && !t.dispatched && (!t.callId || t.callId === key));
          let block = key && agentByCall.get(key);
          if (tb) {
            block = promoteAgentTool(tb, key);
            tb.dispatched = true;
          }
          if (!block) {
            block = { ui: App.buildSubAgentBlock(sessionId), callId: key, agentId: "", done: false };
            bindAgentCall(block, key);
            agentBlocksAll.push(block);
            hasStage = true;
            appendStage(block.ui.wrap);
          }
          if (ts.arguments) block.ui.setDispatchInput(ts.arguments);
          return;
        }
        const activeBlocks = activeToolBlocks ? Array.from(activeToolBlocks.values()) : [];
        let tb = ts.tool_call_id && toolBlocks.find(t => !t.done && t.callId === ts.tool_call_id);
        tb = tb || activeBlocks.find(t => !t.done && (!ts.tool_call_id || !t.callId) && t.name === ts.function_name)
          || activeBlocks.find(t => !t.done && (!ts.tool_call_id || !t.callId));
        if (!tb) {
          tb = ts.tool_call_id ? adoptHistoryToolBlock(ts.tool_call_id) : null;
        }
        if (!tb) {
          tb = { ui: App.buildToolBlock(ts.function_name, { sessionId: sessionId }), argsText: "", done: false };
          toolBlocks.push(tb);
          hasStage = true;
          appendStage(tb.ui.wrap);
        }
        tb.ui.setName(ts.function_name);
        tb.name = ts.function_name; tb.callId = ts.tool_call_id || tb.callId;
        if (tb.ui.bindPermission && tb.callId) tb.ui.bindPermission({ sessionId: sessionId, toolCallId: tb.callId, round: activeStream.round });
        if (ts.arguments) tb.ui.setInput(FormatUtils.prettyJson(ts.arguments));
        tb.ui.executing();
      }
      // 工具结果：填充到对应块
      if (data.tool_return && data.tool_return.function_name) {
        sealTextBlocks();
        const tr = data.tool_return;
        if (App.terminalJobs && App.terminalJobs.noteToolResult) App.terminalJobs.noteToolResult(sessionId, tr.function_name);
        // 子任务最终回复：轨迹已由 sub_agent 事件渲染到子任务块，
        // 此处仅在块尾追加引用条；无块可挂时降级为普通工具块
        if (tr.function_name === "sub_agent") {
          const info = tr.sub_agent || null;
          let block = (info && info.agent_id ? agentById.get(info.agent_id) : null)
            || agentByCall.get(tr.tool_call_id);
          if (block) {
            if (tr.arguments) block.ui.setDispatchInput(tr.arguments);
            if (!block.ui.isDone()) block.ui.finalizeInterrupted();
            block.ui.markReturned(info);
            block.done = true;
          } else {
            const tb = { ui: App.buildToolBlock("sub_agent"), argsText: "", done: false };
            toolBlocks.push(tb);
            tb.ui.setInput(FormatUtils.prettyJson(tr.arguments));
            App.applyToolResult(
              tb.ui, tr.function_name,
              typeof tr.result === "string" ? tr.result : JSON.stringify(tr.result, null, 2),
              tr.file_diff,
              tr.arguments
            );
            tb.ui.finish();
            tb.done = true;
            hasStage = true;
            appendStage(tb.ui.wrap);
          }
          if (activeToolBlocks) {
            activeToolBlocks.forEach(function (tb) {
              if (tb.agentBlock === block) { tb.done = true; tb.ui.finish(); }
            });
            if (Array.from(activeToolBlocks.values()).every(tb => tb.done)) activeToolBlocks = null;
          }
          if (state.sessionId === sessionId) {
            App.scheduleContextTokenStatsRefresh(App.CONTEXT_STATS_EVENT_DEBOUNCE_MS, sessionId);
          }
          return;
        }
        const activeBlocks = activeToolBlocks ? Array.from(activeToolBlocks.values()) : [];
        let tb = tr.tool_call_id && toolBlocks.find(t => !t.done && t.callId === tr.tool_call_id);
        tb = tb || activeBlocks.find(t => !t.done && (!tr.tool_call_id || !t.callId) && t.name === tr.function_name)
          || activeBlocks.find(t => !t.done && (!tr.tool_call_id || !t.callId));
        if (!tb) {
          tb = tr.tool_call_id ? adoptHistoryToolBlock(tr.tool_call_id) : null;
        }
        if (!tb) {
          tb = { ui: App.buildToolBlock(tr.function_name, { sessionId: sessionId, toolCallId: tr.tool_call_id, round: activeStream.round }), argsText: "", done: false };
          toolBlocks.push(tb);
          hasStage = true;
          appendStage(tb.ui.wrap);
        }
        tb.ui.setName(tr.function_name);
        tb.ui.setInput(FormatUtils.prettyJson(tr.arguments));
        App.applyToolResult(
          tb.ui, tr.function_name,
          typeof tr.result === "string" ? tr.result : JSON.stringify(tr.result, null, 2),
          tr.file_diff,
          tr.arguments
        );
        tb.ui.finish();
        tb.done = true;
        if (activeToolBlocks && Array.from(activeToolBlocks.values()).every(function (toolBlock) { return toolBlock.done; })) {
          activeToolBlocks = null;
        }
        // 工具结果已写入后端历史并计入当前轮上下文：刷新统计增量
        // （防抖合并，流式期间多次结果只产生一次请求）
        if (state.sessionId === sessionId) {
          App.scheduleContextTokenStatsRefresh(App.CONTEXT_STATS_EVENT_DEBOUNCE_MS, sessionId);
          // V2 文件版本链：仅文件写型工具结果触发徽标刷新（防抖）；
          // 其它工具（搜索/命令/ask_user/todo 等）不产生版本链变更，不请求
          if (FILE_BADGE_TOOLS.indexOf(tr.function_name) >= 0) {
            scheduleFileChangesBadgeRefresh(sessionId);
          }
        }
      }
      if (data.finish_reason || data.response_complete || data.stream_truncated) {
        sealTextBlocks();
      }
      // token 用量：每次 LLM 调用一份独立统计，按 completion id 去重后累加为本轮累计
      if (data.usage && typeof data.usage === "object") {
        const usageAdded = onRoundUsage(data.usage, data.id);
        if (usageAdded && state.sessionId === sessionId) {
          App.refreshContextTokenStats(sessionId);
        }
      }
      if (state.sessionId === sessionId) stickToBottom();
      // 标题生成（前端驱动）：流内累计达 100 字符即发起一次标题请求（一次）
      maybeTriggerTitle(sessionId, false);
    }

    // 标题触发判定（管线内：访问采集变量）：未触发且有预览时发起一次请求；
    // force=true（流结束兜底）不足 100 字符也以已有全文触发
    function maybeTriggerTitle(sessionId, force) {
      if (titleTriggered || replayWindow) return;
      const preview = pickTitlePreview(titleReasoning, titleContent);
      if (!preview) return;
      if (!force && preview.length < TITLE_PREVIEW_CHARS) return;
      titleTriggered = true;
      requestSessionTitle(sessionId, activeStream.userText, activeStream.questionParts, preview);
    }

    return {
      handle: handle,
      // 附接回放 marker.model → 本轮展示模型（与 round_model 帧同一入口）
      setRoundModel: setRoundModel,
      setRoundUsageBaseline: setRoundUsageBaseline,
      // 回放去重窗口开关：附接 marker 打开、replay_end 关闭
      setReplayWindow: setReplayWindow,
      finish: function () {
        // 先关闭去重窗口：未 seal 的暂存按实时落定，不丢内容
        setReplayWindow(false);
        // 流结束时仍未收到消费信号：清除待注入提示（避免停止/异常时残留）
        App.clearInjectedPending(sessionId);
        // 流结束仍未触发标题：不足 100 字符以已有全文触发一次（有输出才触发）
        maybeTriggerTitle(sessionId, true);
        // 强制刷出未渲染的回答节流帧（收尾必须完整，不能丢最后 60ms 的字）
        sealTextBlocks();
        msg.querySelectorAll(".think-block.is-streaming").forEach(function (think) {
          think.classList.remove("is-streaming");
        });
        toolBlocks.forEach(function (tb) { if (tb && !tb.done) tb.ui.finish(); });
        // 流结束时仍未收到 done 的子任务块：标记中断态（父级停止/断流时
        // 后端通常会补写 stopped/interrupted，这里只是前端本地兜底）
        agentBlocksAll.forEach(function (block) {
          if (block && !block.done) block.ui.finalizeInterrupted();
        });
        if (App.processGroups) App.processGroups.sync(msg);
        if (App.permissions && App.permissions.syncPending) App.permissions.syncPending(sessionId);
        // 全部命中历史（收养/丢弃）且无其它内容时，移除空回复容器，避免空白占位
        if (!msg.children.length && msg.parentNode) msg.parentNode.removeChild(msg);
        return {
          hasStage: hasStage,
          roundUsage: roundUsageAcc,
          empty: !hasStage && !msg.querySelector(".notice-bar") && !msg.querySelector(".msg-user"),
        };
      },
    };
  }

  App.createStreamPipeline = createStreamPipeline;
})(window.App);
