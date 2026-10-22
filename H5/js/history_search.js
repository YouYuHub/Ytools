/** Async history search state; independent of DOM so races can be tested. */
(function (global) {
  "use strict";
  function createController(search, onState, options) {
    options = options || {};
    const delay = options.delay == null ? 280 : options.delay;
    const timeout = options.timeout == null ? 60000 : options.timeout;
    let seq = 0, timer = null, active = null, opened = false;
    let scope = { includeInternal: false, includeFilenames: false, archiveScope: "active" };
    let state = { query: "", status: "idle", results: [], total: 0, session_count: 0, next_offset: null };
    function emit(patch) { state = Object.assign({}, state, patch); onState(state); }
    function cancel() {
      seq += 1;
      if (timer != null) clearTimeout(timer);
      timer = null;
      if (active) active.abort();
      active = null;
    }
    async function run(more) {
      if (!opened || !state.query || (more && state.next_offset == null)) return;
      cancel();
      const mine = seq, query = state.query;
      const controller = new AbortController();
      active = controller;
      let timedOut = false;
      const timeoutId = setTimeout(function () { timedOut = true; controller.abort(); }, timeout);
      emit({ status: more ? "more" : "loading", error: "" });
      try {
        const data = await search(query, { signal: controller.signal, offset: more ? state.next_offset : 0,
          revision: more ? state.revision : null, includeInternal: scope.includeInternal, includeFilenames: scope.includeFilenames, archiveScope: scope.archiveScope });
        if (mine !== seq || !opened || state.query !== query) return;
        emit(Object.assign({}, data, { results: more ? state.results.concat(data.results || []) : (data.results || []), status: "ready", error: "" }));
      } catch (error) {
        if (mine !== seq || !opened) return;
        if (more && error.status === 409) {
          emit({ results: [], next_offset: null });
          return run(false);
        }
        if (error.name === "AbortError" && !timedOut) return;
        emit({ status: "error", error: timedOut ? "搜索超时，请重试" : (error.message || "搜索失败，请重试") });
      } finally {
        clearTimeout(timeoutId);
        if (mine === seq) active = null;
      }
    }
    function update(value, composing) {
      cancel(); // Invalidate old results immediately, before the debounce elapses.
      const query = String(value || "").trim();
      emit({ query: query, status: query ? "pending" : "idle", results: [], total: 0,
        session_count: 0, next_offset: null, revision: null, error: "" });
      if (!opened || composing || !query) return;
      timer = setTimeout(function () { timer = null; run(false); }, delay);
    }
    return {
      open: function (value) { opened = true; update(value || ""); },
      close: function () { opened = false; cancel(); },
      update: update,
      setScope: function (next, composing) {
        const selected = { includeInternal: Boolean(next.includeInternal), includeFilenames: Boolean(next.includeFilenames), archiveScope: next.archiveScope || scope.archiveScope };
        if (selected.includeInternal === scope.includeInternal && selected.includeFilenames === scope.includeFilenames && selected.archiveScope === scope.archiveScope) return;
        scope = selected;
        update(state.query, composing);
      },
      flush: function () { return run(false); },
      more: function () { if (state.status === "ready") return run(true); },
      getState: function () { return state; },
    };
  }
  const api = { createController: createController };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else global.HistorySearch = api;
})(typeof self !== "undefined" ? self : globalThis);
