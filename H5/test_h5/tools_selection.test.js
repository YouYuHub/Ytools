const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function setup(options = {}) {
  function node() {
    const classes = new Set();
    return { handlers: {}, disabled: false, attrs: {}, style: { setProperty() {} },
      classList: { add: c => classes.add(c), remove: c => classes.delete(c), contains: c => classes.has(c) },
      setAttribute(key, value) { this.attrs[key] = value; },
      addEventListener(key, handler) { this.handlers[key] = handler; },
      querySelector() { return node(); }, querySelectorAll() { return []; },
    };
  }
  const nodes = new Map();
  const $ = selector => { if (!nodes.has(selector)) nodes.set(selector, node()); return nodes.get(selector); };
  const state = { sessionId: options.global ? null : 'session-a',
    tools: [{ name: 'mcp_read_alias', originalName: 'read_file', serverId: 'Server' }],
    selectedTools: new Set(['read_file']), draftTools: new Set(['mcp_read_alias']),
    toolSelectionOverridden: false };
  const messages = [], writes = [];
  let closed = 0;
  const App = { $, state, BUILTIN_SERVER_KEY: '__builtin__', BUILTIN_TOOLS: [],
    isBuiltinToolName: name => name === 'read_file', toast: message => messages.push(message),
    refreshContextTokenStats() {}, hideToolTip() {},
    closeToolModal() { if (!App.toolModalSaving) { closed++; App.toolModal.classList.add('hidden'); } },
  };
  ['toolModal', 'toolGroups', 'toolSearchInput', 'toolSelected', 'toolCollapseAll',
    'toolRefresh', 'toolTip', 'toolFollowGlobal'].forEach(key => { App[key] = $('#' + key); });
  const API = {
    async updateToolSelection(...args) { writes.push(args); if (options.save) await options.save(...args); },
    async getToolSelection() { return options.selection || { effective_selection: {}, is_overridden: false }; },
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../js/app/tools.js'), 'utf8'), {
    window: { App, addEventListener() {} }, API,
  });
  return { App, state, $, messages, writes, closed: () => closed };
}

test('工具选择保存失败保留草稿、原生效选择和弹窗，允许重试', async () => {
  let fail = true;
  const x = setup({ save: async () => { if (fail) throw new Error('network failed'); } });
  await x.$('#toolConfirm').handlers.click();
  assert.deepEqual([...x.state.selectedTools], ['read_file']);
  assert.deepEqual([...x.state.draftTools], ['mcp_read_alias']);
  assert.equal(x.closed(), 0);
  assert.equal(x.App.toolModalSaving, false);
  assert.equal(x.$('#toolConfirm').disabled, false);
  assert.equal(x.$('#toolConfigContent').inert, false);
  assert.match(x.messages[0], /保存失败/);
  assert.equal(x.messages.some(s => s.startsWith('已保存')), false);
  fail = false;
  await x.$('#toolConfirm').handlers.click();
  assert.deepEqual([...x.state.selectedTools], ['mcp_read_alias']);
  assert.equal(x.closed(), 1);
  assert.equal(x.state.toolSelectionOverridden, true);
  assert.deepEqual(JSON.parse(JSON.stringify(x.writes[1][0])), { Server: ['mcp_read_alias'] });
});

test('慢速保存阻止重复提交，成功前不关闭弹窗或应用草稿', async () => {
  let finish;
  const x = setup({ save: () => new Promise(resolve => { finish = resolve; }) });
  const saving = x.$('#toolConfirm').handlers.click();
  assert.equal(x.App.toolModalSaving, true);
  assert.equal(x.$('#toolCancel').disabled, true);
  assert.equal(x.$('#toolModalClose').disabled, true);
  assert.equal(x.closed(), 0);
  assert.deepEqual([...x.state.selectedTools], ['read_file']);
  await x.$('#toolConfirm').handlers.click();
  assert.equal(x.writes.length, 1);
  finish(); await saving;
  assert.equal(x.closed(), 1);
  assert.equal(x.App.toolModalSaving, false);
});

test('新会话保存全局默认；已有会话全取消仍提交显式空选择', async () => {
  const global = setup({ global: true });
  await global.$('#toolConfirm').handlers.click();
  assert.equal(global.writes[0][1], undefined);
  assert.equal(global.state.toolSelectionOverridden, false);
  assert.match(global.messages[0], /新会话默认/);
  const session = setup(); session.state.draftTools.clear();
  await session.$('#toolConfirm').handlers.click();
  assert.equal(session.writes[0][1], 'session-a');
  assert.deepEqual(Object.keys(session.writes[0][0]), []);
  assert.equal(session.state.selectedTools.size, 0);
  assert.match(session.messages[0], /显式无工具模式/);
});

test('保存期间切换会话，响应不覆盖新会话的工具状态', async () => {
  let finish;
  const x = setup({ save: () => new Promise(resolve => { finish = resolve; }) });
  const saving = x.$('#toolConfirm').handlers.click();
  x.state.sessionId = 'session-b';
  x.state.selectedTools = new Set(['session-b-tool']);
  finish(); await saving;
  assert.equal(x.writes[0][1], 'session-a');
  assert.deepEqual([...x.state.selectedTools], ['session-b-tool']);
});

test('工具选择加载可等待，按服务把原名映射为当前 MCP 别名', async () => {
  const x = setup({ selection: { effective_selection: { Server: ['read_file'] }, is_overridden: true } });
  assert.equal(await x.App.loadToolSelection(), true);
  assert.deepEqual([...x.state.selectedTools], ['mcp_read_alias']);
  assert.equal(x.state.toolSelectionOverridden, true);
  assert.match(x.$('#toolSelectionScope').textContent, /独立工具选择/);
});
