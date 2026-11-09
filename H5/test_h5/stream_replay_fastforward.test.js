const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { stripTypeScriptTypes } = require('node:module');
const StreamReplayDedup = require('../js/stream_replay_dedup.js');

function makeNode(tag, cls, text) {
  const node = {
    tagName: tag, className: cls || '', children: [], parentNode: null,
    dataset: {}, textContent: text || '', hidden: false,
    classList: null,
    appendChild(c) {
      if (c.parentNode) c.parentNode.removeChild(c);
      c.parentNode = node; node.children.push(c); return c;
    },
    insertBefore(c, ref) {
      if (c.parentNode) c.parentNode.removeChild(c);
      c.parentNode = node;
      const at = ref ? node.children.indexOf(ref) : -1;
      if (at < 0) node.children.push(c); else node.children.splice(at, 0, c);
      return c;
    },
    removeChild(c) {
      const at = node.children.indexOf(c);
      if (at >= 0) node.children.splice(at, 1);
      c.parentNode = null; return c;
    },
    remove() { if (node.parentNode) node.parentNode.removeChild(node); },
    querySelector(sel) { return queryAll(node, sel)[0] || null; },
    querySelectorAll(sel) { return queryAll(node, sel); },
    matches(sel) { return matches(node, sel); },
  };
  node.classList = {
    add: (...cs) => { node.className = Array.from(new Set((node.className + ' ' + cs.join(' ')).split(/\s+/).filter(Boolean))).join(' '); },
    remove: (...cs) => { node.className = node.className.split(/\s+/).filter(c => c && !cs.includes(c)).join(' '); },
    toggle: c => { node.classList.contains(c) ? node.classList.remove(c) : node.classList.add(c); },
    contains: c => node.className.split(/\s+/).includes(c),
  };
  Object.defineProperty(node, 'innerHTML', {
    configurable: true,
    get() { return node.textContent; },
    set(v) { node.textContent = String(v).replace(/<[^>]*>/g, ''); },
  });
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

// mock 节点的 textContent 不自动聚合子节点：递归收集等价于真 DOM 的取值
function textOf(node) {
  let out = node.textContent || '';
  for (const child of node.children || []) out += textOf(child);
  return out;
}

function harness() {
  const source = stripTypeScriptTypes(fs.readFileSync(path.join(__dirname, '../src/app/stream_pipeline.ts'), 'utf8'));
  const start = source.indexOf('  function createStreamPipeline(');
  const end = source.indexOf('\n  App.createStreamPipeline =', start);
  const functionEnd = source.lastIndexOf('\n  }', end) + 4;
  const chatInner = makeNode('div', 'chat-inner');
  const msg = makeNode('div', 'msg');
  chatInner.appendChild(msg);
  const counts = {
    render: 0, stick: 0, sync: 0, nav: 0,
    addDelta: 0, setInputText: 0, setInput: 0, setName: 0, bind: 0, begin: 0,
  };
  const builtTools = [];
  const context = {
    state: { sessionId: 's' },
    chatInner,
    StreamReplayDedup,
    stickToBottom() { counts.stick++; },
    pickTitlePreview() { return ''; },
    setTimeout() { return 1; }, clearTimeout() {}, FILE_BADGE_TOOLS: [],
    Markdown: { render: text => { counts.render++; return '<p>' + text + '</p>'; } },
    el(tag, cls, text) { return makeNode(tag, cls, text); },
    FormatUtils: {
      roundSummaryText: () => '',
      prettyJson: v => JSON.stringify(v),
      fmtTime: () => '',
    },
    App: {
      buildThinkBlock() {
        const wrap = makeNode('div', 'think-block');
        const content = makeNode('div', 'think-content');
        wrap.appendChild(content);
        const ui = { wrap, text: '',
          streaming() {}, setText(t) { ui.text = t; content.textContent = t; },
          add(t) { ui.text += t; content.textContent = ui.text; }, done() {} };
        return ui;
      },
      buildToolBlock(name) {
        const wrap = makeNode('div', 'tool-block');
        const ui = { wrap, name,
          setName(n) { counts.setName++; ui.name = n; },
          setInput() { counts.setInput++; },
          setOutput() {}, finish() {}, executing() {},
          beginStream() { counts.begin++; },
          addInputDelta() { counts.addDelta++; },
          setInputText() { counts.setInputText++; },
          bindPermission() { counts.bind++; } };
        builtTools.push(ui);
        return ui;
      },
      renderPreservingWidgets(node, html) { node.innerHTML = html; },
      highlightCodeBlocks() {}, updateCodeblockCopyButtons() {},
      refreshContextTokenStats() {},
      applyToolResult(block, name, resultText) { block.setOutput(resultText); },
      scheduleContextTokenStatsRefresh() {}, clearInjectedPending() {},
      rebuildQnav() { counts.nav++; },
      attachLiveRoundEntry() {},
      roundFold: { fmtDuration(ms) { return Math.max(0, Math.round(ms / 1000)) + 's'; } },
      processGroups: { sync() { counts.sync++; }, setTimestamp() {} },
    },
  };
  vm.createContext(context);
  vm.runInContext(source.slice(start, functionEnd), context);
  const activeStream = { round: 2 };
  const pipe = context.createStreamPipeline(msg, 's', activeStream);
  return {
    pipe, chatInner, msg, activeStream, context, counts, builtTools,
    send: data => pipe.handle({ data }),
  };
}

function contentFrames(n, ch) {
  const frames = [];
  for (let i = 0; i < n; i++) frames.push({ content: ch || 'x' });
  return frames;
}

test('无本轮历史节点时跳过去重匹配（直接落定）', () => {
  const h = harness();
  h.pipe.setReplayWindow(true);
  contentFrames(3, 'y').forEach(h.send);
  const bodies = h.msg.children.filter(n => (n.className || '').includes('msg-assistant-body'));
  assert.equal(bodies.length, 1);
  h.pipe.setReplayWindow(false);
  assert.match(bodies[0].textContent, /yyy/);
});

test('回放窗口内 N 帧正文只渲染一次（与帧数无关）', () => {
  const h = harness();
  h.pipe.setReplayWindow(true);
  contentFrames(50).forEach(h.send);
  h.send({ finish_reason: 'stop' });
  h.pipe.setReplayWindow(false);
  assert.equal(h.counts.render, 1);
  assert.match(textOf(h.msg), /x{50}/);

  // 帧数翻倍，渲染次数不变
  const h2 = harness();
  h2.pipe.setReplayWindow(true);
  contentFrames(200).forEach(h2.send);
  h2.send({ finish_reason: 'stop' });
  h2.pipe.setReplayWindow(false);
  assert.equal(h2.counts.render, 1);
});

test('回放窗口内工具参数分片不逐帧刷 UI，关闭时一次落定', () => {
  const h = harness();
  h.pipe.setReplayWindow(true);
  for (let i = 0; i < 20; i++) {
    h.send({ tool_calls: [{ index: 0, id: 'c1', function: { name: i ? undefined : 'read_file', arguments: 'a' } }] });
  }
  assert.equal(h.counts.addDelta, 0);
  assert.equal(h.counts.setInputText, 0);
  assert.equal(h.counts.begin, 0);
  h.pipe.setReplayWindow(false);
  assert.equal(h.counts.setName, 1);
  assert.equal(h.counts.bind, 1);
  assert.equal(h.counts.setInputText, 1);
  // 关闭后恢复实时行为：新分片照常流式追加
  h.send({ tool_calls: [{ index: 0, id: 'c1', function: { arguments: 'b' } }] });
  assert.equal(h.counts.addDelta, 1);
  assert.equal(h.counts.begin, 1);
});

test('窗口内完成的工具：结果帧整体覆盖，不多一次输入渲染', () => {
  const h = harness();
  h.pipe.setReplayWindow(true);
  for (let i = 0; i < 10; i++) {
    h.send({ tool_calls: [{ index: 0, id: 'c1', function: { name: 'read_file', arguments: 'a' } }] });
  }
  h.send({ tool_return: { function_name: 'read_file', tool_call_id: 'c1', arguments: { p: 1 }, result: 'ok' } });
  h.pipe.setReplayWindow(false);
  assert.equal(h.counts.addDelta, 0);
  assert.equal(h.counts.setInputText, 0); // 已完成：关闭落定跳过，结果帧已整体写入
  assert.equal(h.counts.setInput, 1);
});

test('窗口内滚动/折叠同步/导航各只在关闭时执行一次', () => {
  const h = harness();
  h.pipe.setReplayWindow(true);
  contentFrames(10).forEach(h.send);
  for (let i = 0; i < 10; i++) {
    h.send({ tool_calls: [{ index: 0, id: 'c1', function: { arguments: 'a' } }] });
  }
  h.send({ usage: { total_tokens: 5 }, id: 'u1' });
  h.send({ round_started: { round: 2 } });
  assert.equal(h.counts.stick, 0);
  assert.equal(h.counts.sync, 0);
  assert.equal(h.counts.nav, 0);
  h.pipe.setReplayWindow(false);
  assert.equal(h.counts.stick, 1);
  assert.equal(h.counts.sync, 1);
  assert.equal(h.counts.nav, 1);
});

test('快进与实时渲染最终 DOM 一致', () => {
  const frames = [
    { reasoning_content: '想' },
    ...contentFrames(30, '正'),
    { tool_calls: [{ index: 0, id: 'c1', function: { name: 'read_file', arguments: '{"p":' } }] },
    { tool_calls: [{ index: 0, id: 'c1', function: { arguments: '1}' } }] },
    { tool_return: { function_name: 'read_file', tool_call_id: 'c1', arguments: { p: 1 }, result: '文件内容' } },
    ...contentFrames(20, '尾'),
    { finish_reason: 'stop' },
  ];
  const live = harness();
  frames.forEach(live.send);
  live.pipe.finish();
  const replay = harness();
  replay.pipe.setReplayWindow(true);
  frames.forEach(replay.send);
  replay.pipe.setReplayWindow(false);
  replay.pipe.finish();
  assert.equal(replay.msg && textOf(replay.msg), textOf(live.msg));
});

test('无轮次号回放（非 staging 路径）同样一次落定不丢字', () => {
  const h = harness();
  h.activeStream.round = null;
  h.pipe.setReplayWindow(true);
  contentFrames(30, 'y').forEach(h.send);
  assert.equal(h.counts.render, 0);
  h.pipe.setReplayWindow(false);
  assert.equal(h.counts.render, 1);
  assert.match(textOf(h.msg), /y{30}/);
});

test('直播计时行：建管线即挂载，收尾时移除', () => {
  const h = harness();
  const row = h.msg.children.find(n => (n.className || '').includes('live-round-timer'));
  assert.ok(row, '计时行已挂载');
  assert.equal(h.msg.children[0], row, '计时行在首位');
  h.pipe.finish();
  assert.equal(h.msg.children.filter(n => (n.className || '').includes('live-round-timer')).length, 0, '收尾移除');
});
