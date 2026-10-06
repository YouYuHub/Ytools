const assert = require("node:assert/strict");
const test = require("node:test");
const fs = require("node:fs");
const path = require("node:path");

const source = fs.readFileSync(path.join(__dirname, "../js/app/messages.js"), "utf8");
const start = source.indexOf("  function attachLiveRoundEntry(");
const end = source.indexOf("  function beginUserMessageEdit(", start);
const attach = new Function("attachUserEditAction", source.slice(start, end) + "\nreturn attachLiveRoundEntry;")(() => {});

test("重连复用历史用户气泡时，新回复及其子任务块仍归属原轮次", () => {
  const reply = { dataset: {}, childNodes: [{ className: "agent-block" }] };
  attach({ userNode: { dataset: { round: "2" } }, messageNode: reply }, 2);
  assert.equal(reply.dataset.round, "2");
  // 编辑重发按 data-round 删除整个回复容器，内部子任务随之清理。
  const next = { dataset: { round: "3" } };
  const remaining = [reply, next].filter(node => node.dataset.round !== "2");
  assert.deepEqual(remaining, [next]);
});

test("缺少旧后端提问部件时仍标记回复归属，并避免生成不完整编辑数据", () => {
  const user = { dataset: {} };
  const reply = { dataset: {} };
  attach({ userNode: user, messageNode: reply, userContent: null }, 4);
  assert.equal(reply.dataset.round, "4");
  assert.equal(user._editData, undefined);
});
