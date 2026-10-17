const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

// 最小 fake DOM：支持 class 记录、监听器记录与手动触发、按类名查询。
class Node {
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
  appendChild(n) { this.children.push(n); n.parentNode = this; }
  querySelector(selector) {
    if (selector === '.msg-bubble') return this.children.find(n => n.cls === 'msg-bubble') || null;
    return null;
  }
  querySelectorAll(selector) {
    const want = selector.slice(1);
    return this.children.flatMap(n => [...(n.cls === want ? [n] : []), ...n.querySelectorAll(selector)]);
  }
  addEventListener(type, fn) { (this._listeners[type] = this._listeners[type] || []).push(fn); }
  fire(type, arg) { (this._listeners[type] || []).forEach(fn => fn(arg || {})); }
  closest(selector) { return this.cls === selector.slice(1) ? this : null; }
  contains(node) { return node === this || this.children.some(n => n === node || n.contains(node)); }
  set innerHTML(value) { this.children = []; }
}

function setup(count) {
  const messages = fs.readFileSync(path.join(__dirname, '../js/app/messages.js'), 'utf8');
  const root = new Node('root');
  for (let i = 0; i < count; i++) {
    const user = new Node('msg-user'); user.appendChild(new Node('msg-bubble', '问题' + (i + 1)));
    root.appendChild(user);
  }
  const context = {
    chatInner: root, qnav: new Node('qnav'), qnavRail: new Node('rail'), qnavPanel: new Node('panel'),
    QNAV_MAX_DASHES: 40, el: (tag, cls, text) => new Node(cls, text), updateQnavActive() {},
  };
  vm.createContext(context);
  const navStart = messages.indexOf('  let qnavUsers =');
  const navEnd = messages.indexOf('  // ---------- 从用户引用', navStart);
  vm.runInContext(messages.slice(navStart, navEnd), context);
  context.rebuildQnav();
  return context;
}

test('hover 涟漪：所在条无 near 类，相邻按距离加 near-1/near-2', () => {
  const { qnavRail } = setup(5);
  const dashes = qnavRail.querySelectorAll('.qnav-dash');
  assert.equal(dashes.length, 5);
  dashes[2].fire('mouseenter');
  const near = dashes.map(d => d.classList.contains('near-1') ? 1 : d.classList.contains('near-2') ? 2 : 0);
  assert.deepEqual(near, [2, 1, 0, 1, 2]);
  // 换一条 hover：旧涟漪清除，新涟漪按新位置计算
  dashes[0].fire('mouseenter');
  const near2 = dashes.map(d => d.classList.contains('near-1') ? 1 : d.classList.contains('near-2') ? 2 : 0);
  assert.deepEqual(near2, [0, 1, 2, 0, 0]);
});

test('hover 联动面板段首项 peek，离开 rail 后全部清除', () => {
  const { qnavRail, qnavPanel } = setup(5);
  const dashes = qnavRail.querySelectorAll('.qnav-dash');
  dashes[3].fire('mouseenter');
  const items = qnavPanel.querySelectorAll('.qnav-item');
  assert.equal(items.length, 5);
  assert.deepEqual(items.map(d => d.classList.contains('peek')), [false, false, false, true, false]);
  qnavRail.fire('mouseleave');
  assert.ok(dashes.every(d => !d.classList.contains('near-1') && !d.classList.contains('near-2')));
  assert.ok(items.every(d => !d.classList.contains('peek')));
});

test('多次重建导航只绑定一次 rail mouseleave', () => {
  const context = setup(5);
  context.rebuildQnav();
  context.rebuildQnav();
  assert.equal((context.qnavRail._listeners.mouseleave || []).length, 1);
});

test('鼠标在轨道空隙移动时清除涟漪与面板 peek', () => {
  const { qnavRail, qnavPanel } = setup(5);
  const dashes = qnavRail.querySelectorAll('.qnav-dash');
  dashes[2].fire('mouseenter');
  assert.ok(dashes[1].classList.contains('near-1'));
  // 移到空隙：事件 target 为 rail 本身，closest 找不到 dash
  qnavRail.fire('mousemove', { target: qnavRail });
  assert.ok(dashes.every(d => !d.classList.contains('near-1') && !d.classList.contains('near-2')));
  assert.ok(qnavPanel.querySelectorAll('.qnav-item').every(d => !d.classList.contains('peek')));
  // 在横条上移动不清除：target 为 dash 本身
  dashes[2].fire('mouseenter');
  qnavRail.fire('mousemove', { target: dashes[2] });
  assert.ok(dashes[1].classList.contains('near-1'));
});

test('鼠标进入面板浏览时清除轨道涟漪', () => {
  const { qnavRail, qnavPanel } = setup(5);
  const dashes = qnavRail.querySelectorAll('.qnav-dash');
  dashes[1].fire('mouseenter');
  assert.ok(dashes[0].classList.contains('near-1'));
  qnavPanel.fire('mouseenter');
  assert.ok(dashes.every(d => !d.classList.contains('near-1') && !d.classList.contains('near-2')));
  assert.ok(qnavPanel.querySelectorAll('.qnav-item').every(d => !d.classList.contains('peek')));
});
