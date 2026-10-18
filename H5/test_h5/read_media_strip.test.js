const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const Strip = require('../js/read_media_strip.js');

function makeNode(tag, cls, text) {
  const node = {
    tagName: tag, className: cls || '', children: [], parentNode: null,
    dataset: {}, textContent: text || '', hidden: false, title: '',
    listeners: {},
    appendChild(c) { c.parentNode = node; node.children.push(c); return c; },
    removeChild(c) { const i = node.children.indexOf(c); if (i >= 0) node.children.splice(i, 1); c.parentNode = null; },
    remove() { if (node.parentNode) node.parentNode.removeChild(node); },
    addEventListener(t, fn) { (node.listeners[t] = node.listeners[t] || []).push(fn); },
    click() { (node.listeners.click || []).forEach(fn => fn()); },
    fire(t) { (node.listeners[t] || []).forEach(fn => fn()); },
    querySelector(sel) { return queryAll(node, sel)[0] || null; },
    querySelectorAll(sel) { return queryAll(node, sel); },
  };
  node.classList = {
    add: (...cs) => { node.className = [...new Set((node.className + ' ' + cs.join(' ')).split(/\s+/).filter(Boolean))].join(' '); },
    remove: (...cs) => { node.className = node.className.split(/\s+/).filter(c => c && !cs.includes(c)).join(' '); },
    toggle: (c, force) => { const has = node.classList.contains(c); const on = force === undefined ? !has : !!force; on ? node.classList.add(c) : node.classList.remove(c); },
    contains: c => node.className.split(/\s+/).includes(c),
  };
  return node;
}

function matches(node, sel) {
  const m = sel.match(/^\.([^\[]+?)(?:\[([^\]=]+)="([^"]*)"\])?$/);
  if (!m) return false;
  for (const c of m[1].split('.')) {
    if (!(node.className || '').split(/\s+/).includes(c)) return false;
  }
  if (m[2]) {
    const prop = m[2].startsWith('data-')
      ? m[2].slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase()) : m[2];
    if (String(node.dataset[prop] ?? '') !== m[3]) return false;
  }
  return true;
}

function queryAll(root, sel) {
  const out = [];
  (function walk(node) {
    for (const child of node.children) {
      if (matches(child, sel)) out.push(child);
      walk(child);
    }
  })(root);
  return out;
}

const el = (tag, cls, text) => makeNode(tag, cls, text);

test('parseReadMediaResult 只接受含 loaded/skipped 的结果', () => {
  const ok = { loaded: [{ reference: 'media://a.png' }], skipped: [] };
  assert.deepEqual(Strip.parseReadMediaResult(JSON.stringify(ok)), ok);
  assert.deepEqual(Strip.parseReadMediaResult(ok), ok);
  assert.equal(Strip.parseReadMediaResult('not json'), null);
  assert.equal(Strip.parseReadMediaResult(JSON.stringify({ ok: true })), null);
  assert.equal(Strip.parseReadMediaResult(JSON.stringify({ loaded: [], skipped: [] })), null);
  assert.equal(Strip.parseReadMediaResult(null), null);
  assert.equal(Strip.parseReadMediaResult(42), null);
});

test('displayNameForReference 取文件名', () => {
  assert.equal(Strip.displayNameForReference('media://clipboard-1.png'), 'clipboard-1.png');
  assert.equal(Strip.displayNameForReference('https://x.test/a/b.png?size=1#frag'), 'b.png');
  assert.equal(Strip.displayNameForReference('C:\\Users\\a\\b.jpg'), 'b.jpg');
  assert.equal(Strip.displayNameForReference('/tmp/x/y.webp'), 'y.webp');
  assert.equal(Strip.displayNameForReference(''), '未知来源');
  assert.equal(Strip.displayNameForReference(null), '未知来源');
});

test('resolveReferenceUrl 按来源拼装可访问地址', () => {
  const API = {
    sessionMediaUrl: (sid, name) => `media:${sid}/${name}`,
    localFileUrl: (sid, p) => `local:${sid}/${p}`,
  };
  assert.equal(Strip.resolveReferenceUrl(API, 'media://a.png', 's'), 'media:s/a.png');
  assert.equal(Strip.resolveReferenceUrl(API, 'https://x.test/a.png', 's'), 'https://x.test/a.png');
  assert.equal(Strip.resolveReferenceUrl(API, 'data:image/png;base64,AA', 's'), 'data:image/png;base64,AA');
  assert.equal(Strip.resolveReferenceUrl(API, 'C:\\a\\b.png', 's'), 'local:s/C:\\a\\b.png');
  assert.equal(Strip.resolveReferenceUrl(API, '', 's'), null);
  assert.equal(Strip.resolveReferenceUrl({}, 'C:\\a\\b.png', 's'), null);
});

