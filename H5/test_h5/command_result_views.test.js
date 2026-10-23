const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../js/app/messages.js'), 'utf8');
const start = source.indexOf('  function applyToolResult(');
const end = source.indexOf('  // ---------- 子任务块', start);
function render(name, result) {
  const context = {extractPathFromArgsValue: () => ''};
  vm.createContext(context);
  vm.runInContext(source.slice(start, end), context);
  let failed = false;
  const ui = {wrap: {querySelector: () => ({classList: {toggle: (_, value) => {failed = value;}}})},
    setOutput(value) {this.output = value;}, setFileTag() {}};
  context.applyToolResult(ui, name, JSON.stringify(result), null, {});
  return {output: ui.output, failed};
}

for (const name of ['run_command', 'poll_command', 'write_stdin', 'stop_command']) {
  test(name + ' 显示新增输出且不泄漏游标或重复正文', () => {
    const result = render(name, {message: 'status=running', status: 'running', output: '中文正文',
      next_cursor: 'internal-cursor', next_call: {tool: 'poll_command'}, has_more: false});
    assert.equal(result.failed, false);
    assert.equal(result.output.split('中文正文').length - 1, 1);
    assert.doesNotMatch(result.output, /internal-cursor|next_call/);
  });
}

test('输入回执和失败可见，已接受与写入中不误报失败', () => {
  for (const status of ['accepted', 'queued', 'writing']) {
    const result = render('write_stdin', {message: 'running', status: 'running', input_status: status});
    assert.equal(result.failed, false);
    assert.match(result.output, /输入：/);
  }
  const result = render('write_stdin', {message: 'running', status: 'running', input_status: 'failed', input_error: 'pipe closed'});
  assert.equal(result.failed, true);
  assert.match(result.output, /输入：失败/);
  assert.match(result.output, /pipe closed/);
});

test('日志重置及位置保存失败有提示；旧尾部结果继续兼容', () => {
  const result = render('poll_command', {message: 'completed', output_reset: true, output: 'reset-output',
    cursor_warning: '输出位置未保存；后续请传 next_cursor'});
  assert.match(result.output, /日志已重置/);
  assert.match(result.output, /输出位置未保存/);
  const legacy = render('poll_command', {message: 'running', output_tail: 'old-tail'});
  assert.match(legacy.output, /输出末尾/);
  assert.match(legacy.output, /old-tail/);
});
