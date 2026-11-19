const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { stripTypeScriptTypes } = require('node:module');
const { JSDOM } = require('jsdom');

// 回归：附接长任务回放时，代码块复制按钮的定位扫描必须被抑制。
// 该函数每次全文档查 .codeblock .copy-btn 并逐个读 rect（强制同步布局）；
// 追赶期 DOM 不断长大，逐段调用 ≈ O(N²)，实测 14k 帧回放里独占 1.4s+。
// 修复：追赶期只记脏标，关窗后统一补一次；实时路径（滚动/缩放）仍同步更新。
const source = stripTypeScriptTypes(fs.readFileSync(path.join(__dirname, '../src/app/messages_nav.ts'), 'utf8'));

function setup(t, blocks = 3) {
  const dom = new JSDOM('<div id="scroll"><div id="chat"></div></div>');
  t.after(() => dom.window.close());
  const d = dom.window.document;
  const chatInner = d.querySelector('#chat');
  const scroll = d.querySelector('#scroll');
  let rectReads = 0;
  scroll.getBoundingClientRect = () => { rectReads++; return { top: 60, bottom: 560 }; };
  for (let i = 0; i < blocks; i++) {
    const block = d.createElement('div');
    block.className = 'codeblock';
    block.getBoundingClientRect = () => { rectReads++; return { top: 100 + i * 40, bottom: 140 + i * 40, width: 300 }; };
    const btn = d.createElement('button');
    btn.className = 'copy-btn';
    Object.defineProperties(btn, { offsetHeight: { value: 28 }, offsetWidth: { value: 60 } });
    btn.closest = sel => (sel === '.codeblock' ? block : null);
    block.appendChild(btn);
    const msg = d.createElement('div');
    msg.className = 'msg';
    msg.getBoundingClientRect = () => { rectReads++; return { top: 80, bottom: 400 }; };
    msg.appendChild(block);
    chatInner.appendChild(msg);
  }
  const context = {
    chatInner, chatScroll: scroll, document: d, window: dom.window,
    requestAnimationFrame: fn => fn(),
    getComputedStyle: () => ({ marginRight: '12px' }),
    App: {},
  };
  vm.createContext(context);
  const start = source.indexOf('  const COPY_PIN_OFFSET');
  const end = source.indexOf('  scrollBottomBtn.addEventListener', start);
  assert.ok(start >= 0 && end > start, '应能切出复制按钮定位段');
  vm.runInContext(source.slice(start, end), context);
  return {
    ctx: context,
    reads: () => rectReads,
    reset: () => { rectReads = 0; },
    update: () => context.updateCodeblockCopyButtons(),
    setFF: on => context.setReplayFastForward(on),
  };
}

test('实时路径不变：每次调用都做定位扫描', t => {
  const h = setup(t);
  h.reset();
  h.update();
  const first = h.reads();
  assert.ok(first > 0, '实时调用应读取布局');
  h.reset();
  h.update();
  assert.ok(h.reads() > 0, '第二次实时调用同样扫描');
});

test('追赶期抑制：多次调用零布局读取', t => {
  const h = setup(t);
  h.setFF(true);
  h.reset();
  for (let i = 0; i < 50; i++) h.update();
  assert.equal(h.reads(), 0, '追赶期不得触发全文档布局扫描');
});

test('关窗后统一补一次（脏标驱动）', t => {
  const h = setup(t);
  h.setFF(true);
  for (let i = 0; i < 50; i++) h.update();
  h.reset();
  h.setFF(false);
  assert.ok(h.reads() > 0, '关窗应补一次定位');
  const once = h.reads();
  h.reset();
  h.setFF(false); // 幂等：已关闭再关不应重复扫描
  assert.equal(h.reads(), 0);
  assert.ok(once > 0);
});

test('追赶期无渲染时关窗不做多余扫描', t => {
  const h = setup(t);
  h.setFF(true);
  h.reset();
  h.setFF(false);
  assert.equal(h.reads(), 0, '无脏标不扫描');
});

test('关窗后实时路径恢复同步扫描', t => {
  const h = setup(t);
  h.setFF(true);
  h.update();
  h.setFF(false);
  h.reset();
  h.update();
  assert.ok(h.reads() > 0, '关窗后实时调用恢复扫描');
});

test('切会话防御性复位：openSession 关闭遗留追赶标志', () => {
  const src = fs.readFileSync(path.join(__dirname, '../src/app/sessions.ts'), 'utf8');
  const start = src.indexOf('async function openSession(');
  const head = src.slice(start, start + 600);
  assert.match(head, /App\.setReplayFastForward\(false\)/, 'openSession 应复位追赶标志');
});

test('管线开关窗联动追赶标志（开→true，关→false）', () => {
  const src = fs.readFileSync(path.join(__dirname, '../src/app/stream_pipeline.ts'), 'utf8');
  const start = src.indexOf('function setReplayWindow(');
  const body = src.slice(start, src.indexOf('// 回放快进落定', start));
  assert.match(body, /App\.setReplayFastForward\(true\)/);
  assert.match(body, /App\.setReplayFastForward\(false\)/);
  // 关窗分支在 finally 内复位，异常路径也不会遗留抑制
  const finallyAt = body.indexOf('} finally {');
  assert.ok(finallyAt > 0 && body.indexOf('setReplayFastForward(false)') > finallyAt);
});
