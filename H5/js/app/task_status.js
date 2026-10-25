/** One compact status row for the visible session. No background polling. */
(function (App) {
  "use strict";
  const rows = new Map();
  const host = document.getElementById("taskStatus");
  if (!host) return;
  const label = host.querySelector(".task-status-label");
  const detail = host.querySelector(".task-status-detail");
  const reconnect = host.querySelector("button");
  let serial = 0, reconnecting = false;
  const VIS_KEY = "ytools-task-status-visible";
  let statusVisible = true;
  try {
    const saved = localStorage.getItem(VIS_KEY);
    if (saved === "0" || saved === "false") statusVisible = false;
  } catch (_) { /* 私密模式等无法访问本地存储时默认显示 */ }
  function current(handle) { return handle && rows.get(handle.sessionId) === handle; }
  window.addEventListener("ytools-preferences-change", () => {
    try { statusVisible = !["0", "false"].includes(localStorage.getItem(VIS_KEY)); refresh(); } catch (_) { /* restricted storage */ }
  });
  function publish(handle) {
    if (App.sessionActivity) App.sessionActivity.note(handle.sessionId, handle.value, handle.id);
  }
  function refresh() {
    if (!statusVisible) {
      if (!host.hidden) host.hidden = true;
      return;
    }
    const entry = rows.get(App.state.sessionId);
    const v = TaskState.view(entry && entry.value);
    if (host.hidden !== !!v.hidden) host.hidden = !!v.hidden;
    if (v.hidden) return;
    if (host.dataset.phase !== v.phase) host.dataset.phase = v.phase;
    if (host.dataset.tone !== v.tone) host.dataset.tone = v.tone;
    if (label.textContent !== v.label) label.textContent = v.label;
    if (detail.textContent !== v.detail) detail.textContent = v.detail;
    const title = [v.detail, v.error].filter(Boolean).join("；");
    if (host.title !== title) host.title = title;
    if (reconnect.hidden !== !v.reconnect) reconnect.hidden = !v.reconnect;
    if (reconnect.disabled !== reconnecting) reconnect.disabled = reconnecting;
    const text = reconnecting ? "连接中…" : "重新连接";
    if (reconnect.textContent !== text) reconnect.textContent = text;
  }
  function update(handle, action) {
    if (!current(handle)) return;
    handle.value = TaskState.reduce(handle.value, action);
    publish(handle);
    refresh();
  }
  function begin(sessionId) {
    const entry = {sessionId, id: ++serial, value: TaskState.initial()};
    rows.set(sessionId, entry);
    publish(entry);
    if (rows.size > 64) {
      const old = [...rows.keys()].find(sid => sid !== App.state.sessionId && sid !== sessionId);
      if (old) rows.delete(old);
    }
    refresh(); return entry;
  }
  async function end(handle, completed, error, detached) {
    if (!current(handle) || detached || handle.value.connection === "background") return false;
    if (error && error.status >= 400 && error.status < 500) update(handle,{type:"frame",data:{error:error.message}});
    update(handle, { type: "end", completed: !!completed });
    if (error && !handle.value.error) handle.value.error = String(error.message || error).slice(0, 300);
    refresh();
    // A dropped browser connection cannot establish whether the task ended.
    // Reconnect only attaches (messages=[]); it never resends the user prompt.
    return !!completed || handle.value.stopped;
  }
  function isUncertain(sid) {
    const h = rows.get(sid);
    return !!h && !h.value.terminal && h.value.connection === "disconnected";
  }
  function checked(sid, data) {
    const h = rows.get(sid);
    if (h && !h.value.terminal && ["background", "disconnected"].includes(h.value.connection) && data && !data.running) update(h, { type: "ended" });
  }
  // 后端中断/重启后旧任务已消失：向后端确认一次；无任务则收尾为已结束。
  // 返回 true=已无不确定态（可继续发送），false=任务仍在运行；后端不可用时抛错。
  async function reconcileUncertain(sid) {
    if (!isUncertain(sid)) return true;
    const status = await API.streamStatus(sid);
    checked(sid, status);
    return !isUncertain(sid);
  }
  function snapshot(sessionId, data) {
    if (!data || sessionId !== App.state.sessionId) return;
    const listening = App.state.streaming && App.state.streamingSession === sessionId;
    const suspended = (data.tasks || []).find(t => t.state === "suspended");
    if (suspended) {
      const entry = rows.get(sessionId) || begin(sessionId);
      if (entry.value.connection !== "live" && entry.value.connection !== "connecting") update(entry, {type:"suspended"});
      else if (!listening) update(entry, {type:"suspended"});
    } else {
      const entry = rows.get(sessionId);
      if (entry && entry.value.phase === "suspended" && !listening) update(entry,{type:"ended"});
    }
  }
  reconnect.addEventListener("click", async function () {
    const sid = App.state.sessionId;
    if (!sid || reconnecting) return;
    reconnecting = true; refresh();
    try {
      const entry = rows.get(sid);
      const compact = entry && entry.kind === "compaction";
      const status = await (compact ? API.manualCompactionStatus(sid) : API.streamStatus(sid));
      if (App.state.sessionId !== sid) return;
      if (rows.get(sid) !== entry) return;
      if (status && status.running) {
        if (compact) await App.attachManualCompaction(sid);
        else await App.attachStreamSession(sid);
      }
      else {
        update(rows.get(sid),{type:"ended"});
        await App.openSession(sid);
      }
    } catch (error) { App.toast("重新连接失败：" + error.message); }
    finally { reconnecting = false; refresh(); }
  });
  function isStatusVisible() { return statusVisible; }
  function setStatusVisible(visible) {
    statusVisible = !!visible;
    try { localStorage.setItem(VIS_KEY, statusVisible ? "1" : "0"); } catch (_) { /* 存储不可用时仅本次生效 */ }
    refresh();
    return statusVisible;
  }
  function toggleStatusVisible() { return setStatusVisible(!statusVisible); }
  // 手动压缩是独立 SSE 流（不在聊天流内），单独建一条任务行以便状态条实时显示
  // “压缩上下文 · 跨轮历史/官方压缩窗口”，结束后收尾为已完成/失败/已停止。
  function compactBegin(sessionId) {
    const handle = begin(sessionId);
    handle.kind = "compaction";
    handle.value = TaskState.reduce(handle.value, { type: "connection", value: "live" });
    handle.value = TaskState.reduce(handle.value, { type: "frame",
      data: { event: "context_compaction", phase: "start", scope: "session" } });
    publish(handle);
    refresh();
    return handle;
  }
  function compactEvent(handle, data) {
    if (!current(handle) || !data) return;
    update(handle, { type: "frame", data });
  }
  function compactEnd(handle, outcome) {
    if (!current(handle)) return false;
    outcome = outcome || {};
    if (outcome.missing) {
      update(handle, { type: "ended" });
    } else if (outcome.disconnected) {
      update(handle, { type: "end", completed: false });
    } else if (outcome.stopped) {
      update(handle, { type: "stopped" });
    } else if (outcome.error) {
      update(handle, { type: "frame", data: { error: String(outcome.error).slice(0, 300) } });
      update(handle, { type: "end", completed: true });
      if (!handle.value.error) handle.value.error = String(outcome.error).slice(0, 300);
    } else {
      update(handle, { type: "end", completed: true });
    }
    refresh();
    return true;
  }
  App.taskStatus = { begin, refresh, end, snapshot,
    isUncertain, checked, reconcileUncertain,
    event: (h, evt) => { if (current(h) && evt && evt.type !== "done") update(h,{type:"frame",data:evt.data}); },
    connected: h => update(h,{type:"connection",value:"live"}),
    detach: h => update(h,{type:"connection",value:"background"}),
    isStopping: sid => { const h = rows.get(sid); return !!h && h.value.phase === "stopping"; },
    stopping: sid => { const h = rows.get(sid); update(h,{type:"stopping"}); return h; },
    stopped: (sid,h) => update(h || rows.get(sid),{type:"stopped"}),
    stopFailed: (sid,error,h) => update(h || rows.get(sid),{type:"stop_failed",error:error.message}),
    compactBegin, compactEvent, compactEnd,
    isStatusVisible, setStatusVisible, toggleStatusVisible,
  };
})(window.App);
