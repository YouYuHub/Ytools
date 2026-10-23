const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const StreamReplayDedup = require('../js/stream_replay_dedup.js');

function makeNode(tag, cls, text) {
  const node = {
    tagName: tag, className: cls || '', children: [], parentNode: null,
    dataset: {}, textContent: text || '', hidden: false,
    classList: null, // assigned below (needs node ref)
    appendChild(c) {
      if (c.parentNode) c.parentNode.removeChild(c);
      c.parentNode = node; node.children.push(c); return c;
    },
    insertBefore(c, ref) {
      if (c.parentNode) c.parentNode.removeChild(c);
      c.parentNode = node;
      const at = ref ? node.children.indexOf(ref) : -1;
      if (at < 0) node.children.push(c); else node.children.splice(at, 0, c);
      return c;
    },
    removeChild(c) {
      const at = node.children.indexOf(c);
      if (at >= 0) node.children.splice(at, 1);
      c.parentNode = null; return c;
    },
    remove() { if (node.parentNode) node.parentNode.removeChild(node); },
    contains(other) {
      if (other === node) return true;
      for (const c of node.children) if (c.contains ? c.contains(other) : c === other) return true;
      return false;
    },
    querySelector(sel) { return queryAll(node, sel)[0] || null; },
    querySelectorAll(sel) { return queryAll(node, sel); },
  };
  node.classList = {
    add: (...cs) => { node.className = Array.from(new Set((node.className + ' ' + cs.join(' ')).split(/\s+/).filter(Boolean))).join(' '); },
    remove: (...cs) => { node.className = node.className.split(/\s+/).filter(c => c && !cs.includes(c)).join(' '); },
    toggle: c => { node.classList.contains(c) ? node.classList.remove(c) : node.classList.add(c); },
    contains: c => node.className.split(/\s+/).includes(c),
  };
  Object.defineProperty(node, 'innerHTML', {
    configurable: true,
    get() { return node.textContent; },
    set(v) { node.textContent = String(v).replace(/<[^>]*>/g, ''); },
  });
  Object.defineProperty(node, 'children', { configurable: true, writable: true, value: node.children });
  return node;
}

function matches(node, sel) {
  const m = sel.match(/^\.([^\[]+?)(?:\[([^\]=]+)="([^"]*)"\])?$/);
  if (!m) return false;
  for (const c of m[1].split('.')) {
    if (!(node.className || '').split(/\s+/).includes(c)) return false;
  }
  if (m[2]) {
    const prop = m[2].startsWith('data-')
      ? m[2].slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase()) : m[2];
    if (String(node.dataset[prop] ?? '') !== m[3]) return false;
  }
  return true;
}

function queryAll(root, sel) {
  const out = [];
  (function walk(node) {
    for (const child of node.children) {
      if (matches(child, sel)) out.push(child);
      walk(child);
    }
  })(root);
  return out;
}

