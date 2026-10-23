const test=require('node:test');const assert=require('node:assert/strict');
const fs=require('node:fs');const path=require('node:path');const vm=require('node:vm');
function setup(fail,other){
  const calls={stops:[],aborts:0,states:[],toasts:[]};
  const state={sessionId:'visible',activeStream:{sessionId:other?'other':'visible'},abort:{abort(){calls.aborts++;}}};
  const source=fs.readFileSync(path.join(__dirname,'../js/app/chat.js'),'utf8');
  const start=source.indexOf('  stopBtn.addEventListener('),end=source.indexOf('\n  });',start)+6;
  let handler;
  const context={state,stopBtn:{addEventListener(name,fn){handler=fn;}},toast:m=>calls.toasts.push(m),
    App:{refreshComposerButtons(){},taskStatus:{stopping:s=>calls.states.push(['stopping',s]),stopped:s=>calls.states.push(['stopped',s]),stopFailed:s=>calls.states.push(['failed',s])}},
    API:{async stopChat(sid){calls.stops.push(sid);if(fail)throw new Error('offline');}}};
  vm.runInNewContext(source.slice(start,end),context);return {calls,state,context,run:()=>handler()};
}
test('停止请求失败时保持监听，明确提示失败并可重试',async()=>{
  const x=setup(true,false);await x.run();assert.equal(x.calls.aborts,0);assert.equal(x.state.suppressFlushOnce,false);
  assert.deepEqual(x.calls.states,[['stopping','visible'],['failed','visible']]);assert.match(x.calls.toasts[0],/停止失败/);
});
test('当前会话的停止按钮不停止或中断另一个会话的监听',async()=>{
  const x=setup(false,true);await x.run();assert.deepEqual(x.calls.stops,['visible']);assert.equal(x.calls.aborts,0);
  assert.deepEqual(x.calls.states,[['stopping','visible'],['stopped','visible']]);
});
test('后端确认停止后才中断对应的本地连接',async()=>{
  const x=setup(false,false);await x.run();assert.equal(x.calls.aborts,1);assert.deepEqual(x.calls.states.at(-1),['stopped','visible']);
});
test('停止请求尚在处理时，重复点击不再次请求',async()=>{
  const x=setup(false,false);x.context.App.taskStatus.isStopping=()=>true;
  await x.run();assert.deepEqual(x.calls.stops,[]);assert.equal(x.calls.aborts,0);
});
