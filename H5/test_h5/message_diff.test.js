const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),{JSDOM}=require('jsdom');

function setup(t){
  const dom=new JSDOM('<main></main>',{runScripts:'outside-only'});t.after(()=>dom.window.close());
  const w=dom.window;
  w.App={state:{workDir:'/one'},el(tag,cls,text){const node=w.document.createElement(tag);node.className=cls||'';if(text!=null)node.textContent=text;return node;}};
  w.eval(fs.readFileSync(path.join(__dirname,'../js/app/message_diff.js'),'utf8'));
  return w;
}

test('diff renderer works independently of message orchestration and escapes source text',t=>{
  const w=setup(t), api=w.App.messageDiff;
  const view=api.buildDiffView({diff:'--- a/x.js\n+++ b/x.js\n@@ -1 +1 @@\n-old\n+<img src=x onerror=alert(1)>',lines_added:1,lines_removed:1});
  assert.equal(view.querySelectorAll('.is-add').length>0,true);
  assert.equal(view.querySelectorAll('img,script').length,0);
  assert.match(view.textContent,/<img src=x/);
});

test('tool path labels still follow the current session work directory',t=>{
  const w=setup(t),api=w.App.messageDiff;
  assert.equal(api.resolveToolPath('x.py'),'/one/x.py');
  w.App.state.workDir='C:\\two';
  assert.equal(api.resolveToolPath('x.py'),'C:\\two\\x.py');
  assert.equal(api.resolveToolPath('/absolute/x.py'),'/absolute/x.py');
  assert.equal(api.extractPathFromArgsValue('{"path":"partial.py"','edit_file'),'partial.py');
});
