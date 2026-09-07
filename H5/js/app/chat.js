/**
 * 聊天流：发送 / 停止 / 后台流附接
 * - SSE 流式渲染管线（思考/回答/工具/压缩/usage 分块，send 与重连共用）
 * - 发送（附件上传、多模态部件、会话惰性建号）、停止、断线重连续看、标题刷新
 * 依赖：app/core.js、API、Markdown、FormatUtils、SessionUtils；App.*：多个模块
 */
(function (App) {
  "use strict";
  const {
    state, el, toast, input,
    chatInner, sessionList, scrollToBottom, nearBottom,
    setEmpty, sendBtn, stopBtn
  } = App;

  // ---------- 发送 / 停止 ----------

  /**
   * 构建一条 SSE 流式事件的 DOM 渲染管线（思考/回答/工具/usage 分块处理）。
   * send() 与 attachStreamSession() 共用，保证刷新重连后渲染行为一致。
   */
  function createStreamPipeline(msg, sessionId, activeStream) {
    let curThink = null;
    let curAnswer = null;
    let answerText = "";
    let hasStage = false;
    const toolBlocks = [];
    let activeToolBlocks = null;
    const activeCompactions = {}; // scope -> [尚未完成的压缩 ui]
    let usageLine = null;
    // 一个模型 completion 可能在多个 SSE 帧重复携带 usage（甚至是逐步增长的快照）。
    // 按 completion id 保留最新快照，不能按每个 usage 对象直接相加，否则会把同一
    // 次请求重复累计成百万级 token。
    const usageByCompletion = new Map();
    const usageWithoutId = new Map();
    let roundUsageAcc = null;

    function sealTextBlocks() {
      if (curThink) curThink.done();
      if (curAnswer) curAnswer.classList.remove("msg-cursor");
      curThink = null;
      curAnswer = null;
    }

    function appendStage(node) {
      msg.appendChild(node);
      if (usageLine) msg.appendChild(usageLine);
    }

    function ensureUsageLine() {
      if (!usageLine) usageLine = el("div", "round-usage");
      return usageLine;
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
      activeStream.usage = roundUsageAcc;
      ensureUsageLine().textContent = FormatUtils.usageText(roundUsageAcc);
      msg.appendChild(usageLine);
      return true;
    }

    function handle(evt) {
      if (evt.type === "done") return;
      const data = evt.data || {};

      // 事件不含 reasoning_content 字段 = 当前思考阶段已结束
      const hasReasoning = typeof data.reasoning_content === "string" && data.reasoning_content;
      if (!hasReasoning && curThink) {
        curThink.done();
        curThink = null;
      }

      // 压缩事件不是普通文本/工具事件，单独渲染为可展开的状态块，
      // 并在完成后刷新一次上下文统计。兼容旧版后端的嵌套 payload。
      if (data.event === "todo") {
        // 模型通过 todo_write 更新了任务计划
        App.applySessionTodo(data.todos);
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
        if (state.sessionId === sessionId) App.openAskModal(questions, askCard);
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
            if (state.sessionId === sessionId && nearBottom()) scrollToBottom();
          }
          return;
        }
        const compactionUsage = compaction.compress_usage || compaction.summary_usage;
        const isMergeEvent = compactionUsage && Number(compactionUsage.merge_block_count) > 0;
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
        if (state.sessionId === sessionId && nearBottom()) scrollToBottom();
        return;
      }

      if (data.warning) {
        const warnText = typeof data.warning === "string" ? data.warning : (data.warning.message || JSON.stringify(data.warning));
        msg.insertBefore(el("div", "notice-bar", "提示：" + warnText), msg.firstChild);
      }
      // 网络失败重试帧：显示重试进度与可展开的原始错误；重试成功继续时
      // 上方条保留（审计可见），不自动移除。
      if (data.error && data.retrying) {
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
        msg.appendChild(el("div", "notice-bar", "出错：" + data.error));
      }
      // 思考：每段独立一个块
      if (typeof data.reasoning_content === "string" && data.reasoning_content) {
        if (curAnswer) {
          curAnswer.classList.remove("msg-cursor");
          curAnswer = null;
        }
        if (!curThink) {
          curThink = App.buildThinkBlock();
          curThink.streaming();
          hasStage = true;
          appendStage(curThink.wrap);
        }
        curThink.add(data.reasoning_content);
      }
      // 回答：每段独立一个块
      if (typeof data.content === "string" && data.content) {
        curThink = null;
        if (!curAnswer) {
          curAnswer = el("div", "msg-assistant-body msg-cursor");
          answerText = "";
          hasStage = true;
          appendStage(curAnswer);
        }
        answerText += data.content;
        curAnswer.innerHTML = Markdown.render(answerText);
        App.highlightCodeBlocks(curAnswer);
        // 流式整块重建后立即同步设置钉住态，避免 MutationObserver 延迟到下一帧造成左右闪切
        App.updateCodeblockCopyButtons();
      }
      // 工具调用增量：按 index 合并，参数 JSON 流式展示
      if (Array.isArray(data.tool_calls)) {
        sealTextBlocks();
        if (!activeToolBlocks) activeToolBlocks = new Map();
        data.tool_calls.forEach(function (d) {
          const idx = d.index != null ? d.index : 0;
          if (!activeToolBlocks.has(idx)) {
            const toolBlock = { ui: App.buildToolBlock(""), argsText: "", done: false };
            activeToolBlocks.set(idx, toolBlock);
            toolBlocks.push(toolBlock);
            hasStage = true;
            appendStage(toolBlock.ui.wrap);
          }
          const tb = activeToolBlocks.get(idx);
          const fn = d.function || {};
          if (fn.name) tb.ui.setName(tb.name = fn.name);
          if (typeof fn.arguments === "string") {
            tb.argsText += fn.arguments;
            tb.ui.setInput(tb.argsText);
          } else if (fn.arguments && typeof fn.arguments === "object") {
            tb.argsText = JSON.stringify(fn.arguments, null, 2);
            tb.ui.setInput(tb.argsText);
          }
        });
      }
      // 工具结果：填充到对应块
      if (data.tool_return && data.tool_return.function_name) {
        const tr = data.tool_return;
        const activeBlocks = activeToolBlocks ? Array.from(activeToolBlocks.values()) : [];
        let tb = activeBlocks.find(function (t) { return t && !t.done && t.name === tr.function_name; })
          || activeBlocks.find(function (t) { return t && !t.done; });
        if (!tb) {
          tb = { ui: App.buildToolBlock(tr.function_name), argsText: "", done: false };
          toolBlocks.push(tb);
          hasStage = true;
          appendStage(tb.ui.wrap);
        }
        tb.ui.setName(tr.function_name);
        tb.ui.setInput(FormatUtils.prettyJson(tr.arguments));
        tb.ui.setOutput(typeof tr.result === "string" ? tr.result : JSON.stringify(tr.result, null, 2));
        tb.ui.finish();
        tb.done = true;
        if (activeToolBlocks && Array.from(activeToolBlocks.values()).every(function (toolBlock) { return toolBlock.done; })) {
          activeToolBlocks = null;
        }
        // 工具结果已写入后端历史并计入当前轮上下文：刷新统计增量
        // （防抖合并，流式期间多次结果只产生一次请求）
        if (state.sessionId === sessionId) {
          App.scheduleContextTokenStatsRefresh(App.CONTEXT_STATS_EVENT_DEBOUNCE_MS, sessionId);
        }
      }
      // token 用量：每次 LLM 调用一份独立统计，按 completion id 去重后累加为本轮累计
      if (data.usage && typeof data.usage === "object") {
        const usageAdded = onRoundUsage(data.usage, data.id);
        if (usageAdded && state.sessionId === sessionId) {
          App.refreshContextTokenStats(sessionId);
        }
      }
      if (state.sessionId === sessionId && nearBottom()) scrollToBottom();
    }

    return {
      handle: handle,
      finish: function () {
        if (curAnswer) curAnswer.classList.remove("msg-cursor");
        msg.querySelectorAll(".think-block.is-streaming").forEach(function (think) {
          think.classList.remove("is-streaming");
        });
        toolBlocks.forEach(function (tb) { if (tb && !tb.done) tb.ui.finish(); });
        return {
          hasStage: hasStage,
          roundUsage: roundUsageAcc,
          empty: !hasStage && !msg.querySelector(".notice-bar") && !msg.querySelector(".msg-user"),
        };
      },
    };
  }

  async function send() {
    const text = input.value.trim();
    const pendingMedia = state.pendingMedia.slice();
    // 有附件时允许无文本发送
    if (!text && !pendingMedia.length) return;
    // 仅当“当前会话”正在流式时禁止发送；其他会话在后台流式不影响本会话发送
    if (state.streaming && state.streamingSession === state.sessionId) return;
    // 手动压缩进行中：发送会打断压缩任务并交错写入会话历史
    if (state.manualCompactRunning) {
      toast("正在压缩对话上下文，请稍后再发送");
      return;
    }
    // 其他会话的流仍在监听：先放弃本地监听（其后端任务继续在后台生成，可随时切回续看）
    if (state.streaming) detachAttachedStream();

    // 首次发送才分配会话 ID（"新对话"后一直处于待开始状态）
    const streamSessionId = App.ensureSessionId();
    // 先置流式态再上传附件：上传期间同样锁住发送入口
    state.streaming = true;
    state.streamingSession = streamSessionId;
    state.abort = new AbortController();
    state.hasConversation = true;
    state.importedHistoryText = null;
    App.updateExportButton();
    setEmpty(false);
    App.refreshComposerButtons();

    // 上传媒体附件到 history_files/upload/<session>/media/，换回 media:// 引用；
    // 后端发送上游前会把引用解析为 data URL / base64（兼容 Chat Completions 格式）
    const uploadedMedia = [];
    if (pendingMedia.length) {
      try {
        const uploadRes = await API.uploadSessionMedia(
          streamSessionId,
          pendingMedia.map(function (m) { return m.file; })
        );
        (uploadRes.results || []).forEach(function (result) {
          if (result.status === "success") uploadedMedia.push(result);
          else toast("附件上传失败：" + result.filename + "：" + (result.message || "未知原因"));
        });
      } catch (err) {
        toast("附件上传失败：" + err.message);
        state.streaming = false;
        App.refreshComposerButtons();
        return;
      }
    }

    // 构造多模态消息内容：纯文本保持字符串；带附件时为 OpenAI 兼容部件列表
    const contentParts = [];
    if (text) contentParts.push({ type: "text", text: text });
    uploadedMedia.forEach(function (m) {
      if (m.kind === "image") {
        contentParts.push({ type: "image_url", image_url: { url: m.media_ref } });
      } else if (m.kind === "audio") {
        contentParts.push({
          type: "input_audio",
          input_audio: { data: m.media_ref, format: (m.stored_name.split(".").pop() || "").toLowerCase() },
        });
      } else if (m.kind === "video") {
        contentParts.push({ type: "video_url", video_url: { url: m.media_ref } });
      }
    });
    const userContent = contentParts.length === 1 && contentParts[0].type === "text" ? text : contentParts;

    const userNode = App.appendUserMessage(userContent, null, streamSessionId, state.sessionDocs);
    App.upsertLocalSession(streamSessionId, { userText: text || (uploadedMedia.length ? "[图片/附件]" : text) });
    App.rebuildQnav();
    App.clearPendingMedia();
    input.value = "";
    App.autosize();
    App.refreshComposerButtons();
    // 只做一次首轮刷新，让刚提交的用户消息尽快出现在上下文统计中；
    // 后续更新由 SSE usage / 压缩完成事件驱动，不再持续轮询。
    App.refreshContextTokenStats(streamSessionId);

    // 本轮回复容器：思考/回答/工具按到达顺序分块
    const msg = el("div", "msg");
    chatInner.appendChild(msg);
    scrollToBottom();
    const activeStream = {
      sessionId: streamSessionId,
      userText: text,
      userNode: userNode,
      messageNode: msg,
      completed: false,
      usage: null,
    };
    state.activeStream = activeStream;

    const pipe = createStreamPipeline(msg, streamSessionId, activeStream);

    const payload = {
      messages: [{ role: "user", content: userContent }],
      session_id: streamSessionId,
    };
    // 内置工具（todo_write/ask_user）并入 selectedTools，与 MCP 工具一起随 tool_names 上送，
    // 后端按名称识别注入；未选择任何工具时不携带该字段，由后端回退会话/全局默认选择
    if (state.selectedTools.size > 0) {
      payload.tool_names = Array.from(state.selectedTools);
    }
    // 生成参数（temperature/top_p/presence_penalty/reasoning_effort/extra_body 等）
    // 不显式传递，由服务端按 model_selection.chat_model.parameter 填充（面板“参数”设置）

    try {
      await API.chatStream(payload, pipe.handle, state.abort.signal);
    } catch (err) {
      if (err.name !== "AbortError") {
        msg.appendChild(el("div", "notice-bar", "请求失败：" + err.message));
      }
    } finally {
      const fin = pipe.finish();
      const shouldReloadVisibleSession = state.sessionId === streamSessionId && activeStream.messageNode.parentNode !== chatInner;
      if (fin.empty && !msg.querySelector(".msg-user")) msg.remove();
      const usageReconciled = await App.refreshSessionUsage(streamSessionId);
      if (!usageReconciled && fin.roundUsage && fin.roundUsage.total_tokens) {
        // 后端暂不可用时才使用本地累计值作为临时回退，避免正常情况下重复累加。
        if (state.sessionId === streamSessionId) App.addSessionUsage(fin.roundUsage);
      }
      activeStream.completed = true;
      // 仅当本流仍是当前被监听的流时才清理全局标志，避免误清切换后新会话的流状态
      const wasAttached = state.activeStream === activeStream;
      if (wasAttached) {
        state.activeStream = null;
        state.streaming = false;
        state.streamingSession = null;
        state.abort = null;
      }
      if (state.sessionId === streamSessionId) App.clearContextStatsTimer();
      App.refreshComposerButtons();
      refreshSessionTitle(streamSessionId);
      if (shouldReloadVisibleSession) {
        setTimeout(function () { App.openSession(streamSessionId); }, 0);
      }
      // 流结束（含 [DONE]/出错/停止）：后端已收尾当前轮，刷新一次统计。
      // 用防抖合并：与 usage 事件触发的刷新合并成一个请求
      if (state.sessionId === streamSessionId) {
        App.scheduleContextTokenStatsRefresh(App.CONTEXT_STATS_EVENT_DEBOUNCE_MS, streamSessionId);
      }
      // 流结束后刷新提问卡片可答性：出现过普通用户消息/更新提问后旧卡片转为过期
      App.refreshAskBlockStates();
    }
  }

  sendBtn.addEventListener("click", send);

  stopBtn.addEventListener("click", async function () {
    const streamSessionId = state.activeStream ? state.activeStream.sessionId : state.sessionId;
    if (streamSessionId) {
      try {
        await API.stopChat(streamSessionId);
      } catch (_) { /* 后端停止失败也继续本地中止 */ }
    }
    if (state.abort) state.abort.abort();
  });

  /**
   * 放弃当前监听的流：仅中止本地连接（后端生成任务继续在后台运行，随时可切回续看），
   * 并清理全局流状态。切换会话/在别处新发送时调用，保证不会误停其他会话的任务。
   */
  function detachAttachedStream() {
    if (state.abort) state.abort.abort();
    state.abort = null;
    state.streaming = false;
    state.streamingSession = null;
    if (state.activeStream) {
      state.activeStream.completed = true;
      state.activeStream = null;
    }
    App.refreshComposerButtons();
  }

  /**
   * 刷新/重新打开会话时若后端仍在为同一会话生成，则附接该后台流：
   * 收到 replay 标记后补建“当前轮提问”气泡，再回放当前轮事件并持续推流。
   */
  async function attachStreamSession(sessionId) {
    if (!sessionId) return;
    if (state.streaming && state.streamingSession === sessionId) return;
    if (state.streaming) detachAttachedStream();
    state.streaming = true;
    state.streamingSession = sessionId;
    state.abort = new AbortController();
    App.refreshComposerButtons();
    App.refreshContextTokenStats(sessionId);
    setEmpty(false);

    const msg = el("div", "msg");
    chatInner.appendChild(msg);
    scrollToBottom();
    const activeStream = {
      sessionId: sessionId,
      userText: "",
      userNode: null,
      messageNode: msg,
      completed: false,
      usage: null,
    };
    state.activeStream = activeStream;
    const pipe = createStreamPipeline(msg, sessionId, activeStream);
    let sawReplay = false;

    try {
      await API.chatStream(
        { session_id: sessionId, messages: [] },
        function (evt) {
          const data = evt.data || {};
          if (data && data.replay) {
            sawReplay = true;
            if (data.question_text) {
              activeStream.userText = data.question_text;
              // 历史文件已落盘时可能已经渲染过本轮提问，避免重复气泡
              const duplicated = Array.from(chatInner.querySelectorAll(".msg-user .msg-bubble"))
                .some(function (node) { return node.textContent === data.question_text; });
              if (!duplicated) {
                // 提问气泡必须排在回答上方：回答容器 msg 已先入列，把气泡插入到它之前
                const userNode = App.appendUserMessage(data.question_text);
                chatInner.insertBefore(userNode, msg);
                activeStream.userNode = userNode;
              }
            }
            return;
          }
          pipe.handle(evt);
        },
        state.abort.signal
      );
    } catch (err) {
      if (err.name !== "AbortError") {
        msg.appendChild(el("div", "notice-bar", "请求失败：" + err.message));
      }
    } finally {
      const fin = pipe.finish();
      if (fin.empty && !sawReplay) msg.remove();
      const usageReconciled = await App.refreshSessionUsage(sessionId);
      if (!usageReconciled && fin.roundUsage && fin.roundUsage.total_tokens) {
        if (state.sessionId === sessionId) App.addSessionUsage(fin.roundUsage);
      }
      activeStream.completed = true;
      // 仅当本流仍是当前被监听的流时才清理全局标志，避免误清切换后新会话的流状态
      const wasAttached = state.activeStream === activeStream;
      if (wasAttached) {
        state.activeStream = null;
        state.streaming = false;
        state.streamingSession = null;
        state.abort = null;
      }
      if (state.sessionId === sessionId) App.clearContextStatsTimer();
      App.refreshComposerButtons();
      // 附接流结束：同样用防抖合并刷新一次统计增量
      if (state.sessionId === sessionId) {
        App.scheduleContextTokenStatsRefresh(App.CONTEXT_STATS_EVENT_DEBOUNCE_MS, sessionId);
      }
    }
  }

  /** 打开会话后：若该会话后台仍在生成，自动附接续看。 */
  function maybeAttachRunningStream(sessionId) {
    if (!sessionId) return;
    const sid = SessionUtils.sanitizeSessionId(sessionId);
    if (state.streaming && state.streamingSession === sid) return;
    API.streamStatus(sid)
      .then(function (status) {
        if (status && status.running && !(state.streaming && state.streamingSession === sid)) {
          attachStreamSession(sid);
        }
      })
      .catch(function () { /* 状态查询失败则跳过续接 */ });
  }

  // 发送后刷新侧边栏标题（新会话首条消息会生成历史文件）
  let titleTimer = null;
  function refreshSessionTitle(sessionId) {
    const targetSessionId = SessionUtils.sanitizeSessionId(sessionId || state.sessionId);
    clearTimeout(titleTimer);
    titleTimer = setTimeout(async function () {
      const known = Array.from(sessionList.querySelectorAll(".session-item"))
        .some(function (n) { return n.dataset.session === targetSessionId; });
      if (!known) {
        await App.loadSessions();
        App.markActiveSession();
      }
      try {
        const meta = await API.getSessionMeta(targetSessionId);
        if (meta.title) {
          const node = sessionList.querySelector('[data-session="' + targetSessionId + '"] .session-name');
          if (node) {
            node.textContent = SessionUtils.getSessionTitle(targetSessionId, meta.title);
            App.refreshSessionFades();
          }
        }
      } catch (_) { /* 忽略 */ }
    }, 1200);
  }


  // ---------- 导出（供其它模块经 App.* 调用） ----------
  App.send = send;
  App.maybeAttachRunningStream = maybeAttachRunningStream;
})(window.App);
