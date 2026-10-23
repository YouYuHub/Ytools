const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function setup(initial = [], sessionId = 's') {
  let document;
  class Element {
    constructor(tag) {
      this.tag = tag; this.children = []; this.listeners = {}; this.attributes = {};
      this.className = ''; this.textContent = ''; this.isConnected = true;
      this.scrollHeight = 500; this.scrollTop = 0; this.clientHeight = 100;
      this.classList = {
        contains: key => this.className.split(' ').includes(key),
        toggle: (key, enabled) => {
          const classes = new Set(this.className.split(' ').filter(Boolean));
          if (enabled) classes.add(key); else classes.delete(key);
          this.className = [...classes].join(' ');
        },
      };
    }
    append(...nodes) { nodes.forEach(node => { this.children.push(node); node.parent = this; }); }
    replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
    addEventListener(type, callback) { this.listeners[type] = callback; }
    setAttribute(key, value) { this.attributes[key] = value; }
    focus() { document.activeElement = this; }
    remove() { this.isConnected = false; this.parent.children = this.parent.children.filter(child => child !== this); }
    querySelectorAll() { return []; }
    click() { return this.listeners.click({ target: this }); }
  }
  const button = new Element('button'), badge = new Element('span'); button.className = 'hidden';
  document = {
    hidden: false, activeElement: button, body: new Element('body'), listeners: {},
    createElement: tag => new Element(tag), createTextNode: text => ({ textContent: text }),
    getElementById: id => id === 'terminalJobsBtn' ? button : badge,
    addEventListener(type, callback) { this.listeners[type] = callback; },
  };
  const state = { jobs: initial, polls: [], operations: [], lists: [], scopes: [], listError: null, pending: {} };
  const api = {
    async listTerminalJobs(sid) {
      state.lists.push(sid);
      if (state.listError) throw state.listError;
      if (state.pendingList) return state.pendingList;
      return { jobs: state.jobs };
    },
    async getTerminalJob(id, sid) {
      state.scopes.push(['get', sid]);
      state.polls.push(id);
      if (state.pending[id]) return state.pending[id];
      return { ...state.jobs.find(job => job.job_id === id), output_tail: '<script>log ' + id + '</script>' };
    },
    async operateTerminalJob(id, operation, sid) {
      state.scopes.push([operation, sid]);
      state.operations.push([id, operation]);
      if (operation === 'close') state.jobs = state.jobs.filter(job => job.job_id !== id);
      else state.jobs = state.jobs.map(job => job.job_id === id ? { ...job, status: 'stopped', can_stop: false } : job);
      return { ...job(id), status: 'stopped', can_stop: false };
    },
    terminalLogUrl: (id, sid) => '/terminal/jobs/' + id + '/log/download?session_id=' + sid,
  };
  const timers = {};
  const window = { API: api, App: { state: { sessionId }, toast() {} }, listeners: {},
    addEventListener(type, callback) { this.listeners[type] = callback; } };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../js/app/terminals.js'), 'utf8'), {
    window, document, AbortController, setTimeout, clearTimeout,
    setInterval(fn, ms) { timers[ms] = fn; return ms; }, clearInterval(id) { delete timers[id]; },
  });
  function find(className, root = document.body) {
    if ((root.className || '').split(' ').includes(className)) return root;
    for (const node of root.children || []) { const found = find(className, node); if (found) return found; }
  }
  return { state, app: window.App, timers, button, badge, document, window, find };
}
function job(id, extra = {}) { return { job_id: id, command: '命令 ' + id, status: 'running', shell: 'sh', pid: 123,
  created_at: Date.now() / 1000, session_id: 's', cwd: '/', can_stop: true, ...extra }; }
const settle = () => new Promise(resolve => setImmediate(resolve));

