const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

test('聊天设置加载、恢复和保存不改隐藏的回传配置，保留其他配置映射', async () => {
  const source = fs.readFileSync(path.join(__dirname, '../js/app/composer.js'), 'utf8');
  const core = fs.readFileSync(path.join(__dirname, '../js/app/core.js'), 'utf8');
  const saved = [], loaded = [], notices = [];
  const node = () => ({ value: '', events: {}, classList: { remove() {} }, setAttribute() {},
    addEventListener(event, fn) { this.events[event] = fn; } });
  const context = { state: { sessionId: 'test-session' }, API: {}, App: { refreshContextTokenStats() {} },
    toast: text => notices.push(text), loadSettingsRetitle() {}, closeChatSettings() {},
  };
  for (const name of ['chatSettingsModal', 'chatSettingsConfirm', 'chatSettingsReset', 'chatSettingsCancel',
    'chatSettingsClose', 'chatSettingsBackdrop', 'toolCallTimeoutSeconds', 'networkRetryMaxAttempts',
    'compactionRetryMaxAttempts', 'videoReadMaxSeconds', 'mcpToolWorkers', 'subAgentMaxConcurrent',
    'subAgentMaxRounds', 'subAgentTimeoutSeconds', 'subAgentFinalReplyRetryMax',
    'subAgentStreamErrorRetryMax', 'subAgentTodoRemindMax', 'triggerRatio', 'summaryBudgetRatio',
    'historyTargetTokens', 'oversizedRejectFactor', 'maxOversizedRejections']) context[name] = node();
  context.effectiveThresholdHint = null; context.historyTargetHint = null;
  const configs = {
    HistoryCompaction: { trigger_ratio: 0.7, summary_budget_ratio: 0.3, target_tokens: 2000, oversized_reject_factor: 2, max_oversized_rejections: 4 },
    McpTool: { call_timeout_seconds: 123 }, NetworkRetry: { max_attempts: 8 },
    CompactionRetry: { max_attempts: 9 }, VideoReadLimit: { max_seconds: 90 },
    ToolConcurrency: { mcp_tool_workers: 5, sub_agent_max_concurrent: 6 },
    SubAgentRetry: { final_reply_max_attempts: 10, stream_error_max_attempts: 11, todo_remind_max: 12 },
    SubAgentLimits: { max_rounds: 55, timeout_seconds: 456 },
  };
  for (const [name, config] of Object.entries(configs)) {
    context.API['get' + name + 'Config'] = async sid => { loaded.push([name, sid]); return config; };
    context.API['update' + name + 'Config'] = async (payload, sid) => { saved.push([name, payload, sid]); };
  }
  context.API.getContextReturnConfig = context.API.updateContextReturnConfig = () => { throw new Error('must not access hidden settings'); };
  vm.createContext(context);
  vm.runInContext(core.slice(core.indexOf('  const CHAT_SETTINGS_DEFAULTS ='), core.indexOf('  function readTitleOverrides')), context);
  vm.runInContext(source.slice(source.indexOf('  async function openChatSettings()'), source.indexOf('  // ---------- 每会话独立的输入草稿')), context);
  await context.openChatSettings();
  assert.equal(loaded.length, 8);
  assert.equal(context.toolCallTimeoutSeconds.value, 123);
  assert.equal(context.subAgentTimeoutSeconds.value, 456);
  await context.chatSettingsConfirm.events.click();
  assert.equal(saved.length, 8);
  for (const [name, payload, sid] of saved) {
    assert.deepEqual(JSON.parse(JSON.stringify(payload)), configs[name]);
    assert.equal(sid, name === 'HistoryCompaction' ? 'test-session' : undefined);
  }
  context.chatSettingsReset.events.click();
  assert.equal(context.toolCallTimeoutSeconds.value, 300);
  assert.equal(context.subAgentMaxRounds.value, 40);
  await context.chatSettingsConfirm.events.click();
  assert.equal(saved.length, 16);
  assert.ok(!notices.some(text => text.includes('失败')));
  const html = fs.readFileSync(path.join(__dirname, '../index.html'), 'utf8');
  assert.ok(!html.includes('id="reasoningMaxLength"'));
  assert.ok(!html.includes('id="toolResultMaxLength"'));
  assert.ok(html.includes('每条消息重新标题（仅本会话）'));
});
