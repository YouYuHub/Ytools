/**
 * 格式化工具（纯逻辑，无 DOM 依赖，可单元测试）
 * - 数字 / 时间 / JSON 展示格式化
 * - token 用量归一化与文案
 * 浏览器挂 window.FormatUtils；Node 下 module.exports。
 */
(function (global) {
  "use strict";

  function fmtNum(n) {
    return Number(n || 0).toLocaleString("en-US");
  }

  // "2026-08-02 20:34:11" -> "08-02 20:34:11"
  function fmtTime(ts) {
    if (!ts || typeof ts !== "string") return "";
    return ts.length > 5 ? ts.slice(5) : ts;
  }

  // 尝试把字符串 JSON 格式化展示；失败原样返回，非字符串则 JSON.stringify
  function prettyJson(text) {
    if (typeof text !== "string") return JSON.stringify(text, null, 2);
    try {
      return JSON.stringify(JSON.parse(text), null, 2);
    } catch (_) {
      return text;
    }
  }

  function normalizeUsage(usage) {
    const source = usage || {};
    const promptTokens = Number(source.prompt_tokens || 0);
    const completionTokens = Number(source.completion_tokens || 0);
    const totalTokens = Number(source.total_tokens || promptTokens + completionTokens);
    return {
      prompt_tokens: promptTokens,
      completion_tokens: completionTokens,
      total_tokens: totalTokens,
    };
  }

  function usageText(usage) {
    const p = usage.prompt_tokens || 0;
    const c = usage.completion_tokens || 0;
    const t = usage.total_tokens || p + c;
    return "本轮消耗 " + fmtNum(t) + " tokens（输入 " + fmtNum(p) + " · 输出 " + fmtNum(c) + "）";
  }

  function compactionUsageText(usage) {
    if (!usage || typeof usage !== "object") return "";
    const p = usage.prompt_tokens || 0;
    const c = usage.completion_tokens || 0;
    const t = usage.total_tokens || p + c;
    const parts = [];
    // "压缩模型输入"限定语义：这是压缩调用本身消耗的输入 tokens（被压缩的
    // 节选文本），不是触发压缩的上下文规模——避免被误读为"触发点偏低"
    //（触发规模看 detail 行的"上下文 X → Y"，X 为触发时实际估算值）
    if (t) parts.push("压缩消耗 " + fmtNum(t) + " tokens（压缩模型输入 " + fmtNum(p) + " · 输出 " + fmtNum(c) + "）");
    if (usage.compressed_rounds) parts.push("已压缩 " + fmtNum(usage.compressed_rounds) + " 个旧轮次");
    if (usage.before_tokens !== undefined && usage.after_tokens !== undefined) {
      parts.push("上下文 " + fmtNum(usage.before_tokens) + " → " + fmtNum(usage.after_tokens) + " tokens");
    }
    if (usage.merge_block_count) parts.push("合并 " + fmtNum(usage.merge_block_count) + " 个摘要块");
    return parts.join(" · ");
  }

  const api = {
    fmtNum: fmtNum,
    fmtTime: fmtTime,
    prettyJson: prettyJson,
    normalizeUsage: normalizeUsage,
    usageText: usageText,
    compactionUsageText: compactionUsageText,
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  } else {
    global.FormatUtils = api;
  }
})(typeof self !== "undefined" ? self : globalThis);