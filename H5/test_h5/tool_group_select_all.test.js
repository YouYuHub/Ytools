const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');

// 分组一键全选/取消回归（js/app/tools.js 整模块加载 + 真实 DOM）：
// 当前模型不支持视觉时 read_media 置灰禁用，但分组全选的分母必须只计
// 可选工具——否则全选后分组框永远是半选态（此前缺陷），用户无法一键全选。
const SOURCE = fs.readFileSync(path.join(__dirname, '../js/app/tools.js'), 'utf8');

const BUILTINS = [
  { name: 'todo_write', description: '任务计划' },
  { name: 'read_media', description: '读取媒体' },
  { name: 'run_command', description: '终端命令' },
];

function setup({ vision = true, selected = [] } = {}) {
  const dom = new JSDOM(
    '<div id="toolModal"><div class="tool-modal-toolbar"></div>' +
    '<div id="toolConfigContent"><div id="toolGroups"></div></div>' +
    '<input id="toolSearchInput"><span id="toolSelected"></span>' +
    '<button id="toolCollapseAll"></button><button id="toolRefresh"></button>' +
    '<div id="toolTip"></div><button id="toolFollowGlobal"></button></div>',
    { runScripts: 'outside-only' });
  const { window } = dom;
  const { document } = window;
  const byId = {};
  ['toolModal', 'toolGroups', 'toolSearchInput', 'toolSelected', 'toolCollapseAll',
   'toolRefresh', 'toolTip', 'toolFollowGlobal', 'toolConfigContent'].forEach(id => {
    byId['#' + id] = document.getElementById(id);
  });
  const state = {
    sessionId: null,
    tools: [],
    servers: [],
    failedServers: [],
    selectedTools: new Set(selected),
    draftTools: new Set(),
    toolSelectionOverridden: false,
  };
  window.App = {
    state,
    BUILTIN_SERVER_KEY: '__builtin__',
    BUILTIN_TOOLS: BUILTINS,
    isBuiltinToolName: () => true,
    toolIconSvg: () => '<svg></svg>',
    getChatModelVision: () => vision,
    refreshChatModelLabel: async () => true,
    refreshContextTokenStats: () => {},
    hideToolTip: () => {},
    closeToolModal: () => {},
    toast: () => {},
    el(tag, cls, text) {
      const n = document.createElement(tag);
      if (cls) n.className = cls;
      if (text != null) n.textContent = text;
      return n;
    },
    $(sel) {
      if (!byId[sel]) {
        byId[sel] = document.createElement('div');
        document.body.appendChild(byId[sel]);
      }
      return byId[sel];
    },
  };
  ['toolModal', 'toolGroups', 'toolSearchInput', 'toolSelected', 'toolCollapseAll',
   'toolRefresh', 'toolTip', 'toolFollowGlobal'].forEach(key => {
    window.App[key] = document.getElementById(key);
  });
  window.eval(SOURCE);
  return { window, document, state, App: window.App, close: () => window.close() };
}

function builtinGroup(document) {
  const group = document.querySelector('.tool-group');
  return {
    group,
    check: group.querySelector('.tool-group-check'),
    count: group.querySelector('.tool-group-count'),
    rows: Array.from(group.querySelectorAll('.tool-row')),
  };
}

function setChecked(window, checkbox, checked) {
  checkbox.checked = checked;
  checkbox.dispatchEvent(new window.Event('change', { bubbles: true }));
}

test('无视觉模型：一键全选只选中可选工具，分组框为全选态（非半选）', async t => {
  const x = setup({ vision: false });
  t.after(x.close);
  await x.App.openToolModal();
  const g = builtinGroup(x.document);
  assert.equal(g.rows.length, 3);
  // read_media 行置灰禁用
  const mediaRow = g.rows.find(r => r.dataset.toolName === 'read_media');
  assert.equal(mediaRow.querySelector('.tool-row-check').disabled, true);
  assert.match(g.count.textContent, /0 \/ 2/);
  // 一键全选
  setChecked(x.window, g.check, true);
  assert.deepEqual([...x.state.draftTools].sort(), ['run_command', 'todo_write']);
  assert.equal(x.state.draftTools.has('read_media'), false);
  assert.equal(g.check.checked, true);
  assert.equal(g.check.indeterminate, false);
  assert.equal(g.count.textContent, '2 / 2');
  assert.match(g.count.title, /1 项当前模型不可用/);
  assert.match(g.check.title, /全部可选工具/);
});

test('无视觉模型：一键取消清空全部已选', async t => {
  const x = setup({ vision: false, selected: ['todo_write', 'run_command'] });
  t.after(x.close);
  await x.App.openToolModal();
  const g = builtinGroup(x.document);
  assert.equal(g.check.checked, true);
  setChecked(x.window, g.check, false);
  assert.equal(x.state.draftTools.size, 0);
  assert.equal(g.check.checked, false);
  assert.equal(g.check.indeterminate, false);
  assert.equal(g.count.textContent, '0 / 2');
});

test('无视觉模型：部分选中时分组框为半选态', async t => {
  const x = setup({ vision: false, selected: ['todo_write'] });
  t.after(x.close);
  await x.App.openToolModal();
  const g = builtinGroup(x.document);
  assert.equal(g.check.checked, false);
  assert.equal(g.check.indeterminate, true);
  assert.equal(g.count.textContent, '1 / 2');
});

test('有视觉模型：read_media 参与全选，分母为全部工具', async t => {
  const x = setup({ vision: true });
  t.after(x.close);
  await x.App.openToolModal();
  const g = builtinGroup(x.document);
  const mediaRow = g.rows.find(r => r.dataset.toolName === 'read_media');
  assert.equal(mediaRow.querySelector('.tool-row-check').disabled, false);
  assert.equal(g.count.textContent, '0 / 3');
  setChecked(x.window, g.check, true);
  assert.deepEqual([...x.state.draftTools].sort(), ['read_media', 'run_command', 'todo_write']);
  assert.equal(g.check.checked, true);
  assert.equal(g.check.indeterminate, false);
  assert.equal(g.count.textContent, '3 / 3');
  assert.equal(g.check.title, '选择此服务下的全部工具');
});

test('无视觉模型：草稿预选 read_media 打开弹窗后被剔除（已有行为守护）', async t => {
  const x = setup({ vision: false, selected: ['read_media', 'todo_write'] });
  t.after(x.close);
  await x.App.openToolModal();
  assert.equal(x.state.draftTools.has('read_media'), false);
  assert.deepEqual([...x.state.draftTools], ['todo_write']);
  const g = builtinGroup(x.document);
  assert.equal(g.check.indeterminate, true);
});