function harness() {
  const source = fs.readFileSync(path.join(__dirname, '../js/app/stream_pipeline.js'), 'utf8');
  const start = source.indexOf('  function createStreamPipeline(');
  const end = source.indexOf('\n  App.createStreamPipeline =', start);
  const functionEnd = source.lastIndexOf('\n  }', end) + 4;
  const chatInner = makeNode('div', 'chat-inner');
  const msg = makeNode('div', 'msg');
  chatInner.appendChild(msg);
  const builtTools = [];
  const builtThinks = [];
  const context = {
    state: { sessionId: 's' },
    chatInner,
    StreamReplayDedup,
    stickToBottom() {}, pickTitlePreview() { return ''; },
    setTimeout() { return 1; }, clearTimeout() {}, FILE_BADGE_TOOLS: [],
    Markdown: { render: text => '<p>' + text + '</p>' },
    el(tag, cls, text) { return makeNode(tag, cls, text); },
    FormatUtils: {
      roundSummaryText: (usage, model) => usage && usage.total_tokens
        ? String(usage.total_tokens) : model && model.name ? 'model ' + model.name : '',
      prettyJson: v => JSON.stringify(v),
    },
    App: {
      buildThinkBlock() {
        const wrap = makeNode('div', 'think-block');
        const content = makeNode('div', 'think-content');
        wrap.appendChild(content);
        const ui = { wrap, text: '', closed: false,
          streaming() {}, setText(t) { ui.text = t; content.textContent = t; },
          add(t) { ui.text += t; content.textContent = ui.text; }, done() { ui.closed = true; } };
        builtThinks.push(ui);
        return ui;
      },
      buildToolBlock(name, identity) {
        const wrap = makeNode('div', 'tool-block');
        const ui = { wrap, name, finished: false, inputs: [], outputs: [], binds: [],
          setName(n) { ui.name = n; }, setInput(t) { ui.inputs.push(t); },
          setOutput(t) { ui.outputs.push(t); }, finish() { ui.finished = true; },
          executing() {}, beginStream() {}, addInputDelta(t) { ui.inputs.push(t); },
          setInputText(t) { ui.inputs.push(t); },
          bindPermission(id) { ui.binds.push(id); } };
        if (identity && identity.toolCallId) {
          wrap.dataset.toolCallId = identity.toolCallId;
          wrap._toolBlockUi = ui;
        }
        builtTools.push(ui);
        return ui;
      },
      renderPreservingWidgets(node, html) { node.innerHTML = html; },
      highlightCodeBlocks() {}, updateCodeblockCopyButtons() {},
      refreshContextTokenStats() {},
      applyToolResult(block, name, resultText) { block.setOutput(resultText); },
      scheduleContextTokenStatsRefresh() {}, clearInjectedPending() {}, rebuildQnav() {},
    },
  };
  vm.createContext(context);
  vm.runInContext(source.slice(start, functionEnd), context);
  const activeStream = { round: 2 };
  const pipe = context.createStreamPipeline(msg, 's', activeStream);
  return {
    pipe, chatInner, msg, activeStream, context, state: context.state, builtTools, builtThinks,
    send: data => pipe.handle({ data }),
    toolsIn: root => queryAll(root, '.tool-block'),
    textsIn: root => queryAll(root, '.msg-assistant-body'),
  };
}

test('续答的实时容器被尾部窗口重建移除后，下一帧恢复原节点并显示工具过程', () => {
  const h = harness();
  h.state.activeStream = h.activeStream;
  h.activeStream.userNode = makeNode('div','msg-user','回答模型提问');
  h.msg.remove();
  h.send({tool_start:{function_name:'read_file',tool_call_id:'live-after-answer'}});
  assert.equal(h.msg.parentNode,h.chatInner);
  assert.equal(h.activeStream.userNode.parentNode,h.chatInner);
  assert.equal(h.chatInner.children[0],h.activeStream.userNode);
  assert.equal(h.chatInner.children[1],h.msg);
  assert.equal(h.toolsIn(h.chatInner).length,1);
});

for (const detached of [false,true]) test('实时帧恢复尾部节点的可见性及提问气泡：detached='+detached, () => {
  const h = harness(); h.state.activeStream = h.activeStream;
  const user = h.activeStream.userNode = makeNode('div','msg-user history-live-hidden','最新提问');
  if (!detached) h.chatInner.insertBefore(user,h.msg);
  h.msg.classList.add('history-live-hidden');
  h.context.App.getHistoryWindow = () => ({records:[1,2,3],end:3});
  h.send({content:'实时答案'});
  assert.equal(user.parentNode,h.chatInner);
  assert.equal(user.classList.contains('history-live-hidden'),false);
  assert.equal(h.msg.classList.contains('history-live-hidden'),false);
  assert.equal(h.chatInner.children[0],user);
});

for (const reason of ['other session','detached','superseded','reading history']) test('实时容器恢复不打断浏览状态：'+reason, () => {
  const h = harness(); h.state.activeStream = h.activeStream; h.msg.remove();
  if(reason==='other session')h.state.sessionId='other';
  if(reason==='detached')h.activeStream.detached=true;
  if(reason==='superseded')h.state.activeStream={round:3};
  if(reason==='reading history')h.context.App.getHistoryWindow=()=>({records:[1,2,3],end:1});
  h.send({content:'后台新内容'});
  assert.equal(h.msg.parentNode,null);
});

