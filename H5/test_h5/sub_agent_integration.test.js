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
}
function pipeline() {
  const source = fs.readFileSync(path.join(__dirname, '../js/app/chat.js'), 'utf8');
  const start = source.indexOf('  function createStreamPipeline(');
  const end = source.indexOf('\n  async function send(', start);
  const functionEnd = source.lastIndexOf('\n  }', end) + 4;
  const cards = [];
  let navRefreshes = 0;
  const context = {
    state: { sessionId: 'session' }, stickToBottom() {}, pickTitlePreview() { return ''; },
    FormatUtils: { prettyJson: JSON.stringify },
    App: {
      clearInjectedPending() {},
      appendUserMessage(text, ts, sid, docs, container) { container.appendChild(new Node('user')); },
      rebuildQnav() { navRefreshes += 1; },
      buildToolBlock() { return { wrap: new Node('tool'), setName() {}, beginStream() {}, addInputDelta() {}, setInputText() {}, finish() {} }; },
      buildSubAgentBlock(sid) {
        const card = { wrap: new Node('agent'), sid, input: '', events: [], done: false,
          setDispatchInput(value) { this.input = value; },
          applyEvent(evt) { this.events.push(evt); if (evt.phase === 'done') this.done = true; },
          markReturned() { this.returned = true; }, isDone() { return this.done; },
          finalizeInterrupted() { this.done = true; },
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
  return { root, cards, navRefreshes: () => navRefreshes, send: data => pipe.handle({ data }) };
}
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
