/**
 * 历史 jsonl 解析（纯逻辑，无 DOM 依赖，可单元测试）
 * 输入为后端 `history_files/<session_id>_chat.jsonl` 文本：
 * - 首行 _meta（usage 等）
 * - 其余为 chat_round 记录，展开为前端渲染用的 records 列表
 * 浏览器挂 window.HistoryParser；Node 下 module.exports。
 */
(function (global) {
  "use strict";

  /** 记录 content 的正文文本：字符串原样返回，多部件列表取 text 部件拼接。 */
  function recordContentText(content) {
    if (typeof content === "string") return content;
    if (!Array.isArray(content)) return content == null ? "" : String(content);
    return content
      .map(function (part) {
        return part && part.type === "text" && typeof part.text === "string" ? part.text : "";
      })
      .filter(function (text) { return text.trim(); })
      .join("\n");
  }

  /** 引用快照签名（去重比较用）：仅取各段文本，忽略来源/UI 字段。 */
  function quotesSignature(quotes) {
    if (!Array.isArray(quotes) || !quotes.length) return "";
    return JSON.stringify(quotes.map(function (quote) {
      return quote && typeof quote.text === "string" ? quote.text : "";
    }));
  }

  /**
   * 打开会话时，若该会话正有后台流在跑，历史中应剔除当前轮提问，
   * 避免与 restoreActiveStream/attachStreamSession 补建的提问气泡重复。
   *
   * 优先按轮次号精确匹配（activeRound 为后端 round_started / 回放 marker
   * 下发的本轮最终轮次号）：历史中存在同文本提问时不会再被误判成当前轮；
   * 多模态提问（content 为部件数组）也能匹配。轮次号有效但未命中说明本轮
   * 尚未落盘（生成中），此时直接返回全部——不能回退文本匹配，否则会误裁
   * 历史中同文本的旧提问（连同其后的整段记录）。无 activeRound（旧后端/
   * 旧调用方）时才回退文本匹配——对部件数组先取 text 部件拼接再比较，
   * 避免多模态提问因 content 不是字符串而匹配失败（气泡重建后重复渲染）；
   * 传入 activeQuotes 时文本匹配还要求引用快照一致（同文本不同引用不误裁）。
   */
  function recordsBeforeActiveRound(records, userText, activeRound, activeQuotes) {
    let activeIndex = -1;
    const round = Number(activeRound);
    if (Number.isFinite(round) && round > 0) {
      records.forEach(function (record, index) {
        if (record.kind === "user" && Number(record.round) === round) activeIndex = index;
      });
      return activeIndex >= 0 ? records.slice(0, activeIndex) : records;
    }
    const text = typeof userText === "string" ? userText : "";
    if (!text) return records;
    const wantedQuotes = quotesSignature(activeQuotes);
    records.forEach(function (record, index) {
      if (record.kind !== "user" || recordContentText(record.content) !== text) return;
      // 引用参与比较：仅当两侧都有引用时要求签名一致（旧记录无 quotes
      // 字段时不拒绝匹配，保持旧行为）
      if (wantedQuotes && record.quotes && quotesSignature(record.quotes) !== wantedQuotes) return;
      activeIndex = index;
    });
    return activeIndex >= 0 ? records.slice(0, activeIndex) : records;
  }

  function compactionRecord(obj) {
    const compactionUsage = obj.compress_usage || obj.summary_usage || null;
    return {
      kind: "compaction",
      scope: obj.scope || "round",
      phase: obj.phase || "",
      content: obj.compress_context || obj.context_summary || "",
      summary_text: typeof obj.summary_text === "string" ? obj.summary_text : "",
      usage: compactionUsage,
      before_tokens: obj.before_tokens != null
        ? obj.before_tokens : compactionUsage && compactionUsage.before_tokens,
      after_tokens: obj.after_tokens != null
        ? obj.after_tokens : compactionUsage && compactionUsage.after_tokens,
      // 触发阈值（min(聊天,压缩)窗口 × 触发比例）：历史回放同样显示，
      // 供用户核对"为何此时触发"；旧数据无此字段则不显示
      token_limit: obj.token_limit != null
        ? obj.token_limit : compactionUsage && compactionUsage.token_limit,
      // 触发来源与真实触发比较（task/first_call 路径：全量上下文 vs 阈值）
      trigger_reason: obj.trigger_reason != null ? obj.trigger_reason : "",
      trigger_context_tokens: obj.trigger_context_tokens != null
        ? obj.trigger_context_tokens : null,
      trigger_threshold: obj.trigger_threshold != null
        ? obj.trigger_threshold : null,
      target_tokens: obj.target_tokens != null
        ? obj.target_tokens : compactionUsage && compactionUsage.target_tokens,
      budget_scope: obj.budget_scope != null ? obj.budget_scope : "",
      compress_index: obj.compress_index != null
        ? obj.compress_index : compactionUsage && compactionUsage.compress_index,
      block_count: obj.block_count != null
        ? obj.block_count : compactionUsage && compactionUsage.block_count,
      // 批次诊断（P2-1）：本批喂入压缩模型的源规模 / 批源预算 / 保留原始对话的
      // 尾部轮次数 / 单段输出上限 / 源截断标记——历史回放同样可见，供用户核对
      // "每批到底吃了多少、是否被截断"
      batch_source_tokens: obj.batch_source_tokens != null
        ? obj.batch_source_tokens : compactionUsage && compactionUsage.batch_source_tokens,
      source_budget: obj.source_budget != null
        ? obj.source_budget : compactionUsage && compactionUsage.source_budget,
      tail_rounds: obj.tail_rounds != null
        ? obj.tail_rounds : compactionUsage && compactionUsage.tail_rounds,
      output_token_limit: obj.output_token_limit != null
        ? obj.output_token_limit : compactionUsage && compactionUsage.output_token_limit,
      source_was_truncated: obj.source_was_truncated != null
        ? !!obj.source_was_truncated
        : !!(compactionUsage && compactionUsage.source_was_truncated),
      source_was_chunked: obj.source_was_chunked != null
        ? !!obj.source_was_chunked
        : !!(compactionUsage && compactionUsage.source_was_chunked),
      source_chunk_count: obj.source_chunk_count != null
        ? obj.source_chunk_count
        : (compactionUsage && compactionUsage.source_chunk_count),
      warnings: Array.isArray(obj.warnings)
        ? obj.warnings
        : (compactionUsage && Array.isArray(compactionUsage.warnings)
          ? compactionUsage.warnings : null),
      ts: obj.timestamp || "",
    };
  }

  function appendCompactionRecord(records, obj) {
    if (obj.phase === "aborted") {
      const scope = obj.scope || "round";
      for (let i = records.length - 1; i >= 0; i -= 1) {
        const rec = records[i];
        if (rec.kind === "compaction" && rec.scope === scope && rec.phase === "start") {
          rec.interrupted = true;
          break;
        }
      }
      return;
    }
    records.push(compactionRecord(obj));
  }

  // ---------- 子任务（sub_agent）事件聚合 ----------
  // 后端在 chat_round.events 内以 event="sub_agent" 落盘子任务全轨迹
  //（start/model_call/tool_start/tool_result/todo/done），历史回放时按
  // agent_id 聚合为一个独立块记录（kind=agentBlock），渲染时机与事件在
  // events 中的先后顺序一致：start 前的记录先渲染、块体在原位展开。
  // 父级 sub_agent 工具调用（assistant.tool_calls 内 name=sub_agent）与
  // 对应 tool 角色结果条目不单独渲染（块尾已显示最终回复引用条）。

  /** 头部任务摘要：首行或前 80 字符（换行截断）。 */
  function summarizeTask(task) {
    const text = String(task || "");
    const firstLine = text.split("\n").find(function (line) { return line.trim(); }) || "";
    const short = firstLine.trim().slice(0, 80);
    return short || text.slice(0, 80) || "子任务";
  }

  function subAgentRecord(evt) {
    return {
      kind: "agentBlock",
      agent_id: evt.agent_id || "",
      parent_tool_call_id: evt.parent_tool_call_id || "",
      agent_index: Number(evt.agent_index || 0),
      task: evt.task || "",
      task_summary: summarizeTask(evt.task),
      todo: Array.isArray(evt.todo) ? evt.todo : null,
      tools: Array.isArray(evt.tools) ? evt.tools : [],
      rounds_limit: evt.rounds_limit != null ? Number(evt.rounds_limit) : null,
      started_at: evt.timestamp || "",
      // 块体条目（model_call/tool_start/tool_result/todo 按 seq 归位）
      entries: [],
      // done 聚合字段（无 done 事件 = 中断遗留，仍渲染已完成部分）
      status: "interrupted",
      final_reply: "",
      rounds: null,
      usage_total: null,
      error: "",
      ended_at: "",
      done_emitted: false,
    };
  }

  /**
   * 把子任务事件折叠进 records：
   * start 新建块；其余按 agent_id 追加到最近一块；done 收尾。
   * 块记录保持在其 start 事件出现的位置（时间顺序不变）。
   */
  function foldSubAgentEvent(records, evt, openBlocks) {
    const agentId = evt.agent_id || "";
    if (evt.phase === "start") {
      const record = subAgentRecord(evt);
      records.push(record);
      openBlocks[agentId] = record;
      return;
    }
    const block = openBlocks[agentId] || null;
    if (!block) return; // 孤儿事件（start 缺失）：忽略
    if (evt.phase === "done") {
      block.status = evt.status || "done";
      block.final_reply = typeof evt.final_reply === "string" ? evt.final_reply : "";
      block.rounds = evt.rounds != null ? Number(evt.rounds) : block.rounds;
      block.usage_total = evt.usage_total && typeof evt.usage_total === "object" ? evt.usage_total : block.usage_total;
      block.error = typeof evt.error === "string" ? evt.error : "";
      block.ended_at = evt.ended_at || evt.timestamp || "";
      block.done_emitted = true;
      delete openBlocks[agentId];
      return;
    }
    // delta 仅实时推流（JSONL 白名单本就不含，防御式跳过）
    if (evt.phase === "delta" || evt.phase === "heartbeat") return;
    block.entries.push({
      phase: evt.phase,
      seq: evt.seq != null ? Number(evt.seq) : null,
      reasoning_content: typeof evt.reasoning_content === "string" ? evt.reasoning_content : "",
      content: typeof evt.content === "string" ? evt.content : "",
      tool_calls: Array.isArray(evt.tool_calls) ? evt.tool_calls : null,
      tool_call_id: evt.tool_call_id || "",
      tool_name: evt.tool_name || "",
      arguments: typeof evt.arguments === "string" ? evt.arguments : (evt.arguments == null ? "" : JSON.stringify(evt.arguments)),
      result: evt.result,
      todos: Array.isArray(evt.todos) ? evt.todos : null,
      ts: evt.timestamp || "",
    });
  }

  // 解析 jsonl：首行 _meta，其余为 chat_round 记录或跨轮压缩事件
  // 每条渲染 record 附带 round（1-based 轮次号，与后端 delete_rounds 的
  // start_round 同口径）：供用户消息编辑/删除定位 JSONL 轮次
  function parseHistory(text) {
    const records = [];
    const pendingRoundCompactions = Object.create(null);
    // 带 display_round 锚点的压缩行按轮次号预收集（保持文件顺序）。
    // 锚点行在旧版后端"回答插入/编辑重发"收尾时可能位于其锚定轮次行之后，
    // 主循环跳过这些行、在解析到锚定轮次时按锚点归位合并——不依赖文件
    // 物理顺序，避免旧文件的压缩块被兜底显示到会话末尾。
    const anchoredCompactionLines = Object.create(null);
    let metaUsageTotal = 0;
    let metaUsage = {};
    let roundCounter = 0;

    // 预读轮次时间窗：修复旧 JSONL 中尚无 display_round 锚点的任务内历史压缩事件。
    const parsedLines = [];
    const roundTimeWindows = [];
    let candidateRoundNo = 0;
    text.split("\n").forEach(function (line, idx) {
      const trimmed = line.trim();
      if (!trimmed) return;
      let obj;
      try {
        obj = JSON.parse(trimmed);
      } catch (_) { return; }
      parsedLines.push({ index: idx, object: obj });
      if (obj.event === "chat_round" && Array.isArray(obj.events)) {
        candidateRoundNo += 1;
        roundTimeWindows.push({ round: candidateRoundNo, entry: obj });
      } else if (obj.event === "context_compaction") {
        const anchorRound = Number(obj.display_round);
        const anchorIndex = Number(obj.display_event_index);
        if (Number.isInteger(anchorRound) && anchorRound > 0
            && Number.isInteger(anchorIndex) && anchorIndex >= 0) {
          if (!anchoredCompactionLines[anchorRound]) anchoredCompactionLines[anchorRound] = [];
          anchoredCompactionLines[anchorRound].push(obj);
        }
      }
    });

    function timestampValue(value) {
      if (typeof value !== "string" || !value.trim()) return null;
      const parsed = Date.parse(value.includes("T") ? value : value.replace(" ", "T"));
      return Number.isFinite(parsed) ? parsed : null;
    }

    function inferLegacyRoundAnchor(compaction) {
      if (compaction.scope !== "session") return null;
      const compactionTime = timestampValue(compaction.timestamp);
      if (compactionTime === null) return null;
      for (let i = roundTimeWindows.length - 1; i >= 0; i -= 1) {
        const candidate = roundTimeWindows[i];
        const entry = candidate.entry;
        const events = Array.isArray(entry.events) ? entry.events : [];
        const start = timestampValue(entry.started_at)
          ?? (events.length ? timestampValue(events[0] && events[0].timestamp) : null);
        const end = timestampValue(entry.ended_at)
          ?? (events.length ? timestampValue(events[events.length - 1] && events[events.length - 1].timestamp) : null);
        // 使用严格结束边界，避免任务开始前的独立历史压缩与上一轮 ended_at
        // 同秒时被误认为发生在上一轮内部。
        if (start === null || end === null || compactionTime < start || compactionTime >= end) continue;

        // 时间戳精度为秒；遇到同秒事件时放在该秒最后一条之后，尽量贴近压缩触发点。
        let eventIndex = 0;
        events.forEach(function (event, index) {
          const eventTime = timestampValue(event && event.timestamp);
          if (eventTime !== null && eventTime <= compactionTime) eventIndex = index + 1;
        });
        return { round: candidate.round, eventIndex: eventIndex };
      }
      return null;
    }

    parsedLines.forEach(function (lineRecord) {
      const idx = lineRecord.index;
      const obj = lineRecord.object;

      if (idx === 0 && obj._meta) {
        metaUsage = obj._meta.usage || {};
        metaUsageTotal = metaUsage.total_tokens || 0;
        return;
      }
      // 压缩过程事件行（与 SSE 实时推送同一份 payload，字段一致）
      if (obj.event === "context_compaction") {
        const displayRound = Number(obj.display_round);
        const displayEventIndex = Number(obj.display_event_index);
        if (Number.isInteger(displayRound) && displayRound > 0
            && Number.isInteger(displayEventIndex) && displayEventIndex >= 0) {
          // 带轮内锚点的压缩行已在预扫描阶段按轮次收集（anchoredCompactionLines），
          // 此处跳过：即使物理位置在锚定轮次行之后（旧版后端"回答插入/编辑重发"
          // 收尾产生），也会在解析到该轮次时按锚点归位合并，不依赖文件顺序。
          return;
        }
        // 无锚点（旧 JSONL）：按时间窗推断轮次锚点，插回对应轮次
        const inferred = inferLegacyRoundAnchor(obj);
        if (inferred) {
          if (!pendingRoundCompactions[inferred.round]) pendingRoundCompactions[inferred.round] = [];
          pendingRoundCompactions[inferred.round].push({
            event: obj,
            eventIndex: inferred.eventIndex,
          });
          return;
        }
        appendCompactionRecord(records, obj);
        return;
      }
      if (obj.event !== "chat_round" || !Array.isArray(obj.events)) return;

      roundCounter += 1;
      const roundNo = roundCounter;
      const roundStart = records.length; // 本行产生的 records 起点（子块统一补轮次号用）
      const openBlocks = {}; // agent_id -> agentBlock record（轮内聚合）
      const anchoredCompactions = pendingRoundCompactions[roundNo] || [];
      delete pendingRoundCompactions[roundNo];
      // 预扫描收集的锚点行（含物理位置在轮次行之后的旧文件）并入归位列表：
      // 放在时间窗推断行之后（推断行仅服务旧数据兜底，两类通常不同时出现于
      // 同一轮）；同类内部保持 JSONL 写入顺序，相同锚点不重排。
      const presetAnchored = anchoredCompactionLines[roundNo];
      if (presetAnchored) {
        presetAnchored.forEach(function (anchorObj) {
          anchoredCompactions.push({
            event: anchorObj,
            eventIndex: Number(anchorObj.display_event_index),
          });
        });
        delete anchoredCompactionLines[roundNo];
      }
      const orderedEvents = [];
      let compactionIndex = 0;
      for (let eventIndex = 0; eventIndex <= obj.events.length; eventIndex += 1) {
        // 把任务中独立落盘的历史压缩事件合并回 pending chat_round.events。
        // 锚点表示压缩发生时已经写入的轮内事件数量；相同锚点保持 JSONL 写入顺序。
        while (compactionIndex < anchoredCompactions.length
            && anchoredCompactions[compactionIndex].eventIndex <= eventIndex) {
          orderedEvents.push({
            event: anchoredCompactions[compactionIndex].event,
            eventIndex: eventIndex,
          });
          compactionIndex += 1;
        }
        if (eventIndex < obj.events.length) {
          orderedEvents.push({ event: obj.events[eventIndex], eventIndex: eventIndex });
        }
      }
      // 防御异常/旧记录中的越界锚点，不能因此丢掉压缩记录。
      while (compactionIndex < anchoredCompactions.length) {
        orderedEvents.push({
          event: anchoredCompactions[compactionIndex].event,
          eventIndex: obj.events.length,
        });
        compactionIndex += 1;
      }
      orderedEvents.forEach(function (orderedEvent) {
        const evt = orderedEvent.event;
        const eventIndex = orderedEvent.eventIndex;
        if (!evt || typeof evt !== "object") return;
        const ts = evt.timestamp || "";

        // 子任务事件块：按 agent_id 聚合（渲染顺序与 events 顺序一致）
        if (evt.event === "sub_agent") {
          foldSubAgentEvent(records, evt, openBlocks);
          return;
        }

        // 轮内压缩事件及带锚点并回来的任务内历史压缩事件，按轮内位置渲染。
        if (evt.event === "context_compaction") {
          appendCompactionRecord(records, evt);
          return;
        }

        if (evt.role === "user" && evt.content && evt.content !== "停止任务") {
          const userRec = {
            kind: "user", content: evt.content, ts: ts, round: roundNo,
            source_event_index: eventIndex,
          };
          // 引用快照（选中文本引用到提问）：透传给渲染层绘制引用卡片；
          // 旧记录无该字段时正常回放（不显示卡片）
          if (Array.isArray(evt.quotes) && evt.quotes.length) userRec.quotes = evt.quotes;
          records.push(userRec);
          return;
        }
        // 父级 sub_agent 工具结果：轨迹已在 agentBlock 中，跳过普通渲染
        if (evt.role === "tool" && evt.tool_name === "sub_agent") {
          return;
        }
        if (evt.role === "tool") {
          const toolRec = {
            kind: "toolResult",
            name: evt.tool_name || "tool",
            args: evt.arguments || "",
            result: typeof evt.result === "string" ? evt.result : JSON.stringify(evt.result, null, 2),
            ts: ts,
            round: roundNo,
          };
          // 内置文件工具的展示用 diff（write_file/edit_file）：透传给渲染层
          if (evt.file_diff) toolRec.file_diff = evt.file_diff;
          records.push(toolRec);
          return;
        }
        if (evt.role !== "assistant") return;
        // 网络重试失败事件（逐次落盘）：历史回放中显示第几次失败与原始错误
        if (evt.event === "network_retry" && evt.error) {
          const retryNo = Number(evt.retry || 0);
          const maxNo = evt.max_attempts != null ? Number(evt.max_attempts) : null;
          records.push({
            kind: "notice",
            content: "网络请求失败（第 " + retryNo + (maxNo ? "/" + maxNo : "") + " 次重试前）：" + evt.error,
            ts: ts,
            round: roundNo,
          });
          return;
        }
        if (evt.error) {
          records.push({ kind: "notice", content: "出错：" + evt.error, ts: ts, round: roundNo });
          return;
        }
        if (typeof evt.reasoning_content === "string" && evt.reasoning_content.trim()) {
          records.push({ kind: "think", content: evt.reasoning_content, ts: ts, round: roundNo });
        }
        if (typeof evt.content === "string" && evt.content.trim() && !evt.done) {
          records.push({
            kind: "assistant", content: evt.content, ts: ts, round: roundNo,
            source_event_index: eventIndex,
          });
        }
        if (Array.isArray(evt.tool_calls)) {
          evt.tool_calls.forEach(function (call) {
            const fn = call.function || {};
            // 父级 sub_agent 派发调用：块内已显示完整任务与轨迹，跳过普通工具块
            if ((fn.name || call.name) === "sub_agent") return;
            records.push({ kind: "tool", name: fn.name || call.name || "tool", args: fn.arguments || "", ts: ts, round: roundNo });
          });
        }
      });

      if (obj.usage_total && obj.usage_total.total_tokens) {
        records.push({ kind: "usage", usage: obj.usage_total, round: roundNo });
      }
      // 轮内子块（sub_agent 聚合块等由辅助函数 push 的记录）统一补轮次号
      for (let r = roundStart; r < records.length; r += 1) {
        if (records[r].round == null) records[r].round = roundNo;
      }
    });

    // 正在进行/中断的轮次可能尚未写成 chat_round。保留其独立压缩事件，
    // 放在目前可见历史末尾，避免刷新后事件消失。
    Object.keys(pendingRoundCompactions).forEach(function (roundKey) {
      pendingRoundCompactions[roundKey].forEach(function (item) {
        appendCompactionRecord(records, item.event);
      });
    });

    // 锚定轮次不存在（轮次被删除/尚未收尾）的锚点行：同样保留在可见历史
    // 末尾，避免刷新后事件消失
    Object.keys(anchoredCompactionLines).forEach(function (roundKey) {
      anchoredCompactionLines[roundKey].forEach(function (anchorObj) {
        appendCompactionRecord(records, anchorObj);
      });
    });

    return { records: records, metaUsageTotal: metaUsageTotal, metaUsage: metaUsage };
  }

  const api = {
    recordsBeforeActiveRound: recordsBeforeActiveRound,
    parseHistory: parseHistory,
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  } else {
    global.HistoryParser = api;
  }
})(typeof self !== "undefined" ? self : globalThis);
