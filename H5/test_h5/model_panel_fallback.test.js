const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function node() {
  const classes = new Set();
  return { value: '0', handlers: {}, checked: false,
    classList: { add: c => classes.add(c), remove: c => classes.delete(c), contains: c => classes.has(c),
      toggle(c, on) { if (on) classes.add(c); else classes.delete(c); } },
    addEventListener(t, fn) { this.handlers[t] = fn; }, querySelectorAll() { return []; }, appendChild() {} };
}
function setup(invalid, sessionId = 's', options = {}) {
  const roles = ['chat_model', 'compaction_model', 'title_model', 'sub_agent_model'];
  const config = role => ({ models: [{ provider_name: 'P', model_name: 'Deepseek', max_output_tokens: 8192 },
      { provider_name: 'P', model_name: 'Other', max_output_tokens: 8192 }],
    role_info: { selection: { provider: 'P', model: 'Deepseek' }, selection_invalid: invalid && role === 'chat_model',
      is_overridden: Boolean(options.overridden),
      effective_parameter: { temperature: 0.7, max_tokens: 8192 }, effective_headers: [] } });
  const state = { sessionId, activeModelRole: 'chat_model', modelConfigs: {}, modelOverridden: {}, modelPanelDrafts: {} };
  const app = { state, MODEL_ROLES: roles.map(role => ({ role, label: role })),
    ROLE_DEFAULT_PARAMS: { chat_model: { temperature: 0.7, top_p: 1, presence_penalty: 0, reasoning_effort: 'low', enable_thinking: true } },
    API_TYPE_DEFAULT_PARAMS: {}, toast() {}, renderChatModelLabel() {}, refreshContextTokenStats() {},
    el: () => node(), setChatModelConfig(data) { state.modelConfigs.chat_model = data; } };
  for (const name of ['enhancePanel', 'modelTabs', 'modelPicker', 'modelPickerLabel', 'modelList', 'reasoningEffort',
    'enableThinking', 'temperature', 'temperatureValue', 'maxTokens', 'maxTokensValue', 'topP', 'topPValue',
    'presencePenalty', 'presencePenaltyValue', 'enhanceCancel', 'enhanceConfirm', 'enhanceReset', 'enhanceClearOverride',
    'enhanceHint', 'customHeadersRows', 'addHeaderRow']) app[name] = node();
  roles.forEach(role => { state.modelConfigs[role] = config(role); });
  const calls = [];
  const context = { window: { App: app }, API: {
    async getModels(role, sid) { return options.getModels ? options.getModels(role, sid, config(role)) : config(role); },
    async selectModel(...args) { calls.push(args); invalid = false; },
  } };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../js/app/model_panel.js'), 'utf8'), context);
  return { app, state, calls };
}

test('会话失效模型已显示为 Deepseek，确认仍保存当前选择', async () => {
  const { app, calls } = setup(true);
  app.openModelPanel();
  await new Promise(resolve => setImmediate(resolve));
  assert.match(app.enhanceHint.textContent, /原会话模型已失效/);
  assert.equal(app.enhanceClearOverride.classList.contains('hidden'), false);
  await app.enhanceConfirm.handlers.click();
  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0].slice(0, 3), ['P', 'Deepseek', 'chat_model']);
  assert.equal(calls[0][3], null, '未改动参数，沿用后端实际生效参数');
  assert.equal(calls[0][4], 's');
});

function choose(app, key) {
  app.modelList.handlers.click({ target: { closest: () => ({ dataset: { key } }) } });
}
function tab(app, role) {
  app.modelTabs.handlers.click({ target: { closest: () => ({ dataset: { role } }) } });
}
const tick = () => new Promise(resolve => setImmediate(resolve));

test('手动选择与全局相同的模型也建立会话独立配置', async () => {
  const { app, calls } = setup(false);
  app.openModelPanel(); await tick();
  choose(app, 'P::Deepseek');
  await app.enhanceConfirm.handlers.click();
  assert.equal(calls.length, 1); assert.equal(calls[0][4], 's');
});

