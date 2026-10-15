const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ToolApprovalState = require('../js/tool_approval_state.js');
class Node {
  constructor(tag, cls, text) { this.tagName = tag; this.className = cls || ''; this.textContent = text || ''; this.children = []; this.listeners = {}; this.dataset = {}; this.isConnected = true; this.hidden = false; }
  appendChild(node) { this.children.push(node); node.parentNode = this; }
  replaceChildren() { this.children = []; }
  remove() { this.parentNode.children.splice(this.parentNode.children.indexOf(this), 1); }
  closest() { return null; }
  addEventListener(type, fn) { this.listeners[type] = fn; }
  setAttribute(name, value) { this[name] = value; }
}
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
const a = (id = 'a') => ({ id, session_id: 's', agent_id: 'child', tool_call_id: 'call', state: 'pending', tool: 'run_command', args: { command: 'echo ok' }, root: 'C:/work', round: 2 });
function setup(overrides = {}) {
  const nodes = new Map(), calls = [], state = { sessionId: 's' };
  const API = { getPermissions: async () => ({ mode: 'ask', global_mode: 'ask', session_mode: null }),
    pendingApprovals: async () => ({ approvals: [] }), setPermissions: async (...args) => calls.push(args), ...overrides };
  const App = { state, toast() {}, el: (...args) => new Node(...args), $: selector => { if (!nodes.has(selector)) nodes.set(selector, new Node('div')); return nodes.get(selector); } };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../js/app/permissions.js'), 'utf8'), { window: { App }, App, API, ToolApprovalState, document: { querySelectorAll: () => [] } });
  return { App, nodes, calls, state };
}
function ui() { return { wrap: new Node('div'), approvalHost: new Node('div'), setApprovalWaiting() {} }; }
function buttons(ui) { return ui.approvalHost.children[0].children.filter(node => node.tagName === 'button'); }
const click = button => button.listeners.click({ stopPropagation() {} });

test('子任务顶部与内部按钮绑定同一项审批，同步禁用，任意一处可允许/拒绝', async () => {
  for (const decision of ['approve', 'deny']) {
    const result = deferred(), submitted = [];
    const { App } = setup({ decideApproval: async (item, value) => { submitted.push([item.id, value]); return result.promise; } });
    const inside = ui(), top = ui(), otherRound = ui();
    App.permissions.bindTool(inside, { sessionId: 's', agentId: 'child', toolCallId: 'call', round: 2 });
    App.permissions.bindAgent(top, { sessionId: 's', agentId: 'child', round: 2 });
    App.permissions.bindTool(otherRound, { sessionId: 's', agentId: 'child', toolCallId: 'call', round: 1 });
    App.permissions.receive({ phase: 'requested', approval: a() });
    assert.equal(inside.approvalHost.hidden, false); assert.equal(top.approvalHost.hidden, false); assert.equal(otherRound.approvalHost.hidden, true);
    const pending = click(buttons(decision === 'approve' ? top : inside)[decision === 'approve' ? 0 : 1]);
    assert.ok(buttons(top).every(button => button.disabled)); assert.ok(buttons(inside).every(button => button.disabled));
    await click(buttons(inside)[0]); result.resolve({}); await pending;
    assert.deepEqual(submitted, [['a', decision]]); assert.equal(inside.approvalHost.hidden, true); assert.equal(top.approvalHost.hidden, true);
  }
});

test('审批失败在两个标题栏显示错误，并恢复按钮供重试', async () => {
  const { App } = setup({ decideApproval: async () => { throw new Error('网络连接失败'); }, pendingApprovals: async () => ({ approvals: [a()] }) });
  const inside = ui(), top = ui();
  App.permissions.bindTool(inside, { sessionId: 's', agentId: 'child', toolCallId: 'call' });
  App.permissions.bindAgent(top, { sessionId: 's', agentId: 'child' });
  App.permissions.receive({ phase: 'requested', approval: a() });
  await click(buttons(top)[0]);
  for (const block of [inside, top]) {
    assert.ok(buttons(block).every(button => !button.disabled));
    assert.equal(block.approvalHost.children[0].children.at(-1).textContent, '网络连接失败');
  }
});

