const assert = require("node:assert/strict");
const test = require("node:test");

const calls = [];
global.HistoryParser = { parseHistory: text => ({ records: [text] }) };
global.Worker = class {
  constructor(url) { this.url = url; calls.push(this); }
  postMessage(text) { this.onmessage({ data: { ok: true, result: { records: [text] } } }); }
  terminate() { this.terminated = true; }
};
require("../js/history_parse_async.js");

test("small history parses directly; large history uses a worker", async () => {
  const small = await global.HistoryParseAsync.parse("short");
  assert.deepEqual(small.records, ["short"]);
  assert.equal(calls.length, 0);
  const largeText = "x".repeat(global.HistoryParseAsync.WORKER_THRESHOLD);
  const large = await global.HistoryParseAsync.parse(largeText);
  assert.deepEqual(large.records, [largeText]);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].terminated, true);
});
