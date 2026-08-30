"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const sessionUtils = require("../js/session_utils.js");

test("sanitizeSessionId: 去掉文件名后缀", function () {
  assert.equal(sessionUtils.sanitizeSessionId("web-abc123_chat.jsonl"), "web-abc123");
  assert.equal(sessionUtils.sanitizeSessionId("web-abc123.jsonl"), "web-abc123");
});

test("sanitizeSessionId: 保留中文/Unicode/空格/点横线", function () {
  assert.equal(sessionUtils.sanitizeSessionId("会话 1.2-3"), "会话 1.2-3");
});

test("sanitizeSessionId: 危险字符替换并拦截路径穿越", function () {
  assert.equal(sessionUtils.sanitizeSessionId("../../../etc/passwd"), "etc_passwd");
  assert.equal(sessionUtils.sanitizeSessionId("a..b"), "a_b");
});

test("sanitizeSessionId: 空值回退 default", function () {
  assert.equal(sessionUtils.sanitizeSessionId(""), "default");
  assert.equal(sessionUtils.sanitizeSessionId(null), "default");
  assert.equal(sessionUtils.sanitizeSessionId(undefined), "default");
});

test("fileNameToSessionId: 与 sanitize 一致", function () {
  assert.equal(sessionUtils.fileNameToSessionId("web-xyz_chat.jsonl"), "web-xyz");
});

test("generateSessionId: yt-前缀 + 系统时间戳 + 三位随机数", function () {
  const id = sessionUtils.generateSessionId(new Date(2000, 0, 1, 0, 0, 0));
  assert.match(id, /^yt-\d{4}-\d{2}-\d{2}_\d{2}\.\d{2}\.\d{2}_\d{3}$/);
  // 传入固定时间时时间戳部分应与真实系统时间一致
  assert.equal(id, "yt-2000-01-01_00.00.00_" + id.slice(-3));
});

test("generateSessionId: 随机数在合法范围内且两次生成可不同", function () {
  for (let i = 0; i < 50; i++) {
    const rand = Number(sessionUtils.generateSessionId().slice(-3));
    assert.ok(rand >= 0 && rand <= 999, "随机数应在 000-999 内: " + rand);
  }
  const seen = new Set();
  for (let i = 0; i < 20; i++) seen.add(sessionUtils.generateSessionId());
  assert.ok(seen.size > 1, "随机数应使同一秒内生成的 ID 大概率不同");
});

test("getSessionTitle: 返回兜底标题", function () {
  assert.equal(sessionUtils.getSessionTitle("s1", "标题"), "标题");
});