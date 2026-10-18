const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Node {
  constructor(cls = '') {
    this.children = []; this.clientWidth = 320; this.props = {};
    this.dataset = {}; this.hidden = false; this.textContent = '';
    this.listeners = {};
    const classes = new Set(cls.split(' ').filter(Boolean));
    this.classList = { contains: c => classes.has(c), add: c => classes.add(c), remove: c => classes.delete(c),
      toggle: (c, enabled) => enabled ? classes.add(c) : classes.delete(c) };
    this.style = { setProperty: (key, value) => this.props[key] = value };
  }
  appendChild(n) { this.children.push(n); n.parentNode = this; }
  addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); }
  setAttribute(k, v) { this[k] = v; }
  click() { (this.listeners.click || []).forEach(fn => fn()); }
  querySelectorAll(selector) {
    const names = selector.split(',').map(s => s.trim().slice(1));
    return this.children.flatMap(n => [...(names.some(name => n.classList.contains(name)) ? [n] : []), ...n.querySelectorAll(selector)]);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}

function setup(width) {
  const blocks = new Node();
  blocks.clientWidth = width;
  const frames = [];
  const context = {
    getComputedStyle: () => ({ paddingLeft: '8px', paddingRight: '8px', columnGap: '6px' }),
    requestAnimationFrame: fn => frames.push(fn),
    el: (tag, cls, text) => new Node(cls),
  };
  vm.createContext(context);
  const core = fs.readFileSync(path.join(__dirname, '../js/app/core.js'), 'utf8');
  const start = core.indexOf('  function fitAttachmentBlockGrid(');
  const end = core.indexOf('  // ---------- 导出到共享命名空间', start);
  vm.runInContext(core.slice(start, end), context);
  const fit = blocks => context.fitAttachmentBlockGrid(blocks);
  const flush = () => { while (frames.length) frames.shift()(); };
  return { blocks, fit, flush, frames };
}

function tiles(blocks, count, cls = 'msg-user-media-thumb') {
  for (let i = 0; i < count; i++) blocks.appendChild(new Node(cls));
  return blocks;
}

test('不可测时延迟到下一帧重试，可测后按宽度算列', () => {
  const { blocks, fit, flush, frames } = setup(0);
  tiles(blocks, 2);
  fit(blocks);
  assert.equal(blocks.props['--attachment-block-columns'], undefined);
  assert.equal(frames.length, 1);
  blocks.clientWidth = 320;
  flush();
  assert.equal(blocks.props['--attachment-block-columns'], '2');
  assert.equal(frames.length, 0);
});

test('重试耗尽后回退单列，不抛错', () => {
  const { blocks, fit, flush } = setup(0);
  tiles(blocks, 2);
  fit(blocks);
  for (let i = 0; i < 6; i++) flush();
  assert.equal(blocks.props['--attachment-block-columns'], '1');
});

test('超出两行折叠进 +N 展开块，点击展开/收起', () => {
  const { blocks, fit } = setup(320);
  tiles(blocks, 10);
  fit(blocks);
  // 320-16 可用：floor((304+6)/58)=5 列，两行容量 10 → 10 <= 10 不折叠
  assert.equal(blocks.props['--attachment-block-columns'], '5');
  assert.equal(blocks.querySelectorAll('.attachment-more-tile').length, 0);
  // 收窄到 3 列：容量 6，10 个需折叠（5 可见 + +5）
  blocks.clientWidth = 3 * 58 + 10;
  fit(blocks);
  assert.equal(blocks.props['--attachment-block-columns'], '3');
  const more = blocks.querySelector('.attachment-more-tile');
  assert.ok(more);
  assert.equal(more.textContent, '+5');
  const visibles = blocks.children.filter(n => n.classList.contains('msg-user-media-thumb') && !n.hidden);
  assert.equal(visibles.length, 5);
  more.click();
  assert.ok(blocks.children.filter(n => n.classList.contains('msg-user-media-thumb')).every(n => !n.hidden));
  assert.equal(more.textContent, '收起');
  more.click();
  assert.equal(blocks.children.filter(n => n.classList.contains('msg-user-media-thumb') && !n.hidden).length, 5);
  assert.equal(more.textContent, '+5');
});

test('缩减到容纳范围内时移除展开块并恢复全部', () => {
  const { blocks, fit } = setup(3 * 58 + 10);
  tiles(blocks, 10);
  fit(blocks);
  assert.equal(blocks.querySelectorAll('.attachment-more-tile').filter(n => !n.hidden).length, 1);
  blocks.clientWidth = 1200;
  fit(blocks);
  assert.equal(blocks.querySelectorAll('.attachment-more-tile').filter(n => !n.hidden).length, 0);
  assert.ok(blocks.children.filter(n => n.classList.contains('msg-user-media-thumb')).every(n => !n.hidden));
});

test('单列窄屏也只占两行（1 可见 + 展开块）', () => {
  const { blocks, fit } = setup(60);
  tiles(blocks, 4);
  fit(blocks);
  assert.equal(blocks.props['--attachment-block-columns'], '1');
  const more = blocks.querySelector('.attachment-more-tile');
  assert.equal(more.textContent, '+3');
  assert.equal(blocks.children.filter(n => n.classList.contains('msg-user-media-thumb') && !n.hidden).length, 1);
});
