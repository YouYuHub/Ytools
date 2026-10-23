const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
class Node {
  constructor(kind) { this.kind = kind; this.children = []; this.classList = { remove() {} }; }
  appendChild(n) { if (n.parentNode) n.parentNode.removeChild(n); this.children.push(n); n.parentNode = this; }
  removeChild(n) { this.children.splice(this.children.indexOf(n), 1); n.parentNode = null; }
  replaceChild(n, old) { const i = this.children.indexOf(old); this.children[i] = n; n.parentNode = this; old.parentNode = null; }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
}
function pipeline() {
  const source = fs.readFileSync(path.join(__dirname, '../js/app/stream_pipeline.js'), 'utf8');
  const start = source.indexOf('  function createStreamPipeline(');
  const end = source.indexOf('\n  App.createStreamPipeline =', start);
  const functionEnd = source.lastIndexOf('\n  }', end) + 4;
  const cards = [];
  const tools = [];
  const terminalNotices = [];
  let navRefreshes = 0;
  const context = {
    state: { sessionId: 'session' }, stickToBottom() {}, pickTitlePreview() { return ''; },
    el(kind) { return new Node(kind); },
    FormatUtils: { prettyJson: JSON.stringify },
    FILE_BADGE_TOOLS: [],
    App: {
      clearInjectedPending() {},
      appendUserMessage(text, ts, sid, docs, container) { container.appendChild(new Node('user')); },
      rebuildQnav() { navRefreshes += 1; },
      buildToolBlock() {
        const tool = { wrap: new Node('tool'), setName() {}, beginStream() {}, addInputDelta() {}, setInputText() {}, setInput() {}, finish() { this.finished = true; },
          executing() { this.started = true; }, bindPermission(identity) { this.identity = identity; } };
        tools.push(tool); return tool;
      },
      applyToolResult(ui, name, result) { ui.result = result; },
      permissions: { receive() {} },
      terminalJobs: { noteToolResult(sid, name) { terminalNotices.push([sid, name]); } },
      buildSubAgentBlock(sid) {
        const card = { wrap: new Node('agent'), sid, input: '', events: [], done: false,
          setDispatchInput(value) { this.input = value; },
          applyEvent(evt) { this.events.push(evt); if (evt.phase === 'done') this.done = true; },
          markReturned() { this.returned = true; }, isDone() { return this.done; },
          finalizeInterrupted() { this.done = true; },
          applyApproval(approval) { this.approval = approval; },
        };
        cards.push(card); return card;
      },
      scheduleContextTokenStatsRefresh() {},
    },
  };
  vm.createContext(context);
  vm.runInContext(source.slice(start, functionEnd), context);
  const root = new Node('root');
  const pipe = context.createStreamPipeline(root, 'session', {});
  return { root, cards, tools, terminalNotices, navRefreshes: () => navRefreshes, send: data => pipe.handle({ data }) };
}

test('同名工具的审批、启动和逆序结果按调用 ID 配对，不重复建块', () => {
  const { root, tools, send } = pipeline();
  send({ tool_calls: [
    { index: 0, id: 'a', function: { name: 'run_command', arguments: '{}' } },
    { index: 1, id: 'b', function: { name: 'run_command', arguments: '{}' } },
  ] });
  for (const id of ['a', 'b']) send({ event: 'tool_approval', phase: 'requested', approval: { id: 'approval-' + id, agent_id: 'main', tool_call_id: id, tool: 'run_command', round: 1 } });
  send({ tool_start: { function_name: 'run_command', tool_call_id: 'b' } });
  send({ tool_return: { function_name: 'run_command', tool_call_id: 'b', result: 'B' } });
  send({ tool_start: { function_name: 'run_command', tool_call_id: 'a' } });
  send({ tool_return: { function_name: 'run_command', tool_call_id: 'a', result: 'A' } });
  assert.equal(root.children.length, 2); assert.equal(tools.length, 2);
  assert.equal(tools[0].result, 'A'); assert.equal(tools[1].result, 'B');
});

