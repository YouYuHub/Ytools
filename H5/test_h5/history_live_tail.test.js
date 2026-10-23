const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');

const source = name => fs.readFileSync(path.join(__dirname, '../js/app', name + '.js'), 'utf8');

function setup(t) {
  const dom = new JSDOM('<main><div id="scroll"><div id="inner"></div></div></main>', {runScripts:'outside-only'});
  t.after(() => dom.window.close());
  const w = dom.window, d = w.document, inner = d.querySelector('#inner'), scroll = d.querySelector('#scroll');
  let top = 0, serial = 0;
  const frames = new Map();
  w.requestAnimationFrame = fn => { frames.set(++serial, fn); return serial; };
  w.cancelAnimationFrame = id => frames.delete(id);
  const visible = () => [...inner.children].filter(n => !n.classList.contains('history-live-hidden') && !n.classList.contains('qnav-load-older'));
  const prefix = () => parseFloat(inner.style.getPropertyValue('--history-prefix-space')) || 0;
  const height = () => prefix() + visible().length * 50 + 120 + (parseFloat(inner.style.getPropertyValue('--history-suffix-space')) || 0);
  Object.defineProperties(scroll, {
    scrollHeight:{get:height}, clientHeight:{value:400},
    scrollTop:{get:() => top, set:v => { top = Math.max(0, Math.min(height() - 400, v)); }},
  });
  scroll.getBoundingClientRect = () => ({top:0,bottom:400});
  inner.style.rowGap = '24px';
  w.HistoryScrollModel = require('../js/history_scroll_model');
  const state = {sessionId:'s'};
  let navs = 0;
  const App = w.App = {state, chatInner:inner, chatScroll:scroll,
    el(tag, cls, text) {
      const n = d.createElement(tag); n.className = cls || ''; n.textContent = text || '';
      n.getBoundingClientRect = () => {
        const i = visible().indexOf(n), y = 64 + prefix() + Math.max(0,i) * 50 - top;
        return {top:y,bottom:y + (i < 0 ? 0 : 26),height:i < 0 ? 0 : 26};
      };
      return n;
    },
    rebuildQnav() { navs++; }, refreshAskBlockStates() {}, updateCodeblockCopyButtons() {},
  };
  Object.assign(w, App, {setEmpty() {}, scrollToBottom() { scroll.scrollTop = scroll.scrollHeight; },
    HistoryParser:require('../js/history_parser'), updateQnavActive() {}});
  const messages = source('messages');
  w.eval(messages.slice(messages.indexOf('  const RENDER_TAIL_RECORDS'), messages.indexOf('  function appendTime(')) +
    '\nApp.getHistoryWindow = () => loadOlderState;\n' +
    messages.slice(messages.indexOf('  App.showHistoryTail ='), messages.indexOf('  App.appendTime =')));
  w.renderOneRecordInto = (container, rec) => container.appendChild(App.el('div', rec.kind === 'user' ? 'msg-user' : 'msg-assistant-body',rec.content));
  App.renderHistoryWindowAt = w.renderHistoryWindowAt;
  const sessions = source('sessions');
  w.eval(sessions.slice(sessions.indexOf('  function restoreActiveStream('), sessions.indexOf('  function noteSessionArchive(')));
  App.restoreActiveStream = w.restoreActiveStream;
  w.eval(source('history_scroll'));
  const records = Array.from({length:500},(_,i) => ({kind:i%5 ? 'assistant' : 'user',round:Math.floor(i/5)+1,content:'history '+i}));
  w.renderRecords(records,{noScroll:true});
  const user = App.el('div','msg-user','最新消息'), msg = App.el('div','msg','实时回复');
  inner.append(user,msg);
  state.activeStream = {sessionId:'s',userNode:user,messageNode:msg,userText:'最新消息'};
  App.historyScroll.refresh();
  return {w,App,state,inner,scroll,user,msg,records,frames,navs:() => navs};
}

test('already-mounted live nodes lose stale history hiding when restoring the active stream', t => {
  const h = setup(t);
  h.user.classList.add('history-live-hidden'); h.msg.classList.add('history-live-hidden');
  h.App.restoreActiveStream('s');
  assert.equal(h.user.classList.contains('history-live-hidden'),false);
  assert.equal(h.msg.classList.contains('history-live-hidden'),false);
});

