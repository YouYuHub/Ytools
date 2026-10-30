const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');
const settle = () => new Promise(resolve => setImmediate(resolve));

const FILES = [
  { key: 'a', path: '/repo/a.py', display_path: 'a.py', added: 3, removed: 1 },
  { key: 'b', path: '/repo/b.MD', display_path: 'b.MD', added: 1, removed: 1 },
  { key: 'c', path: '/repo/sub/c.js', display_path: 'sub/c.js', added: 2, removed: 0 },
  { key: 'd', path: '/repo/Makefile', display_path: 'Makefile', added: 1, removed: 0 },
];

function setup() {
  const dom = new JSDOM(`<body>
    <button id="fileChangesBtn"><span class="fh-badge hidden"></span></button>
    <div id="fileChangesPanel" class="hidden">
      <span id="fileChangesStats"></span>
      <div id="fileChangesList"></div>
      <div id="fileChangesFoot" class="hidden">
        <span class="fh-foot-actions">
          <span class="fh-filter-wrap">
            <button type="button" id="fileChangesFilterBtn">全部后缀 ▾</button>
            <div id="fileChangesFilterMenu" class="hidden"></div>
          </span>
        </span>
      </div>
    </div>
    <div id="fhConfirmModal" class="hidden">
      <div id="fhConfirmBackdrop"></div>
      <p id="fhConfirmMessage"></p>
      <button id="fhConfirmOk">确定</button>
      <button id="fhConfirmCancel">取消</button>
    </div>
  </body>`, { url: 'http://localhost/ui/', runScripts: 'outside-only' });
  const w = dom.window;
  const api = {
    currentFiles: FILES.slice(),
    listFileChanges: async () => ({ files: api.currentFiles, stats: { total: api.currentFiles.length, added: 7, removed: 2 } }),
    fileKeepAll: async () => ({ kept_count: 1, cleaned_count: 0, skipped: [] }),
  };
  w.App = {
    state: { sessionId: 's1' },
    el(tag, cls, text) { const n = w.document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; },
    toast() {}, closeMenus() {},
  };
  w.API = api;
  w.eval(fs.readFileSync(path.join(__dirname, '../js/app/filehistory.js'), 'utf8'));
  w.document.dispatchEvent(new w.Event('DOMContentLoaded'));
  return { w, api, close: () => w.close() };
}

function rowNames(w) {
  return Array.from(w.document.querySelectorAll('#fileChangesList .fh-file-name')).map(n => n.textContent);
}

function menuItems(w) {
  // 跨 realm 对象转纯数据再比对
  return JSON.parse(JSON.stringify(Array.from(
    w.document.querySelectorAll('#fileChangesFilterMenu .fh-filter-item')
  ).map(n => ({
    label: n.querySelector('.fh-filter-label').textContent,
    count: n.querySelector('.fh-filter-count').textContent,
    checked: n.querySelector('.fh-filter-check').textContent,
    selected: n.classList.contains('is-selected'),
  }))));
}

function clickMenuItem(w, label) {
  const item = Array.from(w.document.querySelectorAll('#fileChangesFilterMenu .fh-filter-item'))
    .find(n => n.querySelector('.fh-filter-label').textContent === label);
  assert.ok(item, '菜单应包含选项：' + label);
  item.click();
}

async function openPanel(x) {
  x.w.App.fileHistory.togglePanel();
  await settle(); await settle();
}

test('collectFileExts 按后缀统计个数并排序，无后缀沉底', t => {
  const x = setup(); t.after(x.close);
  const got = JSON.parse(JSON.stringify(x.w.App.fileHistory.collectFileExts(FILES)));
  assert.deepEqual(got, [
    { ext: 'js', count: 1 },
    { ext: 'md', count: 1 },
    { ext: 'py', count: 1 },
    { ext: '', count: 1 },
  ]);
  assert.deepEqual(JSON.parse(JSON.stringify(x.w.App.fileHistory.collectFileExts([]))), []);
});

