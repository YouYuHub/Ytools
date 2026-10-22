const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');

const settle = () => new Promise(resolve => setImmediate(resolve));
const successful = () => ({ ok: true, redacted: true, server_id: 'Fake', elapsed_ms: 12, tool_count: 1,
  protocol_version: '2025-11-25', steps: [{ phase: 'initialize', ok: true }, { phase: 'list_tools', ok: true }],
  tools: [{ name: 'echo', description: 'Echo input', input_schema: { type: 'object', properties: { text: { type: 'string' } }, required: ['text'] } }], warnings: [] });

function setup() {
  const dom = new JSDOM('<body><main id="background"><button id="mcpDiagnosticsOpen">MCP 测试</button></main></body>', { runScripts: 'outside-only', pretendToBeVisual: true });
  const w = dom.window, calls = [], copied = [], downloads = [], toasts = [];
  w.TextEncoder = TextEncoder; w.Blob = Blob;
  w.URL.createObjectURL = blob => { downloads.push(blob); return 'blob:test'; };
  w.URL.revokeObjectURL = () => {};
  w.HTMLAnchorElement.prototype.click = () => {};
  Object.defineProperty(w.navigator, 'clipboard', { value: { writeText: async text => copied.push(text) } });
  w.App = { toast: text => toasts.push(text) };
  w.API = {
    async mcpDiagnosticServers(signal) { calls.push({ type: 'inventory', signal }); return { servers: [{ id: 'Fake' }, { id: 'Other' }] }; },
    async testMcpServer(payload, signal) { calls.push({ type: 'test', payload, signal }); return successful(); },
  };
  w.eval(fs.readFileSync(path.join(__dirname, '../js/app/mcp_schema_form.js'), 'utf8'));
  w.eval(fs.readFileSync(path.join(__dirname, '../js/app/mcp_diagnostics.js'), 'utf8'));
  const $ = selector => w.document.querySelector(selector);
  const btn = text => Array.from(w.document.querySelectorAll('.mcp-test-modal button')).find(node => node.textContent === text);
  async function open(server, tool) { w.App.mcpDiagnostics.open(server, tool); await settle(); }
  return { w, calls, copied, downloads, toasts, $, btn, open, close: () => w.close() };
}

test('opening only inventories configured services; connection probe never auto-executes a tool', async t => {
  const s = setup(); t.after(s.close);
  assert.equal(s.calls.length, 0);
  s.$('#mcpDiagnosticsOpen').click(); await settle();
  assert.equal(s.calls.length, 1); assert.equal(s.calls[0].type, 'inventory');
  s.btn('测试连接').click(); await settle();
  assert.equal(s.calls[1].payload.action, 'connect');
  assert.equal(s.calls[1].payload.tool_name, null);
  assert.equal(s.$('#mcpTestTool').value, 'echo');
  assert.match(s.$('.mcp-test-schema').textContent, /"required"/);
  assert.match(s.$('.mcp-test-status').textContent, /测试成功/);
  assert.equal(s.calls.length, 2);
});

test('extension services show authorization hints without initiating a browser tool call', async t => {
  const s = setup(); t.after(s.close);
  s.w.API.mcpDiagnosticServers = async () => ({ servers: [{ id: 'ExtensionFixture', browser_mode: 'extension', connection_label: '现有浏览器 · 扩展连接', connection_hint: '请在 Chrome 中选择目标标签页。' }, { id: 'Other' }] });
  await s.open('ExtensionFixture', 'browser_tabs');
  assert.match(s.$('#mcpTestConnectionHint').textContent, /Chrome.*目标标签页/);
  assert.match(s.$('#mcpTestConnectionHint').textContent, /120 秒.*常驻连接/);
  assert.equal(s.$('#mcpTestConnectionHint').hidden, false);
  assert.match(s.$('#mcpTestServer').selectedOptions[0].textContent, /现有浏览器/);
  assert.equal(s.calls.length, 0);
  s.$('#mcpTestServer').value = 'Other'; s.$('#mcpTestServer').dispatchEvent(new s.w.Event('change'));
  assert.equal(s.$('#mcpTestConnectionHint').hidden, true);
});

