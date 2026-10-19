const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');

const settle = () => new Promise(resolve => setImmediate(resolve));
function setup(taskState = 'completed') {
  const dom = new JSDOM('<body><div id="chatInner"><div class="round-usage" data-round="1">旧统计</div></div><div id="taskBudgetBanner" hidden></div><button id="taskCostsItem"></button><button id="tokenTotal"></button></body>', { runScripts: 'outside-only', pretendToBeVisual: true });
  const w = dom.window, timers = new Map(), frames = []; let count = 0;
  w.setTimeout = fn => { const id = ++count; timers.set(id, fn); return id; }; w.clearTimeout = id => timers.delete(id);
  w.requestAnimationFrame = fn => { frames.push(fn); return frames.length; };
  const cfg = { global: { tokens: 0, cost: 0, model_calls: 0, tool_calls: 0, seconds: 0 }, override: null, limits: { tokens: 0, cost: 0, model_calls: 0, tool_calls: 0, seconds: 0 }, pricing: { currency: 'CNY', models: {} }, models: [{ key: '["p", "m"]', label: 'p / m' }] };
  const snapshot = { tracked: true, usage: { total_tokens: 110, prompt_tokens: 100, completion_tokens: 10 }, currency: 'CNY', cost: .01, tasks: [{ id: 'task', session_id: 's', round: 1, kind: 'chat', state: taskState, version: 3, reason: 'model_calls', limits: { ...cfg.limits, model_calls: 1 }, elapsed: 8, model_calls: 1, tool_calls: 0, usage: { total_tokens: 110 }, roles: { main: { calls: 1, usage: { total_tokens: 70 } }, sub_agent: { calls: 1, usage: { total_tokens: 40 } } }, cost: .01, unknown_price_calls: 1, estimated_calls: 0 }] };
  const calls = { saved: [], continued: [], stopped: [], usage: [] };
  w.API = {
    async taskCostConfig() { return cfg; },
    async taskCostUsage(sid) { calls.usage.push(sid); return snapshot; },
    async saveTaskCostConfig(limits, sid) { calls.saved.push({ limits, sid }); },
    async saveTaskPricing() {},
    async continueTaskBudget(task, limits) { calls.continued.push({ task, limits }); snapshot.tasks[0].state = 'running'; },
    async stopTaskBudget(task) { calls.stopped.push(task.id); snapshot.tasks[0].state = 'cancelled'; },
  };
  w.FormatUtils = { fmtNum: n => String(n || 0), roundSummaryText: usage => '本轮消耗 ' + usage.total_tokens + ' tokens' };
  w.App = { state: { sessionId: 's', sessionUsage: {} }, toast() {}, setSessionUsage() {} };
  w.eval(fs.readFileSync(path.join(__dirname, '../js/app/task_costs.js'), 'utf8'));
  const findButton = text => Array.from(w.document.querySelectorAll('button')).find(b => b.textContent === text);
  async function flushFrames() {
    await settle();
    for (let i = 0; i < 20 && frames.length; i++) {
      const queued = frames.splice(0); queued.forEach(fn => fn()); await settle();
    }
    assert.equal(frames.length, 0, '用量更新应稳定下来，不能反复触发自身的 DOM 观察器');
  }
  return { w, cfg, snapshot, calls, timers, frames, flushFrames, findButton, close: () => w.close() };
}

test('authoritative refresh does not add usage twice; footer includes child usage', async t => {
  const s = setup(); t.after(s.close); await settle();
  await s.w.App.taskCosts.refresh(); await s.w.App.taskCosts.refresh();
  assert.equal(s.w.App.taskCosts.usage().total_tokens, 110);
  assert.equal(s.w.document.querySelector('.round-usage').textContent, '本轮消耗 110 tokens');
  assert.equal(s.timers.size, 0);
});

test('session change rejects a late response from the previous session', async t => {
  const s = setup(); t.after(s.close); await settle();
  let resolve;
  s.w.API.taskCostUsage = sid => sid === 's' ? new Promise(r => { resolve = r; }) : Promise.resolve({ tracked: true, usage: { total_tokens: 7 }, tasks: [] });
  const pending = s.w.App.taskCosts.refresh();
  s.w.App.state.sessionId = 'new'; s.w.App.taskCosts.noteSessionChanged();
  resolve(s.snapshot); await pending; await settle();
  assert.equal(s.w.App.taskCosts.usage().total_tokens, 7);
  assert.equal(s.w.document.getElementById('taskBudgetBanner').hidden, true);
});

test('limits default to unlimited; explicit amount saves independently for the session', async t => {
  const s = setup(); t.after(s.close); await settle(); await s.w.App.taskCosts.open();
  const scope = s.w.document.querySelector('[aria-label="预算配置范围"]'); scope.value = 'session'; scope.dispatchEvent(new s.w.Event('change'));
  const mode = s.w.document.querySelector('[aria-label="累计 tokens模式"]');
  const input = s.w.document.querySelector('[aria-label="累计 tokens额度"]');
  assert.equal(mode.value, 'unlimited'); assert.equal(input.disabled, true);
  mode.value = 'limited'; mode.dispatchEvent(new s.w.Event('change')); input.value = '1234';
  s.findButton('保存预算设置').click(); await settle(); await settle();
  assert.equal(s.calls.saved[0].limits.tokens, 1234); assert.equal(s.calls.saved[0].limits.cost, 0);
  assert.equal(s.calls.saved[0].sid, 's');
});

