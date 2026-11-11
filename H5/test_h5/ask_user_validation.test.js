const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = () => fs.readFileSync(path.join(__dirname, '../js/app/builtin.js'), 'utf8');

function loadParse() {
  const src = source();
  const context = {};
  vm.createContext(context);
  // askCodePoints + parseAskQuestionsFromArgs（两函数相邻，一并载入）
  vm.runInContext(src.slice(src.indexOf('  function askCodePoints('), src.indexOf('  function renderAskModal(')), context);
  return context.parseAskQuestionsFromArgs;
}

test('历史回放：合法提问原样建卡（含边界值与 emoji 计数）', () => {
  const parse = loadParse();
  const q200 = '中'.repeat(200), id64 = 'i'.repeat(64), opt100 = 'o'.repeat(100);
  const args = JSON.stringify({ questions: [
    { id: id64, question: q200, options: [opt100], multiple: false },
    { question: 'emoji 🤖 按 1 字符计', options: [] },
    { question: '无 options 键', multiple: true },
  ]});
  const out = parse(args);
  assert.ok(out && out.length === 3);
  // emoji：200 个 emoji（400 UTF-16 单元）仍应通过
  assert.ok(parse(JSON.stringify({ questions: [{ question: '🤖'.repeat(200) }] })));
});

test('历史回放：后端会拒绝的非法提问不建卡（与 schema 口径一致）', () => {
  const parse = loadParse();
  const cases = [
    ['空数组', { questions: [] }],
    ['6 题超上限', { questions: [1, 2, 3, 4, 5, 6].map(i => ({ question: '第' + i + '题' })) }],
    ['题干 201 字符', { questions: [{ question: 'x'.repeat(201) }] }],
    ['题干空串', { questions: [{ question: '' }] }],
    ['题干纯空格', { questions: [{ question: '   ' }] }],
    ['7 个选项', { questions: [{ question: 'q', options: ['1', '2', '3', '4', '5', '6', '7'] }] }],
    ['选项 101 字符', { questions: [{ question: 'q', options: ['y'.repeat(101)] }] }],
    ['选项空串', { questions: [{ question: 'q', options: [''] }] }],
    ['id 65 字符', { questions: [{ id: 'i'.repeat(65), question: 'q' }] }],
    ['multiple 非布尔', { questions: [{ question: 'q', multiple: 'yes' }] }],
    ['options 非数组', { questions: [{ question: 'q', options: '甲' }] }],
    ['缺 question 键', { questions: [{ options: ['甲'] }] }],
    ['非法 JSON', '{not json'],
  ];
  for (const [name, args] of cases) {
    assert.equal(parse(typeof args === 'string' ? args : JSON.stringify(args)), null, name + ' 应返回 null');
  }
  // 会话实测的三个被拦截调用：id65 / opt101 / 6 题，一律不建卡
  assert.equal(parse(JSON.stringify({ questions: [{ id: '0'.repeat(65), options: ['甲'], question: '测 id 上界' }] })), null);
  assert.equal(parse(JSON.stringify({ questions: [{ id: 'x', options: ['0'.repeat(101)], question: '测选项上界' }] })), null);
});

test('提交门禁：多选可 0 选择、可留空提交（未回答标记），空卷才禁用', () => {
  const src = source();
  const mkItem = ({ selected = 0, custom = '' } = {}) => ({
    querySelectorAll: sel => sel === '.ask-option.selected' ? Array(selected).fill({}) : [],
    querySelector: sel => sel === '.ask-custom' ? { value: custom } : null,
  });
  const run = items => {
    const context = { askQuestions: { querySelectorAll: () => items }, askSubmit: {} };
    vm.createContext(context);
    vm.runInContext(src.slice(src.indexOf('  function updateAskSubmitState('), src.indexOf('  function openAskModal(')), context);
    context.updateAskSubmitState();
    return context.askSubmit.disabled;
  };
  assert.equal(run([mkItem({}), mkItem({})]), false, '全部留空也允许提交（跳过语义）');
  assert.equal(run([mkItem({ selected: 0, custom: '' })]), false, '多选 0 选择允许提交');
  assert.equal(run([mkItem({ selected: 1 })]), false, '已作答保持可提交');
  assert.equal(run([]), true, '无题目时禁用');
});

test('回答分流：后续用户气泡是回答轮时走更新分支（DOM 直查）', () => {
  const src = source();
  const bubble = { textContent: '【回答模型提问】\n1. 甲' };
  const userNode = {
    getAttribute: name => name === 'data-round' ? '21' : null,
    matches: sel => sel === '.msg-user',
    querySelector: sel => sel === '.msg-bubble' ? bubble : null,
  };
  const chatInner = { children: [userNode] };
  const block = { isConnected: true, getAttribute: () => '20', parentElement: chatInner };
  const confirmed = [], sent = [];
  const context = {
    chatInner,
    App: { historyQuestionAfterRound: () => null, send: data => sent.push(data) },
    showAskAnswerConfirm: (...args) => confirmed.push(args),
  };
  vm.createContext(context);
  vm.runInContext(src.slice(src.indexOf('  function submitAskAnswer('), src.indexOf('  function showAskAnswerConfirm(')), context);
  context.submitAskAnswer('新回答', block);
  assert.equal(sent.length, 0);
  assert.deepEqual(confirmed, [['新回答', 20, true]]);
});

test('回答分流：后续是普通对话轮时走插入分支', () => {
  const src = source();
  const bubble = { textContent: '继续聊别的' };
  const userNode = {
    getAttribute: name => name === 'data-round' ? '21' : null,
    matches: sel => sel === '.msg-user',
    querySelector: sel => sel === '.msg-bubble' ? bubble : null,
  };
  const chatInner = { children: [userNode] };
  const block = { isConnected: true, getAttribute: () => '20', parentElement: chatInner };
  const confirmed = [], sent = [];
  const context = {
    chatInner,
    App: { historyQuestionAfterRound: () => null, send: data => sent.push(data) },
    showAskAnswerConfirm: (...args) => confirmed.push(args),
  };
  vm.createContext(context);
  vm.runInContext(src.slice(src.indexOf('  function submitAskAnswer('), src.indexOf('  function showAskAnswerConfirm(')), context);
  context.submitAskAnswer('新回答', block);
  assert.equal(sent.length, 0);
  assert.deepEqual(confirmed, [['新回答', 20, false]]);
});