test('one Browser service diagnoses either runtime target without duplicating services or leaking target into tool arguments', async t => {
  const s = setup(); t.after(s.close);
  s.w.API.mcpDiagnosticServers = async () => ({ servers: [{ id: 'Browser', mcp_profiles: [{id:'isolated',label:'独立'},{id:'extension',label:'扩展',browser_mode:'extension'}], default_profile: 'isolated', connection_hint: '可分别保留两个连接。' }] });
  await s.open('Browser', 'echo');
  assert.equal(s.$('#mcpTestServer').options.length, 1);
  assert.equal(s.$('#mcpTestProfile').value, 'isolated');
  s.$('#mcpTestProfile').value = 'extension'; s.$('#mcpTestProfile').dispatchEvent(new s.w.Event('change'));
  assert.match(s.$('#mcpTestConnectionHint').textContent, /授权/);
  s.$('#mcpTestTool').value = 'echo'; s.$('#mcpTestTool').dispatchEvent(new s.w.Event('input'));
  s.$('#mcpTestArguments').value = '{"text":"hello"}';
  s.btn('执行测试（实际调用）').click(); await settle();
  assert.equal(s.calls[0].payload.server_id, 'Browser');
  assert.equal(s.calls[0].payload.mcp_profile, 'extension');
  assert.deepEqual(JSON.parse(JSON.stringify(s.calls[0].payload.arguments)), { text: 'hello' });
});

test('user manually selects tool, edits JSON and timeout, executes once using tool original name', async t => {
  const s = setup(); t.after(s.close); await s.open('Other', 'original_name');
  s.$('#mcpTestArguments').value = '{"text":"manual value"}'; s.$('#mcpTestTimeout').value = '60';
  s.btn('执行测试（实际调用）').click(); s.btn('执行测试（实际调用）').click(); await settle();
  assert.deepEqual(JSON.parse(JSON.stringify(s.calls[1].payload)), { server_id: 'Other', tool_name: 'original_name',
    action: 'call', arguments: { text: 'manual value' }, timeout_seconds: 60 });
  assert.equal(s.calls.length, 2);
});

test('generic profiles isolate tool selections, JSON drafts and schemas; locked profile is enforced in the selector', async t => {
  const s = setup(); t.after(s.close);
  s.w.API.mcpDiagnosticServers = async () => ({ servers: [{ id: 'Generic', default_profile: 'test',
    mcp_profiles: [{ id: 'test', label: '测试环境' }, { id: 'production', label: '生产环境' }] }] });
  await s.open('Generic', 'echo');
  s.$('#mcpTestArguments').value = '{"text":"test draft"}';
  s.$('#mcpTestProfile').value = 'production'; s.$('#mcpTestProfile').dispatchEvent(new s.w.Event('change'));
  assert.equal(s.$('#mcpTestTool').value, '');
  assert.equal(s.$('#mcpTestArguments').value, '{}');
  s.$('#mcpTestTool').value = 'echo'; s.$('#mcpTestTool').dispatchEvent(new s.w.Event('input'));
  s.$('#mcpTestArguments').value = '{"count":3}';
  s.w.API.testMcpServer = async payload => { s.calls.push({ payload }); return { ok: true, tools: [{ name: 'echo', input_schema: { type: 'object', properties: { count: { type: 'integer' } } } }] }; };
  s.btn('测试连接').click(); await settle();
  assert.match(s.$('.mcp-test-schema').textContent, /count/);
  s.$('#mcpTestProfile').value = 'test'; s.$('#mcpTestProfile').dispatchEvent(new s.w.Event('change'));
  assert.doesNotMatch(s.$('.mcp-test-schema').textContent, /count/);
  s.$('#mcpTestTool').value = 'echo'; s.$('#mcpTestTool').dispatchEvent(new s.w.Event('input'));
  assert.equal(s.$('#mcpTestArguments').value, '{"text":"test draft"}');
  s.w.App.mcpDiagnostics.close();
  s.w.API.mcpDiagnosticServers = async () => ({ servers: [{ id: 'Generic', default_profile: 'production', locked_profile: 'production',
    mcp_profiles: [{ id: 'test', label: '测试环境' }, { id: 'production', label: '生产环境' }] }] });
  await s.open('Generic');
  assert.equal(s.$('#mcpTestProfile').value, 'production');
  assert.equal(s.$('#mcpTestProfile option[value=test]').disabled, true);
});

