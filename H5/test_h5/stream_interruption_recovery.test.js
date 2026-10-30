const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { JSDOM } = require('jsdom');

const chatSource = fs.readFileSync(path.join(__dirname, '../js/app/chat.js'), 'utf8');
const helperSource = chatSource.slice(
  chatSource.indexOf('  // 流错误的展示文案'),
  chatSource.indexOf('  async function send(direct)'));

// ---------- task_status.reconcileUncertain ----------

function statusSetup(t, streamStatusImpl) {
  const dom = new JSDOM('<div id="chat"></div><div id="taskStatus" hidden><span class="task-status-label"></span><span class="task-status-detail"></span><button hidden>重新连接</button></div>', { runScripts: 'outside-only' });
  t.after(() => dom.window.close());
  const w = dom.window;
  w.TaskState = require('../js/task_state');
  w.App = { state: { sessionId: 's', streaming: false }, toast() {} };
  w.API = { streamStatus: streamStatusImpl || (async () => ({ running: true })) };
  w.eval(fs.readFileSync(path.join(__dirname, '../js/app/task_status.js'), 'utf8'));
  return { w, status: w.App.taskStatus, host: w.document.querySelector('#taskStatus') };
}

async function makeUncertain(status) {
  const h = status.begin('s');
  status.connected(h);
  status.event(h, { data: { content: 'partial' } });
  await status.end(h, false, new Error('network error'));
  assert.equal(status.isUncertain('s'), true);
  return h;
}

test('reconcileUncertain：后端无任务时收尾为已结束，返回可发送', async t => {
  const x = statusSetup(t, async () => ({ running: false }));
  await makeUncertain(x.status);
  assert.equal(await x.status.reconcileUncertain('s'), true);
  assert.equal(x.status.isUncertain('s'), false);
  assert.equal(x.host.dataset.phase, 'ended');
});

test('reconcileUncertain：任务仍在运行时保持连接中断，返回不可发送', async t => {
  const x = statusSetup(t, async () => ({ running: true }));
  await makeUncertain(x.status);
  assert.equal(await x.status.reconcileUncertain('s'), false);
  assert.equal(x.status.isUncertain('s'), true);
  assert.equal(x.host.dataset.phase, 'disconnected');
});

test('reconcileUncertain：非不确定态不查询后端，直接返回可发送', async t => {
  let calls = 0;
  const x = statusSetup(t, async () => { calls++; return { running: true }; });
  const h = x.status.begin('s');
  x.status.connected(h);
  await x.status.end(h, true);
  assert.equal(await x.status.reconcileUncertain('s'), true);
  assert.equal(calls, 0);
});

test('reconcileUncertain：后端不可用时抛错，保持不确定态', async t => {
  const x = statusSetup(t, async () => { throw new Error('fetch failed'); });
  await makeUncertain(x.status);
  await assert.rejects(() => x.status.reconcileUncertain('s'), /fetch failed/);
  assert.equal(x.status.isUncertain('s'), true);
});

// ---------- chat.js ensureSendable（发送前自动确认） ----------

function sendableSetup(reconcileImpl) {
  const calls = { toasts: [], attached: [], opened: [] };
  const context = {
    state: { sessionId: 's' },
    toast: m => calls.toasts.push(m),
    App: {
      taskStatus: {
        isUncertain: () => true,
        reconcileUncertain: reconcileImpl,
      },
      attachStreamSession: async sid => calls.attached.push(sid),
      openSession: async sid => calls.opened.push(sid),
    },
  };
  vm.createContext(context);
  vm.runInContext(helperSource, context);
  return { context, calls };
}

test('发送前：后端无任务则自动收尾并重载，放行本次发送', async () => {
  const x = sendableSetup(async () => true);
  assert.equal(await x.context.ensureSendable(), true);
  assert.deepEqual(x.calls.opened, ['s']);
  assert.deepEqual(x.calls.attached, []);
  assert.deepEqual(x.calls.toasts, []);
});

test('发送前：任务仍在运行则自动重连并拦截，输入保留', async () => {
  const x = sendableSetup(async () => false);
  assert.equal(await x.context.ensureSendable(), false);
  assert.deepEqual(x.calls.attached, ['s']);
  assert.deepEqual(x.calls.opened, []);
  assert.match(x.calls.toasts[0], /仍在运行.*重新连接/);
});

test('发送前：后端不可用则提示检查后端并拦截', async () => {
  const x = sendableSetup(async () => { throw new Error('Failed to fetch'); });
  assert.equal(await x.context.ensureSendable(), false);
  assert.deepEqual(x.calls.attached, []);
  assert.deepEqual(x.calls.opened, []);
  assert.match(x.calls.toasts[0], /后端服务不可用/);
});

test('发送前：无不确定态时零查询直接放行', async () => {
  let calls = 0;
  const context = {
    state: { sessionId: 's' }, toast() {},
    App: { taskStatus: { isUncertain: () => false, reconcileUncertain: async () => { calls++; return true; } } },
  };
  vm.createContext(context);
  vm.runInContext(helperSource, context);
  assert.equal(await context.ensureSendable(), true);
  assert.equal(calls, 0);
});

// ---------- chat.js streamErrorText（网络中断提示） ----------

test('网络层中断提示检查后端，HTTP 错误保持原文', () => {
  const context = {};
  vm.createContext(context);
  vm.runInContext(helperSource, context);
  assert.match(context.streamErrorText(new Error('network error')), /后端服务可能已停止.*network error/);
  assert.equal(context.streamErrorText(Object.assign(new Error('bad'), { status: 500 })), 'bad');
});

