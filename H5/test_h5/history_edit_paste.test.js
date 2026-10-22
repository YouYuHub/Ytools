const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');
const { mediaFiles } = require('../js/clipboard_media_utils');
const settle = () => new Promise(resolve => setImmediate(resolve));
const kindOf = name => /\.(png|jpe?g|webp|gif|bmp|tiff?|ico)$/i.test(name) ? 'image'
  : /\.mp4$/i.test(name) ? 'video' : /\.mp3$/i.test(name) ? 'audio' : null;

test('clipboard screenshots without usable names get an image filename and preserve bytes', async () => {
  const file = new File(['image bytes'], 'clipboard', { type: 'image/png' });
  const result = mediaFiles({ items: [{ kind: 'file', type: 'image/png', getAsFile: () => file }] }, kindOf);
  assert.equal(result.length, 1);
  assert.match(result[0].name, /^clipboard-.*\.png$/);
  assert.equal(result[0].type, 'image/png');
  assert.equal(await result[0].text(), 'image bytes');
  const jpeg = new File(['jpeg'], '', { type: 'image/jpeg' });
  assert.match(mediaFiles({ files: [jpeg] }, kindOf)[0].name, /\.jpg$/);
});

test('clipboard items and files are not duplicated; files-only, named media and text are handled', () => {
  const file = new File(['x'], 'photo.png', { type: 'image/png' });
  const data = { items: [{ kind: 'file', getAsFile: () => file }], files: [file] };
  assert.deepEqual(mediaFiles(data, kindOf), [file]);
  assert.deepEqual(mediaFiles({ files: [file] }, kindOf), [file]);
  assert.deepEqual(mediaFiles({ items: [{ kind: 'string', type: 'text/plain' },
    { kind: 'file', getAsFile: () => null }], files: [] }, kindOf), []);
  assert.deepEqual(mediaFiles(null, kindOf), []);
  assert.deepEqual(mediaFiles({ files: [new File(['text'], 'note.txt')] }, kindOf), []);
  const video = new File(['v'], 'clip.mp4', { type: 'video/mp4' });
  assert.equal(mediaFiles({ files: [video] }, kindOf)[0], video);
});

function setup(t, { insert = false, content = 'Original message' } = {}) {
  const dom = new JSDOM('<body><div class="msg msg-user"><div class="msg-bubble">Original message</div></div></body>',
    { runScripts: 'outside-only', pretendToBeVisual: true });
  const w = dom.window, calls = { prepared: [], plans: [], toasts: [], created: [], revoked: [] };
  t.after(() => w.close());
  w.App = {
    state: { sessionId: 'session-1', pendingMedia: [{ id: 'composer-draft' }] },
    MAX_PENDING_MEDIA: 6, MAX_DOC_FILE_SIZE: 10 * 1024 * 1024, mediaKindOf: kindOf,
    async prepareMediaFile(file) {
      calls.prepared.push(file);
      return { file, name: file.name, kind: kindOf(file.name) };
    },
    getChatModelVision: () => true, resolveMediaSrc: ref => '/media/' + ref.slice(8),
    openMediaPreview() {}, toast: text => calls.toasts.push(text),
    confirmEditResend: plan => calls.plans.push(plan), autosize() {},
  };
  w.URL.createObjectURL = file => { calls.created.push(file); return 'blob:edit/' + calls.created.length; };
  w.URL.revokeObjectURL = url => calls.revoked.push(url);
  for (const file of ['quote_utils.js', 'clipboard_media_utils.js']) {
    w.eval(fs.readFileSync(path.join(__dirname, '../js', file), 'utf8'));
  }
  const source = fs.readFileSync(path.join(__dirname, '../js/app/messages.js'), 'utf8');
  const helpers = source.slice(source.indexOf('  function extractUserMediaItems('),
    source.indexOf('  /**', source.indexOf('  function contentToPlainText(')));
  const editor = source.slice(source.indexOf('  function beginUserMessageEdit('),
    source.indexOf('  function appendStaticAssistant('));
  w.eval(`window.EditTest = (function () {
    function el(tag, cls, text) {
      const node = document.createElement(tag); node.className = cls || '';
      if (text != null) node.textContent = text; return node;
    }
    ${helpers}
    ${editor}
    return { begin: beginUserMessageEdit, exit: exitUserMessageEdit };
  })();`);
  const msg = w.document.querySelector('.msg');
  msg._editData = { content, round: 3, quotes: [] };
  w.EditTest.begin(msg, insert);
  const panel = msg.querySelector('.msg-edit-bubble'), textarea = panel.querySelector('textarea');
  const file = (name = 'image.png') => new w.File(['image bytes'], name, { type: 'image/png' });
  function paste(files = [file()], text = '') {
    const e = new w.Event('paste', { bubbles: true, cancelable: true });
    Object.defineProperty(e, 'clipboardData', { value: {
      items: files.map(f => ({ kind: 'file', type: f.type, getAsFile: () => f })),
      files, getData: () => text,
    } });
    textarea.dispatchEvent(e); return e;
  }
  function choose(files) {
    const input = panel.querySelector('input[type=file]');
    Object.defineProperty(input, 'files', { configurable: true, value: files });
    input.dispatchEvent(new w.Event('change'));
  }
  const send = panel.querySelector('.msg-edit-buttons .msg-edit-send');
  return { w, msg, panel, textarea, file, paste, choose, send, calls };
}

