const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { JSDOM } = require('jsdom');
const source = fs.readFileSync(path.join(__dirname, '../js/app/messages.js'), 'utf8');

function setup() {
  const dom = new JSDOM('<div id="scroll"><div id="chat"></div></div><div id="nav"></div><div id="rail"></div><div id="panel"></div>');
  const document = dom.window.document;
  const scroll = document.querySelector('#scroll');
  Object.defineProperties(scroll, {scrollHeight:{value:5000,configurable:true}, clientHeight:{value:500,configurable:true}});
  scroll.scrollTop = 2000;
  const timers = new Map(), observers = [];
  let serial = 0, bottoms = 0;
  const context = { document, HistoryParser: require('../js/history_parser'), state: { sessionId: 's' }, chatInner: document.querySelector('#chat'),
    chatScroll: scroll, qnav: document.querySelector('#nav'),
    qnavRail: document.querySelector('#rail'), qnavPanel: document.querySelector('#panel'), QNAV_MAX_DASHES: 40,
    el(tag, cls, text) { const node = document.createElement(tag); node.className = cls; node.textContent = text || ''; return node; },
    window: { getComputedStyle:node => dom.window.getComputedStyle(node),
      setTimeout(fn) { timers.set(++serial, fn); return serial; }, clearTimeout(id) { timers.delete(id); } },
    IntersectionObserver: class {
      constructor(callback) { this.callback = callback; this.disconnected = false; this.observed = []; observers.push(this); }
      observe(node) { this.observed.push(node); }
      unobserve(node) { this.observed = this.observed.filter(item => item !== node); }
      disconnect() { this.disconnected = true; }
    },
    scrollToBottom() { bottoms++; scroll.scrollTop = scroll.scrollHeight-scroll.clientHeight; }, contentToPlainText: s => s,
    updateQnavActive() {}, App: { pauseAutoScroll() {}, refreshAskBlockStates() {}, updateCodeblockCopyButtons() {} },
  };
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('  const RENDER_TAIL_RECORDS'), source.indexOf('  function appendTime(')), context);
  vm.runInContext(source.slice(source.indexOf('  let qnavUsers ='), source.indexOf('  // ---------- 从用户引用')), context);
  // 用轻量内容代替 Markdown 和工具的细节，验证真实分页/导航编排。
  context.renderOneRecordInto = (container, rec) => {
    const node = context.el('div', rec.kind === 'user' ? 'msg-user' : 'msg-assistant-body');
    if (rec.kind === 'user') node.appendChild(context.el('div', 'msg-bubble', rec.content));
    container.appendChild(node);
  };
  context.App.rebuildQnav = context.rebuildQnav;
  const paging = () => vm.runInContext('loadOlderState', context);
  return { context, paging, timers, observers, bottoms: () => bottoms };
}
function records(count) {
  return Array.from({length: count}, (_, i) => ({ kind: i % 10 === 0 ? 'user' : 'assistant',
    content: 'record ' + i, round: Math.floor(i / 10) + 1, source_event_index: i % 10 }));
}

test('search renders the distant exact event directly without filling intervening history', () => {
  const h = setup(), all = records(10000);
  h.context.renderRecords(all, {historyMatch: { round_no: 20, event_index: 8 }});
  assert.ok(h.paging().start <= 198 && h.paging().end > 198);
  assert.ok(h.context.chatInner.children.length < 150);
  assert.equal(h.timers.size, 0);
  assert.equal(h.bottoms(), 0);
  h.context.rebuildQnav();
  assert.equal(h.context.qnavPanel.children.length, 1000);
  assert.equal(h.context.qnavRail.children.length, 40);
});

test('ordinary open keeps the actual tail; incremental render does not destroy paging', () => {
  const h = setup(), all = records(10000);
  h.context.renderRecords(all);
  assert.equal(h.paging().end, all.length);
  assert.ok(h.paging().start > 9800);
  assert.equal(h.bottoms(), 1);
  h.context.renderRecords([{kind:'assistant',content:'live',round:1001}], null);
  assert.equal(h.paging().records, all);
  assert.equal(h.paging().end, 10000);
});

test('one edge load adds one batch, in order, without duplicates or background chaining', async () => {
  const h = setup(), all = records(1000);
  h.context.renderRecords(all, {historyMatch: {round_no:50, event_index:5}});
  const {start, end} = h.paging();
  await h.context.runOlderBatch();
  assert.equal(h.paging().start, start - 80);
  assert.equal(h.paging().end, end);
  await h.context.runNewerBatch();
  assert.equal(h.paging().end, end + 80);
  const nodes = [...h.context.chatInner.querySelectorAll('[data-source-event]')];
  const keys = nodes.map(n => n.dataset.round + ':' + n.dataset.sourceEvent);
  assert.equal(new Set(keys).size, keys.length);
  assert.equal(nodes.length, h.paging().end - h.paging().start);
  assert.equal(h.timers.size, 0);
});

