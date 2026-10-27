const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM, VirtualConsole } = require('jsdom');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../js/security_client.js'), 'utf8');
function setup() {
  const dom = new JSDOM('<head><script id="account-context" type="application/json">{"user":{"id":"alice","role":"member"},"csrf":"csrf-a","execution_allowed":false,"account_sharing":true}</script></head>',
    { url: 'https://chat.example.test/ui/', runScripts: 'outside-only', virtualConsole: new VirtualConsole() });
  const w = dom.window, calls = [], replies = [];
  w.Headers = Headers; w.Request = Request;
  w.localStorage.setItem('legacy', 'original');
  w.localStorage.setItem('ytools:user:bob:draft', 'bob-private');
  w.fetch = async (url, options) => { calls.push({ url: String(url), options }); return replies.shift() || { ok: true, status: 200, headers: new Headers(), json: async () => ({}) }; };
  const rawGet = w.Storage.prototype.getItem;
  w.eval(source);
  return { dom, w, calls, rawGet, replies };
}
test('authenticated fetch adds CSRF only to same-origin mutations', async () => {
  const { dom, w, calls } = setup();
  await w.fetch('/user/profile', { method: 'POST', body: '{}' });
  assert.equal(calls[0].options.headers.get('X-CSRF-Token'), 'csrf-a');
  assert.equal(calls[0].options.headers.get('X-Ytools-Expected-Account'), 'alice');
  assert.equal(calls[0].options.credentials, 'same-origin');
  await w.fetch('https://external.example.test/data', { method: 'POST' });
  assert.equal(calls[1].options.headers, undefined);
  w.YtoolsAuth.update({ csrf: 'rotated' });
  await w.fetch('/auth/logout', { method: 'POST' });
  assert.equal(calls[2].options.headers.get('X-CSRF-Token'), 'rotated'); dom.window.close();
});
test('storage and clear only affect the authenticated account namespace', () => {
  const { dom, w, rawGet } = setup();
  w.localStorage.setItem('draft', 'alice-private');
  assert.equal(w.localStorage.getItem('draft'), 'alice-private');
  assert.equal(rawGet.call(w.localStorage, 'draft'), null);
  w.localStorage.clear();
  assert.equal(rawGet.call(w.localStorage, 'legacy'), 'original');
  assert.equal(rawGet.call(w.localStorage, 'ytools:user:bob:draft'), 'bob-private');
  assert.equal(w.localStorage.getItem('draft'), null); dom.window.close();
});
test('new-session authorization is deduplicated and does not rely on web-storage credentials', async () => {
  const { dom, w, calls, rawGet } = setup();
  await Promise.all([w.fetch('/file/upload_session_files?session_id=new-chat',{method:'POST'}), w.fetch('/file/upload_session_media?session_id=new-chat',{method:'POST'})]);
  assert.equal(calls.filter(call => call.url === '/chat_history/ensure_session').length, 1);
  assert.equal(w.YtoolsAuth.executionAllowed, false);
  assert.equal(w.document.getElementById('account-context').textContent, '');
  assert.equal(rawGet.call(w.localStorage, 'csrf'), null); dom.window.close();
});
test('single-session summary lookup stays read-only even when access is denied', async t => {
  const {dom,w,calls,replies}=setup();t.after(()=>dom.window.close());
  replies.push({ok:false,status:404,headers:new Headers(),json:async()=>({detail:'not found'})});
  const response=await w.fetch('/chat_history/summaries?session_id=default&archive_scope=all');
  assert.equal(response.status,404);assert.equal(calls.length,1);
  assert.ok(calls.every(call=>call.url!=='/chat_history/ensure_session'));
});
test('all GET lookups and raw imports avoid claiming source session identifiers', async t=>{
  const {dom,w,calls}=setup();t.after(()=>dom.window.close());
  await w.fetch('/chat_history/file?session_id=unknown');await w.fetch('/chat_history/meta?session_id=unknown');
  await w.fetch('/chat_config/models?session_id=unknown');
  await w.fetch('/chat_history/upload_chat_file?session_id=foreign',{method:'POST',body:'fixture'});
  assert.equal(calls.length,4);assert.ok(calls.every(call=>call.url!=='/chat_history/ensure_session'));
});
test('identity bootstrap must run before preferences and deferred application code', () => {
  const html = fs.readFileSync(path.join(__dirname, '../index.html'), 'utf8');
  assert.match(html, /<script src="js\/security_client\.js[^\"]*"><\/script>/);
  assert.ok(html.indexOf('security_client.js') < html.indexOf('js/theme.js'));
});
test('file editor uses the same identity boundary before preferences and API code', () => {
  const html = fs.readFileSync(path.join(__dirname, '../editor.html'), 'utf8');
  assert.match(html, /<html[^>]*data-ytools-auth-required/);
  assert.ok(html.indexOf('js/security_client.js') > 0);
  assert.ok(html.indexOf('js/security_client.js') < html.indexOf('js/theme.js'));
  assert.ok(html.indexOf('js/security_client.js') < html.indexOf('js/api.js'));
});
test('file change storage fallback recognizes only this account events', () => {
  const { dom, w } = setup();
  w.eval(fs.readFileSync(path.join(__dirname, '../js/file_history_sync.js'), 'utf8'));
  let notifications = 0;
  const unsubscribe = w.FileHistorySync.subscribe(() => { notifications++; });
  w.dispatchEvent(new w.StorageEvent('storage', { key: 'ytools:user:alice:ytools-file-history-dirty', newValue: '1', storageArea: w.localStorage }));
  assert.equal(notifications, 1);
  w.dispatchEvent(new w.StorageEvent('storage', { key: 'ytools:user:bob:ytools-file-history-dirty', newValue: '1', storageArea: w.localStorage }));
  w.dispatchEvent(new w.StorageEvent('storage', { key: 'ytools-file-history-dirty', newValue: '1', storageArea: w.localStorage }));
  assert.equal(notifications, 1);
  unsubscribe(); dom.window.close();
});
test('file change broadcasts use an account-specific channel', () => {
  const { dom, w } = setup(), channels = [];
  w.BroadcastChannel = class {
    constructor(name) { this.name = name; channels.push(name); }
    postMessage() {}
    close() {}
  };
  w.eval(fs.readFileSync(path.join(__dirname, '../js/file_history_sync.js'), 'utf8'));
  const unsubscribe = w.FileHistorySync.subscribe(() => {});
  w.FileHistorySync.markDirty();
  assert.equal(channels.length, 2);
  assert.equal(channels[0], 'ytools:user:alice:ytools-file-history');
  assert.equal(channels[1], channels[0]);
  unsubscribe(); dom.window.close();
});
test('CSRF rotation in another tab refreshes and retries only the rejected request', async () => {
  const { dom, w, calls, replies } = setup();
  replies.push({ ok: false, status: 403, headers: new Headers({ 'X-CSRF-Required': 'true' }) },
    { ok: true, status: 200, headers: new Headers({ 'X-Ytools-Account': 'alice' }), json: async () => ({ csrf: 'new-csrf', user:{id:'alice'}, mfa_verified:true }) });
  await w.fetch('/user/profile', { method: 'POST', body: '{}' });
  assert.equal(calls.length, 3); assert.equal(calls[1].url, '/auth/me');
  assert.equal(calls[2].options.headers.get('X-CSRF-Token'), 'new-csrf'); dom.window.close();
});
test('another account cookie cannot populate this account UI or storage', async () => {
  const { dom, w, calls, replies } = setup();
  let lost = false; w.addEventListener('ytools-auth-lost', () => { lost = true; });
  replies.push({ ok: true, status: 200, headers: new Headers({ 'X-Ytools-Account': 'bob' }) });
  await assert.rejects(w.fetch('/chat_history/summaries'), /账号已变化/);
  assert.equal(lost, true); assert.equal(calls.length, 1); dom.window.close();
});
function entry(protocol, pathname, stored) {
  const targets = [];
  const location = { protocol, pathname, origin: 'http://localhost:48621', search: '?session=example', hash: '', replace: url => targets.push(url) };
  const window = { localStorage: { getItem: () => stored }, location };
  const document = { getElementById: () => null, documentElement: { hasAttribute: () => true } };
  vm.runInNewContext(source, { window, document, location, URL });
  return targets;
}
test('local and standalone index redirect to the authenticated backend', () => {
  assert.deepEqual(entry('file:', '/C:/project/H5/index.html'), ['http://127.0.0.1:48621/ui/?session=example']);
  assert.deepEqual(entry('http:', '/H5/index.html', 'https://chat.example.test'), ['http://127.0.0.1:48621/ui/?session=example']);
  assert.deepEqual(entry('file:', '/index.html', 'javascript:alert(1)'), ['http://127.0.0.1:48621/ui/?session=example']);
});
test('missing server identity redirects to login instead of silently disabling auth', () => {
  assert.deepEqual(entry('http:', '/ui/index.html'), ['http://localhost:48621/ui/login.html']);
});
test('account refresh checks the body identity and MFA even if a proxy omitted the account header',async t=>{
  const {dom,w,replies}=setup();t.after(()=>dom.window.close());
  replies.push({ok:true,status:200,headers:new Headers(),json:async()=>({user:{id:'alice',role:'member',name:'Updated'},csrf:'new',mfa_verified:true,account_panel:true})});
  await w.YtoolsAuth.refresh();assert.equal(w.YtoolsAuth.user.name,'Updated');assert.equal(w.YtoolsAuth.csrf,'new');assert.equal(w.YtoolsAuth.accountPanelAllowed,true);
  let lost=false;w.addEventListener('ytools-auth-lost',event=>{lost=true;event.preventDefault();});
  replies.push({ok:true,status:200,headers:new Headers(),json:async()=>({user:{id:'bob'},mfa_verified:true})});
  await assert.rejects(w.YtoolsAuth.refresh(),/身份已变化/);assert.equal(lost,true);assert.equal(w.YtoolsAuth.user.id,'alice');
});

