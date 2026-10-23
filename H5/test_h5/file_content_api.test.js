const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

test('文件快照 API 可传字符分页，并保持旧三参数调用', async () => {
  const calls=[], window={};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../js/api.js'),'utf8'), {
    window, localStorage:{getItem(){return null;},setItem(){},removeItem(){}},
    fetch:async url=>{calls.push(new URL(url));return {ok:true,json:async()=>({content:'part'})};},
  });
  await window.API.fileContent('会话 /1','abc',2,{offset:123,limit:456});
  assert.equal(calls[0].searchParams.get('session_id'),'会话 /1');
  assert.equal(calls[0].searchParams.get('v'),'2');
  assert.equal(calls[0].searchParams.get('offset'),'123');
  assert.equal(calls[0].searchParams.get('limit'),'456');
  await window.API.fileContent('s','abc',0);
  assert.equal(calls[1].searchParams.get('v'),'0');
  assert.equal(calls[1].searchParams.has('offset'),false);
});
