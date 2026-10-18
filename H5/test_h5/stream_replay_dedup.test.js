const test = require('node:test');
const assert = require('node:assert/strict');
const Dedup = require('../js/stream_replay_dedup.js');

function makeNode(tag, cls, attrs) {
  const node = {
    tagName: tag, className: cls || '', children: [], parentNode: null,
    dataset: {}, textContent: '',
    appendChild(c) { c.parentNode = node; node.children.push(c); return c; },
    contains(other) { let n = other; while (n) { if (n === node) return true; n = n.parentNode; } return false; },
    querySelector(sel) { return queryAll(node, sel)[0] || null; },
    querySelectorAll(sel) { return queryAll(node, sel); },
  };
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k.startsWith('data-')) {
      node.dataset[k.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = v;
    }
  }
  return node;
}

function matches(node, part) {
  const m = part.match(/^\.([^\[]+)(?:\[([^\]=]+)="([^"]*)"\])?$/);
  if (!m) return false;
  if (!(node.className || '').split(/\s+/).includes(m[1])) return false;
  if (m[2]) {
    const prop = m[2].slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
    if (String(node.dataset[prop] || '') !== m[3]) return false;
  }
  return true;
}

function queryAll(root, sel) {
  const parts = sel.split(' ');
  const out = [];
  (function walk(node) {
    for (const child of node.children) {
      if (parts.length === 1 && matches(child, parts[0])) out.push(child);
      walk(child);
    }
  })(root);
  return out;
}

function toolCard(callId, round) {
  return makeNode('div', 'tool-block', { 'data-tool-call-id': callId, 'data-round': String(round) });
}

function textBlock(text, round) {
  const node = makeNode('div', 'msg-assistant-body', { 'data-round': String(round) });
  node.textContent = text;
  return node;
}

function thinkBlock(text, round) {
  const wrap = makeNode('div', 'think-block', { 'data-round': String(round) });
  const content = makeNode('div', 'think-content');
  content.textContent = text;
  wrap.appendChild(content);
  return wrap;
}

test('按 callId 找到历史工具卡，但排除管线自己的新容器', () => {
  const chatInner = makeNode('div', 'chat-inner');
  const history = toolCard('call_1', 2);
  chatInner.appendChild(history);
  const msg = makeNode('div', 'msg');
  chatInner.appendChild(msg);
  const own = toolCard('call_9', 2);
  msg.appendChild(own);
  assert.equal(Dedup.findHistoryToolWrap(chatInner, msg, 'call_1'), history);
  assert.equal(Dedup.findHistoryToolWrap(chatInner, msg, 'call_9'), null);
  assert.equal(Dedup.findHistoryToolWrap(chatInner, msg, 'call_missing'), null);
  assert.equal(Dedup.findHistoryToolWrap(chatInner, msg, ''), null);
  assert.equal(Dedup.findHistoryToolWrap(null, msg, 'call_1'), null);
});

test('同轮历史正文按 DOM 顺序收集，排除管线容器、无轮次返回空', () => {
  const chatInner = makeNode('div', 'chat-inner');
  const first = textBlock('第一段', 2);
  const otherRound = textBlock('其它轮', 1);
  chatInner.appendChild(first);
  chatInner.appendChild(otherRound);
  const msg = makeNode('div', 'msg');
  chatInner.appendChild(msg);
  const live = textBlock('实时块', 2);
  msg.appendChild(live);
  const second = textBlock('第二段', 2);
  chatInner.appendChild(second);
  assert.deepEqual(Dedup.collectHistoryTexts(chatInner, msg, 2), [first, second]);
  assert.deepEqual(Dedup.collectHistoryTexts(chatInner, msg, 1), [otherRound]);
  assert.deepEqual(Dedup.collectHistoryTexts(chatInner, msg, null), []);
});

test('同轮历史思考返回内容区元素', () => {
  const chatInner = makeNode('div', 'chat-inner');
  const wrap = thinkBlock('让我想想', 2);
  chatInner.appendChild(wrap);
  const msg = makeNode('div', 'msg');
  chatInner.appendChild(msg);
  const found = Dedup.collectHistoryThinks(chatInner, msg, 2);
  assert.equal(found.length, 1);
  assert.equal(found[0].textContent, '让我想想');
  assert.deepEqual(Dedup.collectHistoryThinks(chatInner, msg, 3), []);
});

test('源码离屏渲染后与节点文本比对（忽略首尾空白）', () => {
  const Markdown = { render: source => '<p>' + source + '</p>' };
  const el = () => {
    const node = makeNode('div', '');
    Object.defineProperty(node, 'innerHTML', {
      set(v) { node.textContent = String(v).replace(/<[^>]*>/g, ''); },
      get() { return node.textContent; },
    });
    return node;
  };
  assert.equal(Dedup.renderedSourceEquals(Markdown, el, '正在查看目录。', '正在查看目录。'), true);
  assert.equal(Dedup.renderedSourceEquals(Markdown, el, '  正在查看目录。\n', '正在查看目录。'), true);
  assert.equal(Dedup.renderedSourceEquals(Markdown, el, '第一段', '第二段'), false);
});

test('顺序匹配器命中即消费，原文重复时不互相误配', () => {
  const matcher = Dedup.createSequentialMatcher();
  const a = textBlock('好的', 2);
  const b = textBlock('好的', 2);
  const equalsGood = node => node.textContent === '好的';
  assert.equal(matcher.match([a, b], equalsGood), a);
  assert.equal(matcher.match([a, b], equalsGood), b);
  assert.equal(matcher.match([a, b], equalsGood), null);
  matcher.reset();
  assert.equal(matcher.match([a, b], equalsGood), a);
});
