const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');
const source = fs.readFileSync(path.join(__dirname, '../js/app/user_profile.js'), 'utf8');

function setup(t, getProfile) {
  const dom = new JSDOM('<button id="profileCard"><span id="profileName">访客用户</span></button>',
    { url: 'http://localhost/ui/', runScripts: 'outside-only' });
  t.after(() => dom.window.close());
  const w = dom.window;
  w.App = { profileName: w.document.getElementById('profileName') };
  w.YtoolsAuth = { user: { id: 'fixture-user', username: 'alice', name: '初始显示名' },
    update(data) { Object.assign(this, data); } };
  w.API = { getUserProfile: getProfile };
  w.eval(source);
  return w;
}

test('sidebar name is read-only and its click reaches the settings button', t => {
  let reads = 0, clicks = 0;
  const w = setup(t, async () => { reads++; return { name: '已保存' }; });
  w.document.getElementById('profileCard').addEventListener('click', () => clicks++);
  w.App.profileName.click();
  assert.equal(clicks, 1);
  assert.equal(w.document.querySelector('input'), null);
  assert.equal(reads, 0);
  assert.equal(w.App.profileName.textContent, '初始显示名');
  assert.doesNotMatch(w.App.profileName.title, /修改|改名/);
});

test('returning via browser page cache refreshes the account display name', async t => {
  const w = setup(t, async () => ({ name: '<新的显示名>', default_name: 'alice' }));
  const event = new w.Event('pageshow');
  Object.defineProperty(event, 'persisted', { value: true });
  w.dispatchEvent(event);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(w.App.profileName.textContent, '<新的显示名>');
  assert.equal(w.App.profileName.children.length, 0);
  assert.equal(w.YtoolsAuth.user.name, '<新的显示名>');
  assert.equal(w.YtoolsAuth.user.id, 'fixture-user');
});

test('stale profile reads cannot undo a newer saved name and failures keep the displayed name', async t => {
  const pending = [];
  const w = setup(t, () => new Promise((resolve, reject) => pending.push({ resolve, reject })));
  const first = w.App.loadUserProfile(), second = w.App.loadUserProfile();
  pending[1].resolve({ name: '新名字' }); await second;
  pending[0].resolve({ name: '旧名字' }); await first;
  assert.equal(w.App.profileName.textContent, '新名字');
  const failed = w.App.loadUserProfile(); pending[2].reject(new Error('fixture offline')); await failed;
  assert.equal(w.App.profileName.textContent, '新名字');
});
