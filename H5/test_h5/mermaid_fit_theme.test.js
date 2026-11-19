const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { stripTypeScriptTypes } = require('node:module');
const { JSDOM } = require('jsdom');

// Mermaid 渲染改进回归：
// 1) 极端宽高比（<0.5 / >2.5）按宽度适配 + is-fit-width 滚动容器，不再按高度压成细条；
// 2) 主题跟随应用深浅色/强调色（base 主题 + 透明子图底），缓存 key 带主题签名；
// 3) viewBox 缺失时按数值 width/height 兜底；
// 4) 大图上限放宽（maxTextSize/maxEdges）。
const Markdown = require(path.join(__dirname, '..', 'js', 'markdown.js'));

function makeHolderDom() {
  const dom = new JSDOM('<div id="chatInner"></div>');
  const { document } = dom.window;
  const block = document.createElement('div');
  block.className = 'md-svg-block md-mermaid-block';
  const holder = document.createElement('div');
  holder.className = 'md-mermaid-loading';
  holder.setAttribute('data-mermaid-holder', 'm1');
  block.appendChild(holder);
  document.getElementById('chatInner').appendChild(block);
  return { dom, document, block, holder };
}

// 在受控 sandbox 里加载 markdown.ts 源码（可注入 document/getComputedStyle/假 mermaid）
function loadMarkdownSandbox(env) {
  const src = stripTypeScriptTypes(fs.readFileSync(path.join(__dirname, '../src/markdown.ts'), 'utf8'));
  const sandbox = Object.assign({
    module: { exports: {} },
    setTimeout, clearTimeout, console,
  }, env);
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(src, sandbox);
  return { api: sandbox.module.exports, sandbox };
}

test('mermaidFitWidth：极端比例阈值 0.5 / 2.5', () => {
  assert.equal(Markdown.mermaidFitWidth(0.156), true);   // 1:6.4 竖长条
  assert.equal(Markdown.mermaidFitWidth(0.49), true);
  assert.equal(Markdown.mermaidFitWidth(0.5), false);   // 边界不触发
  assert.equal(Markdown.mermaidFitWidth(1), false);
  assert.equal(Markdown.mermaidFitWidth(2.5), false);
  assert.equal(Markdown.mermaidFitWidth(2.51), true);   // 横长条
  assert.equal(Markdown.mermaidFitWidth(6), true);
  assert.equal(Markdown.mermaidFitWidth(0), false);
  assert.equal(Markdown.mermaidFitWidth(NaN), false);
});

