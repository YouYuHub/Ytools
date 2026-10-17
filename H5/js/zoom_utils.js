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

  // ---------- 预览弹窗平移缩放（pan/zoom）几何 ----------
  // 约定与 media.js 的 transform 写法对应：translate(tx,ty) scale(s)，
  // 位移取屏幕像素（不受倍率影响），缩放中心为容器中心。

  // 以锚点为中心的缩放：返回 {scale, tx, ty}。
  // (ax,ay) 锚点（鼠标）在容器中的坐标，(cx,cy) 容器中心，
  // (tx1,ty1) 当前位移；min/max 为倍率边界（缺省 0.5~8，与档位对齐）。
  // 锚点传 null 时以中心缩放（位移不变）。
  function zoomAtPoint(scale1, factor, ax, ay, cx, cy, tx1, ty1, min, max) {
    const lo = Number.isFinite(min) && min > 0 ? min : MIN_PERCENT / 100;
    const hi = Number.isFinite(max) && max > lo ? max : MAX_PERCENT / 100;
    const s1 = Number.isFinite(scale1) && scale1 > 0 ? scale1 : 1;
    const f = Number.isFinite(factor) && factor > 0 ? factor : 1;
    const s2 = Math.max(lo, Math.min(hi, s1 * f));
    const x1 = Number.isFinite(tx1) ? tx1 : 0;
    const y1 = Number.isFinite(ty1) ? ty1 : 0;
    if (s2 === s1 || ax == null || ay == null) return { scale: s2, tx: x1, ty: y1 };
    // 保持锚点下的图像点不动：T2 = T1 + (A - C - T1) * (1 - s2/s1)
    const k = 1 - s2 / s1;
    return { scale: s2, tx: x1 + (ax - cx - x1) * k, ty: y1 + (ay - cy - y1) * k };
  }

  // 位移钳制：图像边缘不超过容器中心（拖不出视野）；
  // 图像小于容器时位移归零。返回 {tx, ty}。
  function clampPan(tx, ty, scaledW, scaledH, contW, contH) {
    const maxTx = Math.max(0, (scaledW - contW) / 2);
    const maxTy = Math.max(0, (scaledH - contH) / 2);
    const x = Number.isFinite(tx) ? tx : 0;
    const y = Number.isFinite(ty) ? ty : 0;
    return {
      tx: Math.min(maxTx, Math.max(-maxTx, x)),
      ty: Math.min(maxTy, Math.max(-maxTy, y)),
    };
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
    zoomAtPoint: zoomAtPoint,
    clampPan: clampPan,
  };
});
