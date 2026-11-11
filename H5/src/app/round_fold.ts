/** 轮级大折叠：每轮只保留最新一段正文，其余收进摘要条（Codex 同款）。
   机制沿用 processGroups：成员原位隐藏（is-roundfold-hidden），不搬运 DOM；
   小折叠逻辑完整保留（过程组头一并收进大折叠，展开后按各自状态显示）。
   直播 msg 容器与扁平历史（按用户节点分轮）两种布局都支持。
   尾后收纳：尾正文之后已完成的过程节点（暂停/中断冻结的残留，续跑新开
   容器后永远到不了尾前）一并收进折叠，只有进行中/待确认内容与最新用量
   行保持可见；单小块不折叠（单个思考/工具/子任务等自带折叠，不套大折叠，
   单段旧正文除外——流式进行态仍需折叠）。
   计时：运行中（DOM 进行态，或本容器是当前会话直播流）每秒跳动，即使在
   等待模型的间隙也不停；待确认一票否决冻结；暂停/结束停表并最后渲染一次
   （摘转圈、冻结静态跨度）；继续后复用同一容器恢复跳动（暂停间隙从用时中
   扣除，秒表语义）。
   尾上时间戳：紧贴尾正文上方的时间行永不折叠（尾的注脚）。 */
(function (App: any) {
  "use strict";
  // 可折叠成员（只看容器直接子节点）：正文/思考/工具/子任务/通知/过程组头/时间行/用量行
  // （用量行在 syncRound 里特殊处理：每轮只保留最后一条）
  var MEMBER_SELECTOR = ".msg-assistant-body, .think-block, .tool-block, .agent-block, " +
    ".notice-bar, .process-group-summary, .msg-time, .round-usage";
  // 永不折叠：用户节点/压缩条（会话级）/提问卡（可交互）/加载钮/自家头/隐藏节点/轮容器
  // （用量行单独处理：每轮只保留最后一条，其余随折叠内容隐藏）
  var EXCLUDE_SELECTOR = ".msg-user, .compaction-bar, .ask-block, " +
    ".qnav-load-older, .round-fold-summary, .history-live-hidden, .msg, .live-round-timer";
  // 记录级成员口径（estimateHeights/windowBounds 用，与 DOM 口径对齐）：
  // user/usage/compaction 永不折叠；toolResult/approval 无独立节点（已按 0 计）
  var MEMBER_KINDS: any = { think: true, tool: true, agentBlock: true, notice: true, assistant: true };
  var FOLD_HEADER_HEIGHT = 36;
  var foldMap: any = new Map(); // key -> fold（全局唯一；同轮扁平+容器合并后不再分容器）
  var allFolds: any = new Map(); // key -> fold（estimate/snap 用 DOM 真值优先）
  var choices: any = new Map(); // key -> open（跨重建记忆）
  var choiceRevision = 0;
  var sequence = 0;
  // 用时实时跳动：全局共享一个 1s 计时器，只追踪运行中的折叠；
  // 等待确认/已结束的轮次冻结在最后值（避免通宵挂起的审批计时失控）。
  var tickingFolds: any = new Set();
  var tickTimer: any = null;

  function sessionId(): string { return (App.state && App.state.sessionId) || ""; }
  function roundKey(round: any): string { return sessionId() + ":r" + String(round); }
  function identityKey(node: any): string {
    if (!node._roundFoldIdentity) node._roundFoldIdentity = "live-" + (++sequence);
    return sessionId() + ":" + node._roundFoldIdentity;
  }
  function keyForUserNode(user: any): string {
    return user.dataset.round != null ? roundKey(user.dataset.round) : identityKey(user);
  }
  function keyForMsg(msg: any): string {
    return msg.dataset.round != null ? roundKey(msg.dataset.round) : identityKey(msg);
  }
  function setText(node: any, value: any): void {
    if (node.textContent !== value) node.textContent = value;
  }
  function isFoldable(node: any): any {
    return node.nodeType === 1 && node.matches(MEMBER_SELECTOR) && !node.matches(EXCLUDE_SELECTOR);
  }
  function isTailBody(node: any): any {
    return node.nodeType === 1 && node.matches(".msg-assistant-body") && !node.matches(EXCLUDE_SELECTOR);
  }
  // 进行中/待确认节点：流式光标、执行中过程、待审批——尾后保持可见，绝不收进
  // 折叠（与 renderHeader/foldActiveNow 的运行判定同口径，is-waiting 亦视为 live）。
  function isLiveNode(node: any): any {
    return node.matches(".is-streaming, .is-executing, .is-running, .msg-cursor, .is-waiting") ||
      node.querySelector(".tool-status-icon");
  }
  function remember(fold: any): void {
    if (choices.get(fold.key) !== fold.open) choiceRevision++;
    choices.set(fold.key, fold.open);
    if (choices.size > 500) choices.delete(choices.keys().next().value);
  }
  function applyVisibility(fold: any): void {
    fold.button.setAttribute("aria-expanded", String(fold.open));
    fold.header.classList.toggle("is-open", fold.open);
    fold.members.forEach(function (node: any) {
      node.classList.toggle("is-roundfold-hidden", !fold.open);
    });
  }
  function openFold(fold: any, open: any, user: any): void {
    fold.open = open;
    if (user) remember(fold);
    applyVisibility(fold);
    if (App.historyScroll) App.historyScroll.refresh({ preserve: false });
    if (App.updateCodeblockCopyButtons) App.updateCodeblockCopyButtons();
  }
  function fmtDuration(ms: any): string {
    if (!(ms > 0)) return "";
    var total = Math.round(ms / 1000);
    if (total < 1) return "";
    if (total < 60) return total + "s";
    var minutes = Math.floor(total / 60);
    if (minutes < 60) {
      var rest = total % 60;
      return rest ? minutes + "m " + rest + "s" : minutes + "m";
    }
    var hours = Math.floor(minutes / 60);
    var minRest = minutes % 60;
    var secRest = total % 60;
    var out = hours + "h " + minRest + "m";
    return secRest ? out + " " + secRest + "s" : out;
  }
  function renderHeader(fold: any): void {
    var tools = 0, agents = 0, bodies = 0, thinks = 0, failures = 0;
    var waiting: any = null, active = false;
    var times: any[] = [];
    fold.members.forEach(function (node: any) {
      if (node.matches(".tool-block")) tools++;
      else if (node.matches(".agent-block")) agents++;
      else if (node.matches(".msg-assistant-body")) bodies++;
      else if (node.matches(".think-block")) thinks++;
      if (node.matches(".is-error, .is-failed, .compaction-interrupted") ||
        node.querySelector(".tool-block-tool-icon.is-failed")) failures++;
      if (node.matches(".is-waiting")) waiting = node;
      if (node.matches(".is-streaming, .is-executing, .is-running") ||
        node.querySelector(".tool-status-icon")) active = true;
      pushSpan(times, node);
    });
    // 尾正文时间计入用时（整轮跨度）；流式光标视为进行中
    if (fold.tail) {
      pushSpan(times, fold.tail);
      if (fold.tail.matches(".msg-cursor")) active = true;
    }
    // 尾后可见区（进行中/待确认/整组可见的小折叠）计入状态与用时，不计入折叠统计
    (fold.liveTail || []).forEach(function (node: any) {
      if (node.matches(".is-error, .is-failed, .compaction-interrupted") ||
        node.querySelector(".tool-block-tool-icon.is-failed")) failures++;
      if (node.matches(".is-waiting")) waiting = node;
      if (node.matches(".is-streaming, .is-executing, .is-running, .msg-cursor") ||
        node.querySelector(".tool-status-icon")) active = true;
      pushSpan(times, node);
    });
    var floor = floorFor(fold);
    if (floor) times.push(floor);
    var counts: any[] = [];
    if (tools) counts.push(tools + " 次工具调用");
    if (agents) counts.push(agents + " 个子任务");
    if (thinks) counts.push(thinks + " 段思考");
    if (bodies) counts.push(bodies + " 段正文");
    if (failures) counts.push(failures + " 项异常");
    // 直播流等待模型时同样亮运行态（转圈 + 计时），与 foldActiveNow 同口径；
    // 待确认 veto：审批等待期间不亮灯、不计时。非末段（无尾）不用该兜底。
    if (!waiting && !active && fold.tail && isLiveFold(fold)) active = true;
    var duration = "";
    if (times.length >= 2) {
      var spanStart = Math.min.apply(null, times), spanEnd = Math.max.apply(null, times);
      duration = fmtDuration(spanEnd - spanStart - foldGapMs(fold, spanStart, spanEnd));
    }
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
  // 暂停间隙（秒表语义）：管线 finish() 记录起点，续跑复用容器时闭合；
  // 用时扣掉已闭合间隙与 [start, end] 的重叠部分。开口间隙（正常结束/永不
  // 续跑）贡献为 0。存在容器节点上，折叠重建不丢失，随节点移除而回收。
  function recordPauseGap(container: any): void {
    if (!container) return;
    if (!container._foldPauseGaps) container._foldPauseGaps = [];
    var gaps = container._foldPauseGaps;
    if (gaps.length > 50) gaps.splice(0, gaps.length - 50);
    gaps.push({ pauseAt: Date.now(), resumeAt: 0 });
  }
  function closePauseGap(container: any): void {
    var gaps = container && container._foldPauseGaps;
    if (!gaps) return;
    for (var i = gaps.length - 1; i >= 0; i--) {
      if (!gaps[i].resumeAt) { gaps[i].resumeAt = Date.now(); return; }
    }
  }
  function foldGapMs(fold: any, start: any, end: any): number {
    var gaps = (fold.container && fold.container._foldPauseGaps) || [];
    var total = 0;
    for (var i = 0; i < gaps.length; i++) {
      var p = gaps[i].pauseAt, r = gaps[i].resumeAt;
      if (!(r > p)) continue;
      var overlap = Math.min(r, end) - Math.max(p, start);
      if (overlap > 0) total += overlap;
    }
    return total;
  }
  // 轮次起始时间（成员+尾最早的开始戳）；无时间戳返回 0
  function foldStartTime(fold: any): any {
    var min = Infinity;
    function feed(node: any): void {
      var stamp = tsStart(node);
      if (stamp > 0 && stamp < min) min = stamp;
    }
    fold.members.forEach(feed);
    if (fold.tail) feed(fold.tail);
    (fold.liveTail || []).forEach(feed);
    var floor = floorFor(fold);
    if (floor && floor < min) min = floor;
    return min === Infinity ? 0 : min;
  }
  // 节点起止戳：开始（创建）与结束（最后活动）分槽——长工具调用/长回复
  // 若只用创建时间，段内时长会被系统性吞掉。结束缺失回退开始。
  function tsStart(node: any): number {
    var t = Date.parse(node.dataset.stageTimestamp || "");
    return isNaN(t) ? 0 : t;
  }
  function tsEnd(node: any): number {
    var t = Date.parse(node.dataset.stageEndTimestamp || "");
    if (!isNaN(t)) return t;
    return tsStart(node);
  }
  function pushSpan(times: any, node: any): void {
    var s = tsStart(node), e = tsEnd(node);
    if (s > 0) times.push(s);
    if (e > 0 && e !== s) times.push(e);
  }
  // 当前是否仍在运行（读实时 DOM，不依赖上次 render 的 class 快照）
  function foldActiveNow(fold: any): any {
    var active = false;
    var nodes = fold.tail ? fold.members.concat([fold.tail], fold.liveTail || []) : fold.members;
    for (var i = 0; i < nodes.length; i++) {
      var node = nodes[i];
      // 待确认一票否决：审批等待期间冻结计时（通宵挂起也不跳）
      if (node.matches(".is-waiting")) return false;
      if (node.matches(".is-streaming, .is-executing, .is-running, .msg-cursor") ||
        node.querySelector(".tool-status-icon")) active = true;
    }
    // 等待模型：DOM 上无进行态（工具刚结束、下一段输出未到），但本容器仍是
    // 当前会话的直播流 → 任务活着，视为运行中（否则每次等模型都停表）。
    // 暂停/结束时 streaming 清掉、activeStream 置空，自然停表；继续后复用容器
    // 重新接上 activeStream，自然恢复（暂停间隙从用时中扣除，见 foldGapMs）。
    // 非末段（无尾）永不适用该兜底：段内容已结算，后续输出属于更晚的段，
    // 若跟随跳秒会导致同轮多个折叠块同时计时、读数还可能相同。
    if (!active && fold.tail && isLiveFold(fold)) active = true;
    return active;
  }
  // 本折叠所在容器是否当前会话的直播流容器
  function isLiveFold(fold: any): any {
    var st = App.state;
    if (!st || !st.streaming || !st.activeStream) return false;
    if (st.streamingSession !== sessionId()) return false;
    return fold.container === st.activeStream.messageNode;
  }
  // 进行中的直播容器（跨会话豁免，不校验当前会话/ streaming 标志）：
  // 切走看别的会话时历史同步不得解散/劫持它；完成/废弃的流不豁免。
  function activeLiveContainer(): any {
    var st = App.state;
    if (!st || !st.activeStream || !st.activeStream.messageNode) return null;
    if (st.activeStream.completed || st.activeStream.detached) return null;
    return st.activeStream.messageNode;
  }
  function isSparedLiveFold(fold: any): any {
    var live = activeLiveContainer();
    if (!live || !fold || fold.container !== live) return false;
    // 仅游离期豁免（切会话/窗口重建，容器暂时脱离 chatInner）：挂回后
    // 参与正常合并；轮次号迁移（live-X → rN）等挂载态键变更不豁免，
    // 否则旧键折叠永远不清，会留下双头。
    return live.parentNode !== App.chatInner;
  }
  // 直播轮计时下限：附接回放重建的节点时间戳都是“现在”，用 marker 下发的
  // 轮次开始墙钟兜底（存容器上，随续跑复用延续）。非直播折叠、缺失、非法、
  // 未来值（时钟偏斜）一律返回 0，即退回纯节点时间戳口径。
  function liveStartFloor(fold: any): number {
    if (!isLiveFold(fold)) return 0;
    var at = fold.container && (fold.container as any)._foldRoundStartedAt;
    if (typeof at !== "number" || !(at > 0) || at > Date.now()) return 0;
    return at;
  }
  // 计时下限三级口径（从高到低）：
  // 1) 直播后端下发的轮次开始墙钟（最准：发送→首输出的间隙也计入）；
  // 2) 直播期间缓存的下限：流结束后 live 判定失效，用缓存延续——
  //    否则完成瞬间重算会丢掉开始→首节点的间隙，用时缩水；
  // 3) 历史兜底：用户发送时间（刷新后直播缓存丢失时用）。1 小时 sanity：
  //    重发旧轮的用户气泡时间戳远早于成员，误用会把用时拉长到天级，
  //    此时退回纯成员口径（只计量本次重发工作）。
  var HISTORY_FLOOR_MAX_GAP = 3600000;
  function floorFor(fold: any): number {
    // 引导段不用直播轮次起点（那是整轮开始，会把本段用时拉长回轮首）
    var live = fold.allowLiveFloor === false ? 0 : liveStartFloor(fold);
    if (live > 0) { fold.startFloor = live; return live; }
    if (fold.startFloor > 0) return fold.startFloor;
    var userTs = fold.userTs || 0;
    if (!(userTs > 0)) return 0;
    var min = Infinity;
    fold.members.forEach(function (node: any) {
      var stamp = tsStart(node);
      if (stamp > 0 && stamp < min) min = stamp;
    });
    if (fold.tail) {
      var tailStamp = tsStart(fold.tail);
      if (tailStamp > 0 && tailStamp < min) min = tailStamp;
    }
    (fold.liveTail || []).forEach(function (node: any) {
      var liveStamp = tsStart(node);
      if (liveStamp > 0 && liveStamp < min) min = liveStamp;
    });
    if (!(min < Infinity) || userTs > min || min - userTs > HISTORY_FLOOR_MAX_GAP) return 0;
    return userTs;
  }
  function maybeStopTimer(): void {
    if (!tickingFolds.size && tickTimer != null && typeof clearInterval === "function") {
      clearInterval(tickTimer);
      tickTimer = null;
    }
  }
  function tickFold(fold: any): void {
    if (!fold.header.isConnected) { tickingFolds.delete(fold); return; }
    // 停表沿：最后渲染一次，把转圈摘掉、用时冻结为静态跨度（收尾 sync 发生在
    // streaming 清掉之前，头状态会滞后一个节拍，不补这次就会转圈常亮）。
    if (!foldActiveNow(fold)) { tickingFolds.delete(fold); renderHeader(fold); return; }
    var start = foldStartTime(fold);
    // 跳秒起点与静态口径对齐（含直播下限/用户兜底），否则刷新后续跑计时从 0 起跳
    var floor = floorFor(fold);
    if (floor > 0 && (!start || floor < start)) start = floor;
    if (!start) return;
    var now = Date.now();
    var text = fmtDuration(now - start - foldGapMs(fold, start, now));
    setText(fold.duration, text ? "用时 " + text : "");
  }
  // 每秒刷新一次运行中折叠的用时（导出供单测确定性驱动）
  function tick(): void {
    tickingFolds.forEach(tickFold);
    maybeStopTimer();
  }
  function ensureTicking(fold: any): void {
    if (fold.header.classList.contains("is-running")) tickingFolds.add(fold);
    else tickingFolds.delete(fold);
    if (tickingFolds.size && tickTimer == null && typeof setInterval === "function") {
      tickTimer = setInterval(tick, 1000);
    }
  }
  function buildFold(container: any, key: any): any {
    var header = App.el("div", "round-fold-summary");
    var button = App.el("button", "round-fold-toggle");
    button.type = "button";
    button.innerHTML = '<svg class="icon round-fold-chevron" viewBox="0 0 24 24" aria-hidden="true"><path d="m9 18 6-6-6-6"/></svg>';
    var duration = App.el("span", "round-fold-duration");
    var counts = App.el("span", "round-fold-counts");
    button.append(duration, counts);
    header.appendChild(button);
    var fold = { container: container, key: key, header: header, button: button,
      duration: duration, counts: counts, members: [], tail: null, liveTail: [], open: false, waiting: null,
      startFloor: 0, userTs: 0, allowLiveFloor: true };
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
  function disbandFold(fold: any): void {
    tickingFolds.delete(fold);
    maybeStopTimer();
    fold.header.remove();
    fold.members.forEach(function (node: any) {
      if (node._roundFold !== fold) return;
      delete node._roundFold;
      node.classList.remove("is-roundfold-hidden");
    });
    // choices 保留（重建后仍按用户选择恢复）；allFolds 注销
    if (allFolds.get(fold.key) === fold) allFolds.delete(fold.key);
    foldMap.delete(fold.key);
  }
  // 非末段成员收集：整组收纳（任一成员进行中/搬离/跨段则整组可见，
  // 避免头藏起来而成员裸露）；进行中/待确认与轮内最新用量行保持可见。
  function closedMembers(nodes: any, roundLastUsage: any): any[] {
    var members: any[] = [];
    var seenGroups: any[] = [];
    nodes.forEach(function (node: any) {
      if (!isFoldable(node) || isLiveNode(node) || node === roundLastUsage) return;
      var g = node._processGroup ||
        (node.matches(".process-group-summary") ? node._processSummary : null);
      if (!g) {
        if (members.indexOf(node) < 0) members.push(node);
        return;
      }
      if (seenGroups.indexOf(g) >= 0) return;
      seenGroups.push(g);
      var ms = g.members || [];
      for (var k = 0; k < ms.length; k++) {
        var m = ms[k];
        if (!m || nodes.indexOf(m) < 0) return;
        if (isLiveNode(m) || m === roundLastUsage) return;
      }
      ms.forEach(function (m: any) {
        if (m && m !== node && members.indexOf(m) < 0 && isFoldable(m)) members.push(m);
      });
      if (members.indexOf(node) < 0) members.push(node);
    });
    members.sort(function (a: any, b: any) { return nodes.indexOf(a) - nodes.indexOf(b); });
    return members;
  }
  // 段归属容器：段内有直播流容器成员则取它（直播判定/起点读取），否则取首成员容器
  function liveContainerFor(nodes: any): any {
    var liveMsg = App.state && App.state.activeStream ? App.state.activeStream.messageNode : null;
    if (liveMsg) {
      for (var i = 0; i < nodes.length; i++) {
        if (nodes[i].parentNode === liveMsg) return liveMsg;
      }
    }
    return nodes.length && nodes[0].parentNode ? nodes[0].parentNode : App.chatInner;
  }
  // 一轮内容同步：按引导气泡切分段（同轮第 2+ 个用户节点是引导），每段独立
  // 折叠/计时：前段停表封存（相当于已结束的轮），末段按常规规则（留最新正文）。
  // userNode：段首用户气泡（首段传轮次气泡；历史计时兜底用，可空）。
  // 返回本轮所有段 key（供调用方 pruneStale）。
  function syncRound(nodes: any, key: any, userNode: any): any[] {
    var segments: any[] = [];
    var head = userNode, current: any[] = [];
    nodes.forEach(function (node: any) {
      if (node.nodeType === 1 && node.matches(".msg-user")) {
        segments.push({ head: head, nodes: current });
        head = node;
        current = [];
        return;
      }
      current.push(node);
    });
    segments.push({ head: head, nodes: current });
    // 轮内最新用量行（跨段只保留一条可见）
    var roundLastUsage: any = null;
    for (var u = nodes.length - 1; u >= 0; u--) {
      if (nodes[u].nodeType === 1 && nodes[u].matches(".round-usage")) { roundLastUsage = nodes[u]; break; }
    }
    var keys: any[] = [];
    segments.forEach(function (seg: any, i: any) {
      var segKey = i === 0 ? key : key + ":s" + i;
      keys.push(segKey);
      syncSegment(seg.nodes, segKey, seg.head, i === segments.length - 1, roundLastUsage, i === 0);
    });
    return keys;
  }
  // 单段同步：末段 tail 取最后一段正文（即使尚空——流式新段先占位）；
  // 非末段无尾（内容已结算，引导在检查点消费，之前节点必然完成），全部收纳，
  // 只有进行中/待确认内容与轮内最新用量行保持可见。无有效成员时不建折叠。
  function syncSegment(nodes: any, key: any, userNode: any,
      isFinal: boolean, roundLastUsage: any, allowLiveFloor: boolean): void {
    var tailIdx = -1;
    if (isFinal) {
      for (var i = nodes.length - 1; i >= 0; i--) {
        if (isTailBody(nodes[i])) { tailIdx = i; break; }
      }
    }
    var tail = tailIdx >= 0 ? nodes[tailIdx] : null;
    // 压缩条 = 活动区分界：任务进行中触发压缩时，压缩条出现在最新正文之后，
    // 其后的工具调用/思考/压缩本身属于“压缩后的新阶段”。若按普通尾正文口径，
    // 这些块会被尾后收纳卷进顶部折叠，用户只看得到一段正文 + 一行折叠头，
    // 压缩过程完全不可见。处理：有压缩条晚于尾正文时，放弃尾后收纳，
    // 让压缩条及其前后内容全部保持可见。
    var compactCut = -1;
    if (tail) {
      for (var ci = nodes.length - 1; ci > tailIdx; ci--) {
        if (nodes[ci].nodeType === 1 && nodes[ci].matches(".compaction-bar")) { compactCut = ci; break; }
      }
    }
    // 尾上时间戳：紧贴尾正文上方的时间行是尾的注脚（可见正文何时开始），永不折叠
    var tailTime: any = null;
    if (tailIdx >= 1) {
      var prev = nodes[tailIdx - 1];
      if (prev.nodeType === 1 && prev.matches(".msg-time")) tailTime = prev;
    }
    // 成员只取尾之前的节点；用量行同理（尾之前的旧用量折叠，尾之后最新一条保留）。
    // 非末段无尾：整组收纳全部可折叠节点。
    var members = tail ? nodes.slice(0, tailIdx).filter(function (node: any) { return isFoldable(node) && node !== tailTime; })
      : closedMembers(nodes, roundLastUsage);
    // 尾后收纳：暂停/中断会把已完成的过程节点冻结在尾正文之后；续跑新开 .msg
    // 容器后它们永远到不了尾前，不收纳就会永久裸露（暂停几次就留下几块）。
    var liveTail: any[] = [];
    if (tail && compactCut < 0) {
      var lastUsage: any = roundLastUsage;
      var postMembers: any[] = [];
      var postStart = tailIdx + 1;
      var postHeads: any[] = [];
      for (var p = postStart; p < nodes.length; p++) {
        var node = nodes[p];
        if (!isFoldable(node)) continue;
        // 小折叠组头第二遍整体判定；已编组节点由所属组头认领（头失联则保持可见）
        if (node.matches(".process-group-summary")) { postHeads.push(node); continue; }
        if (node._processGroup && node._processGroup.header &&
          node._processGroup.header.isConnected) continue;
        if (isLiveNode(node)) continue;
        if (node.matches(".round-usage") && node === lastUsage) continue;
        postMembers.push(node);
      }
      // 尾后小折叠组整体收纳：成员全已完成才连头一起收；任一进行中/待确认或
      // 已搬离本容器则整组可见（避免头藏起来而成员裸露、或头留着而成员被掏空）。
      // 无小折叠 linkage 的裸头（如手搭节点）保守保持可见。
      postHeads.forEach(function (head: any) {
        var group = head._processSummary;
        if (!group || !group.members || !group.members.length) return;
        var joiners: any[] = [];
        for (var g = 0; g < group.members.length; g++) {
          var m = group.members[g];
          if (!m || nodes.indexOf(m) < 0) return;
          if (m === head) continue;
          if (!isFoldable(m)) {
            if (m.matches && m.matches(EXCLUDE_SELECTOR)) continue;
            return;
          }
          if (isLiveNode(m)) return;
          if (m.matches(".round-usage") && m === lastUsage) return;
          if (postMembers.indexOf(m) < 0 && members.indexOf(m) < 0) joiners.push(m);
        }
        postMembers.push(head);
        joiners.forEach(function (m: any) { postMembers.push(m); });
      });
      postMembers.sort(function (a: any, b: any) { return nodes.indexOf(a) - nodes.indexOf(b); });
      members = members.concat(postMembers);
    }
    // 可见尾区（无论是否有压缩条）：尾正文之后、未进折叠的节点全部保持可见。
    // 有压缩条时 postMembers 为空，所以压缩条、正文后的工具/思考/压缩过程
    // 天然落在 liveTail 里展示（以前它们会被尾后收纳卷进顶部折叠）。
    if (tail) {
      liveTail = nodes.slice(tailIdx + 1).filter(function (node: any) {
        return node.nodeType === 1 && members.indexOf(node) < 0;
      });
    }
    // 单小块不折叠（与小折叠 members.length<2 同理）：单个思考/工具/子任务等
    // 自带折叠，再套大折叠只是双层噪音；单个小折叠组（闭合时只显示一个头）
    // 同样视为一块；单段旧正文（流式进行态）仍折叠。时间行不计入（无记录
    // 对应，计入会与记录口径分叉）；无 linkage 的裸头按独立块计入。
    var units: any[] = [];
    var seenGroups: any[] = [];
    members.forEach(function (node: any) {
      if (node.matches(".msg-time")) return;
      var g = node._processGroup ||
        (node.matches(".process-group-summary") ? node._processSummary : null);
      if (g) {
        if (seenGroups.indexOf(g) < 0) { seenGroups.push(g); units.push(g); }
        return;
      }
      units.push(node);
    });
    var singleBody = units.length === 1 && units[0].nodeType === 1 &&
      units[0].matches(".msg-assistant-body");
    var fold = foldMap.get(key);
    // 直播豁免（防劫持）：游离直播容器的折叠不得被纯扁平同步接管。
    // 切回瞬间历史里若有同轮残留（裁剪失败），扁平同步会把直播折叠对象
    // 改接到扁平区（头被搬到用户消息上方、直播侧裸露）；跳过等挂回合并。
    var liveContainer = activeLiveContainer();
    if (fold && liveContainer && fold.container === liveContainer &&
        liveContainer.parentNode !== App.chatInner) {
      var touchesLive = false;
      for (var li = 0; li < nodes.length; li++) {
        if (nodes[li] && nodes[li].parentNode === liveContainer) { touchesLive = true; break; }
      }
      if (!touchesLive) return;
    }
    // 末段无正文（纯工具轮）不建折叠；非末段无有效成员不建（单小块规则统一下方）
    if ((!isFinal && (!members.length || !units.length)) ||
      (isFinal && (!tail || !members.length || !units.length)) ||
      (units.length === 1 && !singleBody)) {
      if (fold) disbandFold(fold);
      return;
    }
    if (!fold) {
      fold = buildFold(liveContainerFor(nodes), key);
      foldMap.set(key, fold);
    } else {
      // 成员跨容器变化时（窗口移动/合并）更新归属容器（直播判定/起点读取用）
      fold.container = liveContainerFor(nodes);
    }
    allFolds.set(key, fold);
    // 跨容器认领：作用域内被其它 fold 标记过的节点（用量行收养/窗口移动）先与
    // 旧 fold 脱钩并恢复可见，再按本轮重新安置；被掏空的旧 fold 就地解散。
    nodes.forEach(function (node: any) {
      var other = node._roundFold;
      if (!other || other === fold) return;
      delete node._roundFold;
      node.classList.remove("is-roundfold-hidden");
      if (other.members) {
        var at = other.members.indexOf(node);
        if (at >= 0) other.members.splice(at, 1);
      }
      if (other.members && !other.members.length && foldMap.get(other.key) === other) disbandFold(other);
    });
    fold.members.forEach(function (node: any) {
      if (node._roundFold === fold && members.indexOf(node) < 0) {
        delete node._roundFold;
        node.classList.remove("is-roundfold-hidden");
      }
    });
    fold.members = members;
    fold.tail = tail;
    fold.liveTail = liveTail;
    // 引导段不用直播轮次起点（那是整轮开始，会把本段用时拉长回轮首）；
    // 本段起点一律用段首用户气泡时间。
    fold.allowLiveFloor = allowLiveFloor;
    // 历史计时兜底：用户发送时间（非法/缺失记 0，不参与）
    var userStamp = userNode ? Date.parse(userNode.dataset.stageTimestamp || "") : NaN;
    fold.userTs = !isNaN(userStamp) ? userStamp : 0;
    members.forEach(function (node: any) { node._roundFold = fold; });
    // 尾前/尾后任有待确认都强制展开（审批上下文绝不能藏起来；与 renderHeader 边沿触发互补）
    var waiting = members.concat(liveTail).some(function (node: any) { return node.matches && node.matches(".is-waiting"); });
    if (waiting) fold.open = true;
    else if (choices.has(key)) fold.open = choices.get(key);
    else fold.open = false;
    if (fold.header.nextElementSibling !== members[0]) members[0].parentNode.insertBefore(fold.header, members[0]);
    // 折叠接管计时：移除直播计时行（若有；起播到成折叠前的过渡显示）
    var liveTimer = members[0].parentNode.querySelector(".live-round-timer");
    if (liveTimer) liveTimer.remove();
    applyVisibility(fold);
    renderHeader(fold);
  }
  function pruneStale(seen: any): void {
    Array.from(foldMap.keys()).forEach(function (key: any) {
      if (seen.indexOf(key) < 0) {
        var fold = foldMap.get(key);
        // 直播游离时（切会话/窗口重建，chatInner 已清空）保留其折叠对象：
        // 头和成员都还在游离的直播容器里，挂回后合并；此时解散会把头从
        // 游离容器里摘掉、成员全部展开，切回后顶部折叠/用时凭空消失。
        if (isSparedLiveFold(fold)) return;
        disbandFold(fold);
      }
    });
  }
  function userNodeBefore(node: any): any {
    var want = node.dataset ? node.dataset.round : null;
    var p = node.previousElementSibling;
    while (p) {
      if (p.nodeType === 1 && p.matches(".msg-user")) {
        // 只认同轮气泡：跨轮说明本轮气泡不在 DOM（窗口裁剪），不拿别轮的
        if (want == null || p.dataset.round == null || String(p.dataset.round) === String(want)) return p;
        return null;
      }
      p = p.previousElementSibling;
    }
    return null;
  }
  // 按轮收集节点：扁平段 + 同轮 .msg 子节点合并（刷新续跑/窗口恢复后同轮
  // 内容常分裂在两处，分开折叠会产生同 key 双头、计数与计时全乱）。
  // 返回 Map(key -> {flat: [], msgs: [], user})。
  function collectRounds(chatInner: any): any {
    var rounds: any = new Map();
    function group(key: any): any {
      var g = rounds.get(key);
      if (!g) { g = { flat: [], msgs: [], user: null }; rounds.set(key, g); }
      return g;
    }
    var rangeUser: any = null, rangeNodes: any[] = [];
    function flushFlat(): void {
      if (rangeUser) {
        var g = group(keyForUserNode(rangeUser));
        if (!g.user) g.user = rangeUser;
        g.flat = g.flat.concat(rangeNodes);
      }
      rangeUser = null;
      rangeNodes = [];
    }
    Array.from(chatInner.children).forEach(function (node: any) {
      if (node.nodeType !== 1) return;
      if (node.matches(".msg-user")) {
        // 同轮追发/引导消息不分段：与前段同属一轮（若分段则同 key 覆盖，
        // 前段折叠丢失）；气泡本身计入段内，由 syncRound 按气泡切分段。
        if (rangeUser && node.dataset.round != null && rangeUser.dataset.round != null &&
          String(node.dataset.round) === String(rangeUser.dataset.round)) {
          rangeNodes.push(node);
          return;
        }
        flushFlat(); rangeUser = node; return;
      }
      if (node.matches(".msg")) {
        flushFlat();
        group(keyForMsg(node)).msgs.push(node);
        return;
      }
      if (rangeUser) rangeNodes.push(node);
    });
    flushFlat();
    return rounds;
  }
  function docOrder(a: any, b: any): number {
    if (a === b) return 0;
    return (a.compareDocumentPosition(b) & 4) ? -1 : 1;
  }
  // 单轮同步：扁平节点 + 各容器子节点按文档序合并，一套折叠键。
  // user 取扁平段头气泡（计时起点准）；纯容器轮回退容器前同轮气泡。
  function syncMergedRound(key: any, g: any): any[] {
    var nodes: any[] = g.flat.slice();
    g.msgs.forEach(function (msg: any) {
      Array.from(msg.children).forEach(function (node: any) {
        if (node.nodeType === 1) nodes.push(node);
      });
    });
    nodes.sort(docOrder);
    var user = g.user;
    if (!user && g.msgs.length) user = userNodeBefore(g.msgs[0]);
    return syncRound(nodes, key, user);
  }
  function syncMsgContainer(msg: any): void {
    if (!App.chatInner) return;
    var key = keyForMsg(msg);
    var seen: any[] = [];
    if (msg.isConnected) {
      var rounds = collectRounds(App.chatInner);
      var g = rounds.get(key);
      if (g) seen = syncMergedRound(key, g);
    }
    if (!seen.length) {
      // 游离容器（未挂载）：退化为只同步自身子节点
      seen = syncRound(Array.from(msg.children).filter(function (n: any) { return n.nodeType === 1; }),
        key, userNodeBefore(msg));
    }
    // 定向剪枝：只处理本轮键（整轮键 + :s 段键），不动别轮
    Array.from(foldMap.keys()).forEach(function (k: any) {
      if ((k === key || k.indexOf(key + ":s") === 0) && seen.indexOf(k) < 0) disbandFold(foldMap.get(k));
    });
    sweepStrays(msg);
    if (App.chatInner) sweepStrays(App.chatInner);
  }
  // 孤儿头清扫：移除不属于任何现存 fold 的摘要头（窗口恢复/重建残留的旧头，
  // 无它则幽灵头越积越多且点击无响应——成员早已不在）。
  function sweepStrays(container: any): void {
    var live: any[] = [];
    foldMap.forEach(function (fold: any) { live.push(fold.header); });
    Array.from(container.children).forEach(function (node: any) {
      if (node.nodeType === 1 && node.matches(".round-fold-summary") && live.indexOf(node) < 0) {
        node.remove();
      }
    });
  }
  function sync(container: any): void {
    if (!container || !container.children) return;
    // 直播轮容器：整体一轮（合并其扁平同轮节点）
    if (container !== App.chatInner && container.matches &&
      container.matches(".msg") && !container.matches(".msg-user")) {
      syncMsgContainer(container);
      return;
    }
    // 扁平历史：按轮收集（扁平段 + 同轮容器合并），逐轮同步
    var rounds = collectRounds(container);
    var seen: any[] = [];
    rounds.forEach(function (g: any, key: any) {
      var keys = syncMergedRound(key, g);
      for (var k = 0; k < keys.length; k++) seen.push(keys[k]);
    });
    pruneStale(seen);
    sweepStrays(container);
    Array.from(container.children).forEach(function (node: any) {
      if (node.nodeType === 1 && node.matches(".msg")) sweepStrays(node);
    });
  }
  function reveal(node: any): void {
    for (var current = node; current; current = current.parentElement) {
      if (current._roundFold) openFold(current._roundFold, true, true);
      if (current === App.chatInner) break;
    }
  }
  function isOpen(key: any): any {
    if (allFolds.has(key)) return allFolds.get(key).open;
    return choices.get(key) === true;
  }
  // 记录级折叠判定（estimateHeights/windowBounds 用）：按引导切分段，返回每段
  // 的成员下标与尾下标（与 DOM 分段口径一致；无引导时退化为单段）。
  function foldableRanges(records: any, round: any): any[] {
    var wanted = String(round);
    var idxs: any[] = [];
    for (var i = 0; i < records.length; i++) {
      if (records[i] && String(records[i].round) === wanted) idxs.push(i);
    }
    if (!idxs.length) return [];
    // 段边界：同轮第 2+ 个 user 记录是引导，每段从引导（含）起至下段止
    var segStarts: any[] = [0];
    for (var k = 1; k < idxs.length; k++) {
      if (records[idxs[k]] && records[idxs[k]].kind === "user") segStarts.push(k);
    }
    var lastUsage = -1;
    for (var u = idxs.length - 1; u >= 0; u--) {
      if (records[idxs[u]].kind === "usage") { lastUsage = idxs[u]; break; }
    }
    var pg = App.processGroups;
    var ranges: any[] = [];
    segStarts.forEach(function (s: any, si: any) {
      var e = si + 1 < segStarts.length ? segStarts[si + 1] : idxs.length;
      var segIdx = idxs.slice(s, e);
      var isFinal = si === segStarts.length - 1;
      var tail = -1;
      if (isFinal) {
        for (var k2 = segIdx.length - 1; k2 >= 0; k2--) {
          if (records[segIdx[k2]].kind === "assistant") { tail = segIdx[k2]; break; }
        }
        if (tail < 0) return;
      }
      // 成员：末段=尾前可折叠 + 尾后已完成（最新用量行永不折叠）；
      // 非末段=段内全部可折叠（历史记录均为 settled）。
      var members = segIdx.filter(function (i: any) {
        var kind = records[i].kind;
        if (!MEMBER_KINDS[kind] && kind !== "usage") return false;
        if (!isFinal) return kind !== "usage" || i !== lastUsage;
        if (i < tail) return true;
        if (i === tail) return false;
        if (kind === "usage" && i === lastUsage) return false;
        return true;
      });
      if (!members.length) return;
      // 单小块不折叠（与 DOM 口径一致）：小折叠组整体算一块（经 processGroups
      // 记录分组判定），单个未分组正文仍折叠。无分组信息时退化为按条计数。
      var units: any[] = [];
      var seen: any[] = [];
      members.forEach(function (i: any) {
        var g = pg && pg.groupOf ? pg.groupOf(records[i]) : null;
        if (g) {
          if (seen.indexOf(g) < 0) { seen.push(g); units.push(g); }
          return;
        }
        units.push(i);
      });
      if (!units.length) return;
      if (units.length === 1) {
        var only = units[0];
        if (typeof only !== "number" || records[only].kind !== "assistant") return;
      }
      ranges.push({
        key: si === 0 ? roundKey(round) : roundKey(round) + ":s" + si,
        start: idxs[s], end: (si + 1 < segStarts.length ? idxs[segStarts[si + 1]] : idxs[idxs.length - 1] + 1),
        tail: tail, members: members,
      });
    });
    return ranges;
  }
  // 高度估算后处理：已渲染节点以实测为准（含隐藏=0）；未渲染且被折叠的记 0，
  // 首个成员记折叠头高度（与 processGroups 的 36 口径一致）
  function adjustHeights(records: any, heights: any, measured: any): void {
    if (!Array.isArray(records) || !Array.isArray(heights)) return;
    var rounds: any = {};
    records.forEach(function (rec: any) {
      if (rec && rec.round != null) rounds[String(rec.round)] = true;
    });
    Object.keys(rounds).forEach(function (round: any) {
      foldableRanges(records, round).forEach(function (range: any) {
        if (isOpen(range.key)) return;
        range.members.forEach(function (i: any, n: any) {
          if (n === 0) { heights[i] = FOLD_HEADER_HEIGHT; return; }
          if (measured && measured.has(records[i])) return;
          heights[i] = 0;
        });
      });
    });
  }
  // 窗口边界吸附：窗口切进已折叠段的成员区时扩到整段，避免半段渲染
  function snapBounds(records: any, start: any, end: any): any {
    if (!Array.isArray(records) || !(end > start)) return { start: start, end: end };
    function snapEdge(index: any, isStart: any): any {
      var fallback = isStart ? index : index + 1;
      var rec = records[index];
      if (!rec || rec.round == null) return fallback;
      var ranges = foldableRanges(records, rec.round);
      for (var r = 0; r < ranges.length; r++) {
        if (ranges[r].members.indexOf(index) < 0) continue;
        if (isOpen(ranges[r].key)) return fallback;
        return isStart ? ranges[r].start : ranges[r].end;
      }
      return fallback;
    }
    return { start: snapEdge(start, true), end: snapEdge(end - 1, false) };
  }
  function revision(): any { return choiceRevision; }
  App.roundFold = { sync: sync, reveal: reveal, adjustHeights: adjustHeights,
    snapBounds: snapBounds, revision: revision, tick: tick, fmtDuration: fmtDuration,
    recordPauseGap: recordPauseGap, closePauseGap: closePauseGap };
})((window as unknown as { App: any }).App);
