const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const QuoteUtils = require('../js/quote_utils.js');
class Node {
  constructor(cls = '') {
    this.children = []; this.clientWidth = 320; this.props = {};
    const classes = new Set(cls.split(' ').filter(Boolean));
    this.classList = { contains: c => classes.has(c), add: c => classes.add(c), remove: c => classes.delete(c),
      toggle: (c, enabled) => enabled ? classes.add(c) : classes.delete(c) };
    this.style = { setProperty: (key, value) => this.props[key] = value };
  }
  appendChild(n) { this.children.push(n); }
  addEventListener() {} setAttribute() {}
  set innerHTML(value) { this.children = []; }
  querySelectorAll(selector) {
    const names = selector.split(',').map(s => s.trim().slice(1));
    return this.children.flatMap(n => [...(names.some(name => n.classList.contains(name)) ? [n] : []), ...n.querySelectorAll(selector)]);
  }
}
function setup(streaming) {
  const quotes = new Node('hidden'), media = new Node('hidden'), blocks = new Node('hidden');
  blocks.appendChild(quotes); blocks.appendChild(media);
  const context = {
    composerQuotes: quotes, composerAttachments: media, composerAttachmentBlocks: blocks, composer: new Node(),
    state: { pendingQuotes: [], streaming }, QuoteUtils,
    el: (tag, cls, text) => { const n = new Node(cls); n.textContent = text; return n; },
    updateScrollBottomOffset() {}, getComputedStyle() { return { paddingLeft: '8px', paddingRight: '8px', columnGap: '6px' }; },
    App: { autosize() {} }, toast() {},
  };
  vm.createContext(context);
  const core = fs.readFileSync(path.join(__dirname, '../js/app/core.js'), 'utf8');
  const start = core.indexOf('  function fitAttachmentBlockGrid(');
  const end = core.indexOf('  // ---------- 导出到共享命名空间', start);
  vm.runInContext(core.slice(start, end), context);
  context.App.syncComposerAttachmentBlocks = context.syncComposerAttachmentBlocks;
  const source = fs.readFileSync(path.join(__dirname, '../js/app/quotes.js'), 'utf8');
  vm.runInContext(source.slice(source.indexOf('  function renderComposerQuotes('), source.indexOf('  // ---------- 气泡引用卡片')), context);
  return { context, quotes, media, blocks };
}
for (const streaming of [false, true]) test(`添加引用后外层区域可见，清除最后引用后隐藏（运行中=${streaming}）`, () => {
  const { context, quotes, blocks } = setup(streaming);
  assert.equal(context.addDraftQuote({ text: '选中的引用原文' }), true);
  assert.equal(context.state.pendingQuotes.length, 1);
  assert.equal(quotes.classList.contains('hidden'), false);
  assert.equal(blocks.classList.contains('hidden'), false);
  assert.equal(context.composer.classList.contains('has-quotes'), true);
  assert.equal(blocks.props['--attachment-block-columns'], '1');
  context.removeDraftQuote(context.state.pendingQuotes[0].id);
  assert.equal(blocks.classList.contains('hidden'), true);
});
test('删除引用后媒体区域仍可见，删除全部附件后外层隐藏', () => {
  const { context, media, blocks } = setup(true);
  context.addDraftQuote({ text: '引用原文' });
  media.appendChild(new Node('media-chip')); media.classList.remove('hidden');
  context.syncComposerAttachmentBlocks();
  assert.equal(blocks.props['--attachment-block-columns'], '2');
  context.clearPendingQuotes();
  assert.equal(blocks.classList.contains('hidden'), false);
  assert.equal(context.composer.classList.contains('has-media'), true);
  media.innerHTML = ''; media.classList.add('hidden'); context.syncComposerAttachmentBlocks();
  assert.equal(blocks.classList.contains('hidden'), true);
});
