const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

// uploadRawBatch 并行语义：经 API.uploadSessionMedia 覆盖（uploadRaw 优先走
// global.YtoolsAuth.upload 桩，不碰真实 XHR）。
function setup(stub) {
  const window = {};
  const storage = { getItem: () => null, setItem() {}, removeItem() {} };
  window.YtoolsAuth = { upload: stub };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../js/api.js'), 'utf8'), {
    window, localStorage: storage, sessionStorage: storage, URLSearchParams,
    location: { origin: 'http://test.local', protocol: 'http:', pathname: '/ui/', search: '', hash: '' },
    fetch: async () => { throw new Error('no fetch in upload test'); },
  });
  return window.API;
}
const files = (...names) => names.map(name => ({ name }));

test('批量上传并行发出：3x60ms 耗时远小于串行累加，且 progress index 正确', async () => {
  let running = 0, maxConcurrent = 0;
  const seen = [];
  const API = setup(async (input, file, onProgress) => {
    running++; maxConcurrent = Math.max(maxConcurrent, running);
    onProgress({ phase: 'upload', percent: 50 });
    await new Promise(r => setTimeout(r, 60));
    running--;
    return { success: 1, failed: 0, results: [{ filename: file.name, status: 'success' }], upload_id: 'u-' + file.name };
  });
  const started = Date.now();
  const res = await API.uploadSessionMedia('s1', files('a.png', 'b.mp4', 'c.png'),
    (index, progress) => seen.push([index, progress.phase]));
  const elapsed = Date.now() - started;
  assert.equal(maxConcurrent, 3);
  assert.ok(elapsed < 150, `elapsed=${elapsed}ms`);
  assert.deepEqual(Array.from(res.results, r => r.filename), ['a.png', 'b.mp4', 'c.png']);
  assert.equal(res.success, 3);
  assert.equal(res.upload_id, 'u-c.png');
  assert.deepEqual(seen.map(s => s[0]).sort(), [0, 1, 2]);
});

test('完成顺序打乱时 results 仍按输入顺序组装', async () => {
  const delays = { 'slow.png': 80, 'mid.png': 40, 'fast.png': 10 };
  const API = setup(async (input, file) => {
    await new Promise(r => setTimeout(r, delays[file.name]));
    return { success: 1, failed: 0, results: [{ filename: file.name, status: 'success' }] };
  });
  const res = await API.uploadSessionMedia('s1', files('slow.png', 'mid.png', 'fast.png'), () => {});
  assert.deepEqual(Array.from(res.results, r => r.filename), ['slow.png', 'mid.png', 'fast.png']);
});

test('单个失败不影响其它：计数与 error 回调 index 正确', async () => {
  const phases = [];
  const API = setup(async (input, file) => {
    if (file.name === 'bad.png') throw new Error('保存失败: boom');
    return { success: 1, failed: 0, results: [{ filename: file.name, status: 'success' }] };
  });
  const res = await API.uploadSessionMedia('s1', files('a.png', 'bad.png', 'c.png'),
    (index, progress) => phases.push([index, progress.phase]));
  assert.equal(res.success, 2);
  assert.equal(res.failed, 1);
  assert.equal(res.results[1].status, 'failed');
  assert.equal(res.results[1].filename, 'bad.png');
  assert.match(res.results[1].message, /boom/);
  assert.deepEqual(phases, [[1, 'error']]);
});

test('AbortError 穿透整批', async () => {
  const API = setup(async () => {
    const err = new Error('上传已取消');
    err.name = 'AbortError';
    throw err;
  });
  await assert.rejects(
    API.uploadSessionMedia('s1', files('a.png', 'b.png'), () => {}),
    err => err.name === 'AbortError');
});

test('超过 10 个文件直接抛错且不发起任何上传', async () => {
  let calls = 0;
  const API = setup(async () => { calls++; return {}; });
  await assert.rejects(
    API.uploadSessionMedia('s1', files('1', '2', '3', '4', '5', '6', '7', '8', '9', '10', '11'), () => {}),
    /最多上传 10 个文件/);
  assert.equal(calls, 0);
});
