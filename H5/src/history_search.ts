/** 搜索请求参数（offset/revision 为后端游标，上游无类型）。 */
interface SearchRequestOptions {
  signal: AbortSignal;
  offset: any;
  revision: any;
  includeInternal: boolean;
  includeFilenames: boolean;
  archiveScope: string;
}

/** 后端搜索函数（返回的 data 形态由后端决定，用 any 保留原语义）。 */
type SearchFn = (query: string, options: SearchRequestOptions) => Promise<any>;

interface ControllerOptions {
  delay?: any;
  timeout?: any;
}

interface SearchScope {
  includeInternal: boolean;
  includeFilenames: boolean;
  archiveScope: string;
}

interface SearchState {
  query: string;
  status: string;
  results: any[];
  total: number;
  session_count: number;
  next_offset: any;
  revision?: any;
  error?: string;
}

interface ScopeSelection {
  includeInternal?: unknown;
  includeFilenames?: unknown;
  archiveScope?: string;
}

interface HistorySearchController {
  open: (value: unknown) => void;
  close: () => void;
  update: (value: unknown, composing?: unknown) => void;
  setScope: (next: ScopeSelection, composing?: unknown) => void;
  flush: () => Promise<void>;
  more: () => Promise<void> | undefined;
  getState: () => SearchState;
}

interface HistorySearchApi {
  createController: (search: SearchFn, onState: (state: SearchState) => void, options?: ControllerOptions) => HistorySearchController;
}

/** Async history search state; independent of DOM so races can be tested. */
(function (global: typeof globalThis) {
  "use strict";
  function createController(search: SearchFn, onState: (state: SearchState) => void, options?: ControllerOptions): HistorySearchController {
    options = options || {};
    const delay = options.delay == null ? 280 : options.delay;
    const timeout = options.timeout == null ? 60000 : options.timeout;
    let seq = 0, timer: ReturnType<typeof setTimeout> | null = null, active: AbortController | null = null, opened = false;
    let scope: SearchScope = { includeInternal: false, includeFilenames: false, archiveScope: "active" };
    let state: SearchState = { query: "", status: "idle", results: [], total: 0, session_count: 0, next_offset: null };
    function emit(patch: Partial<SearchState>): void { state = Object.assign({}, state, patch); onState(state); }
    function cancel(): void {
      seq += 1;
      if (timer != null) clearTimeout(timer);
      timer = null;
      if (active) active.abort();
      active = null;
    }
    async function run(more: unknown): Promise<void> {
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
        const err = error as { status?: unknown; name?: string; message?: string };
        if (more && err.status === 409) {
          emit({ results: [], next_offset: null });
          return run(false);
        }
        if (err.name === "AbortError" && !timedOut) return;
        emit({ status: "error", error: timedOut ? "搜索超时，请重试" : (err.message || "搜索失败，请重试") });
      } finally {
        clearTimeout(timeoutId);
        if (mine === seq) active = null;
      }
    }
    function update(value: unknown, composing?: unknown): void {
      cancel(); // Invalidate old results immediately, before the debounce elapses.
      const query = String(value || "").trim();
      emit({ query: query, status: query ? "pending" : "idle", results: [], total: 0,
        session_count: 0, next_offset: null, revision: null, error: "" });
      if (!opened || composing || !query) return;
      timer = setTimeout(function () { timer = null; run(false); }, delay);
    }
    return {
      open: function (value: unknown): void { opened = true; update(value || ""); },
      close: function (): void { opened = false; cancel(); },
      update: update,
      setScope: function (next: ScopeSelection, composing?: unknown): void {
        const selected: SearchScope = { includeInternal: Boolean(next.includeInternal), includeFilenames: Boolean(next.includeFilenames), archiveScope: next.archiveScope || scope.archiveScope };
        if (selected.includeInternal === scope.includeInternal && selected.includeFilenames === scope.includeFilenames && selected.archiveScope === scope.archiveScope) return;
        scope = selected;
        update(state.query, composing);
      },
      flush: function (): Promise<void> { return run(false); },
      more: function (): Promise<void> | undefined { if (state.status === "ready") return run(true); },
      getState: function (): SearchState { return state; },
    };
  }
  const api: HistorySearchApi = { createController: createController };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else (global as unknown as { HistorySearch: HistorySearchApi }).HistorySearch = api;
})(typeof self !== "undefined" ? self : globalThis);
