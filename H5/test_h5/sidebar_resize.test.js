const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { JSDOM } = require('jsdom');
const source = fs.readFileSync(path.join(__dirname, '../js/app/core.js'), 'utf8');
function setup() {
  const dom = new JSDOM('<aside id="sidebar"></aside><div id="scrim"></div>' +
    ['sidebarResizeHandle','sidebarToggle','railExpand','railChats','menuToggle','railAvatar'].map(id=>'<button id="'+id+'"></button>').join(''), {url:'http://local.test/'});
  const {document} = dom.window, frames = new Map(); let serial=0, reads=0;
  const sidebar=document.querySelector('#sidebar');
  sidebar.getBoundingClientRect=()=>{reads++;return {left:0};};
  const context={window:dom.window,document,localStorage:dom.window.localStorage,Event:dom.window.Event,
    $:id=>document.querySelector(id),sidebar,scrim:document.querySelector('#scrim'),isMobile:()=>false,
    getComputedStyle:()=>({getPropertyValue:()=> '50px'}),App:{},
    requestAnimationFrame:fn=>{frames.set(++serial,fn);return serial;},cancelAnimationFrame:id=>frames.delete(id)};
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('  const sidebarResizeHandle'),source.indexOf('  // ---------- 会话搜索')),context);
  function fire(target,type,x){
    const event=new dom.window.Event(type,{bubbles:true});Object.assign(event,{pointerId:7,button:0,clientX:x});
    target.dispatchEvent(event);
  }
  const handle=document.querySelector('#sidebarResizeHandle');
  return {document,context,sidebar,frames,handle,reads:()=>reads,
    fire:(type,x)=>fire(type==='pointerdown'?handle:dom.window,type,x),
    flush(){const queued=[...frames.values()];frames.clear();queued.forEach(fn=>fn());},
    width:()=>document.documentElement.style.getPropertyValue('--sidebar-width')};
}
test('pointer bursts coalesce into one frame and do not measure layout on every move',()=>{
  const h=setup();h.fire('pointerdown',240);const reads=h.reads();
  for(let x=250;x<=500;x++)h.fire('pointermove',x);
  assert.equal(h.frames.size,1);assert.equal(h.reads(),reads);assert.equal(h.width(),'240px');
  h.flush();assert.equal(h.width(),'499px');assert.equal(h.reads(),reads);
});
test('continuous dragging can collapse and expand; pointerup commits the latest pending point',()=>{
  const h=setup();h.fire('pointerdown',240);h.fire('pointermove',80);h.flush();
  assert.equal(h.sidebar.classList.contains('collapsed'),true);
  h.fire('pointermove',300);h.flush();assert.equal(h.sidebar.classList.contains('collapsed'),false);
  h.fire('pointermove',400);h.fire('pointerup',400);
  assert.equal(h.width(),'399px');assert.equal(h.frames.size,0);
  assert.equal(h.context.localStorage.getItem('ytools-sidebar-width'),'399');
});
test('cancel restores initial width and collapse state without saving a pending move',()=>{
  const h=setup();h.fire('pointerdown',240);h.fire('pointermove',600);h.flush();
  h.fire('pointermove',700);h.fire('pointercancel',700);
  assert.equal(h.width(),'240px');assert.equal(h.frames.size,0);
  assert.equal(h.document.body.classList.contains('sidebar-resizing'),false);
  assert.equal(h.context.localStorage.getItem('ytools-sidebar-width'),null);
});
test('collapsed rail avatar opens settings without expanding sidebar',()=>{
  const h=setup();let opened=0;h.context.App.sidebarSettings={toggle:()=>{opened++;}};
  h.sidebar.classList.add('collapsed');
  h.document.querySelector('#railAvatar').dispatchEvent(new h.context.window.Event('click',{bubbles:true}));
  assert.equal(opened,1);
  assert.equal(h.sidebar.classList.contains('collapsed'),true);
});
