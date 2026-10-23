/* Isolated UI preview. Serves real assets; no model requests or user history. */
const http=require('node:http'),fs=require('node:fs'),path=require('node:path');
const root=path.resolve(__dirname,'..');
const index=fs.readFileSync(path.join(root,'index.html'),'utf8');
const status=index.match(/<div class="task-status"[\s\S]*?<\/div>/)[0];
const html=`<!doctype html><html lang="zh-CN" data-theme="dark"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>任务状态栏验证</title><link rel="stylesheet" href="/ui/style/css/main.css">
<style>body{display:block;padding:32px;background:var(--bg);color:var(--text)}h1{font-size:20px}nav{display:flex;flex-wrap:wrap;gap:8px;margin:20px 0}nav button{padding:7px 12px;background:var(--bg-hover);border:1px solid var(--border);border-radius:8px;color:var(--text)}.preview-stage{max-width:980px;margin:32px auto;padding:16px;border:1px solid var(--border);border-radius:16px}.preview-stage.narrow{max-width:390px}.preview-stage .bottom{position:static;padding:12px 0;width:100%;}.preview-stage .composer-wrap{width:100%;max-width:none}.preview-stage .composer{display:flex;padding:14px;border:1px solid var(--border-strong);border-radius:22px}.preview-stage textarea{width:100%;height:40px;resize:none;background:transparent;border:0;color:var(--text);font:inherit}.sample{min-height:100px;font-size:15px;line-height:1.8}.preview-note{font-size:12px;color:var(--text-secondary)}</style>
<h1>任务状态栏验证</h1><p class="preview-note">使用项目实际 CSS 与状态模块；操作仅切换本地模拟状态。</p>
<nav><button data-mode="thinking">思考</button><button data-mode="tools">工具</button><button data-mode="approval">待确认</button><button data-mode="budget">预算等待</button><button data-mode="question">等待回复</button><button data-mode="disconnected">断线</button><button data-mode="completed">完成</button><button id="theme">切换明暗</button><button id="width">切换窄屏</button></nav>
<section class="preview-stage"><div class="sample">我正在核对项目中的工具与任务流程。<br>这里保留回答正文，运行阶段显示在输入框上方。</div><div class="bottom"><div class="composer-wrap">${status}<div class="composer"><textarea aria-label="消息" placeholder="应该做什么"></textarea></div></div></div></section><p id="result" class="preview-note"></p>
<script>window.App={state:{sessionId:'preview',streaming:false},toast:m=>document.querySelector('#result').textContent=m};window.API={streamStatus:async()=>({running:true})};</script>
<script src="/ui/js/task_state.js"></script><script src="/ui/js/app/task_status.js"></script>
<script>
App.attachStreamSession=async sid=>{const h=App.taskStatus.begin(sid);App.taskStatus.connected(h);App.taskStatus.event(h,{data:{reasoning_content:'continued'}});document.querySelector('#result').textContent='已附接；未重新发送提问';};
App.openSession=async()=>{};
async function scenario(mode){document.querySelector('#result').textContent='';const s=App.taskStatus,h=s.begin('preview');s.connected(h);const frame=data=>s.event(h,{data});
if(mode==='thinking')frame({reasoning_content:'plan'});
if(mode==='tools'){frame({tool_start:{tool_call_id:'a',function_name:'read_file'}});frame({tool_start:{tool_call_id:'b',function_name:'run_command'}});}
if(mode==='approval')frame({event:'tool_approval',phase:'requested',approval:{id:'a',agent_id:'main',state:'pending'}});
if(mode==='budget')frame({event:'task_usage',task:{kind:'chat',state:'paused'}});
if(mode==='question'){frame({event:'ask_user'});await s.end(h,true);}
if(mode==='disconnected')await s.end(h,false);
if(mode==='completed')await s.end(h,true);
}
document.querySelectorAll('[data-mode]').forEach(b=>b.onclick=()=>scenario(b.dataset.mode));
document.querySelector('#theme').onclick=()=>{document.documentElement.dataset.theme=document.documentElement.dataset.theme==='dark'?'light':'dark';};
document.querySelector('#width').onclick=()=>document.querySelector('.preview-stage').classList.toggle('narrow');scenario('thinking');
</script></html>`;
const server=http.createServer((req,res)=>{
  const url=new URL(req.url,'http://localhost');
  if(url.pathname==='/preview'){res.writeHead(200,{'Content-Type':'text/html;charset=utf-8','Cache-Control':'no-store'});res.end(html);return;}
  const file=path.resolve(root,decodeURIComponent(url.pathname.replace(/^\/ui\//,'')));
  if(!file.startsWith(root+path.sep)||!fs.existsSync(file)||!fs.statSync(file).isFile()){res.writeHead(404);res.end();return;}
  res.writeHead(200,{'Content-Type':file.endsWith('.js')?'application/javascript; charset=utf-8':file.endsWith('.css')?'text/css':'application/octet-stream','Cache-Control':'no-store'});fs.createReadStream(file).pipe(res);
});
server.listen(0,'127.0.0.1',()=>console.log('http://127.0.0.1:'+server.address().port+'/preview'));
