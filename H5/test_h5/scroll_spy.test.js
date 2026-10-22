const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const MESSAGES = fs.readFileSync(path.join(__dirname, '../js/app/messages.js'), 'utf8');

// 最小 fake 节点：rect 可配、classList 含 toggle、parentElement 链可配。
class Node {
  constructor(rect) {
    this._rect = rect || { top: 0, bottom: 0 };
    this._classes = new Set();
    this.parentElement = null;
    this.classList = {
      add: (...cs) => cs.forEach(c => this._classes.add(c)),
      remove: (...cs) => cs.forEach(c => this._classes.delete(c)),
      contains: (c) => this._classes.has(c),
      toggle: (c, force) => { if (force) this._classes.add(c); else this._classes.delete(c); },
    };
    this.style = {
      _props: {},
      setProperty: (k, v) => { this.style._props[k] = v; },
      getPropertyValue: (k) => this.style._props[k],
      removeProperty: (k) => { delete this.style._props[k]; },
    };
  }
  getBoundingClientRect() { return this._rect; }
  closest(selector) {
    if (selector.startsWith('.') && this.classList.contains(selector.slice(1))) return this;
    return this.parentElement ? this.parentElement.closest(selector) : null;
  }
}

function slice(from, to) {
  const start = MESSAGES.indexOf(from);
  assert.ok(start >= 0, 'slice start missing: ' + from);
  const end = MESSAGES.indexOf(to, start);
  assert.ok(end > start, 'slice end missing: ' + to);
  return MESSAGES.slice(start, end);
}

function setupCodeblockPin() {
  const chatScroll = new Node({ top: 60, bottom: 960 });
  const chatInner = new Node({ top: 60, bottom: 5000 });
  // 按钮 → 代码块 → 顶层消息 → chatInner
  const rootMsg = new Node({ top: 100, bottom: 600 });
  rootMsg.parentElement = chatInner;
  const codeblock = new Node({ top: 120, bottom: 500 });
  codeblock.parentElement = rootMsg;
  const btn = new Node({ top: 120, bottom: 148 });
  btn.parentElement = codeblock;
  btn.offsetHeight = 28;
  btn.offsetWidth = 60;
  btn.closest = () => codeblock;
  chatInner.querySelectorAll = () => [btn];
  const context = {
    document: { body: { classList: { contains: () => false } } },
    chatScroll, chatInner,
    getComputedStyle: () => ({ marginRight: '12px' }),
  };
  vm.createContext(context);
  vm.runInContext(slice('  const COPY_PIN_OFFSET', '  window.addEventListener("sidebarresizeend"'), context);
  return { context, btn };
}

test('代码块复制按钮：视口内有代码块时不抛错（visible 遮蔽 TDZ 回归）', () => {
  const { context, btn } = setupCodeblockPin();
  assert.doesNotThrow(() => context.updateCodeblockCopyButtons());
  assert.equal(btn.classList.contains('is-hidden'), false);
});

test('代码块复制按钮：顶部越过钉住线时加 is-pinned 并写位移', () => {
  const { context, btn } = setupCodeblockPin();
  // pinY = 60 + 8 = 68；代码块顶部 120 未越过 → 不钉住
  context.updateCodeblockCopyButtons();
  assert.equal(btn.classList.contains('is-pinned'), false);
});

function setupQnavActive(userTops) {
  const chatScroll = new Node({ top: 60, bottom: 960 });
  const qnavUsers = userTops.map(top => (top == null ? null : new Node({ top, bottom: top + 100 })));
  const items = userTops.map(() => new Node());
  const context = {
    chatScroll, qnavUsers,
    qnavRail: { querySelectorAll: () => [] },
    qnavPanel: { querySelectorAll: () => items },
  };
  vm.createContext(context);
  vm.runInContext(slice('  function updateQnavActive()', '  // ---------- 滚动：回到底部'), context);
  context.updateQnavActive();
  return items.findIndex(n => n.classList.contains('active'));
}

test('导航高亮：窗口内用户消息全在 120px 线下方时指向最后一个未渲染条目', () => {
  // 尾部窗口：前 28 条未渲染（null），第 29 条在折叠线下方 → 应高亮第 28 条而非第 1 条
  const tops = [];
  for (let i = 0; i < 28; i++) tops.push(null);
  tops.push(791);
  assert.equal(setupQnavActive(tops), 27);
});

test('导航高亮：已渲染消息越过 120px 线时正常跟随', () => {
  assert.equal(setupQnavActive([null, null, -50, 500]), 2);
  assert.equal(setupQnavActive([-100, -20, 300, 900]), 1);
  assert.equal(setupQnavActive([70, 300, 900, 1200]), 0);
});

