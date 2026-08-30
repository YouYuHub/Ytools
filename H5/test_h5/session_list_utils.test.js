"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const utils = require("../js/session_list_utils.js");

test("timeValue: 解析 yyyy-MM-dd HH:mm:ss", function () {
  assert.equal(utils.timeValue("2026-08-08 18:33:11"), Date.parse("2026-08-08T18:33:11"));
  assert.ok(utils.timeValue("2026-08-08 18:33:11") > utils.timeValue("2026-08-03 17:13:01"));
});

test("timeValue: 空值/非法值归零", function () {
  assert.equal(utils.timeValue(""), 0);
  assert.equal(utils.timeValue("   "), 0);
  assert.equal(utils.timeValue(undefined), 0);
  assert.equal(utils.timeValue(null), 0);
  assert.equal(utils.timeValue("not-a-time"), 0);
});

test("firstQuestionTitle: 去空格并截断到 40 字符", function () {
  assert.equal(utils.firstQuestionTitle("  帮我写代码  "), "帮我写代码");
  const long = "\u4e00".repeat(60);
  assert.equal(utils.firstQuestionTitle(long).length, utils.TITLE_MAX);
  assert.equal(utils.firstQuestionTitle(long), long.slice(0, 40));
  assert.equal(utils.firstQuestionTitle(""), "");
  assert.equal(utils.firstQuestionTitle(null), "");
  assert.equal(utils.firstQuestionTitle(42), "42");
});

test("sortRows: 后更新的排前面", function () {
  const rows = [
    { id: "old", title: "a", updated: "2026-08-01 10:00:00", created: "2026-08-01 08:00:00" },
    { id: "new", title: "b", updated: "2026-08-08 18:33:11", created: "2026-08-08 08:00:00" },
  ];
  assert.deepEqual(utils.sortRows(rows, {}).map(function (r) { return r.id; }), ["new", "old"]);
});

test("sortRows: updated 相同用 created 兜底", function () {
  const rows = [
    { id: "oldfile", title: "a", updated: "2026-08-08 18:33:11", created: "2026-08-03 08:00:00" },
    { id: "newfile", title: "b", updated: "2026-08-08 18:33:11", created: "2026-08-08 08:00:00" },
  ];
  assert.deepEqual(utils.sortRows(rows, {}).map(function (r) { return r.id; }), ["newfile", "oldfile"]);
});

test("sortRows: 本地 recency 让刚发送/重命名的会话置顶（即使后端 updated 落后）", function () {
  const rows = [
    { id: "backend", title: "a", updated: "2026-08-08 12:00:00", created: "2026-08-08 08:00:00" },
    { id: "draft", title: "b", updated: "", created: "" },
  ];
  const recency = { draft: Date.now() };
  assert.deepEqual(utils.sortRows(rows, recency).map(function (r) { return r.id; }), ["draft", "backend"]);
});

test("sortRows: 无时间且无 recency 的排最后", function () {
  const rows = [
    { id: "a", title: "a", updated: "2026-08-08 12:00:00", created: "" },
    { id: "broken", title: "b", updated: "", created: "" },
  ];
  assert.deepEqual(utils.sortRows(rows, {}).map(function (r) { return r.id; }), ["a", "broken"]);
});

test("sortRows: 返回新数组，不修改入参", function () {
  const rows = [{ id: "x", title: "x", updated: "2026-08-08 12:00:00", created: "" }];
  const sorted = utils.sortRows(rows, {});
  assert.notEqual(sorted, rows);
  assert.equal(rows.length, 1);
  assert.equal(sorted.length, 1);
});

test("sortRows: 完全相同时保持原有顺序（稳定）", function () {
  const rows = [
    { id: "c", title: "c", updated: "2026-08-08 12:00:00", created: "2026-08-01 08:00:00" },
    { id: "b", title: "b", updated: "2026-08-08 12:00:00", created: "2026-08-01 08:00:00" },
    { id: "a", title: "a", updated: "2026-08-08 12:00:00", created: "2026-08-01 08:00:00" },
  ];
  assert.deepEqual(utils.sortRows(rows, {}).map(function (r) { return r.id; }), ["c", "b", "a"]);
});