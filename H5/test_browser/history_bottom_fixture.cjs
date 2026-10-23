// Real UI/history/Mermaid, deterministic synthetic history; no backend/model calls.
const http = require('node:http'), fs = require('node:fs'), path = require('node:path');
const root = path.resolve(__dirname, '..');
// Optional read-only history shape. Keep only the final answer; replace tool
// arguments/results and earlier messages with synthetic text before serving.
let recorded = [];
if (process.argv[2]) {
  const records = require('../js/history_parser').parseHistory(fs.readFileSync(process.argv[2], 'utf8')).records;
  const last = records.findLastIndex(record => record.kind === 'assistant');
  recorded = records.map((record, index) => ({kind:record.kind, round:record.round,
    name:record.name, source_event_index:record.source_event_index, tool_call_id:record.tool_call_id,
    content:index === last ? record.content : record.kind === 'user' ? '模拟提问' : '模拟过程',
    args:{}, result:'模拟工具回执', usage:record.usage}));
}
const api = `window.API=new Proxy({BASE:location.origin,base:location.origin},{get:(o,k)=>k in o?o[k]:async()=>({})});`;
const bootstrap = String.raw`(function(){
  App.state.sessionId='bottom-fixture';App.state.hasConversation=true;App.setEmpty(false);
  document.documentElement.dataset.theme='dark';
  const panel=document.createElement('aside');panel.style='position:fixed;top:8px;left:260px;right:38px;z-index:1000;background:#203944;color:white;padding:9px;border-radius:8px;font-size:13px';
  panel.innerHTML='<strong>高速滚动到底部验证（模拟历史）</strong>';
  const controls=document.createElement('nav');controls.style='display:flex;gap:12px;margin:6px 0';panel.append(controls);
  const output=document.createElement('output');output.id='fixture-result';panel.append(output);document.body.append(panel);
  let renders=0,run=0;
  const render=App.renderHistoryWindowAt;App.renderHistoryWindowAt=function(){renders++;return render.apply(this,arguments);};
  const graph='flowchart TD\nA[代码与扩展包] --> B[单元与集成检查通过]\nB --> C[技术层面可发布]\nC --> D{上架材料}\nD --> E[隐私政策 URL 缺失]\nD --> F[版本号不一致]\nD --> G[署名待完善]\nD --> H[补齐以上三项]\nE --> I[不可提审]\nF --> I\nG --> I\nH --> J[可提交 Partner Center]\nJ --> K[手工点一次工具栏做最终验证]';
  function load(mode){run++;renders=0;App.historyScroll.reset();App.chatInner.replaceChildren();App.state.activeStream=null;
    const count=mode==='single'?1:600;
    const records=mode==='recorded'?${JSON.stringify(recorded)}:Array.from({length:count},(_,i)=>({kind:i%6===0?'user':'assistant',round:Math.floor(i/6)+1,source_event_index:i%6,
      content:i%6===0?'问题 '+i:'历史回答 '+i+'：'+('用于核对窗口切换后的阅读位置。'.repeat(i%3?3:15))}));
    if(mode!=='recorded')records[count-1]={kind:'assistant',round:100,content:'## 五、判定\n\n'+(mode==='text'?'长正文。'.repeat(1800):'\u0060\u0060\u0060mermaid\n'+graph+'\n\u0060\u0060\u0060')+'\n\n**一句话：包没问题，补齐上架材料后再提审。**'};
    App.renderRecords(records,{noScroll:true});App.pauseAutoScroll();App.chatScroll.style.scrollBehavior='auto';
    App.chatScroll.scrollTop=Math.max(0,App.chatScroll.scrollHeight-App.chatScroll.clientHeight-450);report('就绪');
  }
  function report(label,backs=0){const s=App.chatScroll,v=App.getHistoryWindow();
    output.textContent=label+'；重建 '+renders+' 次；回跳 '+backs+' 次；离底 '+Math.round(s.scrollHeight-s.clientHeight-s.scrollTop)+'px；窗口 '+v.start+'–'+v.end;}
  function sample(scroll){const ticket=++run,start=renders;let i=0,last=App.chatScroll.scrollTop,backs=0;
    function frame(){if(ticket!==run)return;const s=App.chatScroll;if(scroll&&i<45)s.scrollBy(0,Math.max(3,Math.min(680,(s.scrollHeight-s.clientHeight-s.scrollTop)*0.65)));
      const top=s.scrollTop;if(top<last-8)backs++;last=top;i++;
      if(i<110){report('采样中',backs);requestAnimationFrame(frame);}else report('采样完成，新增重建 '+(renders-start)+' 次',backs);}
    requestAnimationFrame(frame);}
  [['录屏会话结构',()=>load('recorded')],['长图表末尾',()=>load('graph')],['纯文本末尾',()=>load('text')],['单条长回复',()=>load('single')],
    ['连续快速下滚',()=>sample(true)],['距底 12px',()=>{App.pauseAutoScroll();App.chatScroll.scrollTop=App.chatScroll.scrollHeight-App.chatScroll.clientHeight-12;sample(false);}],
    ['只观察稳定性',()=>sample(false)]].forEach(([label,action])=>{const b=document.createElement('button');b.textContent=label;b.onclick=action;controls.append(b);});
  load('graph');
})();`;
http.createServer((req,res)=>{
  const name=decodeURIComponent(new URL(req.url,'http://localhost').pathname).replace(/^\//,'')||'index.html';
  if(name==='fixture-api.js'||name==='fixture-start.js'){res.writeHead(200,{'Content-Type':'text/javascript'});res.end(name==='fixture-api.js'?api:bootstrap);return;}
  const file=path.resolve(root,name);
  if(!file.startsWith(root+path.sep)||!fs.existsSync(file)||!fs.statSync(file).isFile()){res.writeHead(404);res.end();return;}
  let body=fs.readFileSync(file);
  if(name==='index.html')body=body.toString().replace(/src="js\/api.js[^"]*"/,'src="fixture-api.js"').replace(/src="js\/app.js[^"]*"/,'src="fixture-start.js"');
  res.writeHead(200,{'Content-Type':name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':name.endsWith('.html')?'text/html':'application/octet-stream','Cache-Control':'no-store'});res.end(body);
}).listen(Number(process.argv[3])||0,'127.0.0.1',function(){console.log('http://127.0.0.1:'+this.address().port+'/');});
