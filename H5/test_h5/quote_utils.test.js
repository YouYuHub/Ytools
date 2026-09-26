"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const QuoteUtils = require("../js/quote_utils.js");

test("normalizeQuoteText: 统一换行并去掉首尾空白", function () {
  assert.equal(QuoteUtils.normalizeQuoteText("  a\r\nb\r  "), "a\nb");
  assert.equal(QuoteUtils.normalizeQuoteText(null), "");
  assert.equal(QuoteUtils.normalizeQuoteText(123), "");
  assert.equal(QuoteUtils.normalizeQuoteText("  \n\t "), "");
});

test("normalizeQuotes: 丢弃 id 之外保留来源，跳过非法项", function () {
  const normalized = QuoteUtils.normalizeQuotes([
    { id: "q1", text: " 第一段 ", source: { role: "assistant", round: 3 } },
    "bad",
    { text: "" },
    { text: "第二段", source: { role: "system" } },
  ]);
  assert.equal(normalized.length, 2);
  assert.equal(normalized[0].text, "第一段");
  assert.deepEqual(normalized[0].source, { role: "assistant", round: 3 });
  assert.equal(normalized[1].text, "第二段");
  assert.equal(normalized[1].source, undefined);
});

test("normalizeQuotes: 超量截断、超长裁剪、合计超限丢弃", function () {
  const many = [];
  for (let i = 0; i < 8; i += 1) many.push({ text: "第" + i + "段" });
  assert.equal(QuoteUtils.normalizeQuotes(many).length, QuoteUtils.QUOTE_LIMITS.maxQuotes);

  const longText = "字".repeat(QuoteUtils.QUOTE_LIMITS.maxQuoteChars + 50);
  const clipped = QuoteUtils.normalizeQuotes([{ text: longText }]);
  assert.equal(clipped[0].text.length, QuoteUtils.QUOTE_LIMITS.maxQuoteChars);

  const total = QuoteUtils.normalizeQuotes([
    { text: "x".repeat(4000) },
    { text: "y".repeat(4000) },
    { text: "z".repeat(4000) },
    { text: "w".repeat(4000) },
  ]);
  const sum = total.reduce(function (acc, q) { return acc + q.text.length; }, 0);
  assert.ok(sum <= QuoteUtils.QUOTE_LIMITS.maxTotalChars);
});

test("checkQuoteAppend: 各类超限给出可读原因", function () {
  assert.equal(QuoteUtils.checkQuoteAppend([], "文本").ok, true);
  assert.equal(QuoteUtils.checkQuoteAppend([], "   ").ok, false);

  const full = [];
  for (let i = 0; i < QuoteUtils.QUOTE_LIMITS.maxQuotes; i += 1) full.push({ text: "x" });
  const over = QuoteUtils.checkQuoteAppend(full, "y");
  assert.equal(over.ok, false);
  assert.match(over.reason, /最多引用/);

  const long = QuoteUtils.checkQuoteAppend([], "字".repeat(4001));
  assert.equal(long.ok, false);
  assert.match(long.reason, /单段引用最多/);

  const total = QuoteUtils.checkQuoteAppend([{ text: "x".repeat(9000) }], "y".repeat(4000));
  assert.equal(total.ok, false);
  assert.match(total.reason, /合计/);
});

test("quotePreview: 折叠空白并截断", function () {
  assert.equal(QuoteUtils.quotePreview("  a  \n  b  ", 80), "a b");
  const long = QuoteUtils.quotePreview("字".repeat(100), 10);
  assert.equal(long, "字".repeat(10) + "…");
});

test("quoteSourceLabel: 来源提示文案", function () {
  assert.equal(QuoteUtils.quoteSourceLabel({ role: "assistant", round: 12 }), "来自助手 · 第 12 轮");
  assert.equal(QuoteUtils.quoteSourceLabel({ role: "user" }), "来自用户");
  assert.equal(QuoteUtils.quoteSourceLabel({ round: 3 }), "第 3 轮");
  assert.equal(QuoteUtils.quoteSourceLabel(null), "");
});

test("composeCopyText: 人可读复制文本，无协议标签", function () {
  const text = QuoteUtils.composeCopyText(
    [{ text: "第一段" }, { text: "第二段" }],
    "问题正文"
  );
  assert.equal(text, "引用 1：第一段\n\n引用 2：第二段\n\n问题：问题正文");
  assert.equal(QuoteUtils.composeCopyText([], "只有问题"), "只有问题");
  assert.equal(QuoteUtils.composeCopyText(null, ""), "");
});

test("quotesForRequest: 剥离本地 UI 字段", function () {
  const payload = QuoteUtils.quotesForRequest([
    { id: "q1", text: "引用", source: { role: "assistant", round: 2 } },
  ]);
  assert.deepEqual(payload, [{ text: "引用", source: { role: "assistant", round: 2 } }]);
  assert.deepEqual(QuoteUtils.quotesForRequest([]), []);
});
