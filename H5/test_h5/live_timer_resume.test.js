const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { stripTypeScriptTypes } = require('node:module');
const { JSDOM } = require('jsdom');

// 直播计时行（折叠出现前的“● 用时 Xs”）：起点现读 + 游离续跳。
// 回归：切会话/刷新切回后，折叠前的计时不得从切回时刻重计，也不得冻结。
function harness() {
  const source = stripTypeScriptTypes(fs.readFileSync(path.join(__dirname, '../src/app/stream_pipeline.ts'), 'utf8'));
  const start = source.indexOf('  function createStreamPipeline(');
  const end = source.indexOf('\n  App.createStreamPipeline =', start);
  const functionEnd = source.lastIndexOf('\n  }', end) + 4;
  const dom = new JSDOM('<div id="chatInner"></div>');
  const { window } = dom;
  const { document } = window;
  const chatInner = document.querySelector('#chatInner');
  const msg = document.createElement('div');
  msg.className = 'msg';
  chatInner.appendChild(msg);
  // 可控时钟 + 手动定时器队列
  let now = 1_700_000_000_000;
  const RealDate = Date;
  function FakeDate(...args) {
    if (!(this instanceof FakeDate)) return new FakeDate(...args);
    this._t = args.length ? new RealDate(...args).getTime() : now;
  }
  FakeDate.now = () => now;
  FakeDate.parse = RealDate.parse;
  FakeDate.prototype.toISOString = function () { return new RealDate(this._t).toISOString(); };
  let timerSeq = 0;
  let timers = [];
  const context = {
    Date: FakeDate,
    setTimeout: (fn) => { timers.push({ id: ++timerSeq, fn }); return timerSeq; },
    clearTimeout: (id) => { timers = timers.filter(t => t.id !== id); },
    state: { sessionId: 's' },
    chatInner,
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
      roundFold: { fmtDuration: ms => (ms > 0 ? Math.max(1, Math.round(ms / 1000)) + 's' : '') },
      processGroups: { sync() {}, setTimestamp() {} },
      rebuildQnav() {}, updateCodeblockCopyButtons() {}, clearInjectedPending() {},
    },
  };
  vm.createContext(context);
  vm.runInContext(source.slice(start, functionEnd), context);
  const activeStream = {};
  const pipe = context.createStreamPipeline(msg, 's', activeStream);
  const row = () => msg.querySelector('.live-round-timer');
  const text = () => { const r = row(); return r ? r.querySelector('.live-round-timer-text').textContent : null; };
  return {
    dom, document, chatInner, msg, activeStream, pipe,
    recreate: (stream) => context.createStreamPipeline(msg, 's', stream || {}),
    row, text,
    setNow: v => { now = v; },
    getNow: () => now,
    pendingTicks: () => timers.length,
    // 执行最早的一次 paint（只取计时循环的回调：按注册顺序取队首）
    tick: () => { const t = timers.shift(); if (t) t.fn(); },
    drainTicks: n => { for (let i = 0; i < n && timers.length; i++) timers.shift().fn(); },
  };
}

test('管线创建即落盘本轮起点，复用容器时不覆盖', t => {
  const h = harness();
  t.after(() => h.dom.window.close());
  assert.ok(h.row(), '应建计时行');
  assert.equal(typeof h.msg._foldRoundStartedAt, 'number');
  assert.equal(h.msg._foldRoundStartedAt, h.getNow());
  // 续跑复用同一容器：旧起点保留（不被新管线创建时间覆盖）
  h.msg._foldRoundStartedAt = h.getNow() - 120000;
  h.pipe.finish();
  assert.equal(h.msg.querySelector('.live-round-timer'), null);
  h.setNow(h.getNow() + 60000);
  h.recreate({});
  assert.equal(h.msg._foldRoundStartedAt, h.getNow() - 180000, '复用容器时沿用旧起点');
});

test('附接 marker 晚到：起点回填后计时从真实起点算，不从附接时刻重计', t => {
  const h = harness();
  t.after(() => h.dom.window.close());
  const attachedAt = h.getNow();
  assert.equal(h.text(), '用时 0s');
  // 5 秒后 marker 到达，回填 2 分钟前的真实起点（后端权威墙钟）
  h.setNow(attachedAt + 5000);
  h.msg._foldRoundStartedAt = attachedAt - 120000;
  h.tick();
  assert.equal(h.text(), '用时 125s');
});

test('切会话游离：计时循环不停止，挂回即是正确读数（不冻结不重计）', t => {
  const h = harness();
  t.after(() => h.dom.window.close());
  const t0 = h.getNow();
  h.setNow(t0 + 3000);
  h.tick();
  assert.equal(h.text(), '用时 3s');
  // 切走：msg 游离（行仍在 msg 内）
  h.msg.remove();
  assert.equal(h.row().isConnected, false);
  assert.equal(h.row().parentNode, h.msg);
  // 游离期间时间流逝：循环应继续（有新的 tick 被调度），读数继续走
  const pendingBefore = h.pendingTicks();
  assert.ok(pendingBefore > 0, '游离前应有待执行的 tick');
  h.setNow(t0 + 8000);
  h.tick();
  assert.ok(h.pendingTicks() > 0, '游离期间循环不停止（行仍在 msg 内）');
  // 切回：挂回后读数正确（从原始起点算，不是从切回时刻重计）
  h.chatInner.appendChild(h.msg);
  h.setNow(t0 + 10000);
  h.tick();
  assert.equal(h.text(), '用时 10s');
});

test('折叠接管摘掉计时行后停跳；收尾后停跳', t => {
  const h = harness();
  t.after(() => h.dom.window.close());
  h.row().remove(); // 模拟 round_fold 接管移除
  h.drainTicks(5);
  assert.equal(h.pendingTicks(), 0, '行被摘掉后不再调度');
  // 收尾路径：重建一行再 finish
  const h2 = harness();
  t.after(() => h2.dom.window.close());
  h2.pipe.finish();
  assert.equal(h2.msg.querySelector('.live-round-timer'), null, '收尾移除计时行');
  assert.equal(h2.pendingTicks(), 0, '收尾后无待执行 tick');
});

test('未来起点（时钟偏斜）回退本地起点，不显示负数', t => {
  const h = harness();
  t.after(() => h.dom.window.close());
  h.msg._foldRoundStartedAt = h.getNow() + 60000;
  h.setNow(h.getNow() + 2000);
  h.tick();
  assert.equal(h.text(), '用时 2s');
});
