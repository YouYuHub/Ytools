const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { stripTypeScriptTypes } = require('node:module');
const QuoteUtils = require('../js/quote_utils.js');

class Node {
  constructor(cls = '', text = '') {
    this.children = []; this.textContent = text; this.handlers = {};
    this.classList = { add() {}, remove() {} };
  }
  appendChild(n) { this.children.push(n); }
  addEventListener(type, fn) { this.handlers[type] = fn; }
  set innerHTML(value) { this.children = []; }
}
function setup(options = {}) {
  const calls = [], revoked = [];
  const state = { streaming: true, streamingSession: 's', sessionId: 's', pendingMedia: [],
    pendingQuotes: [], pendingDocs: [], pendingQueue: [], injectedPending: [], steerMessage: null };
  const context = { state, input: { value: '请继续' }, composer: new Node(), pendingOutbox: new Node(),
    QuoteUtils, toast() {}, el: (tag, cls, text) => new Node(cls, text),
    URL: { revokeObjectURL: url => revoked.push(url) },
    App: { autosize() {}, renderComposerAttachments() {}, renderComposerQuotes() {}, stopVoiceInput() {},
      async send(message) { calls.push(['send', message]); } },
    API: {
      async uploadSessionMedia(session, files) {
        calls.push(['upload', session, files]);
        return { results: files.map((file, i) => options.failUpload ? { status: 'failed', filename: file.name } :
          { status: 'success', kind: 'image', stored_name: file.name, media_ref: 'media://' + file.name }) };
      },
      async injectMessage(session, content, opts) {
        calls.push(['inject', session, content, opts]);
        if (options.inject) return options.inject(context, session, content, opts);
        return { ok: true };
      },
      async cancelInjectMessage(session, text, id) { calls.push(['cancel', session, text, id]); return { ok: options.cancelled !== false }; },
      async uploadSessionFiles(session, files) {
        calls.push(['uploadFiles', session, files.map(f => f.name)]);
        if (options.failDocs) return { success: 0, failed: files.length, results: files.map(f => ({ status: 'failed', filename: f.name })) };
        return { success: files.length, failed: 0, results: files.map(f => ({ status: 'success', filename: f.name })) };
      },
      async saveOutbox(session, entry) { calls.push(['saveOutbox', session, entry]); return { entry }; },
      async removeOutbox(session, ids) { calls.push(['removeOutbox', session, ids]); return { removed: ids.length }; },
    },
  };
  vm.createContext(context);
  const chat = stripTypeScriptTypes(fs.readFileSync(path.join(__dirname, '../src/app/chat.ts'), 'utf8'));
  vm.runInContext(chat.slice(chat.indexOf('  function buildUserContent('), chat.indexOf('  // V2 文件版本链')), context);
  const composer = stripTypeScriptTypes(fs.readFileSync(path.join(__dirname, '../src/app/composer.ts'), 'utf8'));
  vm.runInContext(composer.slice(composer.indexOf('  async function submitSteerOrQueue('), composer.indexOf('  // ---------- 弹层锚定工具')), context);
  return { context, state, calls, revoked, submit: mode => context.submitSteerOrQueue(mode || 'steer') };
}
function addPicture(state, name = 'image.png') {
  state.pendingMedia.push({ kind: 'image', file: { name }, name, objUrl: 'blob:' + name });
}
test('带图及引用引导先上传，再投递 media://，不等任务结束', async () => {
  const { state, calls, context, submit } = setup();
  addPicture(state); state.pendingQuotes.push({ id: 'quote-ui', text: '引用原文' });
  await submit();
  assert.deepEqual(calls.map(c => c[0]), ['upload', 'inject']);
  const inject = calls[1];
  assert.equal(inject[2][1].image_url.url, 'media://image.png');
  assert.equal(inject[3].quotes[0].text, '引用原文');
  assert.equal(context.input.value, ''); assert.equal(state.pendingMedia.length, 0);
  assert.equal(state.steerMessage, null); assert.equal(state.injectedPending.length, 1);
});
test('连续两条同文本图片消息独立跟踪，消费一条不清掉另一条', async () => {
  const { state, context, submit, revoked } = setup();
  addPicture(state, 'first.png'); await submit();
  const first = state.injectedPending[0];
  context.input.value = '请继续'; addPicture(state, 'second.png'); await submit();
  assert.equal(state.injectedPending.length, 2);
  context.App.clearInjectedPending('s', first.id);
  assert.equal(state.injectedPending.length, 1);
  assert.equal(state.injectedPending[0].media[0].media_ref, 'media://second.png');
  assert.deepEqual(revoked, ['blob:first.png']);
});
test('纯图片引导可按 ID 撤回并恢复附件及引用', async () => {
  const { state, context, submit, calls } = setup();
  context.input.value = ''; addPicture(state); state.pendingQuotes.push({ text: '原文' });
  await submit(); const id = state.injectedPending[0].id;
  const row = context.pendingOutbox.children[0];
  await row.children[row.children.length - 1].handlers.click();
  assert.equal(calls.at(-1)[3], id);
  assert.equal(state.injectedPending.length, 0); assert.equal(state.pendingMedia.length, 1);
  assert.equal(state.pendingQuotes[0].text, '原文');
});
test('已消费导致撤回失败时不恢复附件，不重复发送', async () => {
  const { state, context, submit } = setup({ cancelled: false });
  addPicture(state); await submit(); const row = context.pendingOutbox.children[0];
  await row.children[row.children.length - 1].handlers.click();
  assert.equal(state.pendingMedia.length, 0); assert.equal(state.injectedPending.length, 1);
});
test('SSE 消费先于 HTTP 响应时不复活待注入条目', async () => {
  const { state, submit } = setup({ inject(context, session, content, options) {
    context.App.clearInjectedPending(session, options.messageId); return { ok: true };
  } });
  addPicture(state); await submit(); assert.equal(state.injectedPending.length, 0);
});
test('上传失败保留附件和引用供重试，不投递残缺消息', async () => {
  const { state, submit, calls } = setup({ failUpload: true });
  addPicture(state); state.pendingQuotes.push({ text: '不要丢失' }); await submit();
  assert.equal(calls.length, 1); assert.equal(state.injectedPending.length, 0);
  assert.equal(state.steerMessage.media[0].file.name, 'image.png');
  assert.equal(state.steerMessage.quotes[0].text, '不要丢失');
});
test('上传期间任务结束时立即回退普通发送', async () => {
  const { state, calls, submit } = setup({ inject(context) { context.state.streaming = false; return { ok: false }; } });
  addPicture(state); await submit();
  assert.deepEqual(calls.map(c => c[0]), ['upload', 'inject', 'saveOutbox', 'removeOutbox', 'send']);
  assert.equal(calls[4][1].media[0].media_ref, 'media://image.png');
  assert.equal(state.steerMessage, null);
});
test('队列模式先上传换引用再登记（刷新后可恢复发送）', async () => {
  const { state, calls, submit } = setup(); addPicture(state); await submit('queue');
  assert.deepEqual(calls.map(c => c[0]), ['upload', 'saveOutbox']);
  assert.equal(state.pendingQueue.length, 1);
  assert.equal(state.pendingQueue[0].media[0].media_ref, 'media://image.png');
  assert.equal(calls[1][2].kind, 'queue');
});
test('队列文档先上传再登记，失败仅内存暂存', async () => {
  const { state, calls, submit, context } = setup();
  state.pendingDocs.push({ id: 'd1', sessionId: 's', filename: 'a.pdf', file: { name: 'a.pdf' }, staged: true });
  context.input.value = '';
  await submit('queue');
  assert.deepEqual(calls.map(c => c[0]), ['uploadFiles', 'saveOutbox']);
  assert.equal(state.pendingQueue[0].docs[0].uploaded, true);
  assert.equal(state.pendingDocs.length, 0);
  const bad = setup({ failDocs: true });
  bad.state.pendingDocs.push({ id: 'd2', sessionId: 's', filename: 'b.pdf', file: { name: 'b.pdf' }, staged: true });
  bad.context.input.value = '';
  await bad.submit('queue');
  assert.deepEqual(bad.calls.map(c => c[0]), ['uploadFiles']);
  assert.equal(bad.state.pendingQueue.length, 1);
});
test('无源文件的恢复条目只删除不放回', async () => {
  const { state, context } = setup();
  state.pendingQueue.push({ id: 'o1', sessionId: 's', text: 'hi',
    media: [{ media_ref: 'media://a.png', name: 'a.png', kind: 'image' }], quotes: [], docs: [] });
  let toasted = '';
  context.toast = m => { toasted = m; };
  context.cancelPendingMessage({ kind: 'queue', message: state.pendingQueue[0] });
  assert.equal(state.pendingQueue.length, 0);
  assert.equal(context.input.value, '请继续');
  assert.match(toasted, /不可恢复编辑/);
});
test('任务已结束时提交回退普通发送（不静默丢弃）', async () => {
  const { state, context, calls, submit } = setup();
  state.streaming = false;
  await submit('queue');
  assert.deepEqual(calls.map(c => c[0]), ['send']);
  assert.equal(state.pendingQueue.length, 0);
});
test('暂存行缩略图：内存用本地、恢复用远端', async () => {
  const { context } = setup();
  context.App.resolveMediaSrc = (ref, sid) => 'https://cdn/' + ref.slice(8) + '?s=' + sid;
  context.App.openMediaPreview = () => {};
  const item = new Node();
  context.appendOutboxThumbs(item, { sessionId: 's', media: [
    { kind: 'image', name: 'a.png', dataUrl: 'data:image/png;base64,AAA' },
    { kind: 'image', name: 'b.png', media_ref: 'media://b.png' },
    { kind: 'video', name: 'c.mp4', media_ref: 'media://c.mp4' },
    { kind: 'image', name: 'd.png' },
  ] });
  assert.equal(item.children.length, 2);
  assert.equal(item.children[0].src, 'data:image/png;base64,AAA');
  assert.equal(item.children[1].src, 'https://cdn/b.png?s=s');
});
test('打开会话时恢复持久化暂存并核销已消费引导', async () => {
  const { state, context } = setup();
  // 本地残留：一条已消费（后端队列无，历史有）、一条仍在后端队列
  state.injectedPending.push(
    { id: 'm-consumed', sessionId: 's', text: 'alpha', media: [], quotes: [] },
    { id: 'm-live', sessionId: 's', text: 'beta', media: [], quotes: [] },
  );
  context.App.loadOlderState = { records: [
    { kind: 'user', content: 'alpha' }, { kind: 'assistant', content: 'r' },
  ] };
  context.App.contentToPlainText = c => (typeof c === 'string' ? c : '');
  context.API.getOutbox = async session => ({
    entries: [{ id: 'o1', kind: 'queue', text: 'queued', media: [], quotes: [] }],
    injected: [{ id: 'm-live', text: 'beta', quotes: [] }],
  });
  await context.App.loadSessionOutbox('s');
  assert.equal(state.injectedPending.length, 1);
  assert.equal(state.injectedPending[0].id, 'm-live');
  assert.equal(state.pendingQueue.length, 1);
  assert.equal(state.pendingQueue[0].id, 'o1');
});
test('后端丢失的引导转回暂存槽（不静默丢消息）', async () => {
  const { state, context } = setup();
  state.injectedPending.push(
    { id: 'm-lost', sessionId: 's', text: 'gamma', media: [], quotes: [] },
  );
  context.App.loadOlderState = { records: [{ kind: 'user', content: 'other' }] };
  context.App.contentToPlainText = c => (typeof c === 'string' ? c : '');
  context.API.getOutbox = async session => ({ entries: [], injected: [] });
  let toasted = '';
  context.toast = m => { toasted = m; };
  await context.App.loadSessionOutbox('s');
  assert.equal(state.injectedPending.length, 0);
  assert.equal(state.steerMessage.text, 'gamma');
  assert.equal(state.steerMessage._converted, true);
  assert.match(toasted, /手动发送/);
  // 转存条目不自动派发（防误判重复发送），只响应手动发送
  state.streaming = false;
  await context.flushPendingMessages('s');
  assert.equal(state.steerMessage.text, 'gamma');
});
test('任务运行中延迟对账（不等消费事件）', async () => {
  const { state, context } = setup();
  state.injectedPending.push(
    { id: 'm-run', sessionId: 's', text: 'gamma', media: [], quotes: [] },
  );
  context.App.loadOlderState = { records: [{ kind: 'user', content: 'other' }] };
  context.App.contentToPlainText = c => (typeof c === 'string' ? c : '');
  context.API.getOutbox = async session => ({ entries: [], injected: [], running: true });
  await context.App.loadSessionOutbox('s');
  assert.equal(state.injectedPending.length, 1);
  assert.equal(state.steerMessage, null);
});
test('消费事件顺带清除同 id 暂存行（误转存残留）', async () => {
  const { state, context } = setup();
  state.steerMessage = { id: 'm1', sessionId: 's', text: 'hi', media: [], quotes: [] };
  state.pendingQueue.push({ id: 'm2', sessionId: 's', text: 'q', media: [], quotes: [] });
  context.App.clearInjectedPending('s', 'm1');
  assert.equal(state.steerMessage, null);
  assert.equal(state.pendingQueue.length, 1);
  context.App.clearInjectedPending('s', 'm2');
  assert.equal(state.pendingQueue.length, 0);
});
test('injected 查询失败（null）时跳过对账不转存', async () => {
  const { state, context } = setup();
  state.injectedPending.push(
    { id: 'm-x', sessionId: 's', text: 'gamma', media: [], quotes: [] },
  );
  context.App.loadOlderState = { records: [{ kind: 'user', content: 'other' }] };
  context.App.contentToPlainText = c => (typeof c === 'string' ? c : '');
  context.API.getOutbox = async session => ({ entries: [], injected: null });
  await context.App.loadSessionOutbox('s');
  assert.equal(state.injectedPending.length, 1);
  assert.equal(state.steerMessage, null);
});
test('抑制只作用于被抑制会话，其他会话派发不受影响', async () => {
  const { state, context, calls } = setup();
  state.streaming = false;
  state.sessionId = 'b';
  state.suppressFlushSession = 'a';
  state.pendingQueue.push({ id: 'o1', sessionId: 'b', text: 'go', media: [], quotes: [] });
  await context.flushPendingMessages('b');
  assert.equal(calls.filter(c => c[0] === 'send').length, 1);
  assert.equal(state.suppressFlushSession, 'a');
  state.sessionId = 'a';
  state.pendingQueue.push({ id: 'o2', sessionId: 'a', text: 'held', media: [], quotes: [] });
  await context.flushPendingMessages('a');
  assert.equal(calls.filter(c => c[0] === 'send').length, 1);
  assert.equal(state.suppressFlushSession, null);
  assert.equal(state.pendingQueue.length, 1);
  await context.flushPendingMessages('a');
  assert.equal(calls.filter(c => c[0] === 'send').length, 2);
});
test('派发引导时标记来源（失败重试放回引导槽）', async () => {
  const { state, context, calls } = setup();
  state.streaming = false;
  state.steerMessage = { sessionId: 's', text: 'go', media: [], quotes: [] };
  await context.flushPendingMessages('s');
  assert.equal(state.steerMessage, null);
  assert.equal(calls[0][0], 'send');
  assert.equal(calls[0][1]._originKind, 'steer');
});

