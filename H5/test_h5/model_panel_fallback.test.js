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
    addEventListener(t, fn) { this.handlers[t] = fn; }, querySelectorAll() { return []; }, querySelector() { return this.option || null; }, appendChild() {} };
}
function setup(invalid, sessionId = 's', options = {}) {
  const roles = ['chat_model', 'compaction_model', 'title_model', 'sub_agent_model'];
  const config = role => ({ models: [{ provider_name: 'P', model_name: 'Deepseek', max_output_tokens: 8192, api_type: options.protocol || 'chat-completions',
      parameter_capabilities: options.parameterCapabilities || {} },
      { provider_name: 'P', model_name: 'Other', max_output_tokens: 8192 }],
    role_info: { selection: { provider: 'P', model: 'Deepseek', api_type: options.protocol || 'chat_completions', parameter: options.parameters || {} }, selection_invalid: invalid && role === 'chat_model',
      is_overridden: Boolean(options.overridden),
      effective_parameter: options.protocol === 'messages' ? { max_tokens: 8192 } : options.protocol === 'responses' ? { max_output_tokens: 8192 } : { temperature: 0.7, max_tokens: 8192 }, effective_headers: [] } });
  const state = { sessionId, activeModelRole: 'chat_model', modelConfigs: {}, modelOverridden: {}, modelPanelDrafts: {} };
  const app = { state, MODEL_ROLES: roles.map(role => ({ role, label: role })),
    ROLE_DEFAULT_PARAMS: { chat_model: { temperature: 0.7, top_p: 1, presence_penalty: 0, reasoning_effort: 'low', enable_thinking: true } },
    API_TYPE_DEFAULT_PARAMS: {}, toast() {}, renderChatModelLabel() {}, refreshContextTokenStats() {},
    el: () => node(), setChatModelConfig(data) { state.modelConfigs.chat_model = data; } };
  for (const name of ['enhancePanel', 'modelTabs', 'modelPicker', 'modelPickerLabel', 'modelList', 'reasoningEffort',
    'enableThinking', 'chatParametersGroup', 'messagesParametersGroup', 'temperature', 'temperatureValue', 'maxTokens', 'maxTokensValue', 'topP', 'topPValue',
    'presencePenalty', 'presencePenaltyValue', 'messagesTemperatureRow', 'messagesTemperature', 'messagesTemperatureValue',
    'messagesTopPRow', 'messagesTopP', 'messagesTopPValue', 'messagesThinkingMode', 'messagesThinkingBudgetRow',
    'messagesThinkingBudget', 'messagesThinkingBudgetValue', 'messagesThinkingHint', 'messagesEffort', 'messagesStopSequences', 'messagesExtraBody',
    'responsesParametersGroup', 'responsesEffort', 'responsesSummary', 'responsesVerbosity', 'responsesSampling', 'responsesTemperatureRow', 'responsesTemperature', 'responsesTemperatureValue', 'responsesTopPRow', 'responsesTopP', 'responsesTopPValue', 'responsesInstructions', 'responsesExtraBody', 'responsesStateHint', 'responsesRebuildSource', 'responsesRebuild',
    'enhanceCancel', 'enhanceConfirm', 'enhanceReset', 'enhanceClearOverride',
    'enhanceHint', 'customHeadersRows', 'addHeaderRow']) app[name] = node();
  roles.forEach(role => { state.modelConfigs[role] = config(role); });
  const calls = [];
  const context = { window: { App: app }, API: {
    async getModels(role, sid) { return options.getModels ? options.getModels(role, sid, config(role)) : config(role); },
    async selectModel(...args) { calls.push(args); invalid = false; },
    async getResponsesState() { return { responses_state: options.responsesState || {} }; },
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

test('Responses 区分压缩窗口生成与新链启用，不凭 trigger 成功声称已生效', async () => {
  for (const activation of [undefined, 'pending_rebase', 'active']) {
    const { app } = setup(false, 's', { protocol: 'responses', responsesState: {
      status: activation === 'active' ? 'ready' : 'ready_to_rebase', has_remote_head: true,
      last_compaction: { phase: 'done', mode: 'trigger', activation, input_tokens_after: activation === 'active' ? 107 : null }
    } });
    app.openModelPanel(); await tick();
    if (activation === 'active') {
      assert.match(app.responsesStateHint.textContent, /已从官方压缩窗口建立新链/);
      assert.match(app.responsesStateHint.textContent, /107 tokens/);
    } else {
      assert.match(app.responsesStateHint.textContent, /压缩窗口待启用/);
      assert.doesNotMatch(app.responsesStateHint.textContent, /官方压缩已生效|已从官方压缩窗口建立新链/);
    }
  }
});

test('Messages 面板切换协议控件，保留显式原生参数', async () => {
  const { app, calls } = setup(false, 's', { native: true, protocol: 'messages',
    parameters: { messages: { thinking: { type: 'adaptive' }, output_config: { effort: 'high' } } } });
  app.openModelPanel(); await tick();
  assert.equal(app.chatParametersGroup.classList.contains('hidden'), true);
  assert.equal(app.messagesParametersGroup.classList.contains('hidden'), false);
  assert.equal(app.temperature.disabled, true);
  assert.equal(app.enableThinking.disabled, true);
  app.maxTokens.value = '4096'; app.maxTokens.handlers.input();
  await app.enhanceConfirm.handlers.click();
  const params = JSON.parse(JSON.stringify(calls[0][3]));
  assert.deepEqual(params, { max_tokens: 4096, thinking: { type: 'adaptive' }, output_config: { effort: 'high' } });
});

test('Messages 标准参数可通过控件编辑，高级扩展 JSON 校验为对象', async () => {
  const { app, calls } = setup(false, 's', { protocol: 'messages' });
  app.openModelPanel(); await tick();
  app.messagesExtraBody.value = '[]';
  app.messagesExtraBody.handlers.input();
  await app.enhanceConfirm.handlers.click();
  assert.equal(calls.length, 0);
  app.messagesExtraBody.value = '';
  app.messagesStopSequences.value = 'STOP';
  app.messagesStopSequences.handlers.input();
  app.messagesEffort.value = 'high'; app.messagesEffort.handlers.change();
  app.messagesThinkingMode.value = 'adaptive'; app.messagesThinkingMode.handlers.change();
  await app.enhanceConfirm.handlers.click();
  assert.deepEqual(JSON.parse(JSON.stringify(calls[0][3])).stop_sequences, ['STOP']);
  assert.deepEqual(JSON.parse(JSON.stringify(calls[0][3])).thinking, { type: 'adaptive' });
  assert.equal(calls[0][3].output_config.effort, 'high');
});

test('Messages 调整输出预算保留 extra_body，非法扩展对象不能保存', async () => {
  const extra = { cache_control: { type: 'ephemeral', ttl: '1h' }, stop: ['END'] };
  const { app, calls } = setup(false, 's', { native: true, protocol: 'messages',
    parameters: { messages: { extra_body: extra } } });
  app.openModelPanel(); await tick();
  assert.deepEqual(JSON.parse(app.messagesExtraBody.value), extra);
  app.messagesExtraBody.value = '[]';
  app.messagesExtraBody.handlers.input();
  await app.enhanceConfirm.handlers.click();
  assert.equal(calls.length, 0);
  app.messagesExtraBody.value = JSON.stringify(extra);
  app.messagesExtraBody.handlers.input();
  app.maxTokens.value = '2048'; app.maxTokens.handlers.input();
  await app.enhanceConfirm.handlers.click();
  assert.deepEqual(JSON.parse(JSON.stringify(calls[0][3])), { max_tokens: 2048, extra_body: extra });
});

test('Messages 恢复默认清除显式 thinking，不生成采样参数', async () => {
  const { app, calls } = setup(false, 's', { native: true, protocol: 'messages',
    parameters: { messages: { thinking: { type: 'adaptive' } } } });
  app.openModelPanel(); await tick();
  app.enhanceReset.handlers.click();
  await app.enhanceConfirm.handlers.click();
  assert.deepEqual(Object.keys(calls[0][3]), ['max_tokens']);
});

test('Messages 不支持 temperature 的模型会隐藏滑块并过滤旧会话参数', async () => {
  const { app, calls } = setup(false, 's', { protocol: 'messages',
    parameterCapabilities: { temperature: false },
    parameters: { messages: { temperature: 0.7, top_p: 0.9 } } });
  app.openModelPanel(); await tick();
  assert.equal(app.messagesTemperatureRow.classList.contains('hidden'), true);
  app.maxTokens.value = '2048'; app.maxTokens.handlers.input();
  await app.enhanceConfirm.handlers.click();
  assert.equal(calls[0][3].temperature, undefined);
  assert.equal(calls[0][3].top_p, 0.9);
});

test('Messages 恢复默认后切回 Chat，隐藏控件仍保留有效参数', async () => {
  const { app, calls } = setup(false, 's', { native: true, protocol: 'messages' });
  app.openModelPanel(); await tick();
  app.enhanceReset.handlers.click();
  choose(app, 'P::Other'); await tick();
  assert.equal(app.temperature.disabled, false);
  await app.enhanceConfirm.handlers.click();
  assert.equal(calls[0][3].temperature, 0.7);
  assert.equal(calls[0][3].top_p, 1);
});

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

test('Messages 模型声明不支持手动预算时禁用 Enabled 并说明原因', async () => {
  const { app } = setup(false, 's', { protocol: 'messages', parameterCapabilities: { thinking_budget: false } });
  app.messagesThinkingMode.option = { disabled: false };
  app.openModelPanel(); await tick();
  assert.equal(app.messagesThinkingMode.option.disabled, true);
  assert.equal(app.messagesThinkingBudgetRow.classList.contains('hidden'), true);
  assert.equal(app.messagesThinkingHint.classList.contains('hidden'), false);
  assert.match(app.messagesThinkingHint.textContent, /不支持手动思考预算/);
});

test('手动预算上限随最大输出 token 联动，超出时下调并随保存生效', async () => {
  const { app, calls } = setup(false, 's', { protocol: 'messages',
    parameters: { messages: { thinking: { type: 'enabled', budget_tokens: 8192 } } } });
  app.openModelPanel(); await tick();
  assert.equal(app.messagesThinkingMode.value, 'enabled');
  app.maxTokens.value = '4096'; app.maxTokens.handlers.input();
  assert.equal(app.messagesThinkingBudget.max, '3072');
  assert.equal(app.messagesThinkingBudget.value, '3072');
  assert.match(app.messagesThinkingHint.textContent, /3,072/);
  await app.enhanceConfirm.handlers.click();
  assert.deepEqual(JSON.parse(JSON.stringify(calls[0][3].thinking)), { type: 'enabled', budget_tokens: 3072 });
});

test('手动预算不小于最大输出 token 时保存前报错，不发送无效配置', async () => {
  const { app, calls } = setup(false, 's', { protocol: 'messages',
    parameters: { messages: { thinking: { type: 'enabled', budget_tokens: 8192 } } } });
  app.openModelPanel(); await tick();
  app.maxTokens.value = '4096'; // 未经 input 事件，预算不会被自动下调
  app.messagesTopP.value = '1'; app.messagesTopP.handlers.input(); // 制造一个改动以触发保存
  await app.enhanceConfirm.handlers.click();
  assert.equal(calls.length, 0);
});

test('开启 Thinking 时 Temperature 非 1 在保存前报错，调回 1 后可保存', async () => {
  const { app, calls } = setup(false, 's', { protocol: 'messages',
    parameters: { messages: { thinking: { type: 'adaptive' } } } });
  app.openModelPanel(); await tick();
  app.messagesTemperature.value = '0.5'; app.messagesTemperature.handlers.input();
  await app.enhanceConfirm.handlers.click();
  assert.equal(calls.length, 0);
  app.messagesTemperature.value = '1'; app.messagesTemperature.handlers.input();
  await app.enhanceConfirm.handlers.click();
  assert.equal(calls.length, 1);
  assert.equal(calls[0][3].temperature, 1);
});

test('全局面板未修改时不保存会话回退值', async () => {
  const { app, calls } = setup(true, null);
  app.openModelPanel();
  await new Promise(resolve => setImmediate(resolve));
  await app.enhanceConfirm.handlers.click();
  assert.equal(calls.length, 0);
});


test('Responses 切换原生控件，保存预算时不带 Chat 参数', async () => {
  const { app, calls } = setup(false, 's', { protocol: 'responses',
    parameters: { responses: { reasoning: { effort: 'low', summary: 'auto' }, text: { verbosity: 'low' } } } });
  app.openModelPanel(); await tick();
  assert.equal(app.responsesParametersGroup.classList.contains('hidden'), false);
  assert.equal(app.chatParametersGroup.classList.contains('hidden'), true);
  assert.equal(app.messagesParametersGroup.classList.contains('hidden'), true);
  assert.equal(app.enhancePanel.classList.contains('responses-mode'), true);
  app.maxTokens.value = '2048'; app.maxTokens.handlers.input();
  await app.enhanceConfirm.handlers.click();
  assert.deepEqual(JSON.parse(JSON.stringify(calls[0][3])), {
    max_output_tokens: 2048, reasoning: { effort: 'low', summary: 'auto' }, text: { verbosity: 'low' }
  });
});

test('Responses 默认省略采样，显式开启后滑块可调整', async () => {
  const { app, calls } = setup(false, 's', { protocol: 'responses' });
  app.openModelPanel(); await tick();
  assert.equal(app.responsesTemperatureRow.classList.contains('hidden'), true);
  app.responsesSampling.checked = true; app.responsesSampling.handlers.change();
  app.responsesTemperature.value = '0.5'; app.responsesTemperature.handlers.input();
  app.responsesTopP.value = '0.9'; app.responsesTopP.handlers.input();
  app.responsesEffort.value = 'medium'; app.responsesEffort.handlers.change();
  await app.enhanceConfirm.handlers.click();
  const param = JSON.parse(JSON.stringify(calls[0][3]));
  assert.equal(param.temperature, 0.5);
  assert.equal(param.top_p, 0.9);
  assert.deepEqual(param.reasoning, { effort: 'medium' });
  assert.equal(param.presence_penalty, undefined);
  assert.equal(param.extra_body, undefined);
});

test('Responses 跨角色草稿和恢复默认保留正确原生参数', async () => {
  const { app, calls } = setup(false, 's', { protocol: 'responses' });
  app.openModelPanel(); await tick();
  app.responsesEffort.value = 'high'; app.responsesEffort.handlers.change();
  tab(app, 'title_model');
  app.responsesSummary.value = 'auto'; app.responsesSummary.handlers.change();
  tab(app, 'chat_model');
  assert.equal(app.responsesEffort.value, 'high');
  app.enhanceReset.handlers.click();
  await app.enhanceConfirm.handlers.click();
  const chat = calls.find(call => call[2] === 'chat_model')[3];
  assert.equal(chat.reasoning, undefined);
  const title = calls.find(call => call[2] === 'title_model')[3];
  assert.equal(title.reasoning.summary, 'auto');
});
