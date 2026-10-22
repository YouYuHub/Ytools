const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname,'../js/app/chat.js'),'utf8');
const helper = source.slice(source.indexOf('  async function requestStopRunningGeneration('),source.indexOf('  async function confirmEditResend('));

async function confirm(suspended,accept=true) {
  let dialog,stops=0;
  const context={state:{},App:{
    openConfirmDialog(options) { dialog=options; if(accept) options.onConfirm(); else options.onCancel(); },
    refreshComposerButtons() {},
  },API:{async stopChat(session) { assert.equal(session,'s'); stops++; }}};
  vm.createContext(context); vm.runInContext(helper,context);
  const result=await context.requestStopRunningGeneration('s',suspended);
  return {dialog,stops,result};
}

test('editing a suspended task describes interruption rather than a running generation',async () => {
  const {dialog,stops,result}=await confirm(true);
  assert.equal(dialog.title,'任务已中断');
  assert.match(dialog.message,/实际未在运行/);
  assert.equal(dialog.confirmText,'结束旧任务并重发');
  assert.equal(stops,1); assert.equal(result,true);
});

test('editing a live generation still confirms stopping it; cancelling preserves the task',async () => {
  const live=await confirm(false);
  assert.equal(live.dialog.title,'任务正在进行中');
  assert.equal(live.dialog.confirmText,'停止生成并重发');
  const cancelled=await confirm(true,false);
  assert.equal(cancelled.result,false); assert.equal(cancelled.stops,0);
});
