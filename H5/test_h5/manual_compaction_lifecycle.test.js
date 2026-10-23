const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),{JSDOM}=require('jsdom');
const source=name=>fs.readFileSync(path.join(__dirname,'../js/app',name+'.js'),'utf8');
function setup(t){
  const dom=new JSDOM('<div id="chat"></div><div id="taskStatus"><span class="task-status-label"></span><span class="task-status-detail"></span><button></button></div>',{runScripts:'outside-only',pretendToBeVisual:true,url:'http://test'});
  const w=dom.window,d=w.document,root=d.querySelector('#chat');t.after(()=>dom.window.close());
  w.requestAnimationFrame=()=>1;w.cancelAnimationFrame=()=>{};w.setTimeout=()=>1;w.clearTimeout=()=>{};
  w.FormatUtils={fmtNum:String,fmtTime:String,compactionUsageText:()=>''};w.TaskState=require('../js/task_state');
  const state={sessionId:'s',selectedTools:new Set()},records=[],notices=[],discarded=[];
  const App=w.App={state,chatInner:root,el(tag,cls,text){const n=d.createElement(tag);n.className=cls||'';n.textContent=text||'';return n;},
    toast:m=>notices.push(m),setEmpty(){},scrollToBottom(){},stickToBottom(){},refreshComposerButtons(){},
    refreshSessionUsage(){},scheduleContextTokenStatsRefresh(){},flushPendingMessages(){},getHistoryWindow:()=>({records}),
    historyScroll:{discardNodes(nodes){nodes.forEach(n=>{discarded.push(n);n.remove();});}}};
  Object.assign(w,{el:App.el,state,loadOlderState:{records},readCompactionChoice:()=>null,rememberCompactionChoice(){}});
  const messages=source('messages'),start=messages.indexOf('  function buildCompactionBlock('),end=messages.indexOf('  // ---------- 消息渲染',start);
  w.eval(messages.slice(start,end)+'\nApp.buildCompactionBlock=buildCompactionBlock;');
  w.eval(source('process_groups'));w.eval(source('task_status'));
  w.eval(source('compaction').replace('  // ---------- 导出','  App.consumeForTest = consumeCompactionStream;\n  // ---------- 导出'));
  w.API={manualCompactionStatus:async()=>({running:false})};
  const frame=data=>({type:'data',data});
  const startEvent={event:'context_compaction',scope:'session',phase:'start',context_summary:'source'};
  const doneEvent={event:'context_compaction',scope:'session',phase:'done',summary_text:'saved summary',summary_usage:{}};
  function block(data){const ui=App.buildCompactionBlock(data);root.append(ui.wrap);return ui;}
  return {w,App,state,root,records,notices,discarded,frame,startEvent,doneEvent,block,
    async run(emit,attach=true){w.API.compactContextStream=async(sid,handler,signal)=>{await emit(data=>handler(frame(data)),signal);return {completed:true};};
      const value=await App.consumeForTest('s',attach);App.processGroups.sync(root);return value;}};
}
test('截图回归：当前卡片仍连接时，完成事件也清除遗留运行副本及过程转圈',async t=>{
  const h=setup(t);let live;
  await h.run(emit=>{emit(h.startEvent);live=h.root.querySelector('.compaction-bar');h.block(h.startEvent);
    emit(h.doneEvent);emit({event:'compaction_manual_result',compressed_rounds:3});});
  assert.equal(h.root.querySelectorAll('.compaction-bar.is-running').length,0);
  assert.equal(h.root.querySelectorAll('.compaction-bar').length,1);assert.equal(live.classList.contains('is-done'),true);
  assert.equal(h.root.querySelectorAll('.process-group-summary.is-running').length,0);
});
test('重复 start 复用同一块，两个真正批次各保留一份完成结果',async t=>{
  const h=setup(t);await h.run(emit=>{
    emit(h.startEvent);emit({...h.startEvent,phase:'delta',content:'partial'});emit(h.startEvent);emit(h.doneEvent);
    emit({...h.startEvent,context_summary:'next'});emit({...h.doneEvent,summary_text:'next saved'});
    emit({event:'compaction_manual_result',compressed_rounds:4});});
  assert.equal(h.root.querySelectorAll('.compaction-bar').length,2);
  assert.equal(h.root.querySelectorAll('.compaction-bar.is-running').length,0);
});
test('附接可收养历史运行卡，终态同步历史窗口缓存，刷新窗口不退回运行态',async t=>{
  const h=setup(t);h.records.push({kind:'compaction',...h.startEvent});
  const history=h.block(h.startEvent);history.wrap.dataset.historyIndex='0';
  await h.run(emit=>{emit(h.doneEvent);emit({event:'compaction_manual_result',compressed_rounds:2});});
  assert.equal(h.root.querySelectorAll('.compaction-bar').length,1);
  assert.equal(history.wrap.classList.contains('is-done'),true);assert.equal(h.records[0].phase,'done');
  assert.equal(h.records[0].compress_context,'source');
});
test('附接 404 清除任务状态，不能永久显示正在压缩',async t=>{
  const h=setup(t);h.w.API.compactContextStream=async()=>{throw Object.assign(new Error('missing'),{status:404});};
  assert.equal(await h.App.attachManualCompaction('s'),false);
  assert.equal(h.w.document.querySelector('#taskStatus').dataset.phase,'ended');
});

