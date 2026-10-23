const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');

test('大文件快照视图不生成全文编辑，快捷键不能保存截断正文，仍能回退', async t => {
  const dom = new JSDOM(fs.readFileSync(path.join(__dirname, '../editor.html'), 'utf8'), {
    runScripts: 'outside-only', url: 'http://localhost/editor.html?session_id=s&key=12345678',
  });
  t.after(() => dom.window.close());
  const w = dom.window, d = w.document;
  let saves = 0, rollbacks = 0;
  w.ThemeManager = {getPreference: () => 'dark', setPreference() {}};
  w.API = {
    fileFullView: async () => ({snapshot_only:true,truncated:true,rows:[],baseline_v:0,current_v:1}),
    fileContent: async () => ({snapshot_only:true,content:'partial',path:'C:/large.txt',display_path:'large.txt'}),
    fileVersions: async () => ({versions:[{v:1,round:1}]}),
    fileSave: async () => { saves++; },
    fileRollback: async () => { rollbacks++; },
  };
  w.eval(fs.readFileSync(path.join(__dirname, '../js/app/editor.js'), 'utf8'));
  await new Promise(resolve => setImmediate(resolve));
  assert.match(d.querySelector('#ehBody').textContent, /完整压缩快照/);
  assert.equal(d.querySelectorAll('#ehBody textarea').length, 0);
  assert.equal(d.querySelector('#ehSave').disabled, true);
  d.dispatchEvent(new w.KeyboardEvent('keydown', {key:'s',ctrlKey:true,bubbles:true}));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(saves, 0);
  d.querySelector('#ehRevert').click();
  d.querySelector('#ehConfirmOk').click();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(rollbacks, 1);
});
