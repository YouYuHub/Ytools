const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { JSDOM } = require('jsdom');

// ask_user 回答附件：弹窗内粘贴图片/音视频或按钮选择文件（含文档），缩略图
// 网格预览，可删除；提交时媒体/文档随回答文本一起发送（直接发送，不污染
// 主输入框暂存）。切片载入 renderAskModal（含附件辅助函数）与 handleAskSubmit。
const source = () => fs.readFileSync(path.join(__dirname, '../js/app/builtin.js'), 'utf8');

function mediaKindOf(name) {
  const ext = (String(name).split('.').pop() || '').toLowerCase();
  if (['png', 'jpg', 'jpeg', 'gif', 'webp'].includes(ext)) return 'image';
  if (['mp4', 'webm', 'mov'].includes(ext)) return 'video';
  if (['mp3', 'wav', 'ogg'].includes(ext)) return 'audio';
  return null;
}

function harness(questions) {
  const src = source();
  const dom = new JSDOM('<div id="askQuestions"></div>');
  const { document } = dom.window;
  const askQuestions = document.querySelector('#askQuestions');
  const sent = [];
  const toasts = [];
  const previews = [];
  let vision = true;
  const context = {
    document,
    Array, Number, JSON, Object, Math, String, RegExp, Promise, Uint8Array,
    URL: { createObjectURL: () => 'blob:test-obj', revokeObjectURL: () => {} },
    ClipboardMediaUtils: { mediaFiles: (data) => (data && data._files) || [] },
    App: {
      mediaKindOf,
      MAX_PENDING_MEDIA: 6,
      MAX_DOC_FILE_SIZE: 10 * 1024 * 1024,
      getChatModelVision: () => vision,
      setVision: (v) => { vision = v; },
      prepareMediaFile: async (file) => {
        const buf = new Uint8Array(await file.arrayBuffer());
        let hex = '';
        for (const b of buf) hex += b.toString(16).padStart(2, '0');
        return { id: 'm-' + file.name, file, name: file.name, kind: mediaKindOf(file.name), hash: file.size + ':t:' + hex };
      },
      openMediaPreview: (opts) => previews.push(opts),
    },
    state: { pendingAskQuestions: questions, pendingAskBlock: null, streaming: false, sessionId: 's', streamingSession: '' },
    el: (tag, cls, text) => {
      const n = document.createElement(tag);
      if (cls) n.className = cls;
      if (text != null) n.textContent = text;
      return n;
    },
    askQuestions,
    askSubmit: { disabled: false },
    toast: (msg) => toasts.push(msg),
    closeAskModal: () => {},
    submitAskAnswer: (text, block, media, docs) => sent.push({ text, block, media, docs }),
  };
  vm.createContext(context);
  vm.runInContext(src.slice(src.indexOf('function renderAskModal('), src.indexOf('function updateAskSubmitState(')), context);
  vm.runInContext(src.slice(src.indexOf('function updateAskSubmitState('), src.indexOf('function openAskModal(')), context);
  vm.runInContext(src.slice(src.indexOf('function handleAskSubmit('), src.indexOf('askSubmit.addEventListener')), context);
  context.renderAskModal(questions);
  return { dom, context, askQuestions, sent, toasts, previews };
}

const QS1 = [{ question: '选哪个方案？', options: ['A', 'B'] }];
const makeFile = (dom, name, size = 3) =>
  new dom.window.File([name + ':' + 'x'.repeat(size)], name, { type: 'application/octet-stream' });

async function drain(h) {
  for (let i = 0; i < 300 && (h.askQuestions._askPending || 0) > 0; i++) {
    await new Promise((resolve) => setImmediate(resolve));
  }
  assert.equal(h.askQuestions._askPending || 0, 0, '附件队列应在有限步内排空');
}

function chooseFiles(h, files) {
  const input = h.askQuestions.querySelector('.ask-attach-input');
  Object.defineProperty(input, 'files', { value: files, configurable: true });
  input.dispatchEvent(new h.dom.window.Event('change'));
}

function pasteFiles(h, target, files) {
  const ev = new h.dom.window.Event('paste', { bubbles: true, cancelable: true });
  ev.clipboardData = { _files: files };
  target.dispatchEvent(ev);
  return ev;
}

