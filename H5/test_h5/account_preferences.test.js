const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM, VirtualConsole } = require('jsdom');
const source = name => fs.readFileSync(path.join(__dirname, '../js/', name), 'utf8');
function setup(options = {}) {
  const dom = new JSDOM('<script id="account-context" type="application/json"></script>', {
    url: 'https://fixture.example/ui/', runScripts: 'outside-only', virtualConsole: new VirtualConsole()
  });
  const w = dom.window, id = options.id || 'alice', calls = [];
  let server = structuredClone(options.server || {version: 0, ui: {}}), online = true, hold = null;
  w.document.getElementById('account-context').textContent = JSON.stringify({user:{id, role:'member'}, csrf:'fixture-csrf', preferences: options.legacy ? undefined : server});
  for (const [key, value] of Object.entries(options.storage || {})) w.localStorage.setItem(key, value);
  w.Headers = Headers;
  w.matchMedia = () => ({matches:true,addEventListener(){}});
  w.fetch = async (url, opts = {}) => {
    calls.push({url, opts});
    if (!online) throw new Error('offline');
    if (hold) { const wait = hold; hold = null; await wait; }
    if (opts.method === 'PATCH') {
      const body = JSON.parse(opts.body);
      for (const [key,value] of Object.entries(body.ui)) if (!body.only_if_unset || !Object.hasOwn(server.ui,key)) server.ui[key] = value;
      server.version++;
    }
    return {ok:true,status:200,headers:new Headers({'X-Ytools-Account':id}),json:async()=>structuredClone(server)};
  };
  const rawGet = w.Storage.prototype.getItem;
  w.eval(source('security_client.js')); w.eval(source('account_preferences.js')); w.eval(source('theme.js'));
  return {dom,w,calls,rawGet, server:()=>server, online:value=>online=value, hold:promise=>hold=promise};
}
const plain = value => JSON.parse(JSON.stringify(value));
test('server settings hydrate before theme and do not import shared or other-account cache', async t => {
  const f = setup({server:{version:3,ui:{theme:'light',accent:'pink',task_status_visible:false}},storage:{'ytools-theme-preference':'dark','ytools:user:bob:ytools-accent-preference':'green'}});
  t.after(()=>f.dom.window.close());
  assert.equal(f.w.document.documentElement.dataset.theme,'light');
  assert.equal(f.w.document.documentElement.dataset.accent,'pink');
  assert.equal(f.w.localStorage.getItem('ytools-task-status-visible'),'0');
  await f.w.AccountPreferences.flush(); assert.equal(f.calls.length,0);
});
test('migrate only missing account-scoped UI fields using conditional merge', async t => {
  const f = setup({server:{version:2,ui:{theme:'dark'}},storage:{'ytools:user:alice:ytools-theme-preference':'light','ytools:user:alice:ytools-accent-preference':'green','ytools:user:alice:ytools-sidebar-width':'not-a-number'}});
  t.after(()=>f.dom.window.close());
  await f.w.AccountPreferences.flush();
  assert.deepEqual(f.server().ui,{theme:'dark',accent:'green'});
  assert.equal(JSON.parse(f.calls[0].opts.body).only_if_unset,true);
  assert.equal(f.calls[0].opts.headers.get('X-Ytools-Expected-Account'),'alice');
});
test('legacy theme name migrates only inside this account namespace', async t => {
  const f=setup({storage:{'ytools:user:alice:theme-preference':'light','theme-preference':'dark'}});
  t.after(()=>f.dom.window.close()); await f.w.AccountPreferences.flush();
  assert.equal(f.server().ui.theme,'light');
});
test('edits batch to server; logout keeps only safe UI values and clears drafts', async t => {
  const f=setup(); t.after(()=>f.dom.window.close());
  f.w.ThemeManager.setPreference('light'); f.w.ThemeManager.setAccentPreference('pink');
  f.w.localStorage.setItem('ytools-task-status-visible','0'); f.w.localStorage.setItem('ytools-session-groups-open','1');
  f.w.localStorage.setItem('draft','private text'); f.w.sessionStorage.setItem('draft','private session text');
  await f.w.AccountPreferences.flush();
  assert.deepEqual(f.server().ui,{theme:'light',accent:'pink',task_status_visible:false,groups_expanded:true});
  f.w.YtoolsAuth.clearLocal();
  assert.equal(f.w.localStorage.getItem('draft'),null); assert.equal(f.w.sessionStorage.getItem('draft'),null);
  assert.equal(f.w.localStorage.getItem('ytools-theme-preference'),'light');
  assert.ok(f.calls[0].opts.headers.get('X-CSRF-Token'));
});
test('offline edits survive logout and next login then converge to server', async t => {
  const f=setup(); t.after(()=>f.dom.window.close()); f.online(false);
  f.w.ThemeManager.setPreference('light'); await f.w.AccountPreferences.flush(); f.w.YtoolsAuth.clearLocal();
  const storage = {};
  for (let i=0;i<f.w.localStorage.length;i++) { const key=f.w.localStorage.key(i); storage[key]=f.rawGet.call(f.w.localStorage,key); }
  assert.ok(storage['ytools:user:alice:ytools-preference-outbox']);
  const next=setup({storage}); t.after(()=>next.dom.window.close());
  assert.equal(next.w.document.documentElement.dataset.theme,'light'); await next.w.AccountPreferences.flush();
  assert.equal(next.server().ui.theme,'light');
});
test('a newer edit during an in-flight save cannot be dropped by its old response', async t => {
  const f=setup(); t.after(()=>f.dom.window.close()); let release;
  f.hold(new Promise(resolve=>release=resolve));
  f.w.ThemeManager.setPreference('light'); const saving=f.w.AccountPreferences.flush();
  f.w.ThemeManager.setPreference('dark'); release(); await saving;
  assert.equal(f.server().ui.theme,'dark'); assert.equal(f.w.ThemeManager.getPreference(),'dark');
  assert.equal(f.calls.length,2);
});
test('local removal saves an explicit default and invalid cache/outbox is discarded', async t => {
  const f=setup({server:{version:1,ui:{theme:'light'}},storage:{'ytools:user:alice:ytools-preference-outbox':'{"constructor":{"value":"x","seed":false}}'}});
  t.after(()=>f.dom.window.close());
  f.w.localStorage.removeItem('ytools-theme-preference'); await f.w.AccountPreferences.flush();
  assert.equal(f.server().ui.theme,'system');
  assert.equal(f.w.AccountPreferences.retain('ytools-sidebar-width','9999'),false);
  assert.equal(f.w.AccountPreferences.retain('csrf','secret'),false);
});
test('old backend does not receive unsupported preference writes during rolling deployment', async t => {
  const f=setup({legacy:true});t.after(()=>f.dom.window.close());
  f.w.ThemeManager.setPreference('light'); await f.w.AccountPreferences.flush();
  assert.equal(f.calls.length,0); f.w.YtoolsAuth.clearLocal();
  assert.equal(f.w.ThemeManager.getPreference(),'light');
});
test('every private page loads preferences synchronously after auth and before theme', () => {
  for (const name of ['index.html','editor.html','account.html']) {
    const html=fs.readFileSync(path.join(__dirname,'../',name),'utf8');
    assert.match(html, /<script src="js\/account_preferences\.js[^\"]*"><\/script>/);
    assert.ok(html.indexOf('security_client.js')<html.indexOf('account_preferences.js'));
    assert.ok(html.indexOf('account_preferences.js')<html.indexOf('js/theme.js'));
  }
});