// ---------- 附接流：network error 后即时确认 ----------

function attachSetup(t, { chatImpl, statusImpl }) {
  const dom = new JSDOM('<div id="chat"></div><div id="taskStatus" hidden><span class="task-status-label"></span><span class="task-status-detail"></span><button hidden>重新连接</button></div>', { runScripts: 'outside-only' });
  t.after(() => dom.window.close());
  const w = dom.window;
  w.TaskState = require('../js/task_state');
  const calls = { opened: [], flushed: [] };
  // 不确定态退避定时器需手动驱动，见下方的 setTimeout mock。
  const held = [];
  w.App = {
    state: { sessionId: 's', streaming: false },
    toast() {},
    attachStreamSession: async () => {},
    openSession: async sid => calls.opened.push(sid),
  };
  w.API = { streamStatus: statusImpl, chatStream: chatImpl };
  w.eval(fs.readFileSync(path.join(__dirname, '../js/app/task_status.js'), 'utf8'));
  const state = w.App.state;
  Object.assign(w, {
    state, chatInner: w.document.querySelector('#chat'), AbortController, stickToBottom() {}, setEmpty() {},
    el(tag, cls, text) { const n = w.document.createElement(tag); n.className = cls || ''; n.textContent = text || ''; return n; },
    createStreamPipeline(msg, sid, active) {
      return { handle: evt => w.App.taskStatus.event(active.taskHandle, evt), finish: () => ({ empty: false }) };
    },
  });
  Object.assign(w.App, {
    refreshComposerButtons: () => w.App.taskStatus.refresh(), refreshContextTokenStats() {},
    refreshSessionUsage: async () => true, clearContextStatsTimer() {}, scheduleContextTokenStatsRefresh() {},
    flushPendingMessages: sid => calls.flushed.push(sid),
  });
  w.setTimeout = (fn, ms) => { if (!ms) { fn(); return 1; } held.push({ fn, ms }); return held.length + 100; };
  w.clearTimeout = (id) => { if (id > 100) delete held[id - 101]; };
  w.eval(helperSource);
  const start = chatSource.indexOf('  async function attachStreamSession(');
  w.eval(chatSource.slice(start, chatSource.indexOf('  /** 打开会话后', start)) + '\nwindow.attachForTest=attachStreamSession;');
  return { w, calls, host: w.document.querySelector('#taskStatus'), status: w.App.taskStatus, held };
}

test('附接流 network error 且后端无任务：自动收尾并重载，不再卡运行中', async t => {
  const x = attachSetup(t, {
    chatImpl: async () => { throw new Error('network error'); },
    statusImpl: async () => ({ running: false }),
  });
  await x.w.attachForTest('s');
  assert.equal(x.status.isUncertain('s'), false);
  assert.equal(x.host.dataset.phase, 'ended');
  assert.deepEqual(x.calls.opened, ['s']);
  const notice = x.w.document.querySelector('#chat .notice-bar');
  assert.match(notice.textContent, /后端服务可能已停止/);
});

test('附接流 network error 但后端不可达：保持连接中断，不重载', async t => {
  const x = attachSetup(t, {
    chatImpl: async () => { throw new Error('network error'); },
    statusImpl: async () => { throw new Error('Failed to fetch'); },
  });
  await x.w.attachForTest('s');
  assert.equal(x.status.isUncertain('s'), true);
  assert.equal(x.host.dataset.phase, 'disconnected');
  assert.deepEqual(x.calls.opened, []);
  assert.deepEqual(x.held.filter(Boolean).map(o => o.ms), [5000]);
});

// ---------- 停止按钮：后端不可用时的本地兜底 ----------

function stopSetup({ activeStream, stopImpl }) {
  const calls = { stops: [], aborts: 0, states: [], toasts: [] };
  const state = { sessionId: 'visible', activeStream, abort: activeStream ? { abort() { calls.aborts++; } } : null };
  const start = chatSource.indexOf('  stopBtn.addEventListener(');
  const end = chatSource.indexOf('\n  });', start) + 6;
  let handler;
  const context = {
    state, stopBtn: { addEventListener(name, fn) { handler = fn; } }, toast: m => calls.toasts.push(m),
    App: {
      refreshComposerButtons() {},
      taskStatus: {
        stopping: s => calls.states.push(['stopping', s]),
        stopped: s => calls.states.push(['stopped', s]),
        stopFailed: s => calls.states.push(['failed', s]),
      },
    },
    API: { stopChat: stopImpl },
  };
  vm.runInNewContext(chatSource.slice(start, end), context);
  return { calls, state, run: () => handler() };
}

test('无本地流且后端不可用：停止按钮本地收尾，不卡运行中', async () => {
  const x = stopSetup({
    activeStream: null,
    stopImpl: async sid => { throw new Error('Failed to fetch'); },
  });
  await x.run();
  assert.deepEqual(x.calls.states, [['stopping', 'visible'], ['stopped', 'visible']]);
  assert.match(x.calls.toasts[0], /本地停止/);
  assert.equal(x.state.suppressFlushOnce, true);
});

test('有本地流时停止失败仍保持监听，可重试', async () => {
  const x = stopSetup({
    activeStream: { sessionId: 'visible' },
    stopImpl: async () => { throw new Error('Failed to fetch'); },
  });
  await x.run();
  assert.deepEqual(x.calls.states, [['stopping', 'visible'], ['failed', 'visible']]);
  assert.match(x.calls.toasts[0], /停止失败/);
  assert.equal(x.state.suppressFlushOnce, false);
  assert.equal(x.calls.aborts, 0);
});
