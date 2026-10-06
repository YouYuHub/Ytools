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