function historyRound(h, round) {
  const user = makeNode('div', 'msg-user');
  user.dataset.round = String(round);
  const text = makeNode('div', 'msg-assistant-body');
  text.dataset.round = String(round);
  text.textContent = '正在查看当前目录的文件列表。';
  const think = makeNode('div', 'think-block');
  think.dataset.round = String(round);
  const thinkContent = makeNode('div', 'think-content');
  thinkContent.textContent = '让我想想';
  think.appendChild(thinkContent);
  const tool = makeNode('div', 'tool-block');
  tool.dataset.round = String(round);
  tool.dataset.toolCallId = 'call_1';
  const historyUi = { wrap: tool, name: 'run_command', finished: false, inputs: ['{"command":"pwd"}'], outputs: [], binds: [],
    setName() {}, setInput(t) { historyUi.inputs.push(t); }, setOutput(t) { historyUi.outputs.push(t); },
    finish() { historyUi.finished = true; }, executing() {}, beginStream() {},
    addInputDelta(t) { historyUi.inputs.push(t); }, setInputText(t) { historyUi.inputs.push(t); },
    bindPermission(id) { historyUi.binds.push(id); } };
  tool._toolBlockUi = historyUi;
  // 插到 msg 之前，模拟历史在先、附接容器在后
  for (const node of [user, text, think, tool]) h.chatInner.insertBefore(node, h.msg);
  return { user, text, think, tool, historyUi };
}

test('完整任务回放保留旧提问卡但不反复自动弹窗，实时提问仍弹窗', () => {
  const h=harness();let opened=0;
  h.context.App.buildAskBlock=()=>makeNode('div','ask-block');
  h.context.App.refreshAskBlockStates=()=>{};
  h.context.App.openAskModal=()=>opened++;
  h.pipe.setReplayWindow(true);
  h.send({event:'ask_user',questions:[{question:'旧提问'}]});
  assert.equal(opened,0);
  h.pipe.setReplayWindow(false);
  h.send({event:'ask_user',questions:[{question:'新提问'}]});
  assert.equal(opened,1);
  assert.equal(h.msg.querySelectorAll('.ask-block').length,2);
});

test('刷新附接流收养同轮历史 usage 行，并随新快照更新', () => {
  const h = harness();
  const previousRound = makeNode('div', 'round-usage', '历史用量');
  previousRound.dataset.round = '1';
  const historical = makeNode('div', 'round-usage', '历史用量');
  historical.dataset.round = '2';
  historical._roundUsageSnapshot = { total_tokens: 15 };
  h.chatInner.insertBefore(previousRound, h.msg);
  h.chatInner.insertBefore(historical, h.msg);

  h.pipe.setRoundModel({ name: 'test-model' });
  assert.equal(historical.parentNode, h.msg, '同轮历史行应被附接流收养');
  assert.equal(historical.textContent, '15', '模型标记不能把已落盘的用量覆盖成仅模型名');
  assert.equal(h.chatInner.querySelectorAll('.round-usage[data-round="2"]').length, 1);
  assert.equal(previousRound.parentNode, h.chatInner, '其他轮次不能被收养');
  h.send({ id: 'completion-1', usage: { total_tokens: 10 } });
  assert.equal(historical.textContent, '15', '部分回放快照不能使已落盘用量回退');
  h.send({ id: 'completion-1', usage: { total_tokens: 20 } });
  assert.equal(historical.textContent, '20', '后续快照应覆盖原行');
  assert.equal(h.chatInner.querySelectorAll('.round-usage[data-round="2"]').length, 1);
  h.pipe.finish();
});

