const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

test('终端列表、输出、停止、关闭和日志下载均携带编码后的会话 ID', async () => {
  const calls = [], window = {};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../js/api.js'), 'utf8'), {
    window, localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
    fetch: async (url, options) => {
      calls.push({ url, options });
      return { ok: true, json: async () => ({ jobs: [] }) };
    },
  });
  const sid = '会话 /&1', query = '?session_id=' + encodeURIComponent(sid), api = window.API;
  const options = { signal: new AbortController().signal };
  await api.listTerminalJobs(sid, options);
  await api.getTerminalJob('bg_one', sid, options);
  await api.operateTerminalJob('bg_one', 'stop', sid);
  await api.operateTerminalJob('bg_one', 'close', sid);
  assert.deepEqual(calls.map(call => new URL(call.url).pathname), [
    '/terminal/jobs', '/terminal/jobs/bg_one', '/terminal/jobs/bg_one/stop', '/terminal/jobs/bg_one/close',
  ]);
  assert.ok(calls.every(call => call.url.endsWith(query)));
  assert.equal(calls[0].options, options);
  assert.equal(calls[1].options, options);
  assert.equal(calls[2].options.method, 'POST');
  assert.ok(api.terminalLogUrl('bg_one', sid).endsWith('/log/download' + query));
  assert.throws(() => api.listTerminalJobs(null), /当前会话/);
  assert.throws(() => api.getTerminalJob('bg_one', ''), /当前会话/);
  assert.throws(() => api.operateTerminalJob('bg_one', 'stop', null), /当前会话/);
  assert.throws(() => api.terminalLogUrl('bg_one', null), /当前会话/);
});
