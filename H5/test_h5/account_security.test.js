const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');
const settle = () => new Promise(resolve => setImmediate(resolve));
function setup(changeRequired, mfaEnabled = true, role = 'admin', search = '', managedUsers = []) {
  const dom = new JSDOM(fs.readFileSync(path.join(__dirname, '../account.html'), 'utf8'), { url: 'http://localhost/ui/account.html'+search, runScripts: 'outside-only' });
  const w = dom.window, calls = [], user = { id: 'admin-id', name: 'Admin', username: 'admin', role, password_change_required: changeRequired, mfa_enabled: mfaEnabled };
  let authenticators = mfaEnabled ? [{ id: 'primary', label: '原有验证器', primary: true }] : [], operation = null;
  w.YtoolsAuth = { user, executionAllowed: true, update(data) { Object.assign(this, data); }, logout: async () => {}, lost() {} };
  w.fetch = async (url, options) => {
    calls.push({ url, options });
    let data = {};
    if (url === '/auth/sessions') data = { sessions: [], current: 'login-id' };
    if (url === '/admin/users') data = options.method === 'POST' ? { id: 'created' } : { users: managedUsers };
    if (url.endsWith('/limits')) data = { limits: { daily_calls: 500, daily_tokens: 10000, storage_bytes: 2 * (1024 ** 3), concurrent_models: 3 } };
    if (url === '/admin/audit') data = { events: [] };
    if (url === '/user/usage') data = { used: { daily_calls: 0, daily_tokens: 0 } };
    if (url === '/user/profile' && options.method === 'POST') data = { state: 'succeed', name: JSON.parse(options.body).name.trim() };
    if (url === '/auth/mfa/start') data = { secret: 'JBSWY3DPEHPK3PXP', uri: 'otpauth://totp/Ytools:fixture?secret=JBSWY3DPEHPK3PXP&issuer=Ytools', expires_at: Date.now() / 1000 + 300 };
    if (url === '/auth/mfa/confirm') data = { recovery_codes: ['fixture-recovery'] };
    if (url === '/auth/mfa/devices') data = { devices: authenticators, recovery_codes_remaining: 1 };
    if (url === '/auth/mfa/recovery/regenerate') data = { csrf:'rotated', recovery_codes:['fixture-regenerated-recovery'] };
    if (url === '/auth/mfa/manage/start') {
      const payload = JSON.parse(options.body); operation = { operation_id: 'c'.repeat(32), mode: payload.mode, label: payload.label,
        secret: 'JBSWY3DPEHPK3PXP', uri: 'otpauth://totp/Ytools:fixture?secret=JBSWY3DPEHPK3PXP&issuer=Ytools', expires_at: Date.now() / 1000 + 300 };
      data = { binding: operation, csrf: 'rotated' };
    }
    if (url === '/auth/mfa/manage/confirm') {
      authenticators = operation.mode === 'replace' ? [{ id: 'primary', label: operation.label, primary: true }] : [...authenticators, { id: operation.operation_id, label: operation.label, primary: false }];
      data = { devices: authenticators, mode: operation.mode, recovery_codes: operation.mode === 'replace' ? ['fixture-new-recovery'] : [] };
    }
    if (url.startsWith('/auth/mfa/devices/')) authenticators = authenticators.filter(device => !url.endsWith(device.id));
    return { ok: true, json: async () => data };
  };
  w.eval(fs.readFileSync(path.join(__dirname, '../js/vendor/qrcode-generator/qrcode.js'), 'utf8'));
  w.eval(fs.readFileSync(path.join(__dirname, '../js/mfa_enrollment.js'), 'utf8'));
  w.eval(fs.readFileSync(path.join(__dirname, '../js/recovery_codes.js'), 'utf8'));
  w.eval(fs.readFileSync(path.join(__dirname, '../js/mfa_management.js'), 'utf8'));
  w.eval(fs.readFileSync(path.join(__dirname, '../js/username_management.js'), 'utf8'));
  w.eval(fs.readFileSync(path.join(__dirname, '../js/account.js'), 'utf8'));
  return { dom, w, calls };
}

test('account settings saves the display name and immediately refreshes the account heading', async t => {
  const {dom,w,calls}=setup(false); t.after(()=>dom.window.close()); await settle();
  const originalId=w.YtoolsAuth.user.id, form=w.document.getElementById('profileForm');
  form.elements.name.value='  新显示名  ';
  form.dispatchEvent(new w.Event('submit',{cancelable:true})); await settle();
  const update=calls.find(call=>call.url==='/user/profile'&&call.options.method==='POST');
  assert.ok(update); assert.equal(w.YtoolsAuth.user.name,'新显示名');
  assert.equal(w.YtoolsAuth.user.username,'admin'); assert.equal(w.YtoolsAuth.user.id,originalId);
  assert.equal(form.elements.name.value,'新显示名');
  assert.equal(w.document.getElementById('accountName').textContent,'新显示名 · admin');
  assert.equal(w.document.getElementById('accountMessage').textContent,'显示名已保存');
});

