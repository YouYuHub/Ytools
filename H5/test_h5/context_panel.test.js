const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');
const settle = () => new Promise(resolve => setImmediate(resolve));

function setup() {
  const dom = new JSDOM('<body><main><button id="contextTokenSummary">10k</button><button id="contextManagerItem">上下文管理</button><input id="draft"></main></body>',
    { runScripts: 'outside-only', pretendToBeVisual: true });
  const w = dom.window, calls = { overview: [], compact: 0, model: 0, settings: 0, rebuild: 0, jumped: [] };
  const fixture = { request_context_tokens: 10000, context_token_limit: 100000, messages_tokens: 7000,
    system_prompt_tokens: 1000, tool_definition_tokens: 1500, file_memory_tokens: 500, rounds: { summarized: 30 },
    inspection: { model: { name: 'Test Model', provider: 'Local', protocol: 'responses', mode: 'stateful' },
      policy: { trigger_ratio: .8, effective_window: 100000, trigger_threshold: 80000, target_tokens: 20000, summary_budget: 12000 },
      remote: { applicable: true, status: 'ready', compatible: true, has_remote_head: true, has_compacted_window: false,
        last_usage: { input_tokens: 1000, output_tokens: 200 }, chain_tokens_estimate: 1200 },
      summary_blocks: [{ index: 1, round_start: 1, round_end: 30, repair_required: true,
        preview: { text: '<script>untrusted summary</script>', truncated: true } }],
      repair_required: true, round_page: { offset: 0, limit: 40, total: 50, has_more: true,
        items: [{ round: 50, representation: 'raw', question: { text: '<img src=x onerror=alert(1)>', truncated: false } }] },
      recent_compactions: [{ phase: 'done', scope: 'session', trigger_reason: 'history_mutation', before_tokens: 200000, after_tokens: 10000 }],
      captured_at: '2026-10-20T00:00:00Z', notes: ['Draft not included.'] } };
  w.setInterval = () => { throw new Error('context panel must not poll'); };
  w.API = {
    async getContextOverview(sid, tools, offset, signal) { calls.overview.push({ sid, tools, offset, signal });
      const value = JSON.parse(JSON.stringify(fixture)); value.inspection.round_page.offset = offset || 0; return value; },
    async rebuildResponsesContext() { calls.rebuild++; }
  };
  w.App = { state: { sessionId: 's', hasConversation: true, selectedTools: new Set(['read_file']), streaming: false }, toast() {},
    startManualCompaction() { calls.compact++; }, openModelPanel() { calls.model++; }, openChatSettings() { calls.settings++; },
    openConfirmDialog(opts) { calls.confirm = opts; }, jumpToQuoteSource(source) { calls.jumped.push(source); } };
  w.eval(fs.readFileSync(path.join(__dirname, '../js/app/context_panel.js'), 'utf8'));
  const find = label => Array.from(w.document.querySelectorAll('.context-panel-modal button')).find(b => b.textContent === label);
  const text = () => w.document.querySelector('.context-panel-content').textContent;
  return { w, calls, fixture, find, text, close: () => w.close() };
}

test('opens from context status, displays bounded snapshots as text and does not execute operations', async t => {
  const s = setup(); t.after(s.close);
  s.w.document.getElementById('contextTokenSummary').click(); await settle();
  assert.equal(s.calls.overview.length, 1);
  assert.ok(s.text().includes('摘要待重建') || s.text().includes('待重建摘要'));
  assert.ok(s.text().includes('历史编辑或删除后重建'));
  assert.ok(s.text().includes('1,200'));
  assert.ok(s.text().includes('<script>untrusted summary</script>'));
  assert.equal(s.w.document.querySelectorAll('.context-panel-content img, .context-panel-content script').length, 0);
  assert.equal(s.calls.compact + s.calls.rebuild, 0);
  assert.deepEqual(Array.from(s.calls.overview[0].tools), ['read_file']);
  assert.equal(s.find('准备从本地重新同步…').disabled, true);
});

test('pagination, explicit refresh and round location preserve the selected session', async t => {
  const s = setup(); t.after(s.close);
  s.w.App.contextPanel.open(); await settle(); s.find('更早轮次').click(); await settle();
  assert.equal(s.calls.overview[1].offset, 40);
  s.find('刷新').click(); await settle(); assert.equal(s.calls.overview[2].offset, 0);
  s.find('<img src=x onerror=alert(1)>').click();
  assert.equal(s.calls.jumped[0].round, 50); assert.equal(s.calls.jumped[0].session_id, 's');
  assert.equal(s.w.document.querySelector('.context-panel-modal').hidden, true);
});

