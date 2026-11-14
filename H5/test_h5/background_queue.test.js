const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { stripTypeScriptTypes } = require('node:module');

// 加载 composer 的后台派发块 + 墓碑助手（不依赖完整 DOM）
function setup(options = {}) {
  const calls = [];
  const store = {};
  const state = {
    sessionId: 'B', streaming: false, streamingSession: null,
    pendingQueue: [], steerMessage: null, injectedPending: [],
    pendingAskQuestions: null, manualCompactRunning: false, manualCompactSession: null,
    suppressFlushSession: null,
  };
  const context = {
    state,
    renderPendingOutbox() {},
    toast: m => calls.push(['toast', m]),
    sessionStorage: {
      getItem: k => (k in store ? store[k] : null),
      setItem: (k, v) => { store[k] = String(v); },
    },
    setTimeout: (fn, ms) => setTimeout(fn, ms), // 真实定时器：链式派发在 finally 释放守卫后执行
    App: {
      renderPendingOutbox() {},
      saveOutboxEntry: (s, k, m) => { calls.push(['save', s, k, m.id]); return Promise.resolve(); },
      removeOutboxEntry: (s, id) => { calls.push(['remove', s, id]); return Promise.resolve(); },
      sendBackground: async (s, m) => {
        calls.push(['bg', s, m.id]);
        return options.result ? options.result(s, m) : { started: true, completed: true };
      },
      send: async (m) => { calls.push(['send', m.id]); },
    },
  };
  vm.createContext(context);
  const src = stripTypeScriptTypes(fs.readFileSync(path.join(__dirname, '../src/app/composer.ts'), 'utf8'));
  const start = src.indexOf('  // 后台派发进行中登记');
  const end = src.indexOf('  // ---------- 弹层锚定工具');
  vm.runInContext(src.slice(start, end), context);
  return { context, state, calls };
}
function q(id, sid = 'A') { return { id, sessionId: sid, text: 't-' + id }; }

test('切走后 flush 走后台派发：取走队列、删侧车、记墓碑', async () => {
  const { state, calls, context } = setup();
  state.pendingQueue.push(q('m1'));
  await context.flushBackground('A');
  assert.deepEqual(calls.filter(c => c[0] === 'bg'), [['bg', 'A', 'm1']]);
  assert.deepEqual(calls.filter(c => c[0] === 'remove'), [['remove', 'A', 'm1']]);
  assert.equal(state.pendingQueue.length, 0);
});

test('后台未发起（started=false）放回队列并取消墓碑', async () => {
  const { state, context } = setup({ result: () => ({ started: false }) });
  state.pendingQueue.push(q('m1'));
  await context.flushBackground('A');
  assert.equal(state.pendingQueue.length, 1);
  assert.equal(state.pendingQueue[0].id, 'm1');
});

test('已投递但连接中断（started+error）不放回，避免重复发送', async () => {
  const { state, context } = setup({ result: () => ({ started: true, error: new Error('net') }) });
  state.pendingQueue.push(q('m1'));
  await context.flushBackground('A');
  assert.equal(state.pendingQueue.length, 0); // 不恢复
});

test('完成后链式派发下一条（仍切走）', async () => {
  const { state, calls, context } = setup();
  state.pendingQueue.push(q('m1'), q('m2'));
  await context.flushBackground('A');
  await new Promise(r => setTimeout(r, 30)); // 等链式派发（finally 释放守卫后）
  assert.deepEqual(calls.filter(c => c[0] === 'bg').map(c => c[2]), ['m1', 'm2']);
  assert.equal(state.pendingQueue.length, 0);
});

test('已切回当前会话时后台派发不执行（交给可见派发）', async () => {
  const { state, calls, context } = setup();
  state.sessionId = 'A';
  state.pendingQueue.push(q('m1'));
  await context.flushBackground('A');
  assert.equal(calls.filter(c => c[0] === 'bg').length, 0);
  assert.equal(state.pendingQueue.length, 1);
});

test('有未答提问时后台派发不抢占', async () => {
  const { state, calls, context } = setup();
  state.pendingAskQuestions = [{}];
  state.pendingQueue.push(q('m1'));
  await context.flushBackground('A');
  assert.equal(calls.filter(c => c[0] === 'bg').length, 0);
  assert.equal(state.pendingQueue.length, 1);
});

test('引导优先于队列，且引导未发起时放回引导槽', async () => {
  const { state, context } = setup({ result: () => ({ started: false }) });
  state.steerMessage = q('s1');
  state.pendingQueue.push(q('m1'));
  await context.flushBackground('A');
  assert.equal(state.steerMessage.id, 's1'); // 放回引导槽
  assert.equal(state.pendingQueue.length, 1); // 队列未动
});
