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
    chatInner, sessionList, scrollToBottom, nearBottom, stickToBottom,
    setEmpty, sendBtn, stopBtn
  } = App;

  // ---------- 发送 / 停止 ----------

  /**
   * 构造多模态消息内容部件：纯文本保持字符串；带附件时为 OpenAI 兼容部件列表。
   * pendingMedia 为 [{file,name,kind,media_ref...}] 快照（须已上传，含 media_ref）。
   */
  function buildUserContent(text, uploadedMedia) {
    const contentParts = [];
    if (text) contentParts.push({ type: "text", text: text });
    (uploadedMedia || []).forEach(function (m) {
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
    return contentParts.length === 1 && contentParts[0].type === "text" ? text : contentParts;
  }

  /**
   * 上传媒体附件到 history_files/session_files/<session>/media/，换回 media:// 引用；
   * 后端发送上游前会把引用解析为 data URL / base64（兼容 Chat Completions 格式）
   */
  async function uploadMediaFiles(streamSessionId, files) {
    const uploadedMedia = [];
    const uploadRes = await API.uploadSessionMedia(
      streamSessionId,
      files
    );
    (uploadRes.results || []).forEach(function (result) {
      if (result.status === "success") uploadedMedia.push(result);
      else toast("附件上传失败：" + result.filename + "：" + (result.message || "未知原因"));
    });
    return uploadedMedia;
  }

  // V2 文件版本链徽标刷新（防抖）：文件工具结果到达后延迟拉一次统计。
  // 仅文件写型工具才可能产生版本链变更（版本链 tool 角色=write_file/edit_file/
  // sub_agent.*）——搜索/命令/问答等工具结果不再触发 /file_diff/list 请求，
  // 避免多工具任务期间每个工具结果一次接口调用的风暴
  let fileChangesTimer = null;
  const FILE_BADGE_TOOLS = ["write_file", "edit_file", "sub_agent"];
  function scheduleFileChangesBadgeRefresh(sessionId) {
    if (state.sessionId !== sessionId) return;
    if (fileChangesTimer) clearTimeout(fileChangesTimer);
    fileChangesTimer = setTimeout(function () {
      fileChangesTimer = null;
      if (App.fileHistory && typeof App.fileHistory.refreshBadge === "function") {
        // 工具结果是数据变更源：force 不受 refreshBadge 的节流窗口限制
        App.fileHistory.refreshBadge(undefined, true);
      }
    }, 400);
  }

  /**
   * 构建一条 SSE 流式事件的 DOM 渲染管线（思考/回答/工具/usage 分块处理）。
   * send() 与 attachStreamSession() 共用，保证刷新重连后渲染行为一致。
   */
  function createStreamPipeline(msg, sessionId, activeStream) {
    let curThink = null;
    let curAnswer = null;
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
    // 标题生成（前端驱动）：流内累计思考/正文 delta，达 TITLE_PREVIEW_CHARS
    // 时由 maybeTriggerTitle 发起一次标题请求（聊天主链路不再感知标题任务）
    let titleReasoning = "";
    let titleContent = "";
    let titleTriggered = false;

    function sealTextBlocks() {
      if (curThink) curThink.done();
      if (curAnswer) curAnswer.classList.remove("msg-cursor");
      flushAnswerRender();
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

    // ---------- 子任务块（sub_agent）事件路由 ----------
    // 同一块对象双键登记：父级 tool_start 只带 tool_call id（预建占位），
    // 子事件带 agent_id（start 到达后绑定）。done 后保留在索引中，
    // tool_return(sub_agent) 到达时据此追加"最终回复已返回父智能体"引用条。
    function handleSubAgentEvent(data) {
      let block = null;
      if (data.agent_id && agentById.has(data.agent_id)) block = agentById.get(data.agent_id);
      if (!block && data.parent_tool_call_id && agentByCall.has(data.parent_tool_call_id)) {
        block = agentByCall.get(data.parent_tool_call_id);
      }
      if (!block) {
        block = { ui: App.buildSubAgentBlock(), callId: "", agentId: "", done: false };
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
      if (state.sessionId === sessionId) stickToBottom();
    }

    function handle(evt) {
      if (evt.type === "done") return;
      const data = evt.data || {};

      // 轮次开始：后端任务启动后推送本轮最终轮次号（普通发送），前端就地
      // 补挂本轮提问气泡的编辑/复制/删除入口（此前收尾不重载就没有入口）；
      // 同时同步到 activeStream.round——中途切走再切回会话时，openSession
      // 的历史裁剪/去重按轮次精确匹配（编辑重发路径 send() 已提前打标）
      if (data.round_started && typeof data.round_started.round === "number") {
        if (activeStream.round == null) activeStream.round = data.round_started.round;
        App.attachLiveRoundEntry(activeStream, data.round_started.round);
      }

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
      if (data.event === "message_injected") {
        // 运行中注入的用户消息（消息引导）在检查点被消费：此刻消息节点
        // 末尾恰好在当前轮工具结果之后，在此插入气泡与后端 messages
        // 顺序一致；同时清除消息框上方的待注入提示
        sealTextBlocks();
        App.clearInjectedPending(sessionId);
        App.appendUserMessage(
          typeof data.text === "string" ? data.text : "",
          null,
          sessionId,
          null,
          msg
        );
        hasStage = true;
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
        if (state.sessionId === sessionId) App.openAskModal(questions, askCard);
        return;
      }
      if (data.event === "sub_agent") {
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
      // 回答：每段独立一个块
      if (typeof data.content === "string" && data.content) {
        // 边界帧可能同帧混发 reasoning_content + content（上游透传原样转发）：
        // 此时上面的"不含 reasoning 字段"检测不会收尾，必须在这里补 done()，
        // 否则思考块停留在点点点态，直到整条流结束才恢复下拉箭头
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
      // 工具调用增量：按 index 合并，参数 JSON 逐帧流式展示。
      // 模型生成 arguments（命令文本/文件内容等）的每个增量帧都会实时
      // 追加到工具块输入区并自动滚动；首次收到增量时自动展开该块。
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
          // 首个参数数据到达：进入流式展示态（自动展开 + 执行中样式）
          if (!tb.streamStarted && fn.arguments) {
            tb.streamStarted = true;
            tb.ui.beginStream();
          }
          if (typeof fn.arguments === "string" && fn.arguments) {
            tb.argsText += fn.arguments;
            tb.ui.addInputDelta(fn.arguments);
          } else if (fn.arguments && typeof fn.arguments === "object") {
            tb.argsText = JSON.stringify(fn.arguments, null, 2);
            tb.ui.setInputText(tb.argsText);
          }
        });
      }
      // 工具开始执行：参数已完整、后端正在调用（等待结果阶段的可感知状态）
      if (data.tool_start && data.tool_start.function_name) {
        const ts = data.tool_start;
        // 子任务派发：预建子任务块（占位），后续 sub_agent 事件续用同一块；
        // 不进入普通工具气泡渲染
        if (ts.function_name === "sub_agent") {
          const key = ts.tool_call_id || "";
          if (!key || !agentByCall.has(key)) {
            const block = { ui: App.buildSubAgentBlock(), callId: key, agentId: "", done: false };
            if (key) agentByCall.set(key, block);
            agentBlocksAll.push(block);
            hasStage = true;
            appendStage(block.ui.wrap);
          }
          return;
        }
        const activeBlocks = activeToolBlocks ? Array.from(activeToolBlocks.values()) : [];
        let tb = activeBlocks.find(function (t) { return t && !t.done && t.name === ts.function_name; })
          || activeBlocks.find(function (t) { return t && !t.done; });
        if (!tb) {
          tb = { ui: App.buildToolBlock(ts.function_name), argsText: "", done: false };
          toolBlocks.push(tb);
          hasStage = true;
          appendStage(tb.ui.wrap);
        }
        tb.ui.setName(ts.function_name);
        tb.ui.executing();
      }
      // 工具结果：填充到对应块
      if (data.tool_return && data.tool_return.function_name) {
        const tr = data.tool_return;
        // 子任务最终回复：轨迹已由 sub_agent 事件渲染到子任务块，
        // 此处仅在块尾追加引用条；无块可挂时降级为普通工具块
        if (tr.function_name === "sub_agent") {
          const info = tr.sub_agent || null;
          let block = info && info.agent_id ? agentById.get(info.agent_id) : null;
          if (block) {
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
          if (state.sessionId === sessionId) {
            App.scheduleContextTokenStatsRefresh(App.CONTEXT_STATS_EVENT_DEBOUNCE_MS, sessionId);
          }
          return;
        }
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
      if (titleTriggered) return;
      const preview = pickTitlePreview(titleReasoning, titleContent);
      if (!preview) return;
      if (!force && preview.length < TITLE_PREVIEW_CHARS) return;
      titleTriggered = true;
      requestSessionTitle(sessionId, activeStream.userText, activeStream.questionParts, preview);
    }

    return {
      handle: handle,
      finish: function () {
        // 流结束时仍未收到消费信号：清除待注入提示（避免停止/异常时残留）
        App.clearInjectedPending(sessionId);
        // 流结束仍未触发标题：不足 100 字符以已有全文触发一次（有输出才触发）
        maybeTriggerTitle(sessionId, true);
        // 强制刷出未渲染的回答节流帧（收尾必须完整，不能丢最后 60ms 的字）
        flushAnswerRender();
        if (curAnswer) curAnswer.classList.remove("msg-cursor");
        msg.querySelectorAll(".think-block.is-streaming").forEach(function (think) {
          think.classList.remove("is-streaming");
        });
        toolBlocks.forEach(function (tb) { if (tb && !tb.done) tb.ui.finish(); });
        // 流结束时仍未收到 done 的子任务块：标记中断态（父级停止/断流时
        // 后端通常会补写 stopped/interrupted，这里只是前端本地兜底）
        agentBlocksAll.forEach(function (block) {
          if (block && !block.done) block.ui.finalizeInterrupted();
        });
        return {
          hasStage: hasStage,
          roundUsage: roundUsageAcc,
          empty: !hasStage && !msg.querySelector(".notice-bar") && !msg.querySelector(".msg-user"),
        };
      },
    };
  }

  /**
   * 发送一条用户消息并开启新一轮回复。
   * @param {object} [direct] 直接载荷（引导/队列 flush 调用）：
   *   { text, media } —— media 为待上传附件快照；缺省时从输入框读取。
   */
  async function send(direct) {
    // 防御：事件监听器直接传引用时，MouseEvent 等非普通对象不算直接载荷
    if (direct && (typeof direct !== "object" || direct instanceof Event)) direct = undefined;
    const fromInput = !direct;
    const text = direct ? (direct.text || "") : input.value.trim();
    const pendingMedia = direct ? (direct.media || []).slice() : state.pendingMedia.slice();
    // 有附件时允许无文本发送
    if (!text && !pendingMedia.length) return;
    // 仅当“当前会话”正在流式时禁止发送；其他会话在后台流式不影响本会话发送
    if (state.streaming && state.streamingSession === state.sessionId) return;
    // 手动压缩进行中：仅压缩会话禁止发送（发送会打断压缩任务并交错写入会话历史）
    if (state.manualCompactRunning && state.manualCompactSession === state.sessionId) {
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

    // 上传媒体附件（输入框路径用 state.pendingMedia 的 file；flush 路径同构；
    // 编辑重发路径的元素已带 media_ref/stored_name 无 file —— 直接复用原引用，不重新上传）
    const uploadedMedia = [];
    const needUploadMedia = [];
    pendingMedia.forEach(function (m) {
      if (m && m.file) needUploadMedia.push(m);
      else if (m && m.media_ref) uploadedMedia.push(m);
    });
    if (needUploadMedia.length) {
      try {
        const files = needUploadMedia.map(function (m) { return m.file; });
        const results = await uploadMediaFiles(streamSessionId, files);
        results.forEach(function (result) { uploadedMedia.push(result); });
      } catch (err) {
        toast("附件上传失败：" + err.message);
        state.streaming = false;
        App.refreshComposerButtons();
        // flush 来源的失败消息放回队列头部，避免丢失
        if (!fromInput) {
          state.pendingQueue.unshift({ text: text, media: pendingMedia, sessionId: streamSessionId });
          App.renderPendingOutbox();
        }
        return;
      }
      // 上传成功后附件已换为 media:// 引用：flush 来源的本地预览 URL 不再需要，释放
      if (!fromInput) {
        pendingMedia.forEach(function (m) {
          if (m.objUrl) {
            try { URL.revokeObjectURL(m.objUrl); } catch (_) { /* ignore */ }
          }
        });
      }
    }

    const userContent = buildUserContent(text, uploadedMedia);

    const userNode = App.appendUserMessage(userContent, null, streamSessionId, state.sessionDocs);
    App.upsertLocalSession(streamSessionId, { userText: text || (uploadedMedia.length ? "[图片/附件]" : text) });
    // 问题导航重建已移到下方 DOM 手术（原地重发删旧轮+原位插入）之后：
    // 若在手术前重建，旧第 N 轮节点仍在（旧文本占第 N 位），新气泡又临时
    // 挂在末尾（新文本占第 total+1 位），导航面板会出现新旧两条重复条目
    if (fromInput) {
      App.clearPendingMedia();
      input.value = "";
    }
    App.autosize();
    App.refreshComposerButtons();
    // 只做一次首轮刷新，让刚提交的用户消息尽快出现在上下文统计中；
    // 后续更新由 SSE usage / 压缩完成事件驱动，不再持续轮询。
    App.refreshContextTokenStats(streamSessionId);

    // 本轮回复容器：思考/回答/工具按到达顺序分块
    const msg = el("div", "msg");
    // insertedMidway：原地重发/插入重答把新节点放在会话中部（非末尾追加）时为真，
    // 用于滚动分叉（停在发起位置而非贴底）；
    // liveRound：本轮在历史中的最终轮次号（重发=第 N 轮，插入=第 N+1 轮），
    // 流式节点补打 data-round 用；普通发送为 null（收尾重载后随历史回放打标）
    let insertedMidway = false;
    const liveRound = direct && direct.targetRound != null ? direct.targetRound
      : direct && direct.insertAfterRound != null ? direct.insertAfterRound + 1
      : null;
    // 编辑重发（重新生成该轮）：屏幕锚点插入——删除旧第 N 轮的屏幕节点，
    // 新回复插入其原位（第 N+1 轮首节点之前），保持与历史结构一致；
    // ask_user 卡片再答（insertAfterRound）：新回复插入第 N+1 轮首节点之前
    // （即原第 N 轮之后），后续轮次节点整体后推——与后端插入收尾一一对应；
    // 普通发送直接追加末尾
    if (direct && direct.targetRound) {
      const oldRound = String(direct.targetRound);
      const firstNext = chatInner.querySelector('[data-round="' + (direct.targetRound + 1) + '"]');
      let removed = false;
      let sweepDone = false;
      // 旧轮节点可能被中间压缩等无 data-round 的节点分隔，统一遍历删除；
      // 命中下一轮已标记节点即停止——旧轮区间之后的无标记节点（后续轮次的
      // 时间分隔条等）不属于旧轮，不能清
      Array.from(chatInner.childNodes).forEach(function (node) {
        if (node.nodeType !== 1) return;
        if (node.getAttribute("data-round") === oldRound) {
          node.remove();
          removed = true;
          return;
        }
        if (!removed || sweepDone) return;
        if (node.getAttribute("data-round")) {
          sweepDone = true;
          return;
        }
        // 旧轮区间内的时间戳等无标记节点一并清掉（停止于旧轮边界）
        if (node.classList && (node.classList.contains("msg-time") || node.classList.contains("round-usage"))) {
          node.remove();
        }
      });
      if (firstNext && firstNext.parentNode === chatInner) {
        chatInner.insertBefore(userNode, firstNext);
        chatInner.insertBefore(msg, firstNext);
        insertedMidway = true;
      } else {
        // 找不到下一轮锚点（编辑的是最后一轮）：直接追加
        chatInner.appendChild(userNode);
        chatInner.appendChild(msg);
      }
    } else if (direct && direct.insertAfterRound) {
      // 回答轮插入：锚点为旧编号第 N+1 轮首节点（其后所有轮次在屏幕上自然后推）。
      // 先取锚点引用再重编号：后端插入语义会把后续轮次编号整体 +1，屏幕节点
      // data-round 同步重编，否则新节点补打 data-round=N+1 后与旧编号碰撞
      // （编辑/删除按编号定位会错轮）
      const firstNext = chatInner.querySelector('[data-round="' + (direct.insertAfterRound + 1) + '"]');
      Array.from(chatInner.querySelectorAll("[data-round]")).forEach(function (node) {
        const r = parseInt(node.getAttribute("data-round"), 10);
        if (r >= direct.insertAfterRound + 1) {
          node.setAttribute("data-round", String(r + 1));
          if (node._editData) node._editData.round = r + 1;
        }
      });
      if (firstNext && firstNext.parentNode === chatInner) {
        chatInner.insertBefore(userNode, firstNext);
        chatInner.insertBefore(msg, firstNext);
        insertedMidway = true;
      } else {
        chatInner.appendChild(userNode);
        chatInner.appendChild(msg);
      }
    } else {
      chatInner.appendChild(userNode);
      chatInner.appendChild(msg);
    }
    // 原地重发/插入重答：新节点已在会话中部就位，补上历史节点才有的
    // data-round 标记与编辑数据——否则本轮没有编辑/复制入口，且后续再次
    // 编辑本轮时删除循环匹配不到旧回复节点。必须在手术完成后打标：
    // 新 userNode 若先带上 data-round=N，会被上面的删除循环误删
    if (liveRound != null) {
      userNode.dataset.round = String(liveRound);
      userNode._editData = { content: userContent, round: liveRound };
      App.attachUserEditAction(userNode);
      msg.dataset.round = String(liveRound);
    }
    // 原地重发/插入重答时视口停留在发起位置：对齐新提问气泡到视口顶部
    // （瞬跳，覆盖 smooth 滚动动画），不滚到底部——底部往往是更靠后的
    // 旧轮次；末尾追加语义（普通发送/编辑最后一轮）保持贴底跟随生成
    if (insertedMidway) {
      const prevBehavior = chatScroll.style.scrollBehavior;
      chatScroll.style.scrollBehavior = "auto";
      const delta = userNode.getBoundingClientRect().top - chatScroll.getBoundingClientRect().top;
      chatScroll.scrollTop += delta;
      chatScroll.style.scrollBehavior = prevBehavior;
      // 向下跳转不触发用户上滚暂停机制，显式暂停自动贴底：
      // 流式期间视口不被 delta 高频贴底拉回，用户滚回底部可自动恢复跟随
      App.pauseAutoScroll();
    } else {
      scrollToBottom();
    }
    App.rebuildQnav();    const activeStream = {
      sessionId: streamSessionId,
      userText: text,
      // 首问原始 content（字符串或部件列表）：round_started 到达时给本轮
      // 提问气泡补挂编辑/删除入口用（编辑回填与媒体芯片解析的数据源）
      userContent: userContent,
      // 多模态时含 media:// 引用，标题请求透传给后端解析
      questionParts: Array.isArray(userContent) ? userContent : null,
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
    // 编辑重发「重新生成该轮」：携带 target_round，后端把本轮回复原地替换
    // 历史第 N 轮（上下文截到第 N-1 轮）；ask_user 卡片再答携带 insert_round，
    // 后端把回答轮插入历史第 N 轮之后（上下文截到第 N 轮）；普通发送不带
    // 该字段走追加语义
    if (direct && direct.targetRound) {
      payload.target_round = direct.targetRound;
    } else if (direct && direct.insertAfterRound) {
      payload.insert_round = direct.insertAfterRound;
    }
    // 内置工具（todo_write/ask_user）并入 selectedTools，与 MCP 工具一起随 tool_names 上送，
    // 后端按名称识别注入；未选择任何工具时不携带该字段，由后端回退会话/全局默认选择
    if (state.selectedTools.size > 0) {
      // 模型不支持视觉时 read_media 不上送（工具弹窗已禁选，此处为兜底过滤；
      // 后端请求侧还有一层拦截，前端/后端双保险）
      let toolNames = Array.from(state.selectedTools);
      if (!App.getChatModelVision()) {
        toolNames = toolNames.filter(function (name) { return name !== "read_media"; });
      }
      if (toolNames.length > 0) payload.tool_names = toolNames;
    }
    // 生成参数（temperature/top_p/presence_penalty/reasoning_effort/extra_body 等）
    // 不显式传递，由服务端按 model_selection.chat_model.parameter 填充（面板“参数”设置）

    // 原地重发/插入重答的屏幕状态（删旧轮/重编号/打标）发生在发送瞬间，
    // 而后端替换/插入收尾发生在流结束——失败/中止/空回复时屏幕与落盘可能失配，
    // 收尾后强制重载会话恢复真实状态；成功路径不重载，保持视口停在发起位置
    let streamFailed = false;
    try {
      await API.chatStream(payload, pipe.handle, state.abort.signal);
    } catch (err) {
      streamFailed = true;
      if (err.name !== "AbortError") {
        msg.appendChild(el("div", "notice-bar", "请求失败：" + err.message));
      }
    } finally {
      const fin = pipe.finish();
      if (typeof App.finalizeMermaidBlocks === "function") App.finalizeMermaidBlocks();
      // 编辑重发"停止生成并重发"接管本流时会打 detached 标记：节点被新轮
      // DOM 手术移除属预期行为，旧流收尾的重载兜底必须跳过，否则会把新
      // 一轮流式渲染的节点整段重建掉
      const wasDetached = activeStream.detached === true;
      const shouldReloadVisibleSession = !wasDetached
        && state.sessionId === streamSessionId
        && activeStream.messageNode.parentNode !== chatInner;
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
      if (shouldReloadVisibleSession) {
        setTimeout(function () { App.openSession(streamSessionId); }, 0);
      } else if (!wasDetached && liveRound != null && (streamFailed || fin.empty)) {
        // 原地重发/插入重答失败（网络中断/停止/空回复等）：屏幕手术已做过但
        // 后端可能未收尾提交，重载会话与 JSONL 对齐
        setTimeout(function () { App.openSession(streamSessionId); }, 0);
      }
      // 流结束（含 [DONE]/出错/停止）：后端已收尾当前轮，刷新一次统计。
      // 用防抖合并：与 usage 事件触发的刷新合并成一个请求
      if (state.sessionId === streamSessionId) {
        App.scheduleContextTokenStatsRefresh(App.CONTEXT_STATS_EVENT_DEBOUNCE_MS, streamSessionId);
      }
      // 流结束后刷新提问卡片可答性：出现过普通用户消息/更新提问后旧卡片转为过期
      App.refreshAskBlockStates();
      // 引导/队列消息派发：本轮结束（含停止/出错）后自动发送下一条。
      // 延迟到下一轮事件循环，确保 state.streaming 已清理完毕
      setTimeout(function () { App.flushPendingMessages(streamSessionId); }, 0);
    }
  }

  sendBtn.addEventListener("click", function () { send(); });

  stopBtn.addEventListener("click", async function () {
    // 手动压缩进行中：终止按钮中止压缩（断开 SSE 连接，后端检测到断开后自行收尾）；
    // 终止作用于压缩会话本身，切到其他会话后此按钮属于该会话的流式任务
    if (state.manualCompactRunning && state.manualCompactSession === state.sessionId) {
      if (state.manualCompactAbort) state.manualCompactAbort.abort();
      return;
    }
    // 用户主动停止：本轮结束后不自动派发引导/队列消息（消费一次后复位）
    state.suppressFlushOnce = true;
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
    // 切换会话放弃监听：该轮结束不自动派发引导/队列（消费一次后复位）
    state.suppressFlushOnce = true;
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
      // 附接回放后回填：提问原始 content（部件列表/文本）与本轮最终轮次号，
      // openSession 的历史裁剪/去重与编辑入口补挂都按这两个字段精确匹配
      userContent: null,
      round: null,
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
            // 本轮最终轮次号（后端 round_started 推送时同步记录、随 marker 下发）：
            // 历史裁剪/去重/补挂编辑入口统一按轮次精确匹配——同文本历史提问
            // 不再被误判成本轮、多模态（部件数组）提问也能命中
            if (typeof data.round === "number" && data.round > 0) {
              activeStream.round = data.round;
            }
            // question_parts 为提问原始 content 部件（多模态时含 media:// 引用；
            // 纯文本提问后端归一为单个 text 部件）：用多部件气泡重建（缩略图
            // 可点开预览），缺失时才退回纯文本
            const parts = Array.isArray(data.question_parts) ? data.question_parts : null;
            if (data.question_text) activeStream.userText = data.question_text;
            else if (parts) {
              activeStream.userText = parts
                .map(function (p) { return p && p.type === "text" && typeof p.text === "string" ? p.text : ""; })
                .filter(function (t) { return t.trim(); })
                .join("\n");
            }
            // 原始 content（重建/复用气泡后补挂编辑入口的数据源）：部件优先，
            // 缺失时退化为文本字符串（编辑重发按文本处理）
            if (parts) activeStream.userContent = parts;
            else if (activeStream.userContent == null && activeStream.userText) {
              activeStream.userContent = activeStream.userText;
            }
            if (activeStream.userText || parts) {
              // 历史文件已落盘时可能已经渲染过本轮提问（生成中刷新，提问事件
              // 已随检查点/收尾写入）：优先按轮次精确匹配并复用该节点补挂
              // 编辑入口。轮次号有效但未命中 = 本轮尚未落盘（生成中），直接
              // 新建气泡——不回退文本匹配，否则会误复用历史中同文本的旧提问
              // 节点（后续编辑/删除定位错轮）。仅无轮次号（旧后端）时回退
              // 文本全等比较（两侧 trim，与历史回放同口径）
              let existing = null;
              if (activeStream.round != null) {
                existing = chatInner.querySelector(
                  '.msg-user[data-round="' + activeStream.round + '"]'
                );
              } else {
                const wanted = String(activeStream.userText || "").trim();
                existing = Array.from(chatInner.querySelectorAll(".msg-user"))
                  .find(function (node) {
                    const bubble = node.querySelector(".msg-bubble");
                    return Boolean(bubble) && bubble.textContent.trim() === wanted;
                  }) || null;
              }
              if (existing) {
                activeStream.userNode = existing;
              } else {
                // 提问气泡必须排在回答上方：回答容器 msg 已先入列，把气泡插入到它之前
                const userNode = parts
                  ? App.appendUserMessage(parts, null, sessionId)
                  : App.appendUserMessage(data.question_text, null, sessionId);
                chatInner.insertBefore(userNode, msg);
                activeStream.userNode = userNode;
              }
              // 补挂轮次标记与编辑/复制/删除入口（复用节点时幂等跳过）
              App.attachLiveRoundEntry(activeStream, activeStream.round);
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
      if (typeof App.finalizeMermaidBlocks === "function") App.finalizeMermaidBlocks();
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
      // 附接的后台流结束：派发该会话暂存的引导/队列消息（用户仍停留在该会话时）
      setTimeout(function () { App.flushPendingMessages(sessionId); }, 0);
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
        } else if (!status || !status.running) {
          // 后台无生成任务：补发该会话暂存的引导/队列消息（切走期间错过的派发）。
          // flush 内部校验用户仍停留在该会话，切走则留在暂存区
          setTimeout(function () { App.flushPendingMessages(sid); }, 0);
        }
      })
      .catch(function () { /* 状态查询失败则跳过续接与补发 */ });
  }

  // ---------- 会话标题（前端驱动，零轮询） ----------
  // 采集与触发：流式管线累计思考/正文 delta，任一达 TITLE_PREVIEW_CHARS 即
  // POST /chat_config/generate_title **一次**；流结束时不足 100 字符以已有
  // 全文触发。后端生成写盘并把标题同步返回（已生成过/失败标记/未配置时
  // 返回 skipped + 当前盘上标题，同样一次往返）——前端收到即替换侧栏标题，
  // 不再轮询 /chat_history/meta。标题生成与聊天主链路完全解耦。
  const TITLE_PREVIEW_CHARS = 100;
  let titleInflight = null; // 进行中的标题请求 {sessionId}（防并发重复）

  function pickTitlePreview(reasoning, content) {
    // 思考与正文取较长者（正文语义更相关），截到 100 字符
    const source = content.length >= reasoning.length ? content : reasoning;
    return source.slice(0, TITLE_PREVIEW_CHARS).trim();
  }

  async function requestSessionTitle(sessionId, questionText, questionParts, preview) {
    const sid = SessionUtils.sanitizeSessionId(sessionId);
    if (!sid || (titleInflight && titleInflight.sessionId === sid)) return;
    titleInflight = { sessionId: sid };
    try {
      const res = await API.generateSessionTitle({
        session_id: sid,
        question_text: String(questionText || "").slice(0, 300),
        model_preview: preview || "",
        question_parts: Array.isArray(questionParts) && questionParts.length ? questionParts : undefined,
      });
      const title = res && res.title || "";
      if (title) applySessionTitle(sid, title);
    } catch (_) { /* 请求失败静默：标题保持现状 */ }
    finally {
      if (titleInflight && titleInflight.sessionId === sid) titleInflight = null;
    }
  }

  function applySessionTitle(sessionId, title) {
    const sid = SessionUtils.sanitizeSessionId(sessionId);
    const node = sessionList.querySelector('[data-session="' + sid + '"] .session-name');
    if (!node) {
      // 侧栏还没有该会话条目（极端时序）：重载会话列表建立条目后再替换
      App.loadSessions().then(function () {
        App.markActiveSession();
        const retry = sessionList.querySelector('[data-session="' + sid + '"] .session-name');
        if (retry && App.setSessionNameText(retry, title)) {
          App.refreshSessionFades();
        }
      }).catch(function () { /* ignore */ });
      return;
    }
    if (App.setSessionNameText(node, title)) {
      App.refreshSessionFades();
    }
  }


  // ---------- 用户消息编辑重发编排（编辑 UI 在 messages.js） ----------

  /**
   * 编辑重发专用：会话正在生成时先弹确认框（再次提醒任务正在进行），
   * 确认返回 true 并立即停止生成——与停止按钮同款链路（后端 stop_chat
   * 会等待任务完全退出、按"手动停止"把已生成内容收尾落盘），同时复位
   * 本地流式状态；取消返回 false（用户放弃本次编辑重发）。
   * @returns {Promise<boolean>} true=已停止可继续删除/重发；false=用户取消
   */
  async function requestStopRunningGeneration(sessionId) {
    const message = "当前会话正在生成回复。确认后将立刻停止本次生成" +
      "（已生成内容按中断收尾落盘），并以编辑后的消息重新发起。";
    const confirmed = await new Promise(function (resolve) {
      if (typeof App.openConfirmDialog !== "function") {
        resolve(window.confirm(message + "确认继续？"));
        return;
      }
      let settled = false;
      App.openConfirmDialog({
        title: "任务正在进行中",
        message: message,
        confirmText: "停止生成并重发",
        onConfirm: function () { settled = true; resolve(true); },
        onCancel: function () { if (!settled) { settled = true; resolve(false); } },
      });
    });
    if (!confirmed) return false;
    // 用户主动停止：本轮结束后不自动派发引导/队列消息（消费一次后复位）
    state.suppressFlushOnce = true;
    try {
      await API.stopChat(sessionId);
    } catch (_) { /* 后端停止失败也继续本地清理（生成可能已自行结束） */ }
    // 中止本地监听（后端任务已由 stopChat 收尾；abort 触发旧 send 的
    // finally 兜底清理，节点已脱管不会误删新内容）
    if (state.activeStream && state.activeStream.sessionId === sessionId) {
      if (state.abort) state.abort.abort();
      // detached 标记：旧流收尾的失败重载兜底跳过（本轮即将被删除/重发，
      // 新一轮流式渲染已在路上，重载会把新节点整段重建掉）
      state.activeStream.detached = true;
      state.activeStream.completed = true;
      state.activeStream = null;
    }
    if (state.streamingSession === sessionId) {
      state.streaming = false;
      state.streamingSession = null;
      // 本地监听已中止：清掉所属控制器（旧 send 的 finally 因 wasAttached
      // 已为 false 不会清理；下次 send 会重新创建）
      state.abort = null;
    }
    App.refreshComposerButtons();
    return true;
  }

  /**
   * 编辑重发总编排：按模式执行删除/预演确认 → 重载会话 → 发送。
   * @param {{msg: HTMLElement, round: number, text: string, media: Array, mode: string}} plan
   *   mode: "regen"=重新生成该轮（原地替换，保留后续轮次）；
   *         "truncate"=删除该轮及之后（GPT 语义）
   */
  async function confirmEditResend(plan) {
    const sessionId = state.sessionId;
    if (!sessionId || !plan || !plan.round) return;
    // 会话正在生成：先经用户二次确认停止任务（再次提醒"任务正在进行"），
    // 停止收尾落盘后才允许删除轮次，随后按计划重发
    if (state.streaming && state.streamingSession === sessionId) {
      const proceed = await requestStopRunningGeneration(sessionId);
      if (!proceed) return;
    }
    // 复用附件：编辑重发不重新上传，删除清理时后端按 keep_media_refs 排除
    const keptRefs = (plan.media || [])
      .map(function (m) { return m.stored_name || ""; })
      .filter(Boolean);

    const doSend = function (targetRound) {
      App.exitUserMessageEdit(plan.msg);
      App.send({
        text: plan.text,
        media: plan.media,
        targetRound: targetRound,
      });
    };

    const reloadAndSend = async function (mode, targetRound, keepRefs) {
      try {
        await API.deleteRounds(sessionId, plan.round, {
          mode: mode,
          deleteFiles: true,
          keepMediaRefs: keepRefs,
        });
      } catch (err) {
        App.toast("删除轮次失败：" + err.message);
        return;
      }
      // 删除成功：强制整段重放（data-round 随新解析自动刷新），然后发送。
      // 删除接口要求会话非运行中；仅复位本会话的本地流式态（不影响其他会话后台流监听）
      if (state.streamingSession === sessionId) {
        state.streaming = false;
        state.streamingSession = null;
      }
      await App.openSession(sessionId);
      doSend(targetRound);
    };

    if (plan.mode === "regen") {
      // 重新生成该轮：不预删！直接带 target_round 发送，后端轮次收尾时把
      // 新回复整轮替换到历史第 N 轮位置（替换即删旧插新）。
      // 若先删后发，后续轮次会前移导致 target_round 与删后编号错位，
      // 新回复会错误替换到原第 N+1 轮的位置。
      // 旧轮的孤儿附件留在盘上（无引用、占位极小），可经消息媒体删除功能清理。
      doSend(plan.round);
      return;
    }
    // 删除该轮及之后：破坏面较大（含后续轮次与其附件），先预演展示明细
    let planned;
    try {
      planned = await API.deleteRounds(sessionId, plan.round, {
        mode: "truncate",
        deleteFiles: true,
        dryRun: true,
      });
    } catch (err) {
      App.toast("删除预演失败：" + err.message);
      return;
    }
    // ask_user 卡片再答等无编辑面板的调用方（plan.msg 为 null）：降级用
    // 原生 confirm 展示预演明细（ dry_run 已执行，确认后直接发送）
    const plansArea = plan.msg ? plan.msg.querySelector(".msg-edit-plans") : null;
    if (!plansArea) {
      const summary = planned.planned_rounds || [];
      const mediaFiles = (planned.planned_files && planned.planned_files.media_files) || [];
      const docFiles = (planned.planned_files && planned.planned_files.doc_files) || [];
      const lines = ["将删除 " + summary.length + " 个轮次"];
      if (summary.length && summary[0].round) lines.push("（第 " + summary[0].round + " 轮起）");
      if (mediaFiles.length) lines.push("、" + mediaFiles.length + " 个附件文件");
      if (docFiles.length) lines.push("、" + docFiles.length + " 个上传文档");
      if (window.confirm(lines.join("") + "。确认删除并发送？")) {
        doSend(null);
      }
      return;
    }
    plansArea.innerHTML = "";
    const box = el("div", "msg-edit-confirm");
    const summary = planned.planned_rounds || [];
    const mediaFiles = (planned.planned_files && planned.planned_files.media_files) || [];
    const docFiles = (planned.planned_files && planned.planned_files.doc_files) || [];
    const lines = [];
    lines.push("将删除 " + summary.length + " 个轮次"
      + (summary.length && summary[0].round ? "（第 " + summary[0].round + " 轮起）" : "")
      + "。");
    if (mediaFiles.length) lines.push("将删除 " + mediaFiles.length + " 个附件文件。");
    if (docFiles.length) lines.push("将删除 " + docFiles.length + " 个上传文档。");
    const desc = el("div", "msg-edit-confirm-desc", lines.join(" "));
    box.appendChild(desc);
    const actions = el("div", "msg-edit-confirm-actions");
    const cancelBtn = el("button", "msg-edit-cancel", "取消");
    const confirmBtn = el("button", "msg-edit-send", "确认删除并发送");
    cancelBtn.type = "button";
    confirmBtn.type = "button";
    cancelBtn.addEventListener("click", function () {
      // 复位面板确认条状态（showPlanConfirm 与预演条共用 plans 区域）
      const panel = plan.msg.querySelector(".msg-edit-panel");
      if (panel) panel._confirmShown = false;
      plansArea.innerHTML = "";
    });
    confirmBtn.addEventListener("click", async function () {
      confirmBtn.disabled = true;
      await reloadAndSend("truncate", null, keptRefs);
    });
    actions.appendChild(cancelBtn);
    actions.appendChild(confirmBtn);
    box.appendChild(actions);
    plansArea.appendChild(box);
  }

  // ---------- 导出（供其它模块经 App.* 调用） ----------
  App.send = send;
  App.confirmEditResend = confirmEditResend;
  App.maybeAttachRunningStream = maybeAttachRunningStream;
})(window.App);
