const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {JSDOM} = require('jsdom');
const source = fs.readFileSync(path.join(__dirname, '../js/app/messages.js'), 'utf8');

function setup(t, count=600, stride=200) {
  const dom = new JSDOM('<div id="scroll"><div id="chat"></div></div><div id="nav"><div id="rail"></div><div id="panel"></div></div>');
  t.after(() => dom.window.close());
  const d = dom.window.document, chatInner = d.querySelector('#chat'), scroll = d.querySelector('#scroll');
  Object.defineProperties(scroll, {scrollHeight:{value:5000}, clientHeight:{value:500}});
  scroll.getBoundingClientRect = () => ({top:60,bottom:560});
  const records = Array.from({length:count}, (_,i) => ({kind:i%stride===0?'user':'assistant',round:Math.floor(i/stride)+1,content:'记录 '+i,source_event_index:i%stride}));
  const context = {chatInner,chatScroll:scroll,loadOlderState:{records,start:0,end:count},
    qnav:d.querySelector('#nav'),qnavRail:d.querySelector('#rail'),qnavPanel:d.querySelector('#panel'),QNAV_MAX_DASHES:40,
    contentToPlainText:String,el(tag,cls,text){const n=d.createElement(tag);n.className=cls;n.textContent=text||'';return n;}};
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('  let qnavUsers ='),source.indexOf('  // ---------- 从用户引用')),context);
  vm.runInContext(source.slice(source.indexOf('  function updateQnavActive()'),source.indexOf('  // ---------- 滚动：回到底部')),context);
  function mount(index,top,options={}) {
    const n=d.createElement('div');n.dataset.historyIndex=String(index);
    n.className=records[index].kind==='user'?'msg-user':'msg-assistant-body';
    if(options.hidden)n.classList.add('is-process-hidden');
    const b=d.createElement('div');b.className='msg-bubble';b.textContent=records[index].content;n.append(b);
    n.getBoundingClientRect=()=>({top,bottom:top+(options.height||150),height:options.height||150});
    chatInner.append(n);return n;
  }
  function active(){context.updateQnavActive();return [...context.qnavPanel.children].findIndex(n=>n.classList.contains('active'));}
  return {context,scroll,mount,active,chatInner,rebuild:()=>context.rebuildQnav()};
}

test('到底时最新提问不在渲染窗口，导航仍定位最后一个问题',t=>{
  const h=setup(t);h.mount(580,-100);h.mount(599,200);h.scroll.scrollTop=4500;h.rebuild();
  assert.equal(h.active(),2);
});
test('中间窗口全是长回答，按可见历史记录找到所属提问',t=>{
  const h=setup(t);h.context.loadOlderState.start=300;h.context.loadOlderState.end=380;
  h.mount(330,-500,{height:900});h.mount(331,400);h.scroll.scrollTop=2000;h.rebuild();
  assert.equal(h.active(),1);
});
test('窗口顶部的下一条提问尚未越过定位线，保持前一条问题',t=>{
  const h=setup(t);h.context.loadOlderState.start=350;h.context.loadOlderState.end=450;
  h.mount(399,-200);h.mount(400,500);h.scroll.scrollTop=2800;h.rebuild();
  assert.equal(h.active(),1);
});
test('折叠隐藏记录与窗口重建后脱离 DOM 的气泡不能覆盖当前位置',t=>{
  const h=setup(t);h.mount(0,-100);h.mount(200,-100);h.mount(400,-100);h.scroll.scrollTop=1500;h.rebuild();
  h.chatInner.replaceChildren();h.context.loadOlderState.start=300;h.context.loadOlderState.end=380;
  h.mount(330,-100);h.mount(500,0,{hidden:true});
  assert.equal(h.active(),1);
});
test('局部回退分页的底部不等于完整会话底部',t=>{
  const h=setup(t);h.context.loadOlderState.start=250;h.context.loadOlderState.end=380;
  h.mount(330,-100);h.scroll.scrollTop=4500;h.rebuild();
  assert.equal(h.active(),1);
});
test('问题超过轨道采样上限时，底部高亮最后一项和最后一段',t=>{
  const h=setup(t,10000);h.context.loadOlderState.start=9880;
  h.mount(9950,-100);h.mount(9999,200);h.scroll.scrollTop=4500;h.rebuild();
  assert.equal(h.active(),49);
  const dashes=[...h.context.qnavRail.children];assert.equal(dashes.length,40);
  assert.equal(dashes.findIndex(n=>n.classList.contains('active')),39);
});
test('display:none 的零高度历史戳不会把导航提前推向未来问题',t=>{
  const h=setup(t);h.context.loadOlderState.end=550;h.mount(330,-100);
  const time=h.mount(500,0);time.getBoundingClientRect=()=>({top:0,bottom:0,height:0});
  h.scroll.scrollTop=2000;h.rebuild();assert.equal(h.active(),1);
});
