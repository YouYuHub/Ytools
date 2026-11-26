const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { JSDOM } = require('jsdom');

// ask_user 补充说明输入框：无论几题都在弹窗末尾追加一个可选补充框；
// 非空（去空白/零宽字符后）时回答文本追加「【补充说明】」段落，否则不出现。
const source = () => fs.readFileSync(path.join(__dirname, '../js/app/builtin.js'), 'utf8');

function harness(questions) {
  const src = source();
  const dom = new JSDOM('<div id="askQuestions"></div>');
  const { document } = dom.window;
  const askQuestions = document.querySelector('#askQuestions');
  const sent = [];
  const context = {
    document,
    Array, Number, JSON, Object, Math, String, RegExp,
    state: { pendingAskQuestions: questions, pendingAskBlock: null, streaming: false, sessionId: 's', streamingSession: '' },
    el: (tag, cls, text) => {
      const n = document.createElement(tag);
      if (cls) n.className = cls;
      if (text != null) n.textContent = text;
      return n;
    },
    askQuestions,
    askSubmit: { disabled: false },
    toast: () => {},
    closeAskModal: () => {},
    submitAskAnswer: (text) => sent.push(text),
    updateAskSubmitState: () => {},
  };
  vm.createContext(context);
  // renderAskModal → updateAskSubmitState 之前 + handleAskSubmit → 事件绑定之前
  vm.runInContext(src.slice(src.indexOf('function renderAskModal('), src.indexOf('function updateAskSubmitState(')), context);
  vm.runInContext(src.slice(src.indexOf('function handleAskSubmit('), src.indexOf('askSubmit.addEventListener')), context);
  context.renderAskModal(questions);
  return { dom, context, askQuestions, sent };
}

const QS2 = [{ question: '选甲还是乙？', options: ['甲', '乙'] }, { question: '还有别的吗' }];

test('渲染：任意题数都追加且仅追加一个补充输入框（附件区之前）', t => {
  for (const qs of [[{ question: '单题' }], QS2, [1, 2, 3, 4, 5].map(i => ({ question: '第' + i + '题' }))]) {
    const h = harness(qs);
    t.after(() => h.dom.window.close());
    const fields = h.askQuestions.querySelectorAll('.ask-supplement-input');
    assert.equal(fields.length, 1);
    // 顺序：题目 → 补充说明 → 附件区（附件区始终在末尾）
    const kids = Array.from(h.askQuestions.children).map((n) => n.className);
    assert.ok(kids.indexOf('ask-supplement') < kids.indexOf('ask-attachments'));
    assert.ok(h.askQuestions.lastElementChild.classList.contains('ask-attachments'));
    // 正式题目数量不受影响
    assert.equal(h.askQuestions.querySelectorAll('.ask-question').length, qs.length);
  }
});

test('提交：补充非空时回答附「【补充说明】」段落', t => {
  const h = harness(QS2);
  t.after(() => h.dom.window.close());
  h.askQuestions.querySelector('.ask-supplement-input').value = '  另外请顺便更新文档  ';
  h.context.handleAskSubmit();
  assert.equal(h.sent.length, 1);
  const text = h.sent[0];
  assert.ok(text.startsWith('【回答模型提问】'));
  assert.ok(text.includes('\n\n【补充说明】\n另外请顺便更新文档'), text);
});

test('提交：留空/纯空白/零宽字符一律不附加补充说明', t => {
  for (const bad of ['', '   ', '\n\t \n', '\u200b\u200c\u200d\ufeff', ' \u200b ']) {
    const h = harness([{ question: 'q' }]);
    t.after(() => h.dom.window.close());
    h.askQuestions.querySelector('.ask-supplement-input').value = bad;
    h.context.handleAskSubmit();
    assert.equal(h.sent.length, 1);
    assert.ok(!h.sent[0].includes('【补充说明】'), JSON.stringify(bad) + ' 不应产生补充段');
  }
});

test('提交门禁：补充框不算题目——只有补充框内容也不改变可提交性', t => {
  const src = source();
  const context = {
    askQuestions: { querySelectorAll: sel => sel === '.ask-question' ? [{}, {}] : [] },
    askSubmit: {},
  };
  vm.createContext(context);
  vm.runInContext(src.slice(src.indexOf('function updateAskSubmitState('), src.indexOf('function openAskModal(')), context);
  context.updateAskSubmitState();
  assert.equal(context.askSubmit.disabled, false);
});

test('重新打开弹窗：补充框重置为空（renderAskModal 全量重建）', t => {
  const h = harness(QS2);
  t.after(() => h.dom.window.close());
  h.askQuestions.querySelector('.ask-supplement-input').value = '旧补充';
  h.context.renderAskModal(QS2);
  assert.equal(h.askQuestions.querySelector('.ask-supplement-input').value, '');
  assert.equal(h.askQuestions.querySelectorAll('.ask-supplement-input').length, 1);
});
