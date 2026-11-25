const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { stripTypeScriptTypes } = require('node:module');
const { JSDOM } = require('jsdom');

// 回归：生成中「顶部折叠块跑到 user 消息上方 + 持续计时」，刷新/切回才恢复。
// 两个根因：
// 1) 非末段（引导气泡之前的内容）按检查点语义已结算，但 renderHeader /
//    foldActiveNow 只扫成员进行态标记，残留标记（工具 done 未及打标、
//    子块状态滞后）会让封存段一直亮灯跳秒；历史回放全 settled 才"恢复"。
// 2) 同轮扁平+容器合并时，早于本轮用户气泡的残留节点（编辑重发手术后
//    未清的思考/工具/正文、容器与气泡顺序错位）被纳入成员，折叠头
//    insertBefore(members[0]) 于是落到 user 上方（上一轮用量行之后）。
function harness() {
  const dom = new JSDOM('<div id="chatInner"></div>');
  const { window } = dom; const document = window.document;
  const chatInner = document.querySelector('#chatInner');
  const el = (t, c, x) => { const n = document.createElement(t); if (c) n.className = c; if (x != null) n.textContent = x; return n; };
  const App = {
    state: { sessionId: 's', activeStream: null, streaming: true, streamingSession: 's' },
    chatInner, el,
    historyScroll: { refresh() {} }, updateCodeblockCopyButtons() {}, pauseAutoScroll() {},
  };
  const ctx = vm.createContext({ App, window, document, Date, console, setInterval: () => 0, clearInterval: () => {} });
  window.App = App;
  for (const f of ['process_groups.ts', 'round_fold.ts']) {
    vm.runInContext(stripTypeScriptTypes(fs.readFileSync(path.join(__dirname, '../src/app', f), 'utf8')), ctx);
  }
  const sync = c => ctx.App.processGroups.sync(c);
  const mk = (cls, round) => { const n = el('div', cls); if (round != null) n.dataset.round = String(round); return n; };
  const heads = () => Array.from(chatInner.querySelectorAll('.round-fold-summary'));
  return { dom, document, chatInner, App, el, mk, sync, heads };
}

// 直播轮：user 气泡 + msg 容器（思考/工具/正文 → 引导气泡 → 直播正文）
function liveRound(h, { strayExecuting = false } = {}) {
  const user = h.mk('msg msg-user', 21); h.chatInner.appendChild(user);
  const msg = h.mk('msg', 21); h.chatInner.appendChild(msg);
  h.App.state.activeStream = { messageNode: msg, userNode: user, completed: false };
  const tool = h.mk('tool-block' + (strayExecuting ? ' is-executing' : ''));
  if (strayExecuting) tool.innerHTML = '<svg class="icon tool-status-icon"></svg>';
  msg.appendChild(tool);
  msg.appendChild(h.mk('msg-assistant-body'));
  const guide = h.mk('msg msg-user', 21); msg.appendChild(guide);
  // 末段需 ≥2 个成员才建折叠（单小块不折叠规则）：思考 + 已完成工具 + 直播正文
  msg.appendChild(h.mk('think-block'));
  msg.appendChild(h.mk('tool-block'));
  const tail = h.mk('msg-assistant-body msg-cursor'); msg.appendChild(tail);
  h.sync(h.chatInner);
  return { user, msg, guide, tail, tool };
}

test('非末段（引导前内容）即使残留进行态标记也不亮灯/不计时', t => {
  const h = harness(); t.after(() => h.dom.window.close());
  liveRound(h, { strayExecuting: true });
  const sealed = h.heads().find(x => x._roundFold && !/:s\d+$/.test(x._roundFold.key));
  assert.ok(sealed, '引导前段应建折叠');
  assert.equal(sealed.classList.contains('is-running'), false, '封存段不得亮运行态（否则在引导气泡上方持续跳秒）');
  const live = h.heads().find(x => x._roundFold && /:s1$/.test(x._roundFold.key));
  assert.ok(live, '末段应建折叠');
  assert.equal(live.classList.contains('is-running'), true, '末段（直播）仍正常计时');
});

test('末段无残留标记时运行态不受影响（回归保护）', t => {
  const h = harness(); t.after(() => h.dom.window.close());
  liveRound(h);
  const sealed = h.heads().find(x => x._roundFold && !/:s\d+$/.test(x._roundFold.key));
  const live = h.heads().find(x => x._roundFold && /:s1$/.test(x._roundFold.key));
  assert.equal(sealed.classList.contains('is-running'), false);
  assert.equal(live.classList.contains('is-running'), true);
});