test('returning to an already-rendered tail repairs both hidden and detached live nodes synchronously', t => {
  const h = setup(t);
  h.user.classList.add('history-live-hidden'); h.msg.classList.add('history-live-hidden'); h.msg.remove();
  h.App.showHistoryTail();
  assert.equal(h.msg.parentNode,h.inner);
  assert.equal(h.user.classList.contains('history-live-hidden'),false);
  assert.equal(h.msg.classList.contains('history-live-hidden'),false);
  assert.ok(h.scroll.scrollHeight > 400);
});

test('history reading keeps live nodes hidden; returning to the tail restores each node once', t => {
  const h = setup(t);
  h.App.renderHistoryWindowAt(100);
  h.App.historyScroll.refresh();
  assert.ok(h.App.getHistoryWindow().end < h.records.length);
  h.App.restoreActiveStream('s');
  assert.equal(h.user.classList.contains('history-live-hidden'),true);
  assert.equal(h.msg.classList.contains('history-live-hidden'),true);
  h.App.showHistoryTail();
  assert.equal(h.App.getHistoryWindow().end,h.records.length);
  assert.equal(h.user.classList.contains('history-live-hidden'),false);
  assert.equal(h.msg.classList.contains('history-live-hidden'),false);
  assert.equal([...h.inner.children].filter(n => n === h.user || n === h.msg).length,2);
  const navs = h.navs();
  h.App.showHistoryTail();
  assert.equal(h.navs(),navs,'normal streaming follow must not rebuild question navigation every frame');
});

test('completed live rounds stay visible after browsing history and returning to the tail', t => {
  const h = setup(t);
  h.state.activeStream = null;
  h.App.renderHistoryWindowAt(100);
  h.App.historyScroll.refresh();
  h.App.showHistoryTail();
  assert.equal(h.user.parentNode,h.inner);
  assert.equal(h.msg.parentNode,h.inner);
  assert.equal(h.user.classList.contains('history-live-hidden'),false);
  assert.equal(h.msg.classList.contains('history-live-hidden'),false);
});

test('window rebuild keeps completed live rounds before the newest mounted stream', t => {
  const h = setup(t);
  const user2 = h.App.el('div','msg-user','下一条消息'), msg2 = h.App.el('div','msg','新的实时回复');
  h.inner.append(user2,msg2);
  h.state.activeStream = {sessionId:'s',userNode:user2,messageNode:msg2};
  h.App.historyScroll.refresh();
  // onScroll 重建期间 refresh 被暂停：先补挂当前流，再由滚动层补回此前轮次。
  [h.user,h.msg,user2,msg2].forEach(n => n.remove());
  h.App.restoreActiveStream('s');
  h.App.historyScroll.revealTail();
  assert.deepEqual([...h.inner.children].slice(-4),[h.user,h.msg,user2,msg2]);
  assert.equal(h.inner.lastElementChild,msg2,'the bottom must contain the newest stream, not the previous answer');
});

test('a replay question created after its reply container still appears before the reply', t => {
  const h = setup(t);
  h.state.activeStream.userNode = null;
  h.user.remove();
  h.App.historyScroll.reset();
  h.App.historyScroll.refresh();
  h.inner.insertBefore(h.user,h.msg);
  h.state.activeStream.userNode = h.user;
  h.App.historyScroll.revealTail();
  h.user.remove(); h.msg.remove();
  h.App.historyScroll.revealTail();
  assert.deepEqual([...h.inner.children].slice(-2),[h.user,h.msg]);
});
test('导航在历史窗口定位和拖动释放完成后同步，不停留在中间布局', t => {
  const h=setup(t),seen=[];
  h.App.updateQnavActive=()=>seen.push({end:h.App.getHistoryWindow().end,
    gap:h.scroll.scrollHeight-h.scroll.scrollTop-h.scroll.clientHeight});
  h.App.renderHistoryWindowAt(100);h.App.historyScroll.refresh();
  h.App.historyScroll.beginDrag();h.scroll.scrollTop=h.scroll.scrollHeight;
  h.App.historyScroll.onScroll();
  assert.equal(seen.at(-1).end,h.records.length);assert.ok(seen.at(-1).gap<=2);
  const release=h.App.historyScroll.captureRelease(),before=seen.length;
  h.App.historyScroll.endDrag(false,release);
  assert.ok(seen.length>before);assert.ok(seen.at(-1).gap<=2);
});

