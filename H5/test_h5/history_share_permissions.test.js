"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../js/app/history.js"), "utf8");
const start = source.indexOf("  function openImportConflict(");
const end = source.indexOf("  /** zip 分享包导入流程", start);

function node() {
  const children = [], classes = new Set();
  return { children, dataset: {}, textContent: "", value: "", disabled: false,
    classList: { add: c => classes.add(c), remove: c => classes.delete(c) },
    setAttribute() {}, appendChild(child) { children.push(child); }, querySelectorAll() { return []; },
  };
}
function harness(result) {
  const list = node(), hint = node(), summary = node(), submit = node(), calls = [];
  const context = {
    pendingImport: null, document: { createElement: node }, el: (_tag, _cls, text) => ({ textContent: text }),
    importConflictList: list, importConflictHint: hint, importConflictSummary: summary,
    importConflictSubmit: submit, importConflictModal: node(),
    closeImportConflictFn: () => calls.push("close"), toast: message => calls.push(message),
    SessionUtils: { sanitizeSessionId: sid => sid },
    API: { submitImport: async () => result },
    App: { loadSessions: async () => calls.push("sessions"),
      openSession: async sid => calls.push("open:" + sid),
      permissions: { refresh: async () => calls.push("permissions") } },
  };
  vm.createContext(context); vm.runInContext(source.slice(start, end), context);
  return { context, list, hint, submit, calls };
}

test("import preview shows full control, ask and inherit before confirmation", () => {
  const h = harness();
  h.context.openImportConflict({ type: "zip", total: 3, sessions: [
    { session_id: "one", permission_included: true, permission_mode: "full_control" },
    { session_id: "two", permission_included: true, permission_mode: "ask" },
    { session_id: "three", permission_included: true, permission_mode: null },
  ] }, {});
  const infos = h.list.children.map(row => row.children[0].children[1].textContent);
  assert.match(infos[0], /完全控制/); assert.match(infos[1], /询问/); assert.match(infos[2], /跟随账号默认/);
  assert.match(h.hint.textContent, /其他会话不变/);
});

test("legacy packages display no invented permissions", () => {
  const h = harness();
  h.context.openImportConflict({ type: "zip", total: 1, sessions: [{ session_id: "old" }] }, {});
  assert.doesNotMatch(h.list.children[0].children[0].children[1].textContent, /权限/);
  assert.doesNotMatch(h.hint.textContent, /还原.*权限/);
});

test("successful import reloads the final renamed session then its SQLite policy", async () => {
  const h = harness({ imported: [{ session_id: "one-renamed", collision: true }], failed: [], skipped: [] });
  h.context.pendingImport = { file: {}, preview: {} };
  await h.context.confirmImport();
  assert.ok(h.calls.indexOf("open:one-renamed") < h.calls.indexOf("permissions"));
  assert.equal(h.submit.disabled, false);
});

test("failed or skipped imports never refresh another session's policy", async () => {
  const h = harness({ imported: [], failed: [{ session_id: "one" }], skipped: [] });
  h.context.pendingImport = { file: {}, preview: {} };
  await h.context.confirmImport();
  assert.ok(!h.calls.includes("permissions")); assert.equal(h.submit.disabled, false);
});

function realClose(h) {
  const from=source.indexOf('  function closeImportConflictFn('), to=source.indexOf('  // 本地预览 jsonl',from);
  vm.runInContext(source.slice(from,to),h.context);
}
test('an import decision completes only after persistence and view refresh; it cannot be cancelled during commit',async()=>{
  const h=harness({imported:[{session_id:'saved'}],failed:[],skipped:[]});realClose(h);
  let release;h.context.App.openSession=()=>new Promise(resolve=>release=resolve);
  const decision=h.context.openImportConflict({type:'zip',total:1,sessions:[{session_id:'saved'}]}, {name:'one.zip'});
  let done=false;decision.then(()=>done=true);const commit=h.context.confirmImport();
  await new Promise(resolve=>setImmediate(resolve));assert.equal(done,false);assert.equal(h.context.pendingImport.submitting,true);
  h.context.closeImportConflictFn();assert.notEqual(h.context.pendingImport,null);
  const busy=await h.context.openImportConflict({type:'zip',total:0,sessions:[]},{name:'other.zip'});
  assert.equal(busy.status,'skipped');assert.equal(h.context.pendingImport.file.name,'one.zip');
  release();await commit;assert.equal((await decision).status,'imported');assert.equal(h.context.pendingImport,null);
});
test('cancelling one pending file resolves it so the batch can advance',async()=>{
  const h=harness();realClose(h);
  const first=h.context.openImportConflict({type:'jsonl',total:1,sessions:[{session_id:'one'}]},{});
  h.context.closeImportConflictFn();assert.equal((await first).status,'skipped');
  const second=h.context.openImportConflict({type:'jsonl',total:1,sessions:[{session_id:'two'}]},{});
  h.context.closeImportConflictFn();assert.equal((await second).status,'skipped');
});
test('a post-commit refresh failure is reported as saved and never retries the write',async()=>{
  const h=harness({imported:[{session_id:'saved'}],failed:[],skipped:[]});realClose(h);
  h.context.App.loadSessions=async()=>{throw Error('temporary refresh failure');};
  const decision=h.context.openImportConflict({type:'zip',total:1,sessions:[{session_id:'saved'}]},{});
  const result=await h.context.confirmImport();assert.equal(result.status,'imported');assert.equal((await decision).status,'imported');
  assert.ok(h.calls.some(value=>typeof value==='string'&&value.includes('会话已保存')));
});
test('sharing a sidebar session works while the main view is an empty new chat',async()=>{
  const calls=[];const context={SessionUtils:{sanitizeSessionId:value=>value},state:{sessionId:null,hasConversation:false},toast:text=>calls.push(text),
    API:{BASE:''},fetch:async url=>{calls.push(url);return {ok:true,headers:{get:name=>name==='Content-Type'?'application/zip':null},blob:async()=>({})};},
    URL:{createObjectURL:()=> 'blob:fixture',revokeObjectURL(){}},document:{body:{appendChild(){}},createElement:()=>({click:()=>calls.push('download'),remove(){}})},setTimeout:fn=>fn()};
  vm.createContext(context);const from=source.indexOf('  async function downloadSession('),to=source.indexOf('  function openHistoryPicker',from);
  vm.runInContext(source.slice(from,to),context);await context.downloadSession('owned-sidebar');
  assert.ok(calls.includes('/chat_history/export_zip?session_ids=owned-sidebar'));assert.ok(calls.includes('download'));
});