test('account limits form preserves storage and model concurrency settings', async t => {
  const {dom,w,calls}=setup(false,true,'admin','',[{id:'member-id',username:'member',name:'Member',role:'member',enabled:true,mfa_enabled:true}]);
  t.after(()=>dom.window.close());
  w.HTMLDialogElement.prototype.showModal=function(){ this.open=true; };
  w.HTMLDialogElement.prototype.close=function(){ this.open=false; };
  await settle(); await settle();
  const manage=[...w.document.querySelectorAll('#users button')].find(button=>button.textContent==='管理');
  assert.ok(manage); manage.click(); await settle();
  const form=w.document.getElementById('editUserForm');
  assert.equal(form.elements.storage_gib.value,'2'); assert.equal(form.elements.concurrent_models.value,'3');
  form.elements.storage_gib.value='1.25'; form.elements.concurrent_models.value='1';
  form.dispatchEvent(new w.Event('submit',{cancelable:true})); await settle();
  const update=calls.find(call=>call.url==='/admin/users/member-id/limits'&&call.options.method==='PUT');
  assert.equal(JSON.parse(update.options.body).storage_bytes,1.25*(1024**3));
  assert.equal(JSON.parse(update.options.body).concurrent_models,1);
});

test('account return link preserves the session as data and cannot redirect to an external site',async t=>{
  for(const value of ['会话 A','//evil.example/','alpha&other=1']){
    const {dom,w}=setup(true,true,'admin','?session='+encodeURIComponent(value));t.after(()=>dom.window.close());
    const link=new URL(w.document.getElementById('backToChat').href);
    assert.equal(link.origin,'http://localhost');assert.equal(link.pathname,'/ui/');
    assert.equal(link.searchParams.get('session'),value);assert.equal(link.searchParams.has('other'),false);
  }
});