test('切走又切回：保留实时增量，移除历史副本并同步其内存终态',async t=>{
  const h=setup(t);let live;
  await h.run(emit=>{
    emit(h.startEvent);live=h.root.querySelector('.compaction-bar');
    emit({...h.startEvent,phase:'delta',content:'partial'});
    live.remove();h.records.push({kind:'compaction',...h.startEvent});
    const alias=h.block(h.startEvent);alias.wrap.dataset.historyIndex='0';
    emit({...h.startEvent,phase:'delta',content:' output'});
    assert.equal(live.textContent.includes('partial output'),true);
    emit(h.doneEvent);emit({event:'compaction_manual_result',compressed_rounds:1});
  });
  assert.equal(h.root.querySelectorAll('.compaction-bar').length,1);
  assert.equal(h.root.querySelector('.compaction-bar'),live);assert.equal(h.records[0].phase,'done');
});

test('已有历史 done 配对保持原样；独立合并摘要不占用当前批次',async t=>{
  const h=setup(t);h.records.push({kind:'compaction',...h.startEvent},{kind:'compaction',...h.doneEvent});
  const old=h.block(h.startEvent);old.wrap.dataset.historyIndex='0';
  await h.run(emit=>{emit(h.startEvent);
    emit({...h.doneEvent,summary_text:'merged',summary_usage:{merge_block_count:2}});
    assert.equal(h.root.querySelectorAll('.compaction-bar.is-running').length,1);
    emit(h.doneEvent);emit({event:'compaction_manual_result',compressed_rounds:1});});
  assert.equal(h.root.querySelectorAll('.compaction-bar').length,2);assert.equal(h.records[0].phase,'start');
  assert.equal(h.records[1].summary_text,'saved summary');
});

test('其他会话和本轮工具压缩卡不被手动压缩收尾误改',async t=>{
  const h=setup(t);let other,round;
  await h.run(emit=>{emit(h.startEvent);h.state.sessionId='other';h.root.replaceChildren();
    other=h.block(h.startEvent);round=h.block({...h.startEvent,scope:'round'});emit(h.doneEvent);
    emit({event:'compaction_manual_result',compressed_rounds:1});});
  assert.equal(other.wrap.classList.contains('is-running'),true);assert.equal(round.wrap.classList.contains('is-running'),true);
  h.state.sessionId='s';h.root.replaceChildren();round=h.block({...h.startEvent,scope:'round'});
  await h.run(emit=>{emit(h.startEvent);emit(h.doneEvent);emit({event:'compaction_manual_result',compressed_rounds:1});});
  assert.equal(round.wrap.classList.contains('is-running'),true);
});

test('终态忽略迟到 start/delta，失败卡不追加生成中内容',t=>{
  const h=setup(t),ui=h.block(h.startEvent);ui.appendDelta({content:'partial'});
  ui.update({phase:'aborted',error:'cancelled'});const text=ui.wrap.textContent;
  ui.appendDelta({content:'late'});ui.update(h.startEvent);
  assert.equal(ui.wrap.textContent,text);assert.equal(ui.wrap.classList.contains('is-running'),false);
  ui.update(h.doneEvent);ui.update(h.startEvent);ui.appendDelta({content:'late'});
  assert.equal(ui.wrap.dataset.compactionPhase,'done');assert.equal(ui.wrap.textContent.includes('late'),false);
});