test('historical editor stages pasted screenshots beside existing media without touching composer draft', async t => {
  const s = setup(t, { content: [{ type: 'text', text: 'Original message' },
    { type: 'image_url', image_url: { url: 'media://existing.png' } }] });
  assert.equal(s.paste().defaultPrevented, true);
  assert.equal(s.send.disabled, true);
  await settle();
  const media = s.panel._getMedia();
  assert.equal(media.length, 2);
  assert.equal(media[0].media_ref, 'media://existing.png');
  assert.match(media[1].file.name, /^clipboard-.*\.png$/);
  assert.equal(s.panel.querySelectorAll('.msg-edit-chip-thumb').length, 2);
  assert.equal(s.w.App.state.pendingMedia[0].id, 'composer-draft');
  assert.equal(s.calls.plans.length, 0); // No upload/send before confirmation.
  assert.equal(s.send.disabled, false);
  assert.equal(s.paste([], 'plain text').defaultPrevented, false);
  s.panel.querySelector('.msg-edit-media-row .msg-edit-chip:last-child .msg-edit-chip-remove').click();
  assert.equal(s.panel._getMedia().length, 1);
  assert.deepEqual(s.calls.revoked, ['blob:edit/1']);
});

test('image-only historical resend and inserted conversation pass staged files to the existing send flow', async t => {
  for (const insert of [false, true]) {
    const s = setup(t, { insert });
    s.textarea.value = '';
    s.paste(); await settle(); s.send.click();
    if (!insert) {
      assert.equal(s.calls.plans.length, 0);
      s.panel.querySelector('.msg-edit-confirm .msg-edit-send').click();
    }
    assert.equal(s.calls.plans.length, 1);
    const plan = s.calls.plans[0];
    assert.equal(plan.mode, insert ? 'insert' : 'regen');
    assert.equal(plan.text, '');
    assert.equal(plan.media.length, 1);
    assert.equal(plan.media[0].file.size, 11);
    s.w.EditTest.exit(s.msg);
    assert.deepEqual(s.calls.revoked, ['blob:edit/1']);
    assert.equal(plan.media[0].file.size, 11); // Revoking the preview doesn't discard upload bytes.
  }
});

test('rapid pastes and file selection share a queue and cannot exceed the attachment limit', async t => {
  const s = setup(t);
  s.paste([s.file('one.png'), s.file('two.png'), s.file('three.png')]);
  s.choose([s.file('four.png'), s.file('five.png')]);
  s.paste([s.file('six.png'), s.file('seven.png')]);
  await settle();
  assert.equal(s.panel._getMedia().length, 6);
  assert.equal(s.calls.prepared.length, 6);
  assert.equal(s.send.disabled, false);
  assert.ok(s.calls.toasts.some(text => text.includes('最多新增 6')));
});

test('cancel during image processing discards late results and restores the original bubble', async t => {
  const s = setup(t); let resolve;
  s.w.App.prepareMediaFile = file => new Promise(r => { resolve = () => r({ file, name: file.name, kind: 'image' }); });
  s.paste(); await settle();
  s.panel.querySelector('.msg-edit-buttons .msg-edit-cancel').click();
  s.panel.querySelector('.msg-edit-confirm .msg-edit-send').click();
  assert.equal(s.msg.querySelector('.msg-edit-bubble'), null);
  assert.equal(s.msg.querySelector('.msg-bubble').style.display, '');
  resolve(); await settle();
  assert.equal(s.calls.created.length, 0);
  assert.equal(s.calls.plans.length, 0);
});

test('session switch during processing never creates a preview in the old editor', async t => {
  const s = setup(t); let resolve;
  s.w.App.prepareMediaFile = file => new Promise(r => { resolve = () => r({ file, name: file.name, kind: 'image' }); });
  s.paste(); await settle();
  s.w.App.state.sessionId = 'another-session';
  resolve(); await settle();
  assert.equal(s.calls.created.length, 0);
  assert.equal(s.panel._getMedia().length, 0);
});

test('preparation failures retain successful items and allow a later paste retry', async t => {
  const s = setup(t), prepare = s.w.App.prepareMediaFile;
  s.w.App.prepareMediaFile = file => file.name === 'bad.png' ? Promise.reject(new Error('decode failed')) : prepare(file);
  s.paste([s.file('good.png'), s.file('bad.png')]); await settle();
  assert.equal(s.panel._getMedia().length, 1);
  assert.equal(s.panel.querySelectorAll('.msg-edit-chip-thumb').length, 1);
  assert.equal(s.send.disabled, false);
  assert.ok(s.calls.toasts.some(text => text.includes('附件处理失败')));
  s.paste([s.file('retry.png')]); await settle();
  assert.equal(s.panel._getMedia().length, 2);
});

test('pasting after a resend confirmation invalidates its stale attachment snapshot', async t => {
  const s = setup(t);
  s.send.click();
  assert.ok(s.panel.querySelector('.msg-edit-confirm'));
  s.paste();
  assert.equal(s.panel.querySelector('.msg-edit-confirm'), null);
  assert.equal(s.send.disabled, true);
  await settle(); s.send.click();
  s.panel.querySelector('.msg-edit-confirm .msg-edit-send').click();
  assert.equal(s.calls.plans[0].media.length, 1);
});
