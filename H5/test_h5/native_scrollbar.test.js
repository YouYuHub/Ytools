const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { JSDOM } = require('jsdom');

const source = fs.readFileSync(path.join(__dirname, '../js/app/core.js'), 'utf8');
function setup() {
  const dom = new JSDOM('<main id="main"><div id="scroll"><div id="inner"><pre></pre></div></div><div id="composer"></div></main>');
  const { document } = dom.window;
  const main = document.querySelector('#main'), scroll = document.querySelector('#scroll');
  const inner = document.querySelector('#inner'), composer = document.querySelector('#composer');
  const frames = new Map(), ends = [];
  let serial = 0, top = 350, naturalHeight = 1200;
  Object.defineProperties(scroll, {
    offsetWidth: { value: 100 }, clientWidth: { value: 85 }, clientHeight: { value: 500 },
    scrollHeight: { get: () => Number.parseFloat(inner.style.getPropertyValue('--scrollbar-content-height')) || naturalHeight },
    scrollTop: { get: () => top, set: value => { top = Math.max(0, Math.min(scroll.scrollHeight - 500, value)); } },
  });
  scroll.getBoundingClientRect = () => ({ left: 20, right: 120, top: 30, bottom: 530, width: 100, height: 500 });
  main.getBoundingClientRect = () => ({ bottom: 530 });
  composer.getBoundingClientRect = () => ({ top: 450 });
  inner.getBoundingClientRect = () => ({ height: naturalHeight });
  scroll.style.scrollBehavior = 'smooth';
  scroll.addEventListener('chat-scrollbar-dragend', event => ends.push(event.detail));
  const context = { window: dom.window, document, App: {}, app: main, main, chatScroll: scroll,
    chatInner: inner, composerWrap: composer, CustomEvent: dom.window.CustomEvent,
    requestAnimationFrame(fn) { frames.set(++serial, fn); return serial; },
    cancelAnimationFrame(id) { frames.delete(id); },
  };
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('  // 自动跟随与用户滚动'), source.indexOf('  function setEmpty(')), context);
  vm.runInContext(source.slice(source.indexOf('  function updateScrollBottomOffset('), source.indexOf('  // ---------- 主题')), context);
  vm.runInContext(source.slice(source.indexOf('  (function observeNativeScrollbarDrag()'), source.indexOf('  // 附件网格跟随缩放')), context);
  function fire(type, fields = {}, target = type === 'pointerdown' ? scroll : dom.window) {
    const event = new dom.window.Event(type, { bubbles: true, cancelable: true });
    Object.assign(event, { button: 0, pointerType: 'mouse', pointerId: 7, clientX: 115, clientY: 250 }, fields);
    target.dispatchEvent(event);
    return event;
  }
  return { context, document, window: dom.window, scroll, inner, frames, ends, fire,
    grow(value) { naturalHeight = value; },
    dragging: () => context.isChatScrollbarDragging(),
    paused: () => vm.runInContext('autoScrollPaused', context),
  };
}

test('native gutter press locks the range without cancelling the browser gesture', () => {
  const h = setup();
  const event = h.fire('pointerdown');
  assert.equal(event.defaultPrevented, false);
  assert.equal(h.dragging(), true);
  assert.equal(h.paused(), true);
  assert.equal(h.inner.style.getPropertyValue('--scrollbar-content-height'), '1200px');
  h.grow(2200);
  assert.equal(h.scroll.scrollHeight, 1200);
  h.context.stickToBottom();
  h.context.scrollToBottom(true);
  assert.equal(h.scroll.scrollTop, 350);
  h.fire('pointerup', { pointerId: 8 });
  assert.equal(h.dragging(), true);
  h.fire('pointerup');
  assert.equal(h.dragging(), false);
  assert.equal(h.scroll.scrollHeight, 2200);
  assert.equal(h.scroll.scrollTop, 350);
  assert.equal(h.paused(), true);
  assert.equal(h.ends.length, 1);
  assert.equal(h.inner.style.getPropertyValue('--scrollbar-content-height'), '');
});

test('content, nested scrollbar, touch and secondary presses keep normal scrolling', () => {
  const h = setup();
  h.fire('pointerdown', { clientX: 100 });
  h.fire('pointerdown', {}, h.inner.querySelector('pre'));
  h.fire('pointerdown', { pointerType: 'touch' });
  h.fire('pointerdown', { button: 2 });
  h.fire('pointerdown', { clientY: 540 });
  assert.equal(h.dragging(), false);
  h.grow(400);
  h.fire('pointerdown');
  assert.equal(h.dragging(), false);
});

