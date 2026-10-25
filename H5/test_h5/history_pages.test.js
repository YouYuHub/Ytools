const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {JSDOM}=require('jsdom');
const parser=require('../js/history_parser');
const source=fs.readFileSync(path.join(__dirname,'../js/app/history_pages.js'),'utf8');
function setup(reply,allowed=true){
  const dom=new JSDOM('<div id="scroll"><div id="chat"></div></div>',{url:'https://fixture.test/ui/',runScripts:'outside-only',pretendToBeVisual:true});
  const w=dom.window,calls=[];
  w.YtoolsAuth={historyPagingAllowed:allowed};w.HistoryParser=parser;
  w.API={fetchSessionPage:async(id,options)=>{calls.push({id,options});return reply(id,options);}};
  w.App={state:{sessionId:'a'},chatInner:w.document.querySelector('#chat'),chatScroll:w.document.querySelector('#scroll'),toast(){},
    getHistoryWindow:()=>({records:[]}),rebuildQnav(){},el:(tag,cls,text)=>{const node=w.document.createElement(tag);node.className=cls;node.textContent=text;return node;}};
  w.eval(source);return {w,calls,dom,pages:w.App.historyPages};
}
function page(){return {revision:'r1',start_round:61,end_round:62,total_rounds:62,round_offset:60,questions:[{round:1,event_index:0,text:'old'},{round:61,event_index:0,text:'q61'},{round:62,event_index:0,text:'q62'}],
  text:JSON.stringify({_meta:{usage:{total_tokens:5}}})+'\n'+[61,62].map(n=>JSON.stringify({event:'chat_round',events:[{role:'user',content:'q'+n},{role:'assistant',content:'answer'+n}]})).join('\n')};}
test('tail page keeps absolute round IDs and complete lightweight navigation',async()=>{
  const f=setup(()=>page());try{
    const parsed=await f.pages.open('a');assert.deepEqual(Array.from(parsed.records.filter(r=>r.kind==='user'),r=>r.round),[61,62]);
    const nav=f.pages.navigation([{round:61,eventIndex:0,index:0,text:'loaded',node:null},{round:62,eventIndex:0,index:2,text:'loaded',node:null}]);
    assert.equal(nav.length,3);assert.equal(nav[0].round,1);assert.equal(nav[0].index,-1);assert.equal(nav[1].text,'loaded');
    assert.equal(f.pages.hasRound(1),false);assert.equal(f.pages.hasRound(61),true);
  }finally{f.dom.window.close();}
});
test('old backend and saved-checkpoint overlay preserve legacy loading',async()=>{
  const legacy=setup(()=>{throw new Error('must not call');},false);assert.equal(await legacy.pages.open('a'),null);assert.equal(legacy.calls.length,0);legacy.dom.window.close();
  const saved=setup(()=>({fallback_full:true}));assert.equal(await saved.pages.open('a'),null);assert.equal(saved.pages.active(),false);saved.dom.window.close();
});
test('late response after a session/account reset cannot populate the new view',async()=>{
  let resolve;const f=setup(()=>new Promise(done=>{resolve=done;}));
  const pending=f.pages.open('a');f.pages.reset();f.w.App.state.sessionId='b';resolve(page());
  assert.equal((await pending).stale,true);assert.equal(f.pages.active(),false);f.dom.window.close();
});
test('authorization failure does not silently downgrade to a full-file read',async()=>{
  const f=setup(()=>{const error=new Error('denied');error.status=403;throw error;});
  await assert.rejects(f.pages.open('a'),/denied/);assert.equal(f.calls.length,1);f.dom.window.close();
});
test('parser uses page offsets for anchored compaction and user rounds',()=>{
  const p=page();p.text+='\n'+JSON.stringify({event:'context_compaction',scope:'session',phase:'done',display_round:61,display_event_index:0,summary_text:'summary'});
  const parsed=parser.parseHistory(p.text,{roundOffset:60});
  assert(parsed.records.some(r=>r.kind==='compaction'&&r.round===61));
  assert.equal(parsed.records.filter(r=>r.kind==='user')[0].round,61);
});
