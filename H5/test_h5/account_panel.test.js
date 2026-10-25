const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {JSDOM} = require('jsdom');
const source = file => fs.readFileSync(path.join(__dirname,'../js/',file),'utf8');
const settle = () => new Promise(resolve=>setTimeout(resolve,100));
async function until(check) { const end=Date.now()+2000;while(!check()){if(Date.now()>end)throw Error('history navigation did not settle');await new Promise(resolve=>setTimeout(resolve,10));} }
function setup(enabled=true) {
  const dom = new JSDOM('<button id="settings">settings</button><main id="chat"><textarea>unsent draft</textarea><div id="reply">reply</div></main>',
    {url:'https://fixture.example/ui/?session=alpha',runScripts:'outside-only',pretendToBeVisual:true});
  const w=dom.window, calls=[];
  w.HTMLDialogElement.prototype.showModal=function(){this.setAttribute('open','');};
  w.HTMLDialogElement.prototype.close=function(){this.removeAttribute('open');this.dispatchEvent(new w.Event('close'));};
  w.YtoolsAuth={user:{id:'alice'},accountPanelAllowed:enabled,refresh:async()=>calls.push('identity'),lost:reason=>calls.push(['lost',reason])};
  w.AccountPreferences={refresh:()=>calls.push('preferences')};
  w.App={state:{sessionId:'alpha',streaming:true},loadUserProfile:()=>calls.push('profile')};
  w.eval(source('app/account_panel.js'));
  function send(overrides={}, options={}) {
    const frame=w.document.querySelector('iframe');
    const token=new URL(frame.src).searchParams.get('panel');
    w.dispatchEvent(new w.MessageEvent('message',{origin:w.location.origin,source:frame.contentWindow,
      data:{channel:'ytools-account-panel',token,userId:'alice',type:'ready',...overrides},...options}));
  }
  return {dom,w,calls,send};
}
test('open/return keeps chat nodes, drafts and streaming state; refreshes only account data',async t=>{
  const f=setup();t.after(()=>f.w.close());const {w}=f;
  const reply=w.document.getElementById('reply');w.document.getElementById('settings').focus();
  assert.equal(w.App.accountPanel.open(),true);assert.equal(w.location.hash,'#account');
  const frame=w.document.querySelector('iframe');assert.equal(frame.hidden,true);f.send();assert.equal(frame.hidden,false);
  f.send({type:'close'});await until(()=>!w.App.accountPanel.isOpen());
  assert.equal(w.document.getElementById('accountPanel'),null);assert.equal(w.location.hash,'');
  assert.equal(w.document.getElementById('reply'),reply);assert.equal(w.document.querySelector('textarea').value,'unsent draft');
  assert.equal(w.App.state.streaming,true);assert.equal(w.document.activeElement.id,'settings');
  assert.deepEqual(f.calls,['identity','profile','preferences']);
});
test('browser Back/Forward recreates just one account panel and duplicate closes do not go back twice',async t=>{
  const {w,send}=setup();t.after(()=>w.close());w.App.accountPanel.open();send();
  w.App.accountPanel.close();w.App.accountPanel.close();await until(()=>!w.App.accountPanel.isOpen());assert.equal(w.App.accountPanel.isOpen(),false);
  w.history.forward();await until(()=>w.App.accountPanel.isOpen());assert.equal(w.App.accountPanel.isOpen(),true);
  assert.equal(w.document.querySelectorAll('#accountPanel').length,1);
  const cancel=new w.Event('cancel',{cancelable:true});w.document.getElementById('accountPanel').dispatchEvent(cancel);
  assert.equal(cancel.defaultPrevented,true);await until(()=>!w.App.accountPanel.isOpen());assert.equal(w.App.accountPanel.isOpen(),false);
});
test('messages must match the origin, source window and current opening token',t=>{
  const f=setup();t.after(()=>f.w.close());f.w.App.accountPanel.open();
  f.send({}, {origin:'https://external.example'});assert.equal(f.w.document.querySelector('iframe').hidden,true);
  f.send({}, {source:f.w});assert.equal(f.w.document.querySelector('iframe').hidden,true);
  f.send({token:'0'.repeat(32)});assert.equal(f.w.document.querySelector('iframe').hidden,true);
  f.send();assert.equal(f.w.document.querySelector('iframe').hidden,false);assert.deepEqual(f.calls,[]);
});
test('another account or a revoked child login invalidates the parent login',t=>{
  const f=setup();t.after(()=>f.w.close());f.w.App.accountPanel.open();f.send({userId:'bob'});
  assert.deepEqual(f.calls,[['lost',undefined]]);
  f.send({type:'auth-lost',reason:'username_changed'});assert.deepEqual(f.calls.at(-1),['lost','username_changed']);
});
test('unsupported backend/browser retains standalone account navigation',t=>{
  const f=setup(false);t.after(()=>f.w.close());assert.equal(f.w.App.accountPanel.open(),false);
  assert.equal(f.w.document.querySelector('iframe'),null);assert.equal(f.w.location.hash,'');
});
function child(embedded=true) {
  const dom=new JSDOM('<a id="backToChat" href="/ui/?session=alpha">back</a>',{url:'https://fixture.example/ui/account.html?panel='+ 'a'.repeat(32),runScripts:'outside-only'});
  const w=dom.window,messages=[];
  if(embedded)Object.defineProperty(w,'parent',{value:{postMessage:(data,origin)=>messages.push({data,origin})}});
  w.YtoolsAuth={user:{id:'alice'}};w.eval(source('account_panel_client.js'));
  return {w,messages};
}
test('embedded account returns to parent and forwards username-change/logout without exposing credentials',async t=>{
  const f=child();t.after(()=>f.w.close());await settle();assert.equal(f.messages[0].data.type,'ready');
  assert.equal(f.messages[0].origin,'https://fixture.example');
  const click=new f.w.MouseEvent('click',{bubbles:true,cancelable:true,button:0});
  f.w.document.getElementById('backToChat').dispatchEvent(click);assert.equal(click.defaultPrevented,true);
  assert.equal(f.messages.at(-1).data.type,'close');
  const lost=new f.w.CustomEvent('ytools-auth-lost',{cancelable:true,detail:{reason:'username_changed'}});
  f.w.dispatchEvent(lost);assert.equal(lost.defaultPrevented,true);assert.equal(f.messages.at(-1).data.reason,'username_changed');
  assert.deepEqual(Object.keys(f.messages.at(-1).data).sort(),['channel','reason','token','type','userId']);
});
test('standalone account pages do not delegate authentication navigation',async t=>{
  const f=child(false);t.after(()=>f.w.close());await settle();
  const lost=new f.w.Event('ytools-auth-lost',{cancelable:true});f.w.dispatchEvent(lost);
  assert.equal(lost.defaultPrevented,false);assert.deepEqual(f.messages,[]);
});
