const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function render(status) {
  const source = fs.readFileSync(path.join(__dirname, '../js/app/messages.js'), 'utf8');
  const start = source.indexOf('  function applyToolResult(');
  const end = source.indexOf('  function isToolFailureResult(', start);
  const context = { isToolFailureResult: () => false, extractPathFromArgsValue: () => '',
    extractPathFromDiffText: () => '' };
  vm.createContext(context); vm.runInContext(source.slice(start, end), context);
  const ui = { setOutput(value) { this.output = value; }, setDiff() {}, setFileTag() {} };
  context.applyToolResult(ui, 'edit_file', JSON.stringify({ path: 'f', success: true, ...status }), { path: 'f' }, {});
  return ui.output;
}

test('文件版本状态可见，旧历史不添加误导提示', () => {
  assert.match(render({ history_recorded: true }), /已记录回退版本/);
  assert.match(render({ history_recorded: false, history_reason: 'file_too_large' }), /超出版本记录上限/);
  assert.match(render({ history_recorded: false, history_reason: 'record_failed' }), /版本未记录/);
  assert.match(render({ history_recorded: false, history_reason: 'history_quota_exceeded' }), /快照配额已满/);
  assert.match(render({ history_recorded: false, history_reason: 'history_version_limit' }), /版本数已达上限/);
  assert.doesNotMatch(render({ history_recorded: false, history_reason: 'unchanged' }), /无法/);
  assert.doesNotMatch(render({}), /回退版本|无法/);
  const appended = render({ created: false, action: 'append' });
  assert.match(appended, /追加文件/);
  assert.doesNotMatch(appended, /全文覆盖/);
});