test('打开面板的慢加载不能清掉各角色刚选择的模型和参数', async () => {
  const pending = [];
  let slow = true;
  const { app, state, calls } = setup(false, 's', { getModels(role, sid, cfg) {
    return slow ? new Promise(resolve => pending.push(() => resolve(cfg))) : cfg;
  } });
  app.openModelPanel();
  choose(app, 'P::Other');
  app.temperature.value = '0.23'; app.temperature.handlers.input();
  tab(app, 'compaction_model');
  app.temperature.value = '0.31'; app.temperature.handlers.input();
  tab(app, 'title_model');
  app.temperature.value = '0.41'; app.temperature.handlers.input();
  tab(app, 'sub_agent_model');
  app.temperature.value = '0.51'; app.temperature.handlers.input();
  pending.forEach(resolve => resolve()); slow = false; await tick();
  tab(app, 'chat_model');
  assert.equal(state.selectedModelKey, 'P::Other'); assert.equal(app.temperature.value, '0.23');
  await app.enhanceConfirm.handlers.click();
  assert.equal(calls.length, 4);
  assert.equal(calls[0][1], 'Other');
  assert.deepEqual(calls.map(call => call[3].temperature), [0.23, 0.31, 0.41, 0.51]);
  assert.ok(calls.every(call => call[4] === 's'));
});

test('慢加载期间确认保存，旧响应不能覆盖保存结果', async () => {
  const pending = [];
  let slow = true;
  const { app, state, calls } = setup(false, 's', { getModels(role, sid, cfg) {
    return slow ? new Promise(resolve => pending.push(() => resolve(cfg))) : cfg;
  } });
  app.openModelPanel();
  choose(app, 'P::Other'); slow = false;
  await app.enhanceConfirm.handlers.click();
  assert.equal(calls[0][1], 'Other');
  state.modelConfigs.chat_model = { role_info: { selection: { provider: 'P', model: 'SavedModel' } } };
  pending.forEach(resolve => resolve()); await tick();
  assert.equal(state.modelConfigs.chat_model.role_info.selection.model, 'SavedModel');
});

test('切换会话后旧面板加载不回填缓存，也不能把旧草稿保存到新会话', async () => {
  const pending = [];
  const { app, state, calls } = setup(false, 's', { getModels(role, sid, cfg) {
    return new Promise(resolve => pending.push(() => resolve(cfg)));
  } });
  app.openModelPanel(); choose(app, 'P::Other');
  state.sessionId = 'new-session';
  state.modelConfigs.chat_model = { role_info: { selection: { provider: 'P', model: 'NewSessionModel' } } };
  pending.forEach(resolve => resolve()); await tick();
  assert.equal(state.modelConfigs.chat_model.role_info.selection.model, 'NewSessionModel');
  await app.enhanceConfirm.handlers.click(); assert.equal(calls.length, 0);
});

test('已有有效独立配置时重选同一模型不产生多余保存', async () => {
  const { app, calls } = setup(false, 's', { overridden: true });
  app.openModelPanel(); await tick(); choose(app, 'P::Deepseek');
  await app.enhanceConfirm.handlers.click(); assert.equal(calls.length, 0);
});

test('有效会话选择未修改时不产生多余保存', async () => {
  const { app, calls } = setup(false);
  app.openModelPanel();
  await new Promise(resolve => setImmediate(resolve));
  await app.enhanceConfirm.handlers.click();
  assert.equal(calls.length, 0);
});

test('全局面板未修改时不保存会话回退值', async () => {
  const { app, calls } = setup(true, null);
  app.openModelPanel();
  await new Promise(resolve => setImmediate(resolve));
  await app.enhanceConfirm.handlers.click();
  assert.equal(calls.length, 0);
});
