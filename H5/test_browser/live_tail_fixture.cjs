// Run: node H5/test_browser/live_tail_fixture.cjs, open the printed URL, click Run.
// Local UI verification fixture. All API calls are mocked; no model or history writes.
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname,'..');
const api = `const api_url = location.origin;
window.API = new Proxy({BASE:api_url,base:api_url}, {get:(target,key) => key in target ? target[key] : async () => ({})});`;
const bootstrap = `
(function () {
 const panel = document.createElement('aside');
 panel.style = 'position:fixed;top:8px;left:250px;z-index:1000;background:#203944;color:white;padding:10px;border-radius:8px;max-width:700px';
 const run = document.createElement('button'); run.textContent = '运行长会话显示验证';
 const result = document.createElement('output'); result.style = 'display:block;font-size:13px';
 result.textContent = '模拟 API · 不调用模型 · 不读写真实会话';
 panel.append(run,result); document.body.append(panel);
 let release = null, calls = 0;
 const tick = () => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
 Object.assign(App, {ensureSessionId:() => 'display-fixture',upsertLocalSession() {},
   refreshContextTokenStats() {},scheduleContextTokenStatsRefresh() {},clearContextStatsTimer() {},
   refreshSessionUsage:async () => true,flushPendingMessages() {},getChatModelVision:() => true});
 API.chatStream = async (payload,handle,signal,options={}) => {
   calls++;
   if(options.onOpen)options.onOpen();
   handle({data:{round_started:{round:100+calls}}});
   handle({data:{reasoning_content:'正在检查前端消息布局。'}});
   handle({data:{content:calls === 1 ? '上一轮回复：应当排在最新消息之前。' : '最新回复已正常显示，历史窗口切换后继续更新。'}});
   if(calls === 2) await new Promise(resolve => {release=resolve;});
   handle({data:{finish_reason:'stop'}});
   return {completed:true};
 };
 run.onclick = async () => {
   run.disabled = true;
   const checks = [];
   const check = (condition,label) => { if(!condition) throw Error(label); checks.push(label); result.textContent = checks.join(' · '); };
   try {
     App.resetHistoryView(); App.chatInner.replaceChildren(); App.state.activeStream = null;
     App.state.sessionId='display-fixture'; App.state.hasConversation=true;
     App.state.selectedTools = new Set(); App.setEmpty(false);
     const records = Array.from({length:500},(_,i) => ({kind:i%5===0?'user':'assistant',round:Math.floor(i/5)+1,
       source_event_index:i%5,content:i%5===0?'历史问题 '+i:'历史回答 '+i+' '+('用于验证长会话的滚动布局。'.repeat(6)),ts:'2026-10-23 14:00:00'}));
     App.renderRecords(records); await tick();
     await App.send({text:'第一条验证消息（完成后保留在实时尾部）'});
     await tick();
     const previous = [...App.chatInner.children].slice(-2);
     const task = App.send({text:'第二条最新消息（历史切换后仍应出现在底部）'});
     await tick();
     const active = App.state.activeStream;
     check(Boolean(active),'发送成功');
     check(document.querySelector('#taskStatus').dataset.phase==='answering','任务状态同步为生成回答');
     App.pauseAutoScroll(); App.chatScroll.style.scrollBehavior='auto';
     App.chatScroll.scrollTop=App.chatScroll.scrollHeight*.35;
     await tick(); await tick();
     check(App.getHistoryWindow().end<records.length,'历史窗口可浏览');
     check(active.messageNode.classList.contains('history-live-hidden'),'阅读历史不抢滚动');
     App.chatScroll.scrollTop=App.chatScroll.scrollHeight;
     await tick(); await tick();
     check(App.getHistoryWindow().end===records.length,'原生滚动恢复尾部');
     const tail=[...App.chatInner.children].slice(-4);
     check(tail[0]===previous[0] && tail[1]===previous[1] && tail[2]===active.userNode && tail[3]===active.messageNode,'先旧轮次后最新轮次');
     check(!active.userNode.classList.contains('history-live-hidden') && !active.messageNode.classList.contains('history-live-hidden'),'最新提问和回答可见');
     App.scrollToBottom(true); await tick();
     const box=active.messageNode.getBoundingClientRect(),screen=App.chatScroll.getBoundingClientRect();
     check(box.bottom>screen.top && box.top<screen.bottom,'底部视口显示最新回复');
     release(); await task; await tick();
     check(!App.state.streaming,'正常收尾');
     check(document.querySelector('#taskStatus').dataset.phase==='completed','任务状态同步为已完成');
     result.textContent='通过 '+checks.length+'/'+checks.length+'：'+checks.join(' · ');
   } catch(error) { result.textContent='失败：'+error.message; console.error(error); if(release)release(); }
 };
})();`;
const server = http.createServer((req,res) => {
 const name = decodeURIComponent(new URL(req.url,'http://localhost').pathname).replace(/^\//,'') || 'index.html';
 if(name==='fixture-api.js' || name==='fixture-start.js') {
   res.setHeader('Content-Type','text/javascript'); res.end(name==='fixture-api.js'?api:bootstrap); return;
 }
 const file=path.resolve(root,name);
 if(!file.startsWith(root+path.sep) || !fs.existsSync(file) || !fs.statSync(file).isFile()) {res.writeHead(404);res.end();return;}
 let body=fs.readFileSync(file);
 if(name==='index.html') body=body.toString().replace(/src="js\/api.js[^\"]*"/,'src="fixture-api.js"').replace(/src="js\/app.js[^\"]*"/,'src="fixture-start.js"');
 res.setHeader('Content-Type',name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':name.endsWith('.html')?'text/html':'application/octet-stream');
 res.end(body);
});
server.listen(0,'127.0.0.1',() => console.log('http://127.0.0.1:'+server.address().port+'/'));