test('previewTypeForKind 按 kind/mime 判定预览类型', () => {
  assert.equal(Strip.previewTypeForKind('video'), 'video');
  assert.equal(Strip.previewTypeForKind('audio'), 'audio');
  assert.equal(Strip.previewTypeForKind('image'), 'image');
  assert.equal(Strip.previewTypeForKind(undefined, 'video/mp4'), 'video');
  assert.equal(Strip.previewTypeForKind(undefined, 'audio/mpeg'), 'audio');
  assert.equal(Strip.previewTypeForKind(undefined, 'image/png'), 'image');
  assert.equal(Strip.previewTypeForKind(undefined, undefined), 'image');
});

test('formatDuration 与标题文案', () => {
  assert.equal(Strip.formatDuration(65), '1:05');
  assert.equal(Strip.formatDuration(7), '0:07');
  assert.equal(Strip.formatDuration(null), '');
  assert.equal(Strip.formatDuration(-1), '');
  const loaded = [{ kind: 'image' }, { kind: 'image' }, { kind: 'video' }, { kind: 'audio' }, { kind: 'other' }];
  assert.equal(Strip.headerTextForCounts(loaded, [{}, {}]), '已查看 2 张图像、1 个视频、1 个音频、1 项媒体（2 项未读取）');
  assert.equal(Strip.headerTextForCounts([{ kind: 'image' }], []), '已查看 1 张图像');
  assert.equal(Strip.headerTextForCounts([], [{}]), '媒体未读取（1 项未读取）');
});

test('buildStripItems 组装条目模型', () => {
  const API = {
    sessionMediaUrl: (sid, name) => `media:${sid}/${name}`,
    localFileUrl: (sid, p) => `local:${sid}/${p}`,
  };
  const model = Strip.buildStripItems(API, {
    loaded: [
      { reference: 'media://a.png', kind: 'image', mime: 'image/png' },
      { reference: 'C:\\v\\b.mp4', kind: 'video', video: { duration: 65 } },
      { reference: '', kind: 'image' },
    ],
    skipped: [{ reference: 'media://gone.png', reason: '未找到，可能已删除: media://gone.png' }],
  }, 's');
  assert.equal(model.headerText, '已查看 2 张图像、1 个视频（1 项未读取）');
  assert.equal(model.items.length, 4);
  assert.deepEqual(model.items[0], { tile: 'image', name: 'a.png', url: 'media:s/a.png', previewType: 'image', reason: '', duration: '' });
  assert.equal(model.items[1].tile, 'video');
  assert.equal(model.items[1].duration, '1:05');
  assert.equal(model.items[1].previewType, 'video');
  assert.equal(model.items[2].tile, 'missing');
  assert.equal(model.items[3].tile, 'skipped');
  assert.match(model.items[3].reason, /未找到/);
});

test('图片条默认折叠，点击展开后才设置 src，再点收起', () => {
  const previews = [];
  const strip = Strip.buildMediaStrip({ el, onPreview: item => previews.push(item) }, '已查看 2 张图像', [
    { tile: 'image', name: 'a.png', url: 'media:s/a.png', previewType: 'image', reason: '', duration: '' },
    { tile: 'video', name: 'b.mp4', url: 'local:s/b.mp4', previewType: 'video', reason: '', duration: '1:05' },
  ]);
  const grid = strip.querySelector('.read-media-grid');
  const head = strip.querySelector('.read-media-head');
  assert.equal(grid.hidden, true);
  assert.equal(strip.querySelector('.read-media-label').textContent, '已查看 2 张图像');
  const img = grid.children[0].children[0];
  assert.equal(img.src, undefined, '折叠时不预加载');
  head.click();
  assert.equal(grid.hidden, false);
  assert.equal(strip.classList.contains('open'), true);
  assert.equal(img.src, 'media:s/a.png');
  assert.equal(grid.children[1].children[0].src, 'local:s/b.mp4');
  assert.equal(grid.children[1].children[1].textContent, '1:05');
  head.click();
  assert.equal(grid.hidden, true);
  assert.equal(strip.classList.contains('open'), false);
});

test('点击缩略图进预览，skipped 项不可点', () => {
  const previews = [];
  const strip = Strip.buildMediaStrip({ el, onPreview: item => previews.push(item) }, '已查看 1 张图像（1 项未读取）', [
    { tile: 'image', name: 'a.png', url: 'media:s/a.png', previewType: 'image', reason: '', duration: '' },
    { tile: 'skipped', name: 'gone.png', url: null, previewType: null, reason: '未找到，可能已删除', duration: '' },
  ]);
  const grid = strip.querySelector('.read-media-grid');
  strip.querySelector('.read-media-head').click();
  grid.children[0].click();
  assert.equal(previews.length, 1);
  assert.deepEqual(previews[0], { tile: 'image', name: 'a.png', url: 'media:s/a.png', previewType: 'image', reason: '', duration: '' });
  grid.children[1].click();
  assert.equal(previews.length, 1, 'skipped 项不触发预览');
  assert.match(grid.children[1].children.map(c => c.textContent).join('|'), /未找到/);
});