test('滚动回调：任一更新器抛错也不卡死 scrollTick，下次滚动仍响应', () => {
  let scrollHandler = null;
  let toggles = 0;
  const rafCallbacks = [];
  const context = {
    chatScroll: {
      scrollHeight: 2000, scrollTop: 0, clientHeight: 900,
      getBoundingClientRect: () => ({ top: 60, bottom: 960 }),
      addEventListener: (type, fn) => { scrollHandler = fn; },
    },
    loadOlderState: { records: null },
    scrollBottomBtn: { classList: { toggle: () => { toggles++; } } },
    updateCodeblockCopyButtons: () => { throw new Error('boom'); },
    updateQnavActive: () => {},
    requestAnimationFrame: (cb) => rafCallbacks.push(cb),
  };
  vm.createContext(context);
  vm.runInContext(slice('  // ---------- 滚动：回到底部', '  // ---------- 代码块复制按钮'), context);
  assert.ok(scrollHandler);
  scrollHandler();
  assert.throws(() => rafCallbacks.shift()(), /boom/);
  // 第二次滚动仍能进回调（修复前 scrollTick 卡死，此处 toggles 不再增加）
  scrollHandler();
  assert.equal(rafCallbacks.length, 1);
  assert.throws(() => rafCallbacks.shift()(), /boom/);
  assert.equal(toggles, 2);
});

class ElNode {
  constructor(cls, text = '') {
    this.cls = cls; this.textContent = text; this.children = [];
    this.dataset = {}; this._classes = new Set(String(cls || '').split(' ').filter(Boolean));
    this._listeners = {};
    this.classList = {
      add: (...cs) => cs.forEach(c => this._classes.add(c)),
      remove: (...cs) => cs.forEach(c => this._classes.delete(c)),
      contains: (c) => this._classes.has(c),
    };
  }
  appendChild(n) { this.children.push(n); }
  addEventListener(type, fn) { (this._listeners[type] = this._listeners[type] || []).push(fn); }
  querySelector(sel) {
    if (sel === '.msg-bubble') return this.children.find(n => n.cls === 'msg-bubble') || null;
    return null;
  }
  querySelectorAll(sel) {
    const want = sel.slice(1);
    return this.children.flatMap(n => [...(n.cls === want ? [n] : []), ...n.querySelectorAll(sel)]);
  }
  set innerHTML(_) { this.children = []; }
}

function setupRebuildQnav(records, rendered) {
  const chatInner = new ElNode('chat-inner');
  rendered.forEach(n => chatInner.appendChild(n));
  const context = {
    chatInner,
    loadOlderState: { records },
    qnav: new ElNode('qnav'), qnavRail: new ElNode('rail'), qnavPanel: new ElNode('panel'),
    QNAV_MAX_DASHES: 40,
    el: (tag, cls, text) => new ElNode(cls, text),
    contentToPlainText: s => s,
    updateQnavActive() {},
  };
  vm.createContext(context);
  vm.runInContext(slice('  let qnavUsers =', '  // ---------- 从用户引用'), context);
  context.rebuildQnav();
  // 跨 vm 域数组原型不同，序列化后回宿主再断言
  return JSON.parse(vm.runInContext(
    'JSON.stringify(qnavUsers.map(function (n) { return n ? n.dataset.round || null : null; }))', context));
}

function userNode(dataset) {
  const n = new ElNode('msg-user');
  n.dataset = Object.assign({}, dataset);
  n.appendChild(new ElNode('msg-bubble', 'q' + (dataset.round || '')));
  return n;
}

test('导航收养：无戳提问气泡按轮次匹配到记录（有戳路径不受影响）', () => {
  const records = [
    { kind: 'user', round: 1, content: 'q1', source_event_index: 5 },
    { kind: 'assistant', round: 1, content: 'a1' },
    { kind: 'user', round: 2, content: 'q2', source_event_index: 6 },
    { kind: 'user', round: 3, content: 'q3', source_event_index: 9 },
  ];
  // 尾部窗口只渲染了第 3 轮的实时提问气泡（仅 data-round，无戳）
  const linkage = setupRebuildQnav(records, [userNode({ round: '3' })]);
  assert.deepEqual(linkage, [null, null, '3']);
  // 有戳节点仍走 byIndex 精确匹配
  const stamped = setupRebuildQnav(records, [userNode({ round: '2', historyIndex: '2', sourceEvent: '6' })]);
  assert.deepEqual(stamped, [null, '2', null]);
});
