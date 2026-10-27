const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),{JSDOM}=require('jsdom');
const messages=fs.readFileSync(path.join(__dirname,'../js/app/messages.js'),'utf8');
function setup(t){
  const dom=new JSDOM('<div id="chat"></div>',{runScripts:'outside-only',url:'http://test'});t.after(()=>dom.window.close());
  const w=dom.window,root=w.document.querySelector('#chat');
  const el=(tag,cls,text)=>{const n=w.document.createElement(tag);n.className=cls||'';n.textContent=text||'';return n;};
  const App=w.App={state:{sessionId:'s',workDir:'/project'},chatInner:root,el,toast(){},stickToBottom(){},rebuildQnav(){},
    isBuiltinToolName:()=>true,toolDisplayName:()=>'x',toolIconSvg:(name,cls)=>'<svg class="'+cls+'"></svg>',
    clearInjectedPending(){},scheduleContextTokenStatsRefresh(){},highlightCodeBlocks(){},updateCodeblockCopyButtons(){}};
  Object.assign(w,{el,state:App.state,toast(){},setStageTimestamp(){},loadOlderState:{records:[]},
    FormatUtils:{prettyJson:JSON.stringify,fmtNum:String,fmtTime:String,roundSummaryText:()=>''},
    Markdown:{render:()=>''},renderPreservingWidgets(){}});
  const thinkStart=messages.indexOf('  function buildThinkBlock(');
  const toolStart=messages.indexOf('  function buildToolBlock(');
  w.eval(messages.slice(thinkStart,toolStart)+'\nObject.assign(App,{buildThinkBlock});');
  const end=messages.indexOf('  // 代码块复制（事件委托）',messages.indexOf('  function buildSubAgentBlock('));
  w.eval(messages.slice(toolStart,end)+'\nObject.assign(App,{buildToolBlock});');
  return {w,App,root};
}

test('工具块展开态按 tool_call_id 持久化，重建后恢复',t=>{
  const {App}=setup(t);
  const ui=App.buildToolBlock('search_files',{sessionId:'s',toolCallId:'call-1',round:2});
  assert.equal(ui.wrap.classList.contains('open'),false);
  ui.wrap.querySelector('.tool-block-head').click();
  assert.equal(ui.wrap.classList.contains('open'),true);
  const rebuilt=App.buildToolBlock('search_files',{sessionId:'s',toolCallId:'call-1',round:2});
  assert.equal(rebuilt.wrap.classList.contains('open'),true,'same call id restores open');
  rebuilt.wrap.querySelector('.tool-block-head').click();
  const closed=App.buildToolBlock('search_files',{sessionId:'s',toolCallId:'call-1',round:2});
  assert.equal(closed.wrap.classList.contains('open'),false,'explicit close clears choice');
  const other=App.buildToolBlock('search_files',{sessionId:'s',toolCallId:'call-2',round:2});
  assert.equal(other.wrap.classList.contains('open'),false,'other calls unaffected');
});

test('无调用 ID 的旧工具记录回退会话+轮次+下标键',t=>{
  const {App}=setup(t);
  const ui=App.buildToolBlock('search_files',{sessionId:'s',round:2,historyIndex:7});
  ui.wrap.querySelector('.tool-block-head').click();
  const rebuilt=App.buildToolBlock('search_files',{sessionId:'s',round:2,historyIndex:7});
  assert.equal(rebuilt.wrap.classList.contains('open'),true);
  const live=App.buildToolBlock('search_files',{sessionId:'s'});
  live.wrap.querySelector('.tool-block-head').click();
  const live2=App.buildToolBlock('search_files',{sessionId:'s'});
  assert.equal(live2.wrap.classList.contains('open'),false,'unstable live identity is not recorded');
});

test('思考块展开态按会话+轮次+事件持久化',t=>{
  const {App}=setup(t);
  const a=App.buildThinkBlock({sessionId:'s',round:3,sourceEvent:4,historyIndex:9});
  a.wrap.querySelector('.think-toggle').click();
  const b=App.buildThinkBlock({sessionId:'s',round:3,sourceEvent:4,historyIndex:9});
  assert.equal(b.wrap.classList.contains('open'),true);
  const c=App.buildThinkBlock({sessionId:'s',round:3,sourceEvent:5,historyIndex:10});
  assert.equal(c.wrap.classList.contains('open'),false);
  const live=App.buildThinkBlock();
  live.wrap.querySelector('.think-toggle').click();
  const live2=App.buildThinkBlock();
  assert.equal(live2.wrap.classList.contains('open'),false);
});
