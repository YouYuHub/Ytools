const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
function setup() {
  let top = 900;
  const listeners = {};
  const frames = new Map();
  let id = 0;
  const scroll = { style: { scrollBehavior: '' }, scrollHeight: 1000, clientHeight: 100,
    get scrollTop() { return top; }, set scrollTop(value) { top = Math.min(900, value); },
    addEventListener(name, fn) { listeners[name] = fn; },
  };
  const source = fs.readFileSync(path.join(__dirname, '../js/app/core.js'), 'utf8');
  const start = source.indexOf('  // 自动跟随与用户滚动');
  const end = source.indexOf('  function setEmpty(', start);
  const context = { chatScroll: scroll, updateScrollBottomOffset() {},
    requestAnimationFrame(fn) { frames.set(++id, fn); return id; },
    cancelAnimationFrame(key) { frames.delete(key); },
  };
  vm.createContext(context);
  vm.runInContext(source.slice(start, end), context);
  return { scroll, context, frames,
    fire(name, event = {}) { listeners[name](event); },
    flush() { const callbacks = [...frames.values()]; frames.clear(); callbacks.forEach(fn => fn()); },
  };
}
test('首次上滚即取消待执行置底，离底部不足24px也保持暂停', () => {
  const { scroll, context, frames, fire, flush } = setup();
  context.stickToBottom();
  assert.equal(frames.size, 1);
  fire('wheel', { deltaY: -5 });
  assert.equal(frames.size, 0);
  scroll.scrollTop = 895;
  fire('scroll');
  context.stickToBottom(); flush();
  assert.equal(scroll.scrollTop, 895);
  context.stickToBottom(); flush();
  assert.equal(scroll.scrollTop, 895);
});
test('取消失败的旧帧也不会将暂停的页面拉回底部', () => {
  const { scroll, context, frames, fire } = setup();
  context.scrollToBottom();
  const callback = [...frames.values()][0];
  fire('wheel', { deltaY: -50 });
  scroll.scrollTop = 850;
  callback();
  assert.equal(scroll.scrollTop, 850);
});
test('用户下滚到底部或主动回底恢复跟随，接近底部时不提前恢复', () => {
  const { scroll, context, fire, flush } = setup();
  fire('wheel', { deltaY: -20 }); scroll.scrollTop = 880; fire('scroll');
  scroll.scrollTop = 890; fire('scroll'); context.stickToBottom();
  assert.equal(scroll.scrollTop, 890);
  scroll.scrollTop = 900; fire('scroll'); scroll.scrollTop = 880; context.stickToBottom(); flush();
  assert.equal(scroll.scrollTop, 900);
  context.pauseAutoScroll(); scroll.scrollTop = 400;
  context.scrollToBottom(true); flush(); assert.equal(scroll.scrollTop, 900);
});
test('触摸向下滑和向上翻页均暂停跟随，输入框按键不干扰', () => {
  const { scroll, context, fire } = setup();
  fire('touchstart', { touches: [{ clientY: 100 }] });
  fire('touchmove', { touches: [{ clientY: 120 }] });
  scroll.scrollTop = 880; context.stickToBottom(); assert.equal(scroll.scrollTop, 880);
  context.scrollToBottom(true);
  fire('keydown', { key: 'PageUp', target: { tagName: 'DIV' } });
  scroll.scrollTop = 870; context.stickToBottom(); assert.equal(scroll.scrollTop, 870);
  context.scrollToBottom(true);
  fire('keydown', { key: 'ArrowUp', target: { tagName: 'TEXTAREA' } });
  scroll.scrollTop = 880; context.stickToBottom(); assert.equal(scroll.scrollTop, 900);
});
