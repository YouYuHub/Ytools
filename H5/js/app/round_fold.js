/** 轮级大折叠：每轮只保留最新一段正文，其余收进摘要条（Codex 同款）。
    机制沿用 processGroups：成员原位隐藏（is-roundfold-hidden），不搬运 DOM；
    小折叠逻辑完整保留（过程组头一并收进大折叠，展开后按各自状态显示）。
    直播 msg 容器与扁平历史（按用户节点分轮）两种布局都支持。 */
(function (App) {
  "use strict";
  // 可折叠成员（只看容器直接子节点）：正文/思考/工具/子任务/通知/过程组头/时间行/用量行
  // （用量行在 syncRound 里特殊处理：每轮只保留最后一条）
  var MEMBER_SELECTOR = ".msg-assistant-body, .think-block, .tool-block, .agent-block, " +
    ".notice-bar, .process-group-summary, .msg-time, .round-usage";
  // 永不折叠：用户节点/压缩条（会话级）/提问卡（可交互）/加载钮/自家头/隐藏节点/轮容器
  // （用量行单独处理：每轮只保留最后一条，其余随折叠内容隐藏）
  var EXCLUDE_SELECTOR = ".msg-user, .compaction-bar, .ask-block, " +
    ".qnav-load-older, .round-fold-summary, .history-live-hidden, .msg";
  // 记录级成员口径（estimateHeights/windowBounds 用，与 DOM 口径对齐）：
  // user/usage/compaction 永不折叠；toolResult/approval 无独立节点（已按 0 计）
  var MEMBER_KINDS = { think: true, tool: true, agentBlock: true, notice: true, assistant: true };
  var FOLD_HEADER_HEIGHT = 36;
  var foldsByContainer = new WeakMap(); // container -> Map(key -> fold)
  var allFolds = new Map(); // key -> fold（estimate/snap 用 DOM 真值优先）
  var choices = new Map(); // key -> open（跨重建记忆）
  var choiceRevision = 0;
  var sequence = 0;
  // 用时实时跳动：全局共享一个 1s 计时器，只追踪运行中的折叠；
  // 等待确认/已结束的轮次冻结在最后值（避免通宵挂起的审批计时失控）。
  var tickingFolds = new Set();
  var tickTimer = null;

  function sessionId() { return (App.state && App.state.sessionId) || ""; }
  function roundKey(round) { return sessionId() + ":r" + String(round); }
  function identityKey(node) {
    if (!node._roundFoldIdentity) node._roundFoldIdentity = "live-" + (++sequence);
    return sessionId() + ":" + node._roundFoldIdentity;
  }
  function keyForUserNode(user) {
    return user.dataset.round != null ? roundKey(user.dataset.round) : identityKey(user);
  }
  function keyForMsg(msg) {
    return msg.dataset.round != null ? roundKey(msg.dataset.round) : identityKey(msg);
  }
  function setText(node, value) {
    if (node.textContent !== value) node.textContent = value;
  }
  function isFoldable(node) {
    return node.nodeType === 1 && node.matches(MEMBER_SELECTOR) && !node.matches(EXCLUDE_SELECTOR);
  }
  function isTailBody(node) {
    return node.nodeType === 1 && node.matches(".msg-assistant-body") && !node.matches(EXCLUDE_SELECTOR);
  }
  function remember(fold) {
    if (choices.get(fold.key) !== fold.open) choiceRevision++;
    choices.set(fold.key, fold.open);
    if (choices.size > 500) choices.delete(choices.keys().next().value);
  }
  function applyVisibility(fold) {
    fold.button.setAttribute("aria-expanded", String(fold.open));
    fold.header.classList.toggle("is-open", fold.open);
    fold.members.forEach(function (node) {
      node.classList.toggle("is-roundfold-hidden", !fold.open);
    });
  }
  function openFold(fold, open, user) {
    fold.open = open;
    if (user) remember(fold);
    applyVisibility(fold);
    if (App.historyScroll) App.historyScroll.refresh({ preserve: false });
    if (App.updateCodeblockCopyButtons) App.updateCodeblockCopyButtons();
  }
  function fmtDuration(ms) {
    if (!(ms > 0)) return "";
    var total = Math.round(ms / 1000);
    if (total < 1) return "";
    if (total < 60) return total + "s";
    var minutes = Math.floor(total / 60);
    if (minutes < 60) {
      var rest = total % 60;
      return rest ? minutes + "m " + rest + "s" : minutes + "m";
    }
    return Math.floor(minutes / 60) + "h " + (minutes % 60) + "m";
  }
  function renderHeader(fold) {
    var tools = 0, agents = 0, bodies = 0, thinks = 0, failures = 0;
    var waiting = null, active = false;
    var times = [];
    fold.members.forEach(function (node) {
      if (node.matches(".tool-block")) tools++;
      else if (node.matches(".agent-block")) agents++;
      else if (node.matches(".msg-assistant-body")) bodies++;
      else if (node.matches(".think-block")) thinks++;
      if (node.matches(".is-error, .is-failed, .compaction-interrupted") ||
        node.querySelector(".tool-block-tool-icon.is-failed")) failures++;
      if (node.matches(".is-waiting")) waiting = node;
      if (node.matches(".is-streaming, .is-executing, .is-running") ||
        node.querySelector(".tool-status-icon")) active = true;
      var stamp = Date.parse(node.dataset.stageTimestamp || "");
      if (!isNaN(stamp)) times.push(stamp);
    });
    // 尾正文时间计入用时（整轮跨度）；流式光标视为进行中
    if (fold.tail) {
      var tailStamp = Date.parse(fold.tail.dataset.stageTimestamp || "");
      if (!isNaN(tailStamp)) times.push(tailStamp);
      if (fold.tail.matches(".msg-cursor")) active = true;
    }
    var counts = [];
    if (tools) counts.push(tools + " 次工具调用");
    if (agents) counts.push(agents + " 个子任务");
    if (thinks) counts.push(thinks + " 段思考");
    if (bodies) counts.push(bodies + " 段正文");
    if (failures) counts.push(failures + " 项异常");
    var duration = "";
    if (times.length >= 2) duration = fmtDuration(Math.max.apply(null, times) - Math.min.apply(null, times));
    setText(fold.duration, duration ? "用时 " + duration : "");
    setText(fold.counts, counts.join(" · "));
    fold.button.title = [fold.duration.textContent, fold.counts.textContent].filter(Boolean).join(" · ") ||
      "折叠本轮此前内容";
    fold.header.classList.toggle("has-error", failures > 0);
    fold.header.classList.toggle("is-waiting", Boolean(waiting));
    fold.header.classList.toggle("is-running", Boolean(active && !waiting));
    // 有待确认项时强制展开一次（审批/等待确认绝不能藏起来）；用户随后仍可手动收起
    if (waiting && waiting !== fold.waiting) {
      fold.open = true;
      applyVisibility(fold);
    }
    fold.waiting = waiting;
    ensureTicking(fold);
  }
  // 轮次起始时间（成员+尾最早的时间戳）；无时间戳返回 0
  function foldStartTime(fold) {
    var min = Infinity;
    function feed(node) {
      var stamp = Date.parse(node.dataset.stageTimestamp || "");
      if (!isNaN(stamp) && stamp < min) min = stamp;
    }
    fold.members.forEach(feed);
    if (fold.tail) feed(fold.tail);
    return min === Infinity ? 0 : min;
  }
  // 当前是否仍在运行（读实时 DOM，不依赖上次 render 的 class 快照）
  function foldActiveNow(fold) {
    var active = false;
    var nodes = fold.tail ? fold.members.concat([fold.tail]) : fold.members;
    for (var i = 0; i < nodes.length; i++) {
      var node = nodes[i];
      if (node.matches(".is-waiting")) return false;
      if (node.matches(".is-streaming, .is-executing, .is-running, .msg-cursor") ||
        node.querySelector(".tool-status-icon")) active = true;
    }
    return active;
  }
  function maybeStopTimer() {
    if (!tickingFolds.size && tickTimer != null && typeof clearInterval === "function") {
      clearInterval(tickTimer);
      tickTimer = null;
    }
  }
  function tickFold(fold) {
    if (!fold.header.isConnected) { tickingFolds.delete(fold); return; }
    if (!foldActiveNow(fold)) { tickingFolds.delete(fold); return; }
    var start = foldStartTime(fold);
    if (!start) return;
    var text = fmtDuration(Date.now() - start);
    setText(fold.duration, text ? "用时 " + text : "");
  }
  // 每秒刷新一次运行中折叠的用时（导出供单测确定性驱动）
  function tick() {
    tickingFolds.forEach(tickFold);
    maybeStopTimer();
  }
  function ensureTicking(fold) {
    if (fold.header.classList.contains("is-running")) tickingFolds.add(fold);
    else tickingFolds.delete(fold);
    if (tickingFolds.size && tickTimer == null && typeof setInterval === "function") {
      tickTimer = setInterval(tick, 1000);
    }
  }
  function buildFold(container, key) {
    var header = App.el("div", "round-fold-summary");
    var button = App.el("button", "round-fold-toggle");
    button.type = "button";
    button.innerHTML = '<svg class="icon round-fold-chevron" viewBox="0 0 24 24" aria-hidden="true"><path d="m9 18 6-6-6-6"/></svg><span class="round-fold-spinner" aria-hidden="true"></span>';
    var duration = App.el("span", "round-fold-duration");
    var counts = App.el("span", "round-fold-counts");
    button.append(duration, counts);
    header.appendChild(button);
    var fold = { container: container, key: key, header: header, button: button,
      duration: duration, counts: counts, members: [], tail: null, open: false, waiting: null };
    header._roundFold = fold;
    button.addEventListener("click", function () {
      var top = header.getBoundingClientRect().top;
      if (App.pauseAutoScroll) App.pauseAutoScroll();
      openFold(fold, !fold.open, true);
      // 折叠长轮时把摘要条留在原位置，避免视口跳到别的回答
      var delta = header.getBoundingClientRect().top - top;
      if (App.chatScroll && Math.abs(delta) > 0.5) {
        var behavior = App.chatScroll.style.scrollBehavior;
        App.chatScroll.style.scrollBehavior = "auto";
        App.chatScroll.scrollTop += delta;
        App.chatScroll.style.scrollBehavior = behavior;
      }
    });
    return fold;
  }
  function disbandFold(map, fold) {
    tickingFolds.delete(fold);
    maybeStopTimer();
    fold.header.remove();
    fold.header.remove();
    fold.members.forEach(function (node) {
      if (node._roundFold !== fold) return;
      delete node._roundFold;
      node.classList.remove("is-roundfold-hidden");
    });
    // choices 保留（重建后仍按用户选择恢复）；allFolds 注销
    if (allFolds.get(fold.key) === fold) allFolds.delete(fold.key);
    map.delete(fold.key);
  }
  function foldsOf(container) {
    var map = foldsByContainer.get(container);
    if (!map) {
      map = new Map();
      foldsByContainer.set(container, map);
    }
    return map;
  }
  // 一轮内容同步：tail 取最后一段正文（即使尚空——流式新段先占位，可见地流出）；
  // 只收尾正文之前的节点，尾之后的进行中内容（运行工具/小折叠）保持可见。
  // 无正文（纯工具轮）或尾前无成员时不建折叠。
  function syncRound(nodes, container, key) {
    var map = foldsOf(container);
    var tailIdx = -1;
    for (var i = nodes.length - 1; i >= 0; i--) {
      if (isTailBody(nodes[i])) { tailIdx = i; break; }
    }
    var tail = tailIdx >= 0 ? nodes[tailIdx] : null;
    // 成员只取尾之前的节点；用量行同理（尾之前的旧用量折叠，尾之后最新一条保留）
    var members = tail ? nodes.slice(0, tailIdx).filter(function (node) { return isFoldable(node); }) : [];
    var fold = map.get(key);
    if (!tail || !members.length) {
      if (fold) disbandFold(map, fold);
      return;
    }
    if (!fold) {
      fold = buildFold(container, key);
      map.set(key, fold);
    }
    allFolds.set(key, fold);
    // 跨容器认领：作用域内被其它 fold 标记过的节点（用量行收养/窗口移动）先与
    // 旧 fold 脱钩并恢复可见，再按本轮重新安置；被掏空的旧 fold 就地解散。
    nodes.forEach(function (node) {
      var other = node._roundFold;
      if (!other || other === fold) return;
      delete node._roundFold;
      node.classList.remove("is-roundfold-hidden");
      if (other.members) {
        var at = other.members.indexOf(node);
        if (at >= 0) other.members.splice(at, 1);
      }
      if (other.members && !other.members.length && other.container) {
        var otherMap = foldsByContainer.get(other.container);
        if (otherMap && otherMap.get(other.key) === other) disbandFold(otherMap, other);
      }
    });
    fold.members.forEach(function (node) {
      if (node._roundFold === fold && members.indexOf(node) < 0) {
        delete node._roundFold;
        node.classList.remove("is-roundfold-hidden");
      }
    });
    fold.members = members;
    fold.tail = tail;
    members.forEach(function (node) { node._roundFold = fold; });
    var waiting = members.some(function (node) { return node.matches && node.matches(".is-waiting"); });
    if (waiting) fold.open = true;
    else if (choices.has(key)) fold.open = choices.get(key);
    else fold.open = false;
    if (fold.header.nextElementSibling !== members[0]) container.insertBefore(fold.header, members[0]);
    applyVisibility(fold);
    renderHeader(fold);
  }
  function pruneStale(container, seen) {
    var map = foldsByContainer.get(container);
    if (!map) return;
    Array.from(map.keys()).forEach(function (key) {
      if (seen.indexOf(key) < 0) disbandFold(map, map.get(key));
    });
  }
  function syncMsgContainer(msg) {
    syncRound(Array.from(msg.children), msg, keyForMsg(msg));
    pruneStale(msg, [keyForMsg(msg)]);
    sweepStrays(msg);
  }
  // 孤儿头清扫：移除不属于任何现存 fold 的摘要头（窗口恢复/重建残留的旧头，
  // 无它则幽灵头越积越多且点击无响应——成员早已不在）。
  function sweepStrays(container) {
    var map = foldsByContainer.get(container);
    var live = [];
    if (map) map.forEach(function (fold) { live.push(fold.header); });
    Array.from(container.children).forEach(function (node) {
      if (node.nodeType === 1 && node.matches(".round-fold-summary") && live.indexOf(node) < 0) {
        node.remove();
      }
    });
  }
  function sync(container) {
    if (!container || !container.children) return;
    // 直播轮容器：整体一轮
    if (container !== App.chatInner && container.matches &&
      container.matches(".msg") && !container.matches(".msg-user")) {
      syncMsgContainer(container);
      return;
    }
    // 扁平历史：按用户节点/.msg 分段；段头之前（顶部杂散节点）不折叠
    var seen = [];
    var rangeUser = null, rangeNodes = [];
    function flush() {
      if (rangeUser) {
        var key = keyForUserNode(rangeUser);
        syncRound(rangeNodes, container, key);
        seen.push(key);
      }
      rangeUser = null;
      rangeNodes = [];
    }
    Array.from(container.children).forEach(function (node) {
      if (node.nodeType !== 1) return;
      if (node.matches(".msg-user")) { flush(); rangeUser = node; return; }
      if (node.matches(".msg")) { flush(); syncMsgContainer(node); return; }
      if (rangeUser) rangeNodes.push(node);
    });
    flush();
    pruneStale(container, seen);
    sweepStrays(container);
  }
  function reveal(node) {
    for (var current = node; current; current = current.parentElement) {
      if (current._roundFold) openFold(current._roundFold, true, true);
      if (current === App.chatInner) break;
    }
  }
  function isOpen(key) {
    if (allFolds.has(key)) return allFolds.get(key).open;
    return choices.get(key) === true;
  }
  // 记录级折叠判定（estimateHeights/windowBounds 用）：返回轮内成员下标与尾下标
  function foldableRange(records, round) {
    var wanted = String(round);
    var idxs = [];
    for (var i = 0; i < records.length; i++) {
      if (records[i] && String(records[i].round) === wanted) idxs.push(i);
    }
    if (!idxs.length) return null;
    var tail = -1;
    for (var k = idxs.length - 1; k >= 0; k--) {
      if (records[idxs[k]].kind === "assistant") { tail = idxs[k]; break; }
    }
    if (tail < 0) return null;
    // 成员只取尾之前的记录（与 DOM 口径一致）；用量行同理（尾之前的旧用量折叠）
    var members = idxs.filter(function (i) {
      if (i >= tail) return false;
      return MEMBER_KINDS[records[i].kind] || records[i].kind === "usage";
    });
    if (!members.length) return null;
    return { start: idxs[0], end: idxs[idxs.length - 1] + 1, tail: tail, members: members };
  }
  // 高度估算后处理：已渲染节点以实测为准（含隐藏=0）；未渲染且被折叠的记 0，
  // 首个成员记折叠头高度（与 processGroups 的 36 口径一致）
  function adjustHeights(records, heights, measured) {
    if (!Array.isArray(records) || !Array.isArray(heights)) return;
    var rounds = {};
    records.forEach(function (rec) {
      if (rec && rec.round != null) rounds[String(rec.round)] = true;
    });
    Object.keys(rounds).forEach(function (round) {
      if (isOpen(roundKey(round))) return;
      var range = foldableRange(records, round);
      if (!range) return;
      range.members.forEach(function (i, n) {
        if (n === 0) { heights[i] = FOLD_HEADER_HEIGHT; return; }
        if (measured && measured.has(records[i])) return;
        heights[i] = 0;
      });
    });
  }
  // 窗口边界吸附：窗口切进已折叠轮的成员区时扩到整轮，避免半轮渲染
  function snapBounds(records, start, end) {
    if (!Array.isArray(records) || !(end > start)) return { start: start, end: end };
    function snapEdge(index, isStart) {
      var fallback = isStart ? index : index + 1;
      var rec = records[index];
      if (!rec || rec.round == null || isOpen(roundKey(rec.round))) return fallback;
      var range = foldableRange(records, rec.round);
      if (!range || range.members.indexOf(index) < 0) return fallback;
      return isStart ? range.start : range.end;
    }
    return { start: snapEdge(start, true), end: snapEdge(end - 1, false) };
  }
  function revision() { return choiceRevision; }
  App.roundFold = { sync: sync, reveal: reveal, adjustHeights: adjustHeights,
    snapBounds: snapBounds, revision: revision, tick: tick };
})(window.App);
