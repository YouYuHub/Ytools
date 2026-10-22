const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');
const settle = () => new Promise(resolve => setImmediate(resolve));
const clone = value => JSON.parse(JSON.stringify(value));
const base = () => ({ name: 'Review', description: '', prompt: '检查项目', output_format: 'Markdown', acceptance: '提供验证结果',
  models: { chat_model: { provider: 'P', model: 'Chat' } }, tools: {}, budget: null, budget_currency: null, work_dir: { mode: 'inherit', path: '' } });

function setup() {
  const dom = new JSDOM('<body><main id="background"><button id="taskTemplatesItem">任务模板</button><textarea id="input">当前会话草稿</textarea></main></body>', { runScripts: 'outside-only', pretendToBeVisual: true, url: 'http://localhost/ui/' });
  const w = dom.window, calls = { saved: [], preview: [], started: [], opened: [], drafts: [], deleted: [], downloads: [], validated: [], status: [] };
  w.Blob = Blob; w.URL.createObjectURL = blob => { calls.downloads.push(blob); return 'blob:test'; }; w.URL.revokeObjectURL = () => {};
  w.HTMLAnchorElement.prototype.click = () => {};
  const cfg = { models: [{ provider: 'P', model: 'Chat', api_type: 'chat_completions' }, { provider: 'P', model: 'Messages', api_type: 'messages' }, { provider: 'P', model: 'Responses', api_type: 'responses' }],
    tools: [{ server: '__builtin__', name: 'run_command' }, { server: 'S', name: 'echo' }], currency: 'CNY', warnings: [],
    capture: { models: { chat_model: { provider: 'P', model: 'Chat' } }, tools: { S: ['echo'] }, budget: { tokens: 500, cost: 0, model_calls: 2, tool_calls: 0, seconds: 0 }, budget_currency: 'CNY', work_dir: { mode: 'fixed', path: 'C:/project' } } };
  let saved = { id: 'saved', version: 1, template: base() };
  function plan(definition) {
    const tools = definition.tools === null ? cfg.capture.tools : definition.tools;
    return { fingerprint: 'a'.repeat(64), summary: { name: definition.name, models: definition.models || cfg.capture.models,
      tools: Object.entries(tools).flatMap(([server, names]) => names.map(name => ({ server, name }))), work_dir: definition.work_dir.path || 'C:/project',
      budget: definition.budget || cfg.capture.budget, currency: 'CNY', prompt: [definition.prompt, definition.output_format, definition.acceptance].filter(Boolean).join('\n\n'), warnings: [] } };
  }
  w.API = {
    async listTaskTemplates() { return { templates: saved ? [{ id: saved.id, name: saved.template.name }] : [] }; },
    async taskTemplateOptions() { return cfg; },
    async getTaskTemplate() { return clone(saved); },
    async saveTaskTemplate(definition, id, version) { calls.saved.push({ definition: clone(definition), id, version }); saved = { id: id || 'new', version: version ? version + 1 : 1, template: clone(definition) }; return clone(saved); },
    async deleteTaskTemplate(id, version) { calls.deleted.push({ id, version }); saved = null; },
    async previewTaskTemplate(definition, session) { calls.preview.push({ definition: clone(definition), session }); return plan(definition); },
    async startTaskTemplate(definition, session, request, fingerprint) { calls.started.push({ definition: clone(definition), session, request, fingerprint }); return { session_id: 'task-created', summary: plan(definition).summary }; },
    async listTools() {},
    async validateTaskTemplate(definition) { calls.validated.push(definition); if (definition.api_key) throw new Error('extra fields forbidden'); return { template: definition }; },
    async streamStatus(session) { calls.status.push(session); return { running: false }; },
  };
  w.App = { state: { sessionId: 'source' }, input: w.document.getElementById('input'), toast() {}, closeMenus() {}, loadSessions() {},
    async openSession(id) { calls.drafts.push({ session: this.state.sessionId, text: this.input.value }); this.state.sessionId = id; this.input.value = ''; calls.opened.push(id); },
    saveSessionDraft() { calls.drafts.push({ session: this.state.sessionId, text: this.input.value }); },
    send() { throw new Error('模板不能自动发送聊天消息'); } };
  w.eval(fs.readFileSync(path.join(__dirname, '../js/app/task_templates.js'), 'utf8'));
  const $ = selector => w.document.querySelector(selector), btn = text => Array.from(w.document.querySelectorAll('.task-template-modal button')).find(n => n.textContent === text);
  function change(selector, value) { const element = $(selector); element.value = value; element.dispatchEvent(new w.Event('change', { bubbles: true })); return element; }
  async function open(prefill) { await w.App.taskTemplates.open(prefill); await settle(); }
  async function selectSaved() { change('#taskTemplateSelect', 'saved'); await settle(); }
  return { w, cfg, calls, $, btn, change, open, selectSaved, plan, close: () => w.close() };
}

