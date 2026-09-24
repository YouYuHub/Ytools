/**
 * 三控件（```mermaid / ```svg / ```canvas）显示缩放纯逻辑模块
 *
 * 设计口径：缩放 = 「基准显示尺寸 × 档位倍率」。
 * - 基准显示尺寸：图片/画布在 100% 档位的自适应显示大小（svg 由 viewBox 比例
 *   决定、canvas 为 100% 宽 × 420px 高、全屏为铺满区域）；
 * - 缩放实现走 CSS 变量 --md-zoom（svg 宽度表达式 / canvas iframe 宽高都写成
 *   calc(... * var(--md-zoom, 1))），本模块只负责「档位/百分比/步进」的纯计算，
 *   DOM 读写与滚动容器由 messages.js 接线。
 *
 * 档位为固定数组（等比与整数兼顾，50%~800%）：缩小用于总览超宽大图，
 * 放大用于复杂流程图/细密节点的局部查看。
 * UMD 双形态：浏览器挂 window.ZoomUtils，Node 侧可 require 做单测
 * （与 session_list_utils.js / widget_reuse.js 同风格）。
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    root.ZoomUtils = factory();
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  // 缩放档位（百分比），严格单调递增；100 为基准档（默认显示）
  const ZOOM_STEPS = [50, 67, 75, 90, 100, 110, 125, 150, 175, 200, 250, 300, 400, 500, 600, 800];
  const MIN_PERCENT = ZOOM_STEPS[0];
  const MAX_PERCENT = ZOOM_STEPS[ZOOM_STEPS.length - 1];
  const BASE_PERCENT = 100;

  // 倍率 → 整数百分比（非法/非正输入按基准 100 处理）
  function percentFromScale(scale) {
    const s = Number(scale);
    if (!Number.isFinite(s) || s <= 0) return BASE_PERCENT;
    return Math.round(s * 100);
  }

  // 找与目标百分比最接近的档位（同距取较小档，保证确定性）
  function nearestStep(percent) {
    let best = ZOOM_STEPS[0];
    let bestDist = Infinity;
    for (let i = 0; i < ZOOM_STEPS.length; i++) {
      const dist = Math.abs(ZOOM_STEPS[i] - percent);
      if (dist < bestDist) {
        bestDist = dist;
        best = ZOOM_STEPS[i];
      }
    }
    return best;
  }

  // 任意数值 → 合法档位百分比：非法归 100、越界钳制到 [50, 800]、再吸附最近档
  function normalizePercent(value) {
    let p = Number(value);
    if (!Number.isFinite(p) || p <= 0) return BASE_PERCENT;
    p = Math.max(MIN_PERCENT, Math.min(MAX_PERCENT, Math.round(p)));
    return nearestStep(p);
  }

  // 任意数值 → 合法档位倍率（供 --md-zoom 使用）
  function normalizeScale(scale) {
    return normalizePercent(percentFromScale(scale)) / 100;
  }

  // 相邻档位步进：dir > 0 放大一档、否则缩小一档；已在边界则保持不动
  function stepScale(scale, dir) {
    const percent = normalizePercent(percentFromScale(scale));
    const idx = ZOOM_STEPS.indexOf(percent);
    const base = idx < 0 ? ZOOM_STEPS.indexOf(BASE_PERCENT) : idx;
    const next = Math.max(0, Math.min(ZOOM_STEPS.length - 1, base + (dir > 0 ? 1 : -1)));
    return ZOOM_STEPS[next] / 100;
  }

  // 该方向上是否还能继续步进（供按钮 disabled 状态）
  function canStep(scale, dir) {
    const percent = normalizePercent(percentFromScale(scale));
    return dir > 0 ? percent < MAX_PERCENT : percent > MIN_PERCENT;
  }

  return {
    ZOOM_STEPS: ZOOM_STEPS,
    MIN_PERCENT: MIN_PERCENT,
    MAX_PERCENT: MAX_PERCENT,
    BASE_PERCENT: BASE_PERCENT,
    percentFromScale: percentFromScale,
    normalizePercent: normalizePercent,
    normalizeScale: normalizeScale,
    stepScale: stepScale,
    canStep: canStep,
  };
});