test('附接标记的累计用量在历史行缺失、回放无 usage 时仍显示', () => {
  const h = harness();
  h.pipe.setRoundUsageBaseline({ total_tokens: 15 });
  h.pipe.setRoundModel({ name: 'test-model' });
  assert.equal(h.chatInner.querySelectorAll('.round-usage[data-round="2"]').length, 1);
  assert.equal(h.chatInner.querySelector('.round-usage[data-round="2"]').textContent, '15');
  h.pipe.finish();
  assert.equal(h.chatInner.querySelector('.round-usage[data-round="2"]').textContent, '15');
});

test('实时轮次摘要保存结构化模型和用量快照，供任务用量展示使用', () => {
  const h = harness();
  const first = {provider:'p',name:'first'};
  h.pipe.setRoundModel(first);
  const line = h.chatInner.querySelector('.round-usage[data-round="2"]');
  assert.equal(line._roundModelSnapshot, first);
  h.send({id:'completion-1',usage:{total_tokens:20}});
  assert.equal(line._roundUsageSnapshot.total_tokens, 20);
  const second = {provider:'p',name:'second'};
  h.pipe.setRoundModel(second);
  assert.equal(line._roundModelSnapshot, second);
  assert.equal(line._roundUsageSnapshot.total_tokens, 20);
  h.pipe.finish();
});

test('后端绝对累计优先于不完整的 usage 回放，并接收后续增长', () => {
  const h = harness();
  h.pipe.setRoundUsageBaseline({ total_tokens: 15 });
  h.send({ id: 'old-completion', usage: { total_tokens: 100 } });
  assert.equal(h.chatInner.querySelector('.round-usage[data-round="2"]').textContent, '15');
  h.send({ round_usage_total: { total_tokens: 25 } });
  assert.equal(h.chatInner.querySelector('.round-usage[data-round="2"]').textContent, '25');
  assert.equal(h.chatInner.querySelectorAll('.round-usage[data-round="2"]').length, 1);
});

test('重发中的服务端绝对用量可低于仍在历史里的旧轮用量', () => {
  const h = harness();
  const historical = makeNode('div', 'round-usage', '旧轮用量');
  historical.dataset.round = '2';
  historical._roundUsageSnapshot = { total_tokens: 50 };
  h.chatInner.insertBefore(historical, h.msg);
  h.pipe.setRoundUsageBaseline({ total_tokens: 5 });
  h.pipe.setRoundModel({ name: 'test-model' });
  assert.equal(historical.parentNode, h.msg);
  assert.equal(historical.textContent, '5', '正在重发的当前轮以服务端绝对用量为准');
});

test('空绝对用量且无模型时隐藏旧轮行，后续用量到达再复用该行', () => {
  const h = harness();
  const historical = makeNode('div', 'round-usage', '旧轮用量');
  historical.dataset.round = '2';
  historical._roundUsageSnapshot = { total_tokens: 50 };
  h.chatInner.insertBefore(historical, h.msg);
  h.pipe.setRoundUsageBaseline({});
  assert.equal(historical.parentNode, null);
  assert.equal(h.chatInner.querySelectorAll('.round-usage[data-round="2"]').length, 0);
  h.send({ content: '新回答' });
  assert.equal(h.chatInner.querySelectorAll('.round-usage[data-round="2"]').length, 0,
    '正文到达时不应把已隐藏的空用量行重新挂回');
  h.send({ round_usage_total: { total_tokens: 5 } });
  assert.equal(historical.parentNode, h.msg);
  assert.equal(historical.textContent, '5');
  assert.equal(h.chatInner.querySelectorAll('.round-usage[data-round="2"]').length, 1);
});

test('重发或插入的附接 marker 按目标轮次收养 usage，邻轮保持原位', () => {
  const h = harness();
  h.activeStream.round = 3;
  const adjacent = makeNode('div', 'round-usage', '邻轮');
  adjacent.dataset.round = '2';
  const target = makeNode('div', 'round-usage', '目标轮');
  target.dataset.round = '3';
  h.chatInner.insertBefore(adjacent, h.msg);
  h.chatInner.insertBefore(target, h.msg);
  h.pipe.setRoundUsageBaseline({ total_tokens: 5 });
  assert.equal(target.parentNode, h.msg);
  assert.equal(target.textContent, '5');
  assert.equal(adjacent.parentNode, h.chatInner);
  assert.equal(adjacent.textContent, '邻轮');
});

