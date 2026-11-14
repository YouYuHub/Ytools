const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { stripTypeScriptTypes } = require('node:module');
const { JSDOM } = require('jsdom');

// 全屏头栏自动隐藏：进全屏先亮后藏，顶部/热区/触摸/焦点点亮，idle 超时隐藏。
function harness() {
  const dom = new JSDOM('<div id="chatInner"></div>');
  const { window } = dom;
  const { document } = window;
  const chatInner = document.querySelector('#chatInner');
  const zoomSrc = stripTypeScriptTypes(fs.readFileSync(path.join(__dirname, '../src/zoom_utils.ts'), 'utf8'));
  const zoomBox = { module: { exports: {} } };
  vm.createContext(zoomBox);
  vm.runInContext(zoomSrc, zoomBox);
  window.ZoomUtils = zoomBox.module.exports;
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
  // 假计时器：捕获回调手动触发，避免 2.2s 真等待
  const timers = new Map();
  let timerSeq = 0;
  const widgetSrc = stripTypeScriptTypes(fs.readFileSync(path.join(__dirname, '../src/app/messages_widgets.ts'), 'utf8'));
  const sandbox = {
    window, document, navigator: window.navigator, URL,
    setTimeout: (fn, ms) => { const id = ++timerSeq; timers.set(id, { fn, ms }); return id; },
    clearTimeout: (id) => { timers.delete(id); },
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(widgetSrc, sandbox);
  return {
    dom, window, document, chatInner, App, timers,
    addBlock(kindCls) {
      const block = document.createElement('div');
      block.className = 'md-svg-block ' + kindCls;
      block.innerHTML = '<div class="md-svg-head"><button type="button" class="md-svg-btn">全屏</button></div>' +
        '<div class="md-svg-view" data-svg-view="image"><svg viewBox="0 0 640 400"></svg></div>';
      chatInner.appendChild(block);
      return block;
    },
    setFullscreen(blockOrNull) {
      Object.defineProperty(document, 'fullscreenElement', {
        value: blockOrNull || null, configurable: true,
      });
    },
    fireFullscreenChange() {
      document.dispatchEvent(new window.Event('fullscreenchange'));
    },
    runTimers() {
      const pending = [...timers.values()];
      timers.clear();
      pending.forEach(t => t.fn());
    },
  };
}

test('进入全屏先显示头栏，提示超时后自动隐藏', t => {
  const h = harness();
  t.after(() => h.dom.window.close());
  const block = h.addBlock('md-chart-block');
  h.setFullscreen(block);
  h.fireFullscreenChange();
  assert.equal(block.classList.contains('is-bar-visible'), true);
  h.runTimers();
  assert.equal(block.classList.contains('is-bar-visible'), false);
});

test('退出全屏清理类与计时器', t => {
  const h = harness();
  t.after(() => h.dom.window.close());
  const block = h.addBlock('md-chart-block');
  h.setFullscreen(block);
  h.fireFullscreenChange();
  assert.equal(block.classList.contains('is-bar-visible'), true);
  h.setFullscreen(null);
  h.fireFullscreenChange();
  assert.equal(block.classList.contains('is-bar-visible'), false);
  assert.equal(h.timers.size, 0);
});

test('非 svg 块全屏不接管', t => {
  const h = harness();
  t.after(() => h.dom.window.close());
  const block = h.addBlock('md-chart-block');
  const other = h.document.createElement('div');
  h.chatInner.appendChild(other);
  h.setFullscreen(other);
  h.fireFullscreenChange();
  assert.equal(block.classList.contains('is-bar-visible'), false);
  assert.equal(h.timers.size, 0);
});

test('顶部 mousemove 显示，底部只续计时不显示', t => {
  const h = harness();
  t.after(() => h.dom.window.close());
  const block = h.addBlock('md-chart-block');
  h.setFullscreen(block);
  h.fireFullscreenChange();
  h.runTimers();
  assert.equal(block.classList.contains('is-bar-visible'), false);
  h.document.dispatchEvent(new h.window.MouseEvent('mousemove', { clientY: 10, bubbles: true }));
  assert.equal(block.classList.contains('is-bar-visible'), true);
  block.classList.remove('is-bar-visible');
  h.timers.clear();
  h.document.dispatchEvent(new h.window.MouseEvent('mousemove', { clientY: 500, bubbles: true }));
  assert.equal(block.classList.contains('is-bar-visible'), false);
  assert.equal(h.timers.size, 1);
});

test('idle 超时隐藏头栏', t => {
  const h = harness();
  t.after(() => h.dom.window.close());
  const block = h.addBlock('md-chart-block');
  h.setFullscreen(block);
  h.fireFullscreenChange();
  h.document.dispatchEvent(new h.window.MouseEvent('mousemove', { clientY: 10, bubbles: true }));
  assert.equal(block.classList.contains('is-bar-visible'), true);
  h.runTimers();
  assert.equal(block.classList.contains('is-bar-visible'), false);
});

test('热区条 mouseover 显示（事件目标即块本身，iframe 上方同样有效）', t => {
  const h = harness();
  t.after(() => h.dom.window.close());
  const block = h.addBlock('md-canvas-block');
  h.setFullscreen(block);
  h.fireFullscreenChange();
  h.runTimers();
  assert.equal(block.classList.contains('is-bar-visible'), false);
  block.dispatchEvent(new h.window.MouseEvent('mouseover', { clientY: 5, bubbles: true }));
  assert.equal(block.classList.contains('is-bar-visible'), true);
});

test('触摸顶部显示，触摸内容区只续计时', t => {
  const h = harness();
  t.after(() => h.dom.window.close());
  const block = h.addBlock('md-chart-block');
  h.setFullscreen(block);
  h.fireFullscreenChange();
  h.runTimers();
  const top = new h.window.Event('touchstart', { bubbles: true });
  top.touches = [{ clientY: 20 }];
  h.document.dispatchEvent(top);
  assert.equal(block.classList.contains('is-bar-visible'), true);
  block.classList.remove('is-bar-visible');
  h.timers.clear();
  const body = new h.window.Event('touchstart', { bubbles: true });
  body.touches = [{ clientY: 400 }];
  h.document.dispatchEvent(body);
  assert.equal(block.classList.contains('is-bar-visible'), false);
  assert.equal(h.timers.size, 1);
});

test('头栏聚焦与 Tab 点亮', t => {
  const h = harness();
  t.after(() => h.dom.window.close());
  const block = h.addBlock('md-chart-block');
  h.setFullscreen(block);
  h.fireFullscreenChange();
  h.runTimers();
  const btn = block.querySelector('.md-svg-btn');
  btn.dispatchEvent(new h.window.FocusEvent('focusin', { bubbles: true }));
  assert.equal(block.classList.contains('is-bar-visible'), true);
  block.classList.remove('is-bar-visible');
  h.document.dispatchEvent(new h.window.KeyboardEvent('keydown', { key: 'Tab', bubbles: true }));
  assert.equal(block.classList.contains('is-bar-visible'), true);
});

test('非全屏时鼠标/触摸不加类', t => {
  const h = harness();
  t.after(() => h.dom.window.close());
  const block = h.addBlock('md-mermaid-block');
  h.setFullscreen(null);
  h.document.dispatchEvent(new h.window.MouseEvent('mousemove', { clientY: 5, bubbles: true }));
  const touch = new h.window.Event('touchstart', { bubbles: true });
  touch.touches = [{ clientY: 5 }];
  h.document.dispatchEvent(touch);
  assert.equal(block.classList.contains('is-bar-visible'), false);
  assert.equal(h.timers.size, 0);
});
