const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { stripTypeScriptTypes } = require('node:module');
const { JSDOM } = require('jsdom');

// 四控件滚轮缩放：非全屏只认 Ctrl+滚轮（普通滚轮留给页面滚动）；
// 全屏内普通滚轮直接步进，到边界放行给容器滚动。
function harness() {
  const dom = new JSDOM('<div id="chatInner"></div>');
  const { window } = dom;
  const { document } = window;
  const chatInner = document.querySelector('#chatInner');
  // ZoomUtils（纯逻辑模块，require 形态导出）
  const zoomSrc = stripTypeScriptTypes(fs.readFileSync(path.join(__dirname, '../src/zoom_utils.ts'), 'utf8'));
  const zoomBox = { module: { exports: {} } };
  vm.createContext(zoomBox);
  vm.runInContext(zoomSrc, zoomBox);
  const ZoomUtils = zoomBox.module.exports;
  window.ZoomUtils = ZoomUtils;
  const App = {
    chatInner,
    el: (tag, cls, text) => {
      const n = document.createElement(tag);
      if (cls) n.className = cls;
      if (text != null) n.textContent = text;
      return n;
    },
    toast() {},
    syncSvgFullscreenLayout() {},
  };
  window.App = App;
  const widgetSrc = stripTypeScriptTypes(fs.readFileSync(path.join(__dirname, '../src/app/messages_widgets.ts'), 'utf8'));
  const sandbox = { window, document, navigator: window.navigator, setTimeout, clearTimeout, URL };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(widgetSrc, sandbox);
  return {
    dom, window, document, chatInner, App, ZoomUtils,
    addBlock(kindCls) {
      const block = document.createElement('div');
      block.className = 'md-svg-block ' + kindCls;
      block.innerHTML = '<div class="md-svg-view" data-svg-view="image">' +
        '<svg viewBox="0 0 640 400"></svg></div>';
      chatInner.appendChild(block);
      return block;
    },
    setFullscreen(blockOrNull) {
      Object.defineProperty(document, 'fullscreenElement', {
        value: blockOrNull || null, configurable: true,
      });
    },
    wheel(target, init) {
      const evt = new window.WheelEvent('wheel', Object.assign(
        { bubbles: true, cancelable: true }, init
      ));
      // dispatchEvent 返回 false 表示被 preventDefault
      const notPrevented = target.dispatchEvent(evt);
      return { prevented: !notPrevented };
    },
  };
}

test('非全屏普通滚轮不缩放不拦截（留给页面滚动）', t => {
  const h = harness();
  t.after(() => h.dom.window.close());
  for (const kind of ['md-chart-block', 'md-svg-kind-svg', 'md-mermaid-block']) {
    const block = h.addBlock(kind);
    const r = h.wheel(block.querySelector('svg'), { deltaY: -100 });
    assert.equal(r.prevented, false, kind);
    assert.equal(block.dataset.zoom || '', '', kind);
  }
});

test('非全屏 Ctrl+滚轮步进缩放', t => {
  const h = harness();
  t.after(() => h.dom.window.close());
  const block = h.addBlock('md-chart-block');
  const svg = block.querySelector('svg');
  let r = h.wheel(svg, { deltaY: -100, ctrlKey: true });
  assert.equal(r.prevented, true);
  assert.equal(block.dataset.zoom, '1.1');
  r = h.wheel(svg, { deltaY: 120, ctrlKey: true });
  assert.equal(r.prevented, true);
  assert.equal(block.dataset.zoom, '1');
});

test('全屏内普通滚轮直接缩放', t => {
  const h = harness();
  t.after(() => h.dom.window.close());
  for (const kind of ['md-chart-block', 'md-svg-kind-svg', 'md-mermaid-block']) {
    const block = h.addBlock(kind);
    h.setFullscreen(block);
    const r = h.wheel(block.querySelector('svg'), { deltaY: -100 });
    assert.equal(r.prevented, true, kind);
    assert.equal(block.dataset.zoom, '1.1', kind);
    h.setFullscreen(null);
  }
});

test('全屏到边界放行：800% 继续放大 / 25% 继续缩小不拦截', t => {
  const h = harness();
  t.after(() => h.dom.window.close());
  const block = h.addBlock('md-chart-block');
  const svg = block.querySelector('svg');
  h.setFullscreen(block);
  block.dataset.zoom = '8';
  let r = h.wheel(svg, { deltaY: -100 });
  assert.equal(r.prevented, false, '顶部边界放行容器滚动');
  assert.equal(block.dataset.zoom, '8');
  block.dataset.zoom = '0.25';
  r = h.wheel(svg, { deltaY: 100 });
  assert.equal(r.prevented, false, '底部边界放行容器滚动');
  assert.equal(block.dataset.zoom, '0.25');
  h.setFullscreen(null);
});

test('纯水平滚动不拦截', t => {
  const h = harness();
  t.after(() => h.dom.window.close());
  const block = h.addBlock('md-chart-block');
  const r = h.wheel(block.querySelector('svg'), { deltaY: 0, deltaX: 50 });
  assert.equal(r.prevented, false);
  assert.equal(block.dataset.zoom || '', '');
});

test('canvas 块跳过（滚轮走沙箱按钮链路）', t => {
  const h = harness();
  t.after(() => h.dom.window.close());
  const block = h.addBlock('md-canvas-block');
  h.setFullscreen(block);
  const r = h.wheel(block.querySelector('svg'), { deltaY: -100 });
  assert.equal(r.prevented, false);
  assert.equal(block.dataset.zoom || '', '');
  h.setFullscreen(null);
});
