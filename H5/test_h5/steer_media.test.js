const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
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
    },
  };
  vm.createContext(context);
  const chat = fs.readFileSync(path.join(__dirname, '../js/app/chat.js'), 'utf8');
  vm.runInContext(chat.slice(chat.indexOf('  function buildUserContent('), chat.indexOf('  // V2 文件版本链')), context);
  const composer = fs.readFileSync(path.join(__dirname, '../js/app/composer.js'), 'utf8');
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
  assert.deepEqual(calls.map(c => c[0]), ['upload', 'inject', 'send']);
  assert.equal(calls[2][1].media[0].media_ref, 'media://image.png');
  assert.equal(state.steerMessage, null);
});
test('队列模式仍等任务完成，不立即上传或注入', async () => {
  const { state, calls, submit } = setup(); addPicture(state); await submit('queue');
  assert.equal(calls.length, 0); assert.equal(state.pendingQueue.length, 1);
});