test('混合允许拒绝的同名命令，只启动允许项，拒绝和执行结果各归原工具块', () => {
  for (const approved of ['a', 'b']) {
    const { root, tools, send } = pipeline();
    const rejected = approved === 'a' ? 'b' : 'a';
    send({ tool_calls: ['a', 'b'].map((id, index) => ({ index, id, function: { name: 'run_command', arguments: '{}' } })) });
    for (const id of ['a', 'b']) send({ event: 'tool_approval', phase: 'requested', approval: {
      id: 'approval-' + id, agent_id: 'main', tool_call_id: id, tool: 'run_command', round: 1,
    } });
    send({ tool_return: { function_name: 'run_command', tool_call_id: rejected, result: 'PERMISSION_DENIED' } });
    send({ tool_start: { function_name: 'run_command', tool_call_id: approved } });
    send({ tool_return: { function_name: 'run_command', tool_call_id: approved, result: '命令完成' } });
    assert.equal(root.children.length, 2); assert.equal(tools.length, 2);
    assert.equal(tools[approved === 'a' ? 0 : 1].result, '命令完成');
    assert.equal(tools[rejected === 'a' ? 0 : 1].result, 'PERMISSION_DENIED');
    assert.equal(tools[rejected === 'a' ? 0 : 1].started, undefined);
    assert.ok(tools.every(tool => tool.finished));
  }
});

test('缺少子任务启动帧时审批补建子块，迟到启动仍复用对应派发', () => {
  const { root, cards, send } = pipeline();
  send({ event: 'tool_approval', phase: 'requested', approval: { id: 'approval', agent_id: 'child', parent_tool_call_id: 'dispatch', tool_call_id: 'call', tool: 'run_command' } });
  send({ event: 'sub_agent', phase: 'start', agent_id: 'child', parent_tool_call_id: 'dispatch' });
  assert.equal(root.children.length, 1); assert.equal(cards.length, 1); assert.equal(cards[0].approval.id, 'approval');
});

test('旧增量没有 ID 时审批认领原工具块，同名调用通过 index 区分', () => {
  const { root, tools, send } = pipeline();
  send({ tool_calls: [
    { index: 0, function: { name: 'run_command', arguments: '{}' } },
    { index: 1, function: { name: 'run_command', arguments: '{}' } },
  ] });
  for (const [index, id] of [[1, 'b'], [0, 'a']]) send({ event: 'tool_approval', phase: 'requested', approval: { agent_id: 'main', tool: 'run_command', tool_index: index, tool_call_id: id } });
  assert.equal(root.children.length, 2); assert.equal(tools.length, 2);
  assert.equal(tools[0].identity.toolCallId, 'a'); assert.equal(tools[1].identity.toolCallId, 'b');
  send({ tool_return: { function_name: 'run_command', tool_call_id: 'b', result: 'B' } });
  send({ tool_return: { function_name: 'run_command', tool_call_id: 'a', result: 'A' } });
  assert.equal(tools.length, 2); assert.equal(tools[0].result, 'A'); assert.equal(tools[1].result, 'B');
});
test('并发委派流式参数、逆序启动、返回和下一批调用均复用对应卡片', () => {
  const { root, cards, send } = pipeline();
  send({ tool_calls: [
    { index: 0, id: 'a', function: { name: 'sub_agent', arguments: '{"task":' } },
    { index: 1, id: 'b', function: { name: 'sub_agent', arguments: '{"task":"B"}' } },
  ] });
  send({ tool_calls: [{ index: 0, function: { arguments: '"A"}' } }] });
  send({ tool_start: { function_name: 'sub_agent', tool_call_id: 'b', arguments: { task: 'B' } } });
  send({ tool_start: { function_name: 'sub_agent', tool_call_id: 'a', arguments: { task: 'A' } } });
  send({ event: 'sub_agent', phase: 'start', parent_tool_call_id: 'b', agent_id: 'agent_b' });
  send({ event: 'sub_agent', phase: 'start', parent_tool_call_id: 'a', agent_id: 'agent_a' });
  assert.equal(cards.length, 2);
  assert.deepEqual(root.children.map(n => n.kind), ['agent', 'agent']);
  assert.equal(cards[0].events[0].agent_id, 'agent_a');
  assert.equal(cards[1].events[0].agent_id, 'agent_b');
  assert.equal(cards[0].sid, 'session');
  for (const [id, agent] of [['b', 'agent_b'], ['a', 'agent_a']]) {
    send({ event: 'sub_agent', phase: 'done', agent_id: agent, status: 'done' });
    send({ tool_return: { function_name: 'sub_agent', tool_call_id: id, sub_agent: { agent_id: agent } } });
  }
  assert.equal(cards[0].returned, true);
  send({ tool_calls: [{ index: 0, id: 'c', function: { name: 'sub_agent', arguments: '{"task":"C"}' } }] });
  assert.equal(cards.length, 3);
  assert.equal(cards[2].input, '{"task":"C"}');
});
test('名称晚到、调用 ID 晚到仍在原位置整合参数', () => {
  const { root, cards, send } = pipeline();
  send({ tool_calls: [{ index: 0, function: { arguments: '{"task":"A"}' } }] });
  send({ tool_calls: [{ index: 0, function: { name: 'sub_agent' } }] });
  send({ tool_start: { function_name: 'sub_agent', tool_call_id: 'late' } });
  send({ event: 'sub_agent', phase: 'start', parent_tool_call_id: 'late', agent_id: 'agent_late' });
  assert.equal(cards.length, 1);
  assert.equal(root.children.length, 1);
  assert.equal(root.children[0].kind, 'agent');
  assert.equal(cards[0].input, '{"task":"A"}');
  assert.equal(cards[0].events[0].agent_id, 'agent_late');
});


