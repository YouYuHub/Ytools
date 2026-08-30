"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const formatUtils = require("../js/format_utils.js");

test("fmtNum: 千分位", function () {
  assert.equal(formatUtils.fmtNum(1234567), "1,234,567");
  assert.equal(formatUtils.fmtNum(null), "0");
  assert.equal(formatUtils.fmtNum("42"), "42");
});

test("fmtTime: 去掉日期部分", function () {
  assert.equal(formatUtils.fmtTime("2026-08-02 20:34:11"), "08-02 20:34:11");
  assert.equal(formatUtils.fmtTime(""), "");
  assert.equal(formatUtils.fmtTime(undefined), "");
  assert.equal(formatUtils.fmtTime(null), "");
});

test("prettyJson: 字符串 JSON 格式化", function () {
  assert.equal(formatUtils.prettyJson('{"a":1}'), '{\n  "a": 1\n}');
  assert.equal(formatUtils.prettyJson("not json"), "not json");
  assert.equal(formatUtils.prettyJson({ b: 2 }), '{\n  "b": 2\n}');
});

test("normalizeUsage: 归一化缺省字段", function () {
  assert.deepEqual(formatUtils.normalizeUsage({ total_tokens: 100 }), {
    prompt_tokens: 0,
    completion_tokens: 0,
    total_tokens: 100,
  });
  assert.deepEqual(formatUtils.normalizeUsage({
    prompt_tokens: 10,
    completion_tokens: 5,
  }), { prompt_tokens: 10, completion_tokens: 5, total_tokens: 15 });
  assert.deepEqual(formatUtils.normalizeUsage(undefined), {
    prompt_tokens: 0,
    completion_tokens: 0,
    total_tokens: 0,
  });
});

test("usageText: 生成本轮 token 文案", function () {
  assert.equal(formatUtils.usageText({ prompt_tokens: 1000, completion_tokens: 500, total_tokens: 1500 }),
    "本轮消耗 1,500 tokens（输入 1,000 · 输出 500）");
  assert.equal(formatUtils.usageText({}), "本轮消耗 0 tokens（输入 0 · 输出 0）");
});

test("compactionUsageText: 压缩 usage 文案", function () {
  assert.equal(
    formatUtils.compactionUsageText({ prompt_tokens: 10, completion_tokens: 5, total_tokens: 15, compressed_rounds: 2 }),
    "压缩消耗 15 tokens（输入 10 · 输出 5） · 已压缩 2 个旧轮次");
  assert.equal(
    formatUtils.compactionUsageText({ prompt_tokens: 10, completion_tokens: 5, total_tokens: 15, before_tokens: 500, after_tokens: 300 }),
    "压缩消耗 15 tokens（输入 10 · 输出 5） · 上下文 500 → 300 tokens");
  assert.equal(formatUtils.compactionUsageText(null), "");
});