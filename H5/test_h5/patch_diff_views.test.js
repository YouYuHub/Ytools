const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),{JSDOM}=require('jsdom');
const messages=fs.readFileSync(path.join(__dirname,'../js/app/messages.js'),'utf8');
function setup(t){
  const dom=new JSDOM('<div id="chat"></div>',{runScripts:'outside-only',url:'http://test'});t.after(()=>dom.window.close());
  const w=dom.window,root=w.document.querySelector('#chat');
  const el=(tag,cls,text)=>{const n=w.document.createElement(tag);n.className=cls||'';n.textContent=text||'';return n;};
  const App=w.App={state:{sessionId:'s',workDir:'/project'},chatInner:root,el,toast(){},stickToBottom(){},rebuildQnav(){},
    isBuiltinToolName:()=>true,toolDisplayName:()=> '应用补丁',toolIconSvg:(name,cls)=>'<svg class="'+cls+'"></svg>',
    clearInjectedPending(){},scheduleContextTokenStatsRefresh(){},highlightCodeBlocks(){},updateCodeblockCopyButtons(){}};
  Object.assign(w,{el,state:App.state,toast(){},setStageTimestamp(){},loadOlderState:{records:[]},
    FormatUtils:{prettyJson:JSON.stringify,fmtNum:String,fmtTime:String,roundSummaryText:()=>''},
    Markdown:{render:()=>''},renderPreservingWidgets(){},
    buildThinkBlock(){return {wrap:el('div','think-block'),textContent:()=>'',done(){},streaming(){},setText(){},add(){}};}});
  w.eval(fs.readFileSync(path.join(__dirname,'../js/app/message_diff.js'),'utf8'));
  const end=messages.indexOf('  // 代码块复制（事件委托）',messages.indexOf('  function buildSubAgentBlock('));
  w.eval(messages.slice(messages.indexOf('  function buildToolBlock('),end)+'\nObject.assign(App,{buildToolBlock,applyToolResult,buildDiffView,buildSubAgentBlock});');
  const start=messages.indexOf('  function renderOneRecordInto(');
  w.eval(messages.slice(start,messages.indexOf('  function renderRecords(',start)));
  w.eval(fs.readFileSync(path.join(__dirname,'../js/app/stream_pipeline.js'),'utf8'));
  const files=[{path:'/project/a.py',action:'update',status:'applied',lines_added:1,lines_removed:1,history_recorded:true},
    {path:'/project/b.js',action:'add',status:'applied',lines_added:1,lines_removed:0,history_recorded:false,history_reason:'record_failed'}];
  const diff={files:files.map((f,i)=>({...f,diff:i?'--- a/b.js\n+++ b/b.js\n@@ -0,0 +1 @@\n+const n=2;':'--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n--- old\n+++ new'})),file_count:2,lines_added:2,lines_removed:1,stats_complete:true};
  const result=JSON.stringify({success:true,message:'补丁已修改 2 个文件，+2 -1 行',files});
  function render(){const ui=App.buildToolBlock('apply_patch');root.append(ui.wrap);App.applyToolResult(ui,'apply_patch',result,diff,{patch:'...'});ui.finish();return ui;}
  return {w,App,root,files,diff,result,render};
}

test('补丁标题显示文件数量和统计，逐文件差异展开时才渲染且重复结果不重复插入',t=>{
  const h=setup(t),ui=h.render();
  assert.equal(ui.wrap.querySelector('.tool-block-file').textContent,'2 个文件');
  assert.match(ui.wrap.querySelector('.tool-block-file').title,/\/project\/a.py/);
  assert.equal(ui.wrap.querySelector('.tool-block-diff').textContent,'+2-1');
  assert.equal(ui.wrap.querySelectorAll('.diff-file').length,2);assert.equal(ui.wrap.querySelectorAll('.diff-code').length,0);
  const file=ui.wrap.querySelector('.diff-file');file.open=true;file.dispatchEvent(new h.w.Event('toggle'));
  assert.equal(file.querySelectorAll('.diff-code .is-add').length,1);assert.equal(file.querySelectorAll('.diff-code .is-del').length,1);
  assert.equal(file.querySelectorAll('.diff-code .is-file').length,2);
  assert.match(ui.wrap.querySelector('.tool-io-out').textContent,/已记录回退版本/);
  assert.match(ui.wrap.querySelector('.tool-io-out').textContent,/未记录回退版本/);
  h.App.applyToolResult(ui,'apply_patch',h.result,h.diff,{patch:'...'});assert.equal(ui.wrap.querySelectorAll('.diff-batch').length,1);
});

test('实时 SSE 管线和历史 JSONL 工具结果都保留多文件 diff',t=>{
  const h=setup(t),msg=h.w.document.createElement('div');h.root.append(msg);
  const pipe=h.App.createStreamPipeline(msg,'s',{});
  pipe.handle({data:{tool_return:{function_name:'apply_patch',tool_call_id:'p',result:h.result,file_diff:h.diff,arguments:{patch:'...'}}}});
  assert.equal(msg.querySelectorAll('.diff-file').length,2);
  const parser=require('../js/history_parser');
  const round={event:'chat_round',question:'patch',started_at:'2026-10-23 17:00:00',events:[
    {role:'assistant',tool_calls:[{id:'p',function:{name:'apply_patch',arguments:'{"patch":"..."}'}}]},
    {role:'tool',tool_name:'apply_patch',tool_call_id:'p',result:h.result,file_diff:h.diff}]};
  const parsed=parser.parseHistory(JSON.stringify(round)),tools={},compactions={};h.root.replaceChildren();
  for(const rec of parsed.records)if(['tool','toolResult'].includes(rec.kind))h.w.renderOneRecordInto(h.root,rec,tools,compactions);
  assert.equal(h.root.querySelectorAll('.diff-file').length,0,'closed history tools defer diff DOM');
  h.root.querySelector('.tool-block-head').click();
  assert.equal(h.root.querySelectorAll('.diff-file').length,2);
  assert.equal(h.root.querySelectorAll('.tool-block').length,1);
});

