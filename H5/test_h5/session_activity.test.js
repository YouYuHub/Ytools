const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path');
const {JSDOM}=require('jsdom');
const T=require('../js/task_state');
const source=name=>fs.readFileSync(path.join(__dirname,'../js/app',name+'.js'),'utf8');
const settle=()=>new Promise(r=>setImmediate(r));
function setup(t){
  const dom=new JSDOM('<div class="session-item" data-session="s"><span class="session-activity" hidden></span></div><div class="session-item" data-session="s"><span class="session-activity" hidden></span></div><div class="session-item" data-session="c"><span class="session-activity" hidden></span></div>',{runScripts:'outside-only',pretendToBeVisual:true,url:'http://test'});
  const w=dom.window,timers=new Map();let serial=0,hidden=false;
  Object.defineProperty(w.document,'hidden',{get:()=>hidden});
  w.setTimeout=(fn,delay)=>{const id=++serial;timers.set(id,{fn,delay});return id;};w.clearTimeout=id=>timers.delete(id);
  w.TaskState=T;w.App={state:{sessionId:'s',streaming:false},refreshSessionFades(){}};
  w.API={streamStatuses:async()=>({sessions:[]})};w.eval(source('session_activity'));
  t.after(()=>{w.App.sessionActivity.stop();dom.window.close();});
  return {w,a:w.App.sessionActivity,timers,icons:()=>[...w.document.querySelectorAll('.session-activity')],
    visibility(value){hidden=value;w.document.dispatchEvent(new w.Event('visibilitychange'));}};
}
const running=()=>T.reduce(T.initial(),{type:'connection',value:'live'});
const snapshot=(...rows)=>({sessions:rows});
const chat=sid=>({session_id:sid,chat_running:true,compacting:false});

