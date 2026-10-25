const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');
const settle = () => new Promise(resolve => setImmediate(resolve));
const names = ['browser_close','browser_resize','browser_console_messages','browser_handle_dialog','browser_emulate_media','browser_evaluate','browser_file_upload','browser_drop','browser_find','browser_fill_form','browser_press_key','browser_type','browser_navigate','browser_navigate_back','browser_network_requests','browser_network_request','browser_run_code_unsafe','browser_take_screenshot','browser_snapshot','browser_click','browser_drag','browser_hover','browser_select_option','browser_tabs','browser_wait_for'];

function setup(options = {}) {
  const dom = new JSDOM(`<body><button id="plus"></button><section id="toolModal" class="hidden">
    <div class="tool-modal-toolbar"><input id="toolSearchInput"><button id="toolRefresh"></button><button id="toolCollapseAll"></button></div>
    <div id="toolConfigContent"><div id="toolGroups"></div></div><span id="toolSelected"></span><small id="toolSelectionScope"></small>
    <button id="toolConfirm"></button><button id="toolCancel"></button><button id="toolModalClose"></button><button id="toolFollowGlobal"></button>
    </section><div id="toolTip"></div></body>`, { runScripts: 'outside-only', pretendToBeVisual: true });
  const w = dom.window, $ = selector => w.document.querySelector(selector), writes = [], diagnostics = [], toasts = [];
  const data = { servers: ['Other', 'Browser'], server_metadata: { Browser: { browser_mode: 'isolated', mcp_profiles: [{id:'isolated',label:'独立'},{id:'extension',label:'扩展'}], default_profile: 'isolated', connection_label: '独立浏览器 / 扩展连接' } },
    failed_servers: options.failed ? ['Browser'] : [],
    tools: [{ server_id: 'Other', original_name: 'echo', function: { name: 'echo', description: 'echo' } }].concat(options.failed ? [] : names.map(name => ({ server_id: 'Browser', original_name: name, function: { name, description: 'Description ' + name } }))) };
  const state = { sessionId: 'chat-a', tools: [], servers: [], failedServers: [], selectedTools: new Set(), draftTools: new Set(), toolSelectionOverridden: true };
  const App = w.App = { $, state, BUILTIN_SERVER_KEY: '__builtin__', BUILTIN_TOOLS: [], isBuiltinToolName: () => false,
    el(tag, cls, text) { const el = w.document.createElement(tag); if (cls) el.className = cls; if (text != null) el.textContent = text; return el; },
    toast: text => toasts.push(text), refreshContextTokenStats() {}, hideToolTip() {}, getChatModelVision: () => true,
    refreshChatModelLabel: async () => true, toolIconSvg: () => '<svg></svg>', plusBtn: $('#plus'),
    mcpDiagnostics: { open: (...args) => diagnostics.push(args) },
    closeToolModal() { if (App.toolModalSaving) return; $('#toolModal').classList.add('hidden'); App.onToolModalClosed(); }
  };
  ['toolModal', 'toolGroups', 'toolSearchInput', 'toolSelected', 'toolCollapseAll', 'toolRefresh', 'toolTip', 'toolFollowGlobal'].forEach(key => { App[key] = $('#' + key); });
  w.API = { listTools: async () => data,
    getToolSelection: async () => ({ effective_selection: options.selection || { Other: ['echo'], Browser: ['browser_tabs','browser_navigate'] }, is_overridden: true }),
    async updateToolSelection(...args) { writes.push(JSON.parse(JSON.stringify(args))); if (options.save) await options.save(); }
  };
  w.eval(fs.readFileSync(path.join(__dirname, '../js/app/tools.js'), 'utf8'));
  const open = async () => { await App.loadTools(); await App.loadToolSelection(); await App.openToolModal(); };
  const preset = id => $('[data-server-id="Browser"] [data-preset="' + id + '"]');
  return { w, $, App, state, data, writes, diagnostics, toasts, open, preset, close: () => w.close() };
}

test('single Browser exposes 25 unchanged tools and feature presets, with no second service or target selector', async t => {
  const s = setup(); t.after(s.close); await s.open();
  assert.equal(s.w.document.querySelectorAll('[data-server-id="Browser"] .tool-row').length, 25);
  assert.equal(s.$('.tool-browser-select'), null);
  assert.equal(s.$('[data-server-id="BrowserExisting"]'), null);
  assert.equal(s.$('[data-server-id="Browser"] .tool-group-count').textContent, '2 / 25');
  assert.match(s.preset('web').textContent, /17/); assert.match(s.preset('debug').textContent, /12/);
  assert.match(s.preset('all').textContent, /25/);
  assert.match(s.$('.tool-browser-note').textContent, /同时保留/);
});