test('无轮次号或切换会话时不收养其他会话的历史 usage 行', () => {
  const noRound = harness();
  const historical = makeNode('div', 'round-usage', '历史用量');
  historical.dataset.round = '2';
  noRound.chatInner.insertBefore(historical, noRound.msg);
  noRound.activeStream.round = null;
  noRound.pipe.setRoundModel({ name: 'test-model' });
  assert.equal(historical.parentNode, noRound.chatInner);

  const switched = harness();
  const otherSession = makeNode('div', 'round-usage', '其他会话用量');
  otherSession.dataset.round = '2';
  switched.chatInner.insertBefore(otherSession, switched.msg);
  switched.state.sessionId = 'another-session';
  switched.pipe.setRoundModel({ name: 'test-model' });
  assert.equal(otherSession.parentNode, switched.chatInner);
  assert.equal(otherSession.textContent, '其他会话用量');
});

test('回放窗口内命中历史的正文/思考/工具都不新建，结果归位到原卡', () => {
  const h = harness();
  const { historyUi } = historyRound(h, 2);
  const toolsBefore = h.builtTools.length;
  h.pipe.setReplayWindow(true);
  h.send({ reasoning_content: '让我想想' });
  h.send({ content: '正在查看当前目录的文件列表。' });
  h.send({ tool_calls: [{ index: 0, id: 'call_1', function: { name: 'run_command', arguments: '{"command":"pwd"}' } }] });
  // seal 发生在 tool_calls 处理入口：暂存已终结并命中丢弃
  assert.equal(h.builtThinks.length, 0, '命中的思考不应建块');
  assert.equal(h.textsIn(h.msg).length, 0, '命中的正文不应新建');
  assert.equal(h.builtTools.length, toolsBefore, '命中的工具不应新建');
  assert.equal(historyUi.inputs.length, 1, '收养卡输入不应被重放增量复写');
  assert.equal(historyUi.binds.length, 1, '收养卡应重新绑定权限');
  h.send({ tool_return: { function_name: 'run_command', tool_call_id: 'call_1', arguments: {}, result: 'ok' } });
  assert.deepEqual(historyUi.outputs, ['ok'], '结果应归位到历史原卡');
  assert.equal(h.toolsIn(h.chatInner).length, 1, '全场只有一张工具卡');
  h.pipe.setReplayWindow(false);
  h.send({ content: '最终答案' });
  h.send({ finish_reason: 'stop' });
  const live = h.textsIn(h.msg);
  assert.equal(live.length, 1);
  assert.equal(live[0].textContent, '最终答案');
  h.pipe.finish();
});

test('窗口内未命中历史的内容正常新建（刷新裁剪/中途刷新场景）', () => {
  const h = harness();
  h.pipe.setReplayWindow(true);
  h.send({ content: '全新正文' });
  h.send({ finish_reason: 'stop' });
  const live = h.textsIn(h.msg);
  assert.equal(live.length, 1);
  assert.equal(live[0].textContent, '全新正文');
  h.send({ tool_calls: [{ index: 0, id: 'call_new', function: { name: 'run_command', arguments: '{}' } }] });
  assert.equal(h.toolsIn(h.msg).length, 1);
  h.pipe.finish();
});

test('窗口关闭后相同文本不再匹配（实时重复不误删）', () => {
  const h = harness();
  historyRound(h, 2);
  h.pipe.setReplayWindow(true);
  h.send({ content: '正在查看当前目录的文件列表。' });
  h.send({ finish_reason: 'stop' });
  assert.equal(h.textsIn(h.msg).length, 0, '窗口内命中应丢弃');
  h.pipe.setReplayWindow(false);
  h.send({ content: '正在查看当前目录的文件列表。' });
  h.send({ finish_reason: 'stop' });
  assert.equal(h.textsIn(h.msg).length, 1, '窗口外相同文本必须保留');
  h.pipe.finish();
});