test('本地任务立即更新同会话的所有行，等待与断线不伪装持续运行，结束清除',t=>{
  const x=setup(t);let s=running();x.a.note('s',s,1);
  assert.equal(x.icons()[0].hidden,false);assert.equal(x.icons()[1].hidden,false);assert.equal(x.icons()[2].hidden,true);
  s=T.reduce(s,{type:'frame',data:{event:'task_usage',task:{kind:'chat',state:'paused'}}});x.a.note('s',s,1);
  assert.equal(x.icons()[0].dataset.kind,'waiting');assert.match(x.icons()[0].title,/预算/);
  s=T.reduce(s,{type:'end',completed:false});x.a.note('s',s,1);assert.equal(x.icons()[0].dataset.kind,'uncertain');
  x.a.note('s',T.reduce(s,{type:'stopped'}),1);assert.equal(x.icons()[0].hidden,true);
});
test('一次快照恢复其他会话和手动压缩，后台结束自动移除；新建行使用当前缓存',async t=>{
  const x=setup(t);x.w.API.streamStatuses=async()=>snapshot(chat('s'),{session_id:'c',chat_running:false,compacting:true});
  await x.a.sync();assert.equal(x.icons()[0].hidden,false);assert.match(x.icons()[2].title,/压缩/);
  const row=x.w.document.createElement('div');row.dataset.session='s';row.innerHTML='<span class="session-activity" hidden></span>';x.a.decorate(row);
  assert.equal(row.firstChild.hidden,false);
  x.w.API.streamStatuses=async()=>snapshot();await x.a.sync();assert.ok(x.icons().every(n=>n.hidden));
});
test('过期快照不能清除新发送或让已完成任务重新转圈',async t=>{
  const x=setup(t);let resolve;
  x.w.API.streamStatuses=()=>new Promise(r=>resolve=r);
  const first=x.a.sync();await settle();x.a.note('s',running(),1);resolve(snapshot());await first;
  assert.equal(x.icons()[0].hidden,false);
  const second=x.a.sync();await settle();x.a.note('s',T.reduce(running(),{type:'end',completed:true}),1);
  resolve(snapshot(chat('s')));await second;assert.equal(x.icons()[0].hidden,true);
});
test('实时监听的本地阶段优先；切走后按服务器状态结束，不残留转圈',async t=>{
  const x=setup(t);Object.assign(x.w.App.state,{streaming:true,streamingSession:'s'});
  x.a.note('s',running(),1);await x.a.sync();assert.equal(x.icons()[0].hidden,false);
  x.w.App.state.streaming=false;x.a.note('s',T.reduce(running(),{type:'connection',value:'background'}),1);
  await x.a.sync();assert.equal(x.icons()[0].hidden,true);
});
test('重复同阶段 token 不修改图标 DOM，隐藏主状态栏不影响侧栏',t=>{
  const x=setup(t),d=x.w.document;
  d.body.insertAdjacentHTML('beforeend','<div id="taskStatus"><span class="task-status-label"></span><span class="task-status-detail"></span><button></button></div>');
  x.w.localStorage.setItem('ytools-task-status-visible','0');x.w.eval(source('task_status'));
  const h=x.w.App.taskStatus.begin('s');x.w.App.taskStatus.connected(h);
  assert.equal(d.querySelector('#taskStatus').hidden,true);assert.equal(x.icons()[0].hidden,false);
  const observer=new x.w.MutationObserver(()=>{});observer.observe(x.icons()[0],{attributes:true});
  x.w.App.taskStatus.event(h,{data:{usage:{total_tokens:4}}});assert.equal(observer.takeRecords().length,0);observer.disconnect();
});
test('请求失败保留未知标记，恢复连接后更新；404 旧后端停止轮询',async t=>{
  const x=setup(t);x.w.API.streamStatuses=async()=>snapshot(chat('s'));await x.a.sync();
  x.w.API.streamStatuses=async()=>{throw new Error('offline');};await x.a.sync();
  assert.equal(x.icons()[0].hidden,false);assert.equal(x.icons()[0].dataset.kind,'uncertain');
  x.w.API.streamStatuses=async()=>snapshot();await x.a.sync();assert.equal(x.icons()[0].hidden,true);
  let calls=0;x.w.API.streamStatuses=async()=>{calls++;throw Object.assign(new Error('old'),{status:404});};
  await x.a.sync();await x.a.sync();assert.equal(calls,1);
  x.a.note('s',T.reduce(running(),{type:'connection',value:'background'}),2);
  assert.equal(x.icons()[0].dataset.kind,'uncertain');
});
test('请求合并、活跃/空闲间隔、隐藏暂停、重新可见和卸载清理',async t=>{
  const x=setup(t);let resolve,calls=0;
  x.w.API.streamStatuses=()=>{calls++;return new Promise(r=>resolve=r);};
  x.a.start();const same=x.a.sync();await settle();assert.equal(calls,1);resolve(snapshot(chat('s')));await same;
  assert.deepEqual([...x.timers.values()].map(t=>t.delay),[4000]);
  x.visibility(true);assert.equal(x.timers.size,0);
  x.w.API.streamStatuses=async()=>{calls++;return snapshot();};x.visibility(false);await settle();
  assert.equal(calls,2);assert.deepEqual([...x.timers.values()].map(t=>t.delay),[12000]);
  x.w.dispatchEvent(new x.w.Event('pagehide'));assert.equal(x.timers.size,0);
});
test('真实会话行把指示器放在标题右侧，保留菜单按钮和标题文本结构',t=>{
  const x=setup(t);x.w.App.el=(tag,cls,text)=>{const n=x.w.document.createElement(tag);n.className=cls;n.textContent=text||'';return n;};
  const all=source('sessions'),start=all.indexOf('  function buildSessionItem('),end=all.indexOf('  // ---------- 会话多选',start);
  x.w.eval('(function(App){const {state,el}=App;const bulkMode=false,bulkSelected=new Set();'+all.slice(start,end)+'App.buildForTest=buildSessionItem;})(window.App);');
  x.a.note('s',running(),1);const row=x.w.App.buildForTest('s','完整标题');
  assert.equal(row.querySelector('.session-name-text').textContent,'完整标题');
  assert.equal(row.querySelector('.session-select').lastChild.className,'session-activity');
  assert.equal(row.querySelector('.session-activity').hidden,false);assert.ok(row.querySelector('.session-actions'));
});
test('页面隐藏中止在途请求，立即返回时补查，不把主动中止当成后端故障',async t=>{
  const x=setup(t);let calls=0;
  x.w.API.streamStatuses=signal=>{calls++;if(calls>1)return Promise.resolve(snapshot(chat('s')));
    return new Promise((resolve,reject)=>signal.addEventListener('abort',()=>reject(new Error('cancelled'))));};
  x.a.start();await settle();x.visibility(true);x.visibility(false);await settle();await settle();
  assert.equal(calls,2);assert.equal(x.icons()[0].dataset.kind,'running');
  assert.deepEqual([...x.timers.values()].map(t=>t.delay),[4000]);
});
test('空闲时新任务立即转为活跃检查间隔，同阶段更新不反复推迟检查',async t=>{
  const x=setup(t);x.a.start();await settle();assert.deepEqual([...x.timers.values()].map(t=>t.delay),[12000]);
  x.a.note('s',running(),1);assert.deepEqual([...x.timers.values()].map(t=>t.delay),[4000]);
  const before=[...x.timers.keys()];x.a.note('s',running(),1);assert.deepEqual([...x.timers.keys()],before);
});
