/* Read complete rounds on demand. Disk history/export/model context stay intact. */
(function (App) {
  "use strict";
  const {state,chatInner,chatScroll}=App;
  let model=null, generation=0, loading=null, observer=null, frame=null, dragging=false;
  const buttons=new Map(), failed=new Set();
  function supported() { return !!(window.YtoolsAuth && YtoolsAuth.historyPagingAllowed && API.fetchSessionPage); }
  function reset() {
    generation++;
    if(model && model.abort)model.abort.abort();
    model=null;loading=null;failed.clear();
    if(observer)observer.disconnect();observer=null;
    buttons.forEach(button=>button.remove());buttons.clear();
    if(frame!=null)cancelAnimationFrame(frame);frame=null;
  }
  function current(saved) { return model===saved && state.sessionId===saved.id; }
  async function parse(page) {
    const options={roundOffset:page.round_offset||0};
    return typeof HistoryParseAsync!=="undefined" ? HistoryParseAsync.parse(page.text,options) : HistoryParser.parseHistory(page.text,options);
  }
  function merge(saved,page,parsed) {
    if(page.revision!==saved.revision)throw new Error("历史版本不一致，请重新打开会话");
    const groups=new Map();
    for(let n=page.start_round;n<=page.end_round;n++)groups.set(n,[]);
    let round=page.start_round;
    parsed.records.forEach(record=>{
      if(Number.isInteger(record.round))round=record.round;
      const key=Math.max(page.start_round,Math.min(page.end_round,round));
      if(!groups.has(key))groups.set(key,[]);
      groups.get(key).push(record);
    });
    groups.forEach((records,number)=>{if(!saved.rounds.has(number))saved.rounds.set(number,records);});
    if(page.questions)saved.questions=page.questions;
    saved.total=page.total_rounds;
  }
  function records(saved) {
    return Array.from(saved.rounds).sort((a,b)=>a[0]-b[0]).flatMap(entry=>entry[1]);
  }
  async function open(id,options) {
    reset();
    if(!supported())return null;
    const ticket=generation,abort=new AbortController();
    let page;
    for(let attempt=0;attempt<2;attempt++) {
      try {page=await API.fetchSessionPage(id,{signal:abort.signal});break;}
      catch(error){if(error.status!==409||attempt)throw error;}
    }
    if(ticket!==generation||state.sessionId!==id)return {records:[],stale:true};
    if(page.fallback_full)return null;
    const saved={id,abort,revision:page.revision,rounds:new Map(),questions:[],total:page.total_rounds};
    model=saved;
    const parsed=await parse(page);
    if(!current(saved))return {records:[],stale:true};
    merge(saved,page,parsed);
    const target=options && options.historyMatch && Number(options.historyMatch.round_no);
    if(target>0 && !saved.rounds.has(target)) {
      const selected=await API.fetchSessionPage(id,{around:target,revision:saved.revision,include_questions:false,signal:abort.signal});
      if(selected.fallback_full){reset();return null;}
      const part=await parse(selected);
      if(!current(saved))return {records:[],stale:true};
      merge(saved,selected,part);
    }
    return Object.assign({},parsed,{records:records(saved)});
  }
  function anchor() {
    const view=App.getHistoryWindow(),top=chatScroll.getBoundingClientRect().top;
    const node=Array.from(chatInner.children).find(node=>node.hasAttribute("data-history-index") && node.getBoundingClientRect().bottom>top);
    return node && {record:view.records[Number(node.dataset.historyIndex)],y:node.getBoundingClientRect().top};
  }
  function repaint(saved,targetRound) {
    const savedAnchor=anchor(),all=records(saved);
    const live=Array.from(chatInner.children).filter(node=>!node.hasAttribute("data-history-index") && !node.matches(".qnav-load-older,.process-group-summary"));
    if(App.historyScroll)App.historyScroll.beforeRender();
    const at=targetRound ? all.findIndex(r=>Number(r.round)===targetRound) : savedAnchor ? all.indexOf(savedAnchor.record) : 0;
    chatInner.replaceChildren();
    App.renderRecords(all,{historyIndex:Math.max(0,at),noScroll:true});
    live.forEach(node=>chatInner.appendChild(node));
    if(App.historyScroll)App.historyScroll.refresh({preserve:false});
    if(!targetRound && savedAnchor) {
      const index=all.indexOf(savedAnchor.record);
      const node=chatInner.querySelector('[data-history-index="'+index+'"]');
      if(node) {
        const previous=chatScroll.style.scrollBehavior;chatScroll.style.scrollBehavior="auto";
        chatScroll.scrollTop+=node.getBoundingClientRect().top-savedAnchor.y;
        chatScroll.style.scrollBehavior=previous;
      }
    }
    App.rebuildQnav();sync();
  }
  async function load(round,query,reveal) {
    const saved=model;
    if(!saved||!current(saved))return false;
    if(saved.rounds.has(round))return true;
    if(loading){await loading;return current(saved)?load(round,query,reveal):false;}
    loading=(async()=>{
      try {
        const page=await API.fetchSessionPage(saved.id,Object.assign({revision:saved.revision,include_questions:false,signal:saved.abort.signal},query||{around:round}));
        if(!current(saved))return false;
        if(page.fallback_full) {await App.openSession(saved.id,{historyMatch:{session_id:saved.id,round_no:round}});return true;}
        const parsed=await parse(page);
        if(!current(saved))return false;
        merge(saved,page,parsed);repaint(saved,reveal?round:null);return true;
      } catch(error) {
        if(!current(saved)||error.name==="AbortError")return false;
        if(error.status===409) {
          App.toast("历史已更新，正在重新加载当前版本");
          return App.openSession(saved.id,{historyMatch:{session_id:saved.id,round_no:round}});
        }
        failed.add(round);App.toast("历史加载失败，可点击重试："+error.message);return false;
      }
    })();
    try{return await loading;}finally{loading=null;schedule();}
  }
  function gaps(saved) {
    const loaded=Array.from(saved.rounds.keys()).filter(n=>n>0).sort((a,b)=>a-b),result=[];
    let next=1;
    loaded.forEach(n=>{if(n>next)result.push({start:next,end:n-1});next=n+1;});
    if(next<=saved.total)result.push({start:next,end:saved.total});
    return result;
  }
  function sync() {
    const saved=model;
    if(!saved||!current(saved))return;
    const nodes=Array.from(chatInner.children).filter(node=>node.hasAttribute("data-history-index") && node.dataset.round);
    const wanted=new Set();
    gaps(saved).forEach(gap=>{
      const right=nodes.find(node=>Number(node.dataset.round)===gap.end+1);
      const left=nodes.filter(node=>Number(node.dataset.round)===gap.start-1).pop();
      if(!right&&!left)return;
      const key=gap.start+":"+gap.end;wanted.add(key);
      let button=buttons.get(key);
      if(button&&!button.isConnected){buttons.delete(key);button=null;}
      if(!button) {
        button=App.el("button","qnav-load-older history-page-gap","加载第 "+gap.start+"–"+gap.end+" 轮历史");
        button.type="button";button.dataset.pageGap=key;
        const target=right?gap.end:gap.start;
        button.addEventListener("click",()=>{failed.delete(target);load(target,right?{before:gap.end+1}:{before:Math.min(gap.end+1,gap.start+20)},false);});
        buttons.set(key,button);
        if(right)right.before(button);else left.after(button);
        if(typeof IntersectionObserver!=="undefined") {
          if(!observer)observer=new IntersectionObserver(entries=>entries.forEach(entry=>{
            if(!entry.isIntersecting||dragging||loading||!entry.target.isConnected)return;
            const [a,b]=entry.target.dataset.pageGap.split(":").map(Number);
            if(!failed.has(a)&&!failed.has(b))entry.target.click();
          }),{root:chatScroll});
          observer.observe(button);
        }
      }
      button.disabled=!!loading;
    });
    buttons.forEach((button,key)=>{if(!wanted.has(key)){if(observer)observer.unobserve(button);button.remove();buttons.delete(key);}});
  }
  function schedule() {
    if(frame!=null)return;
    frame=requestAnimationFrame(()=>{frame=null;sync();});
  }
  function navigation(entries) {
    if(!model||!current(model))return entries;
    const original=entries.slice(),used=new Set();
    const byEvent=new Map(original.map(item=>[item.round+":"+item.eventIndex,item]));
    const all=model.questions.map(question=>{
      const entry=byEvent.get(question.round+":"+question.event_index);
      if(entry){used.add(entry);return entry;}
      return {index:-1,round:question.round,eventIndex:question.event_index,text:question.text,node:null};
    });
    original.forEach(entry=>{if(!used.has(entry))all.push(entry);});
    return all.sort((a,b)=>a.round-b.round || (a.eventIndex||0)-(b.eventIndex||0));
  }
  function deferResult(ui,record) {
    const saved=model,ref=record.history_detail,head=ui.wrap.querySelector(".tool-block-head");
    if(!saved||!head)return;
    const output=ui.wrap.querySelector(".tool-io-out");
    if(output)output.textContent="展开加载完整执行结果";
    async function reveal() {
      if(!ui.wrap.classList.contains("open")||!current(saved))return;
      try {
        if(record.history_detail) {
          if(!record.detailPending)record.detailPending=API.fetchHistoryEvent(saved.id,ref,saved.abort.signal).then(data=>{
            if(!current(saved))return;
            record.result=typeof data.event.result==="string"?data.event.result:JSON.stringify(data.event.result,null,2);
            record.file_diff=data.event.file_diff;delete record.history_detail;
          }).finally(()=>{delete record.detailPending;});
          if(output)output.textContent="正在加载执行结果…";
          await record.detailPending;
        }
        if(current(saved)&&ui.wrap.isConnected&&!record.history_detail) {
          App.applyToolResult(ui,record.name,record.result,record.file_diff,record.args);
          ui.finish();
        }
      } catch(error) {
        if(current(saved)&&error.name!=="AbortError"&&output)output.textContent=error.status===409?
          "历史已更新，请重新打开会话后查看此结果。":"加载失败，关闭后展开可重试："+error.message;
      }
    }
    head.addEventListener("click",reveal);
    if(ui.wrap.classList.contains("open"))reveal();
  }
  App.historyPages={open,reset,sync:schedule,navigation,
    hasRound:round=>!model||model.rounds.has(Number(round)),
    ensureRound:round=>load(Number(round),null,true),
    loadBefore:before=>load(Number(before)-1,{before:Number(before)},false),
    deferResult,
    active:()=>!!model};
  chatScroll.addEventListener("mousedown",event=>{if(event.target===chatScroll)dragging=true;},{passive:true});
  window.addEventListener("mouseup",()=>{dragging=false;schedule();},{passive:true});
  if(typeof MutationObserver!=="undefined")new MutationObserver(schedule).observe(chatInner,{childList:true});
})(window.App);