test('invalid JSON, nonobjects, excessive argument size and timeout block the request', async t => {
  const s = setup(); t.after(s.close); await s.open('Fake', 'echo');
  for (const text of ['{wrong}', '[]', 'null', '42', JSON.stringify({ text: 'x'.repeat(66000) })]) {
    s.$('#mcpTestArguments').value = text; s.btn('执行测试（实际调用）').click(); await settle();
    assert.equal(s.calls.length, 1);
  }
  s.$('#mcpTestArguments').value = '{}'; s.$('#mcpTestTimeout').value = '301';
  s.btn('执行测试（实际调用）').click(); await settle(); assert.equal(s.calls.length, 1);
});

test('schema examples populate the editor without execution; switching service clears stale tools', async t => {
  const s = setup(); t.after(s.close); await s.open();
  s.btn('测试连接').click(); await settle(); s.btn('填入示例参数').click();
  assert.equal(s.$('#mcpTestArguments').value, '{\n  "text": ""\n}'); assert.equal(s.calls.length, 2);
  s.$('#mcpTestServer').value = 'Other'; s.$('#mcpTestServer').dispatchEvent(new s.w.Event('change'));
  assert.equal(s.$('#mcpTestTool').value, ''); assert.equal(s.$('#mcpTestTools').querySelectorAll('input[type=checkbox]').length, 0);
  assert.equal(s.btn('复制脱敏报告').disabled, true);
});

test('error classification and untrusted output render as text; tool error is not empty success', async t => {
  const s = setup(); t.after(s.close); await s.open('Fake', 'echo');
  s.w.API.testMcpServer = async () => ({ ok: false, error: { code: 'tool_error', phase: 'call_tool', message: '<img src=x onerror=alert(1)>' },
    result: { content: [], is_error: true }, tools: [] });
  s.btn('执行测试（实际调用）').click(); await settle();
  assert.match(s.$('.mcp-test-status').textContent, /工具执行失败/);
  assert.match(s.$('.mcp-test-error').textContent, /<img/); assert.equal(s.$('.mcp-test-result img'), null);
  assert.doesNotMatch(s.$('.mcp-test-result').textContent, /成功返回空结果/);
});

test('copy and download use only the backend redacted report, not the current raw editor', async t => {
  const s = setup(); t.after(s.close); await s.open('Fake', 'echo');
  s.$('#mcpTestArguments').value = '{"api_key":"LOCAL_SECRET"}';
  s.w.API.testMcpServer = async () => ({ ok: true, redacted: true, arguments: { api_key: '[REDACTED]' }, tools: [] });
  s.btn('执行测试（实际调用）').click(); await settle();
  s.btn('复制脱敏报告').click(); s.btn('下载脱敏报告').click(); await settle();
  assert.equal(s.copied.length, 1); assert.doesNotMatch(s.copied[0], /LOCAL_SECRET/);
  assert.match(s.copied[0], /REDACTED/); assert.equal(await s.downloads[0].text(), s.copied[0]);
});