function fakeUploads(w, responses) {
  const sent=[];
  w.XMLHttpRequest=class {
    constructor(){this.upload={};this.headers={};this.aborted=false;}
    open(method,url){this.method=method;this.url=url;}
    setRequestHeader(key,value){this.headers[key]=value;}
    getResponseHeader(key){return new Headers(this.reply.headers||{}).get(key);}
    send(body){this.body=body;this.reply=responses.shift()||{status:200,body:{ok:true}};sent.push(this);
      if(this.reply.hold)return;
      queueMicrotask(()=>{if(this.aborted)return;this.status=this.reply.status;this.responseText=JSON.stringify(this.reply.body||{});
        this.upload.onprogress({lengthComputable:true,loaded:5,total:5});this.upload.onload();this.onload();});
    }
    abort(){this.aborted=true;if(this.onabort)this.onabort();}
  };
  return sent;
}
test('authenticated uploads send CSRF and expected account; imports do not register source IDs',async t=>{
  const {dom,w,calls}=setup();t.after(()=>dom.window.close());
  const sent=fakeUploads(w,[{status:200,headers:{'X-Ytools-Account':'alice'},body:{total:1}}]),progress=[];
  const body=new w.File(['fixture'],'share.zip');
  const result=await w.YtoolsAuth.upload('/chat_history/import_preview?session_id=foreign',body,value=>progress.push(value));
  assert.equal(result.total,1);assert.equal(sent[0].withCredentials,true);
  assert.equal(sent[0].headers['X-CSRF-Token'],'csrf-a');assert.equal(sent[0].headers['X-Ytools-Expected-Account'],'alice');
  assert.equal(sent[0].body,body);assert.equal(calls.length,0);assert.equal(progress.at(-1).phase,'done');
});
test('an upload retries only an explicit CSRF rejection after validating the refreshed account',async t=>{
  const {dom,w,calls,replies}=setup();t.after(()=>dom.window.close());
  const sent=fakeUploads(w,[{status:403,headers:{'X-CSRF-Required':'true','X-Ytools-Account':'alice'}},{status:200,body:{saved:true}}]);
  replies.push({ok:true,status:200,headers:new Headers(),json:async()=>({user:{id:'alice'},mfa_verified:true,csrf:'rotated'})});
  assert.equal((await w.YtoolsAuth.upload('/chat_history/import_package',new w.Blob(['fixture']))).saved,true);
  assert.equal(sent.length,2);assert.equal(sent[1].headers['X-CSRF-Token'],'rotated');assert.equal(calls[0].url,'/auth/me');
});
test('foreign account upload responses are rejected before consuming the body',async t=>{
  const {dom,w}=setup();t.after(()=>dom.window.close());let lost=false;w.addEventListener('ytools-auth-lost',event=>{lost=true;event.preventDefault();});
  fakeUploads(w,[{status:200,headers:{'X-Ytools-Account':'bob'},body:{secret:'must not adopt'}}]);
  await assert.rejects(w.YtoolsAuth.upload('/chat_history/import_preview',new w.Blob()),/账号已变化/);
  assert.equal(lost,true);assert.equal(w.YtoolsAuth.user.id,'alice');
});
test('upload cancellation aborts transport; foreign origins are never contacted',async t=>{
  const {dom,w}=setup();t.after(()=>dom.window.close());
  const sent=fakeUploads(w,[{status:200,hold:true}]),controller=new w.AbortController();
  const pending=w.YtoolsAuth.upload('/chat_history/import_preview',new w.Blob(),null,controller.signal);
  controller.abort();await assert.rejects(pending,error=>error.name==='AbortError');assert.equal(sent[0].aborted,true);
  await assert.rejects(w.YtoolsAuth.upload('https://foreign.example/import',new w.Blob()),/当前服务/);assert.equal(sent.length,1);
});
