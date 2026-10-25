const test = require('node:test'), assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), { JSDOM } = require('jsdom');
const root = path.join(__dirname, '..'), settle = () => new Promise(resolve => setImmediate(resolve));

async function setup(t, status = {}, configure = {}) {
  const dom = new JSDOM(fs.readFileSync(path.join(root, 'account.html'), 'utf8'),
    { url: 'http://localhost/ui/account.html', runScripts: 'outside-only' });
  t.after(() => dom.window.close());
  const w = dom.window, calls = [], notices = [], lost = [];
  w.confirm = () => configure.confirm !== false;
  const auth = { user: { username: 'fixture', password_change_required: !!configure.initial }, lost: reason => lost.push(reason) };
  const api = async (url, method, data) => {
    calls.push({ url, method, data });
    if (url === '/auth/status') return { username_change: configure.available !== false };
    if (method === 'POST') {
      if (configure.failure) throw new Error(configure.failure);
      return { username: data.username, relogin_required: true };
    }
    return { username: 'fixture', can_change: true, mfa_required: false, mfa_setup_required: false,
      next_change_at: 0, cooldown_days: 30, ...status };
  };
  w.eval(fs.readFileSync(path.join(root, 'js/username_management.js'), 'utf8'));
  const manager = w.YtoolsUsernameManagement.create(auth, api, (text, error) => notices.push({ text, error }));
  await manager.load();
  const form = w.document.getElementById('usernameForm');
  form.elements.username.value = 'New.Fixture'; form.elements.password.value = 'fixture password'; form.elements.code.value = '123456';
  async function submit() { form.dispatchEvent(new w.Event('submit', { cancelable: true })); await settle(); }
  return { w, calls, notices, lost, form, submit, el: id => w.document.getElementById(id) };
}

test('unsupported backends and initial-password accounts keep the username entry hidden', async t => {
  const old = await setup(t, {}, { available: false });
  assert.equal(old.el('usernameSettings').hidden, true); assert.equal(old.calls.length, 1);
  const initial = await setup(t, {}, { initial: true });
  assert.equal(initial.calls.length, 0); assert.equal(initial.el('usernameSettings').hidden, true);
});

test('members without MFA use a password and enrolled accounts require a factor', async t => {
  const member = await setup(t); assert.equal(member.el('usernameSettings').hidden, false);
  assert.equal(member.el('usernameCodeLabel').hidden, true); assert.equal(member.form.elements.code.required, false);
  const enrolled = await setup(t, { mfa_required: true });
  assert.equal(enrolled.form.elements.code.required, true); assert.equal(enrolled.el('usernameCodeLabel').hidden, false);
});

test('privileged accounts needing enrollment and accounts on cooldown cannot submit', async t => {
  for (const status of [{ mfa_setup_required: true, can_change: false, mfa_required: true },
    { can_change: false, next_change_at: Date.now() / 1000 + 86400 }]) {
    const page = await setup(t, status); await page.submit();
    assert.equal(page.el('usernameSubmit').disabled, true);
    assert.equal(page.calls.filter(call => call.method === 'POST').length, 0);
    assert.ok(page.el('usernameStatus').textContent);
  }
});

test('rename confirms the normalized name, clears secrets and redirects to login with a notice', async t => {
  const page = await setup(t, { mfa_required: true });
  let confirmation; page.w.confirm = text => { confirmation = text; return true; };
  await page.submit();
  const sent = page.calls.find(call => call.method === 'POST');
  assert.deepEqual(JSON.parse(JSON.stringify(sent.data)), { username: 'new.fixture', password: 'fixture password', code: '123456' });
  assert.match(confirmation, /new\.fixture/); assert.match(confirmation, /30 天/);
  assert.deepEqual(page.lost, ['username_changed']); assert.equal(page.form.elements.password.value, '');
  assert.equal(page.form.elements.code.value, ''); assert.equal(page.w.localStorage.length, 0);
});

test('cancel, unchanged names and malformed names never send a mutation', async t => {
  const page = await setup(t, {}, { confirm: false }); await page.submit();
  page.form.elements.username.value = 'FIXTURE'; await page.submit();
  page.form.elements.username.value = '../invalid'; await page.submit();
  assert.equal(page.calls.filter(call => call.method === 'POST').length, 0);
  assert.equal(page.form.elements.password.value, 'fixture password');
});

test('failed MFA retains the login and password for retry, and page exit clears secrets', async t => {
  const page = await setup(t, { mfa_required: true }, { failure: '验证码无效或已使用' }); await page.submit();
  assert.deepEqual(page.lost, []); assert.match(page.notices.at(-1).text, /验证码无效/);
  assert.equal(page.form.elements.password.value, 'fixture password');
  assert.equal(page.w.document.activeElement, page.form.elements.code); assert.equal(page.el('usernameSubmit').disabled, false);
  page.w.dispatchEvent(new page.w.Event('pagehide'));
  assert.equal(page.form.elements.password.value, ''); assert.equal(page.form.elements.code.value, '');
});
