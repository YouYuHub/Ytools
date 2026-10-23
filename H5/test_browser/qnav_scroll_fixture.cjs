// Run: node H5/test_browser/qnav_scroll_fixture.cjs; all APIs are mocked.
// Three questions separated by 199 answer records reproduce a window with no question DOM.
const http=require('node:http'),fs=require('node:fs'),path=require('node:path');
const root=path.resolve(__dirname,'..');
const api=`window.API=new Proxy({BASE:location.origin,base:location.origin},{get:(o,k)=>k in o?o[k]:async()=>({})});`;
const bootstrap=`(function(){
  const panel=document.createElement('aside');panel.style='position:fixed;top:8px;left:260px;right:40px;z-index:1000;background:#203944;color:white;padding:10px;border-radius:8px;font-size:13px';
  const label=document.createElement('strong');label.textContent='问题导航滚动验证（模拟历史，不读写真实会话）';panel.append(label);
  const controls=document.createElement('div');controls.style='display:flex;gap:16px;margin:8px 0';panel.append(controls);
  const result=document.createElement('output');result.style='display:block';panel.append(result);document.body.append(panel);
  App.resetHistoryView();App.chatInner.replaceChildren();App.state.activeStream=null;
  App.state.sessionId='qnav-scroll-fixture';App.state.hasConversation=true;App.setEmpty(false);
  const records=Array.from({length:600},(_,i)=>({kind:i%200===0?'user':'assistant',round:Math.floor(i/200)+1,
    content:i%200===0?'问题 '+(Math.floor(i/200)+1):'回答段落 '+i+'：'+('用于核对滚动位置与导航高亮。'.repeat(i%3?5:14)),
    ts:'2026-10-23 14:00:00',source_event_index:i%200}));
  App.renderRecords(records,{noScroll:true});App.rebuildQnav();App.pauseAutoScroll();App.chatScroll.style.scrollBehavior='auto';
  function report(){const items=[...document.querySelectorAll('.qnav-item')],active=items.findIndex(n=>n.classList.contains('active'));
    const s=App.chatScroll,gap=s.scrollHeight-s.scrollTop-s.clientHeight;
    result.textContent='当前导航：'+(active+1)+'/'+items.length+'；已渲染提问：'+App.chatInner.querySelectorAll('.msg-user').length+'；主区：'+(gap<=24?'底部':s.scrollTop<=1?'顶部':'中段');}
  [['到开头',0],['到中间',.5],['到底部',1]].forEach(([name,ratio])=>{const b=document.createElement('button');b.textContent=name;b.style='padding:4px 12px';
    b.onclick=()=>{App.pauseAutoScroll();App.chatScroll.scrollTop=ratio*(App.chatScroll.scrollHeight-App.chatScroll.clientHeight);};controls.append(b);});
  new MutationObserver(report).observe(document.querySelector('#qnavPanel'),{subtree:true,attributes:true,childList:true});
  App.chatScroll.addEventListener('scroll',()=>requestAnimationFrame(report));App.chatScroll.scrollTop=0;report();
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
