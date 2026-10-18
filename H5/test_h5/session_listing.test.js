const test=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../js/app/sessions.js'),'utf8');
function setup(){
  const list={children:[],appendChild(n){this.children.push(n);n.parent=this;},
    querySelector(selector){return this.querySelectorAll(selector)[0]||null;},
    querySelectorAll(selector){return this.children.filter(n=>selector.split(', ').some(s=>s.startsWith('.')?n.className===s.slice(1):s==='[data-session="'+n.dataset.session+'"]'));}};
  function el(tag,cls,text){return {className:cls,dataset:{},textContent:text,remove(){list.children=list.children.filter(n=>n!==this);}};}
  const timers=new Map();let timerId=0;
  const opened=[],groupRows=[],notices=[];
  const ctx={state:{sessionId:null,sessionRecency:{}},input:{value:''},sessionOpenSeq:0,
    SessionUtils:require('../js/session_utils.js'),SessionListUtils:require('../js/session_list_utils.js'),
    sessionList:list,API:{},App:{sessionGroups:{render:rows=>groupRows.push(rows)}},
    el,toast:msg=>notices.push(msg),bulkMode:false,syncBulkSelectionUi(){},refreshSessionFades(){},
    buildSessionItem(id,title){const n=el('div','session-item',title);n.dataset.session=id;return n;},
    renderBulkGroupedView(){ctx.bulkRows=ctx.rows();},
    setTimeout(fn){const id=++timerId;timers.set(id,fn);return id;},clearTimeout(id){timers.delete(id);},
    openSession(id){opened.push(id);ctx.state.sessionId=id;return true;},startNewChat(){ctx.sessionOpenSeq++;ctx.state.sessionId=null;}};
  vm.createContext(ctx);
  vm.runInContext(source.slice(source.indexOf('  let lastSortedRows ='),source.indexOf('  // 溢出淡出标记')),ctx);
  vm.runInContext(source.slice(source.indexOf('  function renderSessionItems()'),source.indexOf('  /** 多选分组视图')),ctx);
  vm.runInContext(source.slice(source.indexOf('  async function openInitialSession('),source.indexOf('  // 视图就绪跟踪')),ctx);
  vm.runInContext('this.rows=()=>lastSortedRows;',ctx);
  return {ctx,list,opened,groupRows,notices,drain(){while(timers.size){const [id,fn]=timers.entries().next().value;timers.delete(id);fn();}}};
}
const rows=()=>Array.from({length:130},(_,i)=>({id:'s'+i,title:'标题'+i,updated_at:'2026-10-17 12:00:00',created_at:'',group_id:i%2?'g':null}));

test('one batch request replaces N meta requests; first 50 render immediately and all data reaches groups/bulk mode',async()=>{
  const x=setup();let calls=0;
  x.ctx.API.listSessionSummaries=async()=>{calls++;return rows();};
  x.ctx.API.getSessionMeta=()=>{throw Error('must not request individual metadata');};
  await x.ctx.loadSessions();assert.equal(calls,1);
  assert.equal(x.list.children.length,50);assert.equal(x.groupRows.at(-1).length,130);
  x.ctx.bulkMode=true;x.ctx.renderSessionItems();x.drain();
  assert.equal(x.ctx.bulkRows.length,130);assert.equal(x.list.children.length,0);
  x.ctx.bulkMode=false;x.ctx.renderSessionItems();x.drain();
  assert.equal(x.list.children.length,130);
});

test('refresh failure retains old rows and late loads cannot overwrite the newer list',async()=>{
  const x=setup();const pending=[];
  x.ctx.API.listSessionSummaries=()=>new Promise((resolve,reject)=>pending.push({resolve,reject}));
  const old=x.ctx.loadSessions(),newer=x.ctx.loadSessions();
  pending[1].resolve([{id:'new',title:'新标题'}]);await newer;
  pending[0].resolve([{id:'old',title:'旧标题'}]);await old;
  assert.equal(x.list.children[0].dataset.session,'new');
  const failed=x.ctx.loadSessions();pending[2].reject(Error('network'));await failed;
  assert.equal(x.list.children[0].dataset.session,'new');assert.equal(x.notices.length,1);
});

test('initial navigation is independent of the slow list and protects user navigation and drafts',async()=>{
  const x=setup();let releaseList,releaseTarget;
  x.ctx.API.listSessionSummaries=id=>new Promise(resolve=>{if(id)releaseTarget=resolve;else releaseList=resolve;});
  const list=x.ctx.loadSessions(),initial=x.ctx.openInitialSession('s100');
  releaseTarget([{id:'s100'}]);await initial;
  assert.deepEqual(x.opened,['s100']);assert.equal(x.list.children[0].className,'empty-tip');
  releaseList(rows());await list;x.drain();assert.equal(x.list.children.length,130);
  const late=x.ctx.openInitialSession('other');x.ctx.sessionOpenSeq++;
  releaseTarget([{id:'other'}]);await late;assert.deepEqual(x.opened,['s100']);
  const typing=x.ctx.openInitialSession('other');x.ctx.input.value='用户正在输入';
  releaseTarget([{id:'other'}]);await typing;assert.deepEqual(x.opened,['s100']);
});

test('missing/failed startup lookup does not open/create a phantom session; old servers avoid N meta calls',async()=>{
  const x=setup();x.ctx.API.listSessionSummaries=async()=>[];
  await x.ctx.openInitialSession('missing');assert.equal(x.opened.length,0);
  x.ctx.API.listSessionSummaries=async()=>{throw Error('network');};
  await x.ctx.openInitialSession('missing');assert.equal(x.opened.length,0);
  x.ctx.API.listSessionSummaries=async()=>{throw Object.assign(Error('old server'),{status:404});};
  x.ctx.API.listSessions=async()=>['old_chat.jsonl'];
  x.ctx.API.getSessionMeta=()=>{throw Error('N+1 fallback forbidden');};
  await x.ctx.loadSessions();assert.equal(x.list.children[0].dataset.session,'old');
});

test('a rename during list loading wins over a stale server title',async()=>{
  const x=setup();let release;
  x.ctx.API.listSessionSummaries=()=>new Promise(resolve=>release=resolve);
  const pending=x.ctx.loadSessions();x.ctx.rememberSessionTitle('renamed','新标题');
  release([{id:'renamed',title:'旧标题'}]);await pending;
  assert.equal(x.list.children[0].textContent,'新标题');
});
