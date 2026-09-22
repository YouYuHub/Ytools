/*
 * widget_reuse.js — md-svg 控件复用配对决策（纯逻辑，无 DOM 依赖）
 *
 * 背景：流式渲染按 Markdown.render 全量重建正文，```svg/mermaid/canvas 控件
 * 会随节点销毁重建（代码视图滚动位置丢失、运行中的 canvas iframe 被丢弃）。
 * messages.js 的 renderPreservingWidgets 用本模块做配对决策，再把命中的旧
 * DOM 节点整块移植进新树。
 *
 * 配对规则（与渲染层语义一致）：
 *   1) kind + 源码完全一致 → 精确配对优先（应对插入/删除导致的文档位移）；
 *   2) svg/canvas 允许"旧源码是新源码前缀"的流式成长配对（新文档第 k 个对
 *      旧第 k 个，kind 相同且旧块未消费）；mermaid 不走前缀分支——其图片
 *      视图是异步回填渲染，源码变了必须重渲染，重建成本低；
 *   3) canvas 特例：运行中的沙箱 iframe（hasFrame=true）即使源码被改掉也
 *      尽量保住（用户点过确认的沙箱很贵）——只认最后一个未消费的含帧块。
 *
 * 输入：oldItems/newItems 为 {kind, code, hasFrame?} 描述数组（文档顺序）。
 * 输出：plan 数组（长度 = newItems.length），plan[k].reuseIndex 为命中的
 *       oldItems 下标，-1 表示无命中（新建）。
 */
(function (global) {
  "use strict";

  function planWidgetReuse(oldItems, newItems) {
    const olds = Array.isArray(oldItems) ? oldItems : [];
    const news = Array.isArray(newItems) ? newItems : [];
    const plan = new Array(news.length);
    const consumed = new Array(olds.length).fill(false);
    for (let k = 0; k < news.length; k++) plan[k] = { reuseIndex: -1 };
    if (!olds.length || !news.length) return plan;

    let remaining = olds.length;
    // 1) 精确配对：kind + code 完全一致
    for (let k = 0; k < news.length && remaining > 0; k++) {
      if (plan[k].reuseIndex >= 0) continue;
      const nKind = String(news[k].kind || "");
      const nCode = String(news[k].code || "");
      for (let j = 0; j < olds.length; j++) {
        if (consumed[j]) continue;
        const o = olds[j];
        if (String(o.kind || "") === nKind && String(o.code || "") === nCode) {
          plan[k].reuseIndex = j;
          consumed[j] = true;
          remaining--;
          break;
        }
      }
    }
    // 2) 流式成长前缀：svg/canvas（不含 mermaid），新第 k 个对旧第 k 个
    for (let k = 0; k < news.length && remaining > 0; k++) {
      if (plan[k].reuseIndex >= 0) continue;
      const nKind = String(news[k].kind || "");
      if (nKind !== "svg" && nKind !== "canvas") continue;
      if (k >= olds.length) continue;
      const o = olds[k];
      if (consumed[k]) continue;
      if (String(o.kind || "") !== nKind) continue;
      const oc = String(o.code || "");
      const nc = String(news[k].code || "");
      if (nc && oc && nc.indexOf(oc) === 0) {
        plan[k].reuseIndex = k;
        consumed[k] = true;
        remaining--;
      }
    }
    // 3) 运行中的 canvas iframe：源码不匹配也尽量复用最后一个含帧块
    for (let k = 0; k < news.length && remaining > 0; k++) {
      if (plan[k].reuseIndex >= 0) continue;
      if (String(news[k].kind || "") !== "canvas") continue;
      for (let j = olds.length - 1; j >= 0; j--) {
        if (consumed[j]) continue;
        if (String(olds[j].kind || "") !== "canvas") continue;
        if (olds[j].hasFrame) {
          plan[k].reuseIndex = j;
          consumed[j] = true;
          remaining--;
          break;
        }
      }
    }
    return plan;
  }

  const plan = planWidgetReuse;
  const api = { plan: plan, planWidgetReuse: plan };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  } else {
    global.WidgetReuse = api;
  }
})(typeof self !== "undefined" ? self : globalThis);