test('图片加载失败就地替换为找不到占位', () => {
  const strip = Strip.buildMediaStrip({ el, onPreview: () => {} }, '已查看 1 张图像', [
    { tile: 'image', name: 'gone.png', url: 'media:s/gone.png', previewType: 'image', reason: '', duration: '' },
  ]);
  const grid = strip.querySelector('.read-media-grid');
  strip.querySelector('.read-media-head').click();
  const tile = grid.children[0];
  tile.children[0].fire('error');
  assert.equal(tile.classList.contains('is-missing'), true);
  assert.match(tile.children.map(c => c.textContent).join('|'), /找不到图片，可能已删除/);
  assert.match(tile.children.map(c => c.textContent).join('|'), /gone\.png/);
});

function renderStrip() {
  const source = fs.readFileSync(path.join(__dirname, '../js/app/messages.js'), 'utf8');
  const start = source.indexOf('  /** read_media 结果的图片条');
  const end = source.indexOf('  /** 工具输出统一入口', start);
  const previews = [];
  const context = {
    state: { sessionId: 'fallback-sid' },
    el: (tag, cls, text) => makeNode(tag, cls, text),
    API: {
      sessionMediaUrl: (sid, name) => `media:${sid}/${name}`,
      localFileUrl: (sid, p) => `local:${sid}/${p}`,
    },
    App: { openMediaPreview: opts => previews.push(opts) },
    ReadMediaStrip: Strip,
  };
  vm.createContext(context);
  vm.runInContext(source.slice(start, end), context);
  return { render: context.renderReadMediaStrip, previews };
}

function toolBlock(sessionId) {
  const wrap = makeNode('div', 'tool-block');
  const body = makeNode('div', 'tool-block-body');
  const outPre = makeNode('pre', 'tool-io tool-io-out');
  body.appendChild(outPre);
  wrap.appendChild(body);
  const block = { wrap, sessionId };
  return { block, body };
}

const READ_MEDIA_RESULT = JSON.stringify({
  ok: true,
  loaded: [
    { reference: 'media://a.png', kind: 'image', mime: 'image/png' },
    { reference: 'media://b.png', kind: 'image', mime: 'image/png' },
  ],
  skipped: [],
});

test('接线：结果渲染图片条，重复应用幂等并保留展开态', () => {
  const { render, previews } = renderStrip();
  const { block, body } = toolBlock('s');
  render(block, READ_MEDIA_RESULT);
  assert.equal(body.querySelectorAll('.read-media-strip').length, 1);
  assert.match(body.querySelector('.read-media-label').textContent, /已查看 2 张图像/);
  assert.equal(body.querySelector('.read-media-grid').hidden, true);
  body.querySelector('.read-media-head').click();
  assert.equal(body.querySelector('.read-media-grid').hidden, false);
  assert.equal(body.querySelector('.read-media-grid').children[0].children[0].src, 'media:s/a.png');
  render(block, READ_MEDIA_RESULT);
  assert.equal(body.querySelectorAll('.read-media-strip').length, 1, '重复应用不叠加');
  assert.equal(body.querySelector('.read-media-grid').hidden, false, '保留展开态');
  assert.equal(body.querySelector('.read-media-grid').children[0].children[0].src, 'media:s/a.png', '重建后仍已 hydration');
  body.querySelector('.read-media-grid').children[0].click();
  assert.equal(previews.length, 1);
  assert.equal(JSON.stringify(previews[0]), JSON.stringify({ type: 'image', src: 'media:s/a.png', title: 'a.png' }));
});

test('接线：解析失败不画条；无 sessionId 回退当前会话', () => {
  const { render } = renderStrip();
  const { block, body } = toolBlock('s');
  render(block, READ_MEDIA_RESULT);
  assert.equal(body.querySelectorAll('.read-media-strip').length, 1);
  render(block, 'not json');
  assert.equal(body.querySelectorAll('.read-media-strip').length, 0, '解析失败清除旧条');
  const anon = toolBlock(undefined);
  render(anon.block, READ_MEDIA_RESULT);
  anon.block.wrap.querySelector('.read-media-head').click();
  assert.equal(anon.body.querySelector('.read-media-grid').children[0].children[0].src, 'media:fallback-sid/a.png');
});
