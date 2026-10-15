const test = require('node:test');
const assert = require('node:assert/strict');
const { create } = require('../js/tool_approval_state.js');
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
const approval = (id = 'a') => ({ id, session_id: 's', state: 'pending' });

test('审批请求共享锁：顶部和内部同时点击只提交一次，完成后旧 ID 不复活', async () => {
  const response = deferred(); let calls = 0;
  const state = create({ decideApproval: () => { calls++; return response.promise; }, pendingApprovals: async () => ({ approvals: [approval()] }) });
  state.apply({ phase: 'requested', approval: approval() });
  const first = state.decide('a', 'approve'), second = state.decide('a', 'deny');
  assert.equal(first, second); assert.equal(state.busy('a'), true);
  response.resolve({ approval: approval('new-generation') }); await first;
  assert.equal(calls, 1); assert.equal(state.busy('a'), false);
  await state.refresh('s');
  state.apply({ phase: 'requested', approval: approval() });
  assert.equal(state.items('s').length, 0);
});

test('网络异常保留审批并解锁，用户可以重试', async () => {
  let calls = 0;
  const state = create({ decideApproval: async () => { if (!calls++) throw new Error('断线'); return {}; } });
  state.apply({ phase: 'requested', approval: approval() });
  await assert.rejects(state.decide('a', 'approve'), /断线/);
  assert.equal(state.items('s').length, 1); assert.equal(state.busy('a'), false);
  await state.decide('a', 'deny'); assert.equal(state.items('s').length, 0);
});

test('恢复任务换审批代次期间，新旧按钮仍共享同一次提交锁', async () => {
  const response = deferred(); let calls = 0;
  const state = create({ decideApproval: () => { calls++; return response.promise; } });
  const old = { ...approval(), task_id: 'task', agent_id: 'child', tool_call_id: 'call' };
  state.apply({ phase: 'requested', approval: old });
  const request = state.decide(old.id, 'approve');
  const next = { ...old, id: 'new', epoch: 'next' };
  state.apply({ phase: 'requested', approval: next });
  assert.equal(state.busy(next.id), true); assert.equal(state.decide(next.id, 'deny'), request);
  response.resolve({ approval: next }); await request;
  assert.equal(calls, 1); assert.equal(state.items('s').length, 0);
});

test('旧查询响应不能覆盖较新的 SSE 请求或决定，也不清空其他会话', async () => {
  const response = deferred();
  const state = create({ pendingApprovals: () => response.promise });
  const request = state.refresh('s');
  state.apply({ phase: 'requested', approval: approval('new') });
  state.apply({ phase: 'requested', approval: { ...approval('other'), session_id: 'another' } });
  response.resolve({ approvals: [approval('old')] });
  assert.equal(await request, null);
  assert.deepEqual(state.items('s').map(a => a.id), ['new']);
  assert.equal(state.items('another').length, 1);
});

test('历史审批只登记 ID，后端已取消时不恢复按钮，并保留明确终态', async () => {
  const old = { ...approval(), task_id: 'task', tool_call_id: 'call', agent_id: 'main' };
  let queried;
  const state = create({ pendingApprovals: async (sid, ids) => {
    queried = ids;
    return { approvals: [], states: [{ ...old, state: 'cancelled', task_state: 'cancelled', execution_state: null }] };
  } });
  state.track(old);
  assert.equal(state.items('s').length, 0);
  await state.refresh('s');
  assert.deepEqual(queried, ['a']);
  assert.equal(state.items('s').length, 0);
  assert.equal(state.states('s')[0].state, 'cancelled');
});

test('提交成功后保留执行状态，旧查询不能把已决定审批复活', async () => {
  const old = approval();
  const state = create({ decideApproval: async () => ({ approval: { ...old, state: 'approved' } }),
    pendingApprovals: async () => ({ approvals: [], states: [{ ...old, state: 'completed' }] }) });
  state.apply({ phase: 'requested', approval: old });
  await state.decide(old.id, 'approve');
  assert.equal(state.states('s')[0].state, 'approved');
  await state.refresh('s');
  assert.equal(state.states('s')[0].state, 'completed');
  assert.equal(state.items('s').length, 0);
});

test('已提交但尚未执行的审批在后端挂起后可以再次确认恢复', async () => {
  const old = approval();
  const state = create({ decideApproval: async () => ({ approval: { ...old, state: 'approved' } }),
    pendingApprovals: async () => ({ approvals: [{ ...old, state: 'approved', task_state: 'suspended' }] }) });
  state.apply({ phase: 'requested', approval: old });
  await state.decide(old.id, 'approve');
  assert.equal(state.items('s').length, 0);
  await state.refresh('s');
  assert.equal(state.items('s')[0].task_state, 'suspended');
});

test('同批不同调用可同时提交相反决定，审批锁和返回状态互不串用', async () => {
  const replies = { a: deferred(), b: deferred() }, calls = [];
  const state = create({ decideApproval: async (a, decision) => {
    calls.push([a.tool_call_id, decision]); return replies[a.id].promise;
  } });
  for (const id of ['a', 'b']) state.apply({ phase: 'requested', approval: {
    ...approval(id), task_id: 'same-task', agent_id: 'main', tool_call_id: id,
  } });
  const allowed = state.decide('a', 'approve'), denied = state.decide('b', 'deny');
  await Promise.resolve();
  assert.notEqual(allowed, denied); assert.equal(state.busy('a'), true); assert.equal(state.busy('b'), true);
  replies.b.resolve({ approval: { ...approval('b'), state: 'denied' } }); await denied;
  assert.equal(state.busy('a'), true); assert.equal(state.busy('b'), false);
  replies.a.resolve({ approval: { ...approval('a'), state: 'approved' } }); await allowed;
  assert.deepEqual(calls, [['a', 'approve'], ['b', 'deny']]);
  assert.equal(state.items('s').length, 0);
  assert.equal(state.states('s').find(a => a.id === 'a').state, 'approved');
  assert.equal(state.states('s').find(a => a.id === 'b').state, 'denied');
});
