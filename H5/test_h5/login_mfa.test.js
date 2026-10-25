const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),{JSDOM}=require('jsdom');
const root=path.join(__dirname,'..'),settle=()=>new Promise(resolve=>setImmediate(resolve));
const user={username:'fixture',mfa_enabled:true,password_change_required:false};
const secret='JBSWY3DPEHPK3PXP'; // Disposable fixture only.
const binding=mode=>({operation_id:'a'.repeat(32),mode,secret,
  uri:'otpauth://totp/Ytools:fixture?secret='+secret+'&issuer=Ytools',expires_at:Date.now()/1000+300});
async function setup(t,handler,configure){
  const dom=new JSDOM(fs.readFileSync(path.join(root,'login.html'),'utf8'),{url:'http://localhost/ui/login.html',runScripts:'outside-only'});
  t.after(()=>dom.window.close());const w=dom.window,calls=[];
  w.fetch=async(url,options={})=>{
    const call={url,body:options.body&&JSON.parse(options.body),csrf:options.headers&&options.headers['X-CSRF-Token']};calls.push(call);
    const reply=handler&&handler(call,calls);
    const data=reply||{status:200,data:url==='/auth/status'?{initialized:true,login_mfa_management:true}:{user,csrf:'pending',mfa_verified:false}};
    return {ok:data.status>=200&&data.status<300,status:data.status,json:async()=>data.data};
  };
  if(configure)configure(w);
  for(const file of ['js/vendor/qrcode-generator/qrcode.js','js/mfa_enrollment.js','js/recovery_codes.js','js/login_mfa_management.js','login.js'])w.eval(fs.readFileSync(path.join(root,file),'utf8'));
  await settle();return {w,calls,el:id=>w.document.getElementById(id)};
}
async function submit(w,form){form.dispatchEvent(new w.Event('submit',{cancelable:true}));await settle();await settle();}

test('invalid MFA retains the password-verified step without storing the password',async t=>{
  const {w,el}=await setup(t,call=>call.url==='/auth/mfa/verify'?{status:403,data:{detail:'验证码无效或已使用'}}:null);
  el('mfaForm').elements.code.value='123456';await submit(w,el('mfaForm'));
  assert.equal(el('mfaForm').hidden,false);assert.equal(el('loginForm').hidden,true);
  assert.match(el('authMessage').textContent,/验证码无效/);assert.equal(el('mfaForm').querySelector('button').disabled,false);
  assert.equal(w.document.activeElement,el('mfaForm').elements.code);
  assert.equal(w.localStorage.length,0);assert.equal(w.sessionStorage.length,0);
});

test('legacy 401 factor errors preserve the challenge; a revoked cookie returns to login',async t=>{
  let expired=false;
  const {w,el}=await setup(t,call=>call.url==='/auth/mfa/verify'||call.url==='/auth/me'&&expired
    ?{status:401,data:{detail:'验证失败'}}:null);
  await submit(w,el('mfaForm'));assert.equal(el('mfaForm').hidden,false);
  expired=true;await submit(w,el('mfaForm'));
  assert.equal(el('loginForm').hidden,false);assert.equal(el('mfaForm').hidden,true);
  assert.equal(el('loginMfaOpen').hidden,true);
});

for(const mode of ['add','replace'])test('login QR '+mode+' retries both proofs, rotates CSRF and clears secrets',async t=>{
  let starts=0,confirms=0;
  const {w,el,calls}=await setup(t,call=>{
    if(call.url==='/auth/mfa/manage/login_start')return ++starts===1?{status:403,data:{detail:'当前动态码错误'}}
      :{status:200,data:{user,csrf:'after-proof',mfa_verified:true,binding:binding(mode)}};
    if(call.url==='/auth/mfa/manage/confirm')return ++confirms===1?{status:403,data:{detail:'新动态码错误'}}
      :{status:200,data:{user,csrf:'confirmed',mode,recovery_codes:mode==='replace'?['new-recovery']:[]}};
  });
  el('loginMfaOpen').click();const form=el('loginMfaForm');form.elements.mode.value=mode;form.elements.code.value='123456';
  await submit(w,form);assert.equal(el('loginMfaManage').hidden,false);assert.equal(el('loginForm').hidden,true);
  await submit(w,form);assert.match(el('loginMfaEnrollment').querySelector('img').src,/^data:image\/gif/);
  assert.equal(form.elements.code.value,'');assert.equal(el('loginMfaProof').hidden,true);
  form.elements.new_code.value='234567';await submit(w,form);
  assert.equal(el('loginMfaEnrollment').hidden,false,'bad new code must retain the QR');
  await submit(w,form);
  assert.equal(el('loginMfaEnrollment').querySelector('img').getAttribute('src'),null);
  assert.equal(el('loginMfaEnrollment').querySelector('code').textContent,'');
  assert.equal(el(mode==='replace'?'recovery':'loginComplete').hidden,false);
  assert.equal(calls.find(call=>call.url==='/auth/mfa/manage/confirm').csrf,'after-proof');
  assert.ok(calls.filter(call=>call.url.includes('login_start')).every(call=>!('password' in call.body)));
  assert.equal(w.localStorage.length,0);assert.equal(w.sessionStorage.length,0);
  w.dispatchEvent(new w.Event('pagehide'));assert.equal(el('recoveryCodes').textContent,'');
});

