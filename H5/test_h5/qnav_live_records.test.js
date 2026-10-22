const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const {JSDOM}=require('jsdom');
const source=fs.readFileSync(path.join(__dirname,'../js/app/messages.js'),'utf8');
function setup(records){
  const dom=new JSDOM('<div id="chat"></div><div id="nav"><div id="rail"></div><div id="panel"></div></div>');
  const d=dom.window.document;
  const ctx={chatInner:d.querySelector('#chat'),qnav:d.querySelector('#nav'),qnavRail:d.querySelector('#rail'),qnavPanel:d.querySelector('#panel'),
    loadOlderState:{records},QNAV_MAX_DASHES:40,contentToPlainText:String,updateQnavActive(){},el(tag,cls,text){const n=d.createElement(tag);n.className=cls;if(text!=null)n.textContent=text;return n;}};
  vm.createContext(ctx);
  vm.runInContext(source.slice(source.indexOf('  let qnavUsers ='),source.indexOf('  // ---------- 从用户引用')),ctx);
  return {ctx,close:()=>dom.window.close(),user(text,round,index,event){const n=d.createElement('div');n.className='msg-user';
    if(round!=null)n.dataset.round=String(round);if(index!=null)n.dataset.historyIndex=String(index);if(event!=null)n.dataset.sourceEvent=String(event);
    const b=d.createElement('div');b.className='msg-bubble';b.textContent=text;n.append(b);ctx.chatInner.append(n);return n;},
    labels(){ctx.rebuildQnav();return Array.from(ctx.qnavPanel.children,n=>n.textContent);}};
}

test('同一轮的多条引导问题全部进入导航，不能按 round 把实时问题丢掉',t=>{
  const h=setup([{kind:'user',round:2,content:'原问题',source_event_index:0}]);t.after(h.close);
  h.user('原问题',2,0,0);h.user('补充要求 A',2);h.user('补充要求 B',2);
  assert.deepEqual(h.labels(),['1. 原问题','2. 补充要求 A','3. 补充要求 B']);
});

test('无轮次号的新问题不会被旧数据中的 NaN 轮次去重，同文问题仍各占一项',t=>{
  const h=setup([{kind:'user',content:'继续'}]);t.after(h.close);
  h.user('继续',null,0);h.user('继续');
  assert.deepEqual(h.labels(),['1. 继续','2. 继续']);
});

test('收养同轮同文气泡不重复，新增问题紧跟其所属轮次，编辑替换更新原入口',t=>{
  const h=setup([{kind:'user',round:1,content:'旧问题'},{kind:'user',round:2,content:'后续问题'}]);t.after(h.close);
  const replacement=h.user('修改后的问题',1);replacement.dataset.replacesRound='1';
  h.user('同轮补充',1);h.user('后续问题',2);
  assert.deepEqual(h.labels(),['1. 修改后的问题','2. 同轮补充','3. 后续问题']);
});

test('历史窗口恢复实时气泡时优先原对象，不按重复文本收养旧的“继续”',t=>{
  const h=setup([]);t.after(h.close);
  h.user('继续',1,0);
  const live=h.user('继续');live.remove();
  const message=h.ctx.el('div','msg');
  const active={sessionId:'s',userText:'继续',round:null,userNode:live,messageNode:message};
  const sessions=fs.readFileSync(path.join(__dirname,'../js/app/sessions.js'),'utf8');
  h.ctx.state={sessionId:'s',activeStream:active};h.ctx.setEmpty=()=>{};h.ctx.App={rebuildQnav:h.ctx.rebuildQnav};
  vm.runInContext(sessions.slice(sessions.indexOf('  function restoreActiveStream('),sessions.indexOf('  function noteSessionArchive(')),h.ctx);
  h.ctx.restoreActiveStream('s');
  assert.equal(active.userNode,live);assert.equal(live.parentNode,h.ctx.chatInner);
  assert.equal(h.ctx.chatInner.querySelectorAll('.msg-user').length,2);
});

test('导航面板重建保留阅读位置，原本停在末尾时跟随新增项',t=>{
  const h=setup([]);t.after(h.close);h.user('第一条');h.user('第二条');
  Object.defineProperties(h.ctx.qnavPanel,{clientHeight:{value:100},scrollHeight:{value:400}});
  h.ctx.qnavPanel.scrollTop=50;h.labels();assert.equal(h.ctx.qnavPanel.scrollTop,50);
  h.ctx.qnavPanel.scrollTop=300;h.user('最新问题');h.labels();assert.equal(h.ctx.qnavPanel.scrollTop,400);
});
