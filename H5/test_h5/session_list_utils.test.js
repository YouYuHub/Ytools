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

test("pickSendTitle: 已有可信标题时沿用（不被本轮提问覆盖）", function () {
  assert.equal(utils.pickSendTitle("修复压缩中断问题", "新的一轮提问内容", "s-1"), "修复压缩中断问题");
  assert.equal(utils.pickSendTitle("  模型标题  ", "提问", "s-1"), "模型标题");
});

test("pickSendTitle: 无可信标题时退回提问前 40 字占位", function () {
  assert.equal(utils.pickSendTitle("", "第一条提问", "s-1"), "第一条提问");
  assert.equal(utils.pickSendTitle(null, "\u4e00".repeat(60), "s-1"), "\u4e00".repeat(40));
  assert.equal(utils.pickSendTitle(undefined, null, "s-1"), "s-1");
});

test("pickSendTitle: 标题等于会话 ID（后端兜底值）视为无标题", function () {
  assert.equal(utils.pickSendTitle("s-1", "真正的第一条提问", "s-1"), "真正的第一条提问");
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

test("shouldOpenSessionOnClick: 点击已就绪/加载中的当前会话跳过", function () {
  assert.equal(utils.shouldOpenSessionOnClick("s-1", "s-1", ""), false);
  assert.equal(utils.shouldOpenSessionOnClick("s-1", "", "s-1"), false);
  assert.equal(utils.shouldOpenSessionOnClick("s-1", "s-1", "s-1"), false);
});

test("shouldOpenSessionOnClick: 点击其他会话照常打开", function () {
  assert.equal(utils.shouldOpenSessionOnClick("s-2", "s-1", ""), true);
  assert.equal(utils.shouldOpenSessionOnClick("s-2", "", "s-1"), true);
  assert.equal(utils.shouldOpenSessionOnClick("s-1", "", ""), true);
});

test("shouldOpenSessionOnClick: 空值/空白安全（trim 后比较；异常空 id 维持旧行为）", function () {
  assert.equal(utils.shouldOpenSessionOnClick(" s-1 ", "s-1", ""), false);
  assert.equal(utils.shouldOpenSessionOnClick("s-1", " s-1 ", null), false);
  assert.equal(utils.shouldOpenSessionOnClick("", "s-1", ""), true);
  assert.equal(utils.shouldOpenSessionOnClick(null, "s-1", "s-1"), true);
  assert.equal(utils.shouldOpenSessionOnClick(undefined, "", ""), true);
});
test("resolveTimeRange: 全部时间/非法返回 null（不过滤）", function () {
  assert.equal(utils.resolveTimeRange({ preset: "all" }), null);
  assert.equal(utils.resolveTimeRange(null), null);
  assert.equal(utils.resolveTimeRange({ preset: "nope" }), null);
  assert.equal(utils.resolveTimeRange({ preset: "custom", start: "", end: "" }), null);
  assert.equal(utils.resolveTimeRange({ preset: "custom", start: "2026-09-30", end: "2026-09-01" }), null);
});

test("resolveTimeRange: 预设按本地天数回退（含当天整天）", function () {
  const now = new Date(2026, 10, 7, 15, 30, 0).getTime();
  const week = utils.resolveTimeRange({ preset: "week" }, now);
  assert.equal(new Date(week.fromMs).getDate(), 1);
  assert.equal(new Date(week.fromMs).getHours(), 0);
  assert.equal(new Date(week.toMs).getDate(), 7);
  assert.equal(new Date(week.toMs).getHours(), 23);
  const month = utils.resolveTimeRange({ preset: "month" }, now);
  assert.equal(new Date(month.fromMs).getMonth(), 9);
  assert.equal(new Date(month.fromMs).getDate(), 9);
});

test("resolveTimeRange: 自定义缺一边视为开放区间", function () {
  const r = utils.resolveTimeRange({ preset: "custom", start: "2026-09-01", end: "2026-09-30" });
  assert.equal(new Date(r.fromMs).getDate(), 1);
  assert.equal(new Date(r.toMs).getDate(), 30);
  assert.equal(new Date(r.toMs).getHours(), 23);
  const open = utils.resolveTimeRange({ preset: "custom", start: "", end: "2026-09-30" });
  assert.equal(open.fromMs, 0);
});

test("filterRowsByTime: 区间内外 + 无时间保留", function () {
  const rows = [
    { id: "a", updated: "2026-09-10 12:00:00" },
    { id: "b", updated: "2026-10-05 12:00:00" },
    { id: "c", updated: "", created: "" },
  ];
  const range = utils.resolveTimeRange({ preset: "custom", start: "2026-09-01", end: "2026-09-30" });
  const out = utils.filterRowsByTime(rows, range, {});
  assert.deepEqual(out.map(function (r) { return r.id; }), ["a", "c"]);
  assert.deepEqual(utils.filterRowsByTime(rows, null, {}).length, 3);
});

test("filterRowsByTime: 本地 recency 也参与判定", function () {
  const rows = [{ id: "x", updated: "", created: "" }];
  const range = utils.resolveTimeRange({ preset: "custom", start: "2026-09-01", end: "2026-09-30" });
  const inside = {};
  inside.x = new Date(2026, 8, 15).getTime();
  assert.equal(utils.filterRowsByTime(rows, range, inside).length, 1);
  const outside = {};
  outside.x = new Date(2026, 0, 1).getTime();
  assert.equal(utils.filterRowsByTime(rows, range, outside).length, 0);
});

test("describeTimeFilter: 按钮文案", function () {
  assert.equal(utils.describeTimeFilter({ preset: "all" }), "全部时间");
  assert.equal(utils.describeTimeFilter({ preset: "week" }), "近一周");
  assert.equal(utils.describeTimeFilter({ preset: "custom", start: "2026-09-01", end: "2026-09-30" }), "2026/09/01 - 2026/09/30");
  assert.equal(utils.describeTimeFilter({ preset: "custom", start: "2020-01-01", end: "2020-01-31" }), "2020/01/01 - 2020/01/31");
  assert.equal(utils.describeTimeFilter({ preset: "custom", start: "", end: "" }), "全部时间");
});
