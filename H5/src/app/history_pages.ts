/* Read complete rounds on demand. Disk history/export/model context stay intact. */
(function (App: any) {
  "use strict";
  const { state, chatInner, chatScroll } = App;
  let model: any = null, generation = 0, loading: Promise<any> | null = null,
    observer: IntersectionObserver | null = null, frame: number | null = null, dragging = false;
  let catchUpFloor = 0;
  const buttons = new Map<any, any>(), failed = new Set<number>();
  function supported(): boolean { return !!((window as unknown as { YtoolsAuth?: any }).YtoolsAuth && YtoolsAuth.historyPagingAllowed && API.fetchSessionPage); }
  function reset(): void {
    generation++;
    if (model && model.abort) model.abort.abort();
    model = null; loading = null; failed.clear(); catchUpFloor = 0;
    if (observer) observer.disconnect(); observer = null;
    buttons.forEach(button => button.remove()); buttons.clear();
    if (frame != null) cancelAnimationFrame(frame); frame = null;
  }
  function current(saved: any): boolean { return model === saved && state.sessionId === saved.id; }
  async function parse(page: any): Promise<any> {
    const options = { roundOffset: page.round_offset || 0 };
    return typeof HistoryParseAsync !== "undefined" ? HistoryParseAsync.parse(page.text, options) : HistoryParser.parseHistory(page.text, options);
  }
  function merge(saved: any, page: any, parsed: any): void {
    if (page.revision !== saved.revision) throw new Error("历史版本不一致，请重新打开会话");
    const groups = new Map<number, any[]>();
    for (let n = page.start_round; n <= page.end_round; n++) groups.set(n, []);
    let round = page.start_round;
    parsed.records.forEach((record: any) => {
      if (Number.isInteger(record.round)) round = record.round;
      const key = Math.max(page.start_round, Math.min(page.end_round, round));
      if (!groups.has(key)) groups.set(key, []);
      (groups.get(key) as any[]).push(record);
    });
    groups.forEach((records, number) => { if (!saved.rounds.has(number)) saved.rounds.set(number, records); });
    if (page.questions) saved.questions = page.questions;
    saved.total = page.total_rounds;
  }
  function records(saved: any): any[] {
    return Array.from<any>(saved.rounds).sort((a, b) => a[0] - b[0]).flatMap(entry => entry[1]);
  }
  async function open(id: string, options?: any): Promise<any> {
    reset();
    if (!supported()) return null;
    const ticket = generation, abort = new AbortController();
    let page: any;
    for (let attempt = 0; attempt < 2; attempt++) {
      try { page = await API.fetchSessionPage(id, { signal: abort.signal }); break; }
      catch (error) { if ((error as any).status !== 409 || attempt) throw error; }
    }
    if (ticket !== generation || state.sessionId !== id) return { records: [], stale: true };
    if (page.fallback_full) return null;
    const saved = { id, abort, revision: page.revision, rounds: new Map(), questions: [], total: page.total_rounds };
    model = saved;
    const parsed = await parse(page);
    if (!current(saved)) return { records: [], stale: true };
    merge(saved, page, parsed);
    const target = options && options.historyMatch && Number(options.historyMatch.round_no);
    if (target > 0 && !saved.rounds.has(target)) {
      const selected = await API.fetchSessionPage(id, { around: target, revision: saved.revision, include_questions: false, signal: abort.signal });
      if (selected.fallback_full) { reset(); return null; }
      const part = await parse(selected);
      if (!current(saved)) return { records: [], stale: true };
      merge(saved, selected, part);
    }
    return Object.assign({}, parsed, { records: records(saved) });
  }
  function anchor(): { record: any; y: number } | null {
    const view = App.getHistoryWindow(), top = chatScroll.getBoundingClientRect().top;
    const node = Array.from<any>(chatInner.children).find(node => node.hasAttribute("data-history-index") && node.getBoundingClientRect().bottom > top);
    return node && { record: view.records[Number(node.dataset.historyIndex)], y: node.getBoundingClientRect().top };
  }
  function repaint(saved: any, targetRound: number | null): void {
    const savedAnchor = anchor(), all = records(saved);
    const live = Array.from<any>(chatInner.children).filter(node => !node.hasAttribute("data-history-index") && !node.matches(".qnav-load-older,.process-group-summary"));
    if (App.historyScroll) App.historyScroll.beforeRender();
    const at = targetRound ? all.findIndex(r => Number(r.round) === targetRound) : savedAnchor ? all.indexOf(savedAnchor.record) : 0;
    chatInner.replaceChildren();
    App.renderRecords(all, { historyIndex: Math.max(0, at), noScroll: true });
    live.forEach(node => chatInner.appendChild(node));
    if (App.historyScroll) App.historyScroll.refresh({ preserve: false });
    if (!targetRound && savedAnchor) {
      const index = all.indexOf(savedAnchor.record);
      const node = chatInner.querySelector('[data-history-index="' + index + '"]');
      if (node) {
        const previous = chatScroll.style.scrollBehavior; chatScroll.style.scrollBehavior = "auto";
        chatScroll.scrollTop += node.getBoundingClientRect().top - savedAnchor.y;
        chatScroll.style.scrollBehavior = previous;
      }
    }
    App.rebuildQnav(); sync();
  }
  async function load(round: number, query: any, reveal: boolean): Promise<any> {
    const saved = model;
    if (!saved || !current(saved)) return false;
    if (saved.rounds.has(round)) return true;
    if (loading) { await loading; return current(saved) ? load(round, query, reveal) : false; }
    loading = (async () => {
      try {
        const page = await API.fetchSessionPage(saved.id, Object.assign({ revision: saved.revision, include_questions: false, signal: saved.abort.signal }, query || { around: round }));
        if (!current(saved)) return false;
        if (page.fallback_full) { await App.openSession(saved.id, { historyMatch: { session_id: saved.id, round_no: round } }); return true; }
        const parsed = await parse(page);
        if (!current(saved)) return false;
        merge(saved, page, parsed); repaint(saved, reveal ? round : null); return true;
      } catch (error) {
        if (!current(saved) || (error as Error).name === "AbortError") return false;
        if ((error as any).status === 409) {
          App.toast("历史已更新，正在重新加载当前版本");
          return App.openSession(saved.id, { historyMatch: { session_id: saved.id, round_no: round } });
        }
        failed.add(round); App.toast("历史加载失败，可点击重试：" + (error as Error).message); return false;
      }
    })();
    try { return await loading; } finally { loading = null; schedule(); catchUp(); }
  }
  function gaps(saved: any): Array<{ start: number; end: number }> {
    const loaded = Array.from<any>(saved.rounds.keys()).filter(n => n > 0).sort((a, b) => a - b), result: Array<{ start: number; end: number }> = [];
    let next = 1;
    loaded.forEach(n => { if (n > next) result.push({ start: next, end: n - 1 }); next = n + 1; });
    if (next <= saved.total) result.push({ start: next, end: saved.total });
    return result;
  }
  function sync(): void {
    const saved = model;
    if (!saved || !current(saved)) return;
    const nodes = Array.from<any>(chatInner.children).filter(node => node.hasAttribute("data-history-index") && node.dataset.round);
    const wanted = new Set<string>();
    gaps(saved).forEach(gap => {
      const right = nodes.find(node => Number(node.dataset.round) === gap.end + 1);
      const left = nodes.filter(node => Number(node.dataset.round) === gap.start - 1).pop();
      if (!right && !left) return;
      const key = gap.start + ":" + gap.end; wanted.add(key);
      let button = buttons.get(key);
      if (button && !button.isConnected) { buttons.delete(key); button = null; }
      if (!button) {
        button = App.el("button", "qnav-load-older history-page-gap", "加载第 " + gap.start + "–" + gap.end + " 轮历史");
        button.type = "button"; button.dataset.pageGap = key;
        const target = right ? gap.end : gap.start;
        button.addEventListener("click", () => { failed.delete(target); load(target, right ? { before: gap.end + 1 } : { before: Math.min(gap.end + 1, gap.start + 20) }, false); });
        buttons.set(key, button);
        if (right) right.before(button); else left.after(button);
        if (typeof IntersectionObserver !== "undefined") {
          if (!observer) observer = new IntersectionObserver(entries => entries.forEach(entry => {
            if (!entry.isIntersecting || dragging || loading || !entry.target.isConnected) return;
            const [a, b] = ((entry.target as HTMLElement).dataset.pageGap as string).split(":").map(Number);
            if (!failed.has(a) && !failed.has(b)) (entry.target as HTMLElement).click();
          }), { root: chatScroll });
          observer.observe(button);
        }
      }
      button.disabled = !!loading;
    });
    buttons.forEach((button, key) => { if (!wanted.has(key)) { if (observer) observer.unobserve(button); button.remove(); buttons.delete(key); } });
  }
  // 拖动松手（或单次到货）后若已贴边且缺口按钮可见，补点一次；到货后仍贴边则
  // 经 load 收尾继续接力，直到缺口补完、用户介入或到货无进展。拖动中禁止自动
  // 点（滑块会乱跳），松手前 Observer 也不重算，因此松手时必须显式补一次，
  // 否则拖到顶永远停在已加载内容的边界上（2026-10-28 用户实测）。
  // 有界性：每次接力要求轮次数严格增长（catchUpFloor 单调），总轮数有限故必终止；
  // 失败轮次由 failed 集合跳过，用户滚动/拖动/切会话会打破贴边或 current 校验而停下。
  function catchUp(): void {
    const saved = model;
    if (!saved || !current(saved) || loading || dragging) return;
    if (saved.rounds.size <= catchUpFloor) return;
    const view = chatScroll.getBoundingClientRect();
    const nearTop = chatScroll.scrollTop <= 4;
    const nearBottom = chatScroll.scrollHeight - chatScroll.clientHeight - chatScroll.scrollTop <= 4;
    if (!nearTop && !nearBottom) return;
    sync();
    let pick: any = null, pickEdge = nearTop ? Infinity : -Infinity;
    buttons.forEach(button => {
      if (!button.isConnected) return;
      const rect = button.getBoundingClientRect();
      if (rect.bottom <= view.top || rect.top >= view.bottom) return;
      const [a, b] = button.dataset.pageGap.split(":").map(Number);
      if (failed.has(a) && failed.has(b)) return;
      const edge = nearTop ? rect.top : rect.bottom;
      if (nearTop ? edge < pickEdge : edge > pickEdge) { pick = button; pickEdge = edge; }
    });
    if (!pick) return;
    catchUpFloor = saved.rounds.size;
    pick.disabled = false; pick.click();
  }
  function schedule(): void {
    if (frame != null) return;
    frame = requestAnimationFrame(() => { frame = null; sync(); });
  }
  function navigation(entries: any[]): any[] {
    if (!model || !current(model)) return entries;
    const original = entries.slice(), used = new Set<any>();
    const byEvent = new Map(original.map(item => [item.round + ":" + item.eventIndex, item]));
    const all = model.questions.map((question: any) => {
      const entry = byEvent.get(question.round + ":" + question.event_index);
      if (entry) { used.add(entry); return entry; }
      return { index: -1, round: question.round, eventIndex: question.event_index, text: question.text, node: null };
    });
    original.forEach(entry => { if (!used.has(entry)) all.push(entry); });
    return all.sort((a: any, b: any) => a.round - b.round || (a.eventIndex || 0) - (b.eventIndex || 0));
  }
  function deferResult(ui: any, record: any): void {
    const saved = model, ref = record.history_detail, head = ui.wrap.querySelector(".tool-block-head");
    if (!saved || !head) return;
    const output = ui.wrap.querySelector(".tool-io-out");
    if (output) output.textContent = "展开加载完整执行结果";
    async function reveal(): Promise<void> {
      if (!ui.wrap.classList.contains("open") || !current(saved)) return;
      try {
        if (record.history_detail) {
          if (!record.detailPending) record.detailPending = API.fetchHistoryEvent(saved.id, ref, saved.abort.signal).then((data: any) => {
            if (!current(saved)) return;
            record.result = typeof data.event.result === "string" ? data.event.result : JSON.stringify(data.event.result, null, 2);
            record.file_diff = data.event.file_diff; delete record.history_detail;
          }).finally(() => { delete record.detailPending; });
          if (output) output.textContent = "正在加载执行结果…";
          await record.detailPending;
        }
        if (current(saved) && ui.wrap.isConnected && !record.history_detail) {
          App.applyToolResult(ui, record.name, record.result, record.file_diff, record.args);
          ui.finish();
        }
      } catch (error) {
        if (current(saved) && (error as Error).name !== "AbortError" && output) output.textContent = (error as any).status === 409 ?
          "历史已更新，请重新打开会话后查看此结果。" : "加载失败，关闭后展开可重试：" + (error as Error).message;
      }
    }
    head.addEventListener("click", reveal);
    if (ui.wrap.classList.contains("open")) reveal();
  }
  App.historyPages = { open, reset, sync: schedule, navigation,
    hasRound: (round: any) => !model || model.rounds.has(Number(round)),
    ensureRound: (round: any) => load(Number(round), null, true),
    loadBefore: (before: any) => load(Number(before) - 1, { before: Number(before) }, false),
    deferResult,
    active: () => !!model };
  chatScroll.addEventListener("mousedown", (event: any) => { if (event.target === chatScroll) dragging = true; }, { passive: true });
  window.addEventListener("mouseup", () => { dragging = false; schedule(); catchUp(); }, { passive: true });
  if (typeof MutationObserver !== "undefined") new MutationObserver(schedule).observe(chatInner, { childList: true });
})((window as unknown as { App: any }).App);
