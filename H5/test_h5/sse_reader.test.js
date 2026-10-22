const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function api(stream) {
  const window = {}, storage = {getItem:()=>null,setItem(){},removeItem(){}};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../js/api.js'),'utf8'), {
    window,localStorage:storage,sessionStorage:storage,URLSearchParams,TextDecoder,
    location:{origin:'http://test.local',protocol:'http:',pathname:'/ui/',search:'',hash:''},
    fetch:async()=>({ok:true,body:stream}),
  });
  return window.API;
}

test('实时流跨字节分包、无效 JSON 和未换行的末帧均不丢失有效内容', async () => {
  const bytes = new TextEncoder().encode(': ping\r\ndata: {bad}\n\ndata: {"content":"继续处理😀"}\n\ndata: {"content":"末帧"}');
  const stream = new ReadableStream({start(controller){
    for(let i=0;i<bytes.length;i++)controller.enqueue(bytes.slice(i,i+1));
    controller.close();
  }}), events=[];
  await api(stream).chatStream({messages:[]},event=>events.push(event));
  assert.deepEqual(events.map(e=>e.type==='done'?'done':e.data.content),['继续处理😀','末帧','done']);
  assert.equal(stream.locked,false);
});

test('页面渲染异常向聊天层抛出并关闭读取器，不能伪装成无效 JSON 被忽略', async () => {
  let cancelled=false;
  const stream = new ReadableStream({
    start(controller){controller.enqueue(new TextEncoder().encode('data: {"content":"续跑输出"}\n\n'));},
    cancel(){cancelled=true;},
  });
  const failure=new Error('render failed');
  await assert.rejects(api(stream).chatStream({},()=>{throw failure;}),error=>error===failure);
  assert.equal(cancelled,true);
  assert.equal(stream.locked,false);
});

test('[DONE] 只派发一次且释放仍保持连接的流', async () => {
  let cancelled=false;
  const stream=new ReadableStream({
    start(controller){controller.enqueue(new TextEncoder().encode('data: [DONE]\n\n'));},
    cancel(){cancelled=true;},
  }), events=[];
  await api(stream).chatStream({},event=>events.push(event.type));
  assert.deepEqual(events,['done']);
  assert.equal(cancelled,true);
  assert.equal(stream.locked,false);
});
