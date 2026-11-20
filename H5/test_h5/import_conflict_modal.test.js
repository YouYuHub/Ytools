const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

// 导入冲突弹窗回归：后端挂起（如锁自等 30s）时确认/取消不能“点不动”。
// - 提交中点击取消/Esc/遮罩 → abort 请求并关闭弹窗；
// - 全部失败也要关闭弹窗并透出真实失败原因（不再只显示“冲突已跳过”）。
const source = () => fs.readFileSync(path.join(__dirname, '../js/app/history.js'), 'utf8');

function harness({ submitImpl }) {
  const src = source();
  const start = src.indexOf('async function confirmImport(');
  const end = src.indexOf('// 本地预览 jsonl', start);
  const toasts = [];
  let pendingImport = null;
  const modalClasses = new Set();
  const context = {
    AbortController,
    Promise, Object, String, Array, Number, JSON, Error,
    toast: t => toasts.push(t),
    API: { submitImport: (...args) => submitImpl(...args) },
    SessionUtils: { sanitizeSessionId: v => String(v) },
    App: {},
    importConflictSubmit: { disabled: false, textContent: '确认导入' },
    importConflictList: { querySelectorAll: () => [], innerHTML: '' },
    importConflictModal: {
      classList: { add: c => modalClasses.add(c), remove: c => modalClasses.delete(c) },
      setAttribute: () => {},
    },
    get pendingImport() { return pendingImport; },
    set pendingImport(v) { pendingImport = v; },
  };
  vm.createContext(context);
  // slice 含 confirmImport + importPackageFlow + closeImportConflictFn
  vm.runInContext(src.slice(start, end), context);
  return {
    context, toasts, modalClasses,
    open(ctxOverrides) {
      pendingImport = Object.assign({ preview: {}, file: { name: 'x.zip' }, resolve: () => {}, submitting: false }, ctxOverrides || {});
      return pendingImport;
    },
    current: () => pendingImport,
  };
}

test('全部失败：弹窗关闭、按钮恢复、toast 透出首个失败原因', async () => {
  const h = harness({
    submitImpl: async () => ({ imported: [], skipped: [], failed: [{ session_id: 'fresh', error: '获取文件锁超时（30.0s）：import-fresh.lock' }] }),
  });
  const ctx = h.open();
  await h.context.confirmImport();
  assert.ok(h.modalClasses.has('hidden'), '失败后弹窗必须关闭');
  assert.equal(h.current(), null, 'pendingImport 已清空，可重新发起导入');
  assert.equal(h.context.importConflictSubmit.disabled, false);
  assert.equal(h.context.importConflictSubmit.textContent, '确认导入');
  assert.ok(h.toasts.some(t => t.includes('导入失败') && t.includes('获取文件锁超时')), h.toasts.join('|'));
});

test('提交等待中点取消：abort 请求 → 弹窗关闭且状态复位', async () => {
  let signalSeen = null;
  const h = harness({
    submitImpl: (_file, _strategy, _decisions, signal) => new Promise((_resolve, reject) => {
      signalSeen = signal;
      signal.addEventListener('abort', () => {
        const err = new Error('上传已取消'); err.name = 'AbortError'; reject(err);
      });
    }),
  });
  h.open();
  const running = h.context.confirmImport();
  // 模拟等待期间用户点击取消按钮
  h.context.closeImportConflictFn();
  assert.ok(signalSeen && signalSeen.aborted, '取消应中止提交请求');
  const outcome = await running;
  assert.equal(outcome.status, 'cancelled');
  assert.ok(h.modalClasses.has('hidden'), '取消后弹窗关闭');
  assert.equal(h.current(), null);
  assert.equal(h.context.importConflictSubmit.disabled, false);
  assert.equal(h.context.importConflictSubmit.textContent, '确认导入');
});

test('重复点击确认：submitting 期间不重复提交', async () => {
  let calls = 0;
  let release;
  const h = harness({
    submitImpl: () => { calls++; return new Promise(r => { release = r; }); },
  });
  h.open();
  const first = h.context.confirmImport();
  await h.context.confirmImport();
  assert.equal(calls, 1);
  release({ imported: [], skipped: [{ session_id: 'a' }], failed: [] });
  await first;
  assert.equal(h.current(), null, '全跳过也关闭弹窗');
});

test('成功导入路径不受影响：outcome=imported 且正常关闭', async () => {
  const h = harness({
    submitImpl: async () => ({ imported: [{ session_id: 'ok', collision: false }], skipped: [], failed: [] }),
  });
  h.context.App.loadSessions = async () => {};
  h.context.App.openSession = async () => true;
  h.open();
  const outcome = await h.context.confirmImport();
  assert.equal(outcome.status, 'imported');
  assert.ok(h.modalClasses.has('hidden'));
  assert.ok(h.toasts.some(t => t.includes('已导入 1 个会话')));
});
