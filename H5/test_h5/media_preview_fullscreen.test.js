const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { stripTypeScriptTypes } = require('node:module');
const { JSDOM } = require('jsdom');

// 媒体预览全屏按钮状态：未全屏显示「全屏显示+展开图标」，全屏显示
// 「退出全屏+收起图标」；全屏中点 ✕ 关闭后，下次打开不得残留退出态。
const EXPAND_PATH = 'M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5';
const COMPRESS_PATH = 'M9 4v5H4M15 4v5h5M9 20v-5H4M15 20v-5h5';
const TITLE_OFF = '全屏显示（Esc 退出全屏）';
const TITLE_ON = '退出全屏（Esc）';

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
    '<button id="mediaPreviewFull" title="' + TITLE_OFF + '"></button>' +
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
  // jsdom 的 fullscreenElement 只读：用自有属性模拟全屏态
  let fakeFullscreen = null;
  Object.defineProperty(document, 'fullscreenElement', {
    configurable: true,
    get: () => fakeFullscreen,
  });
  const zoomSrc = stripTypeScriptTypes(fs.readFileSync(path.join(__dirname, '../src/zoom_utils.ts'), 'utf8'));
  const zoomBox = { module: { exports: {} } };
  vm.createContext(zoomBox);
  vm.runInContext(zoomSrc, zoomBox);
  window.ZoomUtils = zoomBox.module.exports;
  // Markdown.fullscreenIcons：与线上一致的图标对
  window.Markdown = {
    fullscreenIcons: {
      expand: '<svg><path d="' + EXPAND_PATH + '"/></svg>',
      compress: '<svg><path d="' + COMPRESS_PATH + '"/></svg>',
    },
  };
  const mediaSrc = stripTypeScriptTypes(fs.readFileSync(path.join(__dirname, '../src/app/media.ts'), 'utf8'));
  const sandbox = { window, document, navigator: window.navigator, setTimeout, clearTimeout, URL };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(mediaSrc, sandbox);
  const dialog = document.querySelector('.media-preview-dialog');
  const fullBtn = $('#mediaPreviewFull');
  return {
    dom, window, document, App, dialog, fullBtn,
    setFullscreen(on) {
      fakeFullscreen = on ? dialog : null;
      document.dispatchEvent(new window.Event('fullscreenchange'));
    },
  };
}

test('打开预览：未全屏显示展开态', t => {
  const h = harness();
  t.after(() => h.dom.window.close());
  h.App.openMediaPreview({ type: 'image', src: 'x.png', title: 't' });
  assert.equal(h.fullBtn.title, TITLE_OFF);
  assert.ok(h.fullBtn.innerHTML.includes(EXPAND_PATH), h.fullBtn.innerHTML);
});

test('全屏切换：按钮随 fullscreenchange 同步', t => {
  const h = harness();
  t.after(() => h.dom.window.close());
  h.App.openMediaPreview({ type: 'image', src: 'x.png', title: 't' });
  h.setFullscreen(true);
  assert.equal(h.fullBtn.title, TITLE_ON);
  assert.ok(h.fullBtn.innerHTML.includes(COMPRESS_PATH), h.fullBtn.innerHTML);
  h.setFullscreen(false);
  assert.equal(h.fullBtn.title, TITLE_OFF);
  assert.ok(h.fullBtn.innerHTML.includes(EXPAND_PATH), h.fullBtn.innerHTML);
});

test('全屏中关闭：hidden 后到达的退出事件仍复位按钮', t => {
  const h = harness();
  t.after(() => h.dom.window.close());
  h.App.openMediaPreview({ type: 'image', src: 'x.png', title: 't' });
  h.setFullscreen(true);
  assert.equal(h.fullBtn.title, TITLE_ON);
  // 点 ✕：先 hidden（exitFullscreen 为异步，事件随后才到）
  h.document.querySelector('#mediaPreviewClose').click();
  assert.ok(h.document.querySelector('#mediaPreviewModal').classList.contains('hidden'));
  // 退出全屏事件晚到：此时弹窗已 hidden，按钮仍须复位
  h.setFullscreen(false);
  assert.equal(h.fullBtn.title, TITLE_OFF);
  assert.ok(h.fullBtn.innerHTML.includes(EXPAND_PATH), h.fullBtn.innerHTML);
});

test('残留兜底：打开时按当前全屏态重同步', t => {
  const h = harness();
  t.after(() => h.dom.window.close());
  // 模拟旧残留：按钮卡在退出态
  h.fullBtn.title = TITLE_ON;
  h.fullBtn.innerHTML = '<svg><path d="' + COMPRESS_PATH + '"/></svg>';
  h.App.openMediaPreview({ type: 'image', src: 'x.png', title: 't' });
  assert.equal(h.fullBtn.title, TITLE_OFF);
  assert.ok(h.fullBtn.innerHTML.includes(EXPAND_PATH), h.fullBtn.innerHTML);
});
