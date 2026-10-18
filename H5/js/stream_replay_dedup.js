(function (global) {
  "use strict";
  // 附接回放去重：纯 DOM 查询 + 顺序匹配器。
  // 后端重启/刷新后附接运行时流，buffer 会重放历史已渲染过的正文、思考
  // 与工具调用；流管线在回放窗口内用这里的 helpers 找到历史节点：
  // 工具按 callId 收养（复用其 ui 控制器），正文/思考按同轮文本比对命中
  // 则丢弃重放帧。匹配不到一律按正常新建处理，不删除任何已有节点。
  function escapeAttr(value) {
    if (typeof CSS !== "undefined" && CSS.escape) return CSS.escape(String(value));
    return String(value).replace(/["\\]/g, "\\$&");
  }
  function inside(node, root) {
    return Boolean(node && root && (node === root || (root.contains ? root.contains(node) : false)));
  }
  // 同 callId 的历史工具卡（排除管线自己的新容器，避免收养自己）。
  function findHistoryToolWrap(chatInner, msg, callId) {
    if (!chatInner || !callId || typeof chatInner.querySelector !== "function") return null;
    const wrap = chatInner.querySelector('.tool-block[data-tool-call-id="' + escapeAttr(callId) + '"]');
    if (!wrap || inside(wrap, msg)) return null;
    return wrap;
  }
  function collectRoundNodes(chatInner, msg, round, selector) {
    if (!chatInner || round == null || typeof chatInner.querySelectorAll !== "function") return [];
    const out = [];
    const nodes = chatInner.querySelectorAll(selector + '[data-round="' + round + '"]');
    for (const node of nodes) {
      if (inside(node, msg)) continue;
      out.push(node);
    }
    return out;
  }
  // 同轮历史正文块（DOM 顺序）。
  function collectHistoryTexts(chatInner, msg, round) {
    return collectRoundNodes(chatInner, msg, round, ".msg-assistant-body");
  }
  // 同轮历史思考内容区（DOM 顺序，直接返回 .think-content 元素）。
  function collectHistoryThinks(chatInner, msg, round) {
    const out = [];
    for (const wrap of collectRoundNodes(chatInner, msg, round, ".think-block")) {
      const content = wrap.querySelector ? wrap.querySelector(".think-content") : null;
      out.push(content || wrap);
    }
    return out;
  }
  // 源码渲染后的文本是否与历史节点一致（离屏渲染比对，不碰 DOM）。
  function renderedSourceEquals(Markdown, el, source, nodeText) {
    const probe = el("div");
    probe.innerHTML = Markdown.render(source);
    return (probe.textContent || "").trim() === String(nodeText || "").trim();
  }
  // 顺序消费匹配器：回放与历史同源同序；命中即消费，避免原文重复的
  // 多段文本互相误配（后到达的相同文本找不到未消费项时会正常新建）。
  function createSequentialMatcher() {
    const consumed = new Set();
    return {
      match: function (candidates, equals) {
        for (const node of candidates) {
          if (consumed.has(node)) continue;
          if (equals(node)) {
            consumed.add(node);
            return node;
          }
        }
        return null;
      },
      reset: function () { consumed.clear(); },
    };
  }
  const exported = {
    findHistoryToolWrap: findHistoryToolWrap,
    collectHistoryTexts: collectHistoryTexts,
    collectHistoryThinks: collectHistoryThinks,
    renderedSourceEquals: renderedSourceEquals,
    createSequentialMatcher: createSequentialMatcher,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = exported;
  else global.StreamReplayDedup = exported;
})(typeof window !== "undefined" ? window : globalThis);
