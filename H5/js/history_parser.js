/**
 * 历史 jsonl 解析（纯逻辑，无 DOM 依赖，可单元测试）
 * 输入为后端 `history_files/<session_id>_chat.jsonl` 文本：
 * - 首行 _meta（usage 等）
 * - 其余为 chat_round 记录，展开为前端渲染用的 records 列表
 * 浏览器挂 window.HistoryParser；Node 下 module.exports。
 */
(function (global) {
  "use strict";

  /**
   * 打开会话时，若该会话正有后台流在跑，历史中应剔除当前轮提问，
   * 避免与 restoreActiveStream/attachStreamSession 补建的提问气泡重复。
   */
  function recordsBeforeActiveRound(records, userText) {
    let activeIndex = -1;
    records.forEach(function (record, index) {
      if (record.kind === "user" && record.content === userText) activeIndex = index;
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
      compress_index: obj.compress_index != null
        ? obj.compress_index : compactionUsage && compactionUsage.compress_index,
      block_count: obj.block_count != null
        ? obj.block_count : compactionUsage && compactionUsage.block_count,
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
  function parseHistory(text) {
    const records = [];
    let metaUsageTotal = 0;
    let metaUsage = {};

    text.split("\n").forEach(function (line, idx) {
      const trimmed = line.trim();
      if (!trimmed) return;
      let obj;
      try {
        obj = JSON.parse(trimmed);
      } catch (_) { return; }

      if (idx === 0 && obj._meta) {
        metaUsage = obj._meta.usage || {};
        metaUsageTotal = metaUsage.total_tokens || 0;
        return;
      }
      // 压缩过程事件行（与 SSE 实时推送同一份 payload，字段一致）
      if (obj.event === "context_compaction") {
        appendCompactionRecord(records, obj);
        return;
      }
      if (obj.event !== "chat_round" || !Array.isArray(obj.events)) return;

      const openBlocks = {}; // agent_id -> agentBlock record（轮内聚合）
      obj.events.forEach(function (evt) {
        if (!evt || typeof evt !== "object") return;
        const ts = evt.timestamp || "";

        // 子任务事件块：按 agent_id 聚合（渲染顺序与 events 顺序一致）
        if (evt.event === "sub_agent") {
          foldSubAgentEvent(records, evt, openBlocks);
          return;
        }

        // 单轮压缩事件嵌入 chat_round.events，按其在 events 中的位置渲染。
        if (evt.event === "context_compaction" && evt.scope === "round") {
          appendCompactionRecord(records, evt);
          return;
        }

        if (evt.role === "user" && evt.content && evt.content !== "停止任务") {
          records.push({ kind: "user", content: evt.content, ts: ts });
          return;
        }
        // 父级 sub_agent 工具结果：轨迹已在 agentBlock 中，跳过普通渲染
        if (evt.role === "tool" && evt.tool_name === "sub_agent") {
          return;
        }
        if (evt.role === "tool") {
          records.push({
            kind: "toolResult",
            name: evt.tool_name || "tool",
            args: evt.arguments || "",
            result: typeof evt.result === "string" ? evt.result : JSON.stringify(evt.result, null, 2),
            ts: ts,
          });
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
          });
          return;
        }
        if (evt.error) {
          records.push({ kind: "notice", content: "出错：" + evt.error, ts: ts });
          return;
        }
        if (typeof evt.reasoning_content === "string" && evt.reasoning_content.trim()) {
          records.push({ kind: "think", content: evt.reasoning_content, ts: ts });
        }
        if (typeof evt.content === "string" && evt.content.trim() && !evt.done) {
          records.push({ kind: "assistant", content: evt.content, ts: ts });
        }
        if (Array.isArray(evt.tool_calls)) {
          evt.tool_calls.forEach(function (call) {
            const fn = call.function || {};
            // 父级 sub_agent 派发调用：块内已显示完整任务与轨迹，跳过普通工具块
            if ((fn.name || call.name) === "sub_agent") return;
            records.push({ kind: "tool", name: fn.name || call.name || "tool", args: fn.arguments || "", ts: ts });
          });
        }
      });

      if (obj.usage_total && obj.usage_total.total_tokens) {
        records.push({ kind: "usage", usage: obj.usage_total });
      }
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