const tick=()=>new Promise(resolve=>setImmediate(resolve));
for(const mode of ['409','network','EOF'])test(mode+' 重连接回之后，旧请求的 finally 不会清掉新请求的运行标志',async t=>{
  const h=setup(t);let calls=0,release;const flags=[];
  h.w.API.manualCompactionStatus=async()=>({running:true});
  h.w.API.compactContextStream=async(sid,handler,signal,attach)=>{
    calls++;flags.push(attach);
    if(calls===1){handler(h.frame(h.startEvent));if(mode==='EOF')return {completed:false};
      throw Object.assign(new Error(mode==='network'?'network error':'conflict'),mode==='409'?{status:409}:{});}
    await new Promise(resolve=>{release=resolve;});assert.equal(signal.aborted,false);
    handler(h.frame(h.doneEvent));handler(h.frame({event:'compaction_manual_result',compressed_rounds:1}));return {completed:true};
  };
  const pending=h.App.consumeForTest('s',mode!=='409');await tick();
  assert.equal(calls,2);assert.equal(h.state.manualCompactRunning,true);
  assert.equal(h.state.manualCompactSession,'s');assert.ok(h.state.manualCompactAbort);
  assert.deepEqual(flags,[mode!=='409',true]);release();assert.equal(await pending,true);
  assert.equal(h.state.manualCompactRunning,false);assert.equal(h.state.manualCompactAbort,null);
  assert.equal(h.root.querySelectorAll('.compaction-bar.is-running').length,0);
});

test('连续断线只自动重连一次，并显示连接中断而不是压缩成功',async t=>{
  const h=setup(t);let calls=0;h.w.API.manualCompactionStatus=async()=>({running:true});
  h.w.API.compactContextStream=async(sid,handler)=>{calls++;handler(h.frame(h.startEvent));return {completed:false};};
  assert.equal(await h.App.attachManualCompaction('s'),false);assert.equal(calls,2);
  assert.equal(h.w.document.querySelector('#taskStatus').dataset.phase,'disconnected');
  assert.equal(h.root.querySelectorAll('.compaction-bar.is-running').length,0);
  assert.equal(h.root.querySelector('.compaction-bar').classList.contains('is-done'),false);
});

test('业务结果已到但缺 DONE：保留成功结果，不因尾部 EOF 误报或重连',async t=>{
  const h=setup(t);h.w.API.compactContextStream=async(sid,handler)=>{
    [h.startEvent,h.doneEvent,{event:'compaction_manual_result',compressed_rounds:1},h.startEvent].forEach(data=>handler(h.frame(data)));
    return {completed:false};};
  assert.equal(await h.App.attachManualCompaction('s'),true);
  assert.equal(h.root.querySelectorAll('.compaction-bar').length,1);
  assert.equal(h.root.querySelectorAll('.compaction-bar.is-running').length,0);
});

test('最终业务结果之后读取器报错，不覆盖已经确认的压缩结果',async t=>{
  const h=setup(t);h.w.API.compactContextStream=async(sid,handler)=>{
    [h.startEvent,h.doneEvent,{event:'compaction_manual_result',compressed_rounds:1}].forEach(data=>handler(h.frame(data)));
    throw new Error('network error after final result');};
  assert.equal(await h.App.attachManualCompaction('s'),true);
  assert.equal(h.w.document.querySelector('#taskStatus').dataset.phase,'completed');
});

test('缺少阶段终态时结束运行显示，保留部分输出且不冒充成功摘要',async t=>{
  const h=setup(t);await h.run(emit=>{emit(h.startEvent);emit({...h.startEvent,phase:'delta',content:'partial'});
    emit({event:'compaction_manual_result',compressed_rounds:0});});
  const bar=h.root.querySelector('.compaction-bar');assert.equal(bar.classList.contains('is-running'),false);
  assert.equal(bar.classList.contains('is-done'),false);assert.equal(bar.textContent.includes('partial'),true);
});

test('显式停止失败保留任务；停止旧任务的迟到回执不能取消新任务',async t=>{
  const h=setup(t),old=new h.w.AbortController();Object.assign(h.state,{manualCompactRunning:true,manualCompactSession:'s',manualCompactAbort:old});
  h.w.API.stopManualCompaction=async()=>{throw new Error('offline');};
  assert.equal(await h.App.stopManualCompaction(),false);assert.equal(old.signal.aborted,false);
  let release;h.w.API.stopManualCompaction=()=>new Promise(resolve=>{release=resolve;});
  const pending=h.App.stopManualCompaction(),next=new h.w.AbortController();h.state.manualCompactAbort=next;
  release();assert.equal(await pending,true);assert.equal(next.signal.aborted,false);
});