test('无窗口时行为不变（普通发送/旧后端）', () => {
  const h = harness();
  historyRound(h, 2);
  h.send({ content: '正在查看当前目录的文件列表。' });
  h.send({ tool_calls: [{ index: 0, id: 'call_1', function: { name: 'run_command', arguments: '{}' } }] });
  assert.equal(h.textsIn(h.msg).length, 1, '无窗口不做正文匹配');
  // 工具收养不依赖窗口（callId 全局唯一）：有历史卡则复用
  assert.equal(h.toolsIn(h.msg).length, 0, '同 callId 历史卡应被收养');
  assert.equal(h.toolsIn(h.chatInner).length, 1);
  h.pipe.finish();
});

test('无轮次号时正文不匹配、工具仍按 callId 收养', () => {
  const h = harness();
  historyRound(h, 2);
  h.activeStream.round = null;
  h.pipe.setReplayWindow(true);
  h.send({ content: '正在查看当前目录的文件列表。' });
  h.send({ finish_reason: 'stop' });
  assert.equal(h.textsIn(h.msg).length, 1, '无轮次号不做正文匹配');
  h.send({ tool_calls: [{ index: 0, id: 'call_1', function: { name: 'run_command', arguments: '{}' } }] });
  assert.equal(h.toolsIn(h.msg).length, 0, '工具收养不需要轮次号');
  h.pipe.finish();
});

test('原文重复时按序消费：两次命中都丢弃，第三次新建', () => {
  const h = harness();
  const first = makeNode('div', 'msg-assistant-body');
  first.dataset.round = '2';
  first.textContent = '好的';
  const second = makeNode('div', 'msg-assistant-body');
  second.dataset.round = '2';
  second.textContent = '好的';
  h.chatInner.insertBefore(first, h.msg);
  h.chatInner.insertBefore(second, h.msg);
  h.pipe.setReplayWindow(true);
  h.send({ content: '好的' });
  h.send({ finish_reason: 'stop' });
  h.send({ content: '好的' });
  h.send({ finish_reason: 'stop' });
  assert.equal(h.textsIn(h.msg).length, 0, '两次回放都命中历史');
  h.pipe.setReplayWindow(false);
  h.send({ content: '好的' });
  h.send({ finish_reason: 'stop' });
  assert.equal(h.textsIn(h.msg).length, 1, '窗口外第三次必须保留');
  h.pipe.finish();
});

test('工具卡落盘 callId 并挂载 ui 控制器供收养，绑定更新同步戳记', () => {
  const source = fs.readFileSync(path.join(__dirname, '../js/app/messages.js'), 'utf8');
  const start = source.indexOf('  function buildToolBlock(');
  const end = source.indexOf('  // ---------- 展示用 diff 视图', start);
  function looseNode(tag, cls, text) {
    const node = makeNode(tag, cls, text);
    const strictQuery = node.querySelector;
    node.querySelector = sel => strictQuery.call(node, sel) || makeNode('span', 'stub');
    node.addEventListener = () => {};
    return node;
  }
  const context = {
    state: { sessionId: 's' },
    el: (tag, cls, text) => looseNode(tag, cls, text),
    App: { isBuiltinToolName: () => false, toolDisplayName: n => n, toolIconSvg: () => '<svg></svg>' },
  };
  vm.createContext(context);
  vm.runInContext(source.slice(start, end) + '\nthis.__build = buildToolBlock;', context);
  const ui = context.__build('run_command', { toolCallId: 'call_1' });
  assert.equal(ui.wrap.dataset.toolCallId, 'call_1');
  assert.equal(ui.wrap._toolBlockUi, ui);
  ui.bindPermission({ toolCallId: 'call_2' });
  assert.equal(ui.wrap.dataset.toolCallId, 'call_2');
  const bare = context.__build('run_command', {});
  assert.equal(bare.wrap.dataset.toolCallId, undefined);
  assert.equal(bare.wrap._toolBlockUi, bare);
});