test('只统计当前会话运行中的终端，过滤其他会话和结束记录', async () => {
  const ui = setup(); await settle();
  assert.equal(ui.button.classList.contains('hidden'), true);
  ui.state.jobs = [job('bg_one'), job('bg_two', { session_id: 'other' }),
    ...['completed', 'failed', 'stopped', 'unknown'].map(status => job('bg_' + status, { status }))];
  await ui.app.terminalJobs.noteToolResult('s', 'run_command');
  assert.equal(ui.button.classList.contains('hidden'), false);
  assert.equal(ui.badge.textContent, '1');
  assert.deepEqual(ui.state.lists, ['s', 's']);
  assert.match(fs.readFileSync(path.join(__dirname, '../index.html'), 'utf8'), /id="terminalJobsBtn"[^>]*>[\s\S]*?<svg/);
});

test('停止或停止并关闭最后一个终端都隐藏入口和面板', async () => {
  for (const operation of ['stop', 'close']) {
    const ui = setup([job('bg_one')]); await settle(); ui.button.click(); await settle();
    const log = ui.find('terminal-output');
    assert.equal(log.textContent, '<script>log bg_one</script>');
    assert.equal(log.innerHTML, undefined, '日志只写 textContent');
    assert.equal(ui.find('terminal-actions').children[2].href, '/terminal/jobs/bg_one/log/download?session_id=s');
    const controls = ui.find('terminal-actions');
    await controls.children[operation === 'stop' ? 0 : 1].click();
    assert.deepEqual(ui.state.operations, [['bg_one', operation]]);
    assert.ok(ui.state.scopes.every(item => item[1] === 's'));
    assert.equal(ui.button.classList.contains('hidden'), true);
    assert.equal(ui.find('terminal-overlay'), undefined);
  }
});

test('模型停止或进程退出后自动移出列表，无需用户关闭记录', async () => {
  for (const status of ['completed', 'failed', 'stopped', 'unknown']) {
    const ui = setup([job('bg_one')]); await settle(); ui.button.click(); await settle();
    ui.state.jobs = [job('bg_one', { status, can_stop: false })];
    await ui.timers[1000]();
    assert.equal(ui.button.classList.contains('hidden'), true);
    assert.equal(ui.find('terminal-overlay'), undefined);
  }
  const ui = setup([job('bg_one')]); await settle();
  ui.state.jobs[0].status = 'stopped'; await ui.timers[3000]();
  assert.equal(ui.button.classList.contains('hidden'), true, '面板关闭时也会检测');
});

test('停止后迟到的运行列表不能让已停止的终端重新出现', async () => {
  const ui = setup([job('bg_one')]); await settle(); ui.button.click(); await settle();
  let finish;
  ui.state.pendingList = new Promise(resolve => { finish = resolve; });
  const pending = ui.timers[3000]();
  await ui.find('terminal-stop').click();
  assert.equal(ui.button.classList.contains('hidden'), true);
  ui.state.pendingList = null;
  finish({ jobs: [job('bg_one')] }); await pending; await settle();
  assert.equal(ui.button.classList.contains('hidden'), true);
  assert.equal(ui.badge.textContent, '0');
});

test('正在查看的任务结束后自动选择剩余运行终端并读取其输出', async () => {
  const ui = setup([job('bg_one'), job('bg_two')]); await settle(); ui.button.click(); await settle();
  ui.state.jobs[0].status = 'completed';
  await ui.timers[1000](); await settle();
  assert.equal(ui.badge.textContent, '1');
  assert.equal(ui.button.classList.contains('hidden'), false);
  assert.match(ui.find('terminal-info').textContent, /命令 bg_two/);
  assert.match(ui.find('terminal-output').textContent, /bg_two/);
});

test('新对话不读取全局列表；切换会话立即清空入口和面板', async () => {
  const empty = setup([job('bg_other')], null); await settle();
  assert.deepEqual(empty.state.lists, []);
  assert.equal(empty.button.classList.contains('hidden'), true);
  const ui = setup([job('bg_one'), job('bg_other', { session_id: 'other' })]);
  await settle(); ui.button.click(); await settle();
  ui.app.state.sessionId = null; ui.app.terminalJobs.noteSessionChanged();
  assert.equal(ui.button.classList.contains('hidden'), true);
  assert.equal(ui.find('terminal-overlay'), undefined);
  ui.app.state.sessionId = 'other'; ui.app.terminalJobs.noteSessionChanged(); await settle();
  assert.equal(ui.badge.textContent, '1'); ui.button.click(); await settle();
  assert.match(ui.find('terminal-info').textContent, /命令 bg_other/);
  assert.equal(ui.find('terminal-header').children[0].textContent, '后台终端 · 当前会话');
});