test('holding the native scrollbar blocks both edge loaders and a previously queued timer', async () => {
  const h = setup();
  h.context.renderRecords(records(1000), {historyIndex:450});
  const { start, end, button, newerButton } = h.paging();
  h.context.scheduleBackfill(0);
  const pending = [...h.timers.values()][0];
  h.context.App.isChatScrollbarDragging = () => true;
  await pending();
  h.timers.clear();
  await h.context.runOlderBatch();
  await h.context.runNewerBatch();
  h.observers[0].callback([{target:button,isIntersecting:true}, {target:newerButton,isIntersecting:true}]);
  assert.equal(h.paging().start, start);
  assert.equal(h.paging().end, end);
  assert.equal(h.timers.size, 0);
});

test('windowed history owns edge loading; the legacy observer does not append a batch', () => {
  const h = setup();
  h.context.renderRecords(records(1000), {historyIndex:450});
  const {start,end,button,newerButton} = h.paging();
  h.context.App.historyScroll = {active:()=>true};
  h.observers[0].callback([{target:button,isIntersecting:true},{target:newerButton,isIntersecting:true}]);
  assert.equal(h.paging().start,start);
  assert.equal(h.paging().end,end);
  assert.equal(h.timers.size,0);
});

test('direct window changes disconnect old observers and cancel stale session loads', () => {
  const h = setup(), all = records(10000);
  h.context.renderRecords(all);
  h.context.scheduleBackfill(0);
  const pending = [...h.timers.values()][0];
  h.context.chatInner.replaceChildren();
  h.context.renderRecords(records(20));
  assert.equal(h.observers[0].disconnected, true);
  assert.equal(h.timers.size, 0);
  pending();
  assert.equal(h.paging().records.length, 20);
  assert.equal(h.context.chatInner.children.length, 20);
});

test('search window boundaries preserve adjacent tool call/result pairs', () => {
  const h = setup(), all = records(300);
  all[99].kind = 'tool'; all[100].kind = 'toolResult';
  h.context.renderRecords(all, {historyIndex:120});
  assert.equal(h.paging().start, 99);
  all[218].kind = 'tool'; all[219].kind = 'toolResult';
  h.context.renderRecords(all, {historyIndex:120});
  assert.equal(h.paging().end, 220);
});

test('multiple user messages in one round keep separate navigation targets', () => {
  const h = setup(), all = records(100);
  all[3].kind = 'user';
  h.context.renderRecords(all);
  h.context.rebuildQnav();
  const entries = vm.runInContext('qnavEntries', h.context);
  assert.equal(entries[0].node.dataset.sourceEvent, '0');
  assert.equal(entries[1].node.dataset.sourceEvent, '3');
  assert.notEqual(entries[0].node, entries[1].node);
});

test('complete replay removes the whole pending round from DOM and paging, including injected questions', async () => {
  const h = setup(), all = records(600);
  for (let i = 400; i < all.length; i++) all[i].round = 41;
  h.context.renderRecords(all);
  const live = h.context.el('div', 'msg');
  h.context.chatInner.appendChild(live);
  h.context.scheduleBackfill(0);
  const stale = [...h.timers.values()][0];
  h.context.prepareActiveRoundReplay({sessionId:'s', round:41, replayComplete:true, messageNode:live});
  assert.equal(h.paging().records.length, 400);
  assert.equal(h.paging().start, 400);
  assert.equal(h.paging().end, 400);
  assert.equal(h.timers.size, 0);
  assert.equal(h.context.chatInner.querySelectorAll('[data-round="41"]').length, 0);
  assert.equal(live.parentNode, h.context.chatInner);
  stale();
  await h.context.runOlderBatch();
  assert.ok(h.context.chatInner.querySelector('[data-round="40"]'));
  assert.equal(h.context.chatInner.querySelectorAll('[data-round="41"]').length, 0);
});

test('complete replay preserves an older search window and its paging navigation', async () => {
  const h = setup(), all = records(1000);
  h.context.renderRecords(all, {historyMatch:{round_no:20}});
  const before = [...h.context.chatInner.querySelectorAll('[data-round]')];
  const {start,end} = h.paging();
  h.context.prepareActiveRoundReplay({sessionId:'s', round:100, replayComplete:true});
  assert.equal(h.paging().records.length, 990);
  assert.equal(h.paging().start, start);
  assert.equal(h.paging().end, end);
  assert.deepEqual([...h.context.chatInner.querySelectorAll('[data-round]')], before);
  await h.context.runNewerBatch();
  assert.equal(h.paging().end, end + 80);
});

test('incomplete or legacy replay keeps the pending snapshot as fallback', () => {
  const h = setup(), all = records(300);
  h.context.renderRecords(all);
  const before = h.context.chatInner.innerHTML;
  h.context.prepareActiveRoundReplay({sessionId:'s',round:30,replayComplete:false});
  assert.equal(h.paging().records, all);
  assert.equal(h.context.chatInner.innerHTML, before);
});