test('dragging to the bottom resumes follow only after release and includes streamed growth', () => {
  const h = setup();
  h.fire('pointerdown');
  h.scroll.scrollTop = 700;
  h.fire('scroll', {}, h.scroll);
  assert.equal(h.paused(), true);
  h.grow(1800);
  h.context.stickToBottom();
  assert.equal(h.scroll.scrollTop, 700);
  h.fire('mouseup');
  assert.equal(h.scroll.scrollTop, 1300);
  assert.equal(h.paused(), false);
  h.fire('pointerup');
  assert.equal(h.ends.length, 1);
});

test('lost focus, cancellation, hiding and resize release the layout lock without resuming follow', () => {
  for (const type of ['blur', 'pointercancel', 'visibilitychange', 'resize']) {
    const h = setup();
    h.fire('pointerdown');
    h.scroll.scrollTop = 700;
    h.grow(1800);
    h.fire(type, {}, type === 'visibilitychange' ? h.document : h.window);
    assert.equal(h.dragging(), false, type);
    assert.equal(h.scroll.classList.contains('scrollbar-dragging'), false, type);
    assert.equal(h.inner.style.getPropertyValue('--scrollbar-content-height'), '', type);
    assert.equal(h.scroll.scrollTop, 700, type);
    assert.equal(h.paused(), true, type);
    assert.equal(h.ends[0].cancelled, true, type);
  }
});

test('composer padding updates and stale bottom corrections do not pull a paused scrollbar', () => {
  const h = setup();
  h.context.scrollToBottom(true);
  const stale = [...h.frames.values()][0];
  h.fire('pointerdown');
  h.scroll.scrollTop = 695;
  h.context.updateScrollBottomOffset();
  stale();
  assert.equal(h.scroll.scrollTop, 695);
  h.fire('pointerup');
  h.context.updateScrollBottomOffset();
  assert.equal(h.scroll.scrollTop, 695);
  assert.equal(h.paused(), true);
});

test('release at the physical track edge settles there even when native scroll pixels lag behind', () => {
  for (const edge of ['start','end']) {
    const h = setup();
    h.fire('pointerdown');
    // 保留中间像素位置，模拟原生轨道命中或释放时的回拨；以鼠标落点为准。
    h.scroll.scrollTop = 350;
    h.fire('pointerup', {clientY:edge === 'start' ? 30 : 530});
    assert.equal(h.ends[0].edge, edge);
    assert.equal(h.scroll.scrollTop, edge === 'start' ? 0 : 700);
    assert.equal(h.paused(), edge !== 'end');
  }
});

test('a stationary click near an arrow does not turn a single step into a boundary jump', () => {
  const h = setup();
  h.fire('pointerdown', {clientY:35});
  h.scroll.scrollTop = 320;
  h.fire('pointerup', {clientY:35});
  assert.equal(h.ends[0].edge, null);
  assert.equal(h.scroll.scrollTop, 320);
});

test('hover preparation only measures the scrollbar and is cleared on leave or loss of focus', () => {
  const h = setup();
  h.fire('pointermove', {}, h.scroll);
  assert.equal(h.scroll.classList.contains('scrollbar-ready'),true);
  assert.equal(h.dragging(),false);
  assert.equal(h.paused(),false);
  h.fire('pointermove', {clientX:100}, h.scroll);
  assert.equal(h.scroll.classList.contains('scrollbar-ready'),false);
  h.fire('pointermove', {}, h.scroll);
  h.fire('pointerleave', {}, h.scroll);
  assert.equal(h.scroll.classList.contains('scrollbar-ready'),false);
  h.fire('pointermove', {}, h.scroll);
  h.fire('blur');
  assert.equal(h.scroll.classList.contains('scrollbar-ready'),false);
});

test('history viewport captures its reading anchor before unlock and owns the final position', () => {
  const h = setup();
  let started = 0, released = 0;
  const token = {};
  h.context.App.historyScroll = {
    beginDrag() { started++; },
    captureRelease() {
      assert.equal(h.inner.style.getPropertyValue('--scrollbar-content-height'),'1200px');
      return token;
    },
    endDrag(cancelled, snapshot) {
      assert.equal(cancelled,false); assert.equal(snapshot,token);
      assert.equal(h.inner.style.getPropertyValue('--scrollbar-content-height'),'');
      h.scroll.scrollTop = 420; released++;
    },
  };
  h.fire('pointerdown');
  h.fire('pointerup',{clientY:30});
  assert.equal(started,1); assert.equal(released,1);
  assert.equal(h.scroll.scrollTop,420,'mouseup must not add another endpoint jump over the reading anchor');
});
