/** 完整会话共享滚动坐标；可视窗口按需渲染，原生滑块拖动中同步切换。 */
(function (App: any) {
  "use strict";
  const {chatScroll, chatInner} = App;
  const Geometry: any = (window as unknown as { HistoryScrollModel: any }).HistoryScrollModel;
  let model: any = null, frozen: any = null, rendering = false, refreshing = false, frame: any = null;
  // 用户主动上滚意图：底部 refresh/onScroll 的强制吸底会把 1~2px 的上滚
  // 拉回到底，造成“滚不动/来回抖”。置位后跳过吸底，直到用户自己滚回
  // 底部（wheel 下滚且接近底部）或程序化置底（scroll 到底）才清除。
  let userScrolledUp = false;
  let observedRoots: any[] = [];
  let tailNodes: any = new Set(), editCache: any = new Map(), editHolder: any = null;
  const measured: any = new WeakMap();
  const resize: any = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(scheduleRefresh);

  function snapshot(): any { return App.getHistoryWindow(); }
  function syncNavigation(): void { if (App.updateQnavActive) App.updateQnavActive(); }
  function active(): boolean { return Boolean(model && snapshot().records === model.records && model.records.length); }
  function number(style: any, name: any, fallback: any): any { return parseFloat(style.getPropertyValue(name)) || fallback; }
  function dimensions(): any {
    const style: any = getComputedStyle(chatInner);
    return {top:number(style,"--chat-top-padding",64), bottom:number(style,"--chat-bottom-padding",56), gap:parseFloat(style.rowGap)||24};
  }
  function roots(): any { return Array.from(chatInner.children).filter((node: any) => !node.classList.contains("qnav-load-older") && !node.classList.contains("history-live-hidden") && !node.classList.contains("is-process-hidden") && !node.classList.contains("is-roundfold-hidden")); }
  function rememberTail(): void {
    // 过程标题/轮折叠头是派生节点，由分组模块重建；缓存它会在窗口切换后恢复失效标题。
    const nodes = Array.from(chatInner.children).filter((node: any) => !node.hasAttribute("data-history-index") && !node.matches(".qnav-load-older, .process-group-summary, .round-fold-summary"));
    if (nodes.every((node: any) => tailNodes.has(node))) return;
    // 后加入的提问/过程摘要可能插在已记录节点之前，按相邻节点保存顺序。
    // 已暂存到窗口外的旧轮次仍保留原来的位置。
    const order = Array.from(tailNodes);
    let before = order.length;
    for (let i = nodes.length - 1; i >= 0; i--) {
      const index = order.indexOf(nodes[i]);
      if (index < 0) order.splice(before,0,nodes[i]);
      else before = index;
    }
    tailNodes = new Set(order);
  }
  function restoreTail(view: any): any {
    let questionsChanged = false, changed = false;
    const stream = App.state.activeStream;
    const current = stream && stream.sessionId === App.state.sessionId && !stream.detached ? stream : null;
    if (current) {
      [current.userNode,current.messageNode].forEach((node: any) => { if (node && !node.hasAttribute("data-history-index")) tailNodes.add(node); });
      // restoreActiveStream 可能复用历史中本轮的 userNode，不能同时补回旧气泡。
      if (current.userNode && current.userNode.dataset.round) tailNodes.forEach((node: any) => {
        if (node !== current.userNode && node.classList.contains("msg-user") && node.dataset.round === current.userNode.dataset.round) {
          tailNodes.delete(node); node.remove(); questionsChanged = true;
        }
      });
    }
    const nodes = Array.from(tailNodes);
    if (current) {
      const userIndex = nodes.indexOf(current.userNode), messageIndex = nodes.indexOf(current.messageNode);
      if (messageIndex >= 0 && userIndex > messageIndex) {
        nodes.splice(userIndex,1); nodes.splice(messageIndex,0,current.userNode);
        tailNodes = new Set(nodes);
      }
    }
    // 中部直播轮（插入/原地重发，_liveMidway）：固定在轮次位置，不随尾部
    // 移动、不隐藏——否则 restoreTail 会把它 append 到末尾，跑到视口外“消失”。
    function midwayAnchor(node: any): any {
      const r = parseInt(node.dataset && node.dataset.round, 10);
      if (isNaN(r)) return null;
      return chatInner.querySelector('[data-round="' + (r + 1) + '"]');
    }
    nodes.forEach((node: any) => {
      if (node._liveMidway) {
        if (node.classList.contains("history-live-hidden")) {
          node.classList.remove("history-live-hidden"); changed = true;
        }
        if (node.parentNode !== chatInner) {
          const anchor = midwayAnchor(node);
          if (anchor && anchor.parentNode === chatInner) chatInner.insertBefore(node, anchor);
          else chatInner.appendChild(node);
          changed = true;
          if (node.classList.contains("msg-user") || node.querySelector(".msg-user")) questionsChanged = true;
        }
        return;
      }
      const hidden = view.end !== view.records.length;
      if (node.classList.contains("history-live-hidden") !== hidden) {
        node.classList.toggle("history-live-hidden",hidden); changed = true;
      }
    });
    // 当前流可能已被 restoreActiveStream 提前挂回；旧轮次若只追加缺失节点，
    // 就会跑到最新回复之后。把保存的实时尾部按时间顺序恢复为完整的后缀。
    // （中部直播轮已在上面原位恢复，不参与尾部比对/移动。）
    const tailOnly = nodes.filter((node: any) => !node._liveMidway);
    const mounted = Array.from(chatInner.children).filter((node: any) => !node.classList.contains("process-group-summary") && !node.classList.contains("round-fold-summary")).slice(-tailOnly.length);
    if (tailOnly.length && tailOnly.some((node: any,index: any) => mounted[index] !== node)) {
      tailOnly.forEach((node: any) => chatInner.appendChild(node));
      changed = true;
      questionsChanged = questionsChanged || tailOnly.some((node: any) => node.classList.contains("msg-user") || node.querySelector(".msg-user"));
    }
    if (changed && App.processGroups) App.processGroups.sync(chatInner);
    // 历史窗口先重建导航、随后才补回未入历史快照的实时轮次。必须在补回后
    // 再同步一次，否则任务虽已显示，导航仍停在旧快照的最后一个问题。
    if (questionsChanged && App.rebuildQnav) App.rebuildQnav();
    return changed;
  }
  function revealTail(): void {
    const view = snapshot();
    if (!view.records || view.end !== view.records.length) return;
    // 已在尾部窗口也要恢复实时节点，不能仅凭 end 判断可见性。
    // 只在补挂/解除隐藏时重新测量，避免每个流式帧重建导航或遍历布局。
    rememberTail();
    if (restoreTail(view)) refresh({preserve:false});
  }
  // 永久移除实时占位/副本；只 remove() 会被 tailNodes 在下次窗口恢复时补回。
  function discardNodes(nodes: any): void {
    const removed = new Set(nodes);
    if (!removed.size) return;
    new Set([...tailNodes, ...chatInner.children]).forEach((node: any) => {
      const group = node._processSummary;
      if (group && group.members.some((member: any) => removed.has(member))) removed.add(node);
    });
    removed.forEach((node: any) => { tailNodes.delete(node); node.remove(); });
    if (App.processGroups) App.processGroups.sync(chatInner);
  }
  function stashEdits(info: any): void {
    info.nodes.filter((node: any) => node.hasAttribute("data-history-index") && node.classList.contains("is-editing")).forEach((node: any) => {
      if (!editHolder) {
        editHolder = document.createElement("div"); editHolder.hidden = true;
        chatScroll.parentElement.appendChild(editHolder);
      }
      const record = model.records[Number(node.dataset.historyIndex)];
      if (record) { editCache.set(record,node); editHolder.appendChild(node); }
    });
  }
  function restoreEdits(): void {
    roots().forEach((node: any) => {
      if (!node.hasAttribute("data-history-index")) return;
      const record = model.records[Number(node.getAttribute("data-history-index"))], saved = editCache.get(record);
      if (saved && node.classList.contains("msg-user")) { node.replaceWith(saved); editCache.delete(record); }
    });
  }
  function beforeRender(): void {
    if (!active()) return;
    rememberTail(); stashEdits(inspect());
  }
  function write(name: any, value: any): void {
    const text = Math.abs(value) < 0.01 ? "0px" : value + "px";
    if (chatInner.style.getPropertyValue(name) !== text) chatInner.style.setProperty(name,text);
  }
  function scroll(top: any): void {
    const previous = chatScroll.style.scrollBehavior;
    chatScroll.style.scrollBehavior = "auto";
    if (Math.abs(chatScroll.scrollTop-top) > 0.5) chatScroll.scrollTop = top;
    chatScroll.style.scrollBehavior = previous;
    // 任何落到底部的程序化滚动都代表跟随意图恢复（如回底按钮、拖拽到底）。
    if (chatScroll.scrollHeight-chatScroll.scrollTop-chatScroll.clientHeight <= 2) userScrolledUp = false;
  }
  function watch(nodes: any): void {
    if (!resize || nodes.length === observedRoots.length && nodes.every((node: any,i: any) => node === observedRoots[i])) return;
    resize.disconnect(); nodes.forEach((node: any) => resize.observe(node)); observedRoots = nodes;
  }
  function inspect(): any {
    const nodes: any = roots(), groups: any = new Map(), boxes: any = new Map();
    nodes.forEach((node: any) => {
      const rect = node.getBoundingClientRect(); boxes.set(node,rect);
      const raw = node.getAttribute("data-history-index");
      if (raw == null) return;
      const index = Number(raw);
      if (!groups.has(index)) groups.set(index,[]);
      groups.get(index).push(node);
    });
    const first = nodes[0], last = nodes[nodes.length-1];
    const firstTop = first ? boxes.get(first).top : 0;
    const height = first ? boxes.get(last).bottom-firstTop : 0;
    const leading = first ? parseFloat(getComputedStyle(first).marginTop)||0 : 0;
    const trailing = last ? parseFloat(getComputedStyle(last).marginBottom)||0 : 0;
    const keys = Array.from(groups.keys()).sort((a: any,b: any) => a-b);
    const lastGroup = keys.length ? groups.get(keys[keys.length-1]) : null;
    const indexedBottom = lastGroup ? boxes.get(lastGroup[lastGroup.length-1]).bottom : firstTop;
    watch(nodes);
    return {nodes, groups, boxes, keys, firstTop, height, leading, trailing,
      liveOffset:indexedBottom-firstTop, liveHeight:Math.max(0, first ? boxes.get(last).bottom-indexedBottom : 0)};
  }
  function anchor(info: any): any {
    const box = chatScroll.getBoundingClientRect();
    const node = info.nodes.find((node: any) => {
      const rect = info.boxes.get(node); return rect.bottom > box.top && rect.top < box.bottom;
    });
    return node ? {node, y:info.boxes.get(node).top} : null;
  }
  function updateHeights(info: any, view: any, gap: any): void {
    for (let i=view.start; i<view.end; i++) {
      model.heights[i] = 0;
      if (model.records[i] && typeof model.records[i] === "object") measured.set(model.records[i], 0);
    }
    info.keys.forEach((index: any,k: any) => {
      const group = info.groups.get(index), next = info.groups.get(info.keys[k+1]);
      const start = info.boxes.get(group[0]).top;
      const last = group[group.length-1];
      const end = next ? info.boxes.get(next[0]).top : info.boxes.get(last).bottom+(parseFloat(getComputedStyle(last).marginBottom)||0)+gap;
      const height = Math.max(0,end-start);
      model.heights[index] = height;
      if (model.records[index] && typeof model.records[index] === "object") measured.set(model.records[index],height);
    });
    if (view.end === model.records.length) model.live = info.liveHeight;
    model.offsets = Geometry.prefix(model.heights);
  }
  function normalSpaces(view: any, gap: any, info: any): void {
    const first = info && info.keys.length ? Math.min(view.start, info.keys[0]) : view.start;
    write("--history-prefix-space",model.offsets[first]);
    write("--history-suffix-space",model.offsets[model.records.length]-model.offsets[view.end]+gap+(view.end<model.records.length ? model.live : 0));
    write("--history-window-shift",0);
  }
  function reset(): void {
    model = null; frozen = null;
    tailNodes = new Set();
    editCache.forEach((node: any,record: any) => { if (App.exitUserMessageEdit) App.exitUserMessageEdit(node); });
    editCache.clear(); if (editHolder) editHolder.remove(); editHolder = null;
    chatScroll.classList.remove("history-windowed");
    ["--history-prefix-space","--history-suffix-space","--history-window-shift"].forEach((name: any) => chatInner.style.removeProperty(name));
    if (resize) resize.disconnect(); observedRoots = [];
    if (frame != null) cancelAnimationFrame(frame);
    frame = null;
  }
  function refresh(options?: any): void {
    if (rendering || refreshing) return;
    const view = snapshot();
    if (!view.records || !view.records.length) { reset(); return; }
    refreshing = true;
    try {
      const fresh = !model || model.records !== view.records || model.heights.length !== view.records.length;
      const revision = (App.processGroups && App.processGroups.revision ? App.processGroups.revision() : 0) +
        ":" + (App.roundFold && App.roundFold.revision ? App.roundFold.revision() : 0);
      const d = dimensions();
      if (fresh) {
        tailNodes = new Set();
        const retained = new Set(view.records);
        editCache.forEach((node: any,record: any) => {
          if (retained.has(record)) return;
          if (App.exitUserMessageEdit) App.exitUserMessageEdit(node);
          node.remove(); editCache.delete(record);
        });
      }
      if (fresh || model.groupRevision !== revision) {
        const fallback = (rec: any) => Geometry.estimate(rec,chatInner.clientWidth-48);
        const baseHeights = App.processGroups
          ? App.processGroups.estimateHeights(view.records, fallback, measured)
          : Array.from(view.records, (rec: any) => measured.has(rec) ? measured.get(rec) : fallback(rec));
        // 轮级大折叠后处理：已折叠轮的未渲染成员记 0（已渲染以实测为准）
        if (App.roundFold) App.roundFold.adjustHeights(view.records, baseHeights, measured);
        model = {records:view.records, heights:baseHeights, live:fresh ? 0 : model.live, groupRevision:revision};
        model.offsets = Geometry.prefix(model.heights);
        if (fresh && frozen) { frozen.offsets = model.offsets.slice(); frozen.live = 0; }
      }
      chatScroll.classList.add("history-windowed");
      rememberTail(); restoreTail(view); restoreEdits();
      const info = inspect();
      if (frozen) { updateHeights(info,view,d.gap); placeFrozen(info,view); return; }
      const saved = options && (options.initial || options.preserve === false) ? null : anchor(info);
      const atBottom = view.end === model.records.length && chatScroll.scrollHeight-chatScroll.scrollTop-chatScroll.clientHeight <= 2;
      updateHeights(info,view,d.gap);
      normalSpaces(view,d.gap,info);
      if (options && options.initial) scroll(d.top+model.offsets[view.start]);
      // 用户刚主动上滚时不强制吸底，否则 1~2px 的上滚会被拉回到底，
      // 滚轮继续推又到底、高亮也在首尾跳，体感就是抖动。锚点分支会
      // 按原视口位置跟随，不丢用户正在看的内容。
      else if (atBottom && !userScrolledUp) scroll(chatScroll.scrollHeight);
      else if (saved && saved.node.isConnected) scroll(chatScroll.scrollTop+saved.node.getBoundingClientRect().top-saved.y);
    } finally { refreshing = false; if (!rendering) syncNavigation(); }
  }
  function scheduleRefresh(): void {
    if (frame != null || rendering) return;
    frame = requestAnimationFrame(function () { frame = null; if (!frozen) refresh(); onScroll(); });
  }
  function desiredPosition(): any {
    const d = dimensions(), top = chatScroll.scrollTop, max = chatScroll.scrollHeight-chatScroll.clientHeight;
    // CSS/设备缩放下原生 scrollTop 可为小数，scrollHeight 则按整数返回。
    const head = top <= 1, tail = top >= max-2;
    const offsets = frozen ? frozen.offsets : model.offsets;
    const total = offsets[offsets.length-1], live = frozen ? frozen.live : model.live;
    const logical = frozen ? (top+chatScroll.clientHeight/2-d.top) * (total+live)/Math.max(1,frozen.total-d.top-d.bottom) : top+chatScroll.clientHeight/2-d.top;
    const index = head ? 0 : tail ? model.records.length-1 : Math.min(model.records.length-1,Geometry.locate(offsets,Math.max(0,logical)));
    const isLive = !head && logical >= total && live > 0;
    const size = isLive ? live : offsets[index+1]-offsets[index];
    const fraction = Math.max(0,Math.min(1,(logical-(isLive ? total : offsets[index]))/Math.max(1,size)));
    return {index, fraction, isLive, head, tail, top};
  }
  function placeFrozen(info: any, view: any, wanted?: any): void {
    if (!frozen || !info.nodes.length) return;
    const d = dimensions(); wanted = wanted || desiredPosition();
    const group = info.groups.get(wanted.index) || info.groups.get(info.keys.find((index: any) => index>=wanted.index)) || info.groups.get(info.keys[0]);
    const nextKey = group ? info.keys[info.keys.indexOf(Number(group[0].getAttribute("data-history-index")))+1] : null;
    const next = info.groups.get(nextKey);
    const local = wanted.isLive ? info.liveOffset+wanted.fraction*info.liveHeight
      : group ? info.boxes.get(group[0]).top-info.firstTop+wanted.fraction*((next ? info.boxes.get(next[0]).top : info.boxes.get(group[group.length-1]).bottom+d.gap)-info.boxes.get(group[0]).top)
      : wanted.fraction*info.height;
    const height = info.height+info.leading+info.trailing+d.gap;
    const room = frozen.total-d.top-d.bottom-height;
    const prefix = wanted.head ? 0 : wanted.tail ? room : wanted.top+chatScroll.clientHeight/2-d.top-info.leading-local;
    const fitted = Geometry.fit(frozen.total,d.top,d.bottom,height,prefix);
    // 按住时不改变滚动容器的 padding/布局范围。只移动绘制窗口，避免
    // 原生滑块在布局重建后恢复旧的拖动基点，也减少连续拖动的重排。
    write("--history-window-shift",fitted.before+fitted.shift-frozen.before);
  }
  function onScroll(): void {
    if (!active() || rendering || refreshing) return;
    const wanted = desiredPosition(), view = snapshot(), info = inspect();
    const rect = chatScroll.getBoundingClientRect(), d = dimensions();
    // 首尾的 padding、外边距及 normalSpaces 末尾 gap 也是有效可视范围。
    // 漏掉末尾 gap 会把离底十几像素误判成窗口缺失，销毁当前长回答/
    // Mermaid 后按估高重定位；图表回填后下滚又重建，形成最后一段循环。
    const contentTop = info.firstTop-info.leading-(view.start === 0 ? d.top : 0);
    const last = info.nodes[info.nodes.length-1];
    const contentBottom = last ? info.boxes.get(last).bottom+info.trailing+
      (view.end === model.records.length ? d.bottom+d.gap : 0) : 0;
    // 预取边距：视口接近窗口边缘即提前换窗（此时视口仍被已挂载内容完全
    // 覆盖，重建+锚点补偿用户无感知）。等到边缘真正露出空白占位才换窗，
    // 滚动中会闪现空白并伴随滚动条跳动；工具密集的长会话窗口小、重建频繁，
    // 体感就是“一下跳过一些渲染块”。首尾无更多内容的一侧不预取，避免原地空转。
    const prefetch = Math.min(400, chatScroll.clientHeight*0.5);
    const needTop = view.start > 0 ? prefetch : 0;
    const needBottom = view.end < model.records.length ? prefetch : 0;
    const covered = info.nodes.length && contentTop <= rect.top+2-needTop && contentBottom >= rect.bottom-2+needBottom;
    if (frozen && wanted.index >= view.start && wanted.index < view.end && (!wanted.isLive || view.end === model.records.length)) {
      placeFrozen(info,view,wanted); syncNavigation(); return;
    }
    const boundaryReady = wanted.head ? view.start === 0 : wanted.tail ? view.end === model.records.length : covered;
    if (boundaryReady) {
      if (frozen && (wanted.head || wanted.tail)) placeFrozen(info,view,wanted);
      syncNavigation();
      return;
    }
    rendering = true;
    try {
      const currentAnchor = !frozen && anchor(info);
      const anchoredIndex = currentAnchor && Number(currentAnchor.node.getAttribute('data-history-index'));
      let options: any;
      if (frozen) {
        const offsets = frozen.offsets;
        const first = wanted.head ? 0 : wanted.tail
          ? Math.min(Math.max(0,wanted.index-47),Geometry.locate(offsets,Math.max(0,offsets[offsets.length-1]-chatScroll.clientHeight*3)))
          : Math.min(Math.max(0,wanted.index-8),Geometry.locate(offsets,Math.max(0,offsets[wanted.index]-chatScroll.clientHeight)));
        const end = Math.max(first+48,Geometry.locate(offsets,offsets[wanted.index]+chatScroll.clientHeight*2)+1);
        options = {size:end-first,before:wanted.index-first};
      } else {
        // A folded run can contain hundreds of records but only one visible row.
        // Plan by pixels, so a short raw-record slice cannot strand a blank viewport.
        const offsets = model.offsets;
        const unit = wanted.isLive ? offsets[offsets.length-1] + wanted.fraction*model.live
          : offsets[wanted.index] + wanted.fraction*model.heights[wanted.index];
        const first = Geometry.locate(offsets, Math.max(0, unit-chatScroll.clientHeight*1.5));
        const end = Math.max(wanted.index+1, Geometry.locate(offsets, unit+chatScroll.clientHeight*2.5)+1);
        options = {size:end-first, before:wanted.index-first};
      }
      App.renderHistoryWindowAt(wanted.index,options);
      const after = snapshot(); restoreTail(after); restoreEdits();
      const layout = inspect(), d = dimensions();
      if (frozen) { updateHeights(layout,after,d.gap); placeFrozen(layout,after,wanted); }
      else {
        updateHeights(layout,after,d.gap); normalSpaces(after,d.gap,layout);
        if (wanted.head) scroll(0);
        else if (wanted.tail) scroll(chatScroll.scrollHeight);
        else if (currentAnchor && layout.groups.has(anchoredIndex)) {
          const replacement = layout.groups.get(anchoredIndex)[0];
          // Spacers were just updated; pre-update rectangles are stale here.
          scroll(chatScroll.scrollTop+replacement.getBoundingClientRect().top-currentAnchor.y);
        }
        else {
          const unit = wanted.isLive ? model.offsets[model.records.length]+wanted.fraction*model.live
            : model.offsets[wanted.index]+wanted.fraction*model.heights[wanted.index];
          scroll(d.top+unit-chatScroll.clientHeight/2);
        }
      }
    } finally { rendering = false; syncNavigation(); }
  }
  function beginDrag(): void {
    if (!active()) return;
    frozen = {offsets:model.offsets.slice(),live:model.live,total:chatInner.getBoundingClientRect().height,
      before:parseFloat(chatInner.style.getPropertyValue("--history-prefix-space"))||0};
    // 当前可视段已经是实际布局；全局首尾占位也属于原生拖动范围。
    onScroll();
  }
  function captureRelease(): any {
    if (frozen) onScroll();
    return frozen ? {wanted:desiredPosition(), info:inspect()} : null;
  }
  function endDrag(cancelled: any, release: any): void {
    if (!frozen) return;
    const wanted = release ? release.wanted : desiredPosition(), info = release ? release.info : inspect();
    const saved = anchor(info), view = snapshot(), d = dimensions();
    frozen = null;
    updateHeights(info,view,d.gap); normalSpaces(view,d.gap,info);
    if (wanted.head) scroll(0);
    else if (wanted.tail && !cancelled) scroll(chatScroll.scrollHeight);
    else if (saved && saved.node.isConnected) scroll(chatScroll.scrollTop+saved.node.getBoundingClientRect().top-saved.y);
    syncNavigation();
  }
  function nearBottomNow(): boolean {
    return chatScroll.scrollHeight-chatScroll.scrollTop-chatScroll.clientHeight <= 8;
  }
  // 用户上滚意图只取 wheel/触摸：键盘滚动步长大（远超 2px 吸底容差），
  // 本来就不会被吸底劫持，无需处理。
  chatScroll.addEventListener("wheel", function (event: any) {
    if (event.deltaY < 0) userScrolledUp = true;
    else if (event.deltaY > 0 && nearBottomNow()) userScrolledUp = false;
  }, { passive: true });
  let lastTouchY: any = null;
  chatScroll.addEventListener("touchstart", function (event: any) {
    lastTouchY = event.touches.length ? event.touches[0].clientY : null;
  }, { passive: true });
  chatScroll.addEventListener("touchmove", function (event: any) {
    const y = event.touches.length ? event.touches[0].clientY : null;
    if (y != null && lastTouchY != null) {
      if (y < lastTouchY) userScrolledUp = true;
      else if (y > lastTouchY && nearBottomNow()) userScrolledUp = false;
    }
    lastTouchY = y;
  }, { passive: true });
  App.historyScroll = {active, refresh, reset, beforeRender, revealTail, discardNodes, beginDrag, captureRelease, endDrag, onScroll,
    clearUserScrolledUp: function () { userScrolledUp = false; }};
  chatScroll.addEventListener("scroll",scheduleRefresh,{passive:true});
  if (typeof MutationObserver !== "undefined") new MutationObserver(scheduleRefresh).observe(chatInner,{childList:true,subtree:true,characterData:true});
  window.addEventListener("resize",scheduleRefresh);
})((window as unknown as { App: any }).App);
