// Run: node H5/test_browser/compaction_lifecycle_fixture.cjs. Isolated API; no model/history writes.
const http=require('node:http'),fs=require('node:fs'),path=require('node:path');
const root=path.resolve(__dirname,'..');
const api=`window.API=new Proxy({BASE:location.origin,base:location.origin},{get:(o,k)=>k in o?o[k]:async()=>({})});`;
const bootstrap=`(function(){
  document.documentElement.dataset.theme='dark';
  App.state.sessionId='s';App.state.hasConversation=true;App.setEmpty(false);
  App.sessionList.append(App.buildSessionItem('s','压缩状态回归验证'),App.buildSessionItem('idle','其他已完成的会话'));
  const records=Array.from({length:240},(_,i)=>({kind:i%2?'assistant':'user',round:Math.floor(i/2)+1,content:i===239?'已完成项目检查。下方验证压缩结束后的显示。':'历史消息 '+(i+1)}));
  App.renderRecords(records);
  let active=false,emit=null,release=null;
  const start={event:'context_compaction',scope:'session',phase:'start',context_summary:'模拟待压缩内容；不读取真实历史。'};
  API.manualCompactionStatus=async()=>({running:active});
  API.streamStatuses=async()=>({sessions:active?[{session_id:'s',chat_running:false,compacting:true}]:[]});
  API.compactContextStream=async(sid,handler)=>{
    emit=data=>handler({type:'data',data});emit(start);
    // 重现截图：实时块仍连接，另有历史/重连遗留的运行副本。
    App.chatInner.append(App.buildCompactionBlock(start).wrap);
    App.processGroups.sync(App.chatInner);App.historyScroll.refresh();App.scrollToBottom();
    await new Promise(resolve=>{release=resolve;});return {completed:true};
  };
  const panel=document.createElement('aside');panel.style='position:fixed;top:68px;left:280px;right:40px;background:var(--bg-elevated);border:1px solid var(--border);border-radius:12px;padding:14px;z-index:1000';
  panel.innerHTML='<strong>压缩生命周期验证</strong><p>模拟事件，使用实际页面、压缩卡片、过程分组、侧栏状态与历史窗口模块。</p>';
  const controls=document.createElement('nav');controls.style='display:flex;flex-wrap:wrap;gap:10px';panel.append(controls);
  const output=document.createElement('output');output.id='fixture-result';output.style='display:block;margin-top:12px;font-size:13px';panel.append(output);document.body.append(panel);
  const actions={
    '开始并注入副本':()=>{if(active)return;active=true;App.attachManualCompaction('s');},
    '重复开始事件':()=>{if(emit&&active){emit(start);emit({...start,phase:'delta',content:'摘要正文生成中…'});}},
    '压缩完成':async()=>{if(!active)return;active=false;emit({...start,phase:'done',summary_text:'累计摘要已保存。完成后没有遗留的运行卡片，过程组和侧边栏同时停止转圈。',summary_usage:{compressed_rounds:3,before_tokens:10000,after_tokens:500}});emit({event:'compaction_manual_result',compressed_rounds:3});release();await Promise.resolve();App.processGroups.sync(App.chatInner);App.scrollToBottom();},
    '浏览历史再回底部':()=>{App.renderHistoryWindowAt(10);App.historyScroll.refresh();App.showHistoryTail();App.scrollToBottom();},
    '切换明暗':()=>{document.documentElement.dataset.theme=document.documentElement.dataset.theme==='dark'?'light':'dark';}
  };
  Object.entries(actions).forEach(([name,fn])=>{const button=document.createElement('button');button.textContent=name;button.style='padding:6px 10px;border:1px solid var(--border);border-radius:6px';button.onclick=fn;controls.append(button);});
  function report(){const text='运行卡 '+App.chatInner.querySelectorAll('.compaction-bar.is-running').length+' · 完成卡 '+App.chatInner.querySelectorAll('.compaction-bar.is-done').length+' · 过程组转圈 '+App.chatInner.querySelectorAll('.process-group-summary.is-running').length+' · 侧栏转圈 '+document.querySelectorAll('.session-activity:not([hidden])[data-kind="running"]').length;
    if(output.textContent!==text)output.textContent=text;}
  new MutationObserver(report).observe(document.body,{subtree:true,childList:true,attributes:true});
  App.sessionActivity.start();report();App.scrollToBottom();
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
