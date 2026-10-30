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
function pageRange(start,end,total){
  return {revision:'r1',start_round:start,end_round:end,total_rounds:total,round_offset:start-1,
    text:JSON.stringify({_meta:{usage:{total_tokens:5}}})+'\n'+Array.from({length:end-start+1},(_,i)=>JSON.stringify({event:'chat_round',events:[{role:'user',content:'q'+(start+i)},{role:'assistant',content:'answer'+(start+i)}]})).join('\n')};
}
function setupCatchUp(reply){
  const f=setup(reply);let lastAll=[];
  f.w.App.getHistoryWindow=()=>({records:lastAll});
  f.w.App.renderRecords=records=>{lastAll=records;const chat=f.w.document.querySelector('#chat');
    records.forEach((r,i)=>{if(r.kind!=='user')return;const d=f.w.document.createElement('div');
      d.setAttribute('data-history-index',i);d.dataset.round=r.round;d.textContent='q'+r.round;chat.appendChild(d);});};
  return f;
}
async function settleCalls(f,ms=400){
  const t0=Date.now();let last=-1,stable=0;
  while(Date.now()-t0<ms){await new Promise(r=>setTimeout(r,20));
    if(f.calls.length===last){if(++stable>=3)break;}else{stable=0;last=f.calls.length;}}
  return f.calls.length;
}
function rangeReply(failBefore){
  return (id,options)=>{
    if(options&&(options.before||options.around)){
      const before=options.before||options.around+20;
      if(before===failBefore){const error=new Error('boom');error.status=500;throw error;}
      const end=Math.min(before-1,60),start=Math.max(1,end-19);
      return pageRange(start,end,62);
    }
    return page();
  };
}
async function openWithGap(f){
  const opened=await f.pages.open('a');
  f.w.App.renderRecords(opened.records);
  await new Promise(r=>setTimeout(r,60)); // 等 MutationObserver 调度的 sync 建出缺口按钮
  const scroll=f.w.document.querySelector('#scroll');
  Object.defineProperty(scroll,'scrollHeight',{get:()=>1000,configurable:true});
  Object.defineProperty(scroll,'clientHeight',{get:()=>600,configurable:true});
  scroll.getBoundingClientRect=()=>({top:0,bottom:600,left:0,right:800,width:800,height:600});
  return scroll;
}
test('松手贴顶且缺口可见时补载一页',async()=>{
  const f=setupCatchUp(rangeReply());try{
    const scroll=await openWithGap(f);
    const btn=f.w.document.querySelector('.history-page-gap');
    assert.ok(btn);btn.getBoundingClientRect=()=>({top:10,bottom:50,left:0,right:100,width:100,height:40});
    scroll.scrollTop=0;
    f.w.dispatchEvent(new f.w.MouseEvent('mouseup',{bubbles:true}));
    assert.equal(await settleCalls(f),2); // 初载 + 补载 before=61
    assert.equal(f.calls[1].options.before,61);
    assert.equal(f.pages.hasRound(41),true);assert.equal(f.pages.hasRound(1),false);
  }finally{f.dom.window.close();}
});
test('松手贴顶级联补载直到第一轮后停止',async()=>{
  const f=setupCatchUp(rangeReply());try{
    const scroll=await openWithGap(f);
    f.w.HTMLElement.prototype.getBoundingClientRect=function(){
      if(this.id==='scroll')return {top:0,bottom:600,left:0,right:800,width:800,height:600};
      if(this.classList&&this.classList.contains('history-page-gap'))return {top:10,bottom:50,left:0,right:100,width:100,height:40};
      return {top:0,bottom:0,left:0,right:0,width:0,height:0};
    };
    scroll.scrollTop=0;
    f.w.dispatchEvent(new f.w.MouseEvent('mouseup',{bubbles:true}));
    assert.equal(await settleCalls(f),4); // 初载 + before=61/41/21 三页
    assert.deepEqual(f.calls.slice(1).map(c=>c.options.before),[61,41,21]);
    assert.equal(f.pages.hasRound(1),true);
  }finally{f.dom.window.close();}
});
test('松手不在边缘时不补载',async()=>{
  const f=setupCatchUp(rangeReply());try{
    const scroll=await openWithGap(f);
    const btn=f.w.document.querySelector('.history-page-gap');
    btn.getBoundingClientRect=()=>({top:10,bottom:50,left:0,right:100,width:100,height:40});
    scroll.scrollTop=300; // 上下都不贴边
    f.w.dispatchEvent(new f.w.MouseEvent('mouseup',{bubbles:true}));
    assert.equal(await settleCalls(f),1);
  }finally{f.dom.window.close();}
});
test('补载失败后停止接力不再空转',async()=>{
  const f=setupCatchUp(rangeReply(41));try{
    const scroll=await openWithGap(f);
    f.w.HTMLElement.prototype.getBoundingClientRect=function(){
      if(this.id==='scroll')return {top:0,bottom:600,left:0,right:800,width:800,height:600};
      if(this.classList&&this.classList.contains('history-page-gap'))return {top:10,bottom:50,left:0,right:100,width:100,height:40};
      return {top:0,bottom:0,left:0,right:0,width:0,height:0};
    };
    scroll.scrollTop=0;
    f.w.dispatchEvent(new f.w.MouseEvent('mouseup',{bubbles:true}));
    assert.equal(await settleCalls(f),3); // 初载 + 成功一页 + 失败一页，随后停止
    assert.equal(f.pages.hasRound(1),false);
  }finally{f.dom.window.close();}
});
test('到货零进展时停止接力不再空转',async()=>{
  const f=setupCatchUp(()=>page());try{ // before 查询也只返回已加载的 61-62
    const scroll=await openWithGap(f);
    f.w.HTMLElement.prototype.getBoundingClientRect=function(){
      if(this.id==='scroll')return {top:0,bottom:600,left:0,right:800,width:800,height:600};
      if(this.classList&&this.classList.contains('history-page-gap'))return {top:10,bottom:50,left:0,right:100,width:100,height:40};
      return {top:0,bottom:0,left:0,right:0,width:0,height:0};
    };
    scroll.scrollTop=0;
    f.w.dispatchEvent(new f.w.MouseEvent('mouseup',{bubbles:true}));
    assert.equal(await settleCalls(f),2); // 初载 + 一次零进展，随后停止
  }finally{f.dom.window.close();}
});