test('早于本轮气泡的残留节点不得把折叠头顶到 user 消息上方', t => {
  const h = harness(); t.after(() => h.dom.window.close());
  const { user, msg } = liveRound(h);
  // 模拟顺序错位/手术残留：容器被摆到气泡之前
  h.chatInner.insertBefore(msg, user);
  h.sync(h.chatInner);
  const above = h.heads().filter(x => x.parentNode === h.chatInner && (user.compareDocumentPosition(x) & 2));
  assert.equal(above.length, 0, '折叠头不得出现在本轮 user 气泡之前');
  // 残留节点保持可见（不折叠、不隐藏），避免"头没了内容也被藏"
  const hiddenBefore = Array.from(msg.children).filter(n => n.classList.contains('is-roundfold-hidden'));
  assert.equal(hiddenBefore.length, 0, '早于气泡的残留节点保持可见');
});

test('游离直播容器（切会话/窗口重建）成员不被位置护栏误丢弃', t => {
  const h = harness(); t.after(() => h.dom.window.close());
  const { user, msg } = liveRound(h);
  const before = h.heads().length;
  assert.ok(before > 0);
  // 容器游离：compareDocumentPosition 返回 DISCONNECTED，不能据此丢成员
  msg.remove(); user.remove();
  h.sync(h.chatInner);
  h.chatInner.appendChild(user); h.chatInner.appendChild(msg);
  h.sync(h.chatInner);
  assert.ok(h.heads().length > 0, '挂回后折叠重建');
  const above = h.heads().filter(x => x.parentNode === h.chatInner && (user.compareDocumentPosition(x) & 2));
  assert.equal(above.length, 0);
});

// 回归：引导注入后末段处于纯工具阶段（尚无新正文、不建末段折叠），
// 封存段（无尾、永不计时）不得把顶部直播计时行藏掉——否则这段时间
// 页面上没有任何计时显示，直到新正文出现才恢复。
test('引导后纯工具阶段：封存段不接管计时行，顶部计时保持可见', t => {
  const h = harness(); t.after(() => h.dom.window.close());
  const user = h.mk('msg msg-user', 21); h.chatInner.appendChild(user);
  const msg = h.mk('msg', 21); h.chatInner.appendChild(msg);
  h.App.state.activeStream = { messageNode: msg, userNode: user, completed: false };
  const row = h.el('div', 'live-round-timer'); msg.appendChild(row);
  msg.appendChild(h.mk('think-block'));
  msg.appendChild(h.mk('tool-block'));
  msg.appendChild(h.mk('msg-assistant-body'));
  h.sync(h.chatInner);
  assert.equal(row.classList.contains('is-taken-over'), true, '首段成折叠后正常接管隐藏');
  // 注入引导 + 后续多个已完成工具（无新正文）
  msg.appendChild(h.mk('msg msg-user', 21));
  msg.appendChild(h.mk('tool-block'));
  msg.appendChild(h.mk('think-block'));
  msg.appendChild(h.mk('tool-block'));
  h.sync(h.chatInner);
  const running = h.heads().filter(x => x.classList.contains('is-running'));
  assert.equal(running.length, 0, '封存段不亮灯，末段无正文不建折叠');
  assert.equal(row.classList.contains('is-taken-over'), false, '无任何显示计时的折叠时，顶部计时行必须可见');
  // 新正文流式出现 → 末段成折叠 → 重新接管隐藏
  msg.appendChild(h.mk('think-block'));
  msg.appendChild(h.mk('msg-assistant-body msg-cursor'));
  h.sync(h.chatInner);
  assert.equal(row.classList.contains('is-taken-over'), true, '末段折叠形成后重新接管');
});

test('多条引导：旧末段转封存后交还计时行（集中判定覆盖段迁移）', t => {
  const h = harness(); t.after(() => h.dom.window.close());
  const user = h.mk('msg msg-user', 21); h.chatInner.appendChild(user);
  const msg = h.mk('msg', 21); h.chatInner.appendChild(msg);
  h.App.state.activeStream = { messageNode: msg, userNode: user, completed: false };
  const row = h.el('div', 'live-round-timer'); msg.appendChild(row);
  msg.appendChild(h.mk('think-block'));
  msg.appendChild(h.mk('tool-block'));
  msg.appendChild(h.mk('msg-assistant-body'));
  h.sync(h.chatInner);
  // 第一条引导 + 第二段内容成折叠（有尾正文）→ 接管
  msg.appendChild(h.mk('msg msg-user', 21));
  msg.appendChild(h.mk('think-block'));
  msg.appendChild(h.mk('tool-block'));
  msg.appendChild(h.mk('msg-assistant-body'));
  h.sync(h.chatInner);
  assert.equal(row.classList.contains('is-taken-over'), true);
  // 第二条引导：旧末段变封存段，新末段只有工具 → 交还计时行
  msg.appendChild(h.mk('msg msg-user', 21));
  msg.appendChild(h.mk('tool-block'));
  h.sync(h.chatInner);
  assert.equal(row.classList.contains('is-taken-over'), false, '所有带尾折叠都已封存时必须交还计时行');
});
