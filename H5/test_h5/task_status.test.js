const test=require('node:test');const assert=require('node:assert/strict');
const fs=require('node:fs');const path=require('node:path');const {JSDOM}=require('jsdom');
function setup(t){
  const dom=new JSDOM('<div id="chat"></div><div id="taskStatus" hidden><span class="task-status-label"></span><span class="task-status-detail"></span><button hidden>重新连接</button></div>',{runScripts:'outside-only'});
  const w=dom.window,host=w.document.querySelector('#taskStatus');
  // 跟踪模块调度的定时器，测试结束时清理，防止不确定态重试跨用例触发
  const pendingTimers=new Set();
  const rawSetTimeout=w.setTimeout.bind(w),rawClearTimeout=w.clearTimeout.bind(w);
  w.setTimeout=function(fn,ms){const id=rawSetTimeout(function(){pendingTimers.delete(id);fn();},ms);pendingTimers.add(id);return id;};
  w.clearTimeout=function(id){pendingTimers.delete(id);return rawClearTimeout(id);};
  t.after(()=>{pendingTimers.forEach(id=>{try{rawClearTimeout(id);}catch(_){}});pendingTimers.clear();dom.window.close();});
  const calls={attached:[],opened:[],toasts:[],flushed:[]};
  w.TaskState=require('../js/task_state');w.App={state:{sessionId:'s',streaming:false},
    toast:m=>calls.toasts.push(m),attachStreamSession:async sid=>calls.attached.push(sid),openSession:async sid=>calls.opened.push(sid)};
  w.API={streamStatus:async()=>({running:true})};
  w.eval(fs.readFileSync(path.join(__dirname,'../js/app/task_status.js'),'utf8'));
  return {w,host,calls,status:w.App.taskStatus};
}
const settle=()=>new Promise(r=>setImmediate(r));
test('手动压缩全程同步状态条：开始/官方窗口/完成',t=>{
  const x=setup(t);
  const h=x.status.compactBegin('s');
  assert.equal(x.host.hidden,false);
  assert.equal(x.host.dataset.phase,'compacting');
  assert.equal(x.host.querySelector('.task-status-detail').textContent,'跨轮历史');
  x.status.compactEvent(h,{event:'responses_compaction',phase:'start'});
  assert.equal(x.host.querySelector('.task-status-detail').textContent,'官方压缩窗口');
  x.status.compactEvent(h,{event:'responses_compaction',phase:'done'});
  x.status.compactEvent(h,{event:'context_compaction',phase:'done',scope:'session'});
  x.status.compactEnd(h,{});
  assert.equal(x.host.dataset.phase,'completed');
});
test('手动压缩失败与主动终止分别显示任务出错与已停止',t=>{
  const x=setup(t);
  const failed=x.status.compactBegin('s');
  x.status.compactEvent(failed,{event:'context_compaction',phase:'aborted',error:'boom'});
  x.status.compactEnd(failed,{error:'boom'});
  assert.equal(x.host.dataset.phase,'failed');
  const stopped=x.status.compactBegin('s');
  assert.equal(x.host.dataset.phase,'compacting');
  x.status.compactEnd(stopped,{stopped:true});
  assert.equal(x.host.dataset.phase,'stopped');
});

