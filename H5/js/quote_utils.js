/**
 * 引用（选中文本引用到提问）的纯逻辑工具：规整、限额校验、预览与复制文本。
 * 无 DOM 依赖，可单元测试；浏览器挂 window.QuoteUtils，Node 下 module.exports。
 *
 * 限额与后端 memory/quote_format.py 保持一致（前后端同一口径）：
 * 最多 5 段、单段 ≤4000 字符、合计 ≤12000 字符。
 */
(function (global) {
  "use strict";

  const QUOTE_LIMITS = {
    maxQuotes: 5,
    maxQuoteChars: 4000,
    maxTotalChars: 12000,
  };

  /** 规整单段引用文本：统一换行符为 \n，去掉首尾空白（保留内部空白/换行）。 */
  function normalizeQuoteText(value) {
    if (typeof value !== "string") return "";
    return value.replace(/\r\n/g, "\n").replace(/\r/g, "\n").trim();
  }

  /** 规整来源信息（仅用于卡片提示；角色白名单 user/assistant）。 */
  function normalizeQuoteSource(value) {
    if (!value || typeof value !== "object") return null;
    const source = {};
    if (value.role === "user" || value.role === "assistant") source.role = value.role;
    if (typeof value.session_id === "string" && value.session_id.trim()) {
      source.session_id = value.session_id.trim();
    }
    const round = Number(value.round);
    if (Number.isFinite(round) && round > 0) source.round = Math.floor(round);
    const eventIndex = value.event_index;
    if (Number.isInteger(eventIndex) && eventIndex >= 0) source.event_index = eventIndex;
    return Object.keys(source).length ? source : null;
  }

  /**
   * 规整引用数组（与后端 normalize_quotes 同规则）：
   * 跳过非法项、超量截断、超长裁剪、合计超限截断——前端草稿路径不做拒绝，
   * 保证本地始终能渲染与发送（发送前的严格校验由后端执行）。
   */
  function normalizeQuotes(raw) {
    if (!Array.isArray(raw)) return [];
    const result = [];
    let totalChars = 0;
    raw.forEach(function (item) {
      if (result.length >= QUOTE_LIMITS.maxQuotes) return;
      if (!item || typeof item !== "object") return;
      let text = normalizeQuoteText(item.text);
      if (!text) return;
      if (text.length > QUOTE_LIMITS.maxQuoteChars) {
        text = text.slice(0, QUOTE_LIMITS.maxQuoteChars);
      }
      if (totalChars + text.length > QUOTE_LIMITS.maxTotalChars) return;
      totalChars += text.length;
      const entry = { text: text };
      if (typeof item.id === "string" && item.id) entry.id = item.id;
      const source = normalizeQuoteSource(item.source);
      if (source) entry.source = source;
      result.push(entry);
    });
    return result;
  }

  /**
   * 校验一组引用是否可以加入草稿（前端本地限额提示用）。
   * @returns {{ok: boolean, reason?: string}}
   */
  function checkQuoteAppend(existing, text) {
    const normalized = normalizeQuoteText(text);
    if (!normalized) return { ok: false, reason: "选中内容为空" };
    if ((existing || []).length >= QUOTE_LIMITS.maxQuotes) {
      return { ok: false, reason: "最多引用 " + QUOTE_LIMITS.maxQuotes + " 段" };
    }
    if (normalized.length > QUOTE_LIMITS.maxQuoteChars) {
      return {
        ok: false,
        reason: "单段引用最多 " + QUOTE_LIMITS.maxQuoteChars + " 个字符（当前 "
          + normalized.length + " 个）",
      };
    }
    const total = (existing || []).reduce(function (sum, item) {
      return sum + normalizeQuoteText(item && item.text).length;
    }, 0);
    if (total + normalized.length > QUOTE_LIMITS.maxTotalChars) {
      return {
        ok: false,
        reason: "引用合计最多 " + QUOTE_LIMITS.maxTotalChars + " 个字符",
      };
    }
    return { ok: true };
  }

  /** 卡片/按钮短预览：折叠连续空白、超长截断加省略号。 */
  function quotePreview(text, maxLength) {
    const limit = Number.isFinite(maxLength) && maxLength > 0 ? Math.floor(maxLength) : 80;
    const normalized = normalizeQuoteText(text).replace(/\s+/g, " ");
    if (normalized.length <= limit) return normalized;
    return normalized.slice(0, limit) + "…";
  }

  /** 来源提示文案：如「来自助手 · 第 12 轮」「来自用户」。 */
  function quoteSourceLabel(source) {
    if (!source || typeof source !== "object") return "";
    const roleText = source.role === "user" ? "来自用户"
      : source.role === "assistant" ? "来自助手" : "";
    const round = Number(source.round);
    const roundText = Number.isFinite(round) && round > 0 ? "第 " + Math.floor(round) + " 轮" : "";
    if (roleText && roundText) return roleText + " · " + roundText;
    return roleText || roundText;
  }

  /**
   * 复制消息的人可读文本（不输出协议标签）：
   * 「引用 1：…\n\n引用 2：…\n\n问题：…」（无引用时仅返回问题正文）。
   */
  function composeCopyText(quotes, questionText) {
    const normalized = normalizeQuotes(quotes);
    const question = typeof questionText === "string" ? questionText : "";
    if (!normalized.length) return question;
    const lines = normalized.map(function (quote, index) {
      return "引用 " + (index + 1) + "：" + quote.text;
    });
    if (question) lines.push("问题：" + question);
    return lines.join("\n\n");
  }

  /** 草稿消息的引用快照（发送/暂存共用）：去掉本地 UI 字段 id。 */
  function quotesForRequest(quotes) {
    return normalizeQuotes(quotes).map(function (quote) {
      const entry = { text: quote.text };
      if (quote.source) entry.source = quote.source;
      return entry;
    });
  }

  const api = {
    QUOTE_LIMITS: QUOTE_LIMITS,
    normalizeQuoteText: normalizeQuoteText,
    normalizeQuoteSource: normalizeQuoteSource,
    normalizeQuotes: normalizeQuotes,
    checkQuoteAppend: checkQuoteAppend,
    quotePreview: quotePreview,
    quoteSourceLabel: quoteSourceLabel,
    composeCopyText: composeCopyText,
    quotesForRequest: quotesForRequest,
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  } else {
    global.QuoteUtils = api;
  }
})(typeof self !== "undefined" ? self : globalThis);
