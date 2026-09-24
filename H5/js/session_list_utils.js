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
   * 发送消息时的侧边栏标题决策：优先沿用"当前标题"（后端 _meta.title /
   * 标题模型生成 / 用户手动重命名 / 上次落定的占位——这些才是用户期望
   * 一直显示的标题），只有从未见过的新会话才退回"提问前 40 字"临时占位，
   * 等标题模型生成后由 applySessionTitle 替换。
   *
   * 旧行为是无条件用"当前提问前 40 字"，导致已有会话（含已生成的模型标题）
   * 在发新消息瞬间被临时占位文本顶掉，刷新后才恢复。
   *
   * @param {string} knownTitle 当前已知标题（调用方注册表；空串 = 没有）
   * @param {string} userText   本次提问文本（可为空，如纯附件）
   * @param {string} sessionId  会话 ID（占位兜底值）
   */
  function pickSendTitle(knownTitle, userText, sessionId) {
    const known = knownTitle == null ? "" : String(knownTitle).trim();
    // 已知标题与会话 ID 相同视为"没有标题"（后端兜底值，不值得沿用）
    if (known && known !== sessionId) return known;
    return firstQuestionTitle(userText) || sessionId;
  }

  /**
   * 侧边栏会话行点击决策：点击“已完整显示的当前会话 / 正在加载的当前会话”
   * 时跳过重新加载，避免无谓的重复请求与 DOM 重建（视口被拉回底部）。
   *
   * 判定口径：
   *   - 目标 === readySessionId（该会话历史已完整显示在聊天区）→ 跳过；
   *   - 目标 === loadingSessionId（该会话正在加载中）→ 跳过（防并发重复加载）；
   *   - 其余情况（别的会话 / 新对话 / 空值）→ 放行，正常切换。
   *
   * @param {string} clickedId        被点击行的会话 id
   * @param {string} readySessionId   当前视图已就绪的会话 id（无则空）
   * @param {string} loadingSessionId 当前正在加载的会话 id（无则空）
   * @returns {boolean} true = 应执行 openSession；false = 跳过
   */
  function shouldOpenSessionOnClick(clickedId, readySessionId, loadingSessionId) {
    const target = clickedId == null ? "" : String(clickedId).trim();
    // 空 id 属异常数据：维持旧行为（交给 openSession 的兜底规整），不在此拦截
    if (!target) return true;
    const ready = readySessionId == null ? "" : String(readySessionId).trim();
    if (ready && target === ready) return false;
    const loading = loadingSessionId == null ? "" : String(loadingSessionId).trim();
    if (loading && target === loading) return false;
    return true;
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
    pickSendTitle: pickSendTitle,
    shouldOpenSessionOnClick: shouldOpenSessionOnClick,
    sortRows: sortRows,
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  } else {
    global.SessionListUtils = api;
  }
})(typeof self !== "undefined" ? self : globalThis);