const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),{JSDOM}=require('jsdom');
function setup(t){
  const dom=new JSDOM('<button id="download"></button><pre id="codes"></pre>',{url:'http://localhost/ui/account.html',runScripts:'outside-only'});
  t.after(()=>dom.window.close());const w=dom.window,created=[],revoked=[],downloads=[],timers=[];
  w.URL.createObjectURL=blob=>{created.push(blob);return 'blob:fixture-'+created.length;};
  w.URL.revokeObjectURL=url=>revoked.push(url);w.setTimeout=callback=>{timers.push(callback);return timers.length;};
  w.HTMLAnchorElement.prototype.click=function(){downloads.push({url:this.href,name:this.download});};
  w.fetch=()=>assert.fail('recovery download must remain local');
  w.eval(fs.readFileSync(path.join(__dirname,'../js/recovery_codes.js'),'utf8'));
  const button=w.document.getElementById('download'),source=w.document.getElementById('codes');
  w.YtoolsRecoveryCodes.bindDownload(button,source);return {w,button,source,created,revoked,downloads,timers};
}
test('recovery export needs a click, includes only displayed codes and releases its object URL',async t=>{
  const {w,button,source,created,revoked,downloads,timers}=setup(t);
  source.textContent='fixture-code-one\nfixture-code-two';assert.equal(created.length,0);button.click();
  assert.equal(downloads[0].name,'ytools-recovery-codes.txt');assert.equal(created.length,1);
  const text=await new Promise(resolve=>{const reader=new w.FileReader();reader.onload=()=>resolve(reader.result);reader.readAsText(created[0]);});
  assert.match(text,/fixture-code-one\nfixture-code-two/);timers[0]();assert.deepEqual(revoked,['blob:fixture-1']);
  assert.equal(w.localStorage.length,0);assert.equal(w.sessionStorage.length,0);
});
test('cleared recovery codes cannot be downloaded',t=>{
  const {button,created}=setup(t);button.click();assert.equal(created.length,0);
});
test('leaving the page revokes an outstanding recovery download URL',t=>{
  const {w,button,source,revoked}=setup(t);source.textContent='fixture-code';button.click();
  w.dispatchEvent(new w.Event('pagehide'));assert.deepEqual(revoked,['blob:fixture-1']);
});