test('cancellation aborts waiting and ignores a late result; user can retry', async t => {
  const s = setup(); t.after(s.close); await s.open('Fake', 'echo'); let complete, signal;
  s.w.API.testMcpServer = (_payload, abort) => { signal = abort; return new Promise(resolve => { complete = resolve; }); };
  s.btn('执行测试（实际调用）').click(); assert.equal(s.btn('停止等待').hidden, false);
  s.btn('停止等待').click(); assert.equal(signal.aborted, true);
  assert.equal(s.btn('执行测试（实际调用）').disabled, false);
  complete(successful()); await settle(); assert.equal(s.btn('复制脱敏报告').disabled, true);
  assert.match(s.$('.mcp-test-status').textContent, /已停止等待/);
  s.w.API.testMcpServer = async () => successful();
  s.btn('执行测试（实际调用）').click(); await settle(); assert.match(s.$('.mcp-test-status').textContent, /测试成功/);
});

test('close and reopen reject stale inventory and restore parent focus/inert state', async t => {
  const s = setup(); t.after(s.close); let complete;
  s.$('#mcpDiagnosticsOpen').focus();
  s.w.API.mcpDiagnosticServers = () => new Promise(resolve => { complete = resolve; });
  s.w.App.mcpDiagnostics.open('Other', 'old_tool');
  assert.equal(s.$('#background').inert, true);
  s.w.App.mcpDiagnostics.close(); assert.notEqual(s.$('#background').inert, true);
  assert.equal(s.w.document.activeElement.id, 'mcpDiagnosticsOpen');
  s.w.API.mcpDiagnosticServers = async () => ({ servers: [{ id: 'Fake' }, { id: 'Other' }] });
  await s.open('Fake', 'new_tool'); complete({ servers: [{ id: 'Other' }] }); await settle();
  assert.equal(s.$('#mcpTestServer').value, 'Fake'); assert.equal(s.$('#mcpTestTool').value, 'new_tool');
  s.$('#mcpTestArguments').dispatchEvent(new s.w.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  assert.equal(s.$('.mcp-test-modal').hidden, true); assert.equal(s.w.document.body.classList.contains('mcp-test-open'), false);
});

test('empty configuration and missing backend endpoint explain the next action', async t => {
  const s = setup(); t.after(s.close);
  s.w.API.mcpDiagnosticServers = async () => ({ servers: [] }); await s.open();
  assert.match(s.$('.mcp-test-status').textContent, /尚未配置/);
  s.w.App.mcpDiagnostics.close();
  s.w.API.mcpDiagnosticServers = async () => { throw Object.assign(new Error('Not Found'), { status: 404 }); };
  await s.open(); assert.match(s.$('.mcp-test-status').textContent, /重启/);
});

test('nested tools modal can make the diagnostic sibling inert; opening enables it and closing restores it', async t => {
  const s = setup(); t.after(s.close);
  s.$('.mcp-test-modal').inert = true; s.$('#background').inert = true;
  await s.open(); assert.equal(s.$('.mcp-test-modal').inert, false);
  s.w.App.mcpDiagnostics.close();
  assert.equal(s.$('.mcp-test-modal').inert, true); assert.equal(s.$('#background').inert, true);
});

test('invalid tool schema cannot crash example generation', async t => {
  const s = setup(); t.after(s.close); await s.open();
  const report = successful(); report.tools[0].schema_valid = false; report.tools[0].input_schema.required = 'invalid';
  s.w.API.testMcpServer = async () => report;
  s.btn('测试连接').click(); await settle(); s.btn('填入示例参数').click();
  assert.match(s.$('.mcp-test-status').textContent, /有效 Schema/);
});

test('discovery opens the generated form; submission uses its values and retains them after reports', async t => {
  const s = setup(); t.after(s.close); await s.open();
  s.btn('测试连接').click(); await settle();
  assert.equal(s.$('#mcpTestFormMode').getAttribute('aria-pressed'), 'true');
  assert.equal(s.$('#mcpTestArguments').closest('[hidden]') !== null, true);
  const input = s.$('.mcp-param-field textarea'); input.value = '表单测试';
  s.btn('执行测试（实际调用）').click();
  assert.equal(s.$('#mcpTestFormArguments').disabled, true);
  assert.equal(s.$('#mcpTestJsonMode').disabled, true);
  await settle();
  assert.deepEqual(JSON.parse(JSON.stringify(s.calls[2].payload.arguments)), { text: '表单测试' });
  assert.equal(s.$('.mcp-param-field textarea'), input);
  assert.equal(input.value, '表单测试'); assert.equal(s.$('#mcpTestFormArguments').disabled, false);
});

test('JSON and form synchronize unknown fields, false, zero, null and empty text; invalid JSON stays intact', async t => {
  const s = setup(); t.after(s.close); await s.open();
  const report = successful(); report.tools[0].input_schema.properties.count = { type: 'integer' };
  report.tools[0].input_schema.required.push('count');
  s.w.API.testMcpServer = async () => report;
  s.btn('测试连接').click(); await settle();
  // A blank required number must not trap the user in form mode.
  s.$('#mcpTestJsonMode').click(); assert.equal(s.$('#mcpTestArguments').closest('[hidden]'), null);
  const value = { text: '', count: 0, extension: { enabled: false, nullable: null } };
  s.$('#mcpTestArguments').value = JSON.stringify(value);
  s.$('#mcpTestFormMode').click(); s.$('#mcpTestJsonMode').click();
  assert.deepEqual(JSON.parse(s.$('#mcpTestArguments').value), value);
  s.$('#mcpTestArguments').value = '{invalid}'; s.$('#mcpTestFormMode').click();
  assert.equal(s.$('#mcpTestArguments').value, '{invalid}');
  assert.equal(s.$('#mcpTestJsonMode').getAttribute('aria-pressed'), 'true');
  assert.match(s.$('.mcp-test-status').textContent, /JSON 格式/);
  assert.equal(s.calls.length, 1); // Only inventory: custom API does not record the connection probe.
});

test('an invalid JSON draft before discovery can be fixed and converted without another connection', async t => {
  const s = setup(); t.after(s.close); await s.open('Fake', 'echo');
  s.$('#mcpTestArguments').value = '{invalid}'; s.btn('测试连接').click(); await settle();
  assert.equal(s.$('#mcpTestFormMode').disabled, false);
  assert.equal(s.$('#mcpTestJsonMode').getAttribute('aria-pressed'), 'true');
  s.$('#mcpTestArguments').value = '{"text":"fixed"}'; s.$('#mcpTestFormMode').click();
  assert.equal(s.$('.mcp-param-field textarea').value, 'fixed'); assert.equal(s.calls.length, 2);
  assert.doesNotMatch(s.$('.mcp-test-modes').nextElementSibling.textContent, /格式有误/);
});

test('drafts are isolated by service and tool, including unfinished form fields', async t => {
  const s = setup(); t.after(s.close); await s.open();
  const report = successful(); report.tools.push({ ...report.tools[0], name: 'second' });
  s.w.API.testMcpServer = async () => report;
  s.btn('测试连接').click(); await settle();
  const input = s.$('.mcp-param-field textarea'); input.value = 'echo draft';
  function select(name) { s.$('#mcpTestTool').value = name; s.$('#mcpTestTool').dispatchEvent(new s.w.Event('input')); }
  select('second'); assert.equal(s.$('.mcp-param-field textarea').value, '');
  s.$('.mcp-param-field textarea').value = 'second draft'; select('echo');
  assert.equal(s.$('.mcp-param-field textarea'), input); assert.equal(input.value, 'echo draft');
  s.$('#mcpTestServer').value = 'Other'; s.$('#mcpTestServer').dispatchEvent(new s.w.Event('change')); select('echo');
  assert.equal(s.$('#mcpTestArguments').value, '{}');
  s.$('#mcpTestServer').value = 'Fake'; s.$('#mcpTestServer').dispatchEvent(new s.w.Event('change')); select('echo');
  assert.equal(s.$('.mcp-param-field textarea').value, 'echo draft');
  // Even without a fresh tool list, editing JSON then reopening the cached form must use the new values.
  s.$('#mcpTestJsonMode').click(); s.$('#mcpTestArguments').value = '{"text":"new JSON"}'; s.$('#mcpTestFormMode').click();
  assert.equal(s.$('.mcp-param-field textarea').value, 'new JSON');
});

test('truncated or unsupported root schemas keep JSON available; forms enforce the 64KB request limit', async t => {
  const s = setup(); t.after(s.close); await s.open();
  const report = successful(); report.tools[0].schema_truncated = true; report.tools[0].input_schema = '[Schema 过长，请参考服务文档]';
  s.w.API.testMcpServer = async () => report;
  s.btn('测试连接').click(); await settle();
  assert.equal(s.$('#mcpTestFormMode').disabled, true);
  assert.equal(s.$('#mcpTestArguments').closest('[hidden]'), null);
  s.w.API.testMcpServer = async () => successful(); s.btn('测试连接').click(); await settle();
  s.$('.mcp-param-field textarea').value = '中'.repeat(23000);
  s.btn('执行测试（实际调用）').click(); await settle();
  assert.match(s.$('.mcp-test-status').textContent, /64KB/);
  assert.equal(s.$('#mcpTestFormArguments').disabled, false);
});

async function discoverMultiple(s, schemas = ['echo', 'second', 'third'].map(name => ({ name,
  description: name + ' description', input_schema: successful().tools[0].input_schema }))) {
  const report = { ...successful(), tool_count: schemas.length, tools: schemas };
  s.w.API.testMcpServer = async (payload, signal) => {
    s.calls.push({ type: 'test', payload, signal });
    return { ...report, tool_name: payload.tool_name, result: payload.action === 'call' ? { content: [{ type: 'text', text: payload.tool_name }] } : undefined };
  };
  await s.open(); s.btn('测试连接').click(); await settle();
  return report;
}
function pick(s, name, checked = true) {
  const check = Array.from(s.w.document.querySelectorAll('#mcpTestTools input')).find(el => el.dataset.toolName === name);
  check.checked = checked; check.dispatchEvent(new s.w.Event('change')); return check;
}
function editSelected(s, name) { s.w.document.querySelector(`[aria-label="编辑参数 ${name}"]`).click(); }

test('custom picker shows all tools, supports multiple checks, search and scroll/resize/outside/Escape dismissal', async t => {
  const s = setup(); t.after(s.close); await discoverMultiple(s);
  assert.equal(s.$('#mcpTestTool').hasAttribute('list'), false);
  s.$('#mcpTestChooseTools').click(); assert.equal(s.$('#mcpTestToolPicker').hidden, false);
  assert.equal(s.$('#mcpTestTools').querySelectorAll('input').length, 3);
  pick(s, 'second'); assert.equal(s.$('#mcpTestTool').value, 'second');
  assert.equal(s.$('.mcp-test-selected-tools').children.length, 2);
  assert.match(s.$('#mcpTestExecute').textContent, /2 项/);
  s.$('#mcpTestToolSearch').value = 'third'; s.$('#mcpTestToolSearch').dispatchEvent(new s.w.Event('input'));
  assert.equal(s.$('#mcpTestTools').querySelectorAll('input').length, 1);
  s.$('.mcp-test-body').dispatchEvent(new s.w.Event('scroll')); assert.equal(s.$('#mcpTestToolPicker').hidden, false); // Ignore an already-delivered position.
  s.$('.mcp-test-body').scrollTop = 1;
  s.$('.mcp-test-body').dispatchEvent(new s.w.Event('scroll')); assert.equal(s.$('#mcpTestToolPicker').hidden, true);
  s.$('#mcpTestChooseTools').click(); s.$('#mcpTestTools').dispatchEvent(new s.w.Event('scroll'));
  assert.equal(s.$('#mcpTestToolPicker').hidden, false); // Scrolling the list itself must be possible.
  s.w.dispatchEvent(new s.w.Event('resize')); assert.equal(s.$('#mcpTestToolPicker').hidden, true);
  s.$('#mcpTestChooseTools').click(); Object.defineProperty(s.w, 'scrollY', { value: 2 }); s.w.dispatchEvent(new s.w.Event('scroll'));
  assert.equal(s.$('#mcpTestToolPicker').hidden, true);
  s.$('#mcpTestChooseTools').click(); s.$('#mcpTestTimeout').dispatchEvent(new s.w.Event('pointerdown', { bubbles: true }));
  assert.equal(s.$('#mcpTestToolPicker').hidden, true);
  s.$('#mcpTestChooseTools').click(); s.$('#mcpTestToolSearch').dispatchEvent(new s.w.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  assert.equal(s.$('#mcpTestToolPicker').hidden, true); assert.equal(s.$('.mcp-test-modal').hidden, false);
  assert.equal(s.calls.length, 2); // Checking and editing never invokes business tools.
});

test('multiple tools use independent drafts, run sequentially and export all redacted results', async t => {
  const s = setup(); t.after(s.close); await discoverMultiple(s);
  s.$('.mcp-param-field textarea').value = 'first value'; pick(s, 'second');
  s.$('.mcp-param-field textarea').value = 'second value';
  editSelected(s, 'echo'); assert.equal(s.$('.mcp-param-field textarea').value, 'first value');
  s.$('#mcpTestExecute').click(); s.$('#mcpTestExecute').click(); await settle();
  const calls = s.calls.filter(call => call.payload && call.payload.action === 'call');
  assert.deepEqual(calls.map(call => [call.payload.tool_name, call.payload.arguments.text]), [['echo', 'first value'], ['second', 'second value']]);
  assert.equal(s.$('#mcpTestTool').value, 'echo'); assert.equal(s.$('.mcp-param-field textarea').value, 'first value');
  assert.equal(s.$('.mcp-test-result').querySelectorAll('.mcp-test-batch-result').length, 2);
  assert.match(s.$('.mcp-test-status').textContent, /成功 2 · 失败 0/);
  s.btn('复制脱敏报告').click(); await settle();
  const report = JSON.parse(s.copied[0]); assert.equal(report.action, 'batch_call'); assert.equal(report.completed_count, 2);
  assert.equal(report.ok, true); assert.equal(report.reports.length, 2);
  assert.equal(report.tools.length, 3); assert.ok(report.reports.every(item => !Object.hasOwn(item, 'tools')));
});

test('preflight catches an unfinished later tool before any business call and opens its editor', async t => {
  const s = setup(); t.after(s.close);
  await discoverMultiple(s, [successful().tools[0], { name: 'count', input_schema: { type: 'object', properties: { count: { type: 'integer' } }, required: ['count'] } }]);
  s.$('.mcp-param-field textarea').value = 'first'; pick(s, 'count'); editSelected(s, 'echo');
  s.$('#mcpTestExecute').click(); await settle();
  assert.equal(s.calls.length, 2); assert.equal(s.$('#mcpTestTool').value, 'count');
  assert.match(s.$('.mcp-test-status').textContent, /count.*整数/);
  s.$('.mcp-param-field input[type=number]').value = '0'; s.$('#mcpTestExecute').click(); await settle();
  assert.equal(s.calls.length, 4); assert.equal(s.calls[3].payload.arguments.count, 0);
});

test('batch stops on the first failure by default and optionally continues with other selected tools', async t => {
  const s = setup(); t.after(s.close); const report = await discoverMultiple(s); pick(s, 'second');
  const called = [];
  s.w.API.testMcpServer = async payload => {
    called.push(payload.tool_name);
    return payload.tool_name === 'echo' ? { ...report, ok: false, error: { code: 'tool_error', message: 'failed' } } : report;
  };
  s.$('#mcpTestExecute').click(); await settle();
  assert.deepEqual(called, ['echo']); assert.match(s.$('.mcp-test-status').textContent, /未执行 1/);
  s.$('#mcpTestContinueOnError').checked = true; s.$('#mcpTestExecute').click(); await settle();
  assert.deepEqual(called, ['echo', 'echo', 'second']); assert.match(s.$('.mcp-test-status').textContent, /成功 1 · 失败 1/);
});

test('cancelling a batch aborts its current request, keeps finished results and never starts remaining tools', async t => {
  const s = setup(); t.after(s.close); const report = await discoverMultiple(s); pick(s, 'second'); pick(s, 'third');
  const called = []; let finish, signal;
  s.w.API.testMcpServer = async (payload, abort) => {
    called.push(payload.tool_name);
    if (payload.tool_name === 'echo') return report;
    signal = abort; return new Promise(resolve => { finish = resolve; });
  };
  s.$('#mcpTestExecute').click(); await settle(); assert.deepEqual(called, ['echo', 'second']);
  s.btn('停止等待').click(); assert.equal(signal.aborted, true);
  finish(report); await settle(); assert.deepEqual(called, ['echo', 'second']);
  assert.equal(s.$('.mcp-test-result').querySelectorAll('.mcp-test-batch-result').length, 1);
  s.btn('复制脱敏报告').click(); await settle();
  const exported = JSON.parse(s.copied[0]); assert.equal(exported.cancelled, true); assert.equal(exported.completed_count, 1); assert.equal(exported.ok, false);
});

test('transport failure details stay out of exported batch reports; unknown names require explicit joining', async t => {
  const s = setup(); t.after(s.close); await discoverMultiple(s); pick(s, 'second');
  s.$('#mcpTestTool').value = 'manual'; s.$('#mcpTestTool').dispatchEvent(new s.w.Event('input'));
  s.$('#mcpTestExecute').click(); await settle(); assert.equal(s.calls.length, 2);
  assert.match(s.$('.mcp-test-status').textContent, /尚未加入测试/);
  s.$('#mcpTestAddTool').click(); assert.equal(s.$('.mcp-test-selected-tools').children.length, 3);
  s.$('#mcpTestArguments').value = '{"api_key":"PRIVATE_DRAFT"}';
  s.w.API.testMcpServer = async () => { throw new Error('HTTP 422 PRIVATE_DRAFT'); };
  s.$('#mcpTestExecute').click(); await settle(); s.btn('复制脱敏报告').click(); await settle();
  assert.match(s.$('.mcp-test-result').textContent, /HTTP 422 PRIVATE_DRAFT/);
  assert.doesNotMatch(s.copied[0], /PRIVATE_DRAFT/);
});

test('API wrapper sends selected server and exact JSON payload with abort signal', async t => {
  const dom = new JSDOM('<body></body>', { url: 'http://localhost/ui/', runScripts: 'outside-only' });
  t.after(() => dom.window.close()); const w = dom.window, calls = [];
  w.fetch = async (url, options) => { calls.push({ url, options }); return { ok: true, json: async () => ({ ok: true }) }; };
  w.eval(fs.readFileSync(path.join(__dirname, '../js/api.js'), 'utf8'));
  const controller = new w.AbortController();
  await w.API.mcpDiagnosticServers(controller.signal);
  await w.API.testMcpServer({ server_id: 'MCP 服务', action: 'call', tool_name: 'read original', arguments: { text: '中文' }, timeout_seconds: 5 }, controller.signal);
  assert.equal(new URL(calls[0].url).pathname, '/tools/diagnostics');
  assert.equal(calls[0].options.signal, controller.signal);
  assert.equal(calls[1].options.method, 'POST'); assert.equal(calls[1].options.signal, controller.signal);
  assert.deepEqual(JSON.parse(calls[1].options.body), { server_id: 'MCP 服务', action: 'call', tool_name: 'read original', arguments: { text: '中文' }, timeout_seconds: 5 });
});
