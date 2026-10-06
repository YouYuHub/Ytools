const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
class Node {
  constructor(cls, text = '') { this.cls = cls; this.textContent = text; this.children = []; this.dataset = {}; this.classList = { add() {}, remove() {} }; }
  appendChild(n) { if (n.parentNode) n.parentNode.removeChild(n); this.children.push(n); n.parentNode = this; }
  removeChild(n) { this.children.splice(this.children.indexOf(n), 1); n.parentNode = null; }
  insertBefore(n, anchor) { if (n.parentNode) n.parentNode.removeChild(n); this.children.splice(this.children.indexOf(anchor), 0, n); n.parentNode = this; }
  querySelector(selector) {
    if (selector === '.msg-bubble') return this.children.find(n => n.cls === 'msg-bubble') || null;
    const match = selector.match(/data-round="(\d+)"/);
    return match ? this.querySelectorAll('.msg-user').find(n => n.dataset.round === match[1]) || null : null;
  }
  querySelectorAll(selector) { return this.children.flatMap(n => [...(n.cls === selector.slice(1) ? [n] : []), ...n.querySelectorAll(selector)]); }
  addEventListener() {}
  set innerHTML(value) { this.children = []; }
}
function user(root, text, round) {
  const n = new Node('msg-user'); n.dataset.round = String(round);
  n.appendChild(new Node('msg-bubble', text)); root.appendChild(n); return n;
}
for (const reused of [false, true]) test(`附接进行中的第三轮提问立即进入问题导航（${reused ? '复用' : '新建'}气泡）`, async () => {
  const chatSource = fs.readFileSync(path.join(__dirname, '../js/app/chat.js'), 'utf8');
  const messages = fs.readFileSync(path.join(__dirname, '../js/app/messages.js'), 'utf8');
  const root = new Node('root'); user(root, '第一条', 1); user(root, '第二条', 2);
  if (reused) user(root, '生成中的第三条', 3);
  const rail = new Node('rail'); const panel = new Node('panel');
  let checked = false;
  const context = {
    chatInner: root, qnav: new Node('qnav'), qnavRail: rail, qnavPanel: panel, QNAV_MAX_DASHES: 40,
    el: (tag, cls, text) => new Node(cls, text), updateQnavActive() {},
    state: { sessionId: 'session' }, AbortController, scrollToBottom() {}, setEmpty() {},
    createStreamPipeline() { return { handle() {}, setRoundModel() {}, finish() { return {}; } }; },
    setTimeout() {},
    App: {
      refreshComposerButtons() {}, refreshContextTokenStats() {},
      attachLiveRoundEntry() {}, async refreshSessionUsage() { return true; }, clearContextStatsTimer() {}, scheduleContextTokenStatsRefresh() {},
      appendUserMessage(text) { return user(root, text, 3); },
    },
    API: { async chatStream(body, receive) {
      receive({ data: { replay: true, round: 3, question_text: '生成中的第三条' } });
      // 在流结束前断言，不依赖收尾重载。
      assert.equal(panel.children.length, 3);
      assert.equal(panel.children[2].textContent, '3. 生成中的第三条');
      assert.equal(rail.children.length, 3);
      assert.equal(root.querySelectorAll('.msg-user').length, 3);
      checked = true;
    } },
  };
  vm.createContext(context);
  const navStart = messages.indexOf('  let qnavUsers =');
  const navEnd = messages.indexOf('  // ---------- 从用户引用', navStart);
  vm.runInContext(messages.slice(navStart, navEnd), context);
  context.App.rebuildQnav = context.rebuildQnav;
  context.rebuildQnav();
  const start = chatSource.indexOf('  async function attachStreamSession(');
  const end = chatSource.indexOf('  /** 打开会话后', start);
  vm.runInContext(chatSource.slice(start, end), context);
  await context.attachStreamSession('session');
  assert.equal(checked, true);
});
