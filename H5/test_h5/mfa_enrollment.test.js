const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');
const root = path.join(__dirname, '..');
const vendor = fs.readFileSync(path.join(root, 'js/vendor/qrcode-generator/qrcode.js'), 'utf8');
const helper = fs.readFileSync(path.join(root, 'js/mfa_enrollment.js'), 'utf8');
const secret = 'JBSWY3DPEHPK3PXP'; // Disposable fixture; no real account.
const fixture = () => ({ secret, uri: 'otpauth://totp/Ytools:fixture?secret=' + secret + '&issuer=Ytools', expires_at: Date.now() / 1000 + 300 });
const settle = () => new Promise(resolve => setImmediate(resolve));
function setup(withQr = true) {
  const dom = new JSDOM('<div id="binding" hidden></div>', { url: 'http://localhost/ui/login.html', runScripts: 'outside-only' });
  const w = dom.window;
  if (withQr) w.eval(vendor);
  w.eval(helper);
  return { dom, w, el: w.document.getElementById('binding') };
}
test('vendored encoder matches the exact pinned dependency', () => {
  assert.equal(vendor, fs.readFileSync(require.resolve('qrcode-generator'), 'utf8'));
  assert.equal(require('../package.json').dependencies['qrcode-generator'], '2.0.4');
});
test('QR is generated offline, supports both apps, and secrets clear on completion', () => {
  const { dom, w, el } = setup();
  w.fetch = () => assert.fail('QR must never call an external service');
  const binding = w.YtoolsMfaEnrollment.create(el);
  binding.show(fixture());
  assert.match(el.querySelector('img').src, /^data:image\/gif;base64,/);
  assert.match(el.textContent, /Microsoft Authenticator.*其他帐户/);
  assert.match(el.textContent, /Google Authenticator/);
  assert.equal(el.querySelector('details').open, false);
  assert.equal(w.localStorage.length, 0); assert.equal(w.sessionStorage.length, 0);
  binding.clear();
  assert.equal(el.querySelector('img').getAttribute('src'), null);
  assert.equal(el.querySelector('code').textContent, ''); assert.equal(el.hidden, true);
  dom.window.close();
});
test('missing QR encoder retains a usable manual enrollment fallback', () => {
  const { dom, w, el } = setup(false);
  const binding = w.YtoolsMfaEnrollment.create(el); binding.show(fixture());
  assert.equal(el.querySelector('img').getAttribute('src'), null);
  assert.equal(el.querySelector('details').open, true); assert.equal(el.querySelector('code').textContent, secret);
  assert.match(el.textContent, /二维码暂不可用/); binding.clear(); dom.window.close();
});
test('expiry and browser back navigation remove QR and manual key', () => {
  const { dom, w, el } = setup();
  let now = Date.now(), tick; const reasons = [];
  w.Date.now = () => now; w.setInterval = callback => { tick = callback; return 1; }; w.clearInterval = () => {};
  const binding = w.YtoolsMfaEnrollment.create(el, reason => reasons.push(reason));
  binding.show({ ...fixture(), expires_at: now / 1000 + 2 }); now += 3000; tick();
  assert.deepEqual(reasons, ['expired']); assert.equal(el.querySelector('code').textContent, '');
  binding.show({ ...fixture(), expires_at: now / 1000 + 300 });
  w.dispatchEvent(new w.Event('pagehide'));
  assert.equal(el.querySelector('img').getAttribute('src'), null);
  w.dispatchEvent(new w.PageTransitionEvent('pageshow', { persisted: true }));
  assert.deepEqual(reasons, ['expired', 'return']); dom.window.close();
});
test('non-TOTP and mismatched provisioning data is rejected before rendering', () => {
  const { dom, w, el } = setup(); const binding = w.YtoolsMfaEnrollment.create(el);
  assert.throws(() => binding.show({ ...fixture(), uri: 'https://example.com/?secret=' + secret }), /绑定信息无效/);
  assert.throws(() => binding.show({ ...fixture(), uri: fixture().uri + '&digits=8' }), /绑定信息无效/);
  assert.equal(el.querySelector('img').getAttribute('src'), null); dom.window.close();
});
async function loginSetup(t) {
  const dom = new JSDOM(fs.readFileSync(path.join(root, 'login.html'), 'utf8'), { url: 'http://localhost/ui/login.html', runScripts: 'outside-only' });
  t.after(() => dom.window.close());
  const w = dom.window, calls = [];
  w.fetch = async (url, options) => {
    calls.push({ url, options }); let data;
    if (url === '/auth/status') data = { initialized: true };
    else if (url === '/auth/me') data = { csrf: 'fixture', mfa_verified: false, user: { mfa_enabled: false } };
    else if (url === '/auth/mfa/start') data = fixture();
    else if (url === '/auth/mfa/confirm') data = { csrf: 'rotated', recovery_codes: ['fixture-recovery'] };
    else assert.fail('unexpected request: ' + url);
    return { ok: true, status: 200, json: async () => data };
  };
  w.eval(vendor); w.eval(helper); w.eval(fs.readFileSync(path.join(root, 'js/recovery_codes.js'), 'utf8'));
  w.eval(fs.readFileSync(path.join(root, 'js/login_mfa_management.js'), 'utf8'));
  w.eval(fs.readFileSync(path.join(root, 'login.js'), 'utf8'));
  await settle(); return { dom, w, calls };
}
test('login enrollment switches from password to QR/code and clears QR after confirmation', async t => {
  const { dom, w, calls } = await loginSetup(t), form = w.document.getElementById('enrollForm');
  form.elements.password.value = 'fixture-password-long';
  form.dispatchEvent(new w.Event('submit', { cancelable: true })); await settle();
  assert.equal(form.elements.password.value, ''); assert.equal(form.elements.password.required, false);
  assert.equal(form.elements.code.required, true);
  assert.match(w.document.querySelector('#mfaEnrollment img').src, /^data:image\/gif/);
  form.elements.code.value = '123456'; form.dispatchEvent(new w.Event('submit', { cancelable: true })); await settle();
  assert.equal(w.document.querySelector('#mfaEnrollment img').getAttribute('src'), null);
  assert.equal(w.document.querySelector('#mfaEnrollment code').textContent, '');
  assert.equal(w.document.getElementById('recovery').hidden, false);
  assert.equal(calls.filter(call => call.url === '/auth/mfa/confirm').length, 1); dom.window.close();
});
test('regenerate asks for password again and does not silently issue a new secret', async t => {
  const { dom, w, calls } = await loginSetup(t), form = w.document.getElementById('enrollForm');
  form.elements.password.value = 'fixture-password-long'; form.dispatchEvent(new w.Event('submit', { cancelable: true })); await settle();
  w.document.querySelector('#mfaEnrollment .mfa-restart').click(); await settle();
  assert.equal(w.document.getElementById('enrollPassword').hidden, false);
  assert.equal(form.elements.password.required, true); assert.equal(form.elements.code.required, false);
  assert.equal(calls.filter(call => call.url === '/auth/mfa/start').length, 1); dom.window.close();
});
test('both protected pages load encoder and enrollment helper before their controller', () => {
  for (const [page, controller] of [['login.html', 'login.js?v='], ['account.html', 'js/account.js?v=']]) {
    const html = fs.readFileSync(path.join(root, page), 'utf8');
    assert.ok(html.indexOf('vendor/qrcode-generator/qrcode.js?v=') < html.indexOf('js/mfa_enrollment.js?v='));
    assert.ok(html.indexOf('js/mfa_enrollment.js?v=') < html.indexOf(controller));
  }
});