test('延迟历史差异保留标题统计，首次展开只渲染最新结果，清除后不恢复旧差异',t=>{
  const h=setup(t),ui=h.App.buildToolBlock('apply_patch',null,{deferDetails:true});h.root.append(ui.wrap);
  h.App.applyToolResult(ui,'apply_patch',h.result,h.diff,{patch:'...'});ui.finish();
  assert.equal(ui.wrap.querySelector('.tool-block-diff').textContent,'+2-1');
  assert.equal(ui.wrap.querySelectorAll('.diff-batch').length,0);
  ui.wrap.querySelector('.tool-block-head').click();
  assert.equal(ui.wrap.querySelectorAll('.diff-batch').length,1);
  ui.clearDiff();ui.wrap.querySelector('.tool-block-head').click();ui.wrap.querySelector('.tool-block-head').click();
  assert.equal(ui.wrap.querySelectorAll('.diff-batch').length,0);
});

test('子任务实时及历史聚合事件都回填多文件 diff',t=>{
  const h=setup(t),events=[{event:'sub_agent',agent_id:'child',phase:'start',task:'修改文件'},
    {event:'sub_agent',agent_id:'child',phase:'tool_result',seq:1,tool_name:'apply_patch',tool_call_id:'p',result:h.result,file_diff:h.diff,arguments:'{"patch":"..."}'}];
  const ui=h.App.buildSubAgentBlock('s');h.root.append(ui.wrap);events.forEach(e=>ui.applyEvent(e));
  assert.equal(ui.wrap.querySelectorAll('.diff-file').length,2);
  const parsed=require('../js/history_parser').parseHistory(JSON.stringify({event:'chat_round',question:'patch',events}));
  const entry=parsed.records.find(r=>r.kind==='agentBlock').entries.find(e=>e.phase==='tool_result');
  assert.deepEqual(entry.file_diff,h.diff);
});

test('回滚失败强调需核对，失败结果不展示成功差异且标题仍可定位文件',t=>{
  const h=setup(t),ui=h.render();
  const failed={success:false,error:'PATCH_COMMIT_FAILED',needs_attention:true,files:[
    {path:'/project/a.py',status:'rollback_failed',needs_attention:true},{path:'/project/b.js',status:'not_applied'}]};
  h.App.applyToolResult(ui,'apply_patch',JSON.stringify(failed),null,{});
  assert.match(ui.wrap.querySelector('.tool-io-out').textContent,/需核对磁盘状态/);
  assert.match(ui.wrap.querySelector('.tool-io-out').textContent,/未执行/);
  assert.equal(ui.wrap.querySelector('.tool-block-file').textContent,'2 个文件');
  assert.equal(ui.wrap.querySelector('.tool-block-tool-icon').classList.contains('is-failed'),true);
  assert.equal(ui.wrap.querySelectorAll('.diff-file').length,0);
});

test('文件路径和源码中的 HTML 保持纯文本；旧单文件结果仍正常显示',t=>{
  const h=setup(t),ui=h.App.buildToolBlock('apply_patch'),file={...h.files[0],path:'<img src=x onerror=alert(1)>'};
  const diff={files:[{...file,diff:'--- a/x\n+++ b/x\n@@ -0,0 +1 @@\n+<script>alert(1)</script>'}],lines_added:1,lines_removed:0};
  h.App.applyToolResult(ui,'apply_patch',JSON.stringify({success:true,files:[file]}),diff,{});
  assert.equal(ui.wrap.querySelectorAll('img,script').length,0);assert.match(ui.wrap.textContent,/<script>/);
  const old=h.App.buildToolBlock('edit_file');h.App.applyToolResult(old,'edit_file',JSON.stringify({path:'a.py',success:true}),h.diff.files[0],{});
  assert.equal(old.wrap.querySelectorAll('.diff-view').length,1);assert.equal(old.wrap.querySelectorAll('.diff-file').length,0);
});

test('全部大文件跳过比较时不显示误导性的 +0/-0，旧损坏结果回退原文',t=>{
  const h=setup(t),ui=h.App.buildToolBlock('apply_patch');
  const file={...h.files[0],lines_added:null,lines_removed:null,diff:'',diff_skipped:'file_too_large'};
  const result=JSON.stringify({success:true,message:'增删行数不可用',files:[file]});
  h.App.applyToolResult(ui,'apply_patch',result,{files:[file],stats_complete:false,lines_added:0,lines_removed:0},{});
  assert.equal(ui.wrap.querySelector('.tool-block-diff').textContent,'统计不可用');
  assert.match(ui.wrap.querySelector('.diff-file-head').textContent,/统计不可用/);
  assert.doesNotMatch(ui.wrap.querySelector('.tool-io-out').textContent,/\+0/);
  const invalid=JSON.stringify({files:[null]});h.App.applyToolResult(ui,'apply_patch',invalid,null,{});
  assert.equal(ui.wrap.querySelector('.tool-io-out').textContent,invalid);
});