test('presets only replace this server selection, can be customized or fully selected, never execute tools', async t => {
  const s = setup(); t.after(s.close); await s.open();
  s.preset('web').click(); assert.equal(s.state.draftTools.size, 18); // web 17 + echo
  assert.equal(s.preset('web').getAttribute('aria-pressed'), 'true');
  s.preset('debug').click(); assert.equal(s.state.draftTools.size, 13);
  assert.ok(s.state.draftTools.has('browser_snapshot') && s.state.draftTools.has('browser_network_request'));
  assert.equal(s.state.draftTools.has('browser_run_code_unsafe'), false);
  s.preset('all').click(); assert.equal(s.state.draftTools.size, 26);
  assert.ok(s.state.draftTools.has('browser_run_code_unsafe'));
  s.preset('none').click(); assert.deepEqual([...s.state.draftTools], ['echo']);
  s.$('[data-tool-name="browser_tabs"] .tool-row-check').click();
  assert.deepEqual([...s.state.draftTools].sort(), ['browser_tabs','echo']);
  assert.equal(s.writes.length, 0); assert.equal(s.diagnostics.length, 0);
  assert.equal(s.state.selectedTools.size, 3);
});

test('save submits one real service with raw names; diagnostics always route to that same service', async t => {
  const s = setup(); t.after(s.close); await s.open(); s.preset('all').click();
  s.$('[data-tool-name="browser_tabs"] .mcp-test-entry').click();
  assert.deepEqual(s.diagnostics, [['Browser','browser_tabs']]);
  s.$('#toolConfirm').click(); await settle();
  assert.deepEqual(Object.keys(s.writes[0][0]).sort(), ['Browser','Other']);
  assert.deepEqual(s.writes[0][0].Browser, names.slice().sort());
});

test('removed browser service and obsolete aliases are not mapped into current selections', async t => {
  const s = setup({ selection: { BrowserExisting: ['browser_tabs','browser_click'], Browser: ['old_browser_tabs','browser_navigate'] } });
  t.after(s.close); await s.open();
  assert.deepEqual([...s.state.selectedTools], ['browser_navigate']);
  assert.equal(s.writes.length, 0);
  s.$('#toolConfirm').click(); await settle();
  assert.deepEqual(s.writes[0][0], { Browser: ['browser_navigate'] });
});

test('cancel/reopen restores saved selection, and failed save keeps preset draft for retry', async t => {
  let fail = true;
  const s = setup({ save: () => { if (fail) throw new Error('failed'); } }); t.after(s.close); await s.open();
  s.preset('all').click(); s.$('#toolCancel').click(); await s.App.openToolModal();
  assert.equal(s.state.draftTools.size, 3);
  s.preset('debug').click(); s.$('#toolConfirm').click(); await settle();
  assert.equal(s.$('#toolModal').classList.contains('hidden'), false);
  assert.equal(s.state.selectedTools.size, 3); assert.equal(s.state.draftTools.size, 13);
  fail = false; s.$('#toolConfirm').click(); await settle();
  assert.deepEqual(s.writes[0], s.writes[1]);
});

test('search does not narrow preset application; future tools remain manually/full-selectable', async t => {
  const s = setup(); t.after(s.close);
  s.data.tools.push({ server_id: 'Browser', original_name: 'browser_future_tool', function: { name: 'browser_future_tool' } });
  await s.open();
  s.$('#toolSearchInput').value = 'browser_tabs'; s.$('#toolSearchInput').dispatchEvent(new s.w.Event('input'));
  assert.equal(s.w.document.querySelectorAll('.tool-row').length, 1);
  s.preset('all').click(); assert.equal(s.state.draftTools.size, 27);
  assert.ok(s.state.draftTools.has('browser_future_tool'));
});

test('failed discovery disables presets; independently configured future MCPs are never folded into Browser', async t => {
  const s = setup({ failed: true }); t.after(s.close); await s.open();
  assert.equal(s.preset('all').disabled, true);
  assert.match(s.$('[data-server-id="Browser"] .tool-group-empty').textContent, /连接失败/);
  const other = setup(); t.after(other.close);
  other.data.servers.push('CustomBrowser'); other.data.server_metadata.CustomBrowser = { browser_mode: 'extension' };
  other.data.tools.push({ server_id: 'CustomBrowser', original_name: 'browser_other', function: { name: 'browser_other' } });
  await other.open(); other.preset('all').click();
  assert.equal(other.$('[data-server-id="CustomBrowser"] .tool-row').dataset.toolName, 'browser_other');
  assert.equal(other.state.draftTools.has('browser_other'), false);
});