test('渲染：附件区位于补充框之后，暂存初始化为空', (t) => {
  const h = harness(QS1);
  t.after(() => h.dom.window.close());
  const wrap = h.askQuestions.querySelector('.ask-attachments');
  assert.ok(wrap, '应渲染附件区');
  assert.ok(wrap.querySelector('.ask-attach-button'), '应有添加按钮');
  assert.ok(wrap.querySelector('.ask-attach-input'), '应有隐藏文件框');
  assert.ok(wrap.querySelector('.ask-attach-grid'), '应有预览网格');
  assert.equal(h.askQuestions._askMedia.length, 0);
  assert.equal(h.askQuestions._askDocs.length, 0);
  // 顺序：题目 → 补充说明 → 附件
  const kids = Array.from(h.askQuestions.children).map((n) => n.className);
  assert.ok(kids.indexOf('ask-supplement') < kids.indexOf('ask-attachments'));
});

test('文件选择：媒体与文档分流暂存并渲染预览', async (t) => {
  const h = harness(QS1);
  t.after(() => h.dom.window.close());
  chooseFiles(h, [makeFile(h.dom, 'a.png'), makeFile(h.dom, 'b.mp4'), makeFile(h.dom, 'c.pdf')]);
  await drain(h);
  assert.equal(h.askQuestions._askMedia.length, 2);
  assert.equal(h.askQuestions._askDocs.length, 1);
  assert.equal(h.askQuestions.querySelectorAll('.ask-attach-chip').length, 3);
  // 图片缩略图、视频徽标、文档扩展名徽标
  assert.ok(h.askQuestions.querySelector('.ask-attach-thumb'), '图片应有缩略图');
  const badges = Array.from(h.askQuestions.querySelectorAll('.ask-attach-badge')).map((n) => n.textContent);
  assert.ok(badges.includes('视频'), '视频应显示类别徽标');
  assert.ok(badges.includes('PDF'), '文档应显示扩展名徽标');
});

test('粘贴：有媒体时拦截并暂存，纯文字放行原生粘贴', async (t) => {
  const h = harness(QS1);
  t.after(() => h.dom.window.close());
  const target = h.askQuestions.querySelector('.ask-supplement-input');
  const ev = pasteFiles(h, target, [makeFile(h.dom, 'paste.png')]);
  assert.equal(ev.defaultPrevented, true, '媒体粘贴应拦截默认行为');
  await drain(h);
  assert.equal(h.askQuestions._askMedia.length, 1);
  assert.equal(h.askQuestions._askMedia[0].name, 'paste.png');

  const ev2 = pasteFiles(h, target, []);
  assert.equal(ev2.defaultPrevented, false, '纯文字粘贴不应拦截');
});

test('上限：媒体超 6 个、文档超 10 个、文档超 10MB 均拒绝并提示', async (t) => {
  const h = harness(QS1);
  t.after(() => h.dom.window.close());
  chooseFiles(h, Array.from({ length: 7 }, (_, i) => makeFile(h.dom, 'm' + i + '.png')));
  await drain(h);
  assert.equal(h.askQuestions._askMedia.length, 6);
  assert.ok(h.toasts.some((m) => m.includes('最多附加')), '应提示媒体上限');

  chooseFiles(h, Array.from({ length: 11 }, (_, i) => makeFile(h.dom, 'd' + i + '.pdf')));
  await drain(h);
  assert.equal(h.askQuestions._askDocs.length, 10);
  assert.ok(h.toasts.some((m) => m.includes('最多新增 10 个文档')), '应提示文档上限');

  chooseFiles(h, [makeFile(h.dom, 'big.pdf', 11 * 1024 * 1024)]);
  await drain(h);
  assert.equal(h.askQuestions._askDocs.length, 10, '超大文档不应加入（数量上限已满同样拒绝）');
});

test('上限：单个文档超 10MB 被拒绝并提示', async (t) => {
  const h = harness(QS1);
  t.after(() => h.dom.window.close());
  chooseFiles(h, [makeFile(h.dom, 'big.pdf', 11 * 1024 * 1024)]);
  await drain(h);
  assert.equal(h.askQuestions._askDocs.length, 0, '超大文档不应加入');
  assert.ok(h.toasts.some((m) => m.includes('10MB')), '应提示大小上限');
});

test('无视觉模型：暂存媒体时给出能力提示但不阻止', async (t) => {
  const h = harness(QS1);
  t.after(() => h.dom.window.close());
  h.context.App.setVision(false);
  chooseFiles(h, [makeFile(h.dom, 'a.png')]);
  await drain(h);
  assert.equal(h.askQuestions._askMedia.length, 1, '附件照常暂存');
  assert.ok(h.toasts.some((m) => m.includes('不支持视觉')), '应提示视觉能力');
});

