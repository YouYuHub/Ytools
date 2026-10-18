const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Node {
  constructor(tag, cls, text) {
    this.tagName = tag; this.children = []; this.textContent = text || ''; this.fields = new Map(); this.dataset = {};
    const classes = new Set((cls || '').split(' '));
    this.classList = { add: (...xs) => xs.forEach(x => classes.add(x)), remove: (...xs) => xs.forEach(x => classes.delete(x)),
      contains: x => classes.has(x), toggle: (x, on) => (on === undefined ? !classes.has(x) : on) ? classes.add(x) : classes.delete(x) };
  }
  appendChild(n) { this.children.push(n); n.parentNode = this; }
  addEventListener() {}
  querySelector(selector) { if (!this.fields.has(selector)) this.fields.set(selector, new Node('span')); return this.fields.get(selector); }
  remove() { this.removed = true; }
}

function block() {
  const source = fs.readFileSync(path.join(__dirname, '../js/app/messages.js'), 'utf8');
  const start = source.indexOf('  function buildToolBlock('), end = source.indexOf('  // ---------- 展示用 diff', start);
  const context = { state: { sessionId: 's' }, el: (...args) => new Node(...args),
    App: { isBuiltinToolName: () => true, toolIconSvg: () => '', toolDisplayName: () => '运行命令' },
    FormatUtils: { prettyJson: JSON.stringify }, clearInterval, setInterval };
  vm.createContext(context); vm.runInContext(source.slice(start, end), context);
  const ui = context.buildToolBlock('run_command', { toolCallId: 'call' });
  return { ui, output: ui.wrap.children[1].children[3] };
}
const approval = { id: 'a', state: 'pending', args: { command: 'Get-Date' } };

test('审批取消或失效明确结束等待，完成的工具输出始终优先', () => {
  const { ui, output } = block();
  ui.setApprovalWaiting(approval);
  assert.match(output.textContent, /等待你的允许/);
  ui.setApprovalWaiting(null, { ...approval, state: 'cancelled', task_state: 'cancelled', execution_state: null });
  assert.equal(output.textContent, '任务已取消，本次工具未执行。');
  ui.setOutput('2026年10月14日'); ui.finish();
  ui.setApprovalWaiting(null, { ...approval, state: 'completed' });
  assert.equal(output.textContent, '2026年10月14日');
});

test('提交失败不误报执行中，重试产生新审批后重新等待确认', () => {
  const { ui, output } = block();
  ui.setApprovalWaiting(approval); ui.setApprovalError('网络连接失败');
  ui.setApprovalWaiting(approval);
  assert.equal(output.textContent, '确认失败：网络连接失败');
  ui.setApprovalWaiting({ ...approval, id: 'fresh' });
  assert.match(output.textContent, /等待你的允许/);
  ui.setApprovalWaiting(null, { ...approval, state: 'approved' });
  assert.equal(output.textContent, '已允许，等待执行结果…');
});

test('结果未知和已执行的取消任务不会显示未执行', () => {
  for (const execution_state of ['executing', 'completed']) {
    const { ui, output } = block();
    ui.setApprovalWaiting(null, { ...approval, state: 'cancelled', task_state: 'failed', execution_state });
    assert.match(output.textContent, /任务异常结束/);
    assert.doesNotMatch(output.textContent, /未执行/);
  }
});

test('同批一条允许一条拒绝，拒绝项立即停止转圈，互不覆盖结果', () => {
  const allowed = block(), denied = block();
  allowed.ui.setApprovalWaiting(approval);
  denied.ui.setApprovalWaiting({ ...approval, id: 'b' });
  denied.ui.setApprovalWaiting(null, { id: 'b', state: 'denied' });
  assert.equal(denied.output.textContent, '已拒绝，本次工具不会执行。');
  assert.equal(denied.ui.wrap.children[0].children[0].querySelector('.tool-status-icon').removed, true);
  assert.equal(denied.ui.wrap.classList.contains('is-waiting'), false);
  allowed.ui.setApprovalWaiting(null, { ...approval, state: 'approved' });
  allowed.ui.executing(); allowed.ui.setOutput('执行结果 A'); allowed.ui.finish();
  assert.equal(allowed.output.textContent, '执行结果 A');
  assert.equal(denied.output.textContent, '已拒绝，本次工具不会执行。');
});

test('刷新时查询到已拒绝但未消费的审批，同样立即停止转圈', () => {
  const { ui, output } = block();
  ui.setApprovalWaiting({ ...approval, state: 'denied', task_state: 'running' });
  assert.equal(output.textContent, '已拒绝，本次工具不会执行。');
  assert.equal(ui.wrap.children[0].children[0].querySelector('.tool-status-icon').removed, true);
});
