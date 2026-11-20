const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { stripTypeScriptTypes } = require('node:module');
const { JSDOM } = require('jsdom');

// 回归：运行中引导消息（message_injected）消费时就地挂操作行与时间戳。
// 历史缺陷：实时消费分支 round/ts 恒传 null → appendUserMessage 不挂
// 「删除/插入对话/复制/编辑」操作行，也没有时间戳，必须刷新页面随历史
// 回放才出现。修复：后端帧携带 round/timestamp，前端透传给渲染层。
function harness(sessionId = 's') {
  const source = stripTypeScriptTypes(fs.readFileSync(path.join(__dirname, '../src/app/stream_pipeline.ts'), 'utf8'));
  const start = source.indexOf('  function createStreamPipeline(');
  const end = source.indexOf('\n  App.createStreamPipeline =', start);
  const functionEnd = source.lastIndexOf('\n  }', end) + 4;
  const dom = new JSDOM('<div id="chatInner"></div>');
  const { document } = dom.window;
  const chatInner = document.querySelector('#chatInner');
  const msg = document.createElement('div');
  msg.className = 'msg';
  chatInner.appendChild(msg);
  const appended = [];
  const cleared = [];
  const context = {
    Date,
    setTimeout: () => 0,
    clearTimeout: () => {},
    state: { sessionId },
    chatInner,
    stickToBottom: () => {},
    el: (tag, cls, text) => {
      const n = document.createElement(tag);
      if (cls) n.className = cls;
      if (text != null) n.textContent = text;
      return n;
    },
    Markdown: { render: t => '<p>' + t + '</p>' },
    FormatUtils: { roundSummaryText: () => '', prettyJson: v => JSON.stringify(v), fmtTime: () => '' },
    StreamReplayDedup: undefined,
    App: {
      roundFold: { fmtDuration: () => '' },
      processGroups: { sync() {}, setTimestamp() {} },
      rebuildQnav() {}, updateCodeblockCopyButtons() {},
      clearInjectedPending(sid, mid) { cleared.push([sid, mid]); },
      appendUserMessage(...args) { appended.push(args); return document.createElement('div'); },
    },
  };
  vm.createContext(context);
  vm.runInContext(source.slice(start, functionEnd), context);
  const pipe = context.createStreamPipeline(msg, 's', {});
  return { dom, msg, pipe, appended, cleared };
}

const TS = '2026-11-18 14:04:31';

test('新后端帧：round/timestamp 透传给 appendUserMessage（就地挂操作行+时间戳）', t => {
  const h = harness();
  t.after(() => h.dom.window.close());
  h.pipe.handle({ data: { event: 'message_injected', content: '中途引导', round: 3,
    timestamp: TS, message_id: 'm1', quotes: [{ text: '引用' }] } });
  assert.equal(h.appended.length, 1);
  const [content, ts, sid, docs, container, round, quotes] = h.appended[0];
  assert.equal(content, '中途引导');
  assert.equal(ts, TS, '时间戳应透传（实时气泡与刷新回放一致）');
  assert.equal(sid, 's');
  assert.equal(docs, null);
  assert.equal(container, h.msg, '气泡嵌入当前流式回答容器');
  assert.equal(round, 3, '轮次号应透传（触发操作行挂载）');
  assert.deepEqual(quotes, [{ text: '引用' }]);
  assert.deepEqual(h.cleared, [['s', 'm1']], '待注入提示按 id 清除');
});

test('新后端帧：event_index 透传为第 8 参（引导消息单条删除定位）', t => {
  const h = harness();
  t.after(() => h.dom.window.close());
  h.pipe.handle({ data: { event: 'message_injected', content: '中途引导', round: 3,
    timestamp: TS, message_id: 'mx', event_index: 5 } });
  assert.equal(h.appended[0][7], 5);
  // 缺字段/非法值 → null（渲染层不打 midRound 标记，向后兼容）
  h.pipe.handle({ data: { event: 'message_injected', text: 'x', round: 3, timestamp: TS } });
  assert.equal(h.appended[1][7], null);
  h.pipe.handle({ data: { event: 'message_injected', text: 'x', round: 3, timestamp: TS, event_index: -2 } });
  assert.equal(h.appended[2][7], null);
});

test('旧后端帧（无 round/timestamp）：保持旧行为 round=null/ts=null', t => {
  const h = harness();
  t.after(() => h.dom.window.close());
  h.pipe.handle({ data: { event: 'message_injected', text: '旧版引导', message_id: 'm2' } });
  assert.equal(h.appended.length, 1);
  const [content, ts, , , , round] = h.appended[0];
  assert.equal(content, '旧版引导', '无 content 时回退 text');
  assert.equal(ts, null);
  assert.equal(round, null, '旧后端不挂入口，等收尾重载补挂（向后兼容）');
});

test('非法 round/空 timestamp 一律拒收（宁缺毋滥）', t => {
  const h = harness();
  t.after(() => h.dom.window.close());
  for (const bad of [0, -1, '3', null, true]) {
    h.pipe.handle({ data: { event: 'message_injected', text: 'x', round: bad, timestamp: '' } });
  }
  assert.equal(h.appended.length, 5);
  for (const [, ts, , , , round] of h.appended) {
    assert.equal(round, null);
    assert.equal(ts, null);
  }
});

test('后台会话：只清提示不插气泡（切回由历史回放显示）', t => {
  const h = harness('other-session');
  t.after(() => h.dom.window.close());
  h.pipe.handle({ data: { event: 'message_injected', content: '他人引导', round: 2,
    timestamp: TS, message_id: 'm3' } });
  assert.equal(h.appended.length, 0, '不向当前会话插入他人引导');
  assert.deepEqual(h.cleared, [['s', 'm3']]);
});

test('渲染层守护：round != null 分支挂 _editData + attachUserEditAction + 时间戳', () => {
  // appendUserMessage 的挂载分支是操作行出现的唯一入口（历史回放同款）；
  // 守护其不被回退成"仅历史挂、实时不挂"。
  const src = fs.readFileSync(path.join(__dirname, '../src/app/messages_render.ts'), 'utf8');
  const fn = src.slice(src.indexOf('function appendUserMessage('), src.indexOf('// ---------- 导出'));
  assert.match(fn, /if \(ts\) msg\.appendChild\(el\("div", "msg-time"/);
  assert.match(fn, /if \(round != null\)/);
  assert.match(fn, /msg\._editData = \{ content: content, round: round, quotes: quotes \|\| \[\] \}/);
  assert.match(fn, /App\.attachUserEditAction\(msg\)/);
});
