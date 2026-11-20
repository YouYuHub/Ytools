const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { stripTypeScriptTypes } = require('node:module');
const { JSDOM } = require('jsdom');

// 图片预览弹窗滚轮缩放：有缩放目标（图片/视频）时普通滚轮直接缩放
// （预览/全屏通用，与 SVG 控件全屏手感一致）；无目标（文本/PDF/音频/
// 下载兜底）不拦截；纯水平滚动不触发缩放。
function harness() {
  const dom = new JSDOM(
    '<div id="mediaPreviewModal" class="hidden">' +
    '<div class="media-preview-dialog">' +
    '<div id="mediaPreviewTitle"></div>' +
    '<div id="mediaPreviewZoom" class="hidden">' +
    '<button id="mediaPreviewZoomOut"></button>' +
    '<button id="mediaPreviewZoomLabel">100%</button>' +
    '<button id="mediaPreviewZoomIn"></button></div>' +
    '<div id="mediaPreviewBody"></div>' +
    '<button id="mediaPreviewClose"></button>' +
    '<button id="mediaPreviewFull"></button>' +
    '</div><div id="mediaPreviewBackdrop"></div></div>' +
    '<div id="composerAttachments"></div><div id="composer"></div>' +
    '<div id="input" contenteditable="true"></div>'
  );
  const { window } = dom;
  const { document } = window;
  const $ = (sel) => document.querySelector(sel);
  const App = {
    state: { sessionId: 's', pendingMedia: [], sessionDocs: [] },
    el: (tag, cls, text) => {
      const n = document.createElement(tag);
      if (cls) n.className = cls;
      if (text != null) n.textContent = text;
      return n;
    },
    toast() {},
    $,
    composerAttachments: $('#composerAttachments'),
    composer: $('#composer'),
    input: $('#input'),
    MAX_PENDING_MEDIA: 5,
    syncComposerAttachmentBlocks: null,
    MEDIA_SIZE_LIMITS: {},
    MEDIA_EXTENSIONS: {},
    mediaPreviewModal: $('#mediaPreviewModal'),
    mediaPreviewBody: $('#mediaPreviewBody'),
    mediaPreviewTitle: $('#mediaPreviewTitle'),
    mediaPreviewZoom: $('#mediaPreviewZoom'),
    mediaPreviewZoomIn: $('#mediaPreviewZoomIn'),
    mediaPreviewZoomOut: $('#mediaPreviewZoomOut'),
    mediaPreviewZoomLabel: $('#mediaPreviewZoomLabel'),
  };
  window.App = App;
  const zoomSrc = stripTypeScriptTypes(fs.readFileSync(path.join(__dirname, '../src/zoom_utils.ts'), 'utf8'));
  const zoomBox = { module: { exports: {} } };
  vm.createContext(zoomBox);
  vm.runInContext(zoomSrc, zoomBox);
  window.ZoomUtils = zoomBox.module.exports;
  const mediaSrc = stripTypeScriptTypes(fs.readFileSync(path.join(__dirname, '../src/app/media.ts'), 'utf8'));
  const sandbox = { window, document, navigator: window.navigator, setTimeout, clearTimeout, URL };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(mediaSrc, sandbox);
  const body = $('#mediaPreviewBody');
  return {
    dom, window, document, App, body,
    open(type) {
      App.openMediaPreview({ type, src: 'x.' + type, title: 't' });
    },
    wheel(target, init) {
      const evt = new window.WheelEvent('wheel', Object.assign(
        { bubbles: true, cancelable: true }, init
      ));
      const notPrevented = target.dispatchEvent(evt);
      return { prevented: !notPrevented };
    },
    scaleOf() {
      const img = body.querySelector('img, video');
      const m = img && /scale\(([\d.]+)\)/.exec(img.style.transform || '');
      return m ? parseFloat(m[1]) : 1;
    },
  };
}

test('图片预览：普通滚轮直接缩放（无需 Ctrl）', t => {
  const h = harness();
  t.after(() => h.dom.window.close());
  h.open('image');
  const img = h.body.querySelector('img');
  const r = h.wheel(img, { deltaY: -100, clientX: 50, clientY: 60 });
  assert.equal(r.prevented, true);
  assert.ok(h.scaleOf() > 1, 'scale=' + h.scaleOf());
  const r2 = h.wheel(img, { deltaY: 100, clientX: 50, clientY: 60 });
  assert.equal(r2.prevented, true);
  assert.ok(Math.abs(h.scaleOf() - 1) < 1e-9, 'scale=' + h.scaleOf());
});

test('图片预览：Ctrl+滚轮仍然缩放（向后兼容）', t => {
  const h = harness();
  t.after(() => h.dom.window.close());
  h.open('image');
  const r = h.wheel(h.body.querySelector('img'), { deltaY: -100, ctrlKey: true });
  assert.equal(r.prevented, true);
  assert.ok(h.scaleOf() > 1);
});

test('视频预览：普通滚轮直接缩放', t => {
  const h = harness();
  t.after(() => h.dom.window.close());
  h.open('video');
  const r = h.wheel(h.body.querySelector('video'), { deltaY: -100 });
  assert.equal(r.prevented, true);
  assert.ok(h.scaleOf() > 1);
});

test('音频/下载兜底：滚轮不拦截（无缩放目标）', t => {
  const h = harness();
  t.after(() => h.dom.window.close());
  for (const type of ['audio', 'download']) {
    h.open(type);
    const r = h.wheel(h.body, { deltaY: -100 });
    assert.equal(r.prevented, false, type);
  }
});

test('纯水平滚动不触发缩放', t => {
  const h = harness();
  t.after(() => h.dom.window.close());
  h.open('image');
  const r = h.wheel(h.body.querySelector('img'), { deltaY: 0, deltaX: 50 });
  assert.equal(r.prevented, false);
  assert.equal(h.scaleOf(), 1);
});