test('open and edit never invoke a model or create a session; captures references, tool set, budget, path and current prompt', async t => {
  const s = setup(); t.after(s.close); await s.open();
  assert.equal(s.calls.started.length, 0);
  s.btn('从当前配置创建').click(); await settle();
  assert.equal(s.$('#taskTemplatePrompt').value, '当前会话草稿');
  assert.equal(s.$('#taskTemplateWorkPath').value, 'C:/project');
  s.change('#taskTemplateName', '项目审查'); s.btn('保存模板').click(); await settle();
  const definition = s.calls.saved[0].definition;
  assert.deepEqual(definition.models.chat_model, { provider: 'P', model: 'Chat' });
  assert.deepEqual(definition.tools, { S: ['echo'] }); assert.equal(definition.budget.tokens, 500);
  assert.equal(definition.budget_currency, 'CNY'); assert.equal(s.calls.started.length, 0);
});

test('preview shows exact prompt/config as text; confirming creates one independent session and an unsent draft', async t => {
  const s = setup(); t.after(s.close); await s.open(); await s.selectSaved();
  s.change('#taskTemplatePrompt', '<img src=x onerror=alert(1)>');
  s.btn('预览并新建会话').click(); s.btn('预览并新建会话').click(); await settle();
  assert.equal(s.calls.preview.length, 1); assert.equal(s.calls.started.length, 0);
  assert.match(s.$('.template-preview').textContent, /<img/); assert.equal(s.$('.template-preview img'), null);
  assert.equal(s.$('.template-editor').hidden, true);
  s.btn('确认新建（不自动发送）').click(); s.btn('确认新建（不自动发送）').click(); await settle(); await settle();
  assert.equal(s.calls.started.length, 1); assert.equal(s.calls.started[0].session, 'source');
  assert.equal(s.calls.drafts[0].text, '当前会话草稿'); assert.equal(s.w.App.state.sessionId, 'task-created');
  assert.match(s.w.App.input.value, /<img/); assert.equal(s.$('.task-template-modal').hidden, true);
});

test('editing after preview invalidates it; network retry keeps the same idempotency key', async t => {
  const s = setup(); t.after(s.close); await s.open(); await s.selectSaved();
  s.btn('预览并新建会话').click(); await settle();
  let fail = true; const original = s.w.API.startTaskTemplate;
  s.w.API.startTaskTemplate = async (...args) => { if (fail) { fail = false; s.calls.started.push({ request: args[2] }); throw new Error('network error'); } return original(...args); };
  s.btn('确认新建（不自动发送）').click(); await settle();
  assert.equal(s.$('.task-template-modal').hidden, false); assert.match(s.$('.template-status').textContent, /network error/);
  s.btn('确认新建（不自动发送）').click(); await settle(); assert.equal(s.calls.started[0].request, s.calls.started[1].request);
});

