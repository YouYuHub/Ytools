// Run: node H5/test_browser/patch_diff_fixture.cjs. Mock results; no real file/model calls.
const http=require('node:http'),fs=require('node:fs'),path=require('node:path');
const root=path.resolve(__dirname,'..');
const api=`window.API=new Proxy({BASE:location.origin,base:location.origin},{get:(o,k)=>k in o?o[k]:async()=>({})});`;
const bootstrap=`(function(){
  document.documentElement.dataset.theme='dark';App.state.sessionId='fixture';App.state.workDir='/demo';App.state.hasConversation=true;App.setEmpty(false);
  App.sessionList.append(App.buildSessionItem('fixture','补丁差异验证（模拟文件）'));
  const files=[{path:'/demo/src/main.py',display_path:'src/main.py',action:'update',status:'applied',lines_added:1,lines_removed:1,history_recorded:true},
    {path:'/demo/src/view.js',display_path:'src/view.js',action:'add',status:'applied',lines_added:2,lines_removed:0,history_recorded:false,history_reason:'history_quota_exceeded'},
    {path:'/demo/old.txt',display_path:'old.txt',action:'delete',status:'applied',lines_added:0,lines_removed:1,history_recorded:true}];
  const texts=['--- a/src/main.py\\n+++ b/src/main.py\\n@@ -1 +1 @@\\n-value = 1\\n+value = 2',
    '--- a/src/view.js\\n+++ b/src/view.js\\n@@ -0,0 +1,2 @@\\n+const ready = true;\\n+console.log(ready);',
    '--- a/old.txt\\n+++ b/old.txt\\n@@ -1 +0,0 @@\\n-old'];
  const diff={files:files.map((file,i)=>({...file,diff:texts[i]})),file_count:3,lines_added:3,lines_removed:2,stats_complete:true};
  const result=JSON.stringify({success:true,message:'补丁已修改 3 个文件，+3 -2 行',files});
  const args={patch:'*** Begin Patch\\n*** Update File: src/main.py\\n@@\\n-value = 1\\n+value = 2\\n*** End Patch'};
  function show(mode){
    App.historyScroll.reset();App.chatInner.replaceChildren();
    const records=[{kind:'user',content:'请核对多文件补丁的修改内容与版本记录',round:1},{kind:'assistant',content:'补丁结果可按文件展开。以下全部是模拟数据，不修改真实项目文件。',round:1}];
    App.renderRecords(records);const msg=App.el('div','msg');App.chatInner.append(msg);
    if(mode==='history'){
      const parsed=HistoryParser.parseHistory(JSON.stringify({event:'chat_round',question:'回放多文件补丁',started_at:'2026-10-23 17:02:00',events:[{role:'tool',tool_name:'apply_patch',tool_call_id:'p',arguments:JSON.stringify(args),result,file_diff:diff}]}));
      App.renderRecords(parsed.records);
    } else if(mode==='child'){
      const child=App.buildSubAgentBlock('fixture');msg.append(child.wrap);
      child.applyEvent({phase:'start',agent_id:'demo',task:'批量修改文件'});child.applyEvent({phase:'tool_result',seq:1,tool_call_id:'p',tool_name:'apply_patch',arguments:JSON.stringify(args),result,file_diff:diff});
      child.applyEvent({phase:'done',status:'done',final_reply:'完成'});child.wrap.classList.add('open');
    } else {
      const failed={success:false,error:'PATCH_COMMIT_FAILED：模拟磁盘写入失败',message:'提交失败，部分文件状态需核对',needs_attention:true,rollback_complete:false,
        files:[{path:'/demo/src/main.py',status:'rollback_failed',needs_attention:true,history_recorded:false},{path:'/demo/src/view.js',status:'commit_failed',needs_attention:false},{path:'/demo/old.txt',status:'not_applied'}]};
      const pipe=App.createStreamPipeline(msg,'fixture',{});pipe.handle({data:{tool_return:{function_name:'apply_patch',tool_call_id:'p',arguments:args,result:mode==='failure'?JSON.stringify(failed):result,file_diff:mode==='failure'?null:diff}}});pipe.finish();
    }
    App.chatInner.querySelectorAll('.tool-block').forEach(node=>node.classList.add('open'));App.scrollToBottom();
  }
  const panel=document.createElement('aside');panel.style='position:fixed;top:62px;left:270px;right:40px;background:var(--bg-elevated);border:1px solid var(--border);border-radius:12px;padding:12px;z-index:1000';
  panel.innerHTML='<strong>多文件补丁展示验证</strong><p>模拟结果；使用实际 SSE 渲染、历史解析与子任务卡片。点击各文件查看代码差异。</p>';
  const controls=document.createElement('nav');controls.style='display:flex;gap:10px;flex-wrap:wrap';panel.append(controls);document.body.append(panel);
  const actions={'实时补丁结果':()=>show('live'),'历史回放':()=>show('history'),'子任务输出':()=>show('child'),'模拟回滚不完整':()=>show('failure'),'切换明暗':()=>{document.documentElement.dataset.theme=document.documentElement.dataset.theme==='dark'?'light':'dark';}};
  Object.entries(actions).forEach(([name,action])=>{const button=document.createElement('button');button.textContent=name;button.style='border:1px solid var(--border);padding:5px 9px;border-radius:6px';button.onclick=action;controls.append(button);});
  show('live');
})();`;
http.createServer((req,res)=>{
  const name=decodeURIComponent(new URL(req.url,'http://localhost').pathname).replace(/^\//,'')||'index.html';
  if(name==='fixture-api.js'||name==='fixture-start.js'){res.writeHead(200,{'Content-Type':'text/javascript'});res.end(name==='fixture-api.js'?api:bootstrap);return;}
  const file=path.resolve(root,name);
  if(!file.startsWith(root+path.sep)||!fs.existsSync(file)||!fs.statSync(file).isFile()){res.writeHead(404);res.end();return;}
  let body=fs.readFileSync(file);
  if(name==='index.html')body=body.toString().replace(/src="js\/api.js[^\"]*"/,'src="fixture-api.js"').replace(/src="js\/app.js[^\"]*"/,'src="fixture-start.js"');
  res.writeHead(200,{'Content-Type':name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':name.endsWith('.html')?'text/html':'application/octet-stream','Cache-Control':'no-store'});res.end(body);
}).listen(0,'127.0.0.1',function(){console.log('http://127.0.0.1:'+this.address().port+'/');});