test('session switch aborts and discards a late response', async t => {
  const s = setup(); t.after(s.close); let resolve;
  s.w.API.getContextOverview = () => new Promise(r => { resolve = r; });
  s.w.App.contextPanel.open(); s.w.App.state.sessionId = 'other'; s.w.App.contextPanel.noteSessionChanged();
  resolve(s.fixture); await settle();
  assert.equal(s.w.document.querySelector('.context-panel-modal').hidden, true);
  assert.ok(!s.text().includes('Test Model'));
});

test('error can be retried; empty session issues no request', async t => {
  const s = setup(); t.after(s.close); const original = s.w.API.getContextOverview;
  s.w.API.getContextOverview = async () => { throw new Error('offline'); };
  s.w.App.contextPanel.open(); await settle();
  assert.ok(s.w.document.querySelector('[role=alert]').textContent.includes('offline'));
  s.w.API.getContextOverview = original; s.find('刷新').click(); await settle();
  assert.equal(s.w.document.querySelector('[role=alert]').textContent, '');
  s.w.App.contextPanel.close(); s.w.App.state.sessionId = null; s.w.App.contextPanel.open(); await settle();
  assert.equal(s.calls.overview.length, 1); assert.ok(s.text().includes('请先开始会话'));
});

test('modal restores focus and inert state, Escape closes it, and operations reuse existing flows', async t => {
  const s = setup(); t.after(s.close); const trigger = s.w.document.getElementById('contextTokenSummary'); trigger.focus();
  s.w.App.contextPanel.open(); await settle();
  assert.equal(s.w.document.querySelector('main').inert, true);
  s.w.document.querySelector('.context-panel-modal').dispatchEvent(new s.w.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  assert.equal(s.w.document.activeElement, trigger); assert.ok(!s.w.document.querySelector('main').inert);
  s.w.App.contextPanel.open(); await settle(); s.find('手动压缩…').click();
  assert.equal(s.calls.compact, 1); assert.equal(s.w.document.querySelector('.context-panel-modal').hidden, true);
});

test('rebuild is confirmed and a stale confirmation cannot affect another session', async t => {
  const s = setup(); t.after(s.close); s.fixture.inspection.repair_required = false;
  s.fixture.inspection.summary_blocks[0].repair_required = false;
  s.w.App.contextPanel.open(); await settle(); s.find('准备从本地重新同步…').click();
  assert.equal(s.calls.rebuild, 0); assert.ok(s.calls.confirm);
  s.w.App.state.sessionId = 'other'; await s.calls.confirm.onConfirm(); assert.equal(s.calls.rebuild, 0);
});

test('running tasks disable compression and resynchronization without disabling inspection', async t => {
  const s = setup(); t.after(s.close); s.w.App.state.streaming = true;
  s.w.App.contextPanel.open(); await settle();
  assert.equal(s.find('手动压缩…').disabled, true);
  assert.equal(s.find('准备从本地重新同步…').disabled, true);
  assert.equal(s.find('刷新').disabled, false);
});

test('older backend is reported as requiring restart instead of claiming history is empty', async t => {
  const s = setup(); t.after(s.close);
  s.w.API.getContextOverview = async () => ({ request_context_tokens: 1234 });
  s.w.App.contextPanel.open(); await settle();
  assert.ok(s.text().includes('重启更新后的后端'));
  assert.ok(!s.text().includes('还没有上下文'));
});

test('API wrapper requests explicit inspection with selected tools and abort signal', async t => {
  const dom = new JSDOM('<body></body>', { url: 'http://localhost/ui/', runScripts: 'outside-only' });
  t.after(() => dom.window.close()); const w = dom.window, calls = [];
  w.fetch = async (url, options) => { calls.push({ url, options }); return { ok: true, json: async () => ({ state: 'succeed' }) }; };
  w.eval(fs.readFileSync(path.join(__dirname, '../js/api.js'), 'utf8'));
  const controller = new w.AbortController();
  await w.API.getContextOverview('session with space', ['read_file', 'read_document'], 40, controller.signal);
  const url = new URL(calls[0].url);
  assert.equal(url.searchParams.get('inspection'), 'true'); assert.equal(url.searchParams.get('round_offset'), '40');
  assert.equal(url.searchParams.get('session_id'), 'session with space');
  assert.deepEqual(url.searchParams.getAll('tool_names'), ['read_file', 'read_document']);
  assert.equal(calls[0].options.signal, controller.signal);
});
