/** 连续过程记录的折叠摘要。保留记录为同级节点，兼容历史窗口、回放和定位。
    轮次用量行直接展示，不归入折叠分组；连续过程只有一项时同样直接展示。 */
(function (App) {
  "use strict";
  const stages = ".think-block, .tool-block, .agent-block, .compaction-bar, .notice-bar";
  const heads = ".think-toggle, .tool-block-head, .agent-head, .compaction-head";
  const controllers = new WeakMap();
  const choices = new Map();
  const recordKeys = new WeakMap();
  const recordGroups = new WeakMap();
  let historyGroups = new Map(), choiceRevision = 0;
  let indexedRecords = null, indexedLength = 0, indexedSession = null;
  let sequence = 0;

  function text(node, value) {
    if (node.textContent !== value) node.textContent = value;
  }
  function key(round, event, index) {
    return (App.state.sessionId || "") + ":" + (round || "") + ":" +
      (event != null ? "e" + event : "") + (index != null ? ":h" + index : "");
  }
  function nodeKey(node) {
    if (node._processHistoryKey) return node._processHistoryKey;
    if (node.dataset.sourceEvent == null && node.dataset.historyIndex == null) {
      if (!node._processIdentity) node._processIdentity = "live-" + (++sequence);
      return key(node.dataset.round, node._processIdentity);
    }
    return key(node.dataset.round, node.dataset.sourceEvent, node.dataset.historyIndex);
  }
  function remember(group) {
    if (choices.get(group.key) !== group.open) choiceRevision++;
    choices.set(group.key, group.open);
    if (choices.size > 500) choices.delete(choices.keys().next().value);
  }

  function setTimestamp(node, timestamp) {
    if (!node || !timestamp) return;
    const label = FormatUtils.fmtTime(timestamp);
    node.dataset.stageTimestamp = String(timestamp);
    node.dataset.stageTime = label;
    // 助手正文已有独立时间行，不再加悬停时间提示
    if (node.matches(".msg-assistant-body")) return;
    if (node.matches(".msg-user")) {
      node.title = label;
      const time = node.querySelector(".msg-time");
      if (time) time.classList.add("hover-only-time");
      return;
    }
    const head = node.querySelector(heads);
    if (!head) { node.title = label; return; }
    // 工具只保留标题的原生悬停提示，避免右侧标签与提示重复显示。
    if (node.matches(".tool-block")) {
      head.querySelectorAll(".stage-hover-time").forEach(time => time.remove());
    } else {
      let time = head.querySelector(".stage-hover-time");
      if (!time) {
        time = document.createElement("time");
        time.className = "stage-hover-time";
        head.appendChild(time);
      }
      time.textContent = label;
      time.dateTime = String(timestamp);
    }
    if (head.dataset.timeTitle == null) head.dataset.timeTitle = head.title || "";
    head.title = (head.dataset.timeTitle ? head.dataset.timeTitle + "\n" : "") + label;
  }

  function applyVisibility(group) {
    group.button.setAttribute("aria-expanded", String(group.open));
    group.header.classList.toggle("is-open", group.open);
    group.members.forEach(function (node) {
      node.classList.toggle("is-process-hidden", !group.open);
    });
  }
  function openGroup(group, open, user) {
    group.open = open;
    if (user) remember(group);
    const history = historyGroups.get(group.key);
    if (history && !open) group.header.dataset.historyIndex = String(history.start);
    else if (open && group.members[0] && group.members[0].dataset.historyIndex != null) {
      group.header.dataset.historyIndex = group.members[0].dataset.historyIndex;
    }
    applyVisibility(group);
    if (App.historyScroll) App.historyScroll.refresh({ preserve: false });
    if (App.updateCodeblockCopyButtons) App.updateCodeblockCopyButtons();
  }

  function renderGroup(group) {
    let think = 0, tools = 0, agents = 0, compactions = 0, failures = 0;
    let active = null, waiting = null, note = null;
    group.members.forEach(function (node) {
      if (node.matches(".think-block")) think++;
      else if (node.matches(".tool-block")) tools++;
      else if (node.matches(".agent-block")) agents++;
      else if (node.matches(".compaction-bar")) compactions++;
      else if (node.matches(".notice-bar")) note = node;
      if (node.matches(".is-waiting")) waiting = node;
      if (node.matches(".is-streaming, .is-executing, .is-running") ||
          node.querySelector(".tool-status-icon")) active = node;
      if (node.matches(".is-error, .is-failed, .compaction-interrupted") ||
          node.querySelector(".tool-block-tool-icon.is-failed")) failures++;
    });
    const history = historyGroups.get(group.key);
    if (history) {
      think = Math.max(think, history.counts.think || 0);
      tools = Math.max(tools, history.counts.tool || 0);
      agents = Math.max(agents, history.counts.agentBlock || 0);
    }
    const counts = [];
    if (think) counts.push(think + " 段思考");
    if (tools) counts.push(tools + " 次工具调用");
    if (agents) counts.push(agents + " 个子任务");
    if (compactions) counts.push(compactions + " 次压缩");
    if (failures) counts.push(failures + " 项异常");
    text(group.label, "处理过程");
    text(group.counts, counts.join(" · "));
    let preview = "";
    if (waiting) {
      const label = waiting.querySelector(".permission-inline-label");
      preview = label && label.textContent.trim() || "等待确认";
    } else if (active) {
      const head = active.querySelector(".tool-block-action:not([hidden]), .tool-block-name:not([hidden]), .agent-title, .compaction-title");
      preview = active.matches(".think-block") ? "正在思考…" : (head && head.textContent.trim() || "处理中") + "…";
    } else if (note) preview = note.textContent.trim().slice(0, 120);
    text(group.preview, preview);
    group.header.classList.toggle("is-running", Boolean(active && !waiting));
    group.header.classList.toggle("is-waiting", Boolean(waiting));
    group.header.classList.toggle("has-error", Boolean(failures));
    const times = group.members.map(node => node.dataset.stageTime).filter(Boolean);
    const range = times.length ? times[0] + (times[times.length - 1] !== times[0] ? " — " + times[times.length - 1] : "") : "";
    group.button.title = [range, counts.join(" · "), preview].filter(Boolean).join("\n");
    // 新审批到达时展开一次；用户随后仍可手动收起，摘要始终标明等待确认。
    if (waiting && waiting !== group.waiting) {
      group.open = true;
      applyVisibility(group);
    }
    group.waiting = waiting;
  }

  function buildGroup(container) {
    const header = App.el("div", "process-group-summary");
    const button = App.el("button", "process-group-toggle");
    button.type = "button";
    button.innerHTML = '<svg class="icon process-group-chevron" viewBox="0 0 24 24" aria-hidden="true"><path d="m9 18 6-6-6-6"/></svg><span class="process-group-spinner" aria-hidden="true"></span>';
    const label = App.el("span", "process-group-label", "处理过程");
    const counts = App.el("span", "process-group-counts");
    const preview = App.el("span", "process-group-preview");
    button.append(label, counts, preview);
    header.appendChild(button);
    const group = { container, header, button, label, counts, preview, members: [], open: false, waiting: null, key: "" };
    header._processSummary = group;
    button.addEventListener("click", function () {
      const top = header.getBoundingClientRect().top;
      if (App.pauseAutoScroll) App.pauseAutoScroll();
      openGroup(group, !group.open, true);
      // 折叠长过程时把同一个摘要条留在原位置，避免视口跳到别的回答。
      const delta = header.getBoundingClientRect().top - top;
      if (App.chatScroll && Math.abs(delta) > 0.5) {
        const behavior = App.chatScroll.style.scrollBehavior;
        App.chatScroll.style.scrollBehavior = "auto";
        App.chatScroll.scrollTop += delta;
        App.chatScroll.style.scrollBehavior = behavior;
      }
    });
    return group;
  }

  function controller(container) {
    if (controllers.has(container)) return controllers.get(container);
    let groups = new Set(), timer = null;
    function sync() {
      if (timer != null) { clearTimeout(timer); timer = null; }
      const runs = [], children = Array.from(container.children);
      let run = null;
      children.forEach(function (node) {
        if (node.matches(".process-group-summary, .round-fold-summary")) return;
        if (!node.matches(stages)) { run = null; return; }
        const previous = run && run[run.length - 1];
        if (!run || previous.dataset.round && node.dataset.round && previous.dataset.round !== node.dataset.round) {
          run = []; runs.push(run);
        }
        run.push(node);
      });
      const next = new Set();
      runs.forEach(function (members) {
        // 连续过程只有一项时直接展示，不套折叠摘要（旧分组由下面的解散清理回收）
        if (members.length < 2) return;
        const retained = members.map(node => node._processGroup).find(group => group && group.container === container && !next.has(group));
        const group = retained || buildGroup(container);
        const first = members[0];
        group.members = members;
        group.key = nodeKey(first);
        if (!retained && choices.has(group.key)) group.open = choices.get(group.key);
        ["historyIndex", "round"].forEach(function (name) {
          if (first.dataset[name] != null) group.header.dataset[name] = first.dataset[name];
          else delete group.header.dataset[name];
        });
        const history = historyGroups.get(group.key);
        if (history && !group.open) group.header.dataset.historyIndex = String(history.start);
        members.forEach(function (node) {
          node._processGroup = group;
          node.classList.add("process-stage");
          if (!node.id) node.id = "process-stage-" + (++sequence);
        });
        group.button.setAttribute("aria-controls", members.map(node => node.id).join(" "));
        if (group.header.nextElementSibling !== first) container.insertBefore(group.header, first);
        applyVisibility(group);
        renderGroup(group);
        next.add(group);
      });
      groups.forEach(function (group) {
        if (next.has(group)) return;
        group.header.remove();
        // 被解散分组的成员若未被新分组收养（如不再参与分组的用量行），
        // 必须解除隐藏，否则会留下无摘要条可展开的隐形节点。
        group.members.forEach(function (node) {
          if (node._processGroup !== group) return;
          delete node._processGroup;
          node.classList.remove("is-process-hidden");
        });
      });
      groups = next;
    }
    function schedule() {
      if (timer == null) timer = setTimeout(sync, 100);
    }
    if (typeof MutationObserver !== "undefined") {
      new MutationObserver(function (changes) {
        const changed = changes.some(function (change) {
          const target = change.target.nodeType === 1 ? change.target : change.target.parentElement;
          if (!target || target.closest(".process-group-summary, .round-fold-summary")) return false;
          if (change.type === "childList" && target === container) {
            return Array.from(change.addedNodes).concat(Array.from(change.removedNodes))
              .some(node => node.nodeType === 1 && !node.matches(".process-group-summary, .round-fold-summary"));
          }
          if (change.type === "attributes") {
            const statuses = value => (value || "").split(/\s+/).filter(name => /^is-|^has-error/.test(name) && name !== "is-process-hidden" && name !== "is-roundfold-hidden").sort().join(" ");
            return change.attributeName === "class" && statuses(change.oldValue) !== statuses(target.getAttribute("class"));
          }
          return Boolean(target.closest(heads) || target.matches(".notice-bar"));
        });
        if (changed) schedule();
      }).observe(container, { subtree: true, childList: true, characterData: true,
        attributes: true, attributeFilter: ["class"], attributeOldValue: true });
    }
    const value = { sync };
    controllers.set(container, value);
    return value;
  }

  function sync(container) {
    if (container && container.children) controller(container).sync();
    // 小折叠先定型，大折叠随后按最终子节点同步（同容器幂等，可重入）
    if (App.roundFold) App.roundFold.sync(container);
  }
  function reveal(node) {
    if (App.roundFold) App.roundFold.reveal(node);
    for (let current = node; current; current = current.parentElement) {
      if (current._processGroup) openGroup(current._processGroup, true, true);
      if (current === App.chatInner) break;
    }
  }
  function estimateHeights(records, fallback, measured) {
    indexRecords(records);
    let previous = null;
    return records.map(function (record) {
      if (record.kind === "approval" || record.kind === "toolResult") return measured.has(record) ? measured.get(record) : 0;
      const processKey = recordKeys.get(record);
      const same = processKey && processKey === previous;
      previous = processKey;
      if (!processKey) return measured.has(record) ? measured.get(record) : fallback(record);
      const expanded = choices.get(processKey) === true;
      const cached = measured.get(record);
      if (!expanded) return same ? 0 : cached > 0 ? Math.min(36, cached) : 36;
      const compact = ["think", "tool", "agentBlock", "compaction"].includes(record.kind) ? 42 : fallback(record);
      return cached > 0 ? cached : compact + (same ? 0 : 34);
    });
  }
  // 同一过程横跨多个历史窗口时共用展开选择，不在翻页后重新折叠。
  function indexRecords(records) {
    if (indexedRecords === records && indexedLength === records.length && indexedSession === App.state.sessionId) return;
    indexedRecords = records; indexedLength = records.length; indexedSession = App.state.sessionId;
    let group = null, round = null;
    historyGroups = new Map();
    const assigned = [];
    records.forEach(function (record, index) {
      recordKeys.delete(record); recordGroups.delete(record);
      if (record.kind === "approval" || record.kind === "toolResult") {
        if (round != null && record.round != null && round !== record.round) group = null;
        if (group) {
          const info = historyGroups.get(group); info.end = index + 1;
          assigned.push([record, group]);
        }
        return;
      }
      if (!["think", "tool", "agentBlock", "compaction", "notice"].includes(record.kind)) {
        group = null; return;
      }
      if (!group || round != null && record.round != null && round !== record.round) {
        group = key(record.round, record.source_event_index, index);
        historyGroups.set(group, {start:index, end:index + 1, counts:{}, stages:0});
      }
      round = record.round;
      const info = historyGroups.get(group); info.end = index + 1; info.stages++;
      info.counts[record.kind] = (info.counts[record.kind] || 0) + 1;
      assigned.push([record, group]);
      if (record.kind === "tool" && record.name === "ask_user") group = null;
    });
    // 只有一项的过程直接展示，不参与折叠分组（与 sync 的建组规则一致）
    assigned.forEach(function (pair) {
      const info = historyGroups.get(pair[1]);
      if (info.stages > 1) {
        recordKeys.set(pair[0], pair[1]); recordGroups.set(pair[0], info);
      } else { recordKeys.delete(pair[0]); recordGroups.delete(pair[0]); }
    });
  }
  function windowBounds(records, start, end) {
    indexRecords(records);
    const first = records[start], last = records[end - 1];
    const left = first && recordGroups.get(first), right = last && recordGroups.get(last);
    if (left && choices.get(recordKeys.get(first)) !== true) start = left.start;
    if (right && choices.get(recordKeys.get(last)) !== true) end = right.end;
    // 轮级大折叠的窗口吸附（已折叠轮不切半轮）；无该模块时保持原边界
    if (App.roundFold) return App.roundFold.snapBounds(records, start, end);
    return {start, end};
  }
  function tagRecord(node, record) {
    if (recordKeys.has(record)) node._processHistoryKey = recordKeys.get(record);
  }
  App.processGroups = { sync, reveal, setTimestamp, estimateHeights, indexRecords, tagRecord, windowBounds,
    revision: function () { return choiceRevision; } };
})(window.App);
