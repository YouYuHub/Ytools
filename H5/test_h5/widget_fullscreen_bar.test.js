const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { stripTypeScriptTypes } = require('node:module');
const { JSDOM } = require('jsdom');

// 全屏头栏自动隐藏：进全屏默认隐藏，顶部/热区/触摸/焦点点亮，
// 顶部静止 idle 超时藏；内容区持续移动不得“续命”（短延迟收起）。
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
    addPreview() {
      const dlg = document.createElement('div');
      dlg.className = 'media-preview-dialog';
      dlg.innerHTML = '<div class="media-preview-head"><span class="media-preview-title">图</span>' +
        '<div class="media-preview-actions"><button type="button" class="media-preview-full">全屏</button></div></div>' +
        '<div class="media-preview-body"><img class="media-preview-image" alt=""></div>';
      document.body.appendChild(dlg);
      return dlg;
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

test('进入全屏默认隐藏头栏，不挂计时器', t => {
  const h = harness();
  t.after(() => h.dom.window.close());
  const block = h.addBlock('md-chart-block');
  h.setFullscreen(block);
  h.fireFullscreenChange();
  assert.equal(block.classList.contains('is-bar-visible'), false);
  assert.equal(h.timers.size, 0);
});

test('内容区持续移动不续命：可见→短延迟收起，后续移动不再挂表', t => {
  const h = harness();
  t.after(() => h.dom.window.close());
  const block = h.addBlock('md-chart-block');
  h.setFullscreen(block);
  h.fireFullscreenChange();
  // 顶部点亮
  h.document.dispatchEvent(new h.window.MouseEvent('mousemove', { clientY: 10, bubbles: true }));
  assert.equal(block.classList.contains('is-bar-visible'), true);
  // 移到内容区：挂短延迟（400ms）而非 idle 2200ms
  h.document.dispatchEvent(new h.window.MouseEvent('mousemove', { clientY: 500, bubbles: true }));
  assert.equal(h.timers.size, 1);
  assert.equal([...h.timers.values()][0].ms, 400);
  // 倒计时进行中继续移动：不得重置（计时器 id 不变，否则持续活动永远续命）
  const timerId = [...h.timers.keys()][0];
  for (let i = 0; i < 6; i++) {
    h.document.dispatchEvent(new h.window.MouseEvent('mousemove', { clientY: 450 + i * 10, bubbles: true }));
    assert.equal([...h.timers.keys()][0], timerId, '内容区移动重置了收起倒计时');
  }
  h.runTimers();
  assert.equal(block.classList.contains('is-bar-visible'), false);
  // 已隐藏后继续在内容区移动：不再点亮、不挂表（旧缺陷：永远续命）
  for (let i = 0; i < 5; i++) {
    h.document.dispatchEvent(new h.window.MouseEvent('mousemove', { clientY: 400 + i, bubbles: true }));
  }
  assert.equal(block.classList.contains('is-bar-visible'), false);
  assert.equal(h.timers.size, 0);
});

test('顶部持续活动保持可见（每次移动重置 idle），静止后超时藏', t => {
  const h = harness();
  t.after(() => h.dom.window.close());
  const block = h.addBlock('md-chart-block');
  h.setFullscreen(block);
  h.fireFullscreenChange();
  for (let i = 0; i < 5; i++) {
    h.document.dispatchEvent(new h.window.MouseEvent('mousemove', { clientY: 8 + i, bubbles: true }));
    assert.equal(block.classList.contains('is-bar-visible'), true);
    assert.equal(h.timers.size, 1);
    assert.equal([...h.timers.values()][0].ms, 2200);
  }
  h.runTimers();
  assert.equal(block.classList.contains('is-bar-visible'), false);
});

test('退出全屏清理类与计时器', t => {
  const h = harness();
  t.after(() => h.dom.window.close());
  const block = h.addBlock('md-chart-block');
  h.setFullscreen(block);
  h.fireFullscreenChange();
  h.document.dispatchEvent(new h.window.MouseEvent('mousemove', { clientY: 10, bubbles: true }));
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

test('顶部 mousemove 显示；已隐藏时底部移动不显示不挂表', t => {
  const h = harness();
  t.after(() => h.dom.window.close());
  const block = h.addBlock('md-chart-block');
  h.setFullscreen(block);
  h.fireFullscreenChange();
  assert.equal(block.classList.contains('is-bar-visible'), false);
  h.document.dispatchEvent(new h.window.MouseEvent('mousemove', { clientY: 10, bubbles: true }));
  assert.equal(block.classList.contains('is-bar-visible'), true);
  block.classList.remove('is-bar-visible');
  h.timers.clear();
  h.document.dispatchEvent(new h.window.MouseEvent('mousemove', { clientY: 500, bubbles: true }));
  assert.equal(block.classList.contains('is-bar-visible'), false);
  assert.equal(h.timers.size, 0);
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

test('热区条 mouseover 仅顶部触发（iframe 上方有效，左右 padding 不触发）', t => {
  const h = harness();
  t.after(() => h.dom.window.close());
  const block = h.addBlock('md-canvas-block');
  h.setFullscreen(block);
  h.fireFullscreenChange();
  assert.equal(block.classList.contains('is-bar-visible'), false);
  block.dispatchEvent(new h.window.MouseEvent('mouseover', { clientY: 400, bubbles: true }));
  assert.equal(block.classList.contains('is-bar-visible'), false);
  block.dispatchEvent(new h.window.MouseEvent('mouseover', { clientY: 5, bubbles: true }));
  assert.equal(block.classList.contains('is-bar-visible'), true);
});

test('触摸顶部显示，触摸内容区短延迟收起后不再续命', t => {
  const h = harness();
  t.after(() => h.dom.window.close());
  const block = h.addBlock('md-chart-block');
  h.setFullscreen(block);
  h.fireFullscreenChange();
  const top = new h.window.Event('touchstart', { bubbles: true });
  top.touches = [{ clientY: 20 }];
  h.document.dispatchEvent(top);
  assert.equal(block.classList.contains('is-bar-visible'), true);
  const body = new h.window.Event('touchstart', { bubbles: true });
  body.touches = [{ clientY: 400 }];
  h.document.dispatchEvent(body);
  assert.equal(block.classList.contains('is-bar-visible'), true); // 尚可见：挂短延迟
  assert.equal([...h.timers.values()][0].ms, 400);
  h.runTimers();
  assert.equal(block.classList.contains('is-bar-visible'), false);
  h.timers.clear();
  const body2 = new h.window.Event('touchstart', { bubbles: true });
  body2.touches = [{ clientY: 400 }];
  h.document.dispatchEvent(body2);
  assert.equal(block.classList.contains('is-bar-visible'), false);
  assert.equal(h.timers.size, 0);
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

// ---------- 图片预览弹窗（.media-preview-dialog）同口径接管 ----------
test('图片预览全屏：默认隐藏，顶部点亮，内容区移动短延迟收起', t => {
  const h = harness();
  t.after(() => h.dom.window.close());
  const dlg = h.addPreview();
  h.setFullscreen(dlg);
  h.fireFullscreenChange();
  assert.equal(dlg.classList.contains('is-bar-visible'), false);
  assert.equal(h.timers.size, 0);
  h.document.dispatchEvent(new h.window.MouseEvent('mousemove', { clientY: 8, bubbles: true }));
  assert.equal(dlg.classList.contains('is-bar-visible'), true);
  // 移回内容区持续移动：一次性 400ms 倒计时，不被续命
  h.document.dispatchEvent(new h.window.MouseEvent('mousemove', { clientY: 400, bubbles: true }));
  const id = [...h.timers.keys()][0];
  assert.equal([...h.timers.values()][0].ms, 400);
  for (let i = 0; i < 4; i++) {
    h.document.dispatchEvent(new h.window.MouseEvent('mousemove', { clientY: 420 + i * 10, bubbles: true }));
    assert.equal([...h.timers.keys()][0], id);
  }
  h.runTimers();
  assert.equal(dlg.classList.contains('is-bar-visible'), false);
});

test('图片预览全屏：头栏按钮聚焦点亮（.media-preview-head 口径）', t => {
  const h = harness();
  t.after(() => h.dom.window.close());
  const dlg = h.addPreview();
  h.setFullscreen(dlg);
  h.fireFullscreenChange();
  const btn = dlg.querySelector('.media-preview-full');
  btn.dispatchEvent(new h.window.FocusEvent('focusin', { bubbles: true }));
  assert.equal(dlg.classList.contains('is-bar-visible'), true);
  // 退出全屏清理
  h.setFullscreen(null);
  h.fireFullscreenChange();
  assert.equal(dlg.classList.contains('is-bar-visible'), false);
  assert.equal(h.timers.size, 0);
});

test('编译 CSS：图片预览全屏头栏悬浮+隐藏规则存在', () => {
  const css = fs.readFileSync(path.join(__dirname, '../css/main.css'), 'utf8');
  assert.match(css, /\.media-preview-dialog:fullscreen[\s\S]{0,400}?\.media-preview-head\s*\{[^}]*position:\s*absolute/);
  assert.match(css, /\.media-preview-dialog:fullscreen:not\(\.is-bar-visible\) \.media-preview-head\s*\{[^}]*visibility:\s*hidden/);
});

test('图片预览全屏：热区条 mouseover 仅顶部触发', t => {
  const h = harness();
  t.after(() => h.dom.window.close());
  const dlg = h.addPreview();
  h.setFullscreen(dlg);
  h.fireFullscreenChange();
  dlg.dispatchEvent(new h.window.MouseEvent('mouseover', { clientY: 300, bubbles: true }));
  assert.equal(dlg.classList.contains('is-bar-visible'), false);
  dlg.dispatchEvent(new h.window.MouseEvent('mouseover', { clientY: 6, bubbles: true }));
  assert.equal(dlg.classList.contains('is-bar-visible'), true);
});
