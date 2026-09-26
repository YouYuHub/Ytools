/**
 * 选中文本引用到提问（docs/quote_selection_design.md）
 * - 选区检测：聊天正文中「用户提问正文」或「助手回答正文」内的同一条消息选择
 * - 浮钮「引用到提问」：选区末端附近出现，点击把文本快照加入当前会话草稿
 * - 草稿引用入口：输入框上方显示数量按钮，悬停/聚焦展开原文与来源，可跳转/移除
 * - 气泡引用卡片：用户消息气泡内展示（历史回放与实时发送共用）
 *
 * 依赖：app/core.js（state/el/toast 等）、js/quote_utils.js（QuoteUtils 纯逻辑）。
 * 草稿引用存 state.pendingQuotes（与 pendingMedia 同构），随 sessionDrafts 持久。
 */
(function (App) {
  "use strict";

  const { state, el, toast, chatInner, chatScroll, composer, input } = App;
  const QuoteUtils = window.QuoteUtils;

  // 引用入口（输入框上方）：index.html 中位于附件区之前
  const composerQuotes = App.$("#composerQuotes");

  // ---------- 浮钮 ----------
  let floatBtn = null;
  let floatVisible = false;
  let pendingCapture = null; // mousedown 时保留的选区快照（浏览器点击后可能清选区）

  function ensureFloatBtn() {
    if (floatBtn) return floatBtn;
    floatBtn = el("button", "quote-float-btn");
    floatBtn.type = "button";
    floatBtn.innerHTML = '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M7 7h4v4a4 4 0 0 1-4 4H6"/><path d="M15 7h4v4a4 4 0 0 1-4 4h-1"/></svg><span>引用到提问</span>';
    // mousedown 阶段读取并缓存选区：默认行为会先清掉浏览器选区，
    // 在 click 里再读 selection 会拿到空文本
    floatBtn.addEventListener("mousedown", function (e) {
      e.preventDefault();
      e.stopPropagation();
      captureSelection();
    });
    floatBtn.addEventListener("click", function (e) {
      e.preventDefault();
      e.stopPropagation();
      applyCapture();
    });
    document.body.appendChild(floatBtn);
    return floatBtn;
  }

  function showFloatBtn(rect) {
    const btn = ensureFloatBtn();
    btn.classList.remove("hidden");
    floatVisible = true;
    // 先显示再测量宽度，保证钳制计算准确
    const width = btn.offsetWidth || 132;
    const height = btn.offsetHeight || 34;
    let left = rect.left + rect.width / 2;
    let top = rect.top - 8;
    if (top - height < 4) {
      // 选区太靠上（视口顶）：翻转到选区下方
      top = rect.bottom + 8 + height;
    }
    left = Math.max(8 + width / 2, Math.min(left, window.innerWidth - 8 - width / 2));
    btn.style.left = left + "px";
    btn.style.top = top + "px";
  }

  function hideFloatBtn() {
    if (!floatVisible) return;
    floatVisible = false;
    pendingCapture = null;
    if (floatBtn) floatBtn.classList.add("hidden");
  }

  /**
   * 选区归属判定：只在同一条可引用消息（用户提问正文 / 助手回答正文）内
   * 显示入口；输入框、思考过程、工具输出、按钮文字、弹窗等一律不显示。
   */
  function quotableTarget(node) {
    if (!node) return null;
    const element = node.nodeType === 1 ? node : node.parentElement;
    if (!element || typeof element.closest !== "function") return null;
    if (!chatInner.contains(element)) return null;
    // 按钮/操作行内的文字不参与引用（复制、编辑、删除等）
    if (element.closest("button")) return null;
    const assistant = element.closest(".msg-assistant-body");
    if (assistant) return { element: assistant, role: "assistant" };
    const userText = element.closest(".msg-user-text");
    if (userText) return { element: userText, role: "user" };
    return null;
  }

  /** 取可引用元素所属轮次号（历史节点与流式节点都可能带 data-round）。 */
  function roundOf(element) {
    const host = element.closest("[data-round]");
    const raw = host ? host.getAttribute("data-round") : null;
    const number = Number(raw);
    return Number.isFinite(number) && number > 0 ? Math.floor(number) : null;
  }

  /** 历史回放的同轮事件序号，可区分同一轮中多个助手正文块。 */
  function sourceEventOf(element) {
    const host = element.closest("[data-source-event]");
    const raw = host ? Number(host.getAttribute("data-source-event")) : NaN;
    return Number.isInteger(raw) && raw >= 0 ? raw : null;
  }

  /** 读取当前选区（返回 {text, source, rect} 或 null）。 */
  function readSelection() {
    const selection = window.getSelection && window.getSelection();
    if (!selection || selection.isCollapsed || selection.rangeCount < 1) return null;
    const text = QuoteUtils.normalizeQuoteText(selection.toString());
    if (!text) return null;
    const range = selection.getRangeAt(0);
    const start = quotableTarget(range.startContainer);
    const end = quotableTarget(range.endContainer);
    // 起止必须落在同一条可引用消息内（跨消息选择不显示入口）
    if (!start || !end || start.element !== end.element) return null;
    const rect = range.getBoundingClientRect();
    if (!rect || (!rect.width && !rect.height)) return null;
    const source = {
      role: start.role,
      session_id: state.sessionId || undefined,
      round: roundOf(start.element) || undefined,
    };
    const eventIndex = sourceEventOf(start.element);
    if (eventIndex != null) source.event_index = eventIndex;
    return {
      text: text,
      source: source,
      rect: rect,
    };
  }

  function captureSelection() {
    const snapshot = readSelection();
    if (!snapshot) {
      hideFloatBtn();
      return;
    }
    pendingCapture = snapshot;
  }

  function applyCapture() {
    const snapshot = pendingCapture || readSelection();
    hideFloatBtn();
    if (!snapshot) return;
    const added = App.addDraftQuote({ text: snapshot.text, source: snapshot.source });
    if (added) {
      const selection = window.getSelection && window.getSelection();
      if (selection && selection.removeAllRanges) selection.removeAllRanges();
      if (input && typeof input.focus === "function") input.focus();
      toast("已引用到提问（" + QuoteUtils.quotePreview(snapshot.text, 24) + "）");
    }
  }

  /** 选区变化入口：mouseup / keyup（键盘扩选）后统一检查。 */
  function handleSelectionChange() {
    // 浮钮自身的交互（mousedown/click）已单独处理，避免被这里隐藏
    const snapshot = readSelection();
    if (!snapshot) {
      hideFloatBtn();
      return;
    }
    showFloatBtn(snapshot.rect);
  }

  document.addEventListener("mouseup", function (e) {
    if (floatBtn && (e.target === floatBtn || floatBtn.contains(e.target))) return;
    // 延迟到浏览器完成选区更新后判定
    window.setTimeout(handleSelectionChange, 0);
  });
  document.addEventListener("keyup", function (e) {
    if (e.key !== "Shift" && e.key !== "ArrowUp" && e.key !== "ArrowDown"
      && e.key !== "ArrowLeft" && e.key !== "ArrowRight" && e.key !== "End" && e.key !== "Home") {
      return;
    }
    window.setTimeout(handleSelectionChange, 0);
  });
  document.addEventListener("mousedown", function (e) {
    if (floatBtn && (e.target === floatBtn || floatBtn.contains(e.target))) return;
    hideFloatBtn();
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") hideFloatBtn();
  });
  chatScroll.addEventListener("scroll", hideFloatBtn, { passive: true });

  // ---------- 草稿引用（输入框上方紧凑入口 + 原文浮层） ----------
  function renderComposerQuotes() {
    if (!composerQuotes) return;
    const quotes = state.pendingQuotes || [];
    composerQuotes.innerHTML = "";
    if (!quotes.length) {
      composerQuotes.classList.add("hidden");
      composer.classList.remove("has-quotes");
      if (App.syncComposerAttachmentBlocks) App.syncComposerAttachmentBlocks();
      return;
    }

    const tile = el("div", "composer-quotes-tile");
    const trigger = el("button", "composer-quotes-trigger");
    trigger.type = "button";
    trigger.title = "悬停或聚焦查看引用原文";
    trigger.setAttribute("aria-label", "查看 " + quotes.length + " 条引用原文");
    trigger.setAttribute("aria-controls", "composerQuotesPopover");
    trigger.innerHTML = '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 11.5a8.4 8.4 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.4 8.4 0 0 1-3.8-.9L3 21l1.9-5.7a8.4 8.4 0 0 1-.9-3.8A8.5 8.5 0 0 1 8.7 4a8.4 8.4 0 0 1 3.8-.9h.5a8.5 8.5 0 0 1 8 8v.4Z"/><path d="M8 10h8M8 14h5"/></svg>';
    trigger.appendChild(el("span", "composer-quotes-count", String(quotes.length)));
    tile.appendChild(trigger);

    const popover = el("div", "composer-quotes-popover");
    popover.id = "composerQuotesPopover";
    popover.setAttribute("role", "region");
    popover.setAttribute("aria-label", "已添加的引用原文");
    quotes.forEach(function (quote, index) {
      const card = el("div", "composer-quote");
      const jump = el("button", "composer-quote-jump");
      jump.type = "button";
      jump.title = "点击跳转到引用原文";
      jump.setAttribute("aria-label", "跳转到引用 " + (index + 1) + " 的原文");
      jump.addEventListener("click", function () {
        if (App.jumpToQuoteSource) App.jumpToQuoteSource(quote.source, quote.text);
      });
      const head = el("span", "composer-quote-head");
      head.appendChild(el("span", "composer-quote-badge", "引用 " + (index + 1)));
      const label = QuoteUtils.quoteSourceLabel(quote.source);
      if (label) head.appendChild(el("span", "composer-quote-source", label));
      jump.appendChild(head);
      jump.appendChild(el("span", "composer-quote-text", quote.text));
      const remove = el("button", "composer-quote-remove", "×");
      remove.type = "button";
      remove.title = "移除该引用";
      remove.setAttribute("aria-label", "移除引用 " + (index + 1));
      remove.addEventListener("click", function (e) {
        e.stopPropagation();
        App.removeDraftQuote(quote.id);
      });
      card.appendChild(jump);
      card.appendChild(remove);
      popover.appendChild(card);
    });
    tile.appendChild(popover);
    composerQuotes.appendChild(tile);
    composerQuotes.classList.remove("hidden");
    composer.classList.add("has-quotes");
    if (App.syncComposerAttachmentBlocks) App.syncComposerAttachmentBlocks();
  }

  function addDraftQuote(entry) {
    if (!entry || typeof entry !== "object") return false;
    const text = QuoteUtils.normalizeQuoteText(entry.text);
    const check = QuoteUtils.checkQuoteAppend(state.pendingQuotes || [], text);
    if (!check.ok) {
      toast(check.reason);
      return false;
    }
    state.pendingQuotes = state.pendingQuotes || [];
    state.pendingQuotes.push({
      id: "q_" + Date.now().toString(36) + Math.random().toString(36).slice(2, 7),
      text: text,
      source: QuoteUtils.normalizeQuoteSource(entry.source),
    });
    renderComposerQuotes();
    if (App.autosize) App.autosize();
    return true;
  }

  function removeDraftQuote(id) {
    if (!state.pendingQuotes || !state.pendingQuotes.length) return;
    state.pendingQuotes = state.pendingQuotes.filter(function (quote) {
      return quote.id !== id;
    });
    renderComposerQuotes();
    if (App.autosize) App.autosize();
  }

  function clearPendingQuotes() {
    state.pendingQuotes = [];
    renderComposerQuotes();
  }

  // ---------- 气泡引用卡片（历史回放 / 实时发送共用） ----------
  /** 构造用户气泡中的引用卡片列表；无有效引用返回 null。 */
  function buildQuoteList(quotes) {
    const normalized = QuoteUtils.normalizeQuotes(quotes);
    if (!normalized.length) return null;
    const wrap = el("div", "msg-quote-list");
    const tile = el("div", "msg-quote-tile");
    const trigger = el("button", "msg-quote-card");
    trigger.type = "button";
    trigger.title = "悬停或聚焦查看引用原文";
    trigger.setAttribute("aria-label", "查看 " + normalized.length + " 条引用");

    const icon = el("span", "msg-quote-icon");
    icon.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 11.5a8.4 8.4 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.4 8.4 0 0 1-3.8-.9L3 21l1.9-5.7a8.4 8.4 0 0 1-.9-3.8A8.5 8.5 0 0 1 8.7 4a8.4 8.4 0 0 1 3.8-.9h.5a8.5 8.5 0 0 1 8 8v.4Z"/><path d="M8 10h8M8 14h5"/></svg>';
    trigger.appendChild(icon);
    trigger.appendChild(el("span", "msg-quote-index", String(normalized.length)));

    const popover = el("div", "msg-quote-popover");
    popover.setAttribute("role", "region");
    popover.setAttribute("aria-label", "已发送消息中的引用原文");
    normalized.forEach(function (quote, index) {
      const jump = el("button", "msg-quote-jump");
      jump.type = "button";
      jump.title = "跳转到这条引用的原文";
      jump.setAttribute("aria-label", "跳转到引用 " + (index + 1) + " 的原文");
      jump.addEventListener("click", function (event) {
        event.stopPropagation();
        if (App.jumpToQuoteSource) App.jumpToQuoteSource(quote.source, quote.text);
      });
      const head = el("span", "msg-quote-head");
      head.appendChild(el("span", "msg-quote-badge", "引用 " + (index + 1)));
      const label = QuoteUtils.quoteSourceLabel(quote.source);
      if (label) head.appendChild(el("span", "msg-quote-source", label));
      jump.appendChild(head);
      jump.appendChild(el("span", "msg-quote-text", quote.text));
      popover.appendChild(jump);
    });
    tile.appendChild(trigger);
    tile.appendChild(popover);
    wrap.appendChild(tile);
    return wrap;
  }

  // ---------- 导出 ----------
  App.renderComposerQuotes = renderComposerQuotes;
  App.addDraftQuote = addDraftQuote;
  App.removeDraftQuote = removeDraftQuote;
  App.clearPendingQuotes = clearPendingQuotes;
  App.buildQuoteList = buildQuoteList;
  App.hideQuoteFloat = hideFloatBtn;
})(window.App);