test('删除：点击移除钮丢弃对应附件并刷新预览', async (t) => {
  const h = harness(QS1);
  t.after(() => h.dom.window.close());
  chooseFiles(h, [makeFile(h.dom, 'a.png'), makeFile(h.dom, 'b.pdf')]);
  await drain(h);
  assert.equal(h.askQuestions.querySelectorAll('.ask-attach-chip').length, 2);
  h.askQuestions.querySelector('.ask-attach-chip .ask-attach-remove').click();
  assert.equal(h.askQuestions._askMedia.length, 0);
  assert.equal(h.askQuestions._askDocs.length, 1);
  assert.equal(h.askQuestions.querySelectorAll('.ask-attach-chip').length, 1);
});

test('提交：媒体与文档随回答文本一起透传给发送编排', async (t) => {
  const h = harness(QS1);
  t.after(() => h.dom.window.close());
  chooseFiles(h, [makeFile(h.dom, 'a.png'), makeFile(h.dom, 'b.mp3'), makeFile(h.dom, 'c.pdf')]);
  await drain(h);
  h.context.handleAskSubmit();
  assert.equal(h.sent.length, 1);
  assert.ok(h.sent[0].text.startsWith('【回答模型提问】'));
  assert.equal(h.sent[0].media.map((m) => m.name).join(','), 'a.png,b.mp3');
  assert.equal(h.sent[0].docs.map((d) => d.name).join(','), 'c.pdf');
});

test('门禁：附件处理中禁止提交，排空后恢复', async (t) => {
  const h = harness(QS1);
  t.after(() => h.dom.window.close());
  h.askQuestions._askPending = 1;
  h.context.updateAskSubmitState();
  assert.equal(h.context.askSubmit.disabled, true, '处理中应禁用提交按钮');
  // 模拟竞态：按钮恰好可用但暂存未落定，提交仍被拦截并提示
  h.context.askSubmit.disabled = false;
  h.context.handleAskSubmit();
  assert.equal(h.sent.length, 0, '处理中提交应被拦截');
  assert.ok(h.toasts.some((m) => m.includes('附件处理中')), '应提示等待附件');
  h.askQuestions._askPending = 0;
  h.context.updateAskSubmitState();
  assert.equal(h.context.askSubmit.disabled, false, '排空后恢复可提交');
});

test('重开：再次渲染弹窗清空上一轮附件暂存', async (t) => {
  const h = harness(QS1);
  t.after(() => h.dom.window.close());
  chooseFiles(h, [makeFile(h.dom, 'a.png')]);
  await drain(h);
  assert.equal(h.askQuestions._askMedia.length, 1);
  h.context.renderAskModal(QS1);
  assert.equal(h.askQuestions._askMedia.length, 0);
  assert.equal(h.askQuestions._askDocs.length, 0);
  assert.equal(h.askQuestions.querySelectorAll('.ask-attach-chip').length, 0);
});

test('预览：点击图片缩略图打开媒体预览', async (t) => {
  const h = harness(QS1);
  t.after(() => h.dom.window.close());
  chooseFiles(h, [makeFile(h.dom, 'a.png')]);
  await drain(h);
  h.askQuestions.querySelector('.ask-attach-thumb').click();
  assert.equal(h.previews.length, 1);
  assert.equal(h.previews[0].type, 'image');
});

test('去重：相同图片多次粘贴只保留一份并提示', async (t) => {
  const h = harness(QS1);
  t.after(() => h.dom.window.close());
  const target = h.askQuestions.querySelector('.ask-supplement-input');
  const shot = makeFile(h.dom, 'shot.png');
  pasteFiles(h, target, [shot]);
  await drain(h);
  pasteFiles(h, target, [shot]);
  await drain(h);
  assert.equal(h.askQuestions._askMedia.length, 1);
  assert.equal(h.askQuestions.querySelectorAll('.ask-attach-chip').length, 1);
  assert.ok(h.toasts.some((m) => m.includes('已添加过相同的文件')), h.toasts.join('|'));
});

test('去重：同名不同字节不误判', async (t) => {
  const h = harness(QS1);
  t.after(() => h.dom.window.close());
  chooseFiles(h, [makeFile(h.dom, 'a.png'), makeFile(h.dom, 'b.png')]);
  await drain(h);
  assert.equal(h.askQuestions._askMedia.length, 2);
});
