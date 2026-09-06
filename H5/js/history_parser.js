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

      obj.events.forEach(function (evt) {
        if (!evt || typeof evt !== "object") return;
        const ts = evt.timestamp || "";

        // 单轮压缩事件嵌入 chat_round.events，按其在 events 中的位置渲染。
        if (evt.event === "context_compaction" && evt.scope === "round") {
          appendCompactionRecord(records, evt);
          return;
        }

        if (evt.role === "user" && evt.content && evt.content !== "停止任务") {
          records.push({ kind: "user", content: evt.content, ts: ts });
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