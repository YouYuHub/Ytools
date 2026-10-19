const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

// 压缩块触发来源标签：后端新增 trigger_reason 时前端必须配中文标签，
// 否则该行静默不显示（REASON_LABELS 缺 key 即跳过）。
test('压缩触发来源标签覆盖 history_mutation', () => {
  const source = fs.readFileSync(path.join(__dirname, '../js/app/messages.js'), 'utf8');
  const start = source.indexOf('const REASON_LABELS = {');
  assert.ok(start >= 0);
  const block = source.slice(start, source.indexOf('};', start));
  for (const reason of ['auto', 'task', 'first_call', 'post', 'manual', 'history_mutation']) {
    assert.ok(block.includes(reason + ':'), '缺标签: ' + reason);
  }
});