test('recovery backup requires proof and confirmation, shows count, and clears submitted secrets', async t => {
  const {dom,w,calls}=setup(false);t.after(()=>dom.window.close());await settle();
  const form=w.document.getElementById('mfaManageForm'),button=w.document.getElementById('mfaRecoveryRegenerate');
  assert.match(w.document.getElementById('mfaRecoveryCount').textContent,/剩余 1 条/);
  button.click();await settle();assert.equal(calls.filter(call=>call.url.endsWith('/recovery/regenerate')).length,0);
  form.elements.password.value='fixture-password';form.elements.code.value='fixture-recovery';
  w.confirm=()=>false;button.click();await settle();assert.equal(calls.filter(call=>call.url.endsWith('/recovery/regenerate')).length,0);
  w.confirm=()=>true;button.click();await settle();await settle();
  const call=calls.find(call=>call.url.endsWith('/recovery/regenerate'));assert.ok(call);
  assert.equal(JSON.parse(call.options.body).password,'fixture-password');
  assert.equal(w.document.getElementById('mfaManageRecovery').hidden,false);
  assert.equal(form.elements.password.value,'');assert.equal(form.elements.code.value,'');
  assert.match(w.document.getElementById('mfaManageCodes').textContent,/fixture-regenerated/);
  w.document.getElementById('mfaManageCodesSaved').click();assert.equal(w.document.getElementById('mfaManageCodes').textContent,'');
});
test('initial password page shows the required step without loading forbidden account APIs', async () => {
  const { dom, w, calls } = setup(true);
  await settle();
  assert.equal(calls.length, 0);
  assert.equal(w.document.getElementById('profileForm').hidden, true);
  assert.equal(w.document.getElementById('devices').closest('section').hidden, true);
  assert.equal(w.document.getElementById('adminUsers').hidden, true);
  assert.equal(w.document.getElementById('passwordForm').hidden, false);
  assert.match(w.document.getElementById('accountMessage').textContent, /临时密码/);
  dom.window.close();
});
test('administrator create form sends role and explicit trusted confirmation', async () => {
  const { dom, w, calls } = setup(false);
  await settle();
  const form = w.document.getElementById('createForm');
  form.elements.username.value = 'another-admin'; form.elements.password.value = 'fixture-long-password-123';
  form.elements.role.value = 'admin'; form.elements.trusted.checked = true;
  form.dispatchEvent(new w.Event('submit', { bubbles: true, cancelable: true }));
  await settle();
  const created = calls.find(call => call.url === '/admin/users' && call.options.method === 'POST');
  assert.ok(created);
  const payload = JSON.parse(created.options.body);
  assert.equal(payload.username, 'another-admin'); assert.equal(payload.role, 'admin'); assert.equal(payload.trusted, true);
  assert.equal(form.elements.password.value, ''); dom.window.close();
});
test('member account page enrolls by QR, uses the submit button, and clears secrets', async () => {
  const { dom, w, calls } = setup(false, false, 'member'); await settle();
  const form = w.document.getElementById('mfaSetupForm');
  form.elements.password.value = 'fixture-long-password';
  form.dispatchEvent(new w.Event('submit', { cancelable: true }));
  assert.equal(w.document.getElementById('mfaSetupButton').disabled, true);
  assert.equal(form.querySelector('.mfa-copy').disabled, false); await settle();
  assert.match(form.querySelector('img').src, /^data:image\/gif/);
  assert.equal(form.elements.password.required, false); assert.equal(form.elements.code.required, true);
  form.elements.code.value = '123456'; form.dispatchEvent(new w.Event('submit', { cancelable: true })); await settle();
  assert.equal(form.querySelector('img').getAttribute('src'), null);
  assert.equal(form.querySelector('code').textContent, '');
  assert.equal(w.document.getElementById('mfaRecovery').hidden, false);
  assert.equal(calls.filter(call => call.url === '/auth/sessions').length, 2);
  w.document.getElementById('mfaCodesSaved').click(); assert.equal(w.document.getElementById('mfaCodes').textContent, '');
  dom.window.close();
});
async function prepareManaged(mode = 'add') {
  const context = setup(false); await settle(); const { w } = context, form = w.document.getElementById('mfaManageForm');
  form.elements.mode.value = mode; form.elements.mode.dispatchEvent(new w.Event('change'));
  form.elements.password.value = 'fixture-long-password'; form.elements.code.value = '123456';
  form.dispatchEvent(new w.Event('submit', { cancelable: true })); await settle();
  return { ...context, form };
}
test('already-enrolled account exposes an add QR and preserves the original until confirmed', async () => {
  const { dom, w, calls, form } = await prepareManaged();
  assert.equal(w.document.getElementById('mfaManage').hidden, false);
  assert.match(w.document.querySelector('#mfaManageEnrollment img').src, /^data:image\/gif/);
  assert.equal(w.document.querySelectorAll('.mfa-device-row').length, 1);
  assert.equal(form.elements.password.value, ''); assert.equal(form.elements.code.value, '');
  form.elements.new_code.value = '654321'; form.dispatchEvent(new w.Event('submit', { cancelable: true })); await settle();
  assert.equal(w.document.querySelectorAll('.mfa-device-row').length, 2);
  assert.equal(w.document.querySelector('#mfaManageEnrollment img').getAttribute('src'), null);
  assert.match(w.document.getElementById('mfaManageStatus').textContent, /新旧验证器都可登录/);
  assert.equal(calls.some(call => call.url === '/auth/mfa/start'), false); dom.window.close();
});
test('replacement displays new recovery codes only after confirmation', async () => {
  const { dom, w, form } = await prepareManaged('replace');
  assert.match(w.document.getElementById('mfaManageSubmit').textContent, /更换/);
  assert.match(w.document.querySelector('.mfa-device-row').textContent, /原有验证器/);
  form.elements.new_code.value = '654321'; form.dispatchEvent(new w.Event('submit', { cancelable: true })); await settle();
  assert.match(w.document.querySelector('.mfa-device-row').textContent, /Google Authenticator/);
  assert.equal(w.document.getElementById('mfaManageRecovery').hidden, false);
  assert.match(w.document.getElementById('mfaManageStatus').textContent, /旧验证器已停用/);
  w.document.getElementById('mfaManageCodesSaved').click(); assert.equal(w.document.getElementById('mfaManageCodes').textContent, ''); dom.window.close();
});
test('cancel clears the staged QR and last authenticator cannot be removed', async () => {
  const { dom, w, calls, form } = await prepareManaged();
  assert.equal(w.document.querySelector('.mfa-device-row button').disabled, true);
  w.document.getElementById('mfaManageCancel').click(); await settle();
  assert.equal(w.document.querySelector('#mfaManageEnrollment img').getAttribute('src'), null);
  assert.equal(w.document.getElementById('mfaManageProof').hidden, false);
  assert.equal(form.elements.new_code.required, false);
  assert.ok(calls.some(call => call.url === '/auth/mfa/manage/cancel')); dom.window.close();
});