test('重连时启动事件先到，迟到参数仍合并到既有卡片', () => {
  const { root, cards, send } = pipeline();
  send({ event: 'sub_agent', phase: 'start', parent_tool_call_id: 'replay', agent_id: 'agent_replay' });
  send({ tool_calls: [{ index: 0, id: 'replay', function: { name: 'sub_agent', arguments: '{"task":"重连"}' } }] });
  send({ tool_start: { function_name: 'sub_agent', tool_call_id: 'replay' } });
  assert.equal(cards.length, 1);
  assert.equal(root.children.length, 1);
  assert.equal(cards[0].input, '{"task":"重连"}');
});


test('运行中消费追加提问后立即刷新导航', () => {
  const { root, send, navRefreshes } = pipeline();
  send({ event: 'message_injected', text: '追加提问' });
  assert.equal(root.children[0].kind, 'user');
  assert.equal(navRefreshes(), 1);
});

test('供应商重试移除未执行的参数占位，重试参数从头生成', () => {
  const { root, cards, send } = pipeline();
  send({ tool_calls: [{ index: 0, id: 'broken', function: { name: 'sub_agent', arguments: '{broken' } }] });
  assert.equal(root.children[0].kind, 'agent');
  send({ error: 'failed', retrying: true, retry: 1, discard_tool_calls: true });
  assert.equal(root.children.some(n => n.kind === 'agent'), false);
  send({ tool_calls: [{ index: 0, id: 'fresh', function: { name: 'sub_agent', arguments: '{"task":"new"}' } }] });
  assert.equal(cards[1].input, '{"task":"new"}');
  assert.equal(root.children.filter(n => n.kind === 'agent').length, 1);
});

test('后续请求重试不会移除已经派发的子任务卡片', () => {
  const { root, send } = pipeline();
  send({ tool_calls: [{ index: 0, id: 'a', function: { name: 'sub_agent', arguments: '{"task":"A"}' } }] });
  send({ tool_start: { function_name: 'sub_agent', tool_call_id: 'a', arguments: { task: 'A' } } });
  send({ event: 'sub_agent', phase: 'start', parent_tool_call_id: 'a', agent_id: 'agent_a' });
  send({ error: 'failed', retrying: true, retry: 1, discard_tool_calls: true });
  assert.equal(root.children.filter(n => n.kind === 'agent').length, 1);
});

test('主流与子任务命令结果通知当前会话终端管理器，子任务其他事件不触发', () => {
  const { send, terminalNotices } = pipeline();
  send({ tool_return: { function_name: 'run_command', result: { background: true, job_id: 'bg_one' } } });
  send({ event: 'sub_agent', phase: 'tool_result', agent_id: 'a', tool_name: 'stop_command', result: 'stopped' });
  send({ event: 'sub_agent', phase: 'tool_result', agent_id: 'a', function_name: 'poll_command', result: 'running' });
  send({ event: 'sub_agent', phase: 'tool_start', agent_id: 'a', function_name: 'run_command' });
  assert.deepEqual(terminalNotices, [['session', 'run_command'], ['session', 'stop_command'], ['session', 'poll_command']]);
});
