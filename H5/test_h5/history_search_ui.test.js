const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');

function setup(){
  const nodes=new Map();let render;
  function node(cls='',text=''){
    return {className:cls,textContent:text,children:[],dataset:{},handlers:{},value:'',
      classList:{add(){},remove(){}},setAttribute(){},focus(){},select(){},
      addEventListener(k,fn){this.handlers[k]=fn;},appendChild(child){this.children.push(child);},
      replaceChildren(){this.children=[];},querySelectorAll(){return[];},
      closest(){return this.dataset.snippetIndex!=null?this:null;}};
  }
  const $=id=>{if(!nodes.has(id))nodes.set(id,node());return nodes.get(id);};
  const jumps=[],scopes=[];
  const App={$: $,el:(tag,cls,text)=>node(cls,text),state:{sessionId:'s'},
    openSession:async sid=>{App.state.sessionId=sid;return true;},
    jumpToHistoryMatch:async(match,query,current)=>jumps.push({match,query,current:current()})};
  const context={window:{App},document:{createTextNode:text=>node('',text)},API:{searchHistory(){}},
    HistorySearch:{createController:(search,cb)=>{render=cb;return {close(){},open(){},setScope:value=>scopes.push(value.archiveScope)};}}};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../js/app/history_search.js'),'utf8'),context);
  return {App,$,jumps,render,scopes};
}
const hit=(sid,title)=>({session_id:sid,title,filename:sid+'_chat.jsonl',round_no:3,event_index:4,
  role:'assistant',field:'content',timestamp:'2026-10-17',snippets:[{text:'前面命中',start:2,end:4},{text:'后面命中',start:2,end:4}]});

test('every result keeps its own title and filename, including consecutive hits in the same session',()=>{
  const x=setup();
  x.render({status:'ready',query:'命中',total:3,session_count:2,results:[hit('a','标题 A'),hit('a','标题 A'),hit('b','标题 B')]});
  const rows=x.$('#historySearchResults').children;
  assert.equal(rows.length,3);
  assert.deepEqual(rows.map(row=>row.children.find(n=>n.className==='history-search-hit-title').textContent),['标题 A','标题 A','标题 B']);
  assert.deepEqual(rows.map(row=>row.children.find(n=>n.className==='history-search-filename').textContent),['a_chat.jsonl','a_chat.jsonl','b_chat.jsonl']);
});

test('clicking an excerpt waits for the session to open, then locates that excerpt',async()=>{
  const x=setup();let release;
  x.App.openSession=sid=>new Promise(resolve=>{release=()=>{x.App.state.sessionId=sid;resolve(true);};});
  x.render({status:'ready',query:'命中',results:[hit('a','标题 A')]});
  const row=x.$('#historySearchResults').children[0];
  const excerpts=row.children.filter(n=>n.className==='history-search-excerpt');
  row.handlers.click({target:excerpts[1]});
  assert.equal(x.jumps.length,0);release();await new Promise(resolve=>setImmediate(resolve));
  assert.equal(x.jumps.length,1);assert.equal(x.jumps[0].match.snippets[0].text,'后面命中');
  assert.equal(x.jumps[0].query,'命中');assert.equal(x.jumps[0].current,true);
});

test('normal search reopens in active scope after using the archive content search entry',()=>{
  const x=setup();
  x.App.historySearch.open({archiveScope:'archived',query:'归档内容'});
  assert.equal(x.$('#historySearchArchiveScope').value,'archived');
  x.App.historySearch.close();x.App.historySearch.open();
  assert.equal(x.$('#historySearchArchiveScope').value,'active');
  assert.deepEqual(x.scopes,['archived','active']);
  x.render({status:'idle',results:[]});assert.match(x.$('#historySearchStatus').textContent,/未归档会话/);
});