test('移除的压缩副本及关联过程摘要不会被实时尾部缓存恢复',t=>{
  const h=setup(t),bar=h.App.el('div','compaction-bar is-running','stale'),keep=h.App.el('div','compaction-bar is-done','saved');
  const summary=h.App.el('div','process-group-summary is-running','processing');
  summary._processSummary={members:[bar,keep]};h.inner.append(summary,bar,keep);h.App.historyScroll.refresh();
  h.App.historyScroll.discardNodes([bar]);h.App.historyScroll.refresh();
  h.App.renderHistoryWindowAt(100);h.App.historyScroll.refresh();h.App.showHistoryTail();
  assert.equal(bar.isConnected,false);assert.equal(summary.isConnected,false);
  assert.equal(keep.parentNode,h.inner);assert.equal(h.msg.parentNode,h.inner);
});

test('真实过程组在历史重建后只有一个标题，连续刷新不反复移动成员',t=>{
  const h=setup(t);h.w.eval(source('process_groups'));
  const first=h.App.el('div','compaction-bar is-done','first'),second=h.App.el('div','compaction-bar is-done','second');
  h.inner.append(first,second);h.App.processGroups.sync(h.inner);h.App.historyScroll.refresh();
  h.App.renderHistoryWindowAt(100);h.App.historyScroll.refresh();h.App.showHistoryTail();h.App.processGroups.sync(h.inner);
  assert.equal(h.inner.querySelectorAll('.process-group-summary').length,1);
  const observer=new h.w.MutationObserver(()=>{});observer.observe(h.inner,{childList:true});
  h.App.historyScroll.refresh();h.App.processGroups.sync(h.inner);h.App.historyScroll.refresh();
  assert.equal(observer.takeRecords().length,0,'stable refresh must not append stages again around generated headers');
  observer.disconnect();
});

test('底部留白不应使已加载的尾部窗口反复重建',t=>{
  const h=setup(t);
  const info=h.App.getHistoryWindow();
  let rebuilds=0;
  const render=h.App.renderHistoryWindowAt;
  h.App.renderHistoryWindowAt=(...args)=>{rebuilds++;return render(...args);};
  // Actual content ends above padding + the trailing inter-record gap. The
  // viewport can legitimately contain this blank space just before bottom.
  const last=h.msg;
  h.scroll.scrollTop=h.scroll.scrollHeight-h.scroll.clientHeight-12;
  const top=h.scroll.scrollTop;
  const original=last.getBoundingClientRect;
  last.getBoundingClientRect=()=>({top:210,bottom:340,height:130});
  h.App.historyScroll.onScroll();
  assert.equal(rebuilds,0,'12px above bottom is not a missing history window');
  assert.equal(h.scroll.scrollTop,top,'do not remap the reader into the final answer');
  assert.equal(h.App.getHistoryWindow().end,info.end);
  last.getBoundingClientRect=original;
});

test('末尾外边距计入可视范围，继续上滚时不抢回阅读位置',t=>{
  const h=setup(t);let rebuilds=0;
  h.App.renderHistoryWindowAt=()=>{rebuilds++;};
  h.msg.style.marginBottom='12px';
  h.msg.getBoundingClientRect=()=>({top:210,bottom:320,height:110});
  h.scroll.scrollTop=h.scroll.scrollHeight-h.scroll.clientHeight-12;
  h.scroll.dispatchEvent(new h.w.WheelEvent('wheel',{deltaY:-8}));
  const top=h.scroll.scrollTop;
  for(let i=0;i<12;i++)h.App.historyScroll.onScroll();
  assert.equal(rebuilds,0);
  assert.equal(h.scroll.scrollTop,top);
});

test('未加载的后续历史不能用尾部 padding 抵消，仍须切换窗口',t=>{
  const h=setup(t);
  h.App.renderHistoryWindowAt(100);h.App.historyScroll.refresh();
  assert.ok(h.App.getHistoryWindow().end<h.records.length);
  let rebuilds=0;const render=h.App.renderHistoryWindowAt;
  h.App.renderHistoryWindowAt=(...args)=>{rebuilds++;return render(...args);};
  h.scroll.scrollTop=h.scroll.scrollHeight-h.scroll.clientHeight-1.5;
  h.App.historyScroll.onScroll();
  assert.equal(rebuilds,1);
  assert.equal(h.App.getHistoryWindow().end,h.records.length);
  assert.ok(h.scroll.scrollHeight-h.scroll.scrollTop-h.scroll.clientHeight<=2);
});

test('首屏顶部留白不触发已加载窗口重建',t=>{
  const h=setup(t);
  h.App.renderHistoryWindowAt(0);h.App.historyScroll.refresh();
  let rebuilds=0;
  h.App.renderHistoryWindowAt=()=>{rebuilds++;};
  h.scroll.scrollTop=12;
  h.App.historyScroll.onScroll();
  assert.equal(rebuilds,0);
  assert.equal(h.scroll.scrollTop,12);
});
