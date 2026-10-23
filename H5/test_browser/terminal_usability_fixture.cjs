// Isolated page fixture: real UI modules, simulated tool results, no model/process calls.
const http = require('node:http'), fs = require('node:fs'), path = require('node:path');
const root = path.resolve(__dirname, '..');
const api = `window.API=new Proxy({BASE:location.origin,base:location.origin},{get:(o,k)=>k in o?o[k]:async()=>({})});`;
const bootstrap = `(function(){
  document.documentElement.dataset.theme='dark';App.state.sessionId='terminal-fixture';App.state.hasConversation=true;App.setEmpty(false);
  App.sessionList.append(App.buildSessionItem('terminal-fixture','终端输出与时间提示验证'));
  const cases=[
    ['run_command','普通命令：标题仅保留悬停时间提示。',{status:'running',message:'[run_command] status=running | exit=null',output:'开始处理…',job_id:'bg_demo'}],
    ['poll_command','继续查看：显示新增输出。',{status:'completed',exit_code:0,message:'[run_command] status=completed | exit=0',output:'处理完成。'}],
    ['write_stdin','交互输入：显示输入回执与新增输出。',{status:'running',message:'[run_command] status=running | exit=null',input_status:'accepted',output:'echo:hello'}],
    ['write_stdin','输入失败：回执可见并标记错误。',{status:'running',message:'[run_command] status=running | exit=null',input_status:'failed',input_error:'程序已关闭输入管道。'}]
  ];
  cases.forEach(([name,label,result])=>{
    const msg=App.el('div','msg');msg.append(App.el('p','',label));
    const ui=App.buildToolBlock(name);msg.append(ui.wrap);App.chatInner.append(msg);
    App.applyToolResult(ui,name,JSON.stringify(result),null,{});ui.finish();
    App.processGroups.setTimestamp(ui.wrap,'10-24 00:42:12');
    if(name==='write_stdin')ui.wrap.classList.add('open');
  });
})();`;
http.createServer((req, res) => {
  const name = decodeURIComponent(new URL(req.url, 'http://localhost').pathname).replace(/^\//, '') || 'index.html';
  if (name === 'fixture-api.js' || name === 'fixture-start.js') {
    res.writeHead(200, {'Content-Type': 'text/javascript'}); res.end(name === 'fixture-api.js' ? api : bootstrap); return;
  }
  const file = path.resolve(root, name);
  if (!file.startsWith(root + path.sep) || !fs.existsSync(file) || !fs.statSync(file).isFile()) {res.writeHead(404);res.end();return;}
  let body = fs.readFileSync(file);
  if (name === 'index.html') body = body.toString().replace(/src="js\/api.js[^"]*"/, 'src="fixture-api.js"').replace(/src="js\/app.js[^"]*"/, 'src="fixture-start.js"');
  res.writeHead(200, {'Content-Type': name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : name.endsWith('.html') ? 'text/html' : 'application/octet-stream', 'Cache-Control': 'no-store'});
  res.end(body);
}).listen(0, '127.0.0.1', function () {console.log('http://127.0.0.1:' + this.address().port + '/');});
