const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function pipeline() {
  const source = fs.readFileSync(path.join(__dirname, '../js/app/chat.js'), 'utf8');
  const start = source.indexOf('  function createStreamPipeline(');
  const end = source.indexOf('\n  async function send(', start);
  const functionEnd = source.lastIndexOf('\n  }', end) + 4;
  const root = { children: [], appendChild(node) {
    this.children = this.children.filter(n => n !== node); this.children.push(node);
  }, querySelectorAll() { return []; }, querySelector() { return null; } };
  const thoughts = [];
  const context = {
    state: { sessionId: 's' }, stickToBottom() {}, pickTitlePreview() { return ''; },
    setTimeout() { return 1; }, clearTimeout() {}, FILE_BADGE_TOOLS: [],
    Markdown: { render: text => text },
    el(tag, cls, text) { return { kind: cls, textContent: text, classList: { remove() {} } }; },
    FormatUtils: { roundSummaryText: () => 'usage', prettyJson: JSON.stringify },
    App: {
      buildThinkBlock() {
        const thought = { wrap: { kind: 'thinking' }, text: '', closed: false,
          streaming() {}, add(text) { this.text += text; }, done() { this.closed = true; } };
        thoughts.push(thought); return thought;
      },
      buildToolBlock() { return { wrap: { kind: 'tool' }, setName() {}, executing() {},
        beginStream() {}, addInputDelta() {}, setInput() {}, finish() {} }; },
      renderPreservingWidgets(node, html) { node.html = html; }, highlightCodeBlocks() {},
      updateCodeblockCopyButtons() {}, refreshContextTokenStats() {}, applyToolResult() {},
      scheduleContextTokenStatsRefresh() {}, clearInjectedPending() {}, applySessionTodo() {},
    },
  };
  vm.createContext(context);
  vm.runInContext(source.slice(start, functionEnd), context);
  const pipe = context.createStreamPipeline(root, 's', {});
  return { root, thoughts, send: data => pipe.handle({ data }), finish: pipe.finish };
}

test('空帧、usage、签名活动和空工具数组不拆分同一段思考', () => {
  const { thoughts, send, finish } = pipeline();
  send({ reasoning_content: '第一部分' });
  for (const frame of [{}, { content: '' }, { reasoning_content: '' }, { tool_calls: [] },
    { upstream_activity: true }, { id: 'm', usage: { prompt_tokens: 10 } },
    { event: 'todo', todos: [] }, { round_model: { name: 'Deepseek' } }]) {
    send(frame);
    send({ reasoning_content: '继续' });
  }
  assert.equal(thoughts.length, 1);
  assert.equal(thoughts[0].text, '第一部分' + '继续'.repeat(8));
  assert.equal(thoughts[0].closed, false);
  finish();
  assert.equal(thoughts[0].closed, true);
});

test('正文、工具与 completion 收尾保留真正的思考阶段边界', () => {
  for (const boundary of [{ content: '回答' }, { finish_reason: 'stop' }, { response_complete: true },
    { stream_truncated: true }, { error: '断流' },
    { tool_calls: [{ id: 't', function: { name: 'work', arguments: '{}' } }] },
    { tool_start: { function_name: 'work' } }, { tool_return: { function_name: 'work', result: 'ok' } }]) {
    const { thoughts, send } = pipeline();
    send({ reasoning_content: '前一阶段' });
    send(boundary);
    assert.equal(thoughts[0].closed, true);
    send({ reasoning_content: '下一阶段' });
    assert.equal(thoughts.length, 2);
  }
});

test('同帧思考和正文正常结束思考并完整渲染正文', () => {
  const { thoughts, root, send, finish } = pipeline();
  send({ reasoning_content: '思考', content: '答案', response_complete: true });
  finish();
  assert.equal(thoughts.length, 1);
  assert.equal(thoughts[0].closed, true);
  assert.equal(root.children.find(n => n.kind.includes('msg-assistant-body')).html, '答案');
});