test('paused budget survives refresh; continue uses task version and does not send a chat message', async t => {
  const s = setup('paused'); t.after(s.close); await settle();
  assert.equal(s.w.document.getElementById('taskBudgetBanner').hidden, false);
  assert.equal(s.timers.size, 1);
  await s.w.App.taskCosts.open();
  const unlimited = s.findButton('本任务改为无限并继续'); unlimited.click(); unlimited.click();
  await settle(); await settle();
  assert.equal(s.calls.continued.length, 1);
  assert.equal(s.calls.continued[0].task.version, 3);
  assert.ok(Object.values(s.calls.continued[0].limits).every(v => v === 0));
});

test('stop applies to the selected task and clears pause banner', async t => {
  const s = setup('paused'); t.after(s.close); await settle(); await s.w.App.taskCosts.open();
  s.findButton('停止任务').click(); await settle(); await settle();
  assert.deepEqual(s.calls.stopped, ['task']);
  assert.equal(s.w.document.getElementById('taskBudgetBanner').hidden, true);
});

test('estimated usage with a model label settles without accumulating hints or observer frames', async t => {
  const s = setup('running'); t.after(s.close);
  s.w.FormatUtils = require('../js/format_utils.js');
  const line = s.w.document.querySelector('.round-usage');
  line.textContent = '本轮消耗 10 tokens · 模型 provider/model';
  s.snapshot.tasks[0].estimated_calls = 1;
  await s.flushFrames();
  const expected = line.textContent;
  assert.equal((expected.match(/含估算\/待返回用量/g) || []).length, 1);
  assert.equal((expected.match(/ · 模型 /g) || []).length, 1);
  for (let i = 0; i < 50; i++) s.w.App.taskCosts.decorate();
  await s.flushFrames();
  assert.equal(line.textContent, expected);
});

test('already polluted legacy text is repaired and estimate hints disappear when usage is final', async t => {
  const s = setup('running'); t.after(s.close);
  s.w.FormatUtils = require('../js/format_utils.js');
  const line = s.w.document.querySelector('.round-usage');
  line.textContent = '本轮消耗 10 tokens · 模型 provider/model' + ' · 含估算/待返回用量'.repeat(100);
  s.snapshot.tasks[0].estimated_calls = 2;
  await s.flushFrames();
  assert.equal(line.textContent, s.w.FormatUtils.roundSummaryText(s.snapshot.tasks[0].usage, {name:'provider/model'}) + ' · 含估算/待返回用量');
  s.snapshot.tasks[0].estimated_calls = 0;
  await s.w.App.taskCosts.refresh(); await s.flushFrames();
  assert.equal(line.textContent, s.w.FormatUtils.roundSummaryText(s.snapshot.tasks[0].usage, {name:'provider/model'}));
});

test('structured model snapshots override stale text; model-only legacy lines retain the model', async t => {
  const s = setup('running'); t.after(s.close);
  s.w.FormatUtils = require('../js/format_utils.js');
  const line = s.w.document.querySelector('.round-usage');
  line.textContent = '本轮模型 provider/legacy';
  s.snapshot.tasks[0].estimated_calls = 1;
  await s.flushFrames();
  assert.ok(line.textContent.includes(' · 模型 provider/legacy'));
  line._roundModelSnapshot = {provider:'new-provider',name:'new-model'};
  await s.w.App.taskCosts.refresh(); await s.flushFrames();
  assert.ok(line.textContent.includes(' · 模型 new-provider/new-model'));
  assert.equal(line.textContent.includes('legacy'), false);
});

test('stream summary replacements are decorated once; unrelated token output schedules no usage refresh', async t => {
  const s = setup('running'); t.after(s.close);
  s.w.FormatUtils = require('../js/format_utils.js');
  s.snapshot.tasks[0].estimated_calls = 1;
  await s.flushFrames();
  const line = s.w.document.querySelector('.round-usage');
  line._roundModelSnapshot = {provider:'p',name:'m'};
  line.textContent = '本轮消耗 1 tokens · 模型 p/m';
  await settle();
  assert.equal(s.frames.length, 1);
  await s.flushFrames();
  assert.equal(line.textContent, s.w.FormatUtils.roundSummaryText(s.snapshot.tasks[0].usage, line._roundModelSnapshot) + ' · 含估算/待返回用量');
  const body = s.w.document.createElement('div'); body.className = 'msg-assistant-body';
  s.w.document.getElementById('chatInner').append(body);
  for (let i = 0; i < 50; i++) body.textContent = 'streaming ' + i;
  await settle();
  assert.equal(s.frames.length, 0);
});

test('an explicitly missing model snapshot does not restore a stale label from display text', async t => {
  const s = setup('running'); t.after(s.close);
  s.w.FormatUtils = require('../js/format_utils.js');
  const line = s.w.document.querySelector('.round-usage');
  line._roundModelSnapshot = null;
  line.textContent = '本轮消耗 10 tokens · 模型 stale/model · 含估算/待返回用量';
  await s.flushFrames();
  assert.equal(line.textContent, s.w.FormatUtils.roundSummaryText(s.snapshot.tasks[0].usage, null));
});
