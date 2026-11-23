const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');
const { stripTypeScriptTypes } = require('node:module');

// 历史编辑面板：
// 1. 切换“重新生成该轮/删除该轮及之后”必须关闭已弹出的发送二次确认条，
//    否则确认文案（针对旧范围）与当前选中范围不一致，造成歧义；
// 2. “引用所选文字”按钮与“添加图片…”按钮同处上传行，引用行只放芯片，
//    无芯片时整行隐藏（不占空隙）。
const kindOf = name => /\.(png|jpe?g|webp|gif|bmp|tiff?|ico)$/i.test(name) ? 'image'
  : /\.mp4$/i.test(name) ? 'video' : /\.mp3$/i.test(name) ? 'audio' : null;

function setup(t, { content = 'Original message', quotes = [] } = {}) {
  const dom = new JSDOM('<body><div class="msg msg-user"><div class="msg-bubble">Original message</div></div></body>',
    { runScripts: 'outside-only', pretendToBeVisual: true });
  const w = dom.window, calls = { plans: [], toasts: [] };
  t.after(() => w.close());
  w.App = {
    state: { sessionId: 'session-1' },
    MAX_PENDING_MEDIA: 6, MAX_DOC_FILE_SIZE: 10 * 1024 * 1024, mediaKindOf: kindOf,
    async prepareMediaFile(file) { return { file, name: file.name, kind: kindOf(file.name) }; },
    getChatModelVision: () => true, resolveMediaSrc: ref => '/media/' + ref.slice(8),
    openMediaPreview() {}, toast: text => calls.toasts.push(text),
    confirmEditResend: plan => calls.plans.push(plan), autosize() {},
  };
  for (const file of ['quote_utils.js', 'clipboard_media_utils.js']) {
    w.eval(fs.readFileSync(path.join(__dirname, '../js', file), 'utf8'));
  }
  const source = stripTypeScriptTypes(fs.readFileSync(path.join(__dirname, '../src/app/messages_edit.ts'), 'utf8'));
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
  msg._editData = { content, round: 3, quotes };
  w.EditTest.begin(msg, false);
  const panel = msg.querySelector('.msg-edit-bubble');
  const modes = Array.from(panel.querySelectorAll('.msg-edit-mode'));
  return {
    w, msg, panel, calls,
    textarea: panel.querySelector('textarea'),
    send: panel.querySelector('.msg-edit-buttons .msg-edit-send'),
    cancel: panel.querySelector('.msg-edit-buttons .msg-edit-cancel'),
    regenBtn: modes[0], truncateBtn: modes[1],
    confirmBox: () => panel.querySelector('.msg-edit-confirm'),
  };
}

test('发送确认条弹出后切换模式，确认条关闭（不与新范围混淆）', t => {
  const s = setup(t);
  s.send.click();
  assert.ok(s.confirmBox(), 'regen 发送应弹出二次确认');
  assert.equal(s.panel._confirmShown, true);
  s.truncateBtn.click();
  assert.equal(s.confirmBox(), null);
  assert.equal(s.panel._confirmShown, false);
  assert.equal(s.panel._planArea.innerHTML, '');
  assert.ok(s.truncateBtn.classList.contains('selected'));
  assert.equal(s.calls.plans.length, 0, '切换只关确认条，不触发发送');
});

test('放弃修改确认条弹出后切换模式，同样关闭', t => {
  const s = setup(t);
  s.textarea.value = 'edited';
  s.cancel.click();
  assert.ok(s.confirmBox(), '内容已改时取消应弹出放弃确认');
  s.truncateBtn.click();
  assert.equal(s.confirmBox(), null);
  assert.equal(s.panel._confirmShown, false);
  // 面板仍在（只是关了确认条），原气泡未恢复
  assert.ok(s.msg.querySelector('.msg-edit-bubble'));
});

test('同模式重复点击也关闭确认条（幂等，无副作用）', t => {
  const s = setup(t);
  s.send.click();
  assert.ok(s.confirmBox());
  s.regenBtn.click();
  assert.equal(s.confirmBox(), null);
  assert.ok(s.regenBtn.classList.contains('selected'));
});

test('上传行只有两个按钮（多余提示已删除）', t => {
  const s = setup(t);
  const row = s.panel.querySelector('.msg-edit-upload-row');
  assert.ok(row, '应存在上传行');
  const buttons = Array.from(row.querySelectorAll(':scope > .msg-edit-upload'));
  assert.equal(buttons.length, 2);
  assert.equal(buttons[0].textContent, '添加图片、音视频或文档');
  assert.equal(buttons[1].textContent, '引用所选文字');
  assert.equal(s.panel.querySelector('.msg-edit-upload-hint'), null, '多余提示已删除');
});

test('引用与输入框同款：一方块+计数+悬浮原文卡，无引用时网格隐藏', t => {
  const empty = setup(t);
  assert.ok(empty.panel.querySelector('.msg-edit-attachment-blocks').classList.contains('hidden'));
  const s = setup(t, { quotes: [{ id: 'q_1', text: '被引用的原文内容', source: null }] });
  const blocks = s.panel.querySelector('.msg-edit-attachment-blocks');
  assert.ok(!blocks.classList.contains('hidden'));
  const tile = blocks.querySelector('.composer-quotes-tile');
  assert.ok(tile, '应使用输入框同款引用方块');
  assert.equal(tile.querySelector('.composer-quotes-count').textContent, '1');
  assert.equal(tile.querySelectorAll('.composer-quote').length, 1);
  assert.match(tile.querySelector('.composer-quote-text').textContent, /被引用的原文内容/);
  // 浮层内移除引用后网格重新隐藏
  tile.querySelector('.composer-quote-remove').click();
  assert.ok(blocks.classList.contains('hidden'));
});

test('图片方块无文件名版面（文件名只进 title/aria）', t => {
  const s = setup(t, { content: [{ type: 'text', text: 'hi' },
    { type: 'image_url', image_url: { url: 'media://clipboard-123.png' } }] });
  const chip = s.panel.querySelector('.msg-edit-attachment-blocks .media-chip');
  assert.ok(chip, '应使用输入框同款媒体方块');
  assert.ok(chip.querySelector('img.media-thumb'), '图片为纯缩略图');
  assert.equal(chip.textContent.includes('clipboard-123'), false, '方块内无文件名文本');
  assert.match(chip.title, /clipboard-123\.png/);
  assert.match(chip.getAttribute('aria-label'), /clipboard-123\.png/);
});
