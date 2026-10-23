const test = require('node:test');
const assert = require('node:assert/strict');
const T = require('../js/task_state');
const frame = (s,data) => T.reduce(s,{type:'frame',data});
const live = () => T.reduce(T.initial(),{type:'connection',value:'live'});

test('连接、等待模型、思考、正文、完成依次变化',()=>{
  let s=T.initial(); assert.equal(T.view(s).phase,'connecting');
  s=T.reduce(s,{type:'connection',value:'live'}); assert.equal(T.view(s).phase,'model');
  s=frame(s,{reasoning_content:'计划'}); assert.equal(T.view(s).phase,'thinking');
  s=frame(s,{content:'回答'}); assert.equal(T.view(s).phase,'answering');
  s=T.reduce(s,{type:'end',completed:true}); assert.equal(T.view(s).phase,'completed');
});
test('多个工具按调用 ID 收尾，完成一个仍显示其余工具',()=>{
  let s=frame(live(),{tool_start:{tool_call_id:'a',function_name:'read_file'}});
  s=frame(s,{tool_start:{tool_call_id:'b',function_name:'run_command'}});
  assert.match(T.view(s).detail,/2/);
  s=frame(s,{tool_return:{tool_call_id:'b'}}); assert.equal(T.view(s).detail,'read_file');
  s=frame(s,{tool_return:{tool_call_id:'a'}}); assert.equal(T.view(s).phase,'model');
});
test('审批按 ID 去重，预览审批不阻塞任务，其他输出不掩盖待确认',()=>{
  const a={id:'__proto__',agent_id:'main',state:'pending'};
  let s=frame(live(),{event:'tool_approval',phase:'requested',approval:a});
  s=frame(s,{event:'tool_approval',phase:'requested',approval:a});
  s=frame(s,{content:'其他任务继续'});
  assert.equal(T.view(s).phase,'waiting_approval');assert.match(T.view(s).detail,/1 项/);
  s=frame(s,{event:'tool_approval',phase:'resolved',approval:{...a,state:'approved'}});
  assert.equal(T.view(s).phase,'answering');
  s=frame(s,{event:'tool_approval',phase:'requested',approval:{id:'p',agent_id:'preview'}});
  assert.equal(T.view(s).phase,'answering');
});
test('预算等待和继续不会被心跳或子任务输出覆盖',()=>{
  let s=frame(live(),{event:'task_usage',task:{kind:'chat',state:'paused'}});
  s=frame(s,{event:'sub_agent',phase:'delta',content:'child'});
  assert.equal(T.view(s).phase,'waiting_budget');
  s=frame(s,{event:'task_usage',task:{kind:'chat',state:'running'}});
  assert.notEqual(T.view(s).phase,'waiting_budget');
});
test('ask_user 正常结束后仍等待回复；工具失败不等于主任务失败',()=>{
  let s=frame(live(),{tool_return:{tool_call_id:'x',result:{success:false}}});
  assert.notEqual(T.view(s).phase,'failed');
  s=frame(s,{event:'ask_user',questions:[{}]});
  s=T.reduce(s,{type:'end',completed:true});assert.equal(T.view(s).phase,'waiting_user');
});
test('压缩、重试、恢复正文与致命错误分开显示',()=>{
  let s=frame(live(),{event:'context_compaction',phase:'start'});assert.equal(T.view(s).phase,'compacting');
  s=frame(s,{event:'context_compaction',phase:'done'});assert.equal(T.view(s).phase,'model');
  s=frame(s,{error:'temporary',retrying:true});assert.equal(T.view(s).phase,'retrying');
  s=frame(s,{content:'恢复'});assert.equal(T.view(s).phase,'answering');assert.equal(s.error,'');
  s=frame(s,{error:'fatal'});s=T.reduce(s,{type:'end',completed:true});assert.equal(T.view(s).phase,'failed');
});
test('裸 EOF 只能确定断线，不能推断任务完成或失败',()=>{
  const s=T.reduce(live(),{type:'end',completed:false});
  assert.equal(T.view(s).phase,'disconnected');assert.equal(s.terminal,false);assert.equal(T.view(s).reconnect,true);
});
test('服务端挂起和已结束状态不会被流收尾或迟到正文覆盖',()=>{
  for(const action of [{type:'suspended'},{type:'ended'},{type:'frame',data:{event:'task_usage',task:{kind:'chat',state:'suspended'}}}]){
    let s=T.reduce(live(),action),phase=s.phase;
    s=T.reduce(s,{type:'end',completed:true});
    s=frame(s,{content:'late'});assert.equal(T.view(s).phase,phase);assert.equal(s.terminal,true);
  }
});
test('主动停止失败恢复原阶段，确认停止后迟到输出不能改写状态',()=>{
  let s=frame(live(),{reasoning_content:'x'});
  s=T.reduce(s,{type:'stopping'});assert.equal(T.view(s).phase,'stopping');
  s=T.reduce(s,{type:'stop_failed',error:'timeout'});assert.equal(T.view(s).phase,'thinking');
  s=T.reduce(s,{type:'stopped'});s=frame(s,{content:'late'});
  s=T.reduce(s,{type:'end',completed:false});assert.equal(T.view(s).phase,'stopped');
});
test('状态机不修改传入状态，控制帧不会切断思考阶段',()=>{
  const a=frame(live(),{reasoning_content:'x'}),saved=JSON.stringify(a);
  const b=frame(a,{usage:{total_tokens:2}});
  assert.equal(b.phase,'thinking');assert.equal(JSON.stringify(a),saved);
});
test('终止性错误优先于待审批，迟到输出和停止失败不能改写终态',()=>{
  let s=frame(live(),{event:'tool_approval',phase:'requested',approval:{id:'a',state:'pending'}});
  s=frame(s,{error:'fatal',retrying:false});assert.equal(T.view(s).phase,'failed');
  s=frame(s,{content:'late'});s=T.reduce(s,{type:'stop_failed',error:'timeout'});
  assert.equal(T.view(s).phase,'failed');
});
test('等待停止确认期间仍记录执行阶段，停止失败恢复最新阶段',()=>{
  let s=frame(live(),{reasoning_content:'plan'});s=T.reduce(s,{type:'stopping'});
  s=frame(s,{tool_start:{tool_call_id:'a',function_name:'read_file'}});
  assert.equal(T.view(s).phase,'stopping');
  s=T.reduce(s,{type:'stop_failed'});assert.equal(T.view(s).phase,'tools');
  s=T.reduce(s,{type:'end',completed:true});s=T.reduce(s,{type:'stop_failed'});
  assert.equal(T.view(s).phase,'completed');
});
test('压缩明细区分本轮/跨轮/官方窗口，结束后明细清空',()=>{
  let s=frame(live(),{event:'context_compaction',phase:'start',scope:'round'});
  assert.equal(T.view(s).phase,'compacting');assert.equal(T.view(s).detail,'本轮工具轨迹');
  s=frame(s,{event:'context_compaction',phase:'done'});
  assert.equal(T.view(s).phase,'model');assert.equal(T.view(s).detail,'');
  s=frame(s,{event:'context_compaction',phase:'start',scope:'session'});
  assert.equal(T.view(s).detail,'跨轮历史');
  s=frame(s,{event:'responses_compaction',phase:'start'});
  assert.equal(T.view(s).phase,'compacting');assert.equal(T.view(s).detail,'官方压缩窗口');
  s=frame(s,{event:'responses_compaction',phase:'done'});
  assert.equal(T.view(s).phase,'model');assert.equal(T.view(s).detail,'');
  s=frame(s,{event:'responses_compaction',phase:'error',error:'boom'});
  assert.equal(T.view(s).phase,'model');
});
