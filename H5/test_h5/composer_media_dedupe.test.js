const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

// 主输入框附件去重：addPendingMediaFiles 凭 prepareMediaFile 附带的 hash 判重，
// 相同文件第二次添加被跳过并提示，不占用 6 个名额。
const source = () => fs.readFileSync(path.join(__dirname, '../js/app/media.js'), 'utf8');

function harness() {
  const src = source();
  const toasts = [];
  let renders = 0;
  const context = {
    Array, Object, String, Number, JSON, Promise, Uint8Array,
    state: { pendingMedia: [] },
    MAX_PENDING_MEDIA: 6,
    prepareMediaFile: async (file) => {
      const buf = new Uint8Array(await file.arrayBuffer());
      let hex = '';
      for (const b of buf) hex += b.toString(16).padStart(2, '0');
      return { id: 'm-' + file.name, file, name: file.name, kind: 'audio', hash: file.size + ':t:' + hex };
    },
    App: { getChatModelVision: () => true },
    toast: (msg) => toasts.push(msg),
    FileReader: function () { throw new Error('audio path must not touch FileReader'); },
    renderComposerAttachments: () => { renders += 1; },
  };
  vm.createContext(context);
  vm.runInContext(src.slice(src.indexOf('async function addPendingMediaFiles('), src.indexOf('function renderComposerAttachments(')), context);
  return { context, toasts, renders: () => renders };
}

const audio = (bytes, name) => new File([bytes], name, { type: 'audio/mp3' });

test('相同字节第二次添加被跳过并提示，不占名额', async () => {
  const h = harness();
  await h.context.addPendingMediaFiles([audio('voice-1', 'a.mp3')]);
  await h.context.addPendingMediaFiles([audio('voice-1', 'b.mp3')]);
  assert.equal(h.context.state.pendingMedia.length, 1);
  assert.equal(h.context.state.pendingMedia[0].name, 'a.mp3');
  assert.ok(h.toasts.some((m) => m.includes('已添加过相同的文件')), h.toasts.join('|'));
});

test('不同字节正常添加（同名也不误判）', async () => {
  const h = harness();
  await h.context.addPendingMediaFiles([audio('voice-1', 'a.mp3'), audio('voice-2', 'a.mp3')]);
  assert.equal(h.context.state.pendingMedia.length, 2);
});

test('无哈希条目永不判重（旧桩/超大文件兼容）', async () => {
  const h = harness();
  h.context.prepareMediaFile = async (file) => ({ id: 'x', file, name: file.name, kind: 'audio', hash: '' });
  await h.context.addPendingMediaFiles([audio('same', 'a.mp3'), audio('same', 'b.mp3')]);
  assert.equal(h.context.state.pendingMedia.length, 2);
});