test('会话切换后忽略旧会话迟到的列表和日志，即使旧请求失败也不会恢复入口', async () => {
  const ui = setup([job('bg_one')]); await settle();
  let finishList, finishOutput;
  ui.state.pending.bg_one = new Promise(resolve => { finishOutput = resolve; });
  ui.state.pendingList = new Promise(resolve => { finishList = resolve; });
  ui.button.click(); await settle();
  ui.app.state.sessionId = 'other'; ui.app.terminalJobs.noteSessionChanged();
  assert.equal(ui.button.classList.contains('hidden'), true);
  ui.state.pendingList = null;
  ui.state.jobs = [job('bg_other', { session_id: 'other' })];
  finishList({ jobs: [job('bg_one')] });
  finishOutput({ ...job('bg_one'), output_tail: '旧会话输出' }); await settle();
  assert.equal(ui.badge.textContent, '1'); ui.button.click(); await settle();
  assert.match(ui.find('terminal-output').textContent, /bg_other/);
  assert.doesNotMatch(ui.find('terminal-output').textContent, /旧会话输出/);
  ui.state.listError = new Error('offline');
  ui.app.state.sessionId = null; ui.app.terminalJobs.noteSessionChanged(); await settle();
  assert.equal(ui.button.classList.contains('hidden'), true);
});

test('旧任务不能误停，关闭按钮明确只关闭记录', async () => {
  const ui = setup([job('bg_old', { can_stop: false })]); await settle(); ui.button.click(); await settle();
  assert.equal(ui.find('terminal-stop').disabled, true);
  assert.match(ui.find('terminal-info').textContent, /旧任务/);
  assert.equal(ui.find('terminal-actions').children[1].textContent, '关闭记录');
});

test('切换终端后忽略上一个终端迟到的日志', async () => {
  const ui = setup([job('bg_one'), job('bg_two')]); await settle();
  let finish;
  ui.state.pending.bg_one = new Promise(resolve => { finish = resolve; });
  ui.button.click(); await settle();
  ui.find('terminal-list').children[1].click();
  finish({ ...job('bg_one'), output_tail: '旧终端迟到数据' }); await settle();
  await ui.timers[1000]();
  assert.equal(ui.find('terminal-output').textContent, '<script>log bg_two</script>');
});

test('向上浏览日志会暂停显示更新，恢复跟随后显示新日志', async () => {
  const ui = setup([job('bg_one')]); await settle(); ui.button.click(); await settle();
  const output = ui.find('terminal-output'), checkbox = ui.find('terminal-follow').children[0];
  output.listeners.wheel({ deltaY: -10 });
  assert.equal(checkbox.checked, false);
  ui.state.pending.bg_one = Promise.resolve({ ...job('bg_one'), output_tail: 'new output' });
  await ui.timers[1000]();
  assert.notEqual(output.textContent, 'new output');
  checkbox.checked = true; await checkbox.listeners.change();
  assert.equal(output.textContent, 'new output');
  assert.equal(output.scrollTop, 500);
});

test('短暂网络故障保留任务入口，隐藏页面暂停轮询', async () => {
  const ui = setup([job('bg_one')]); await settle();
  ui.state.listError = new Error('offline'); await ui.timers[3000]();
  assert.equal(ui.button.classList.contains('hidden'), false);
  ui.document.hidden = true; ui.button.click(); await settle();
  ui.document.listeners.visibilitychange();
  const count = ui.state.polls.length;
  assert.equal(ui.timers[1000], undefined);
  assert.equal(ui.timers[3000], undefined);
  assert.equal(ui.state.polls.length, count);
});

