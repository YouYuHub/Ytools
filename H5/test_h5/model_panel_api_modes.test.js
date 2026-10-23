const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function node() {
  const classes = new Set();
  return { value: '0', handlers: {}, checked: false, dataset: {}, textContent: '',
    classList: { add: c => classes.add(c), remove: c => classes.delete(c), contains: c => classes.has(c),
      toggle(c, on) { if (on) classes.add(c); else classes.delete(c); } },
    addEventListener(t, fn) { this.handlers[t] = fn; }, querySelectorAll() { return []; }, querySelector() { return null; }, appendChild() {} };
}

function setup(protocol) {
  const roles = ['chat_model', 'compaction_model', 'title_model', 'sub_agent_model'];
  const config = () => ({ models: [{ provider_name: 'P', model_name: 'M', max_output_tokens: 8192, api_type: protocol,
      parameter_capabilities: {}, responses_mode: 'stateful' }],
    role_info: { selection: { provider: 'P', model: 'M', api_type: protocol, parameter: {} },
      effective_parameter: protocol === 'messages' ? { max_tokens: 8192 } : protocol === 'responses' ? { max_output_tokens: 8192 } : { temperature: 0.7, max_tokens: 8192 }, effective_headers: [] } });
  const state = { sessionId: 's', activeModelRole: 'chat_model', modelConfigs: {}, modelOverridden: {}, modelPanelDrafts: {} };
  const app = { state, MODEL_ROLES: roles.map(role => ({ role, label: role })),
    ROLE_DEFAULT_PARAMS: { chat_model: { temperature: 0.7, top_p: 1, presence_penalty: 0, reasoning_effort: 'low', enable_thinking: true } },
    API_TYPE_DEFAULT_PARAMS: {}, toast() {}, renderChatModelLabel() {}, refreshContextTokenStats() {},
    el: () => node(), setChatModelConfig(data) { state.modelConfigs.chat_model = data; } };
  for (const name of ['enhancePanel', 'enhanceBody', 'enhanceScope', 'enhanceClose', 'modelTabs', 'modelPicker', 'modelPickerLabel', 'modelList', 'reasoningEffort',
    'enableThinking', 'chatParametersGroup', 'messagesParametersGroup', 'temperature', 'temperatureValue', 'maxTokens', 'maxTokensValue', 'topP', 'topPValue',
    'presencePenalty', 'presencePenaltyValue', 'messagesTemperatureRow', 'messagesTemperature', 'messagesTemperatureValue',
    'messagesTopPRow', 'messagesTopP', 'messagesTopPValue', 'messagesThinkingMode', 'messagesThinkingBudgetRow',
    'messagesThinkingBudget', 'messagesThinkingBudgetValue', 'messagesThinkingHint', 'messagesEffort', 'messagesStopSequences', 'messagesExtraBody',
    'responsesParametersGroup', 'responsesMode', 'responsesModeHint', 'responsesStatefulControls', 'responsesEffort', 'responsesSummary', 'responsesVerbosity', 'responsesSampling', 'responsesTemperatureRow', 'responsesTemperature', 'responsesTemperatureValue', 'responsesTopPRow', 'responsesTopP', 'responsesTopPValue', 'responsesInstructions', 'responsesExtraBody', 'responsesStateHint', 'responsesRebuildSource', 'responsesRebuild',
    'enhanceCancel', 'enhanceConfirm', 'enhanceReset', 'enhanceClearOverride',
    'enhanceHint', 'customHeadersRows', 'addHeaderRow']) app[name] = node();
  // 下拉与数字控件需要可写属性
  app.messagesThinkingMode.value = '';
  app.messagesThinkingBudget.value = '8192';
  app.maxTokens.value = '8192';
  roles.forEach(role => { state.modelConfigs[role] = config(); });
  app.showModelPanelModal = () => { app.enhancePanel.classList.remove('hidden'); };
  app.hideModelPanelModal = () => {};
  const context = { window: { App: app }, API: {
    async getModels(role) { return config(); },
    async getResponsesState() { return { responses_state: {} }; },
  } };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../js/app/model_panel.js'), 'utf8'), context);
  return app;
}

for (const [protocol, mode, api] of [['chat-completions', 'completions-mode', 'completions'], ['chat_completions', 'completions-mode', 'completions'], ['messages', 'messages-mode', 'messages'], ['responses', 'responses-mode', 'responses']]) {
  test(`参数面板 ${protocol} 使用 ${mode}（不依赖全局强调色）`, () => {
    const app = setup(protocol);
    app.openModelPanel();
    for (const cls of ['completions-mode', 'messages-mode', 'responses-mode']) {
      assert.equal(app.enhancePanel.classList.contains(cls), cls === mode, `${protocol} 下 ${cls} 互斥`);
    }
    assert.equal(app.enhancePanel.dataset.api, api);
    assert.equal(app.modelPicker.dataset.api, api);
  });
}

test('模型列表项按自身 api_type 打标（data-api 与 api-* 类）', () => {
  const src = fs.readFileSync(path.join(__dirname, '../js/app/model_panel.js'), 'utf8');
  assert.match(src, /apiKindOf\(m\)/);
  assert.match(src, /model-list-item api-" \+ kind/);
  assert.match(src, /item\.dataset\.api = kind/);
});

test('编译后 CSS 包含三色体系与列表项区分样式', () => {
  const css = fs.readFileSync(path.join(__dirname, '../style/css/main.css'), 'utf8');
  assert.match(css, /\.enhance-panel\.completions-mode/);
  assert.match(css, /\.enhance-panel\.messages-mode/);
  assert.match(css, /\.enhance-panel\.responses-mode/);
  assert.match(css, /--api-accent/);
  assert.match(css, /\.model-list-item\[data-api="?messages"?\]/);
  assert.match(css, /\.model-list-item\[data-api="?responses"?\]/);
  // 选中态不再使用全局 --bg-active
  assert.doesNotMatch(css, /\.model-tab\.active\s*\{[^}]*var\(--bg-active\)/);
  assert.doesNotMatch(css, /\.model-list-item\.active\s*\{[^}]*var\(--bg-active\)/);
});