test('switching source session before confirmation does not create a task or navigate', async t => {
  const s = setup(); t.after(s.close); await s.open(); await s.selectSaved(); s.btn('预览并新建会话').click(); await settle();
  s.w.App.state.sessionId = 'other'; s.btn('确认新建（不自动发送）').click(); await settle();
  assert.equal(s.calls.started.length, 0); assert.match(s.$('.template-status').textContent, /会话已切换/);
});

test('save error preserves draft; close/reopen preserves edits; delete requires explicit confirmation and revision', async t => {
  const s = setup(); t.after(s.close); await s.open(); await s.selectSaved();
  s.change('#taskTemplateName', 'Unsaved rename'); s.w.API.saveTaskTemplate = async () => { throw new Error('模板已被其他页面修改'); };
  s.btn('保存模板').click(); await settle(); assert.equal(s.$('#taskTemplateName').value, 'Unsaved rename');
  s.w.App.taskTemplates.close(); await s.open(); assert.equal(s.$('#taskTemplateName').value, 'Unsaved rename');
  s.btn('删除模板').click(); assert.equal(s.calls.deleted.length, 0); s.btn('取消').click(); assert.equal(s.calls.deleted.length, 0);
  s.btn('删除模板').click(); s.btn('确认').click(); await settle(); assert.deepEqual(s.calls.deleted, [{ id: 'saved', version: 1 }]);
});

test('changing template guards unsaved edits; duplicate saves as a new record without overwriting', async t => {
  const s = setup(); t.after(s.close); await s.open(); await s.selectSaved(); s.change('#taskTemplateName', 'Changed');
  s.btn('新建').click(); assert.equal(s.$('.template-confirm').hidden, false); s.btn('取消').click(); assert.equal(s.$('#taskTemplateName').value, 'Changed');
  s.btn('另存为副本').click(); s.btn('保存模板').click(); await settle(); assert.equal(s.calls.saved[0].id, null); assert.match(s.calls.saved[0].definition.name, /副本/);
});

test('explicit empty tool set and zero budgets are preserved; budget currency is editable', async t => {
  const s = setup(); t.after(s.close); await s.open(); s.change('#taskTemplateName', 'Empty');
  s.change('[aria-label="模板工具策略"]', 'custom'); s.change('[aria-label="模板预算策略"]', 'custom'); s.change('[aria-label="费用预算币种"]', 'USD');
  s.btn('保存模板').click(); await settle(); const definition = s.calls.saved[0].definition;
  assert.deepEqual(definition.tools, {}); assert.deepEqual(definition.budget, { tokens: 0, cost: 0, model_calls: 0, tool_calls: 0, seconds: 0 }); assert.equal(definition.budget_currency, 'USD');
});

test('import validates structure before filling the editor and never auto-applies; export contains only definition', async t => {
  const s = setup(); t.after(s.close); await s.open();
  const upload = s.$('input[type=file]');
  Object.defineProperty(upload, 'files', { configurable: true, value: [{ size: 100, text: async () => JSON.stringify({ format: 'ytools-task-template', version: 1, template: base() }) }] });
  upload.dispatchEvent(new s.w.Event('change')); await settle(); await settle();
  assert.equal(s.$('#taskTemplateName').value, 'Review'); assert.equal(s.calls.validated.length, 1); assert.equal(s.calls.started.length, 0); assert.equal(s.calls.saved.length, 0);
  s.btn('导出 JSON').click(); const exported = JSON.parse(await s.calls.downloads[0].text());
  assert.equal(exported.format, 'ytools-task-template'); assert.deepEqual(exported.template.models, base().models); assert.ok(!('source_session_id' in exported));
});

test('initial loading is cancellable and a late response cannot overwrite a reopened draft', async t => {
  const s = setup(); t.after(s.close); let finish; const original = s.w.API.taskTemplateOptions;
  s.w.API.taskTemplateOptions = () => new Promise(resolve => { finish = resolve; });
  const pending = s.w.App.taskTemplates.open(); s.w.App.taskTemplates.close(); assert.equal(s.$('.task-template-modal').hidden, true);
  s.w.API.taskTemplateOptions = original; await s.open(); s.change('#taskTemplateName', 'New draft'); finish(s.cfg); await pending;
  assert.equal(s.$('#taskTemplateName').value, 'New draft');
});

