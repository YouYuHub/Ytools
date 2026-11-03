/**
 * 右侧问题导航/引用定位/滚动与代码块复制钉（原 messages.js 4384-5199 行拆出）
 * 依赖：app/core.js；跨文件调用一律经 App.*（运行时解析，defer 顺序加载保证可用）。
 */
(function (App: any) {
  "use strict";
  const {
    el, state, toast, scrollToBottom, chatScroll, chatInner, qnav, qnavRail, qnavPanel, QNAV_MAX_DASHES, scrollBottomBtn
  } = App;

  // ---------- 右侧问题导航 ----------
  let qnavUsers: any = [];
  let qnavEntries: any = [];
  let qnavRippleBound = false;

  function clearQnavRipple() {
    qnavRail.querySelectorAll(".qnav-dash").forEach(function(d: any) {
      d.classList.remove("near-1", "near-2");
    });
    qnavPanel.querySelectorAll(".qnav-item").forEach(function(item: any) {
      item.classList.remove("peek");
    });
  }

  function rebuildQnav() {
    const panelTop = qnavPanel.scrollTop || 0;
    const followPanel = qnavPanel.clientHeight > 0 && qnavPanel.scrollHeight - panelTop - qnavPanel.clientHeight <= 2;
    const rendered = Array.from(chatInner.querySelectorAll(".msg-user"));
    const records = typeof App.loadOlderState !== "undefined" && App.loadOlderState.records;
    const byIndex = new Map(rendered.filter(function(node: any) { return node.dataset.historyIndex != null; })
      .map(function(node: any) { return [Number(node.dataset.historyIndex), node]; }));
    const byEvent = new Map(rendered.map(function(node: any) {
      return [node.dataset.round + ":" + (node.dataset.sourceEvent || ""), node];
    }));
    // 同一后端轮次可以包含多条用户引导，不能仅凭 round 去重。
    // 无历史戳的气泡按轮次和内容匹配快照，未匹配的实时问题全部保留。
    const byRoundUnstamped = new Map();
    function bubbleText(node: any) {
      const bubble = node.querySelector(".msg-bubble");
      return bubble ? bubble.textContent : "";
    }
    rendered.forEach(function(node: any) {
      if (node.dataset.historyIndex == null && node.dataset.round != null) {
        const round = Number(node.dataset.round);
        if (!byRoundUnstamped.has(round)) byRoundUnstamped.set(round, []);
        byRoundUnstamped.get(round).push(node);
      }
    });
    const matched = new Set(), seenRounds = new Set();
    qnavEntries = [];
    if (records) records.forEach(function(rec: any, index: any) {
      if (rec.kind !== "user") return;
      const text = App.contentToPlainText(rec.content);
      const candidates = byRoundUnstamped.get(Number(rec.round)) || [];
      const replacement = !seenRounds.has(Number(rec.round)) && candidates.find(function(candidate: any) { return Number(candidate.dataset.replacesRound) === Number(rec.round); });
      seenRounds.add(Number(rec.round));
      const node = replacement || byIndex.get(index) || (rec.source_event_index != null && byEvent.get(String(rec.round) + ":" + rec.source_event_index))
        || candidates.find(function(candidate: any) { return !matched.has(candidate) && bubbleText(candidate).trim() === text.trim(); }) || null;
      if (node) matched.add(node);
      qnavEntries.push({ index: index, round: Number(rec.round), eventIndex:rec.source_event_index, text: replacement ? bubbleText(node) : text, node: node });
    });
    rendered.forEach(function(node: any) {
      if (matched.has(node)) return;
      const round = Number(node.dataset.round);
      const entry = { index: -1, round: round, text: bubbleText(node), node: node };
      const next = Number.isFinite(round) ? qnavEntries.findIndex(function(item: any) { return item.round > round; }) : -1;
      if (next < 0) qnavEntries.push(entry); else qnavEntries.splice(next, 0, entry);
    });
    if (typeof App !== "undefined" && App.historyPages) qnavEntries = App.historyPages.navigation(qnavEntries);
    qnavUsers = qnavEntries.map(function(entry: any) { return entry.node; });
    qnavRail.innerHTML = "";
    qnavPanel.innerHTML = "";
    if (qnavUsers.length < 2) {
      qnav.classList.add("hidden");
      return;
    }
    qnav.classList.remove("hidden");

    const total = qnavUsers.length;
    // 指示条均匀采样：问题数超过上限时不再截断（此前超过 40 条的完全不显示），
    // 而是把全部问题均匀映射到 QNAV_MAX_DASHES 条指示条上——
    // 第 j 条负责问题段 [floor(j*n/D), floor((j+1)*n/D))，点击跳到段首问题；
    // 问题数不超过上限时一一对应，行为与原先完全一致。面板列表仍列出全部问题。
    const dashCount = Math.min(QNAV_MAX_DASHES, total);
    const dashes: any = [];
    for (let j = 0; j < dashCount; j++) {
      const start = Math.floor(j * total / dashCount);
      const end = Math.floor((j + 1) * total / dashCount);
      let text = qnavEntries[start].text || "问题 " + (start + 1);
      // 一条指示条对应多个问题时，标题标注问题序号区间
      if (end - start > 1) text = (start + 1) + "-" + end + ". " + text;
      const dash = el("button", "qnav-dash");
      dash.title = text;
      dash.addEventListener("click", function () { jumpToQuestion(start); });
      dashes.push(dash);
      qnavRail.appendChild(dash);
    }
    // hover 涟漪：所在条最长（CSS :hover），相邻按距离加 near-1/near-2 渐短；
    // 联动面板段首问题项高亮 peek。dash 随 rail 重建而销毁，监听不泄漏。
    dashes.forEach(function(dash: any, j: any) {
      dash.addEventListener("mouseenter", function () {
        dashes.forEach(function(other: any, k: any) {
          const dist = Math.abs(k - j);
          other.classList.remove("near-1", "near-2");
          if (dist === 1) other.classList.add("near-1");
          else if (dist === 2) other.classList.add("near-2");
        });
        const segStart = Math.floor(j * total / dashCount);
        qnavPanel.querySelectorAll(".qnav-item").forEach(function(item: any, i: any) {
          if (i === segStart) item.classList.add("peek");
          else item.classList.remove("peek");
        });
      });
    });
    // rail/panel 是持久元素：以下监听只绑定一次，回调查实时 DOM（不闭包旧 dashes）
    if (!qnavRippleBound) {
      qnavRippleBound = true;
      qnavRail.addEventListener("mouseleave", clearQnavRipple);
      // 轨道空隙/内边距上移动：无选中，清除残留涟漪（面板照常展开，
      // 只剩 active 当前位置高亮）；回到横条上由 mouseenter 重新点亮
      qnavRail.addEventListener("mousemove", function(e: any) {
        const target = e && e.target;
        const onDash = target && target.closest ? target.closest(".qnav-dash") : null;
        if (!onDash || !qnavRail.contains(onDash)) clearQnavRipple();
      });
      // 面板浏览时轨道不抢焦点：进入面板即清除涟漪与 peek
      qnavPanel.addEventListener("mouseenter", clearQnavRipple);
    }

    qnavEntries.forEach(function(entry: any, i: any) {
      const text = entry.text || "问题 " + (i + 1);
      const item = el("button", "qnav-item", i + 1 + ". " + text);
      item.addEventListener("click", function () { jumpToQuestion(i); });
      qnavPanel.appendChild(item);
    });
    qnavPanel.scrollTop = followPanel ? qnavPanel.scrollHeight : panelTop;
    updateQnavActive();
  }

  // ---------- 从用户引用卡片定位到原文 ----------
  // 手机端以顶栏按钮打开问题列表；桌面端继续使用右侧悬浮导航。
  const qnavMobileBtn: any = document.getElementById("qnavMobileBtn");
  function closeMobileQnav() {
    qnav.classList.remove("mobile-open");
    qnavMobileBtn.setAttribute("aria-expanded", "false");
  }
  qnavMobileBtn.addEventListener("click", function(event: any) {
    event.stopPropagation();
    const open = !qnav.classList.contains("mobile-open");
    if (open) App.closeMenus();
    qnav.classList.toggle("mobile-open", open);
    qnavMobileBtn.setAttribute("aria-expanded", String(open));
  });
  qnavPanel.addEventListener("click", function(event: any) {
    if (event.target.closest(".qnav-item")) closeMobileQnav();
  });
  document.addEventListener("click", function(event: any) {
    if (!qnav.contains(event.target) && !qnavMobileBtn.contains(event.target)) closeMobileQnav();
  });
  document.addEventListener("keydown", function(event: any) {
    if (event.key === "Escape") closeMobileQnav();
  });

  let highlightedQuoteNode: any = null;
  let quoteHighlightTimer: any = null;
  let restoreSearchVisibility: any = null;

  function quoteMatchText(value: any) {
    return String(value == null ? "" : value)
      .replace(/\u00a0/g, " ")
      .replace(/\s+/g, " ")
      .trim();
  }

  function quoteRecordRoleMatches(record: any, role: any) {
    return record && (role === "user" ? record.kind === "user"
      : role === "assistant" ? record.kind === "assistant"
        : record.kind === "user" || record.kind === "assistant");
  }

  function quoteRecordText(record: any) {
    return quoteMatchText(App.contentToPlainText(record && record.content));
  }

  function findQuoteRecordIndex(records: any, source: any, quoteText: any) {
    if (!Array.isArray(records)) return -1;
    const role = source && source.role;
    const round = Number(source && source.round);
    const eventIndex = Number(source && source.event_index);
    const wanted = quoteMatchText(quoteText);
    const candidates: any = [];
    records.forEach(function(record: any, index: any) {
      if (quoteRecordRoleMatches(record, role)) candidates.push({ record: record, index: index });
    });
    if (!candidates.length) return -1;

    if (Number.isInteger(round) && round > 0 && Number.isInteger(eventIndex) && eventIndex >= 0) {
      const exact = candidates.find(function(item: any) {
        return Number(item.record.round) === round
          && Number(item.record.source_event_index) === eventIndex;
      });
      if (exact && (!wanted || quoteRecordText(exact.record).includes(wanted))) return exact.index;
    }

    const sameRound = Number.isInteger(round) && round > 0
      ? candidates.filter(function(item: any) { return Number(item.record.round) === round; })
      : [];
    function findText(items: any) {
      if (!wanted) return null;
      return items.find(function(item: any) { return quoteRecordText(item.record).includes(wanted); }) || null;
    }
    const matching = findText(sameRound) || findText(candidates);
    return matching ? matching.index : -1;
  }

  function findRenderedQuoteSource(source: any, quoteText: any, allowUnscoped: any) {
    const role = source && source.role;
    const selector = role === "user" ? ".msg-user-text"
      : role === "assistant" ? ".msg-assistant-body"
        : ".msg-user-text, .msg-assistant-body";
    const candidates: any = Array.from(chatInner.querySelectorAll(selector));
    if (!candidates.length) return null;

    const round = Number(source && source.round);
    const eventIndex = Number(source && source.event_index);
    const wanted = quoteMatchText(quoteText);
    const sameRound = Number.isInteger(round) && round > 0
      ? candidates.filter(function(node: any) {
        const host = node.closest("[data-round]");
        return host && Number(host.getAttribute("data-round")) === round;
      })
      : [];
    if (Number.isInteger(round) && round > 0 && !sameRound.length && !allowUnscoped) return null;
    const scoped = sameRound.length ? sameRound : candidates;

    if (Number.isInteger(eventIndex) && eventIndex >= 0) {
      const exact = scoped.find(function(node: any) {
        const host = node.closest("[data-source-event]");
        return host && Number(host.getAttribute("data-source-event")) === eventIndex;
      });
      if (exact && (!wanted || quoteMatchText(exact.innerText || exact.textContent).includes(wanted))) {
        return exact;
      }
    }

    if (!wanted) return null;
    const matching = function(items: any) {
      return items.find(function(node: any) {
        return quoteMatchText(node.innerText || node.textContent).includes(wanted);
      }) || null;
    };
    return matching(scoped) || (allowUnscoped && scoped !== candidates ? matching(candidates) : null);
  }

  // 引用定位仍使用后端 round/event_index；显示编号与提问导航一致。
  // 同一后端轮可含多条用户引导，不能直接把 round 当作导航序号。
  function quoteSourceDisplay(source: any, text: any) {
    if (!source || (source.session_id && source.session_id !== state.sessionId)) return source;
    const records = App.loadOlderState.records;
    const index = findQuoteRecordIndex(records, source, text);
    let question = 0;
    if (index >= 0) {
      for (let i = 0; i <= index; i++) if (records[i].kind === "user") question++;
    } else {
      const target = findRenderedQuoteSource(source, text, false);
      if (target) qnavEntries.forEach(function(entry: any, i: any) {
        if (entry.node && (entry.node === target || entry.node.contains(target)
            || (entry.node.compareDocumentPosition(target) & 4))) question = i + 1;
      });
    }
    return question > 0 ? Object.assign({}, source, { round: question }) : source;
  }

  function normalizedTextOffsets(rawText: any) {
    let text = "";
    const starts = [];
    const ends = [];
    for (let index = 0; index < rawText.length;) {
      if (/\s|\u00a0/.test(rawText[index])) {
        const start = index;
        while (index < rawText.length && (/\s|\u00a0/.test(rawText[index]))) index += 1;
        if (text && text[text.length - 1] !== " ") {
          text += " ";
          starts.push(start);
          ends.push(index);
        }
        continue;
      }
      text += rawText[index];
      starts.push(index);
      ends.push(index + 1);
      index += 1;
    }
    if (text.endsWith(" ")) {
      text = text.slice(0, -1);
      starts.pop();
      ends.pop();
    }
    return { text: text, starts: starts, ends: ends };
  }

  // 尽量把滚动锚点落在引用文字起始处；跨 Markdown 内联节点时，
  // 逐步缩短匹配前缀，仍可定位到引用所在的同一句/段落。
  function quoteTextAnchorRect(target: any, quoteText: any) {
    const wanted = quoteMatchText(quoteText);
    if (!wanted || typeof document.createTreeWalker !== "function") return null;
    const walker = document.createTreeWalker(target, NodeFilter.SHOW_TEXT);
    const nodes = [];
    let node;
    while ((node = walker.nextNode())) {
      if (node.nodeValue && node.nodeValue.trim()) {
        nodes.push({ node: node, mapped: normalizedTextOffsets(node.nodeValue) });
      }
    }
    const minLength = Math.min(8, wanted.length);
    for (let length = Math.min(32, wanted.length); length >= minLength; length -= 1) {
      const prefix = wanted.slice(0, length);
      for (let i = 0; i < nodes.length; i += 1) {
        const item = nodes[i];
        const at = item.mapped.text.indexOf(prefix);
        if (at < 0) continue;
        const range = document.createRange();
        range.setStart(item.node, item.mapped.starts[at]);
        range.setEnd(item.node, item.mapped.ends[at + prefix.length - 1]);
        const rect = range.getBoundingClientRect();
        if (rect && (rect.width || rect.height)) return rect;
      }
    }
    return null;
  }

  // Search snippets include surrounding text so repeated words can resolve to
  // the selected occurrence, including text split across Markdown inline nodes.
  function searchTextAnchor(target: any, quoteText: any, snippet: any) {
    const walker = document.createTreeWalker(target, NodeFilter.SHOW_TEXT);
    const nodes = [];
    let node, raw = "";
    while ((node = walker.nextNode())) {
      nodes.push({ node: node, start: raw.length });
      raw += node.nodeValue || "";
    }
    const mapped = normalizedTextOffsets(raw);
    const needle = quoteMatchText(quoteText);
    if (!needle) return null;
    const snippetText = snippet && snippet.text || "";
    const snippetMap = normalizedTextOffsets(snippetText);
    const context = snippetMap.text;
    const contextAt = context ? mapped.text.indexOf(context) : -1;
    const offset = Array.from(snippetText).slice(0, snippet && snippet.start || 0).join("").length;
    const relative = snippetMap.starts.findIndex((start: any) => start >= offset);
    const candidate = contextAt >= 0 && relative >= 0 ? contextAt + relative : -1;
    const at = candidate >= 0 && mapped.text.slice(candidate, candidate + needle.length) === needle
      ? candidate : mapped.text.indexOf(needle, contextAt >= 0 ? contextAt : 0);
    if (at < 0) return null;
    const start = mapped.starts[at], end = mapped.ends[at + needle.length - 1];
    const first = nodes.find((item: any) => item.start + item.node.nodeValue.length > start);
    const last = nodes.find((item: any) => item.start + item.node.nodeValue.length >= end);
    if (!first || !last) return null;
    const range = document.createRange();
    range.setStart(first.node, start - first.start); range.setEnd(last.node, end - last.start);
    return { range: range, node: first.node.parentElement };
  }

  function revealQuoteSource(target: any, quoteText: any, options?: any) {
    if (target && target.closest(".history-live-hidden") && App.showHistoryTail) App.showHistoryTail();
    if (App.processGroups) App.processGroups.reveal(target);
    options = options || {};
    if (quoteHighlightTimer != null) window.clearTimeout(quoteHighlightTimer);
    if ((window as any).CSS && (window as any).CSS.highlights) (window as any).CSS.highlights.delete("history-search-hit");
    if (restoreSearchVisibility) { restoreSearchVisibility(); restoreSearchVisibility = null; }
    if (highlightedQuoteNode) highlightedQuoteNode.classList.remove("quote-source-highlight");
    if (options.search) {
      // Materialize the selected offscreen lazy message before measuring text.
      let root = target;
      while (root.parentElement && root.parentElement !== chatInner) root = root.parentElement;
      const previous = root.style.contentVisibility;
      root.style.contentVisibility = "visible";
      restoreSearchVisibility = function () { root.style.contentVisibility = previous; };
    }
    highlightedQuoteNode = target;
    target.classList.remove("quote-source-highlight");
    // 重复点击同一引用时重播一次闪烁动画。
    void target.offsetWidth;
    target.classList.add("quote-source-highlight");
    quoteHighlightTimer = window.setTimeout(function () {
      if (highlightedQuoteNode) highlightedQuoteNode.classList.remove("quote-source-highlight");
      highlightedQuoteNode = null;
      quoteHighlightTimer = null;
      if ((window as any).CSS && (window as any).CSS.highlights) (window as any).CSS.highlights.delete("history-search-hit");
      if (restoreSearchVisibility) { restoreSearchVisibility(); restoreSearchVisibility = null; }
    }, 1900);

    if (options.search) {
      const anchor: any = searchTextAnchor(target, quoteText, options.snippet);
      if (anchor && (window as any).CSS && (window as any).CSS.highlights && (window as any).Highlight) {
        (window as any).CSS.highlights.set("history-search-hit", new (window as any).Highlight(anchor.range));
      }
      // A deep match can be thousands of pixels below a scrollable tool <pre>.
      // Scroll those inner containers first; using its unclipped text rectangle
      // directly for the chat scroller would clamp the jump to the session end.
      for (let host = anchor && anchor.node; host && host !== chatScroll; host = host.parentElement) {
        const style = window.getComputedStyle(host);
        const rect = anchor.range.getBoundingClientRect(), box = host.getBoundingClientRect();
        const behavior = host.style.scrollBehavior;
        host.style.scrollBehavior = "auto";
        if (/auto|scroll/.test(style.overflowY) && host.scrollHeight > host.clientHeight) {
          host.scrollTop += rect.top - box.top - (host.clientHeight - rect.height) / 2;
        }
        if (/auto|scroll/.test(style.overflowX) && host.scrollWidth > host.clientWidth) {
          host.scrollLeft += rect.left - box.left - (host.clientWidth - rect.width) / 2;
        }
        host.style.scrollBehavior = behavior;
      }
      function align() {
        if (App.isChatScrollbarDragging && App.isChatScrollbarDragging()) return;
        if (highlightedQuoteNode !== target || !target.isConnected || (options.isCurrent && !options.isCurrent())) return;
        const rect = anchor ? anchor.range.getBoundingClientRect() : target.getBoundingClientRect();
        const box = chatScroll.getBoundingClientRect();
        const behavior = chatScroll.style.scrollBehavior;
        chatScroll.style.scrollBehavior = "auto";
        chatScroll.scrollTop += rect.top - box.top - (chatScroll.clientHeight - rect.height) / 2;
        chatScroll.style.scrollBehavior = behavior;
      }
      align();
      // One bounded correction after lazy neighbours acquire their real height.
      window.requestAnimationFrame(align);
      return;
    }
    const targetRect = quoteTextAnchorRect(target, quoteText) || target.getBoundingClientRect();
    const scrollRect = chatScroll.getBoundingClientRect();
    const top = chatScroll.scrollTop + targetRect.top - scrollRect.top
      - Math.max(0, (chatScroll.clientHeight - targetRect.height) / 2);
    chatScroll.scrollTo({ top: Math.max(0, top), behavior: "smooth" });
  }

  async function jumpToQuoteSource(source: any, quoteText: any) {
    const locator = source && typeof source === "object" ? source : {};
    const sourceSession = typeof locator.session_id === "string" ? locator.session_id : "";
    if (sourceSession && state.sessionId !== sourceSession) {
      if (typeof App.openSession !== "function") {
        toast("无法打开引用所属的会话");
        return;
      }
      try {
        await App.openSession(sourceSession);
      } catch (_) {
        toast("无法打开引用所属的会话");
        return;
      }
      if (state.sessionId !== sourceSession) {
        toast("引用所属的会话未能打开");
        return;
      }
    }

    if (App.historyPages && Number(locator.round)>0 && !App.historyPages.hasRound(Number(locator.round))) {
      if (!await App.historyPages.ensureRound(Number(locator.round))) return;
    }
    const records = App.loadOlderState.records;
    const recordIndex = findQuoteRecordIndex(records, locator, quoteText);
    let target = findRenderedQuoteSource(locator, quoteText, false);
    let renderLocator = locator;
    if (!target && records && recordIndex >= 0 && typeof App.renderHistoryWindowAt === "function") {
      App.renderHistoryWindowAt(recordIndex);
      target = findRenderedQuoteSource(renderLocator, quoteText, false);
    }
    if (!target && records && recordIndex >= 0) {
      const sourceRecord = records[recordIndex];
      renderLocator = Object.assign({}, locator, { round: sourceRecord.round });
      if (Number.isInteger(sourceRecord.source_event_index)) {
        renderLocator.event_index = sourceRecord.source_event_index;
      } else {
        delete renderLocator.event_index;
      }
      target = findRenderedQuoteSource(renderLocator, quoteText, false);
    }
    if (!target && records && recordIndex >= 0) {
      while (App.loadOlderState.records === records && recordIndex < App.loadOlderState.start) {
        const previousStart = App.loadOlderState.start;
        if (App.loadOlderState.loading) {
          await new Promise(function(resolve: any) { window.setTimeout(resolve, 20); });
        } else {
          await App.runOlderBatch();
        }
        if (App.loadOlderState.start === previousStart && !App.loadOlderState.loading) break;
        target = findRenderedQuoteSource(renderLocator, quoteText, false);
        if (target) break;
      }
      if (!target && App.loadOlderState.records === records) {
        target = findRenderedQuoteSource(renderLocator, quoteText, false);
      }
    }
    if (!target && recordIndex < 0) target = findRenderedQuoteSource(locator, quoteText, true);

    if (!target) {
      toast("未找到引用原文，可能已被编辑或删除");
      return;
    }
    revealQuoteSource(target, quoteText);
  }

  // Search hits carry the same physical event index as JSONL. Load older batches
  // on demand, then reveal collapsed reasoning/tools before scrolling to text.
  async function jumpToHistoryMatch(match: any, query: any, isCurrent: any) {
    const round = Number(match.round_no);
    if (!Number.isInteger(round) || round <= 0) return false;
    const current = isCurrent || function () { return state.sessionId === match.session_id; };
    if (!current()) return false;
    if (App.historyPages && !App.historyPages.hasRound(round)) {
      if (!await App.historyPages.ensureRound(round) || !current()) return false;
    }
    // Cancel openSession's scheduled bottom correction and pause live follow.
    if (App.pauseAutoScroll) App.pauseAutoScroll();
    const snippet = match.snippets && match.snippets[0];
    const anchorText = snippet ? Array.from(snippet.text || "").slice(snippet.start, snippet.end).join("") : query;
    const selectors = match.role === "user" ? ".msg-user"
      : match.role === "tool" ? ".tool-block"
      : match.role === "sub_agent" ? ".agent-block"
      : match.role === "compaction" ? ".compaction-bar"
      : match.field === "reasoning_content" ? ".think-block"
      : match.field === "error" ? ".notice-bar" : ".msg-assistant-body";
    function findTarget() {
      const nodes = Array.from(chatInner.querySelectorAll(selectors + '[data-round="' + round + '"]'));
      if (!nodes.length) return null;
      if (match.agent_id) {
        const agent = nodes.find(function(node: any) { return node.dataset.agentId === match.agent_id; });
        if (agent) return agent;
        return null;
      }
      const event = match.event_index;
      if (event != null) {
        const exact = nodes.find(function(node: any) { return node.getAttribute("data-source-event") === String(event)
          || node.getAttribute("data-result-event") === String(event); });
        if (exact) return exact;
        return null; // The exact event may still be in an unrendered older batch.
      }
      const wanted = String(anchorText || "").toLowerCase();
      return nodes.find(function(node: any) { return String(node.textContent || "").toLowerCase().includes(wanted); }) || nodes[0];
    }
    let target: any = findTarget();
    const records = App.loadOlderState.records;
    const index = records && records.findIndex(function(rec: any) { return Number(rec.round) === round; });
    if (!target && records && index >= 0 && typeof App.renderHistoryWindowAt === "function") {
      App.renderHistoryWindowAt(App.historyMatchIndex(records, match));
      target = findTarget();
    }
    while (!target && records && index >= 0 && App.loadOlderState.records === records && index < App.loadOlderState.start && current()) {
      const previous = App.loadOlderState.start;
      if (App.loadOlderState.loading) await new Promise(function(resolve: any) { window.setTimeout(resolve, 20); });
      else await App.runOlderBatch();
      if (!current()) return false;
      target = findTarget();
      if (App.loadOlderState.start === previous && !App.loadOlderState.loading) break;
    }
    if (!current()) return false;
    if (!target) target = chatInner.querySelector('.msg-user[data-round="' + round + '"]');
    if (!target) { toast("匹配记录可能已被编辑或删除，请重新搜索"); return false; }
    if (match.role === "sub_agent") {
      const selector = match.field === "reasoning_content" ? ".think-block" : match.field === "content" ? ".agent-content"
        : match.field === "arguments" || match.field === "result" ? ".tool-block"
          : match.field === "final_reply" ? ".agent-final-reply" : match.field === "task" ? ".agent-task" : "[data-agent-event]";
      const nodes = Array.from(target.querySelectorAll(selector));
      const exact = nodes.find((node: any) => node.getAttribute("data-agent-event") === String(match.event_index)
        || node.getAttribute("data-agent-result-event") === String(match.event_index));
      if (!exact) target.querySelectorAll(".think-block, .tool-block").forEach(function(node: any) { node.classList.add("open"); });
      target = exact || nodes.find((node: any) => String(node.textContent || "").includes(snippet && snippet.text || anchorText)) || target;
    }
    for (let host = target; host && host !== chatInner; host = host.parentElement) {
      if (host.matches(".think-block, .tool-block, .agent-block, .compaction-bar")) {
        host.classList.add("open");
        const head = host.querySelector && host.querySelector('[aria-expanded]');
        if (head) head.setAttribute("aria-expanded", "true");
      }
    }
    if (target.matches(".tool-block") && target.querySelectorAll) {
      const io = target.querySelectorAll(".tool-io");
      target = io[match.field === "arguments" ? 0 : 1] || target;
    }
    revealQuoteSource(target, anchorText, { search: true, snippet: snippet, isCurrent: current });
    return true;
  }

  let qnavJumpGeneration = 0;
  let qnavJumpFrame: any = null;
  let qnavJumpTimer: any = null;
  let qnavJumpRestoreVisibility: any = null;

  function cancelQuestionJump() {
    qnavJumpGeneration += 1;
    if (qnavJumpFrame != null) cancelAnimationFrame(qnavJumpFrame);
    if (qnavJumpTimer != null) clearTimeout(qnavJumpTimer);
    qnavJumpFrame = null;
    qnavJumpTimer = null;
    if (qnavJumpRestoreVisibility) qnavJumpRestoreVisibility();
    qnavJumpRestoreVisibility = null;
  }

  function jumpToQuestion(i: any) {
    let target = qnavUsers[i];
    if (target && target.closest(".history-live-hidden") && App.showHistoryTail) App.showHistoryTail();
    const entry = qnavEntries[i];
    if (entry && App.historyPages && !App.historyPages.hasRound(entry.round)) {
      const round=entry.round,eventIndex=entry.eventIndex;
      App.historyPages.ensureRound(round).then(function(ok: any) {
        if(!ok)return;
        const next=qnavEntries.findIndex((item: any)=>item.round===round && item.eventIndex===eventIndex);
        if(next>=0)jumpToQuestion(next);
      });
      return;
    }
    if (!target && entry && App.renderHistoryWindowAt(entry.index)) {
      target = chatInner.querySelector('.msg-user[data-history-index="' + entry.index + '"]');
    }
    if (!target) return;
    cancelQuestionJump();
    const generation = qnavJumpGeneration;
    // 目标可能在 content-visibility:auto 的离屏占位中；先让该消息真实布局。
    let root = target;
    while (root.parentElement && root.parentElement !== chatInner) root = root.parentElement;
    const previousVisibility = root.style.contentVisibility;
    root.style.contentVisibility = "visible";
    qnavJumpRestoreVisibility = function () { root.style.contentVisibility = previousVisibility; };
    // 流式回复的自动贴底与待执行的置底帧不能覆盖用户主动导航。
    App.pauseAutoScroll();

    function align() {
      if (!chatInner.contains(target)) return false;
      const delta = target.getBoundingClientRect().top - chatScroll.getBoundingClientRect().top;
      if (Math.abs(delta) <= 1) return true;
      // scrollTop/scrollTo(auto) 会继承 CSS 的 smooth；临时改为瞬时滚动。
      const previousBehavior = chatScroll.style.scrollBehavior;
      chatScroll.style.scrollBehavior = "auto";
      chatScroll.scrollTop += delta;
      chatScroll.style.scrollBehavior = previousBehavior;
      return true;
    }

    align();
    // 懒渲染消息进入视口后会用真实高度替换 240px 占位，首次落点随之漂移。
    // 后续帧按同一个目标校正；晚到的布局再补一次，避免用户需要点击第二次。
    let framesLeft = 18;
    function correct() {
      if (generation !== qnavJumpGeneration) return;
      qnavJumpFrame = null;
      if (!align()) { cancelQuestionJump(); return; }
      if (--framesLeft > 0) qnavJumpFrame = requestAnimationFrame(correct);
    }
    qnavJumpFrame = requestAnimationFrame(correct);
    qnavJumpTimer = window.setTimeout(function () {
      if (generation !== qnavJumpGeneration) return;
      align();
      cancelQuestionJump();
    }, 650);
  }

  // 一旦用户开始自行滚动或操作消息，停止导航校正，避免把视口拉回旧目标。
  ["wheel", "touchstart", "pointerdown"].forEach(function(type: any) {
    chatScroll.addEventListener(type, function () {
      if (qnavJumpRestoreVisibility) cancelQuestionJump();
    }, { passive: true, capture: true });
  });
  window.addEventListener("keydown", function(event: any) {
    if (qnavJumpRestoreVisibility && ["ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End", " "].includes(event.key)) {
      cancelQuestionJump();
    }
  }, true);

  function updateQnavActive() {
    if (!qnavUsers.length) return;
    const scrollRect = chatScroll.getBoundingClientRect();
    const paging = typeof App.loadOlderState !== "undefined" ? App.loadOlderState : null;
    const entries = typeof qnavEntries !== "undefined" ? qnavEntries : null;
    function mounted(node: any) {
      return node && node.isConnected !== false && !node.closest(".history-live-hidden, .is-process-hidden");
    }
    // 分页窗口只渲染部分消息：首个已渲染条目之前的全是更早的历史，
    // 逻辑上都在视口上方，基线指向其中最后一个；否则窗口顶部
    // （如尾轮用户消息仍在 120px 线下方时）高亮会钉死第一条
    let current = 0;
    for (let k = 0; k < qnavUsers.length; k++) {
      if (mounted(qnavUsers[k])) {
        if (k > 0) current = k - 1;
        break;
      }
    }
    // 长回答可能占满整个窗口，所属提问早已被卸载。用可见历史记录
    // 的下标回查完整问题索引，不要求提问气泡仍在 DOM 中。
    if (paging && paging.records && entries && typeof chatInner !== "undefined") {
      let recordIndex = paging.start - 1;
      Array.from(chatInner.children).forEach(function(node: any) {
        if (!mounted(node)) return;
        const raw = node.getAttribute("data-history-index");
        if (raw == null) return;
        const rect = node.getBoundingClientRect();
        if (rect.bottom <= rect.top) return;
        if (rect.top - scrollRect.top < 120) recordIndex = Math.max(recordIndex, Number(raw));
      });
      entries.forEach(function(entry: any, i: any) {
        if (entry.index >= 0 && entry.index <= recordIndex) current = Math.max(current, i);
      });
    }
    // 到底吸附：已滚到真正的底部时，高亮最后一个有效问题。
    // 尾部是短气泡（刚提问未回复 / 简短收尾）时它仍在 120px 线下方，
    // 原逻辑会回落到前面的问题（问题少时就是第一条），并在临界线
    // 附近随布局波动来回跳变。阈值与回底跟随的 keepBottom 同口径。
    const bottomGap = chatScroll.scrollHeight - chatScroll.scrollTop - chatScroll.clientHeight;
    const tailWindow = !paging || !paging.records || paging.end >= paging.records.length;
    if (bottomGap <= 24 && tailWindow) {
      // 最后一个问题也可能在窗口之外，完整索引才是会话末尾的依据。
      current = qnavUsers.length - 1;
    } else {
      qnavUsers.forEach(function(userEl: any, i: any) {
        if (!mounted(userEl)) return;
        const rect = userEl.getBoundingClientRect();
        if (rect.bottom > rect.top && rect.top - scrollRect.top < 120) current = Math.max(current, i);
      });
    }
    // 高亮当前问题所属的采样段：与 rebuildQnav 同口径计算每段起点，
    // 找最大的 start_j ≤ current 即当前段；一一对应时直接用下标
    const dashes = qnavRail.querySelectorAll(".qnav-dash");
    let activeDash = -1;
    if (dashes.length && dashes.length === qnavUsers.length) {
      activeDash = current;
    } else {
      for (let j = dashes.length - 1; j >= 0; j--) {
        if (Math.floor(j * qnavUsers.length / dashes.length) <= current) {
          activeDash = j;
          break;
        }
      }
    }
    dashes.forEach(function(d: any, i: any) {
      d.classList.toggle("active", i === activeDash);
    });
    qnavPanel.querySelectorAll(".qnav-item").forEach(function(d: any, i: any) {
      d.classList.toggle("active", i === current);
    });
  }

  // ---------- 滚动：回到底部 + 导航高亮 ----------
  let scrollTick = false;
  chatScroll.addEventListener("scroll", function () {
    if (scrollTick) return;
    scrollTick = true;
    requestAnimationFrame(function () {
      // 任一更新器抛错也不能卡死 scrollTick，否则导航高亮与回底按钮永久冻结
      try {
        const far = chatScroll.scrollHeight - chatScroll.scrollTop - chatScroll.clientHeight > 240
          || Boolean(App.loadOlderState.records && App.loadOlderState.end < App.loadOlderState.records.length);
        scrollBottomBtn.classList.toggle("show", far);
        updateCodeblockCopyButtons();
        updateQnavActive();
      } finally {
        scrollTick = false;
      }
    });
  });

  // ---------- 代码块复制按钮：滚动钉住 ----------
  // 垂直钉住由原生 position: sticky 完成；正常态按钮在右侧，钉住态移到代码块中线，
  // 以避开 topbar 右上角固定分享按钮。流式渲染后由调用方同步更新，避免异步回调闪切。
  const COPY_PIN_OFFSET = 8;

  function updateCodeblockCopyButtons() {
    if ((document.body as HTMLElement).classList.contains("sidebar-resizing")) return;
    const scrollRect = chatScroll.getBoundingClientRect();
    const pinY = scrollRect.top + COPY_PIN_OFFSET;
    const buttons = chatInner.querySelectorAll(".codeblock .copy-btn");
    // 读写分离：先对全部按钮一口气收集 rect（阶段一，纯读不触发写回布局），
    // 再统一计算并写 class/样式（阶段二，纯写）。避免此前「逐个 rect 读 →
    // 写 class → 下一个再读」交替造成的强制同步布局抖动（长会话滚动卡顿主因）
    const visible = [];
    const roots = new Map();
    for (let i = 0; i < buttons.length; i++) {
      const btn = buttons[i];
      // 测量离屏后代会强制 content-visibility:auto 展开整条消息。
      // 先检查顶层消息的占位框，仅测量视口内实际可见的代码块。
      let root = btn;
      while (root.parentElement && root.parentElement !== chatInner) root = root.parentElement;
      if (!roots.has(root)) roots.set(root, root.getBoundingClientRect());
      const box = roots.get(root);
      if (box.bottom < scrollRect.top || box.top > scrollRect.bottom) continue;
      if (btn.checkVisibility && !btn.checkVisibility({ contentVisibilityAuto: true })) continue;
      const rect = btn.closest(".codeblock").getBoundingClientRect();
      visible.push({ btn: btn, rect: rect, height: btn.offsetHeight || 28,
        width: btn.offsetWidth, gap: parseFloat(getComputedStyle(btn).marginRight) || 12 });
    }
    for (let i = 0; i < visible.length; i++) {
      const item = visible[i];
      const btn = item.btn;
      const blockRect = item.rect;
      const btnH = item.height;
      // 代码块在视口内的可见高度（含下边框可见部分）
      // 注：此处不可命名为 visible——外层数组已叫 visible，同块重声明会遮蔽并触发 TDZ
      const visibleHeight = Math.min(blockRect.bottom, scrollRect.bottom) - Math.max(blockRect.top, scrollRect.top);
      if (visibleHeight < btnH) {
        btn.classList.add("is-hidden");
        continue;
      }
      btn.classList.remove("is-hidden");

      // 直接依据代码块顶部判断，避免读取 sticky 按钮自身位置造成临界态误判。
      const pinned = blockRect.top < pinY;
      const wasPinned = btn.classList.contains("is-pinned");
      if (pinned) {
        // 正常态右边距跟随窄屏规则变化，按实际 margin 计算居中位移；
        // 窗口缩放导致按钮宽度变化时，仅在位移真的变化时写入，避免滚动中重复触发布局。
        const shift = item.width / 2 + item.gap - blockRect.width / 2;
        const currentShift = parseFloat(btn.style.getPropertyValue("--pin-shift"));
        if (!wasPinned || !Number.isFinite(currentShift) || Math.abs(currentShift - shift) > 0.5) {
          btn.style.setProperty("--pin-shift", shift + "px");
        }
        btn.classList.add("is-pinned");
      } else {
        btn.classList.remove("is-pinned");
        btn.style.removeProperty("--pin-shift");
      }
    }
  }
  window.addEventListener("sidebarresizeend", function () {
    requestAnimationFrame(updateCodeblockCopyButtons);
  });

  scrollBottomBtn.addEventListener("click", scrollToBottom);


  // ---------- 导出（供其它模块经 App.* 调用） ----------
  App.rebuildQnav = rebuildQnav;
  App.updateQnavActive = updateQnavActive;
  App.jumpToQuoteSource = jumpToQuoteSource;
  App.quoteSourceDisplay = quoteSourceDisplay;
  App.jumpToHistoryMatch = jumpToHistoryMatch;
  App.updateCodeblockCopyButtons = updateCodeblockCopyButtons;

})((window as unknown as { App: any }).App);
