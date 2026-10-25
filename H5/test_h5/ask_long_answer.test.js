const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
test('自定义回答支持多行长文本并完整提交', () => {
  class Node {
    constructor(tag, cls = '', text = '') { this.tag = tag; this.cls = cls; this.textContent = text; this.children = []; this.value = ''; this.events = {}; }
    appendChild(n) { this.children.push(n); }
    setAttribute() {} addEventListener(type, fn) { this.events[type] = fn; }
    querySelectorAll(selector) { return this.children.flatMap(n => [...((n.className || n.cls) === selector.slice(1) ? [n] : []), ...n.querySelectorAll(selector)]); }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    get childElementCount() { return this.children.length; }
    set innerHTML(v) { this.children = []; }
  }
  const source = fs.readFileSync(path.join(__dirname, '../js/app/builtin.js'), 'utf8');
  let submitted;
  const context = { document: { createElement: tag => new Node(tag) }, el: (tag, cls, text) => new Node(tag, cls, text),
    askQuestions: new Node('div'), askSubmit: {}, state: { pendingAskQuestions: [{ question: '详细需求', options: [] }] },
    toast() {}, closeAskModal() {}, submitAskAnswer(text) { submitted = text; },
  };
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('  function renderAskModal('), source.indexOf('  function openAskModal(')), context);
  vm.runInContext(source.slice(source.indexOf('  function handleAskSubmit('), source.indexOf('  askSubmit.addEventListener(')), context);
  context.renderAskModal(context.state.pendingAskQuestions);
  const custom = context.askQuestions.querySelector('.ask-custom');
  assert.equal(custom.tag, 'textarea'); assert.equal(custom.maxLength, undefined);
  const text = '详细说明'.repeat(6000) + '\n第二段：保留换行与末尾';
  custom.value = text; custom.events.input();
  assert.equal(context.askSubmit.disabled, false);
  context.handleAskSubmit();
  assert.ok(submitted.endsWith(text));
});

test('搜索窗口外已有回答时仍先确认处理方式', () => {
  const source = fs.readFileSync(path.join(__dirname, '../js/app/builtin.js'), 'utf8');
  const confirmed = [], sent = [];
  const chatInner = { children: [] };
  const block = { isConnected:true, getAttribute:()=>'20', parentElement:chatInner };
  const context = { chatInner, App: { historyQuestionAfterRound:()=>({round:21,text:'【回答模型提问】\n已回答'}), send:data=>sent.push(data) },
    showAskAnswerConfirm:(...args)=>confirmed.push(args) };
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('  function submitAskAnswer('),source.indexOf('  function showAskAnswerConfirm(')),context);
  context.submitAskAnswer('新回答',block);
  assert.equal(sent.length,0);
  assert.deepEqual(confirmed,[['新回答',20,true]]);
});

test('自动弹窗的最新提问续答按普通新轮发送，避免误入历史插入分支', () => {
  const source = fs.readFileSync(path.join(__dirname, '../js/app/builtin.js'), 'utf8');
  const sent = [], chatInner = { children: [] };
  const block = { isConnected:true, getAttribute:()=>'2', parentElement:chatInner };
  const context = { chatInner, App: { historyQuestionAfterRound:()=>null, send:data=>sent.push(data) },
    showAskAnswerConfirm:()=>assert.fail('最新提问无需再次确认') };
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('  function submitAskAnswer('),source.indexOf('  function showAskAnswerConfirm(')),context);
  context.submitAskAnswer('【回答模型提问】\n完整回答',block);
  assert.equal(sent.length,1);
  assert.equal(sent[0].text,'【回答模型提问】\n完整回答');
  assert.equal(sent[0].insertAfterRound,undefined);
});
