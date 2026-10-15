const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

test('权限决定仍绑定会话和代次，接口无需控制码且不发送认证头', async () => {
  const calls = [], replaced = [], values = new Map(), window = {};
  const storage = { getItem: key => values.get(key) || null, setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key) };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../js/api.js'), 'utf8'), {
    window, localStorage: storage, sessionStorage: storage, URLSearchParams,
    location: { origin: 'http://localhost:123', protocol: 'http:', pathname: '/ui/', search: '', hash: '#control_token=private-code' },
    history: { replaceState: (...args) => replaced.push(args) },
    fetch: async (url, options) => { calls.push({ url, options }); return { ok: true, json: async () => ({}) }; }
  });
  await window.API.decideApproval({ id: 'a1', session_id: '会话1', epoch: 'epoch1' }, 'deny');
  assert.deepEqual(JSON.parse(calls[0].options.body), { session_id: '会话1', epoch: 'epoch1', decision: 'deny' });
  assert.equal(calls[0].options.headers.Authorization, undefined);
  assert.equal(new URL(calls[0].url).origin, 'http://localhost:123');
  await window.API.getWorkDirConfig('会话1');
  assert.ok(!calls[1].options || !calls[1].options.headers || !calls[1].options.headers.Authorization);
  await window.API.setPermissions('ask', '会话1');
  assert.equal(calls[2].options.headers.Authorization, undefined);
  assert.equal(window.API.setControlToken, undefined);
});
