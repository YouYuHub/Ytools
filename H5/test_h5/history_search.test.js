"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const { createController } = require("../js/history_search.js");
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
function harness(options) {
  const requests = [], states = [];
  const controller = createController((query, options) => new Promise((resolve, reject) => {
    requests.push({ query, options, resolve, reject });
  }), (state) => states.push(state), Object.assign({ delay: 5, timeout: 1000 }, options));
  return { controller, requests, states, state: () => controller.getState() };
}
function result(query, ids, more) {
  return { query, results: ids.map((id) => ({ id })), total: 4, session_count: 1,
    next_offset: more == null ? null : more, revision: "v1" };
}
test("debounce sends only the final input; old response cannot publish during debounce", async () => {
  const h = harness({ delay: 15 });
  h.controller.open("old"); await sleep(25);
  h.controller.update("n"); h.controller.update("new");
  assert.equal(h.requests[0].options.signal.aborted, true);
  h.requests[0].resolve(result("old", ["old"])); await sleep(1);
  assert.equal(h.state().query, "new"); assert.deepEqual(h.state().results, []);
  await sleep(25); assert.equal(h.requests.length, 2);
  h.requests[1].resolve(result("new", ["new"])); await sleep(1);
  assert.equal(h.state().results[0].id, "new"); h.controller.close();
});
test("ignored cancellation and out-of-order errors never overwrite newer results", async () => {
  const h = harness(); h.controller.open("old"); await sleep(10);
  h.controller.update("new"); await sleep(10);
  h.requests[1].resolve(result("new", [2])); await sleep(1);
  h.requests[0].reject(new Error("old failure")); await sleep(1);
  assert.equal(h.state().status, "ready"); assert.equal(h.state().results[0].id, 2);
  h.controller.close();
});
test("IME input and clearing input do not request partial or empty searches", async () => {
  const h = harness(); h.controller.open("");
  h.controller.update("中", true); await sleep(12); assert.equal(h.requests.length, 0);
  h.controller.update("中文"); await sleep(12); assert.equal(h.requests.length, 1);
  h.controller.update(" "); h.requests[0].resolve(result("中文", [1])); await sleep(1);
  assert.equal(h.state().status, "idle"); assert.equal(h.requests[0].options.signal.aborted, true);
  h.controller.close();
});
test("closing suppresses all late results; reopening starts a fresh search", async () => {
  const h = harness(); h.controller.open("query"); await sleep(10);
  h.controller.close(); const count = h.states.length;
  h.requests[0].resolve(result("query", [1])); await sleep(1); assert.equal(h.states.length, count);
  h.controller.open("query"); await sleep(10); assert.equal(h.requests.length, 2);
  h.requests[1].resolve(result("query", [2])); await sleep(1); assert.equal(h.state().results[0].id, 2);
  h.controller.close();
});
test("pagination uses snapshot; a changed snapshot automatically restarts", async () => {
  const h = harness(); h.controller.open("query"); await sleep(10);
  h.requests[0].resolve(result("query", [1, 2], 2)); await sleep(1);
  h.controller.more(); await sleep(1);
  assert.equal(h.requests[1].options.offset, 2); assert.equal(h.requests[1].options.revision, "v1");
  h.requests[1].resolve(result("query", [3], 3)); await sleep(1);
  assert.deepEqual(h.state().results.map((r) => r.id), [1, 2, 3]);
  h.controller.more(); h.requests[2].reject(Object.assign(new Error("Changed"), { status: 409 })); await sleep(1);
  assert.equal(h.requests[3].options.offset, 0); assert.equal(h.state().results.length, 0);
  h.requests[3].resolve(result("query", [4])); await sleep(1); assert.equal(h.state().results[0].id, 4);
  h.controller.close();
});
test("Enter flushes immediately; network errors remain retryable", async () => {
  const h = harness({ delay: 200 }); h.controller.open("query"); h.controller.flush();
  assert.equal(h.requests.length, 1); h.requests[0].reject(new Error("network")); await sleep(1);
  assert.equal(h.state().status, "error"); h.controller.flush();
  h.requests[1].resolve(result("query", [1])); await sleep(1); assert.equal(h.state().status, "ready");
  h.controller.close();
});

test("scope changes abort old pagination, reset counts, and reject late results", async () => {
  const h=harness();h.controller.open('hit');await sleep(10);
  assert.equal(h.requests[0].options.includeInternal,false);
  assert.equal(h.requests[0].options.includeFilenames,false);
  h.requests[0].resolve(result('hit',[1],1));await sleep(1);
  h.controller.more();
  h.controller.setScope({includeInternal:true,includeFilenames:false});
  assert.equal(h.requests[1].options.signal.aborted,true);
  assert.equal(h.state().total,0);assert.equal(h.state().next_offset,null);
  h.requests[1].resolve(result('hit',['stale']));await sleep(10);
  assert.deepEqual(h.state().results,[]);
  assert.equal(h.requests[2].options.offset,0);assert.equal(h.requests[2].options.revision,null);
  assert.equal(h.requests[2].options.includeInternal,true);
  h.requests[2].resolve(result('hit',[2],1));await sleep(1);
  h.controller.more();assert.equal(h.requests[3].options.includeInternal,true);
  h.controller.setScope({includeInternal:false,includeFilenames:true});await sleep(10);
  assert.equal(h.requests[4].options.includeInternal,false);assert.equal(h.requests[4].options.includeFilenames,true);
  h.requests[4].resolve(result('hit',[3]));await sleep(1);
  assert.deepEqual(h.state().results.map(r=>r.id),[3]);h.controller.close();
});

test("scope switches respect Chinese IME composition and survive closing/reopening", async () => {
  const h=harness();h.controller.open('');h.controller.update('中',true);
  h.controller.setScope({includeInternal:true},true);await sleep(10);
  assert.equal(h.requests.length,0);
  h.controller.update('中文');await sleep(10);
  assert.equal(h.requests[0].query,'中文');assert.equal(h.requests[0].options.includeInternal,true);
  h.controller.close();h.controller.open('中文');await sleep(10);
  assert.equal(h.requests[1].options.includeInternal,true);h.controller.close();
});