test('cancel before proof returns to MFA; cancel after QR leaves the old device unchanged',async t=>{
  const {w,el,calls}=await setup(t,call=>{
    if(call.url==='/auth/mfa/manage/login_start')return {status:200,data:{user,csrf:'rotated',mfa_verified:true,binding:binding('add')}};
    if(call.url==='/auth/mfa/manage/cancel')return {status:200,data:{ok:true}};
  });
  el('loginMfaOpen').click();el('loginMfaCancel').click();await settle();assert.equal(el('mfaForm').hidden,false);
  el('loginMfaOpen').click();el('loginMfaForm').elements.code.value='123456';await submit(w,el('loginMfaForm'));
  el('loginMfaCancel').click();await settle();await settle();
  assert.equal(el('loginComplete').hidden,false);assert.equal(el('loginMfaEnrollment').querySelector('img').getAttribute('src'),null);
  assert.equal(calls.filter(call=>call.url==='/auth/mfa/manage/cancel').length,1);
});

test('switching accounts revokes the pending cookie and pagehide removes entered secrets',async t=>{
  const {w,el,calls}=await setup(t,call=>call.url==='/auth/logout'?{status:200,data:{ok:true}}:null);
  el('mfaForm').elements.code.value='123456';el('loginForm').elements.password.value='temporary password';
  w.dispatchEvent(new w.Event('pagehide'));
  assert.equal(el('mfaForm').elements.code.value,'');assert.equal(el('loginForm').elements.password.value,'');
  el('changeAccount').click();await settle();
  assert.equal(el('loginForm').hidden,false);assert.equal(el('mfaForm').hidden,true);
  assert.equal(calls.filter(call=>call.url==='/auth/logout').length,1);
});

test('initial-password accounts complete normal MFA before managing devices',async t=>{
  const {el}=await setup(t,call=>call.url==='/auth/me'?{status:200,data:{csrf:'pending',mfa_verified:false,user:{...user,password_change_required:true}}}:null);
  assert.equal(el('mfaForm').hidden,false);assert.equal(el('loginMfaOpen').hidden,true);
});

test('rolling upgrade keeps QR management hidden until the backend supports it',async t=>{
  const {el}=await setup(t,call=>call.url==='/auth/status'?{status:200,data:{initialized:true}}:null);
  assert.equal(el('mfaForm').hidden,false);assert.equal(el('loginMfaOpen').hidden,true);
});

test('lost-device guidance selects recovery login or replaces the lost factor directly',async t=>{
  const {el}=await setup(t);
  el('useRecoveryCode').click();assert.match(el('mfaCodeLabelText').textContent,/恢复码/);
  el('mfaForm').elements.code.value='a'.repeat(24);el('replaceLostDevice').click();
  assert.equal(el('mfaForm').hidden,true);assert.equal(el('loginMfaForm').elements.mode.value,'replace');
  assert.equal(el('loginMfaForm').elements.code.value,'a'.repeat(24));
  assert.match(el('mfaResetCommand').textContent,/--username fixture$/);
});

test('offline recovery is presented as required enrollment rather than ordinary login',async t=>{
  const {el}=await setup(t,call=>call.url==='/auth/me'?{status:200,data:{csrf:'pending',mfa_verified:false,user:{...user,mfa_enabled:false,mfa_setup_required:true}}}:null);
  assert.equal(el('enrollForm').hidden,false);assert.equal(el('mfaForm').hidden,true);
  assert.match(el('authMessage').textContent,/账号恢复/);assert.match(el('enrollIntro').textContent,/重置/);
});

test('an expired login-management QR clears the key and cancels only the staged operation',async t=>{
  let tick,clock=Date.now();
  const {w,el,calls}=await setup(t,call=>{
    if(call.url==='/auth/mfa/manage/login_start')return {status:200,data:{user,csrf:'rotated',mfa_verified:true,binding:binding('add')}};
    if(call.url==='/auth/mfa/manage/cancel')return {status:200,data:{ok:true}};
  },w=>{w.Date.now=()=>clock;w.setInterval=callback=>{tick=callback;return 1;};w.clearInterval=()=>{};});
  el('loginMfaOpen').click();el('loginMfaForm').elements.code.value='123456';await submit(w,el('loginMfaForm'));
  clock+=301000;tick();await settle();await settle();
  assert.equal(el('loginMfaEnrollment').querySelector('code').textContent,'');
  assert.equal(el('loginMfaEnrollment').querySelector('img').getAttribute('src'),null);
  assert.equal(el('loginComplete').hidden,false);
  assert.match(el('authMessage').textContent,/二维码已过期/);
  assert.equal(calls.filter(call=>call.url==='/auth/mfa/manage/cancel').length,1);
});
