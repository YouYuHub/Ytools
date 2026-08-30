/**
 * 会话列表工具（纯逻辑，无 DOM 依赖，可单元测试）
 * - 排序：按“最近更新时间”倒序（后更新的在前）
 * - 标题：与后端一致，取首条提问前 40 个字符
 * 浏览器挂 window.SessionListUtils；Node 下 module.exports，便于测试。
 */
(function (global) {
  "use strict";

  const TITLE_MAX = 40;

  // "2026-08-08 18:33:11" 形式的时间串 -> 毫秒时间戳；空值/解析失败返回 0
  function timeValue(ts) {
    if (!ts || typeof ts !== "string" || !ts.trim()) return 0;
    const t = Date.parse(ts.trim().replace(" ", "T"));
    return Number.isFinite(t) ? t : 0;
  }

  // 标题兜底规则（与后端 _meta.title 规则一致）：trim 后取前 40 个字符
  function firstQuestionTitle(text) {
    return (text == null ? "" : String(text)).trim().slice(0, TITLE_MAX);
  }

  /**
   * 对会话行按“最近更新时间倒序”排序（稳定，返回新数组，不改入参）。
   * rows: [{ id, title, updated, created }]
   * recency: { id -> 毫秒时间戳 }（本地近期触碰时间：发送/重命名等）
   * 排序规则：
   *   1. 主键 = max(后端 updated_at 时间戳, 本地 recency 时间戳)
   *   2. 相同则用 created_at 兜底（旧文件被后端误刷成同值时保证次序稳定）
   *   3. 仍相同保持原有顺序（Array.prototype.sort 稳定）
   */
  function sortRows(rows, recency) {
    const copy = Array.isArray(rows) ? rows.slice() : [];
    const rec = recency || {};
    copy.sort(function (a, b) {
      const keyA = Math.max(timeValue(a.updated), typeof rec[a.id] === "number" ? rec[a.id] : 0);
      const keyB = Math.max(timeValue(b.updated), typeof rec[b.id] === "number" ? rec[b.id] : 0);
      if (keyB !== keyA) return keyB - keyA;
      return timeValue(b.created) - timeValue(a.created);
    });
    return copy;
  }

  const api = {
    TITLE_MAX: TITLE_MAX,
    timeValue: timeValue,
    firstQuestionTitle: firstQuestionTitle,
    sortRows: sortRows,
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  } else {
    global.SessionListUtils = api;
  }
})(typeof self !== "undefined" ? self : globalThis);