"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const utils = require("../js/session_group_utils.js");

test("normalizeGroups: 过滤非法条目并按 order 排序", function () {
  const groups = utils.normalizeGroups([
    { id: "g-b", name: "乙", order: 2 },
    { id: "", name: "无id" },
    { id: "g-a", name: "甲", order: 1 },
    { id: "g-c", name: "" },
    null,
    "bad",
    { id: "g-d", name: "丁" },
  ]);
  assert.deepEqual(groups.map(function (g) { return g.id; }), ["g-d", "g-a", "g-b"]);
  assert.equal(groups[0].collapsed, false);
  assert.equal(groups[1].name, "甲");
});

test("normalizeGroups: 重复 id 只保留首个", function () {
  const groups = utils.normalizeGroups([
    { id: "g-1", name: "先" },
    { id: "g-1", name: "后" },
  ]);
  assert.equal(groups.length, 1);
  assert.equal(groups[0].name, "先");
});

test("normalizeGroups: 名称超长截断到 40", function () {
  const groups = utils.normalizeGroups([{ id: "g-1", name: "长".repeat(80) }]);
  assert.equal(groups[0].name.length, utils.GROUP_NAME_MAX);
});

test("normalizeAssignments: 过滤空值/非字符串并去空白", function () {
  const map = utils.normalizeAssignments({
    "s1": " g-1 ",
    "s2": "",
    "s3": null,
    "s4": 123,
    "s5": "g-2",
  });
  assert.equal(map["s1"], "g-1");
  assert.equal(map["s2"], undefined);
  assert.equal(map["s3"], undefined);
  assert.equal(map["s4"], undefined);
  assert.equal(map["s5"], "g-2");
});

test("bucketSessions: 按归属分桶，未知分组归未分组", function () {
  const groups = utils.normalizeGroups([
    { id: "g-a", name: "甲", order: 1 },
    { id: "g-b", name: "乙", order: 2 },
  ]);
  const assignments = utils.normalizeAssignments({
    "s1": "g-a",
    "s2": "g-b",
    "s3": "g-a",
    "s4": "g-deleted",
  });
  const rows = [
    { id: "s3", title: "3" },
    { id: "s1", title: "1" },
    { id: "s4", title: "4" },
    { id: "s2", title: "2" },
    { id: "s5", title: "5" },
  ];
  const buckets = utils.bucketSessions(rows, groups, assignments);
  assert.deepEqual(buckets.map(function (b) { return b.group.id; }), ["g-a", "g-b"]);
  assert.deepEqual(buckets[0].sessions.map(function (r) { return r.id; }), ["s3", "s1"]);
  assert.deepEqual(buckets[1].sessions.map(function (r) { return r.id; }), ["s2"]);
});

test("bucketSessions: 空输入安全", function () {
  assert.deepEqual(utils.bucketSessions(null, null, null), []);
  assert.deepEqual(utils.bucketSessions([], [], {}), []);
});

test("validateGroupName: 空名拒绝、控制字符折叠、超长截断", function () {
  assert.equal(utils.validateGroupName("   ").ok, false);
  assert.equal(utils.validateGroupName("").ok, false);
  assert.equal(utils.validateGroupName(null).ok, false);

  const multi = utils.validateGroupName(" 多行\n名字\t测试 ");
  assert.equal(multi.ok, true);
  assert.equal(multi.name, "多行 名字 测试");

  const long = utils.validateGroupName("超".repeat(100));
  assert.equal(long.ok, true);
  assert.equal(long.name.length, utils.GROUP_NAME_MAX);
});

// ---------- bucketForBulk（多选分组视图分桶） ----------

test("bucketForBulk: 已定义分组按注册顺序 + 未分组殿后", function () {
  const groups = utils.normalizeGroups([
    { id: "g-a", name: "甲", order: 1 },
    { id: "g-b", name: "乙", order: 2 },
  ]);
  const assignments = utils.normalizeAssignments({
    "s1": "g-a",
    "s2": "g-b",
    "s3": "g-a",
    "s4": "g-deleted", // 未知分组 → 未分组
  });
  const rows = [
    { id: "s3", title: "3" },
    { id: "s1", title: "1" },
    { id: "s4", title: "4" },
    { id: "s2", title: "2" },
    { id: "s5", title: "5" }, // 无归属 → 未分组
  ];
  const buckets = utils.bucketForBulk(rows, groups, assignments);
  assert.deepEqual(buckets.map(function (b) { return b.key; }),
    ["g-a", "g-b", utils.UNGROUPED_KEY]);
  assert.equal(buckets[0].name, "甲");
  assert.equal(buckets[2].name, "未分组");
  assert.deepEqual(buckets[0].sessions.map(function (r) { return r.id; }), ["s3", "s1"]);
  assert.deepEqual(buckets[1].sessions.map(function (r) { return r.id; }), ["s2"]);
  // 未分组按「最近」入参顺序：s4 在 s5 前
  assert.deepEqual(buckets[2].sessions.map(function (r) { return r.id; }), ["s4", "s5"]);
});

test("bucketForBulk: 无未分组会话时不产生未分组桶", function () {
  const groups = utils.normalizeGroups([{ id: "g-a", name: "甲", order: 1 }]);
  const assignments = utils.normalizeAssignments({ "s1": "g-a" });
  const rows = [{ id: "s1", title: "1" }];
  const buckets = utils.bucketForBulk(rows, groups, assignments);
  assert.deepEqual(buckets.map(function (b) { return b.key; }), ["g-a"]);
});

test("bucketForBulk: 空分组保留（可显示空组头），全空输入安全", function () {
  const groups = utils.normalizeGroups([
    { id: "g-a", name: "甲", order: 1 },
    { id: "g-empty", name: "空组", order: 2 },
  ]);
  const assignments = utils.normalizeAssignments({ "s1": "g-a" });
  const rows = [{ id: "s1", title: "1" }];
  const buckets = utils.bucketForBulk(rows, groups, assignments);
  assert.deepEqual(buckets.map(function (b) { return b.key; }), ["g-a", "g-empty"]);
  assert.deepEqual(buckets[1].sessions, []);

  assert.deepEqual(utils.bucketForBulk(null, null, null), []);
  assert.deepEqual(utils.bucketForBulk([], [], {}), []);
});

test("bucketForBulk: 无分组但有会话时全部归未分组", function () {
  const rows = [{ id: "s1", title: "1" }, { id: "s2", title: "2" }];
  const buckets = utils.bucketForBulk(rows, [], {});
  assert.equal(buckets.length, 1);
  assert.equal(buckets[0].key, utils.UNGROUPED_KEY);
  assert.equal(buckets[0].sessions.length, 2);
});