test('template draft restores only for an idle empty session, preserving typed drafts and rejecting stale sessions', async t => {
  const s = setup(); t.after(s.close); const meta = { record_count: 0, completion_count: 0, task_template: { initial_prompt: '模板草稿' } };
  await s.w.App.taskTemplates.restoreDraft(meta, 'source'); assert.equal(s.w.App.input.value, '当前会话草稿');
  s.w.App.input.value = ''; await s.w.App.taskTemplates.restoreDraft(meta, 'source'); assert.equal(s.w.App.input.value, '模板草稿');
  s.w.App.input.value = ''; s.w.API.streamStatus = async () => ({ running: true }); await s.w.App.taskTemplates.restoreDraft(meta, 'source'); assert.equal(s.w.App.input.value, '');
  s.w.API.streamStatus = async () => ({ running: false }); await s.w.App.taskTemplates.restoreDraft(meta, 'old'); assert.equal(s.w.App.input.value, '');
  await s.w.App.taskTemplates.restoreDraft({ ...meta, record_count: 1 }, 'source'); assert.equal(s.w.App.input.value, '');
  await s.w.App.taskTemplates.restoreDraft({ ...meta, task_template: { ...meta.task_template, draft_consumed: true } }, 'source'); assert.equal(s.w.App.input.value, '');
});

test('Skills content can become a template and modal restores focus, inert state and Escape handling', async t => {
  const s = setup(); t.after(s.close); s.$('#taskTemplatesItem').focus(); s.$('.task-template-modal').inert = true;
  await s.open({ capture: true, prompt: 'Skills 正文' }); assert.equal(s.$('#taskTemplatePrompt').value, 'Skills 正文');
  assert.equal(s.$('.task-template-modal').inert, false); assert.equal(s.$('#background').inert, true);
  s.$('#taskTemplateName').dispatchEvent(new s.w.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  assert.equal(s.$('.task-template-modal').hidden, true); assert.equal(s.$('.task-template-modal').inert, true);
  assert.equal(s.w.document.activeElement.id, 'taskTemplatesItem'); assert.notEqual(s.$('#background').inert, true);
});

test('API wrappers use strict JSON payloads, escaped template IDs and the selected source session', async t => {
  const dom = new JSDOM('<body></body>', { url: 'http://localhost/ui/', runScripts: 'outside-only' }); t.after(() => dom.window.close()); const w = dom.window, calls = [];
  w.fetch = async (url, options) => { calls.push({ url, options }); return { ok: true, json: async () => ({}) }; };
  w.eval(fs.readFileSync(path.join(__dirname, '../js/api.js'), 'utf8')); const signal = new w.AbortController().signal;
  await w.API.taskTemplateOptions('a b', signal); await w.API.saveTaskTemplate(base(), 'id/a', 2); await w.API.deleteTaskTemplate('id/a', 2);
  await w.API.previewTaskTemplate(base(), 'source', signal); await w.API.startTaskTemplate(base(), 'source', 'uuid', 'hash');
  assert.equal(new URL(calls[0].url).searchParams.get('source_session_id'), 'a b'); assert.equal(calls[0].options.signal, signal);
  assert.match(calls[1].url, /id%2Fa/); assert.equal(calls[1].options.method, 'PUT'); assert.equal(JSON.parse(calls[1].options.body).version, 2);
  assert.equal(calls[2].options.method, 'DELETE'); assert.equal(calls[3].options.signal, signal);
  assert.equal(JSON.parse(calls[4].options.body).source_session_id, 'source'); assert.equal(JSON.parse(calls[4].options.body).request_id, 'uuid');
});
