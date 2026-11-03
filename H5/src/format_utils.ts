/** token 用量与模型信息的宽松形态（后端/旧数据可能缺字段或类型不标准；any 保留原强制转换语义）。 */
interface UsageLike {
  prompt_tokens?: any;
  completion_tokens?: any;
  total_tokens?: any;
  cached_tokens?: any;
  compressed_rounds?: any;
  before_tokens?: any;
  after_tokens?: any;
  merge_block_count?: any;
}

interface ModelLike {
  name?: any;
  provider?: any;
}

interface NormalizedUsage {
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  cached_tokens: number;
}

interface FormatUtilsApi {
  fmtNum: (n: unknown) => string;
  fmtTime: (ts: unknown) => string;
  prettyJson: (text: unknown) => string;
  normalizeUsage: (usage: unknown) => NormalizedUsage;
  usageText: (usage: UsageLike, model?: ModelLike | null) => string;
  roundSummaryText: (usage: UsageLike | null | undefined, model?: ModelLike | null) => string;
  compactionUsageText: (usage: unknown) => string;
}

/**
 * 格式化工具（纯逻辑，无 DOM 依赖，可单元测试）
 * - 数字 / 时间 / JSON 展示格式化
 * - token 用量归一化与文案
 * 浏览器挂 window.FormatUtils；Node 下 module.exports。
 */
(function (global: typeof globalThis) {
  "use strict";

  function fmtNum(n: unknown): string {
    return Number(n || 0).toLocaleString("en-US");
  }

  // "2026-08-02 20:34:11" -> "08-02 20:34:11"
  function fmtTime(ts: unknown): string {
    if (!ts || typeof ts !== "string") return "";
    // 实时节点使用 ISO 时间；按浏览器本地时区展示，兼容历史的服务器时间文本。
    if (/^\d{4}-\d{2}-\d{2}T/.test(ts)) {
      const date = new Date(ts);
      if (!Number.isNaN(date.getTime())) {
        const pad = (value: number) => String(value).padStart(2, "0");
        return pad(date.getMonth() + 1) + "-" + pad(date.getDate()) + " " +
          pad(date.getHours()) + ":" + pad(date.getMinutes()) + ":" + pad(date.getSeconds());
      }
    }
    return ts.length > 5 ? ts.slice(5) : ts;
  }

  // 尝试把字符串 JSON 格式化展示；失败原样返回，非字符串则 JSON.stringify
  function prettyJson(text: unknown): string {
    if (typeof text !== "string") return JSON.stringify(text, null, 2);
    try {
      return JSON.stringify(JSON.parse(text), null, 2);
    } catch (_) {
      return text;
    }
  }

  function normalizeUsage(usage: unknown): NormalizedUsage {
    const source = (usage || {}) as UsageLike;
    const promptTokens = Number(source.prompt_tokens || 0);
    const completionTokens = Number(source.completion_tokens || 0);
    const totalTokens = Number(source.total_tokens || promptTokens + completionTokens);
    // 缓存命中是输入的子集：钳制到 [0, prompt]，脏数据不污染展示。
    const cachedTokens = Math.min(Math.max(Number(source.cached_tokens || 0), 0), promptTokens);
    return {
      prompt_tokens: promptTokens,
      completion_tokens: completionTokens,
      total_tokens: totalTokens,
      cached_tokens: cachedTokens,
    };
  }

  function usageText(usage: UsageLike, model?: ModelLike | null): string {
    const p = usage.prompt_tokens || 0;
    const c = usage.completion_tokens || 0;
    const t = usage.total_tokens || p + c;
    const cached = Math.min(Math.max(Number(usage.cached_tokens || 0), 0), Number(p) || 0);
    const modelName = model && typeof model.name === "string" ? model.name.trim() : "";
    const provider = model && typeof model.provider === "string" ? model.provider.trim() : "";
    const label = modelName ? (provider ? provider + "/" + modelName : modelName) : "";
    // 缓存命中占比常常超九成：只展示毛输入会把账单夸大一个数量级，单列缓存。
    const inputPart = cached > 0
      ? "输入 " + fmtNum(p) + "（含缓存 " + fmtNum(cached) + "）"
      : "输入 " + fmtNum(p);
    return "本轮消耗 " + fmtNum(t) + " tokens（" + inputPart + " · 输出 " + fmtNum(c) + "）"
      + (label ? " · 模型 " + label : "");
  }

  // 轮次摘要：仅在服务端实际提供 token 字段时显示用量；Ollama 等未返回
  // usage 的服务仍显示本轮模型，避免把缺失统计误写成 0 tokens。
  function roundSummaryText(usage: UsageLike | null | undefined, model?: ModelLike | null): string {
    const tokenKeys = ["prompt_tokens", "completion_tokens", "total_tokens"];
    const hasUsage = usage && typeof usage === "object" && tokenKeys.some(function (key) {
      if (!Object.prototype.hasOwnProperty.call(usage, key)) return false;
      const value = (usage as Record<string, unknown>)[key];
      return value !== null && value !== "" && Number.isFinite(Number(value));
    });
    if (hasUsage) return usageText(usage as UsageLike, model);

    const modelName = model && typeof model.name === "string" ? model.name.trim() : "";
    const provider = model && typeof model.provider === "string" ? model.provider.trim() : "";
    if (!modelName) return "";
    return "本轮模型 " + (provider ? provider + "/" : "") + modelName;
  }

  function compactionUsageText(usage: unknown): string {
    if (!usage || typeof usage !== "object") return "";
    const data = usage as UsageLike;
    const p = data.prompt_tokens || 0;
    const c = data.completion_tokens || 0;
    const t = data.total_tokens || p + c;
    const parts: string[] = [];
    // "压缩模型输入"限定语义：这是压缩调用本身消耗的输入 tokens（被压缩的
    // 节选文本），不是触发压缩的上下文规模——避免被误读为"触发点偏低"
    //（触发规模看 detail 行的"上下文 X → Y"，X 为触发时实际估算值）
    if (t) parts.push("压缩消耗 " + fmtNum(t) + " tokens（压缩模型输入 " + fmtNum(p) + " · 输出 " + fmtNum(c) + "）");
    if (data.compressed_rounds) parts.push("已压缩 " + fmtNum(data.compressed_rounds) + " 个旧轮次");
    if (data.before_tokens !== undefined && data.after_tokens !== undefined) {
      parts.push("上下文 " + fmtNum(data.before_tokens) + " → " + fmtNum(data.after_tokens) + " tokens");
    }
    if (data.merge_block_count) parts.push("合并 " + fmtNum(data.merge_block_count) + " 个摘要块");
    return parts.join(" · ");
  }

  const api: FormatUtilsApi = {
    fmtNum: fmtNum,
    fmtTime: fmtTime,
    prettyJson: prettyJson,
    normalizeUsage: normalizeUsage,
    usageText: usageText,
    roundSummaryText: roundSummaryText,
    compactionUsageText: compactionUsageText,
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  } else {
    (global as unknown as { FormatUtils: FormatUtilsApi }).FormatUtils = api;
  }
})(typeof self !== "undefined" ? self : globalThis);