test('matchExtFilter：空数组全命中，多选任一命中即可', t => {
  const x = setup(); t.after(x.close);
  const match = x.w.App.fileHistory.matchExtFilter;
  assert.equal(match(FILES[0], []), true);
  assert.equal(match(FILES[0], ['py']), true);
  assert.equal(match(FILES[1], ['md']), true); // 大小写不敏感
  assert.equal(match(FILES[2], ['py']), false);
  assert.equal(match(FILES[2], ['py', 'js']), true); // 多选
  assert.equal(match(FILES[3], ['py']), false);
  assert.equal(match(FILES[3], ['']), true); // Makefile 命中“无后缀”
  assert.equal(match(FILES[0], ['']), false);
  assert.equal(match(FILES[3], ['py', '']), true);
  assert.equal(match({ display_path: '.env' }, ['']), true); // 纯点文件视为无后缀
  assert.equal(match({ display_path: 'a.tar.gz' }, ['gz']), true);
});

test('多选切换：菜单保持打开，按钮文案分级显示', async t => {
  const x = setup(); t.after(x.close);
  await openPanel(x);
  assert.deepEqual(rowNames(x.w), ['a.py', 'b.MD', 'c.js', 'Makefile']);
  const btn = x.w.document.getElementById('fileChangesFilterBtn');
  const menu = x.w.document.getElementById('fileChangesFilterMenu');
  btn.click();
  assert.equal(menu.classList.contains('hidden'), false);
  assert.deepEqual(menuItems(x.w).map(i => i.label), ['全部后缀', 'js', 'md', 'py', '(无后缀)']);
  assert.equal(menuItems(x.w)[0].checked, '✓');
  // 选 py：菜单不关，只剩 py
  clickMenuItem(x.w, 'py');
  assert.equal(menu.classList.contains('hidden'), false);
  assert.equal(btn.textContent, 'py ▾');
  assert.deepEqual(rowNames(x.w), ['a.py']);
  // 再选 md：两者都显示，文案按点选顺序
  clickMenuItem(x.w, 'md');
  assert.equal(menu.classList.contains('hidden'), false);
  assert.equal(btn.textContent, 'py, md ▾');
  assert.deepEqual(rowNames(x.w), ['a.py', 'b.MD']);
  // 再选 js：3 项收拢为“已选 N 项”
  clickMenuItem(x.w, 'js');
  assert.equal(btn.textContent, '已选 3 项 ▾');
  assert.deepEqual(rowNames(x.w), ['a.py', 'b.MD', 'c.js']);
  // 取消 py：剩 md + js
  clickMenuItem(x.w, 'py');
  assert.equal(btn.textContent, 'md, js ▾');
  assert.deepEqual(rowNames(x.w), ['b.MD', 'c.js']);
  // 全部后缀：清空并关闭菜单
  clickMenuItem(x.w, '全部后缀');
  assert.equal(menu.classList.contains('hidden'), true);
  assert.equal(btn.textContent, '全部后缀 ▾');
  assert.equal(btn.classList.contains('is-active'), false);
  assert.deepEqual(rowNames(x.w), ['a.py', 'b.MD', 'c.js', 'Makefile']);
});

test('点击菜单外部收起菜单，面板保持打开', async t => {
  const x = setup(); t.after(x.close);
  await openPanel(x);
  const menu = x.w.document.getElementById('fileChangesFilterMenu');
  x.w.document.getElementById('fileChangesFilterBtn').click();
  assert.equal(menu.classList.contains('hidden'), false);
  assert.equal(x.w.document.getElementById('fileChangesFilterBtn').getAttribute('aria-expanded'), 'true');
  x.w.document.getElementById('fileChangesStats').click(); // 面板内、菜单外
  assert.equal(menu.classList.contains('hidden'), true);
  assert.equal(x.w.document.getElementById('fileChangesFilterBtn').getAttribute('aria-expanded'), 'false');
  assert.equal(x.w.document.getElementById('fileChangesPanel').classList.contains('hidden'), false);
});

