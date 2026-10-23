const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {JSDOM} = require('jsdom');

function setup(t) {
  const dom = new JSDOM('<main></main>', {runScripts: 'outside-only'});
  t.after(() => dom.window.close());
  const w = dom.window;
  w.App = {state: {sessionId: 's'}};
  w.FormatUtils = {fmtTime: String};
  w.eval(fs.readFileSync(path.join(__dirname, '../js/app/process_groups.js'), 'utf8'));
  function stage(kind, headClass, parent) {
    const node = w.document.createElement('section'); node.className = kind;
    const head = w.document.createElement('button'); head.className = headClass;
    head.textContent = '工具标题'; head.title = '原有说明'; node.append(head);
    (parent || w.document.querySelector('main')).append(node);
    return {node, head};
  }
  return {w, stage, set: w.App.processGroups.setTimestamp};
}

test('工具标题只保留 hover 提示；刷新时间不产生右侧标签或重复说明', t => {
  const h = setup(t), tool = h.stage('tool-block', 'tool-block-head');
  h.set(tool.node, '10-24 00:42:12');
  assert.equal(tool.node.querySelectorAll('.stage-hover-time').length, 0);
  assert.equal(tool.head.title, '原有说明\n10-24 00:42:12');
  h.set(tool.node, '10-24 00:43:00');
  assert.equal(tool.head.title, '原有说明\n10-24 00:43:00');
  assert.equal(tool.node.dataset.stageTimestamp, '10-24 00:43:00');
});

test('更新工具标题会清除旧时间标签；子任务内工具同样处理', t => {
  const h = setup(t), child = h.stage('agent-block', 'agent-head');
  const tool = h.stage('tool-block', 'tool-block-head', child.node);
  tool.head.insertAdjacentHTML('beforeend', '<time class="stage-hover-time">旧标签</time>');
  h.set(tool.node, '10-24 00:42:12');
  assert.equal(tool.head.querySelector('.stage-hover-time'), null);
  assert.match(tool.head.title, /00:42:12/);
});

test('其他过程与正文的现有时间规则保持有效', t => {
  const h = setup(t), think = h.stage('think-block', 'think-toggle');
  h.set(think.node, '10-24 00:42:12');
  assert.equal(think.head.querySelector('.stage-hover-time').textContent, '10-24 00:42:12');
  const body = h.stage('msg-assistant-body', 'content');
  body.node.removeAttribute('title'); h.set(body.node, '10-24 00:42:12');
  assert.equal(body.node.getAttribute('title'), null);
  assert.equal(body.node.querySelector('.stage-hover-time'), null);
});
