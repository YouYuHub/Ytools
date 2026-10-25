const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),{JSDOM}=require('jsdom');
function setup(t){
  const dom=new JSDOM('<main></main>',{runScripts:'outside-only'});t.after(()=>dom.window.close());
  const w=dom.window,root=w.document.querySelector('main');
  w.App={state:{sessionId:'fixture'},chatInner:root,el(tag,cls,text){const n=w.document.createElement(tag);n.className=cls||'';if(text!=null)n.textContent=text;return n;}};
  w.eval(fs.readFileSync(path.join(__dirname,'../js/app/process_groups.js'),'utf8'));
  return {w,root,api:w.App.processGroups};
}

test('a collapsed process is one indivisible history window unit including tool results',t=>{
  const {api,root}=setup(t);
  const records=[{kind:'user',round:1},{kind:'think',round:1},{kind:'tool',round:1},
    {kind:'toolResult',round:1},{kind:'think',round:1},{kind:'assistant',round:1},{kind:'think',round:1}];
  const bounds=api.windowBounds(records,3,4);
  assert.equal(bounds.start,1);assert.equal(bounds.end,5);
  assert.equal(root.children.length,0,'window planning must not render hidden stages');
  const single=api.windowBounds(records,6,7);assert.equal(single.start,6);assert.equal(single.end,7);
});

test('expanded processes remain windowed and record grouping does not cross rounds',t=>{
  const {w,api,root}=setup(t);
  const records=[{kind:'think',round:1},{kind:'tool',round:1},{kind:'toolResult',round:1},
    {kind:'think',round:2},{kind:'tool',round:2},{kind:'toolResult',round:2}];
  api.indexRecords(records);
  for(let i=0;i<2;i++){const node=w.App.el('div',i?'tool-block':'think-block');node.dataset.round='1';api.tagRecord(node,records[i]);root.append(node);}
  api.sync(root);root.querySelector('.process-group-toggle').click();
  const open=api.windowBounds(records,1,2);assert.equal(open.start,1);assert.equal(open.end,2);
  const closed=api.windowBounds(records,4,5);assert.equal(closed.start,3);assert.equal(closed.end,6);
});

test('dependent results in another round do not extend a preceding folded process',t=>{
  const {api}=setup(t);
  const records=[{kind:'think',round:1},{kind:'tool',round:1},
    {kind:'toolResult',round:2},{kind:'think',round:2},{kind:'tool',round:2}];
  const first=api.windowBounds(records,1,2);assert.equal(first.start,0);assert.equal(first.end,2);
  const orphan=api.windowBounds(records,2,3);assert.equal(orphan.start,2);assert.equal(orphan.end,3);
});
