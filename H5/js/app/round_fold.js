"use strict";
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
        if (!node._roundFoldIdentity)
            node._roundFoldIdentity = "live-" + (++sequence);
        return sessionId() + ":" + node._roundFoldIdentity;
    }
    function keyForUserNode(user) {
        return user.dataset.round != null ? roundKey(user.dataset.round) : identityKey(user);
    }
    function keyForMsg(msg) {
        return msg.dataset.round != null ? roundKey(msg.dataset.round) : identityKey(msg);
    }
    function setText(node, value) {
        if (node.textContent !== value)
            node.textContent = value;
    }
    function isFoldable(node) {
        return node.nodeType === 1 && node.matches(MEMBER_SELECTOR) && !node.matches(EXCLUDE_SELECTOR);
    }
    function isTailBody(node) {
        return node.nodeType === 1 && node.matches(".msg-assistant-body") && !node.matches(EXCLUDE_SELECTOR);
    }
    // 进行中/待确认节点：流式光标、执行中过程、待审批——尾后保持可见，绝不收进
    // 折叠（与 renderHeader/foldActiveNow 的运行判定同口径，is-waiting 亦视为 live）。
    function isLiveNode(node) {
        return node.matches(".is-streaming, .is-executing, .is-running, .msg-cursor, .is-waiting") ||
            node.querySelector(".tool-status-icon");
    }
    function remember(fold) {
        if (choices.get(fold.key) !== fold.open)
            choiceRevision++;
        choices.set(fold.key, fold.open);
        if (choices.size > 500)
            choices.delete(choices.keys().next().value);
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
        if (user)
            remember(fold);
        applyVisibility(fold);
        if (App.historyScroll)
            App.historyScroll.refresh({ preserve: false });
        if (App.updateCodeblockCopyButtons)
            App.updateCodeblockCopyButtons();
    }
    function fmtDuration(ms) {
        if (!(ms > 0))
            return "";
        var total = Math.round(ms / 1000);
        if (total < 1)
            return "";
        if (total < 60)
            return total + "s";
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
            if (node.matches(".tool-block"))
                tools++;
            else if (node.matches(".agent-block"))
                agents++;
            else if (node.matches(".msg-assistant-body"))
                bodies++;
            else if (node.matches(".think-block"))
                thinks++;
            if (node.matches(".is-error, .is-failed, .compaction-interrupted") ||
                node.querySelector(".tool-block-tool-icon.is-failed"))
                failures++;
            if (node.matches(".is-waiting"))
                waiting = node;
            if (node.matches(".is-streaming, .is-executing, .is-running") ||
                node.querySelector(".tool-status-icon"))
                active = true;
            var stamp = Date.parse(node.dataset.stageTimestamp || "");
            if (!isNaN(stamp))
                times.push(stamp);
        });
        // 尾正文时间计入用时（整轮跨度）；流式光标视为进行中
        if (fold.tail) {
            var tailStamp = Date.parse(fold.tail.dataset.stageTimestamp || "");
            if (!isNaN(tailStamp))
                times.push(tailStamp);
            if (fold.tail.matches(".msg-cursor"))
                active = true;
        }
        // 尾后可见区（进行中/待确认/整组可见的小折叠）计入状态与用时，不计入折叠统计
        (fold.liveTail || []).forEach(function (node) {
            if (node.matches(".is-error, .is-failed, .compaction-interrupted") ||
                node.querySelector(".tool-block-tool-icon.is-failed"))
                failures++;
            if (node.matches(".is-waiting"))
                waiting = node;
            if (node.matches(".is-streaming, .is-executing, .is-running, .msg-cursor") ||
                node.querySelector(".tool-status-icon"))
                active = true;
            var liveStamp = Date.parse(node.dataset.stageTimestamp || "");
            if (!isNaN(liveStamp))
                times.push(liveStamp);
        });
        var floor = floorFor(fold);
        if (floor)
            times.push(floor);
        var counts = [];
        if (tools)
            counts.push(tools + " 次工具调用");
        if (agents)
            counts.push(agents + " 个子任务");
        if (thinks)
            counts.push(thinks + " 段思考");
        if (bodies)
            counts.push(bodies + " 段正文");
        if (failures)
            counts.push(failures + " 项异常");
        // 直播流等待模型时同样亮运行态（转圈 + 计时），与 foldActiveNow 同口径；
        // 待确认 veto：审批等待期间不亮灯、不计时。
        if (!waiting && !active && isLiveFold(fold))
            active = true;
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
    function recordPauseGap(container) {
        if (!container)
            return;
        if (!container._foldPauseGaps)
            container._foldPauseGaps = [];
        var gaps = container._foldPauseGaps;
        if (gaps.length > 50)
            gaps.splice(0, gaps.length - 50);
        gaps.push({ pauseAt: Date.now(), resumeAt: 0 });
    }
    function closePauseGap(container) {
        var gaps = container && container._foldPauseGaps;
        if (!gaps)
            return;
        for (var i = gaps.length - 1; i >= 0; i--) {
            if (!gaps[i].resumeAt) {
                gaps[i].resumeAt = Date.now();
                return;
            }
        }
    }
    function foldGapMs(fold, start, end) {
        var gaps = (fold.container && fold.container._foldPauseGaps) || [];
        var total = 0;
        for (var i = 0; i < gaps.length; i++) {
            var p = gaps[i].pauseAt, r = gaps[i].resumeAt;
            if (!(r > p))
                continue;
            var overlap = Math.min(r, end) - Math.max(p, start);
            if (overlap > 0)
                total += overlap;
        }
        return total;
    }
    // 轮次起始时间（成员+尾最早的时间戳）；无时间戳返回 0
    function foldStartTime(fold) {
        var min = Infinity;
        function feed(node) {
            var stamp = Date.parse(node.dataset.stageTimestamp || "");
            if (!isNaN(stamp) && stamp < min)
                min = stamp;
        }
        fold.members.forEach(feed);
        if (fold.tail)
            feed(fold.tail);
        (fold.liveTail || []).forEach(feed);
        var floor = floorFor(fold);
        if (floor && floor < min)
            min = floor;
        return min === Infinity ? 0 : min;
    }
    // 当前是否仍在运行（读实时 DOM，不依赖上次 render 的 class 快照）
    function foldActiveNow(fold) {
        var active = false;
        var nodes = fold.tail ? fold.members.concat([fold.tail], fold.liveTail || []) : fold.members;
        for (var i = 0; i < nodes.length; i++) {
            var node = nodes[i];
            // 待确认一票否决：审批等待期间冻结计时（通宵挂起也不跳）
            if (node.matches(".is-waiting"))
                return false;
            if (node.matches(".is-streaming, .is-executing, .is-running, .msg-cursor") ||
                node.querySelector(".tool-status-icon"))
                active = true;
        }
        // 等待模型：DOM 上无进行态（工具刚结束、下一段输出未到），但本容器仍是
        // 当前会话的直播流 → 任务活着，视为运行中（否则每次等模型都停表）。
        // 暂停/结束时 streaming 清掉、activeStream 置空，自然停表；继续后复用容器
        // 重新接上 activeStream，自然恢复（暂停间隙从用时中扣除，见 foldGapMs）。
        if (!active && isLiveFold(fold))
            active = true;
        return active;
    }
    // 本折叠所在容器是否当前会话的直播流容器
    function isLiveFold(fold) {
        var st = App.state;
        if (!st || !st.streaming || !st.activeStream)
            return false;
        if (st.streamingSession !== sessionId())
            return false;
        return fold.container === st.activeStream.messageNode;
    }
    // 直播轮计时下限：附接回放重建的节点时间戳都是“现在”，用 marker 下发的
    // 轮次开始墙钟兜底（存容器上，随续跑复用延续）。非直播折叠、缺失、非法、
    // 未来值（时钟偏斜）一律返回 0，即退回纯节点时间戳口径。
    function liveStartFloor(fold) {
        if (!isLiveFold(fold))
            return 0;
        var at = fold.container && fold.container._foldRoundStartedAt;
        if (typeof at !== "number" || !(at > 0) || at > Date.now())
            return 0;
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
    function floorFor(fold) {
        var live = liveStartFloor(fold);
        if (live > 0) {
            fold.startFloor = live;
            return live;
        }
        if (fold.startFloor > 0)
            return fold.startFloor;
        var userTs = fold.userTs || 0;
        if (!(userTs > 0))
            return 0;
        var min = Infinity;
        fold.members.forEach(function (node) {
            var stamp = Date.parse(node.dataset.stageTimestamp || "");
            if (!isNaN(stamp) && stamp < min)
                min = stamp;
        });
        if (fold.tail) {
            var tailStamp = Date.parse(fold.tail.dataset.stageTimestamp || "");
            if (!isNaN(tailStamp) && tailStamp < min)
                min = tailStamp;
        }
        (fold.liveTail || []).forEach(function (node) {
            var liveStamp = Date.parse(node.dataset.stageTimestamp || "");
            if (!isNaN(liveStamp) && liveStamp < min)
                min = liveStamp;
        });
        if (!(min < Infinity) || userTs > min || min - userTs > HISTORY_FLOOR_MAX_GAP)
            return 0;
        return userTs;
    }
    function maybeStopTimer() {
        if (!tickingFolds.size && tickTimer != null && typeof clearInterval === "function") {
            clearInterval(tickTimer);
            tickTimer = null;
        }
    }
    function tickFold(fold) {
        if (!fold.header.isConnected) {
            tickingFolds.delete(fold);
            return;
        }
        // 停表沿：最后渲染一次，把转圈摘掉、用时冻结为静态跨度（收尾 sync 发生在
        // streaming 清掉之前，头状态会滞后一个节拍，不补这次就会转圈常亮）。
        if (!foldActiveNow(fold)) {
            tickingFolds.delete(fold);
            renderHeader(fold);
            return;
        }
        var start = foldStartTime(fold);
        if (!start)
            return;
        var now = Date.now();
        var text = fmtDuration(now - start - foldGapMs(fold, start, now));
        setText(fold.duration, text ? "用时 " + text : "");
    }
    // 每秒刷新一次运行中折叠的用时（导出供单测确定性驱动）
    function tick() {
        tickingFolds.forEach(tickFold);
        maybeStopTimer();
    }
    function ensureTicking(fold) {
        if (fold.header.classList.contains("is-running"))
            tickingFolds.add(fold);
        else
            tickingFolds.delete(fold);
        if (tickingFolds.size && tickTimer == null && typeof setInterval === "function") {
            tickTimer = setInterval(tick, 1000);
        }
    }
    function buildFold(container, key) {
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
            startFloor: 0, userTs: 0 };
        header._roundFold = fold;
        button.addEventListener("click", function () {
            var top = header.getBoundingClientRect().top;
            if (App.pauseAutoScroll)
                App.pauseAutoScroll();
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
        fold.members.forEach(function (node) {
            if (node._roundFold !== fold)
                return;
            delete node._roundFold;
            node.classList.remove("is-roundfold-hidden");
        });
        // choices 保留（重建后仍按用户选择恢复）；allFolds 注销
        if (allFolds.get(fold.key) === fold)
            allFolds.delete(fold.key);
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
    // 尾前节点全部收纳；尾后已完成的节点一并收纳（暂停/中断冻结的残留），只有
    // 进行中/待确认内容与最新用量行保持可见。无正文（纯工具轮）或无有效成员时不建折叠。
    // userNode：本轮用户气泡（历史计时兜底用，可空）。
    function syncRound(nodes, container, key, userNode) {
        var map = foldsOf(container);
        var tailIdx = -1;
        for (var i = nodes.length - 1; i >= 0; i--) {
            if (isTailBody(nodes[i])) {
                tailIdx = i;
                break;
            }
        }
        var tail = tailIdx >= 0 ? nodes[tailIdx] : null;
        // 尾上时间戳：紧贴尾正文上方的时间行是尾的注脚（可见正文何时开始），永不折叠
        var tailTime = null;
        if (tailIdx >= 1) {
            var prev = nodes[tailIdx - 1];
            if (prev.nodeType === 1 && prev.matches(".msg-time"))
                tailTime = prev;
        }
        // 成员只取尾之前的节点；用量行同理（尾之前的旧用量折叠，尾之后最新一条保留）
        var members = tail ? nodes.slice(0, tailIdx).filter(function (node) { return isFoldable(node) && node !== tailTime; }) : [];
        // 尾后收纳：暂停/中断会把已完成的过程节点冻结在尾正文之后；续跑新开 .msg
        // 容器后它们永远到不了尾前，不收纳就会永久裸露（暂停几次就留下几块）。
        var liveTail = [];
        if (tail) {
            var lastUsage = null;
            for (var u = nodes.length - 1; u >= 0; u--) {
                if (nodes[u].nodeType === 1 && nodes[u].matches(".round-usage")) {
                    lastUsage = nodes[u];
                    break;
                }
            }
            var postMembers = [];
            var postStart = tailIdx + 1;
            var postHeads = [];
            for (var p = postStart; p < nodes.length; p++) {
                var node = nodes[p];
                if (!isFoldable(node))
                    continue;
                // 小折叠组头第二遍整体判定；已编组节点由所属组头认领（头失联则保持可见）
                if (node.matches(".process-group-summary")) {
                    postHeads.push(node);
                    continue;
                }
                if (node._processGroup && node._processGroup.header &&
                    node._processGroup.header.isConnected)
                    continue;
                if (isLiveNode(node))
                    continue;
                if (node.matches(".round-usage") && node === lastUsage)
                    continue;
                postMembers.push(node);
            }
            // 尾后小折叠组整体收纳：成员全已完成才连头一起收；任一进行中/待确认或
            // 已搬离本容器则整组可见（避免头藏起来而成员裸露、或头留着而成员被掏空）。
            // 无小折叠 linkage 的裸头（如手搭节点）保守保持可见。
            postHeads.forEach(function (head) {
                var group = head._processSummary;
                if (!group || !group.members || !group.members.length)
                    return;
                var joiners = [];
                for (var g = 0; g < group.members.length; g++) {
                    var m = group.members[g];
                    if (!m || m.parentNode !== container)
                        return;
                    if (m === head)
                        continue;
                    if (!isFoldable(m)) {
                        if (m.matches && m.matches(EXCLUDE_SELECTOR))
                            continue;
                        return;
                    }
                    if (isLiveNode(m))
                        return;
                    if (m.matches(".round-usage") && m === lastUsage)
                        return;
                    if (postMembers.indexOf(m) < 0 && members.indexOf(m) < 0)
                        joiners.push(m);
                }
                postMembers.push(head);
                joiners.forEach(function (m) { postMembers.push(m); });
            });
            postMembers.sort(function (a, b) { return nodes.indexOf(a) - nodes.indexOf(b); });
            members = members.concat(postMembers);
            liveTail = nodes.slice(postStart).filter(function (node) {
                return node.nodeType === 1 && members.indexOf(node) < 0;
            });
        }
        // 单小块不折叠（与小折叠 members.length<2 同理）：单个思考/工具/子任务等
        // 自带折叠，再套大折叠只是双层噪音；单个小折叠组（闭合时只显示一个头）
        // 同样视为一块；单段旧正文（流式进行态）仍折叠。时间行不计入（无记录
        // 对应，计入会与记录口径分叉）；无 linkage 的裸头按独立块计入。
        var units = [];
        var seenGroups = [];
        members.forEach(function (node) {
            if (node.matches(".msg-time"))
                return;
            var g = node._processGroup ||
                (node.matches(".process-group-summary") ? node._processSummary : null);
            if (g) {
                if (seenGroups.indexOf(g) < 0) {
                    seenGroups.push(g);
                    units.push(g);
                }
                return;
            }
            units.push(node);
        });
        var singleBody = units.length === 1 && units[0].nodeType === 1 &&
            units[0].matches(".msg-assistant-body");
        var fold = map.get(key);
        if (!tail || !members.length || !units.length || (units.length === 1 && !singleBody)) {
            if (fold)
                disbandFold(map, fold);
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
            if (!other || other === fold)
                return;
            delete node._roundFold;
            node.classList.remove("is-roundfold-hidden");
            if (other.members) {
                var at = other.members.indexOf(node);
                if (at >= 0)
                    other.members.splice(at, 1);
            }
            if (other.members && !other.members.length && other.container) {
                var otherMap = foldsByContainer.get(other.container);
                if (otherMap && otherMap.get(other.key) === other)
                    disbandFold(otherMap, other);
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
        fold.liveTail = liveTail;
        // 历史计时兜底：用户发送时间（非法/缺失记 0，不参与）
        var userStamp = userNode ? Date.parse(userNode.dataset.stageTimestamp || "") : NaN;
        fold.userTs = !isNaN(userStamp) ? userStamp : 0;
        members.forEach(function (node) { node._roundFold = fold; });
        // 尾前/尾后任有待确认都强制展开（审批上下文绝不能藏起来；与 renderHeader 边沿触发互补）
        var waiting = members.concat(liveTail).some(function (node) { return node.matches && node.matches(".is-waiting"); });
        if (waiting)
            fold.open = true;
        else if (choices.has(key))
            fold.open = choices.get(key);
        else
            fold.open = false;
        if (fold.header.nextElementSibling !== members[0])
            container.insertBefore(fold.header, members[0]);
        applyVisibility(fold);
        renderHeader(fold);
    }
    function pruneStale(container, seen) {
        var map = foldsByContainer.get(container);
        if (!map)
            return;
        Array.from(map.keys()).forEach(function (key) {
            if (seen.indexOf(key) < 0)
                disbandFold(map, map.get(key));
        });
    }
    function userNodeBefore(node) {
        var p = node.previousElementSibling;
        while (p) {
            if (p.nodeType === 1 && p.matches(".msg-user"))
                return p;
            p = p.previousElementSibling;
        }
        return null;
    }
    function syncMsgContainer(msg) {
        syncRound(Array.from(msg.children), msg, keyForMsg(msg), userNodeBefore(msg));
        pruneStale(msg, [keyForMsg(msg)]);
        sweepStrays(msg);
    }
    // 孤儿头清扫：移除不属于任何现存 fold 的摘要头（窗口恢复/重建残留的旧头，
    // 无它则幽灵头越积越多且点击无响应——成员早已不在）。
    function sweepStrays(container) {
        var map = foldsByContainer.get(container);
        var live = [];
        if (map)
            map.forEach(function (fold) { live.push(fold.header); });
        Array.from(container.children).forEach(function (node) {
            if (node.nodeType === 1 && node.matches(".round-fold-summary") && live.indexOf(node) < 0) {
                node.remove();
            }
        });
    }
    function sync(container) {
        if (!container || !container.children)
            return;
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
                syncRound(rangeNodes, container, key, rangeUser);
                seen.push(key);
            }
            rangeUser = null;
            rangeNodes = [];
        }
        Array.from(container.children).forEach(function (node) {
            if (node.nodeType !== 1)
                return;
            if (node.matches(".msg-user")) {
                // 同轮追发消息不分段：流式中途用户追发的消息与当前轮同轮号，
                // 与前段同属一轮（若分段则同 key 覆盖，前段折叠丢失——r143 实证）。
                // 用户节点本身永不折叠，保持可见，故不计入 rangeNodes。
                if (rangeUser && node.dataset.round != null && rangeUser.dataset.round != null &&
                    String(node.dataset.round) === String(rangeUser.dataset.round))
                    return;
                flush();
                rangeUser = node;
                return;
            }
            if (node.matches(".msg")) {
                flush();
                syncMsgContainer(node);
                return;
            }
            if (rangeUser)
                rangeNodes.push(node);
        });
        flush();
        pruneStale(container, seen);
        sweepStrays(container);
    }
    function reveal(node) {
        for (var current = node; current; current = current.parentElement) {
            if (current._roundFold)
                openFold(current._roundFold, true, true);
            if (current === App.chatInner)
                break;
        }
    }
    function isOpen(key) {
        if (allFolds.has(key))
            return allFolds.get(key).open;
        return choices.get(key) === true;
    }
    // 记录级折叠判定（estimateHeights/windowBounds 用）：返回轮内成员下标与尾下标
    function foldableRange(records, round) {
        var wanted = String(round);
        var idxs = [];
        for (var i = 0; i < records.length; i++) {
            if (records[i] && String(records[i].round) === wanted)
                idxs.push(i);
        }
        if (!idxs.length)
            return null;
        var tail = -1;
        for (var k = idxs.length - 1; k >= 0; k--) {
            if (records[idxs[k]].kind === "assistant") {
                tail = idxs[k];
                break;
            }
        }
        if (tail < 0)
            return null;
        // 成员：尾前可折叠记录 + 尾后已完成记录（历史记录均为 settled，与 DOM
        // 尾后收纳对齐；最新用量行永不折叠）。单小块不折叠（与 DOM 口径一致，
        // 记录无时间行/组头，成员即有效成员）。
        var lastUsage = -1;
        for (var u = idxs.length - 1; u >= 0; u--) {
            if (records[idxs[u]].kind === "usage") {
                lastUsage = idxs[u];
                break;
            }
        }
        var members = idxs.filter(function (i) {
            var kind = records[i].kind;
            if (!MEMBER_KINDS[kind] && kind !== "usage")
                return false;
            if (i < tail)
                return true;
            if (i === tail)
                return false;
            if (kind === "usage" && i === lastUsage)
                return false;
            return true;
        });
        if (!members.length)
            return null;
        // 单小块不折叠（与 DOM 口径一致）：小折叠组整体算一块（经 processGroups
        // 记录分组判定），单个未分组正文仍折叠。无分组信息时退化为按条计数。
        var pg = App.processGroups;
        var units = [];
        var seen = [];
        members.forEach(function (i) {
            var g = pg && pg.groupOf ? pg.groupOf(records[i]) : null;
            if (g) {
                if (seen.indexOf(g) < 0) {
                    seen.push(g);
                    units.push(g);
                }
                return;
            }
            units.push(i);
        });
        if (!units.length)
            return null;
        if (units.length === 1) {
            var only = units[0];
            if (typeof only !== "number" || records[only].kind !== "assistant")
                return null;
        }
        return { start: idxs[0], end: idxs[idxs.length - 1] + 1, tail: tail, members: members };
    }
    // 高度估算后处理：已渲染节点以实测为准（含隐藏=0）；未渲染且被折叠的记 0，
    // 首个成员记折叠头高度（与 processGroups 的 36 口径一致）
    function adjustHeights(records, heights, measured) {
        if (!Array.isArray(records) || !Array.isArray(heights))
            return;
        var rounds = {};
        records.forEach(function (rec) {
            if (rec && rec.round != null)
                rounds[String(rec.round)] = true;
        });
        Object.keys(rounds).forEach(function (round) {
            if (isOpen(roundKey(round)))
                return;
            var range = foldableRange(records, round);
            if (!range)
                return;
            range.members.forEach(function (i, n) {
                if (n === 0) {
                    heights[i] = FOLD_HEADER_HEIGHT;
                    return;
                }
                if (measured && measured.has(records[i]))
                    return;
                heights[i] = 0;
            });
        });
    }
    // 窗口边界吸附：窗口切进已折叠轮的成员区时扩到整轮，避免半轮渲染
    function snapBounds(records, start, end) {
        if (!Array.isArray(records) || !(end > start))
            return { start: start, end: end };
        function snapEdge(index, isStart) {
            var fallback = isStart ? index : index + 1;
            var rec = records[index];
            if (!rec || rec.round == null || isOpen(roundKey(rec.round)))
                return fallback;
            var range = foldableRange(records, rec.round);
            if (!range || range.members.indexOf(index) < 0)
                return fallback;
            return isStart ? range.start : range.end;
        }
        return { start: snapEdge(start, true), end: snapEdge(end - 1, false) };
    }
    function revision() { return choiceRevision; }
    App.roundFold = { sync: sync, reveal: reveal, adjustHeights: adjustHeights,
        snapBounds: snapBounds, revision: revision, tick: tick,
        recordPauseGap: recordPauseGap, closePauseGap: closePauseGap };
})(window.App);
