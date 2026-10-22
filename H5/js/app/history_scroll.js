/** 完整会话共享滚动坐标；可视窗口按需渲染，原生滑块拖动中同步切换。 */
(function (App) {
  "use strict";
  const {chatScroll, chatInner} = App;
  const Geometry = window.HistoryScrollModel;
  let model = null, frozen = null, rendering = false, refreshing = false, frame = null;
  let observedRoots = [];
  let tailNodes = new Set(), editCache = new Map(), editHolder = null;
  const measured = new WeakMap();
  const resize = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(scheduleRefresh);

  function snapshot() { return App.getHistoryWindow(); }
  function active() { return Boolean(model && snapshot().records === model.records && model.records.length); }
  function number(style, name, fallback) { return parseFloat(style.getPropertyValue(name)) || fallback; }
  function dimensions() {
    const style = getComputedStyle(chatInner);
    return {top:number(style,"--chat-top-padding",64), bottom:number(style,"--chat-bottom-padding",56), gap:parseFloat(style.rowGap)||24};
  }
  function roots() { return Array.from(chatInner.children).filter(node => !node.classList.contains("qnav-load-older") && !node.classList.contains("history-live-hidden")); }
  function rememberTail() {
    Array.from(chatInner.children).forEach(node => {
      if (!node.hasAttribute("data-history-index") && !node.classList.contains("qnav-load-older")) tailNodes.add(node);
    });
  }
  function restoreTail(view) {
    let questionsChanged = false;
    const current = App.state.activeStream;
    if (current && current.sessionId === App.state.sessionId && !current.detached) {
      [current.userNode,current.messageNode].forEach(node => { if (node && !node.hasAttribute("data-history-index")) tailNodes.add(node); });
      // restoreActiveStream 可能复用历史中本轮的 userNode，不能同时补回旧气泡。
      if (current.userNode && current.userNode.dataset.round) tailNodes.forEach(node => {
        if (node !== current.userNode && node.classList.contains("msg-user") && node.dataset.round === current.userNode.dataset.round) {
          tailNodes.delete(node); node.remove(); questionsChanged = true;
        }
      });
    }
    tailNodes.forEach(node => {
      node.classList.toggle("history-live-hidden",view.end !== view.records.length);
      if (node.parentNode !== chatInner) {
        chatInner.appendChild(node);
        if (node.classList.contains("msg-user") || node.querySelector(".msg-user")) questionsChanged = true;
      }
    });
    // 历史窗口先重建导航、随后才补回未入历史快照的实时轮次。必须在补回后
    // 再同步一次，否则任务虽已显示，导航仍停在旧快照的最后一个问题。
    if (questionsChanged && App.rebuildQnav) App.rebuildQnav();
  }
  function stashEdits(info) {
    info.nodes.filter(node => node.hasAttribute("data-history-index") && node.classList.contains("is-editing")).forEach(node => {
      if (!editHolder) {
        editHolder = document.createElement("div"); editHolder.hidden = true;
        chatScroll.parentElement.appendChild(editHolder);
      }
      const record = model.records[Number(node.dataset.historyIndex)];
      if (record) { editCache.set(record,node); editHolder.appendChild(node); }
    });
  }
  function restoreEdits() {
    roots().forEach(node => {
      if (!node.hasAttribute("data-history-index")) return;
      const record = model.records[Number(node.getAttribute("data-history-index"))], saved = editCache.get(record);
      if (saved && node.classList.contains("msg-user")) { node.replaceWith(saved); editCache.delete(record); }
    });
  }
  function beforeRender() {
    if (!active()) return;
    rememberTail(); stashEdits(inspect());
  }
  function write(name, value) {
    const text = Math.abs(value) < 0.01 ? "0px" : value + "px";
    if (chatInner.style.getPropertyValue(name) !== text) chatInner.style.setProperty(name,text);
  }
  function scroll(top) {
    const previous = chatScroll.style.scrollBehavior;
    chatScroll.style.scrollBehavior = "auto";
    if (Math.abs(chatScroll.scrollTop-top) > 0.5) chatScroll.scrollTop = top;
    chatScroll.style.scrollBehavior = previous;
  }
  function watch(nodes) {
    if (!resize || nodes.length === observedRoots.length && nodes.every((node,i) => node === observedRoots[i])) return;
    resize.disconnect(); nodes.forEach(node => resize.observe(node)); observedRoots = nodes;
  }
  function inspect() {
    const nodes = roots(), groups = new Map(), boxes = new Map();
    nodes.forEach(node => {
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
    const keys = Array.from(groups.keys()).sort((a,b) => a-b);
    const lastGroup = keys.length ? groups.get(keys[keys.length-1]) : null;
    const indexedBottom = lastGroup ? boxes.get(lastGroup[lastGroup.length-1]).bottom : firstTop;
    watch(nodes);
    return {nodes, groups, boxes, keys, firstTop, height, leading, trailing,
      liveOffset:indexedBottom-firstTop, liveHeight:Math.max(0, first ? boxes.get(last).bottom-indexedBottom : 0)};
  }
  function anchor(info) {
    const box = chatScroll.getBoundingClientRect();
    const node = info.nodes.find(node => {
      const rect = info.boxes.get(node); return rect.bottom > box.top && rect.top < box.bottom;
    });
    return node ? {node, y:info.boxes.get(node).top} : null;
  }
  function updateHeights(info, view, gap) {
    for (let i=view.start; i<view.end; i++) model.heights[i] = 0;
    info.keys.forEach((index,k) => {
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
  function normalSpaces(view, gap) {
    write("--history-prefix-space",model.offsets[view.start]);
    write("--history-suffix-space",model.offsets[model.records.length]-model.offsets[view.end]+gap+(view.end<model.records.length ? model.live : 0));
    write("--history-window-shift",0);
  }
  function reset() {
    model = null; frozen = null;
    tailNodes = new Set();
    editCache.forEach(node => { if (App.exitUserMessageEdit) App.exitUserMessageEdit(node); });
    editCache.clear(); if (editHolder) editHolder.remove(); editHolder = null;
    chatScroll.classList.remove("history-windowed");
    ["--history-prefix-space","--history-suffix-space","--history-window-shift"].forEach(name => chatInner.style.removeProperty(name));
    if (resize) resize.disconnect(); observedRoots = [];
    if (frame != null) cancelAnimationFrame(frame);
    frame = null;
  }
  function refresh(options) {
    if (rendering || refreshing) return;
    const view = snapshot();
    if (!view.records || !view.records.length) { reset(); return; }
    refreshing = true;
    try {
      const fresh = !model || model.records !== view.records || model.heights.length !== view.records.length;
      const d = dimensions();
      if (fresh) {
        tailNodes = new Set();
        const retained = new Set(view.records);
        editCache.forEach((node,record) => {
          if (retained.has(record)) return;
          if (App.exitUserMessageEdit) App.exitUserMessageEdit(node);
          node.remove(); editCache.delete(record);
        });
        model = {records:view.records, heights:Array.from(view.records, rec => measured.has(rec) ? measured.get(rec) : Geometry.estimate(rec,chatInner.clientWidth-48)), live:0};
        model.offsets = Geometry.prefix(model.heights);
        if (frozen) { frozen.offsets = model.offsets.slice(); frozen.live = 0; }
      }
      chatScroll.classList.add("history-windowed");
      rememberTail(); restoreTail(view); restoreEdits();
      const info = inspect();
      if (frozen) { updateHeights(info,view,d.gap); placeFrozen(info,view); return; }
      const saved = options && (options.initial || options.preserve === false) ? null : anchor(info);
      const atBottom = view.end === model.records.length && chatScroll.scrollHeight-chatScroll.scrollTop-chatScroll.clientHeight <= 2;
      updateHeights(info,view,d.gap);
      normalSpaces(view,d.gap);
      if (options && options.initial) scroll(d.top+model.offsets[view.start]);
      else if (atBottom) scroll(chatScroll.scrollHeight);
      else if (saved && saved.node.isConnected) scroll(chatScroll.scrollTop+saved.node.getBoundingClientRect().top-saved.y);
    } finally { refreshing = false; }
  }
  function scheduleRefresh() {
    if (frame != null || rendering) return;
    frame = requestAnimationFrame(function () { frame = null; if (!frozen) refresh(); onScroll(); });
  }
  function desiredPosition() {
    const d = dimensions(), top = chatScroll.scrollTop, max = chatScroll.scrollHeight-chatScroll.clientHeight;
    const head = top <= 1, tail = top >= max-1;
    const offsets = frozen ? frozen.offsets : model.offsets;
    const total = offsets[offsets.length-1], live = frozen ? frozen.live : model.live;
    const logical = frozen ? (top+chatScroll.clientHeight/2-d.top) * (total+live)/Math.max(1,frozen.total-d.top-d.bottom) : top+chatScroll.clientHeight/2-d.top;
    const index = head ? 0 : tail ? model.records.length-1 : Math.min(model.records.length-1,Geometry.locate(offsets,Math.max(0,logical)));
    const isLive = !head && logical >= total && live > 0;
    const size = isLive ? live : offsets[index+1]-offsets[index];
    const fraction = Math.max(0,Math.min(1,(logical-(isLive ? total : offsets[index]))/Math.max(1,size)));
    return {index, fraction, isLive, head, tail, top};
  }
  function placeFrozen(info, view, wanted) {
    if (!frozen || !info.nodes.length) return;
    const d = dimensions(); wanted = wanted || desiredPosition();
    const group = info.groups.get(wanted.index) || info.groups.get(info.keys.find(index => index>=wanted.index)) || info.groups.get(info.keys[0]);
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
  function onScroll() {
    if (!active() || rendering || refreshing) return;
    const wanted = desiredPosition(), view = snapshot(), info = inspect();
    const rect = chatScroll.getBoundingClientRect(), d = dimensions();
    const covered = info.nodes.length && info.firstTop <= rect.top+(view.start===0 ? d.top : 1) &&
      info.boxes.get(info.nodes[info.nodes.length-1]).bottom >= rect.bottom-(view.end===model.records.length ? d.bottom : 1);
    if (frozen && wanted.index >= view.start && wanted.index < view.end && (!wanted.isLive || view.end === model.records.length)) {
      placeFrozen(info,view,wanted); return;
    }
    const boundaryReady = wanted.head ? view.start === 0 : wanted.tail ? view.end === model.records.length : covered;
    if (boundaryReady) {
      if (frozen && (wanted.head || wanted.tail)) placeFrozen(info,view,wanted);
      return;
    }
    rendering = true;
    try {
      let options;
      if (frozen) {
        const offsets = frozen.offsets;
        const first = wanted.head ? 0 : wanted.tail
          ? Math.min(Math.max(0,wanted.index-47),Geometry.locate(offsets,Math.max(0,offsets[offsets.length-1]-chatScroll.clientHeight*3)))
          : Math.min(Math.max(0,wanted.index-8),Geometry.locate(offsets,Math.max(0,offsets[wanted.index]-chatScroll.clientHeight)));
        const end = Math.max(first+48,Geometry.locate(offsets,offsets[wanted.index]+chatScroll.clientHeight*2)+1);
        options = {size:end-first,before:wanted.index-first};
      }
      App.renderHistoryWindowAt(wanted.index,options);
      const after = snapshot(); restoreTail(after); restoreEdits();
      const layout = inspect(), d = dimensions();
      if (frozen) { updateHeights(layout,after,d.gap); placeFrozen(layout,after,wanted); }
      else {
        updateHeights(layout,after,d.gap); normalSpaces(after,d.gap);
        if (wanted.head) scroll(0);
        else if (wanted.tail) scroll(chatScroll.scrollHeight);
        else {
          const unit = wanted.isLive ? model.offsets[model.records.length]+wanted.fraction*model.live
            : model.offsets[wanted.index]+wanted.fraction*model.heights[wanted.index];
          scroll(d.top+unit-chatScroll.clientHeight/2);
        }
      }
    } finally { rendering = false; }
  }
  function beginDrag() {
    if (!active()) return;
    frozen = {offsets:model.offsets.slice(),live:model.live,total:chatInner.getBoundingClientRect().height,
      before:parseFloat(chatInner.style.getPropertyValue("--history-prefix-space"))||0};
    // 当前可视段已经是实际布局；全局首尾占位也属于原生拖动范围。
    onScroll();
  }
  function captureRelease() {
    if (frozen) onScroll();
    return frozen ? {wanted:desiredPosition(), info:inspect()} : null;
  }
  function endDrag(cancelled, release) {
    if (!frozen) return;
    const wanted = release ? release.wanted : desiredPosition(), info = release ? release.info : inspect();
    const saved = anchor(info), view = snapshot(), d = dimensions();
    frozen = null;
    updateHeights(info,view,d.gap); normalSpaces(view,d.gap);
    if (wanted.head) scroll(0);
    else if (wanted.tail && !cancelled) scroll(chatScroll.scrollHeight);
    else if (saved && saved.node.isConnected) scroll(chatScroll.scrollTop+saved.node.getBoundingClientRect().top-saved.y);
  }
  App.historyScroll = {active, refresh, reset, beforeRender, beginDrag, captureRelease, endDrag, onScroll};
  chatScroll.addEventListener("scroll",scheduleRefresh,{passive:true});
  if (typeof MutationObserver !== "undefined") new MutationObserver(scheduleRefresh).observe(chatInner,{childList:true,subtree:true,characterData:true});
  window.addEventListener("resize",scheduleRefresh);
})(window.App);
