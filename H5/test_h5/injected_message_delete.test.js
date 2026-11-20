const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { JSDOM } = require('jsdom');
const historyParser = require('../js/history_parser.js');

// 引导消息删除回归：运行中插入的 user 消息点「删除」只删本条（event_index），
// 不再按 data-round 整轮删除把最初提问与回复一起删掉。

function roundLine(events) {
  return JSON.stringify({
    event: 'chat_round', question: '原始任务', status: 'done',
    started_at: '2026-11-20 12:00:00', ended_at: '2026-11-20 12:01:00', events,
  });
}

test('解析：同轮非首条用户消息标 midRound，首条提问不标', () => {
  const parsed = historyParser.parseHistory(roundLine([
    { role: 'user', content: '原始任务', timestamp: '2026-11-20 12:00:00' },
    { role: 'assistant', tool_calls: [{ id: 'c1', function: { name: 'run_command', arguments: '{}' } }] },
    { role: 'tool', tool_call_id: 'c1', tool_name: 'run_command', result: 'ok' },
    { role: 'user', content: '中途引导', timestamp: '2026-11-20 12:00:30' },
    { role: 'assistant', content: '完成', done: '[DONE]' },
  ]));
  const users = parsed.records.filter(r => r.kind === 'user');
  assert.equal(users.length, 2);
  assert.ok(!users[0].midRound, '首条提问不是引导消息');
  assert.equal(users[1].midRound, true);
  assert.equal(users[1].source_event_index, 3);
});

test('解析：内部 nudge / 停止任务不算首条提问基准之外的引导', () => {
  const parsed = historyParser.parseHistory(roundLine([
    { role: 'user', content: '原始任务' },
    { role: 'user', content: '继续', _internal: true },
    { role: 'user', content: '停止任务' },
    { role: 'assistant', content: '好', done: '[DONE]' },
  ]));
  const users = parsed.records.filter(r => r.kind === 'user');
  assert.equal(users.length, 1, '内部消息与控制消息不渲染为用户气泡');
});

test('渲染：appendUserMessage sourceEventIndex>0 打 data-mid-round，=0 不打', () => {
  const src = fs.readFileSync(path.join(__dirname, '../js/app/messages_render.js'), 'utf8');
  const fn = src.slice(src.indexOf('function appendUserMessage('), src.indexOf('// ---------- 导出'));
  const dom = new JSDOM('<div id="chatInner"></div>');
  const { document } = dom.window;
  const chatInner = document.querySelector('#chatInner');
  const attached = [];
  const context = {
    Array, Number, String, Object,
    el: (tag, cls, text) => {
      const n = document.createElement(tag);
      if (cls) n.className = cls;
      if (text != null) n.textContent = text;
      return n;
    },
    chatInner,
    FormatUtils: { fmtTime: () => '' },
    buildUserBubble: () => document.createElement('div'),
    stickToBottom: () => {},
    App: {
      setStageTimestamp: () => {},
      attachUserEditAction: m => attached.push(m),
      fitAttachmentBlockGrid: null,
    },
  };
  vm.createContext(context);
  vm.runInContext(fn, context);
  const first = context.appendUserMessage('原始任务', null, 's', null, chatInner, 1, [], 0);
  const guide = context.appendUserMessage('中途引导', null, 's', null, chatInner, 1, [], 3);
  const legacy = context.appendUserMessage('旧数据无下标', null, 's', null, chatInner, 1, [], undefined);
  assert.notEqual(first.dataset.midRound, '1');
  assert.equal(guide.dataset.midRound, '1');
  assert.equal(guide.dataset.sourceEvent, '3');
  assert.notEqual(legacy.dataset.midRound, '1', '缺事件下标的旧行为保持整轮语义');
  assert.equal(attached.length, 3);
  dom.window.close();
});

function deleteHarness(msg) {
  const src = fs.readFileSync(path.join(__dirname, '../js/app/messages_edit.js'), 'utf8');
  const start = src.indexOf('function requestDeleteRound(');
  // 只取编排函数本身：其后的 performDelete* 真实现会覆盖测试桩
  const end = src.indexOf('async function performDeleteRound(');
  const confirms = [], toasts = [], calls = [];
  const context = {
    Number, window: { confirm: () => true },
    contentToPlainText: c => typeof c === 'string' ? c : '',
    performDeleteRound: r => calls.push(['round', r]),
    performDeleteInjectedMessage: (r, i) => calls.push(['message', r, i]),
    App: {
      state: { streaming: false, sessionId: 's', streamingSession: '' },
      toast: t => toasts.push(t),
      openConfirmDialog: opts => confirms.push(opts),
    },
  };
  vm.createContext(context);
  vm.runInContext(src.slice(start, end), context);
  context.requestDeleteRound(msg);
  return { confirms, toasts, calls, context };
}

test('删除分流：引导消息确认文案为单条删除，确认后走 event_index 路径', () => {
  const msg = {
    classList: { contains: () => false },
    dataset: { midRound: '1', sourceEvent: '3' },
    _editData: { round: 2, content: '顺便清理日志' },
  };
  const h = deleteHarness(msg);
  assert.equal(h.confirms.length, 1);
  assert.equal(h.confirms[0].title, '删除引导消息');
  assert.ok(h.confirms[0].message.includes('原始提问、回复与其他内容保留'));
  h.confirms[0].onConfirm();
  assert.deepEqual(h.calls, [['message', 2, 3]]);
});

test('删除分流：普通轮首条提问仍走整轮删除（原语义不变）', () => {
  const msg = {
    classList: { contains: () => false },
    dataset: { sourceEvent: '0' },
    _editData: { round: 2, content: '原始任务' },
  };
  const h = deleteHarness(msg);
  assert.equal(h.confirms[0].title, '删除该轮对话');
  h.confirms[0].onConfirm();
  assert.deepEqual(h.calls, [['round', 2]]);
});

test('删除分流：标记异常（midRound 但下标缺失/为0）保守回退整轮语义之外不误删——按整轮提示', () => {
  const bad1 = { classList: { contains: () => false }, dataset: { midRound: '1' }, _editData: { round: 2, content: 'x' } };
  const h1 = deleteHarness(bad1);
  assert.equal(h1.confirms[0].title, '删除该轮对话');
  const bad2 = { classList: { contains: () => false }, dataset: { midRound: '1', sourceEvent: '0' }, _editData: { round: 2, content: 'x' } };
  const h2 = deleteHarness(bad2);
  assert.equal(h2.confirms[0].title, '删除该轮对话');
});

test('API 封装：deleteRounds 仅在传入合法 eventIndex 时携带 event_index', async () => {
  const src = fs.readFileSync(path.join(__dirname, '../js/api.js'), 'utf8');
  const fn = src.slice(src.indexOf('async function deleteRounds('), src.indexOf('/**', src.indexOf('async function deleteRounds(')));
  const bodies = [];
  const context = {
    Number, JSON,
    request: async (_p, opts) => { bodies.push(JSON.parse(opts.body)); return {}; },
  };
  vm.createContext(context);
  vm.runInContext(fn + '\nthis.deleteRounds = deleteRounds;', context);
  await context.deleteRounds('s', 2, { mode: 'single', eventIndex: 3 });
  await context.deleteRounds('s', 2, { mode: 'single' });
  await context.deleteRounds('s', 2, { mode: 'single', eventIndex: -1 });
  assert.equal(bodies[0].event_index, 3);
  assert.ok(!('event_index' in bodies[1]));
  assert.ok(!('event_index' in bodies[2]));
});