test('buildMermaidConfig：深色/浅色/强调色色板与大图上限', () => {
  const darkDom = new JSDOM('<html data-theme="dark" data-accent="green"><body></body></html>');
  const darkEnv = {
    document: darkDom.window.document,
    getComputedStyle: () => ({ getPropertyValue: (n) => n === '--accent-swatch' ? '#34a853' : '' }),
  };
  const dark = loadMarkdownSandbox(darkEnv).api.buildMermaidConfig();
  assert.equal(dark.theme, 'base');
  assert.equal(dark.securityLevel, 'strict');          // 不为 htmlLabels 放松安全级别
  assert.equal(dark.suppressErrorRendering, true);
  assert.equal(dark.maxTextSize, 200000);
  assert.equal(dark.maxEdges, 1500);
  assert.equal(dark.themeVariables.darkMode, true);
  assert.equal(dark.themeVariables.clusterBkg, 'transparent');  // 子图透明底，融入深色页
  assert.equal(dark.themeVariables.lineColor, '#34a853');       // 连线取强调色
  assert.equal(dark.themeVariables.primaryTextColor, '#e6e6e6');
  assert.ok(dark.themeVariables.fontFamily.includes('Microsoft YaHei'));

  const lightDom = new JSDOM('<html data-theme="light" data-accent="blue"><body></body></html>');
  const light = loadMarkdownSandbox({
    document: lightDom.window.document,
    getComputedStyle: () => ({ getPropertyValue: () => '#4a90e2' }),
  }).api.buildMermaidConfig();
  assert.equal(light.themeVariables.darkMode, false);
  assert.equal(light.themeVariables.clusterBkg, 'transparent');
  assert.equal(light.themeVariables.lineColor, '#4a90e2');

  // 无 document（纯 Node）：回退默认色板不抛错
  const bare = loadMarkdownSandbox({}).api.buildMermaidConfig();
  assert.equal(bare.themeVariables.darkMode, false);
  assert.ok(/^#/.test(bare.themeVariables.lineColor));
  darkDom.window.close();
  lightDom.window.close();
});

test('mermaidThemeSignature：随 data-theme/data-accent 变化', () => {
  const dom = new JSDOM('<html data-theme="light" data-accent="blue"><body></body></html>');
  const { api } = loadMarkdownSandbox({
    document: dom.window.document,
    getComputedStyle: () => ({ getPropertyValue: () => '#4a90e2' }),
  });
  const sig1 = api.mermaidThemeSignature();
  dom.window.document.documentElement.dataset.theme = 'dark';
  const sig2 = api.mermaidThemeSignature();
  assert.notEqual(sig1, sig2);
  dom.window.document.documentElement.dataset.accent = 'pink';
  assert.notEqual(sig2, api.mermaidThemeSignature());
  dom.window.close();
});

test('renderMermaidInto：竖长图按宽度适配并打 is-fit-width；普通图保持 contain', async () => {
  const { dom, block, holder } = makeHolderDom();
  const fake = {
    initialize() {},
    render: async (id, code) => ({
      svg: code === 'tall'
        ? '<svg viewBox="0 0 200 1280" width="200" height="1280"></svg>'
        : '<svg viewBox="0 0 640 400"></svg>',
    }),
  };
  const { api } = loadMarkdownSandbox({ document: dom.window.document, mermaid: fake });
  await api.renderMermaidInto('m1', 'tall', holder, {});
  assert.equal(block.classList.contains('is-fit-width'), true);
  const tallSvg = holder.querySelector('svg');
  assert.equal(tallSvg.style.width, 'calc(100% * var(--md-zoom, 1))');
  assert.equal(tallSvg.style.maxWidth, 'none');
  // jsdom 会把 aspect-ratio 序列化为 "0.15625 / 1"，浏览器为 "0.15625"，两者都接受
  assert.ok(/^0\.15625( \/ 1)?$/.test(tallSvg.style.aspectRatio), tallSvg.style.aspectRatio);

  // 同 holder 重渲染普通比例图：is-fit-width 应被清除，回到 170vh 钳制
  await api.renderMermaidInto('m1', 'normal', holder, {});
  assert.equal(block.classList.contains('is-fit-width'), false);
  const normalSvg = holder.querySelector('svg');
  assert.ok(normalSvg.style.width.includes('170vh'), normalSvg.style.width);
  dom.window.close();
});

test('renderMermaidInto：viewBox 缺失按数值 width/height 兜底', async () => {
  const { dom, block, holder } = makeHolderDom();
  const fake = {
    initialize() {},
    render: async () => ({ svg: '<svg width="300" height="1500"></svg>' }),
  };
  const { api } = loadMarkdownSandbox({ document: dom.window.document, mermaid: fake });
  await api.renderMermaidInto('m1', 'x', holder, {});
  const svg = holder.querySelector('svg');
  assert.equal(svg.getAttribute('viewBox'), '0 0 300 1500');
  assert.equal(block.classList.contains('is-fit-width'), true);  // ar=0.2 极端竖长
  dom.window.close();
});

test('renderMermaidInto：缓存 key 带主题签名，主题切换后重渲染', async () => {
  const dom = new JSDOM('<html data-theme="light"><body><div id="chatInner"></div></body></html>');
  const { document } = dom.window;
  const block = document.createElement('div');
  block.className = 'md-svg-block md-mermaid-block';
  const holder = document.createElement('div');
  holder.setAttribute('data-mermaid-holder', 'm1');
  block.appendChild(holder);
  document.getElementById('chatInner').appendChild(block);
  let renders = 0;
  const fake = {
    initialize() {},
    render: async () => { renders++; return { svg: '<svg viewBox="0 0 640 400"></svg>' }; },
  };
  const { api } = loadMarkdownSandbox({
    document, mermaid: fake,
    getComputedStyle: () => ({ getPropertyValue: () => '#4a90e2' }),
  });
  await api.renderMermaidInto('m1', 'same-code', holder, { cachedCode: 'same-code' });
  assert.equal(renders, 1);
  await api.renderMermaidInto('m1', 'same-code', holder, { cachedCode: 'same-code' });
  assert.equal(renders, 1);  // 同主题命中缓存
  document.documentElement.dataset.theme = 'dark';
  await api.renderMermaidInto('m1', 'same-code', holder, { cachedCode: 'same-code' });
  assert.equal(renders, 2);  // 主题签名变化 → 缓存失效重渲染
  dom.window.close();
});

// ---------- messages_canvas.ts：主题切换重渲染 done 态块 ----------
function canvasHarness() {
  const dom = new JSDOM('<div id="chatInner"></div>');
  const { window } = dom;
  const { document } = window;
  const chatInner = document.getElementById('chatInner');
  const calls = [];
  const App = { chatInner, toast() {}, syncSvgFullscreenLayout() {} };
  window.App = App;
  const MarkdownStub = {
    unescapeHtml: (s) => s,
    renderMermaidInto: (id, code, holder) => {
      calls.push(id);
      holder.innerHTML = '<svg viewBox="0 0 640 400"></svg>';
      return Promise.resolve(true);
    },
  };
  const src = stripTypeScriptTypes(fs.readFileSync(path.join(__dirname, '../src/app/messages_canvas.ts'), 'utf8'));
  const sandbox = {
    window, document, navigator: window.navigator,
    setTimeout, clearTimeout, URL,
    MutationObserver: window.MutationObserver,
    App, Markdown: MarkdownStub,
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(src, sandbox);
  return {
    dom, window, document, chatInner, App, calls,
    addBlock(state) {
      const block = document.createElement('div');
      block.className = 'md-svg-block md-mermaid-block';
      block.dataset.mermaidId = 'mm-' + state;
      block.dataset.mermaidState = state;
      block.dataset.svgCode = 'graph TD;A-->B';
      const holder = document.createElement('div');
      holder.setAttribute('data-mermaid-holder', block.dataset.mermaidId);
      if (state === 'done') holder.innerHTML = '<svg viewBox="0 0 640 400"></svg>';
      block.appendChild(holder);
      chatInner.appendChild(block);
      return block;
    },
  };
}

test('themechange/accentchange：仅 done 态 mermaid 块重渲染', async () => {
  const h = canvasHarness();
  const done = h.addBlock('done');
  const failed = h.addBlock('failed');
  h.document.dispatchEvent(new h.window.CustomEvent('themechange', { detail: { effective: 'dark' } }));
  await new Promise((r) => setTimeout(r, 20));
  assert.deepEqual(h.calls, ['mm-done']);           // failed 不自动重试
  assert.equal(done.dataset.mermaidState, 'done');  // 重渲染完成回到 done
  assert.equal(failed.dataset.mermaidState, 'failed');
  h.document.dispatchEvent(new h.window.CustomEvent('accentchange', { detail: { accent: 'pink' } }));
  await new Promise((r) => setTimeout(r, 20));
  assert.deepEqual(h.calls, ['mm-done', 'mm-done']);
  h.dom.window.close();
});

// ---------- messages_widgets.ts：is-fit-width 块 100% 也可拖拽平移 ----------
test('拖拽平移：is-fit-width 块 100% 缩放启用，普通块 100% 不拦截', () => {
  const dom = new JSDOM('<div id="chatInner"></div>');
  const { window } = dom;
  const { document } = window;
  const chatInner = document.getElementById('chatInner');
  const App = { chatInner, toast() {}, syncSvgFullscreenLayout() {} };
  window.App = App;
  const zoomSrc = stripTypeScriptTypes(fs.readFileSync(path.join(__dirname, '../src/zoom_utils.ts'), 'utf8'));
  const zoomBox = { module: { exports: {} } };
  vm.createContext(zoomBox);
  vm.runInContext(zoomSrc, zoomBox);
  window.ZoomUtils = zoomBox.module.exports;
  const src = stripTypeScriptTypes(fs.readFileSync(path.join(__dirname, '../src/app/messages_widgets.ts'), 'utf8'));
  const sandbox = { window, document, navigator: window.navigator, setTimeout, clearTimeout, URL };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(src, sandbox);

  function addBlock(fit) {
    const block = document.createElement('div');
    block.className = 'md-svg-block md-mermaid-block' + (fit ? ' is-fit-width' : '');
    const view = document.createElement('div');
    view.className = 'md-svg-view';
    view.innerHTML = '<svg viewBox="0 0 200 1280"></svg>';
    // jsdom 无布局：伪造溢出尺寸让平移门控可判定
    Object.defineProperty(view, 'scrollHeight', { value: 2000, configurable: true });
    Object.defineProperty(view, 'clientHeight', { value: 600, configurable: true });
    Object.defineProperty(view, 'scrollWidth', { value: 300, configurable: true });
    Object.defineProperty(view, 'clientWidth', { value: 300, configurable: true });
    block.appendChild(view);
    chatInner.appendChild(block);
    return view;
  }
  const fitView = addBlock(true);
  const plainView = addBlock(false);
  const down = (view) => view.dispatchEvent(new window.MouseEvent('mousedown', { bubbles: true, cancelable: true, button: 0 }));
  assert.equal(down(fitView), false, 'is-fit-width 100% 应拦截进入平移');   // preventDefault → false
  assert.equal(fitView.classList.contains('is-panning'), true);
  document.dispatchEvent(new window.MouseEvent('mouseup', { bubbles: true }));
  assert.equal(fitView.classList.contains('is-panning'), false);
  assert.equal(down(plainView), true, '普通块 100% 不拦截');
  assert.equal(plainView.classList.contains('is-panning'), false);
  dom.window.close();
});