test('压缩断线的重新连接只附接压缩任务；404 终态同步任务状态',async t=>{
  const x=setup(t),attached=[];x.w.App.attachManualCompaction=async sid=>attached.push(sid);
  x.w.API.manualCompactionStatus=async()=>({running:true});
  const h=x.status.compactBegin('s');x.status.compactEnd(h,{disconnected:true});
  assert.equal(x.host.dataset.phase,'disconnected');x.host.querySelector('button').click();await settle();
  assert.deepEqual(attached,['s']);assert.deepEqual(x.calls.attached,[]);
  x.status.compactEnd(h,{missing:true});assert.equal(x.host.dataset.phase,'ended');assert.equal(x.status.isUncertain('s'),false);
});
test('会话状态可隐藏/显示，隐藏时不展示任何阶段',t=>{
  const x=setup(t);
  const h=x.status.begin('s');x.status.connected(h);
  x.status.event(h,{data:{content:'hi'}});
  assert.equal(x.host.hidden,false);assert.equal(x.host.dataset.phase,'answering');
  assert.equal(x.status.toggleStatusVisible(),false);
  assert.equal(x.host.hidden,true);
  x.status.event(h,{data:{reasoning_content:'still'}});
  assert.equal(x.host.hidden,true);
  assert.equal(x.status.setStatusVisible(true),true);
  assert.equal(x.host.hidden,false);assert.equal(x.host.dataset.phase,'thinking');
});
test('断线后只附接后台任务，不重新发送提问；结束后重新读取历史',async t=>{
  const x=setup(t),h=x.status.begin('s');x.status.connected(h);await x.status.end(h,false);
  assert.equal(x.status.isUncertain('s'),true);assert.equal(x.host.querySelector('button').hidden,false);
  x.host.querySelector('button').click();await settle();assert.deepEqual(x.calls.attached,['s']);
  x.w.API.streamStatus=async()=>({running:false});x.host.querySelector('button').click();await settle();
  assert.deepEqual(x.calls.opened,['s']);assert.equal(x.host.dataset.phase,'ended');
});
test('会话切换、旧请求收尾、迟到事件不会覆盖新任务',async t=>{
  const x=setup(t),old=x.status.begin('s');x.status.connected(old);
  const latest=x.status.begin('s');x.status.connected(latest);x.status.event(latest,{data:{content:'new'}});
  await x.status.end(old,true);assert.equal(x.host.dataset.phase,'answering');
  x.w.App.state.sessionId='other';x.status.refresh();assert.equal(x.host.hidden,true);
  x.status.event(latest,{data:{reasoning_content:'background'}});assert.equal(x.host.hidden,true);
  x.w.App.state.sessionId='s';x.status.refresh();assert.equal(x.host.dataset.phase,'thinking');
});
test('渲染相同状态不重复修改 aria-live 区域',async t=>{
  const x=setup(t),h=x.status.begin('s');x.status.connected(h);
  x.status.event(h,{data:{reasoning_content:'x'}});
  const observer=new x.w.MutationObserver(()=>{});observer.observe(x.host,{childList:true,subtree:true,attributes:true});
  x.status.event(h,{data:{reasoning_content:'more'}});x.status.refresh();
  assert.equal(observer.takeRecords().length,0);observer.disconnect();
});
test('旧任务的停止回执不能覆盖同会话的新任务',t=>{
  const x=setup(t);x.status.begin('s');const stop=x.status.stopping('s');assert.equal(x.status.isStopping('s'),true);
  const next=x.status.begin('s');x.status.connected(next);x.status.event(next,{data:{content:'new'}});
  x.status.stopped('s',stop);x.status.stopFailed('s',new Error('late'),stop);
  assert.equal(x.host.dataset.phase,'answering');assert.equal(x.status.isStopping('s'),false);
});
test('挂起任务可恢复提示与 HTTP 拒绝错误可见',async t=>{
  const x=setup(t);x.status.snapshot('s',{tasks:[{state:'suspended'}]});assert.equal(x.host.dataset.phase,'suspended');
  x.status.snapshot('s',{tasks:[]});assert.equal(x.host.dataset.phase,'ended');
  const h=x.status.begin('s');await x.status.end(h,false,Object.assign(new Error('invalid request'),{status:400}));
  assert.equal(x.host.dataset.phase,'failed');assert.equal(x.status.isUncertain('s'),false);
});
test('另一个会话正在流式生成时，当前挂起任务仍可准确显示和清除',t=>{
  const x=setup(t);Object.assign(x.w.App.state,{streaming:true,streamingSession:'other'});
  x.status.snapshot('s',{tasks:[{state:'suspended'}]});assert.equal(x.host.dataset.phase,'suspended');
  x.status.snapshot('s',{tasks:[]});assert.equal(x.host.dataset.phase,'ended');
});
test('真实附接流程区分 EOF 与 DONE，断线不自动派发队列',async t=>{
  const x=setup(t),w=x.w;const state=w.App.state;
  Object.assign(w,{state,chatInner:w.document.querySelector('#chat'),AbortController,stickToBottom(){},setEmpty(){},
    el(tag,cls){const n=w.document.createElement(tag);n.className=cls||'';return n;},
    createStreamPipeline(msg,sid,active){return {handle:evt=>x.status.event(active.taskHandle,evt),finish:()=>({empty:false})};}});
  Object.assign(w.App,{refreshComposerButtons:()=>x.status.refresh(),refreshContextTokenStats(){},refreshSessionUsage:async()=>true,
    clearContextStatsTimer(){},scheduleContextTokenStatsRefresh(){},flushPendingMessages:sid=>x.calls.flushed.push(sid)});
  w.setTimeout=fn=>{fn();return 1;};
  let completed=false;const payloads=[];
  w.API.chatStream=async (body,onEvent,signal,options)=>{payloads.push(body);options.onOpen();onEvent({data:{content:'partial'}});return {completed};};
  const source=fs.readFileSync(path.join(__dirname,'../js/app/chat.js'),'utf8');
  const start=source.indexOf('  async function attachStreamSession('),end=source.indexOf('  /** 打开会话后',start);
  // 附接收尾调用同文件的流中断确认与错误文案助手，一并载入。
  const helperStart=source.indexOf('  // 流错误的展示文案');
  w.eval(source.slice(helperStart,source.indexOf('  async function send(direct)',helperStart)));
  w.eval(source.slice(start,end)+'\nwindow.attachForTest=attachStreamSession;');
  await w.attachForTest('s');assert.equal(x.host.dataset.phase,'disconnected');assert.deepEqual(x.calls.flushed,[]);
  assert.equal(state.streaming,false);assert.equal(payloads[0].messages.length,0);
  completed=true;await w.attachForTest('s');assert.equal(x.host.dataset.phase,'completed');assert.deepEqual(x.calls.flushed,['s']);
  assert.ok(payloads.every(p=>p.messages.length===0));
});
function stubTimers(x){
  const timers=[];
  x.w.setTimeout=(fn,ms)=>{timers.push({fn,ms});return timers.length;};
  x.w.clearTimeout=(id)=>{delete timers[id-1];};
  return timers;
}
async function firePending(timers){
  const idx=timers.findIndex(Boolean);
  assert.notEqual(idx,-1,'应有待触发的重试定时器');
  const entry=timers[idx];timers[idx]=null; // 消费掉，避免重复触发
  await entry.fn();await settle();await settle();
  return entry.ms;
}
test('不确定态自动退避确认：后端恢复无任务时收尾并重载',async t=>{
  const x=setup(t),timers=stubTimers(x);
  const h=x.status.begin('s');x.status.connected(h);await x.status.end(h,false,new Error('boom'));
  assert.equal(x.status.isUncertain('s'),true);
  assert.deepEqual(timers.filter(Boolean).map(o=>o.ms),[5000]);
  assert.match(x.host.querySelector('.task-status-detail').textContent,/正在自动重试/);
  x.w.API.streamStatus=async()=>({running:false});
  x.w.App.refreshComposerButtons=()=>{};
  await firePending(timers);
  assert.equal(x.status.isUncertain('s'),false);
  assert.equal(x.host.dataset.phase,'ended');
  await firePending(timers); // 重载定时器（ms 0）
  assert.deepEqual(x.calls.opened,['s']);
  assert.equal(timers.filter(Boolean).length,0);
});
test('后端任务仍在运行时停止自动确认，保持不确定态',async t=>{
  const x=setup(t),timers=stubTimers(x);
  const h=x.status.begin('s');x.status.connected(h);await x.status.end(h,false,new Error('boom'));
  assert.equal(timers.filter(Boolean).length,1);
  await firePending(timers); // 默认 mock 返回 running:true
  assert.equal(x.status.isUncertain('s'),true);
  assert.equal(timers.filter(Boolean).length,0);
  assert.doesNotMatch(x.host.querySelector('.task-status-detail').textContent,/正在自动重试/);
  assert.deepEqual(x.calls.opened,[]);
});
test('后端持续不可达时退避耗尽停查，保留手动重连入口',async t=>{
  const x=setup(t),timers=stubTimers(x);
  const h=x.status.begin('s');x.status.connected(h);await x.status.end(h,false,new Error('boom'));
  x.w.API.streamStatus=async()=>{throw new Error('down');};
  const delays=[];
  for(let i=0;i<3;i++) delays.push(await firePending(timers));
  assert.deepEqual(delays,[5000,15000,30000]);
  assert.equal(x.status.isUncertain('s'),true);
  assert.equal(timers.filter(Boolean).length,0);
  assert.equal(x.host.querySelector('button').hidden,false);
});
test('新任务开始即取消旧会话的不确定态观察',async t=>{
  const x=setup(t),timers=stubTimers(x);
  const h=x.status.begin('s');x.status.connected(h);await x.status.end(h,false,new Error('boom'));
  assert.equal(timers.filter(Boolean).length,1);
  x.status.begin('s');
  assert.equal(timers.filter(Boolean).length,0);
});
test('回到页面时补一次确认，后端恢复则自动收尾',async t=>{
  const x=setup(t),timers=stubTimers(x);
  const h=x.status.begin('s');x.status.connected(h);await x.status.end(h,false,new Error('boom'));
  x.w.API.streamStatus=async()=>{throw new Error('down');};
  for(let i=0;i<3;i++) await firePending(timers);
  assert.equal(timers.filter(Boolean).length,0);
  x.w.API.streamStatus=async()=>({running:false});
  x.w.App.refreshComposerButtons=()=>{};
  x.w.dispatchEvent(new x.w.Event('focus'));
  await settle();await settle();await settle();
  assert.equal(x.status.isUncertain('s'),false);
  assert.deepEqual(x.calls.opened,['s']);
});
