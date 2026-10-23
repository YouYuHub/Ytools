// Run: node H5/test_browser/session_activity_fixture.cjs. No real history/model calls.
const http=require('node:http'),fs=require('node:fs'),path=require('node:path');
const root=path.resolve(__dirname,'..');
const api=`window.API=new Proxy({BASE:location.origin,base:location.origin},{get:(o,k)=>k in o?o[k]:async()=>({})});`;
const bootstrap=`(function(){
  document.documentElement.dataset.theme='dark';
  const ids=[['s','检查项目工具设计与实现'],['b','后台会话：这是一个很长的标题，用于检查指示器是否被标题遮住'],['e','正在压缩历史的会话'],['idle','已经完成的会话']];
  const active=new Set(['s','b','e']);let handle=null;
  App.state.sessionId='s';App.state.streaming=true;App.state.streamingSession='s';App.state.hasConversation=true;App.setEmpty(false);
  App.chatInner.textContent='当前会话和后台会话的运行状态显示在侧边栏标题右侧。等待确认时使用静态标记；完成后自动清除。';
  API.streamStatuses=async()=>({sessions:[...active].map(id=>({session_id:id,chat_running:id!=='e',compacting:id==='e'}))});
  function rows(){App.sessionList.querySelectorAll('.session-item').forEach(n=>n.remove());ids.forEach(([id,title])=>App.sessionList.append(App.buildSessionItem(id,title)));App.refreshSessionFades();}
  rows();
  const panel=document.createElement('aside');panel.style='position:fixed;top:120px;left:280px;right:50px;background:var(--bg-elevated);border:1px solid var(--border);border-radius:12px;padding:18px;z-index:1000';
  panel.innerHTML='<strong>侧边栏任务状态验证</strong><p>模拟任务，不读写真实会话。右侧图标与标题菜单分别占位。</p>';
  const controls=document.createElement('nav');controls.style='display:flex;flex-wrap:wrap;gap:12px';panel.append(controls);
  const result=document.createElement('output');result.style='display:block;margin-top:14px;font-size:13px';panel.append(result);document.body.append(panel);
  function begin(){active.add('s');App.state.streaming=true;App.state.streamingSession='s';handle=App.taskStatus.begin('s');App.taskStatus.connected(handle);App.taskStatus.event(handle,{data:{reasoning_content:'checking'}});}
  const actions={
    '开始任务':begin,
    '完成任务':async()=>{active.delete('s');App.state.streaming=false;await App.taskStatus.end(handle,true);await App.sessionActivity.sync();},
    '后台完成':async()=>{active.delete('b');await App.sessionActivity.sync();},
    '等待确认':()=>App.taskStatus.event(handle,{data:{event:'tool_approval',phase:'requested',approval:{id:'approval',agent_id:'main',state:'pending'}}}),
    '切换当前会话':()=>{App.state.sessionId=App.state.sessionId==='s'?'idle':'s';App.markActiveSession();App.taskStatus.refresh();},
    '重建列表':rows,
    '切换明暗':()=>{document.documentElement.dataset.theme=document.documentElement.dataset.theme==='dark'?'light':'dark';}
  };
  Object.entries(actions).forEach(([name,fn])=>{const b=document.createElement('button');b.textContent=name;b.style='padding:6px 10px;border:1px solid var(--border);border-radius:6px';b.onclick=fn;controls.append(b);});
  function report(){result.textContent=ids.map(([id])=>{const n=App.sessionList.querySelector('[data-session="'+id+'"] .session-activity');return id+': '+(n.hidden?'空闲':n.title);}).join(' · ');}
  new MutationObserver(report).observe(App.sessionList,{subtree:true,attributes:true,childList:true});
  begin();App.sessionActivity.start();report();
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
