'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const crypto = require('node:crypto');
const { chromium } = require('../browser/node_modules/playwright');
const cfg = JSON.parse(process.env.YTOOLS_E2E_CONFIG);
const flow = process.argv[2];

function totp(secret, offset = 0) {
  const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567';
  let bits = '', bytes = [];
  for (const c of secret) bits += alphabet.indexOf(c).toString(2).padStart(5, '0');
  for (let i = 0; i + 8 <= bits.length; i += 8) bytes.push(parseInt(bits.slice(i, i + 8), 2));
  const counter = Buffer.alloc(8); counter.writeBigUInt64BE(BigInt(Math.floor((Date.now() / 1000 + offset) / 30)));
  const digest = crypto.createHmac('sha1', Buffer.from(bytes)).update(counter).digest();
  return String((digest.readUInt32BE(digest[19] & 15) & 0x7fffffff) % 1000000).padStart(6, '0');
}
async function login(page, username = 'alice', pending = false, factor = null) {
  await page.goto(cfg.origin + '/ui/');
  await page.locator('#loginForm').waitFor({ state: 'visible' });
  await page.locator('#loginForm [name=username]').fill(username);
  await page.locator('#loginForm [name=password]').fill(cfg.password);
  await page.locator('#loginForm button').click();
  if (username === 'alice' || factor) {
    await page.locator(pending==='enroll'?'#enrollForm':'#mfaForm').waitFor({ state: 'visible' });
    if (pending) return;
    await page.locator('#mfaForm [name=code]').fill(factor||totp(cfg.secret));
    await page.locator('#mfaForm button[type=submit]').click();
  }
  await page.waitForURL(cfg.origin + '/ui/');
  await page.waitForFunction(() => window.App && App.fileHistory && window.YtoolsAuth);
  assert.equal(await page.evaluate(() => YtoolsAuth.user.username), username);
}
async function poll(fn, check, message) {
  const deadline = Date.now() + 15000;
  while (Date.now() < deadline) {
    const value = await fn();
    if (check(value)) return value;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error(message);
}
async function openAccountPanel(page) {
  const profile=page.locator('#profileCard .profile-plan');
  if(await profile.isVisible())await profile.click();else await page.locator('#railAvatar').click();
  try { await page.getByRole('menuitem',{name:'账号与安全',exact:true}).click(); }
  catch(error) {
    await page.screenshot({path:'tmp/account-return-performance-20261025/open-failure.png'});
    console.error('PANEL_OPEN_STATE '+JSON.stringify(await page.evaluate(()=>({url:location.href,menuHidden:document.getElementById('sidebarSettingsMenu')?.hidden,active:document.activeElement?.id,scroll:App.chatScroll.scrollTop,bodyScroll:scrollY,panel:App.accountPanel.isOpen(),trace:window.panelScrollTrace}))));
    throw error;
  }
  const frame=page.frameLocator('#accountPanel iframe');
  await frame.locator('#accountName').waitFor({state:'visible'});
  return frame;
}

(async () => {
  if (!fs.existsSync(chromium.executablePath())) process.exit(77);
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext(flow === 'history_scroll' ? {viewport:{width:1600,height:900}} : {});
  const errors = [];
  context.on('page', page => page.on('pageerror', error => errors.push(error.message)));
  await context.route('**/*', route => {
    const url = new URL(route.request().url());
    return url.origin === cfg.origin ? route.continue() : route.abort();
  });
  try {
    const page = await context.newPage();
    if(flow==='account_share_import') {
      fs.mkdirSync('tmp/share-account-isolation-20261025',{recursive:true});page.setDefaultTimeout(12000);
      page.on('response',async response=>{if(new URL(response.url()).pathname.startsWith('/chat_history/import'))fs.appendFileSync('tmp/share-account-isolation-20261025/browser-api.log',JSON.stringify({path:new URL(response.url()).pathname,status:response.status(),body:(await response.text()).slice(0,600)})+'\n');});
      page.on('pageerror',error=>fs.appendFileSync('tmp/share-account-isolation-20261025/browser-errors.log',error.message+'\n'));
    }
    if(flow==='account_return'||flow.startsWith('account_panel')) {
      fs.mkdirSync('tmp/account-return-performance-20261025',{recursive:true});
      page.setDefaultTimeout(12000);
      const trace='tmp/account-return-performance-20261025/trace-'+flow+'.log';
      page.on('pageerror',error=>fs.appendFileSync(trace,JSON.stringify({error:error.message})+'\n'));
      page.on('console',message=>{if(message.type()==='error')fs.appendFileSync(trace,JSON.stringify({console:message.text().slice(0,500)})+'\n');});
      page.on('response',response=>{if(new URL(response.url()).pathname==='/ui/account.html')fs.appendFileSync(trace,JSON.stringify({accountStatus:response.status(),xfo:response.headers()['x-frame-options'],csp:response.headers()['content-security-policy']})+'\n');});
    }
    const registrations=[];
    page.on('request',request=>{if(new URL(request.url()).pathname==='/chat_history/ensure_session')registrations.push(request.postDataJSON().session_id);});
    await login(page, 'alice', flow==='lost_device_rebind'?'enroll':flow.startsWith('login_mfa_'));
    if (flow === 'account_share_import') {
      assert.equal(await page.evaluate(()=>App.state.hasConversation),false);
      const [download]=await Promise.all([page.waitForEvent('download'),page.evaluate(()=>App.downloadSession('alpha'))]);
      const archive=fs.readFileSync(await download.path());assert.ok(archive.length>0);
      await page.evaluate(()=>YtoolsAuth.logout());await page.waitForURL('**/ui/login.html');await login(page,'bob');
      assert.equal(await page.locator('#sessionList [data-session="alpha"]').count(),0);
      await page.locator('#historyFileInput').setInputFiles({name:'shared.zip',mimeType:'application/zip',buffer:archive});
      await page.locator('#importConflictModal:not(.hidden)').waitFor();
      assert.equal(await page.locator('#importConflictList select.ic-choice').count(),0,'foreign ID was presented as an overwrite target');
      await page.locator('#importConflictSubmit').click();await page.getByText('Fixture reply alpha',{exact:true}).waitFor();
      await page.locator('#importConflictModal.hidden').waitFor({state:'attached'});
      const copied=await page.evaluate(()=>App.state.sessionId);assert.notEqual(copied,'alpha');
      assert.equal((await context.request.get(cfg.origin+'/chat_history/file?session_id=alpha')).status(),404);
      const jsonl=(name)=>Buffer.from(JSON.stringify({_meta:{title:name}})+'\n'+JSON.stringify({event:'chat_round',question:name,status:'done',events:[{role:'user',content:name},{role:'assistant',content:'reply '+name}]})+'\n');
      await page.locator('#historyFileInput').setInputFiles([
        {name:'batch-one_chat.jsonl',mimeType:'application/json',buffer:jsonl('batch-one')},
        {name:'batch-two_chat.jsonl',mimeType:'application/json',buffer:jsonl('batch-two')}]);
      try{await page.locator('#importConflictList [data-session-id="batch-one"]').waitFor();}
      catch(error){console.error('IMPORT_UI_STATE '+JSON.stringify(await page.evaluate(()=>({modal:document.getElementById('importConflictModal').className,list:document.getElementById('importConflictList').textContent,toasts:[...document.querySelectorAll('.toast')].map(node=>node.textContent)}))));throw error;}
      assert.equal(await page.locator('#importConflictList [data-session-id="batch-two"]').count(),0);
      await page.locator('#importConflictSubmit').click();await page.locator('#importConflictList [data-session-id="batch-two"]').waitFor();
      await page.locator('#importConflictSubmit').click();await page.getByText('reply batch-two',{exact:true}).waitFor();
      await page.locator('#importConflictModal.hidden').waitFor({state:'attached'});
      await page.locator('#historyFileInput').setInputFiles([
        {name:'batch-cancel_chat.jsonl',mimeType:'application/json',buffer:jsonl('batch-cancel')},
        {name:'batch-next_chat.jsonl',mimeType:'application/json',buffer:jsonl('batch-next')}]);
      await page.locator('#importConflictList [data-session-id="batch-cancel"]').waitFor();await page.locator('#importConflictCancel').click();
      await page.locator('#importConflictList [data-session-id="batch-next"]').waitFor();await page.locator('#importConflictSubmit').click();
      await page.getByText('reply batch-next',{exact:true}).waitFor();
      await page.reload();await page.waitForFunction(()=>window.App&&App.fileHistory);
      await page.locator('#sessionList [data-session="'+copied+'"] .session-select').click();await page.getByText('Fixture reply alpha',{exact:true}).waitFor();
      const own=await(await context.request.get(cfg.origin+'/chat_history/summaries')).json();
      const ids=own.map(row=>row.id);assert.ok(ids.includes(copied)&&ids.includes('batch-one')&&ids.includes('batch-two')&&ids.includes('batch-next'));
      assert.ok(!ids.includes('alpha')&&!ids.includes('batch-cancel'));
      const bobExport=await context.request.get(cfg.origin+'/chat_history/export_zip?session_ids='+encodeURIComponent(copied));assert.equal(bobExport.status(),200);
    } else if (flow === 'account_preferences') {
      await page.evaluate(async () => {
        ThemeManager.setPreference('light'); ThemeManager.setAccentPreference('pink');
        App.taskStatus.setStatusVisible(false);
        localStorage.setItem('ytools-sidebar-width','400'); localStorage.setItem('ytools-session-groups-open','1');
        await AccountPreferences.flush();
        const response=await fetch('/chat_config/network_retry',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({max_attempts:4})});
        if (!response.ok) throw new Error('personal chat config failed: '+await response.text());
      });
      const settings=await page.evaluate(async()=>(await fetch('/user/preferences')).json());
      assert.deepEqual(settings.ui,{theme:'light',accent:'pink',task_status_visible:false,sidebar_width:400,groups_expanded:true});
      await page.goto(cfg.origin+'/ui/account.html');
      assert.equal(await page.evaluate(()=>document.documentElement.dataset.theme),'light');
      assert.equal(await page.evaluate(()=>document.documentElement.dataset.accent),'pink');
      await page.evaluate(()=>YtoolsAuth.logout()); await page.waitForURL('**/ui/login.html');
      await login(page,'alice',false,cfg.recovery[0]);
      assert.equal(await page.evaluate(()=>ThemeManager.getPreference()),'light');
      assert.equal(await page.evaluate(()=>App.taskStatus.isStatusVisible()),false);
      assert.equal(await page.evaluate(async()=>(await(await fetch('/chat_config/network_retry')).json()).max_attempts),4);
      const fresh=await browser.newContext();
      try {
        const device=await fresh.newPage(); device.on('pageerror',e=>errors.push(e.message));
        await login(device,'alice',false,cfg.recovery[1]);
        assert.equal(await device.evaluate(()=>ThemeManager.getPreference()),'light');
        assert.equal(await device.evaluate(()=>ThemeManager.getAccentPreference()),'pink');
        assert.equal(await device.evaluate(()=>localStorage.getItem('ytools-sidebar-width')),'400');
        await page.evaluate(()=>YtoolsAuth.logout()); await page.waitForURL('**/ui/login.html'); await login(page,'bob');
        assert.equal(await page.evaluate(()=>ThemeManager.getPreference()),'system');
        assert.equal(await page.evaluate(()=>ThemeManager.getAccentPreference()),'blue');
        assert.equal(await page.evaluate(()=>App.taskStatus.isStatusVisible()),true);
        await page.evaluate(async()=>{ThemeManager.setPreference('dark');await AccountPreferences.flush();});
        await device.reload(); await device.waitForFunction(()=>window.App&&App.fileHistory);
        assert.equal(await device.evaluate(()=>ThemeManager.getPreference()),'light');
        assert.deepEqual((await(await fresh.request.get(cfg.origin+'/user/preferences')).json()).ui,settings.ui);
      } finally { await fresh.close(); }
    } else if (flow === 'account_return') {
      const originalUserId=await page.evaluate(()=>YtoolsAuth.user.id);
      await page.locator('#profileName').click();
      assert.equal(await page.locator('.profile-name-editor').count(),0);
      await page.getByRole('menuitem',{name:'账号与安全',exact:true}).click();
      const frame=page.frameLocator('#accountPanel iframe');
      await frame.locator('#profileForm [name=name]').fill('新的账号显示名');
      await frame.getByRole('button',{name:'保存显示名',exact:true}).click();
      await frame.getByText('显示名已保存',{exact:true}).waitFor();
      assert.equal(await frame.locator('#accountName').textContent(),'新的账号显示名 · alice');
      assert.equal(await page.evaluate(()=>YtoolsAuth.user.id),originalUserId);
      assert.equal(await page.evaluate(()=>YtoolsAuth.user.username),'alice');
      await page.goBack();
      await page.locator('#profileName').filter({hasText:'新的账号显示名'}).waitFor();
      await page.goto(cfg.origin+'/ui/account.html');
      await page.getByRole('link',{name:'← 返回聊天',exact:true}).click();
      await page.waitForLoadState('networkidle');
      assert.deepEqual(registrations.filter(id=>id==='default'),[],'returning from account attempted to claim the legacy default session');
      assert.equal(await page.getByText('初始会话暂时无法加载，可稍后从历史列表打开',{exact:true}).count(),0);
      assert.equal(await page.evaluate(()=>App.state.sessionId),null);
      await page.locator('#sessionList [data-session="alpha"] .session-select').click();await page.getByText('Fixture reply alpha',{exact:true}).waitFor();
      await openAccountPanel(page);
      await page.goBack();await page.reload();await page.getByText('Fixture reply alpha',{exact:true}).waitFor();
      const second=await openAccountPanel(page);
      await second.getByRole('link',{name:'← 返回聊天',exact:true}).click();
      await page.locator('#accountPanel').waitFor({state:'detached'});
      await page.getByText('Fixture reply alpha',{exact:true}).waitFor();
      assert.equal(await page.evaluate(()=>App.state.sessionId),'alpha');
      await page.locator('#newChatBtn').click();
      const empty=await openAccountPanel(page);
      await empty.getByRole('link',{name:'← 返回聊天',exact:true}).click();await page.locator('#accountPanel').waitFor({state:'detached'});
      assert.equal(await page.evaluate(()=>App.state.sessionId),null);
      await page.goto(cfg.origin+'/ui/?session=not-owned');await page.waitForLoadState('networkidle');
      assert.equal(registrations.includes('not-owned'),false,'read-only navigation created a session owner');
      assert.equal(await page.getByText('Legacy private fixture',{exact:true}).count(),0);
      assert.equal(await page.getByText('初始会话暂时无法加载，可稍后从历史列表打开',{exact:true}).count(),0);
    } else if (flow === 'account_panel_performance') {
      const requests=[];page.on('request',request=>requests.push({path:new URL(request.url()).pathname,main:request.isNavigationRequest()&&request.frame()===page.mainFrame()}));
      await page.locator('#sessionList [data-session="alpha"] .session-select').click();
      await page.getByText('Large reply 80',{exact:false}).first().waitFor();
      await page.locator('#sessionList [data-session="alpha"]').waitFor();
      // Measure the original standalone navigation as a reference with the same fixture.
      await page.goto(cfg.origin+'/ui/account.html?session=alpha');
      const before=requests.length, start=Date.now();
      await page.getByRole('link',{name:'← 返回聊天',exact:true}).click();
      await page.getByText('Large reply 80',{exact:false}).first().waitFor();
      const baseline={ms:Date.now()-start,history:requests.slice(before).filter(r=>r.path==='/chat_history/file').length};
      await page.locator('#sessionList [data-session="alpha"]').waitFor();
      await page.evaluate(()=>{window.returnProbe={reply:document.querySelector('#chatInner')||App.chatInner};App.input.value='fixture unsent draft';App.chatScroll.scrollTop=150;window.savedChatScroll=App.chatScroll.scrollTop;});
      await page.evaluate(()=>{window.panelScrollTrace=[];window.addEventListener('scroll',event=>{panelScrollTrace.push({id:event.target.id,cls:event.target.className,time:performance.now()});if(panelScrollTrace.length>20)panelScrollTrace.shift();},true);});
      // Wait for the history window's anchor adjustments before measuring position preservation.
      await page.evaluate(()=>{window.lastPanelScroll=performance.now();App.chatScroll.addEventListener('scroll',()=>{window.lastPanelScroll=performance.now();});});
      await page.waitForFunction(()=>performance.now()-window.lastPanelScroll>250);
      const saved=await page.evaluate(()=>({scroll:App.chatScroll.scrollTop,input:App.input.value}));
      const begin=requests.length,elapsed=[];
      for(let i=0;i<3;i++) {
        const frame=await openAccountPanel(page);
        if(i===0)await page.screenshot({path:'tmp/account-return-performance-20261025/account-panel-desktop.png'});
        const started=Date.now();await frame.getByRole('link',{name:'← 返回聊天',exact:true}).click();
        await page.locator('#accountPanel').waitFor({state:'detached'});elapsed.push(Date.now()-started);
        assert.equal(await page.evaluate(()=>returnProbe.reply.isConnected),true);
        assert.equal(await page.evaluate(()=>App.input.value),saved.input);
        assert.equal(await page.evaluate(()=>App.chatScroll.scrollTop),saved.scroll);
      }
      const returned=requests.slice(begin);
      assert.equal(returned.filter(r=>r.main).length,0,'returning navigated the chat document');
      assert.equal(returned.filter(r=>r.path==='/chat_history/file').length,0,'returning reloaded the history');
      assert.equal(returned.filter(r=>r.path==='/chat_history/summaries').length,0,'returning reloaded the sidebar');
      const measurement={rounds:80,baseline,optimized_ms:elapsed,optimized_history_requests:0,optimized_chat_navigations:0};
      fs.writeFileSync('tmp/account-return-performance-20261025/benchmark.json',JSON.stringify(measurement));
      console.log('ACCOUNT_RETURN_PERF '+JSON.stringify(measurement));
      await page.setViewportSize({width:390,height:844});const mobile=await openAccountPanel(page);
      await page.screenshot({path:'tmp/account-return-performance-20261025/account-panel-mobile.png'});
      const bounds=await page.locator('#accountPanel').boundingBox();assert.ok(bounds.x>=0&&bounds.width<=390&&bounds.height<=844);
      await mobile.getByRole('link',{name:'← 返回聊天',exact:true}).click();await page.locator('#accountPanel').waitFor({state:'detached'});
    } else if (flow === 'account_panel_stream') {
      await page.locator('#sessionList [data-session="alpha"] .session-select').click();
      await page.getByText('Fixture reply alpha',{exact:true}).waitFor();
      await page.locator('#input').fill('fixture stream across account panel');await page.locator('#sendBtn').click();
      await page.waitForFunction(()=>App.state.streaming);
      await page.evaluate(()=>{window.streamReturnProbe=App.chatInner;});
      const frame=await openAccountPanel(page);
      await page.waitForFunction(()=>!App.state.streaming);
      await frame.getByRole('link',{name:'← 返回聊天',exact:true}).click();await page.locator('#accountPanel').waitFor({state:'detached'});
      await page.getByText('fixture-chunk-29',{exact:false}).first().waitFor();
      assert.equal(await page.evaluate(()=>streamReturnProbe===App.chatInner),true);
    } else if (flow === 'account_panel_logout') {
      const frame=await openAccountPanel(page);await frame.locator('#logout').click();
      await page.waitForURL('**/ui/login.html');assert.equal(await page.locator('#accountPanel').count(),0);
      await login(page,'bob');assert.equal(await page.getByText('Fixture reply alpha',{exact:true}).count(),0);
    } else if (flow === 'account_panel_username') {
      const frame=await openAccountPanel(page);
      await frame.locator('#usernameForm [name=username]').fill('Alice.Renamed');
      await frame.locator('#usernameForm [name=password]').fill(cfg.password);
      await frame.locator('#usernameForm [name=code]').fill(cfg.recovery[0]);
      page.once('dialog', dialog => dialog.accept());
      await frame.locator('#usernameForm [type=submit]').click();await page.waitForURL('**/ui/login.html?username_changed=1');
      assert.equal(await page.locator('#accountPanel').count(),0);
      await login(page,'alice.renamed',false,cfg.recovery[1]);assert.equal(await page.evaluate(()=>YtoolsAuth.user.id),cfg.alice);
    } else if (flow === 'username_change') {
      const originalId = await page.evaluate(() => YtoolsAuth.user.id);
      await page.goto(cfg.origin + '/ui/account.html');
      await page.locator('#usernameSettings').waitFor({ state: 'visible' });
      assert.equal(await page.locator('#usernameForm [name=code]').getAttribute('required'), '');
      await page.locator('#usernameForm [name=username]').fill('Alice.Renamed');
      await page.locator('#usernameForm [name=password]').fill(cfg.password);
      await page.locator('#usernameForm [name=code]').fill(cfg.recovery[0]);
      page.once('dialog', dialog => dialog.accept());
      await page.locator('#usernameSubmit').click();
      await page.waitForURL(cfg.origin + '/ui/login.html?username_changed=1');
      await page.getByText('登录用户名已修改，请使用新用户名和原密码重新登录。验证器继续有效。', { exact: true }).waitFor();
      const oldLogin = await context.request.post(cfg.origin + '/auth/login', {
        headers: { Origin: cfg.origin }, data: { username: 'alice', password: cfg.password } });
      assert.equal(oldLogin.status(), 401);
      await login(page, 'alice.renamed', false, cfg.recovery[1]);
      assert.equal(await page.evaluate(() => YtoolsAuth.user.id), originalId);
      assert.equal(await page.evaluate(() => YtoolsAuth.user.role), 'admin');
      assert.equal((await context.request.get(cfg.origin + '/chat_history/file?session_id=alpha')).status(), 200);
      const cooldown = await (await context.request.get(cfg.origin + '/user/username')).json();
      assert.equal(cooldown.can_change, false); assert.ok(cooldown.next_change_at > Date.now() / 1000);
      const csrf = await page.evaluate(() => YtoolsAuth.csrf);
      const reclaim = await context.request.post(cfg.origin + '/admin/users', {
        headers: { Origin: cfg.origin, 'X-CSRF-Token': csrf },
        data: { username: 'alice', password: cfg.password, role: 'member' } });
      assert.equal(reclaim.status(), 400);
      const devices = await (await context.request.get(cfg.origin + '/auth/mfa/devices')).json();
      assert.equal(devices.devices.length, 1); assert.equal(devices.recovery_codes_remaining, cfg.recovery.length - 2);
    } else if (flow === 'lost_device_rebind') {
      await page.getByText('账号恢复：请重新绑定验证器',{exact:true}).waitFor();
      assert.equal((await context.request.get(cfg.origin+'/auth/sessions')).status(),403);
      await page.locator('#enrollForm [name=password]').fill(cfg.password);
      await page.locator('#enrollButton').click();await page.locator('#mfaEnrollment img').waitFor({state:'visible'});
      const secret=await page.locator('#mfaEnrollment code').textContent();
      await page.locator('#enrollForm [name=code]').fill(totp(secret));await page.locator('#enrollButton').click();
      await page.locator('#recovery').waitFor({state:'visible'});
      assert.equal((await context.request.get(cfg.origin+'/auth/me')).status(),200);
      const me=await (await context.request.get(cfg.origin+'/auth/me')).json();assert.equal(me.user.mfa_setup_required,false);
      await page.locator('#enterApp').click();await page.waitForURL(cfg.origin+'/ui/');
      await page.goto(cfg.origin+'/ui/?session=alpha');await page.getByText('Fixture reply alpha',{exact:true}).waitFor();
    } else if (flow === 'recovery_regenerate') {
      await page.goto(cfg.origin+'/ui/account.html');await page.locator('#mfaManage').waitFor({state:'visible'});
      await page.locator('#mfaManageForm [name=password]').fill(cfg.password);
      await page.locator('#mfaManageForm [name=code]').fill(cfg.recovery[0]);
      page.once('dialog',dialog=>dialog.accept());await page.locator('#mfaRecoveryRegenerate').click();
      await page.locator('#mfaManageRecovery').waitFor({state:'visible'});
      const codes=(await page.locator('#mfaManageCodes').textContent()).trim().split('\n');assert.equal(codes.length,10);
      const downloadEvent=page.waitForEvent('download');await page.locator('#mfaManageCodesDownload').click();
      const download=await downloadEvent;assert.equal(download.suggestedFilename(),'ytools-recovery-codes.txt');
      const text=fs.readFileSync(await download.path(),'utf8');assert.ok(text.includes(codes.join('\n')));
      assert.ok(!text.includes(cfg.password));assert.ok(!text.includes(cfg.secret));
      const fresh=await browser.newContext();
      try {const restored=await fresh.newPage();await login(restored,'alice',false,codes[0]);}
      finally {await fresh.close();}
      await page.locator('#mfaManageCodesSaved').click();assert.equal(await page.locator('#mfaManageCodes').textContent(),'');
    } else if (flow.startsWith('login_mfa_')) {
      await page.locator('#mfaForm [name=code]').fill('not-a-code');
      await page.locator('#mfaForm button[type=submit]').click();
      await page.getByText('验证码无效或已使用', {exact:false}).waitFor();
      assert.equal(await page.locator('#mfaForm').isVisible(),true);
      assert.equal(await page.locator('#loginForm').isVisible(),false);
      if (flow === 'login_mfa_retry') {
        await page.locator('#mfaForm [name=code]').fill(totp(cfg.secret));
        await page.locator('#mfaForm button[type=submit]').click();
      } else {
        const mode=flow==='login_mfa_replace'?'replace':'add';
        if(mode==='replace')await page.locator('#mfaRecoveryHelp summary').click();
        if(mode==='replace')await page.locator('#replaceLostDevice').click();
        else await page.locator('#loginMfaOpen').click();
        await page.locator('#loginMfaForm [name=mode]').selectOption(mode);
        if(process.env.YTOOLS_LOGIN_PREVIEW&&mode==='add')await page.screenshot({path:process.env.YTOOLS_LOGIN_PREVIEW,fullPage:true});
        await page.locator('#loginMfaForm [name=code]').fill('bad-current-code');
        await page.locator('#loginMfaSubmit').click();
        await page.getByText('验证码无效或已使用', {exact:false}).waitFor();
        assert.equal(await page.locator('#loginMfaForm').isVisible(),true);
        await page.locator('#loginMfaForm [name=code]').fill(mode==='replace'?cfg.recovery[0]:totp(cfg.secret));
        const proofRequest=page.waitForRequest(request=>request.url().endsWith('/auth/mfa/manage/login_start'));
        await page.locator('#loginMfaSubmit').click();
        assert.ok(!('password' in (await proofRequest).postDataJSON()));
        await page.locator('#loginMfaEnrollment img').waitFor({state:'visible'});
        const secret=await page.locator('#loginMfaEnrollment code').textContent();
        const accepted=new Set([-30,0,30].map(offset=>totp(secret,offset)));
        const wrong=Array.from({length:10},(_,index)=>String(index).padStart(6,'0')).find(code=>!accepted.has(code));
        await page.locator('#loginMfaForm [name=new_code]').fill(wrong);
        await page.locator('#loginMfaSubmit').click();
        await page.getByText('请输入新验证器中', {exact:false}).waitFor();
        assert.equal(await page.locator('#loginMfaEnrollment img').isVisible(),true);
        await page.locator('#loginMfaForm [name=new_code]').fill(totp(secret));
        await page.locator('#loginMfaSubmit').click();
        await page.locator(mode==='replace'?'#recovery':'#loginComplete').waitFor({state:'visible'});
        assert.equal(await page.locator('#loginMfaEnrollment img').getAttribute('src'),null);
        const devices=await (await context.request.get(cfg.origin+'/auth/mfa/devices')).json();
        assert.equal(devices.devices.length,mode==='replace'?1:2);
        await page.locator(mode==='replace'?'#enterApp':'#loginCompleteEnter').click();
      }
      await page.waitForURL(cfg.origin+'/ui/');
      await page.waitForFunction(()=>window.App&&window.YtoolsAuth);
      assert.equal(await page.evaluate(()=>YtoolsAuth.user.username),'alice');
    } else if (flow === 'history_scroll') {
      await page.goto(cfg.origin + '/ui/?session=alpha');
      await page.waitForFunction(() => window.App && App.chatInner.textContent.includes('Fixture reply alpha'));
      const fixture=cfg.scroll_fixture ? JSON.parse(fs.readFileSync(cfg.scroll_fixture,'utf8')) : (()=>{
        const records=[];let event=0;
        for(let round=1;round<=30;round++){
          records.push({kind:'user',round,content:'Question '+round});
          for(let block=0;block<3;block++){
            for(let step=0;step<15;step++){
              records.push({kind:'think',round,source_event_index:event++,content:'Thought text '.repeat(500)});
              records.push({kind:'tool',round,source_event_index:event++,name:'run_command',args:{command:'fixture command'}});
              records.push({kind:'toolResult',round,source_event_index:event++,name:'run_command',content:JSON.stringify({success:true,output:'fixture output '.repeat(100)})});
            }
            records.push({kind:'assistant',round,source_event_index:event++,content:'Answer '+round+' / '+block+'\n\n'+'Visible paragraph. '.repeat(35)});
          }
        }
        return records;
      })();
      const setup=await page.evaluate(records=>{
        App.pauseAutoScroll(); App.resetHistoryView(); App.chatInner.replaceChildren();
        const started=performance.now();App.renderRecords(records);App.scrollToBottom(true);
        const metrics=window.__historyScrollMetrics={renders:[],samples:[]};
        const render=App.renderHistoryWindowAt;
        App.renderHistoryWindowAt=function(...args){const t=performance.now();const result=render.apply(this,args);metrics.renders.push(performance.now()-t);return result;};
        return {initial_ms:performance.now()-started,records:records.length};
      },fixture);
      await page.waitForTimeout(250);
      const rect=await page.locator('#chatScroll').boundingBox();
      await page.mouse.move(rect.x+rect.width/2,rect.y+rect.height/2);
      for(const direction of [-1,1,-1,1])for(let step=0;step<24;step++){
        await page.mouse.wheel(0,direction*450);await page.waitForTimeout(35);
        await page.evaluate(direction=>{
          const box=App.chatScroll.getBoundingClientRect(),center=(box.top+box.bottom)/2;
          const nodes=[...App.chatInner.children].filter(node=>!node.matches('.is-process-hidden,.qnav-load-older,.history-live-hidden'));
          const visible=nodes.map(node=>({node,box:node.getBoundingClientRect()})).filter(item=>item.box.bottom>box.top&&item.box.top<box.bottom);
          const middle=visible.find(item=>item.box.top<=center&&item.box.bottom>=center)||visible[0];
          const match=middle&&middle.node._processSummary&&/:h(\d+)$/.exec(middle.node._processSummary.key);
          const view=App.getHistoryWindow();
          window.__historyScrollMetrics.samples.push({direction,top:App.chatScroll.scrollTop,count:view.records.length,start:view.start,end:view.end,
            index:middle ? Number(match?match[1]:middle.node.dataset.historyIndex):null,
            blank:visible.length?Math.max(0,visible[0].box.top-box.top):box.height});
        },direction);
      }
      const metrics=await page.evaluate(()=>window.__historyScrollMetrics);
      let largest={start:0,count:0},run={start:0,count:0};
      fixture.forEach((record,index)=>{if(['think','tool','toolResult','compaction','notice','approval'].includes(record.kind)){
        if(!run.count)run.start=index;run.count++;if(run.count>largest.count)largest={...run};
      }else run={start:index+1,count:0};});
      const expanded=await page.evaluate(index=>{
        App.renderHistoryWindowAt(index,{size:1,before:0});
        const button=App.chatInner.querySelector('.process-group-toggle');if(!button)return null;
        const t=performance.now();button.click();return {ms:performance.now()-t,expanded:button.getAttribute('aria-expanded')};
      },largest.start);
      await page.waitForTimeout(250);
      const folded=await page.evaluate(index=>{
        // Revisit the middle of an expanded run, then collapse its partial window.
        App.renderHistoryWindowAt(index,{size:24,before:6});
        const header=App.chatInner.querySelector('.process-group-summary');
        if(!header)return null;
        const group=header._processSummary,wasOpen=group.open,top=header.getBoundingClientRect().top;
        group.button.click();
        return {wasOpen,closed:!group.open,key:group.key,top,delta:header.getBoundingClientRect().top-top};
      },largest.start+Math.floor(largest.count/2));
      await page.waitForTimeout(250);
      assert.ok(folded&&folded.wasOpen&&folded.closed,'expanded history lost its state or could not collapse');
      assert.ok(Math.abs(folded.delta)<2,'collapsing a partial process window moved its header');
      const drag=await page.evaluate(async()=>{
        const scroller=App.chatScroll,box=scroller.getBoundingClientRect();
        scroller.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,button:0,pointerId:42,
          pointerType:'mouse',clientX:box.right,clientY:(box.top+box.bottom)/2}));
        const locked=App.isChatScrollbarDragging(),height=scroller.scrollHeight,samples=[];
        // Headless Chromium does not expose native thumb movement; drive its scroll
        // coordinates while exercising the application's actual press/release handlers.
        for(const ratio of [1,.75,.5,.25,0,.3,.7,1]){
          scroller.scrollTop=ratio*(scroller.scrollHeight-scroller.clientHeight);
          App.historyScroll.onScroll();
          await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
          const visible=[...App.chatInner.children].filter(node=>!node.matches('.is-process-hidden,.qnav-load-older,.history-live-hidden'))
            .map(node=>node.getBoundingClientRect()).filter(rect=>rect.bottom>box.top&&rect.top<box.bottom);
          samples.push({height:scroller.scrollHeight,blank:visible.length?Math.max(0,visible[0].top-box.top):box.height});
        }
        window.dispatchEvent(new PointerEvent('pointerup',{bubbles:true,button:0,pointerId:42,clientY:box.bottom-1}));
        await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
        const view=App.getHistoryWindow();
        return {locked,height,samples,released:!App.isChatScrollbarDragging(),tail:view.end===view.records.length,
          bottomGap:scroller.scrollHeight-scroller.scrollTop-scroller.clientHeight};
      });
      assert.ok(drag.locked&&drag.released&&drag.tail&&drag.bottomGap<=2,'scrollbar release did not retain the latest messages');
      assert.ok(drag.samples.every(sample=>sample.height===drag.height),'scrollbar range changed during dragging');
      assert.ok(drag.samples.every(sample=>sample.blank<160),'scrollbar drag left a blank viewport');
      let reversals=0;
      metrics.samples.forEach((sample,i)=>{const before=metrics.samples[i-1];if(before&&sample.direction===before.direction&&sample.index!=null&&before.index!=null&&Math.sign(sample.index-before.index)===-sample.direction)reversals++;});
      const summary={...setup,window_renders:metrics.renders.length,max_render_ms:Math.max(0,...metrics.renders),
        backwards_steps:reversals,max_blank_top:Math.max(0,...metrics.samples.map(s=>s.blank)),expand:expanded,
        collapse_delta:folded.delta,drag_max_blank:Math.max(...drag.samples.map(s=>s.blank))};
      console.log('HISTORY_METRICS '+JSON.stringify(summary));
      if(process.env.YTOOLS_SCROLL_METRICS)fs.writeFileSync(process.env.YTOOLS_SCROLL_METRICS,JSON.stringify({...summary,...metrics}));
      assert.ok(expanded&&expanded.expanded==='true','process group did not expand');
      assert.ok(summary.max_blank_top<160,'history window left a large blank area: '+summary.max_blank_top);
      assert.ok(summary.window_renders<60,'history windows were rebuilt repeatedly: '+summary.window_renders);
      assert.equal(summary.backwards_steps,0,'reading position reversed while scrolling in one direction');
    } else if (flow === 'editor') {
      await page.goto(cfg.origin + '/ui/?session=alpha');
      await page.waitForFunction(() => window.App && App.state.sessionId === 'alpha' && App.chatInner.textContent.includes('Fixture reply alpha'));
      const rendered = await page.evaluate(() => {
        const block=App.buildToolBlock('edit_file'); App.chatInner.appendChild(block.wrap);
        App.applyToolResult(block,'edit_file',JSON.stringify({success:true,message:'fixture edit',path:'example.txt'}),
          {diff:'--- a/example.txt\n+++ b/example.txt\n@@ -1 +1 @@\n-old\n+changed',lines_added:1,lines_removed:1,path:'example.txt'}, {path:'example.txt'});
        block.finish(); block.wrap.querySelector('.tool-block-head').click();
        return {diff:!!block.wrap.querySelector('.diff-view'), text:block.wrap.textContent};
      });
      assert.ok(rendered.diff && rendered.text.includes('changed'), 'real message diff view was not mounted');
      await page.locator('#fileChangesBtn').click();
      const [editor] = await Promise.all([
        context.waitForEvent('page'),
        page.locator('.fh-file-row').filter({ hasText: 'example.txt' }).click(),
      ]);
      await editor.waitForLoadState();
      await editor.locator('.eh-input').first().waitFor();
      assert.equal(await editor.evaluate(() => YtoolsAuth.user.id), cfg.alice);
      await editor.locator('.eh-input').first().fill('edited-by-browser');
      const saving = editor.waitForResponse(response => response.url().endsWith('/file_diff/save'));
      await editor.locator('#ehSave').click();
      const saved = await saving;
      assert.equal(saved.status(), 200, await saved.text());
      await poll(async () => (await context.request.get(cfg.origin + '/file_diff/content?session_id=alpha&key=' + cfg.file_key)).json(),
        value => value.content && value.content.includes('edited-by-browser'), 'editor save was not persisted');
      await editor.locator('#ehRevert').click();
      await editor.locator('#ehConfirmOk').click();
      await poll(async () => (await context.request.get(cfg.origin + '/file_diff/content?session_id=alpha&key=' + cfg.file_key)).json(),
        value => value.content === 'original\n', 'editor rollback did not restore baseline');
      await editor.close();
      await page.reload();
      await page.waitForFunction(() => window.App && App.fileHistory);
      assert.equal(await page.evaluate(() => YtoolsAuth.user.id), cfg.alice);
    } else if (flow === 'mfa_rotation') {
      const account = await context.newPage();
      await account.goto(cfg.origin + '/ui/account.html');
      await account.locator('#mfaManageForm').waitFor();
      await account.locator('#mfaManageForm [name=password]').fill(cfg.password);
      await account.locator('#mfaManageForm [name=code]').fill(cfg.recovery[0]);
      const bindingResponse = account.waitForResponse(response => response.url().endsWith('/auth/mfa/manage/start'));
      await account.locator('#mfaManageSubmit').click();
      const binding = (await (await bindingResponse).json()).binding;
      await account.locator('#mfaManageEnrollment img').waitFor({ state: 'visible' });
      await account.waitForFunction(() => document.querySelector('#mfaManageEnrollment img').naturalWidth > 0);
      assert.ok(await account.locator('#mfaManageEnrollment img').evaluate(image => image.complete && image.naturalWidth > 0));
      await account.locator('#mfaManageForm [name=new_code]').fill(totp(binding.secret));
      await account.locator('#mfaManageSubmit').click();
      await account.locator('#mfaDeviceList').getByText('Google Authenticator', { exact: true }).waitFor();
      const statuses = [];
      page.on('response', response => { if (response.url().endsWith('/user/profile')) statuses.push(response.status()); });
      const status = await page.evaluate(async () => (await fetch('/user/profile', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: 'fixture-renamed' }) })).status);
      assert.equal(status, 200);
      assert.deepEqual(statuses, [403, 200]);
    } else if (flow === 'account_switch') {
      await page.goto(cfg.origin + '/ui/account.html');
      await page.waitForFunction(() => window.YtoolsAuth);
      await page.evaluate(() => localStorage.setItem('draft', 'alice-private'));
      const csrf = await page.evaluate(() => YtoolsAuth.csrf);
      assert.equal((await context.request.post(cfg.origin + '/auth/logout', { headers: { Origin: cfg.origin, 'X-CSRF-Token': csrf } })).status(), 200);
      const bob = await context.newPage();
      await login(bob, 'bob');
      assert.equal(await bob.evaluate(() => localStorage.getItem('draft')), null);
      await bob.evaluate(() => localStorage.setItem('draft', 'bob-private'));
      assert.equal((await context.request.get(cfg.origin + '/chat_history/file?session_id=alpha')).status(), 404);
      assert.equal((await context.request.get(cfg.origin + '/file_diff/list?session_id=alpha')).status(), 404);
      await page.evaluate(() => fetch('/auth/me').catch(() => null)).catch(() => {});
      await poll(() => bob.evaluate(() => localStorage.getItem('draft')), value => value === 'bob-private', 'account cleanup removed another account draft');
      await poll(() => bob.evaluate(() => Object.keys(localStorage).includes('ytools:user:alice:draft')),
        value => value === false, 'stale account page did not clear its own draft');
    } else if (flow === 'history_pages') {
      const requests=[],workers=[];
      page.on('request',request=>{if(request.url().includes('/chat_history/'))requests.push(request.url());});
      page.on('worker',worker=>workers.push(worker.url()));
      await page.goto(cfg.origin+'/ui/?session=alpha');
      await page.waitForFunction(()=>window.App&&App.historyPages&&App.getHistoryWindow().records?.some(record=>record.kind==='user'&&record.round===180));
      assert.equal(await page.locator('.qnav-item').count(),180);
      let rounds=await page.evaluate(()=>App.getHistoryWindow().records.filter(r=>r.kind==='user').map(r=>r.round));
      assert(rounds.length<60,'initial page downloaded full history');
      assert(rounds.includes(180)&&!rounds.includes(1));
      const parsedRound=await page.evaluate(async()=>{
        const text=JSON.stringify({_meta:{}})+'\n'+JSON.stringify({event:'chat_round',events:[{role:'assistant',content:'x'.repeat(1100000)}]});
        return (await HistoryParseAsync.parse(text,{roundOffset:72})).records[0].round;
      });
      assert.equal(parsedRound,73);
      assert(workers.some(url=>url.includes('history_parse_worker.js')),'large page did not use the versioned parser worker');
      assert.equal(requests.filter(url=>url.includes('/chat_history/event?')).length,0,'collapsed result was fetched eagerly');
      const tool=page.locator('.tool-block[data-tool-call-id="tool-180"]');
      await tool.locator('.tool-block-head').evaluate(node=>node.click());
      await poll(()=>tool.locator('.tool-io-out').textContent(),text=>text.length>1000000,'deferred tool result did not load completely');
      const detailCount=requests.filter(url=>url.includes('/chat_history/event?')).length;
      await tool.locator('.tool-block-head').evaluate(node=>node.click());await tool.locator('.tool-block-head').evaluate(node=>node.click());
      assert.equal(requests.filter(url=>url.includes('/chat_history/event?')).length,detailCount,'reopening result repeated the download');
      await page.evaluate(()=>{
        window.serverGapHeight=0;
        new MutationObserver(()=>document.querySelectorAll('.history-page-gap').forEach(node=>{
          if(node.isConnected)window.serverGapHeight=Math.max(window.serverGapHeight,node.getBoundingClientRect().height);
        })).observe(App.chatInner,{childList:true});
        App.renderHistoryWindowAt(0);App.chatScroll.scrollTop=0;
      });
      await poll(()=>page.evaluate(()=>App.historyPages.hasRound(160)),Boolean,'scrolling to server page edge did not load earlier rounds');
      assert(await page.evaluate(()=>window.serverGapHeight>=28),'page gap has no usable visible hit area');
      await page.locator('.qnav-item').first().evaluate(node=>node.click());
      await poll(()=>page.evaluate(()=>App.getHistoryWindow().records?.some(r=>r.kind==='user'&&r.round===1)||false),Boolean,'first question did not load');
      await page.locator('.msg-user[data-round="1"]').waitFor({state:'visible'});
      assert.equal(await page.locator('.qnav-item').count(),180);
      await page.locator('.qnav-item').nth(89).evaluate(node=>node.click());
      await page.locator('.msg-user[data-round="90"]').waitFor({state:'visible'});
      rounds=await page.evaluate(()=>App.getHistoryWindow().records.filter(r=>r.kind==='user').map(r=>r.round));
      assert(rounds.length<140,'jump filled every missing round');
      assert.equal(new Set(rounds).size,rounds.length,'overlapping pages duplicate questions');
      await page.evaluate(()=>App.chatScroll.dispatchEvent(new WheelEvent('wheel',{deltaY:-1})));
      const beforeY=await page.locator('.msg-user[data-round="90"]').evaluate(node=>node.getBoundingClientRect().top);
      await page.evaluate(()=>App.historyPages.loadBefore(80));
      await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
      const afterY=await page.locator('.msg-user[data-round="90"]').evaluate(node=>node.getBoundingClientRect().top);
      assert(Math.abs(afterY-beforeY)<3,'loading earlier rounds moved the reading anchor: '+(afterY-beforeY));
      assert.equal(requests.filter(url=>url.includes('/chat_history/file?')).length,0,'normal view fell back to whole-file download');
      await page.evaluate(async()=>{
        const response=await fetch('/chat_with_tool',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({session_id:'alpha',messages:[{role:'user',content:'paging live update'}]})});
        if(!response.ok)throw new Error('fixture task failed '+response.status);
        await response.text();
      });
      await page.evaluate(()=>App.historyPages.ensureRound(50));
      await poll(()=>page.evaluate(()=>App.getHistoryWindow().records?.some(r=>r.kind==='user'&&r.round===50)||false),Boolean,'stale snapshot did not refresh');
      assert.equal(await page.locator('.qnav-item').count(),181);
      await page.locator('.qnav-item').last().evaluate(node=>node.click());
      await page.locator('.msg-user[data-round="181"]').waitFor({state:'visible'});
    } else if (flow === 'stream_reload') {
      const first = await page.evaluate(async () => {
        const controller = new AbortController();
        const response = await fetch('/chat_with_tool', { method: 'POST', signal: controller.signal,
          headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ session_id: 'alpha',
            messages: [{ role: 'user', content: 'fixture streaming question' }] }) });
        if (!response.ok) return { status: response.status, text: await response.text() };
        const reader = response.body.getReader();
        const chunk = await reader.read(); controller.abort();
        return { status: response.status, text: new TextDecoder().decode(chunk.value) };
      });
      assert.equal(first.status, 200, first.text);
      assert.match(first.text, /data:/);
      await page.reload();
      await page.waitForFunction(() => window.YtoolsAuth && window.App);
      await poll(async () => (await context.request.get(cfg.origin + '/chat_history/file?session_id=alpha')).text(),
        value => value.includes('fixture-chunk-11'), 'disconnected stream did not finish and persist');
      await page.goto(cfg.origin + '/ui/?session=alpha');
      await page.waitForFunction(() => window.App && App.fileHistory);
      await page.getByText('fixture-chunk-11', { exact: false }).first().waitFor();
      assert.equal(await page.evaluate(() => YtoolsAuth.user.id), cfg.alice);
    }
    assert.deepEqual(errors, [], 'uncaught browser errors');
    console.log('PASS ' + flow);
  } finally { await context.close(); await browser.close(); }
})().catch(error => { console.error(error.stack); process.exitCode = 1; });
