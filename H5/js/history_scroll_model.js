/** 完整历史的滚动高度索引；只为未渲染记录估高，不构建其 Markdown/工具 DOM。 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.HistoryScrollModel = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";
  function estimate(record, width) {
    if (!record || record.kind === "approval" || record.kind === "toolResult") return 0;
    if (record.kind === "tool" || record.kind === "think" || record.kind === "compaction") return 112;
    if (record.kind === "usage") return 24;
    const content = record.content;
    const text = typeof content === "string" ? content : Array.isArray(content)
      ? content.map(part => typeof part === "string" ? part : part && part.text || "").filter(Boolean).join("\n") : "";
    const media = Array.isArray(content) ? content.filter(part => part && /image|video|audio|file/.test(part.type||"")).length : 0;
    const columns = Math.max(20, Math.floor((width || 800) / 10));
    const lines = Math.max(1, Math.ceil(text.length / columns), (text.match(/\n/g) || []).length + 1);
    // 常量按真实渲染校准（1300px 视口实测：用户气泡 chrome 约 86px、助手正文
    // chrome 约 38px、行高约 22px；旧值 100/64/26 系统性偏大约 1.3–1.9 倍，
    // 导致窗口覆盖像素不足、滚动中频繁重建窗口并跳动滚动条）。
    return (record.kind === "user" ? 72 : 28) + media*128 + Math.min(2000000, lines * 22);
  }
  function prefix(heights) {
    const result = new Float64Array(heights.length + 1);
    for (let i = 0; i < heights.length; i++) result[i+1] = result[i] + Math.max(0, Number(heights[i]) || 0);
    return result;
  }
  function locate(offsets, position) {
    let low = 0, high = offsets.length - 1;
    while (low < high) {
      const mid = Math.ceil((low + high) / 2);
      if (offsets[mid] <= position) low = mid; else high = mid - 1;
    }
    return Math.min(Math.max(0, offsets.length - 2), low);
  }
  // 在固定滚动范围中放置真实渲染窗口。少数超大消息超过整个估算范围时，
  // 用负的绘制位移保留当前所读部分，仍不改变按住滑块期间的 scrollHeight。
  function fit(total, top, bottom, height, desiredPrefix) {
    const room = total - top - bottom - height;
    const origin = room >= 0 ? Math.max(0, Math.min(room, desiredPrefix)) : desiredPrefix;
    return {before:Math.max(0, origin), after:Math.max(0, room-origin), shift:Math.min(0, origin)};
  }
  return {estimate, prefix, locate, fit};
});
