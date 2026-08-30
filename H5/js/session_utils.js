/**
 * 会话标识工具（无 DOM 依赖，可单元测试）
 * - 会话标识以文件名为准：`<session_id>_chat.jsonl`，与后端 normalize_session_id 保持一致
 * - 新建会话 ID 格式：yt-<系统时间戳 YYYY-MM-DD_HH.MM.SS>_<三位随机数>，如 yt-2026-08-26_14.30.05_042
 * 浏览器挂 window.SessionUtils；Node 下 module.exports。
 */
(function (global) {
  "use strict";

  function sanitizeSessionId(raw) {
    let value = String(raw == null ? "" : raw).trim();
    // 兼容直接传入文件名（如 yt-xxx_chat.jsonl / yt-xxx.jsonl）
    value = value.replace(/_chat\.jsonl$/i, "").replace(/\.jsonl$/i, "");
    // 无损保留 Unicode 字母/数字（含中文）、空格、下划线、点、横线；
    // 仅把路径分隔符等危险字符替换为下划线，并拦截 ".." 防止路径穿越
    value = value.replace(/[^\p{L}\p{N}\p{M} _.-]+/gu, "_").replace(/\.\./g, "_").replace(/^[ _.-]+|[ _.-]+$/g, "");
    return value || "default";
  }

  function pad2(n) {
    return String(n).padStart(2, "0");
  }

  // 生成新会话 ID：yt-2000-01-01_00.00.00_abc
  // yt 固定前缀；中间为真实系统时间戳（YYYY-MM-DD_HH.MM.SS）；末尾为三位随机数
  function generateSessionId(now) {
    const d = now instanceof Date ? now : new Date();
    const timestamp = d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate())
      + "_" + pad2(d.getHours()) + "." + pad2(d.getMinutes()) + "." + pad2(d.getSeconds());
    const rand = String(Math.floor(Math.random() * 1000)).padStart(3, "0");
    return "yt-" + timestamp + "_" + rand;
  }

  function fileNameToSessionId(name) {
    return sanitizeSessionId(name);
  }

  // 会话标题优先使用本地重命名覆盖（由调用方管理 localStorage），否则取后端标题
  function getSessionTitle(sessionId, fallback) {
    return fallback;
  }

  const api = {
    sanitizeSessionId: sanitizeSessionId,
    generateSessionId: generateSessionId,
    fileNameToSessionId: fileNameToSessionId,
    getSessionTitle: getSessionTitle,
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  } else {
    global.SessionUtils = api;
  }
})(typeof self !== "undefined" ? self : globalThis);