test('墓碑：已发送 id 不再从侧车恢复，并补删侧车', async () => {
  const { state, context, calls } = setup();
  const store = {};
  context.sessionStorage = {
    getItem: k => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
  };
  context.API.getOutbox = async () => ({ entries: [{ id: 'ghost1', kind: 'queue', text: '已发送内容' }], injected: [] });
  context.markOutboxSent('s', 'ghost1');
  await context.App.loadSessionOutbox('s');
  assert.equal(state.pendingQueue.length, 0, '墓碑条目不恢复');
  assert.ok(calls.some(c => c[0] === 'removeOutbox' && c[2][0] === 'ghost1'), '补删侧车');
});

test('行发送按钮双击只派发一次', async () => {
  const { state, context, calls } = setup();
  state.streaming = false;
  state.pendingQueue.push({ id: 'q1', text: '排队消息', media: [], quotes: [], docs: [], sessionId: 's' });
  context.renderPendingOutbox();
  const item = context.pendingOutbox.children[0];
  const sendBtn = item.children.find(n => n.handlers && n.handlers.click && n.textContent === '发送');
  assert.ok(sendBtn, '找到发送按钮');
  await sendBtn.handlers.click();
  await sendBtn.handlers.click();
  assert.equal(calls.filter(c => c[0] === 'send').length, 1, '只发送一次');
  assert.equal(state.pendingQueue.length, 0, '内存已取走');
});

test('恢复重入守卫：并发打开只拉取一次', async () => {
  const { context } = setup();
  let fetches = 0;
  context.API.getOutbox = async () => { fetches++; return { entries: [], injected: [] }; };
  await Promise.all([context.App.loadSessionOutbox('s'), context.App.loadSessionOutbox('s')]);
  assert.equal(fetches, 1);
});
