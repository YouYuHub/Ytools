/** Sidebar activity: immediate local events plus one batched backend snapshot. */
(function (App) {
  "use strict";
  const entries = new Map();
  let version = 0, timer = null, pending = null, controller = null;
  let started = false, unsupported = false, failures = 0;
  function listening(sid) {
    const s = App.state;
    return s.streaming && s.streamingSession === sid || s.manualCompactRunning && s.manualCompactSession === sid;
  }
  function decorate(item) {
    const icon = item.querySelector(".session-activity");
    if (!icon) return false;
    const value = entries.get(item.dataset.session);
    const hidden = !value || value.kind === "idle", changed = icon.hidden !== hidden;
    if (changed) icon.hidden = hidden;
    if (!hidden) {
      if (icon.dataset.kind !== value.kind) icon.dataset.kind = value.kind;
      if (icon.title !== value.label) icon.title = value.label;
      if (icon.getAttribute("aria-label") !== value.label) icon.setAttribute("aria-label", value.label);
    }
    return changed;
  }
  function paint(sid) {
    let resized = false;
    document.querySelectorAll(".session-item").forEach(function (item) {
      if (!sid || item.dataset.session === sid) resized = decorate(item) || resized;
    });
    if (resized && App.refreshSessionFades) App.refreshSessionFades();
  }
  function note(sid, value, id) {
    if (!sid || !value) return;
    // A detached listener still represents a running backend task. Keep the
    // underlying wait phase; a genuinely disconnected stream stays uncertain.
    const v = TaskState.view(value.connection === "background" ? Object.assign({}, value, {connection:"live"}) : value);
    const missingBackgroundStatus = unsupported && value.connection === "background";
    const kind = value.terminal ? "idle" : v.phase === "disconnected" || missingBackgroundStatus ? "uncertain"
      : ["waiting_approval", "waiting_budget", "waiting_user", "stopping"].includes(v.phase) ? "waiting" : "running";
    const label = missingBackgroundStatus ? "更新后端后可确认后台任务状态" : v.label;
    const key = id + ":" + kind + ":" + label;
    const previous = entries.get(sid);
    if (previous && previous.key === key) return;
    entries.set(sid, {kind, label, key, local:true, version:++version});
    paint(sid);
    if (!pending && (!previous || (previous.kind === "idle") !== (kind === "idle"))) schedule();
  }
  function schedule() {
    if (timer != null) clearTimeout(timer);
    timer = null;
    if (!started || unsupported || document.hidden) return;
    const active = [...entries.values()].some(e => e.kind !== "idle");
    const delay = failures ? Math.min(30000, 4000 * Math.pow(2, failures)) : active ? 4000 : 12000;
    timer = setTimeout(sync, delay);
  }
  function sync() {
    if (pending) return pending;
    if (unsupported || document.hidden || typeof API.streamStatuses !== "function") return Promise.resolve();
    if (timer != null) clearTimeout(timer);
    timer = null;
    const before = version;
    controller = new AbortController();
    const signal = controller.signal;
    let timedOut = false;
    const timeout = setTimeout(() => { timedOut = true; if (controller) controller.abort(); }, 8000);
    pending = Promise.resolve().then(async function () {
      try {
        const data = await API.streamStatuses(signal);
        if (!data || !Array.isArray(data.sessions) || data.sessions.some(row => !row || typeof row.session_id !== "string"
          || typeof row.chat_running !== "boolean" || typeof row.compacting !== "boolean")) throw new Error("任务状态格式无效");
        const remote = new Map(data.sessions.filter(row => row.chat_running || row.compacting).map(row => [row.session_id,row]));
        const ids = new Set([...entries.keys(), ...remote.keys()]);
        ids.forEach(function (sid) {
          const old = entries.get(sid);
          // Do not let an older response erase a new send, revive a completed
          // task, or replace a live local phase with a coarse server label.
          if (old && (old.version > before || old.local && listening(sid))) return;
          const row = remote.get(sid);
          if (!row) entries.delete(sid);
          else entries.set(sid,
            {kind:"running",label:row.compacting ? "正在压缩上下文" : "任务进行中",version:before});
        });
        failures = 0;
        paint();
      } catch (error) {
        if (signal.aborted && !timedOut) return;
        if (error.status === 404) unsupported = true; // Old backend: local events still work.
        if (!signal.aborted || !document.hidden) {
          failures++;
          entries.forEach(function (old,sid) {
            if (old.version > before || listening(sid) || old.kind === "idle") return;
            entries.set(sid, {kind:"uncertain",label:"后台状态暂不可用，连接恢复后更新",version:before});
          });
          paint();
        }
      } finally {
        clearTimeout(timeout); controller = null; pending = null;
        if (signal.aborted && !timedOut && started && !document.hidden) Promise.resolve().then(sync);
        else schedule();
      }
    });
    return pending;
  }
  function start() { if (!started) { started = true; sync(); } }
  function stop() {
    started = false;
    if (timer != null) clearTimeout(timer);
    timer = null;
    if (controller) controller.abort();
  }
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) {
      if (timer != null) clearTimeout(timer);
      timer = null;
      if (controller) controller.abort();
    } else if (started) sync();
  });
  window.addEventListener("focus", function () { if (started) sync(); });
  window.addEventListener("pagehide", stop);
  window.addEventListener("pageshow", function (event) { if (event.persisted && !started) start(); });
  App.sessionActivity = {note, decorate, sync, start, stop};
})(window.App);