test('保留操作后：消失项剔除、剩余项保持，全消失才回到全部', async t => {
  const x = setup(); t.after(x.close);
  await openPanel(x);
  const btn = x.w.document.getElementById('fileChangesFilterBtn');
  btn.click();
  clickMenuItem(x.w, 'py');
  clickMenuItem(x.w, 'js');
  assert.equal(btn.textContent, 'py, js ▾');
  // 场景一：py 消失、js 仍在 → 保留 js
  x.api.currentFiles = [FILES[2]];
  x.w.App.fileHistory.keepAll();
  x.w.document.getElementById('fhConfirmOk').click();
  await settle(); await settle(); await settle();
  assert.equal(btn.textContent, 'js ▾');
  assert.deepEqual(rowNames(x.w), ['c.js']);
  // 场景二：js 也消失 → 回到全部
  x.api.currentFiles = [FILES[3]];
  x.w.App.fileHistory.keepAll();
  x.w.document.getElementById('fhConfirmOk').click();
  await settle(); await settle(); await settle();
  assert.equal(btn.textContent, '全部后缀 ▾');
  assert.deepEqual(rowNames(x.w), ['Makefile']);
});

test('Esc 三级处理：关菜单 → 清筛选 → 放行', async t => {
  const x = setup(); t.after(x.close);
  await openPanel(x);
  const btn = x.w.document.getElementById('fileChangesFilterBtn');
  const menu = x.w.document.getElementById('fileChangesFilterMenu');
  let bubbled = 0;
  x.w.addEventListener('keydown', e => { if (e.key === 'Escape') bubbled++; });
  const esc = () => x.w.document.dispatchEvent(new x.w.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  btn.click();
  clickMenuItem(x.w, 'py');
  assert.equal(menu.classList.contains('hidden'), false); // 多选不自动关菜单
  esc();
  assert.equal(menu.classList.contains('hidden'), true); // 第一级：仅关菜单
  assert.equal(btn.textContent, 'py ▾');
  assert.equal(bubbled, 0);
  esc();
  assert.equal(btn.textContent, '全部后缀 ▾'); // 第二级：仅清筛选
  assert.deepEqual(rowNames(x.w), ['a.py', 'b.MD', 'c.js', 'Makefile']);
  assert.equal(bubbled, 0);
  esc();
  assert.equal(bubbled, 1); // 第三级：干净态放行
});

test('菜单视口钳制：上方不足翻转向下，空间不足限高', async t => {
  const x = setup(); t.after(x.close);
  await openPanel(x);
  const btn = x.w.document.getElementById('fileChangesFilterBtn');
  const menu = x.w.document.getElementById('fileChangesFilterMenu');
  const wrap = menu.parentElement;
  Object.defineProperty(x.w, 'innerHeight', { value: 800, configurable: true });
  const setGeom = (top, bottom, naturalH) => {
    btn.getBoundingClientRect = () => ({ top, bottom, left: 0, right: 100, width: 100, height: bottom - top });
    Object.defineProperty(menu, 'scrollHeight', { get: () => naturalH, configurable: true });
  };
  // 场景一：上方 392px 放不下 500px 菜单，但下方 368 更小 → 不翻转，限高 240（能完整显示）
  setGeom(400, 424, 500);
  btn.click();
  assert.equal(wrap.classList.contains('is-below'), false);
  assert.equal(menu.style.maxHeight, '240px');
  // 场景二：上方 92 放不下、下方 668 更大 → 翻转向下
  btn.click();
  setGeom(100, 124, 500);
  btn.click();
  assert.equal(wrap.classList.contains('is-below'), true);
  assert.equal(menu.style.maxHeight, '240px');
  // 场景三：视口 300 高，上下都放不下 500 → 选大的一侧（上 142 > 下 118）并限高到可用空间
  Object.defineProperty(x.w, 'innerHeight', { value: 300, configurable: true });
  btn.click();
  setGeom(150, 174, 500);
  btn.click();
  assert.equal(wrap.classList.contains('is-below'), false);
  assert.equal(menu.style.maxHeight, '142px');
  // 场景四：visualViewport 存在时以其为准（offsetTop 100 + 高 500 → 可见 100~600）
  Object.defineProperty(x.w, 'visualViewport', {
    value: { offsetTop: 100, height: 500 }, configurable: true,
  });
  btn.click();
  setGeom(200, 224, 500); // 上 92 < 500，下 368 更大 → 翻转向下
  btn.click();
  assert.equal(wrap.classList.contains('is-below'), true);
  assert.equal(menu.style.maxHeight, '240px');
  // 场景五：翻转偏置——下方优势不足 96px（上 300 / 下 350）→ 不翻转，向上贴合
  delete x.w.visualViewport;
  Object.defineProperty(x.w, 'innerHeight', { value: 800, configurable: true });
  btn.click();
  setGeom(308, 442, 400);
  btn.click();
  assert.equal(wrap.classList.contains('is-below'), false);
  assert.equal(menu.style.maxHeight, '240px');
});

test('菜单打开/重建时选中项滚入可见', async t => {
  const x = setup(); t.after(x.close);
  await openPanel(x);
  const calls = [];
  x.w.Element.prototype.scrollIntoView = function (opts) {
    calls.push({ label: this.querySelector('.fh-filter-label').textContent, block: opts && opts.block });
  };
  const btn = x.w.document.getElementById('fileChangesFilterBtn');
  btn.click();
  assert.deepEqual(calls, [{ label: '全部后缀', block: 'nearest' }]); // 打开即对齐当前选中项
  clickMenuItem(x.w, 'py'); // 点选 → 重建菜单 → 钳制 → 选中项滚入可见
  assert.deepEqual(calls, [{ label: '全部后缀', block: 'nearest' }, { label: 'py', block: 'nearest' }]);
});

test('点选改行数后钳制按落定几何计算（先挂行后钳制）', async t => {
  const x = setup(); t.after(x.close);
  await openPanel(x);
  const btn = x.w.document.getElementById('fileChangesFilterBtn');
  const menu = x.w.document.getElementById('fileChangesFilterMenu');
  const list = x.w.document.getElementById('fileChangesList');
  const wrap = menu.parentElement;
  Object.defineProperty(x.w, 'innerHeight', { value: 700, configurable: true });
  // 模拟真实布局：按钮纵坐标随列表行数变化（空列表高位 150，有行低位 446）
  btn.getBoundingClientRect = () => {
    const top = list.children.length === 0 ? 150 : 446;
    return { top, bottom: top + 26, left: 0, right: 100, width: 100, height: 26 };
  };
  Object.defineProperty(menu, 'scrollHeight', { get: () => 600, configurable: true });
  btn.click(); // 打开菜单（4 行，按钮低位 → 上开）
  assert.equal(wrap.classList.contains('is-below'), false);
  // 点选 py：renderPanel 会先清空列表再重挂行；钳制必须按落定后几何（仍上开），
  // 若按清空瞬间的高位计算就会错误翻转向下（旧顺序的 bug）
  clickMenuItem(x.w, 'py');
  assert.equal(wrap.classList.contains('is-below'), false);
  assert.deepEqual(rowNames(x.w), ['a.py']);
});

test('开关菜单同几何复验不丢失限高（测量解除后必恢复）', async t => {
  const x = setup(); t.after(x.close);
  await openPanel(x);
  const btn = x.w.document.getElementById('fileChangesFilterBtn');
  const menu = x.w.document.getElementById('fileChangesFilterMenu');
  btn.click(); // 打开：落定限高
  assert.notEqual(menu.style.maxHeight, 'none');
  assert.notEqual(menu.style.maxHeight, '');
  const capped = menu.style.maxHeight;
  btn.click(); // 关闭（保留限高）
  btn.click(); // 重开：同几何命中指纹跳过方向切换，但限高必须仍在
  assert.equal(menu.style.maxHeight, capped);
  assert.notEqual(menu.style.maxHeight, 'none');
});