test('409 后同步已取消状态到原工具块，不新建卡片或继续等待', async () => {
  const item = a();
  const { App } = setup({ decideApproval: async () => { throw Object.assign(new Error('任务已取消'), { status: 409 }); },
    pendingApprovals: async () => ({ approvals: [], states: [{ ...item, state: 'cancelled', task_state: 'cancelled' }] }) });
  const inside = ui(), values = [], errors = [];
  inside.setApprovalWaiting = (waiting, outcome) => values.push([waiting, outcome]);
  inside.setApprovalError = text => errors.push(text);
  App.permissions.bindTool(inside, { sessionId: 's', agentId: 'child', toolCallId: 'call' });
  App.permissions.receive({ phase: 'requested', approval: item });
  await click(buttons(inside)[0]);
  assert.deepEqual(errors, ['任务已取消']);
  assert.equal(inside.approvalHost.hidden, true);
  assert.equal(values.at(-1)[0], null);
  assert.equal(values.at(-1)[1].state, 'cancelled');
});

test('后端恢复成功即重新加载并接入结果流，客户端过时的运行态不会漏接', async () => {
  const { App, state } = setup({ decideApproval: async () => ({ resumed: true }) });
  const opened = [], inside = ui();
  App.openSession = async sid => opened.push(sid);
  state.streaming = true; state.streamingSession = 's';
  App.permissions.bindTool(inside, { sessionId: 's', agentId: 'child', toolCallId: 'call' });
  App.permissions.receive({ phase: 'requested', approval: { ...a(), task_state: 'running' } });
  await click(buttons(inside)[0]);
  assert.deepEqual(opened, ['s']);
});

test('子任务一条已拒绝另一条待选择时，顶部等待状态以未决定项为准', async () => {
  const denied = { ...a('denied'), state: 'denied', tool_call_id: 'old' };
  const waiting = { ...a('waiting'), tool_call_id: 'new' };
  const { App } = setup({ pendingApprovals: async () => ({ approvals: [denied, waiting] }) });
  const top = ui(), seen = [];
  top.setApprovalWaiting = item => seen.push(item);
  App.permissions.bindAgent(top, { sessionId: 's', agentId: 'child' });
  await App.permissions.syncPending('s');
  assert.equal(seen.at(-1).id, 'waiting');
  assert.match(top.approvalHost.children[0].children[0].textContent, /已拒绝/);
});

test('聊天设置更改不即时保存；全局与会话分别提交，清除覆盖使用 null', async () => {
  const { App, nodes, calls } = setup({ getPermissions: async () => ({ mode: 'ask', global_mode: 'ask', session_mode: 'ask' }) });
  await App.permissions.loadSettings();
  nodes.get('#permissionGlobal').value = 'full_control'; nodes.get('#permissionSession').value = '';
  assert.equal(calls.length, 0);
  await App.permissions.saveSettings();
  assert.deepEqual(calls, [['full_control', null], [null, 's']]);
});

test('配置保存失败保留选择并显示错误，无响应假象不会被刷新覆盖', async () => {
  const { App, nodes } = setup({ setPermissions: async () => { throw new Error('网络连接失败'); } });
  await App.permissions.loadSettings(); nodes.get('#permissionGlobal').value = 'full_control';
  await assert.rejects(App.permissions.saveSettings(), /网络连接失败/);
  assert.equal(nodes.get('#permissionGlobal').value, 'full_control');
  assert.equal(nodes.has('#permissionPairing'), false);
  assert.match(nodes.get('#permissionStatus').textContent, /网络连接失败/);
});

test('切换会话后旧配置响应不会覆盖当前会话；未保存配置不能写入新会话', async () => {
  const response = deferred(), { App, state, nodes } = setup({ getPermissions: () => response.promise });
  const loading = App.permissions.loadSettings(); state.sessionId = 'new';
  response.resolve({ mode: 'ask', global_mode: 'full_control', session_mode: 'full_control' }); await loading;
  assert.notEqual(nodes.get('#permissionGlobal').value, 'full_control');
  await assert.rejects(App.permissions.saveSettings(), /尚未加载/);
});
