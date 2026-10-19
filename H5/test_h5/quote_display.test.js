const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { JSDOM } = require('jsdom');

function setup() {
  const dom = new JSDOM('<div id="chat"></div>');
  const source = fs.readFileSync(path.join(__dirname, '../js/app/messages.js'), 'utf8');
  const context = {state:{sessionId:'s'}, loadOlderState:{records:[]}, qnavEntries:[],
    chatInner:dom.window.document.querySelector('#chat'), contentToPlainText:s=>s};
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('  function quoteMatchText('),
    source.indexOf('  function normalizedTextOffsets(')), context);
  return { context, document:dom.window.document };
}

test('引用显示使用完整提问序号，定位仍保留真实后端轮次与事件号', () => {
  const {context:c} = setup();
  c.loadOlderState.records = [
    {kind:'user',round:1,content:'问题1'}, {kind:'assistant',round:1,content:'回答1',source_event_index:1},
    {kind:'user',round:1,content:'追加引导'}, {kind:'assistant',round:1,content:'引导回答',source_event_index:3},
    {kind:'user',round:2,content:'问题2'}, {kind:'assistant',round:2,content:'目标回答',source_event_index:5}];
  const locator = {session_id:'s',role:'assistant',round:2,event_index:5};
  const display = c.quoteSourceDisplay(locator,'目标回答');
  assert.equal(display.round,3);
  assert.equal(display.event_index,5);
  assert.equal(locator.round,2);
  assert.equal(c.quoteSourceDisplay({role:'assistant',round:1,event_index:1},'回答1').round,1);
  assert.equal(c.quoteSourceDisplay({role:'assistant',round:1,event_index:3},'引导回答').round,2);
  assert.equal(c.quoteSourceDisplay({...locator,session_id:'other'},'目标回答').round,2);
});

test('实时回答按所属提问在导航中的位置显示，不把后面的提问算入', () => {
  const {context:c,document:d} = setup();
  c.chatInner.innerHTML='<div class="msg-user">问题</div><div class="msg-assistant-body" data-round="2">实时回答</div><div class="msg-user">后面的提问</div>';
  const users=c.chatInner.querySelectorAll('.msg-user');
  c.qnavEntries=[{node:null},{node:users[0]},{node:users[1]}];
  assert.equal(c.quoteSourceDisplay({role:'assistant',round:2},'实时回答').round,2);
});

test('引用浮层按聊天区左右边界限位，并根据可用空间缩窄', () => {
  const source=fs.readFileSync(path.join(__dirname,'../js/app/quotes.js'),'utf8');
  const {document:d}=setup();
  const tile=d.createElement('div'), popover=d.createElement('div');
  let tileLeft=330, width=360;
  tile.getBoundingClientRect=()=>({left:tileLeft,right:tileLeft+40,top:200,bottom:240});
  Object.defineProperty(popover,'offsetLeft',{get:()=>40-Math.min(width,parseFloat(popover.style.maxWidth)||width)});
  popover.getBoundingClientRect=()=>{
    const w=Math.min(width,parseFloat(popover.style.maxWidth)||width);
    const left=tileLeft+(popover.style.left ? parseFloat(popover.style.left) : 40-w);
    return {left,right:left+w,width:w,height:150};
  };
  let scroll={left:300,right:900,top:0,bottom:800};
  const c={window:{innerWidth:1000,innerHeight:800},chatScroll:{getBoundingClientRect:()=>scroll}};
  vm.createContext(c);
  vm.runInContext(source.slice(source.indexOf('  function clampQuotePopover('),source.indexOf('  /** 构造用户气泡')),c);
  c.clampQuotePopover(tile,popover);
  assert.equal(popover.getBoundingClientRect().left,308);
  // 侧栏进一步拖宽：浮层宽度不能继续按整个视口计算。
  scroll={left:700,right:900,top:0,bottom:800};tileLeft=730;
  c.clampQuotePopover(tile,popover);
  const rect=popover.getBoundingClientRect();
  assert.equal(rect.width,184);
  assert.ok(rect.left>=708 && rect.right<=892);
});