test('空会话检查一次后没有定时器，无关工具和其他会话不能唤醒轮询', async () => {
  const ui = setup(); await settle();
  assert.deepEqual(ui.state.lists, ['s']);
  assert.deepEqual(Object.keys(ui.timers), []);
  await ui.app.terminalJobs.noteToolResult('s', 'read_file');
  await ui.app.terminalJobs.noteToolResult('other', 'run_command');
  assert.deepEqual(ui.state.lists, ['s']);
  const empty = setup([], null); await settle();
  assert.deepEqual(Object.keys(empty.timers), []);
  assert.deepEqual(empty.state.lists, []);
});

test('后台命令结果唤醒列表轮询，输出仅在面板展开时轮询', async () => {
  const ui = setup(); await settle();
  ui.state.jobs = [job('bg_one')];
  await ui.app.terminalJobs.noteToolResult('s', 'run_command');
  assert.ok(ui.timers[3000]);
  assert.equal(ui.timers[1000], undefined);
  ui.button.click(); await settle();
  assert.ok(ui.timers[1000]);
  ui.find('terminal-header').children[1].click();
  assert.equal(ui.timers[1000], undefined);
  assert.ok(ui.timers[3000]);
  ui.state.jobs = [];
  await ui.timers[3000]();
  assert.deepEqual(Object.keys(ui.timers), []);
  assert.equal(ui.button.classList.contains('hidden'), true);
});

test('停止或进程结束移除最后一个任务后立即停止两种轮询', async () => {
  for (const path of ['stop', 'output', 'list']) {
    const ui = setup([job('bg_one')]); await settle(); ui.button.click(); await settle();
    if (path === 'stop') await ui.find('terminal-stop').click();
    else {
      ui.state.jobs[0].status = 'completed';
      await ui.timers[path === 'output' ? 1000 : 3000]();
    }
    assert.deepEqual(Object.keys(ui.timers), [], path);
  }
});

test('空闲状态重新聚焦发现新任务，隐藏与 bfcache 生命周期停用定时器', async () => {
  const ui = setup(); await settle();
  ui.state.jobs = [job('bg_one')];
  await ui.window.listeners.focus(); await settle();
  assert.ok(ui.timers[3000]);
  ui.button.click(); await settle();
  ui.document.hidden = true; ui.document.listeners.visibilitychange();
  assert.deepEqual(Object.keys(ui.timers), []);
  ui.document.hidden = false; ui.document.listeners.visibilitychange(); await settle();
  assert.ok(ui.timers[1000]); assert.ok(ui.timers[3000]);
  ui.window.listeners.pagehide();
  assert.deepEqual(Object.keys(ui.timers), []);
  ui.state.jobs = [];
  ui.window.listeners.pageshow({ persisted: true }); await settle();
  assert.deepEqual(Object.keys(ui.timers), []);
  assert.equal(ui.button.classList.contains('hidden'), true);
});

test('空列表请求期间到达命令结果会合并成一次后续检查，不丢失新任务', async () => {
  const ui = setup(); await settle();
  let resolve;
  ui.state.pendingList = new Promise(done => { resolve = done; });
  const pending = ui.app.terminalJobs.refresh();
  ui.state.jobs = [job('bg_one')];
  ui.app.terminalJobs.noteToolResult('s', 'run_command');
  ui.app.terminalJobs.noteToolResult('s', 'poll_command');
  ui.app.terminalJobs.noteToolResult('s', 'write_stdin');
  ui.state.pendingList = null;
  resolve({ jobs: [] }); await pending; await settle();
  assert.deepEqual(ui.state.lists, ['s', 's', 's']);
  assert.equal(ui.badge.textContent, '1');
  assert.ok(ui.timers[3000]);
});

test('没有已知任务的发现请求失败只重试两次，后续事件可重新恢复', async () => {
  const ui = setup(); await settle();
  ui.state.listError = new Error('offline');
  await ui.app.terminalJobs.noteToolResult('s', 'run_command');
  assert.ok(ui.timers[3000]);
  await ui.timers[3000]();
  await ui.timers[3000]();
  assert.deepEqual(ui.state.lists, ['s', 's', 's', 's']);
  assert.deepEqual(Object.keys(ui.timers), []);
  ui.state.listError = null; ui.state.jobs = [job('bg_one')];
  await ui.app.terminalJobs.noteToolResult('s', 'run_command');
  assert.ok(ui.timers[3000]);
});
