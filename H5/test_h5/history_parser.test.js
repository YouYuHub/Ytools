"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const historyParser = require("../js/history_parser.js");

function fakeRound(events, usageTotal) {
  const line = {
    event: "chat_round",
    question: "问题",
    started_at: "2026-08-08 18:00:00",
    events: events,
    status: "done",
    ended_at: "2026-08-08 18:01:00",
  };
  if (usageTotal) line.usage_total = usageTotal;
  return JSON.stringify(line);
}

test("parseHistory: 首行 _meta 提取 usage", function () {
  const meta = JSON.stringify({
    _meta: {
      title: "标题",
      usage: { total_tokens: 2745, completion_tokens: 2516, prompt_tokens: 229 },
    },
  });
  const parsed = historyParser.parseHistory(meta + "\n");
  assert.deepEqual(parsed.metaUsage, { total_tokens: 2745, completion_tokens: 2516, prompt_tokens: 229 });
  assert.equal(parsed.metaUsageTotal, 2745);
  assert.equal(parsed.records.length, 0);
});

test("parseHistory: user/assistant 记录与 skip done 块", function () {
  const round = fakeRound([
    { timestamp: "2026-08-08 18:00:00", role: "user", content: "你好" },
    { timestamp: "2026-08-08 18:00:05", role: "assistant", content: "你好！" },
    { timestamp: "2026-08-08 18:00:06", role: "assistant", content: "回答完成", done: "[DONE]" },
  ]);
  const parsed = historyParser.parseHistory(round);
  assert.deepEqual(parsed.records.map(function (r) { return r.kind; }), ["user", "assistant"]);
  assert.equal(parsed.records[1].content, "你好！");
});

test("parseHistory: 跳过“停止任务”用户消息与 error 记录", function () {
  const round = fakeRound([
    { timestamp: "2026-08-08 18:00:00", role: "user", content: "停止任务" },
    { timestamp: "2026-08-08 18:00:01", role: "assistant", error: "boom" },
  ]);
  const parsed = historyParser.parseHistory(round);
  assert.deepEqual(parsed.records.map(function (r) { return r.kind; }), ["notice"]);
});

test("parseHistory: 思考/工具调用/工具结果/usage", function () {
  const round = fakeRound([
    { timestamp: "2026-08-08 18:00:00", role: "user", content: "查时间" },
    { timestamp: "2026-08-08 18:00:01", role: "assistant", reasoning_content: "想一想" },
    { timestamp: "2026-08-08 18:00:02", role: "assistant", tool_calls: [{ function: { name: "get_time", arguments: "{}" } }] },
    { timestamp: "2026-08-08 18:00:03", role: "tool", tool_name: "get_time", arguments: "{}", result: "14:20" },
    { timestamp: "2026-08-08 18:00:04", role: "assistant", content: "现在是 14:20。" },
  ], { prompt_tokens: 10, completion_tokens: 5, total_tokens: 15 });
  const parsed = historyParser.parseHistory(round);
  assert.deepEqual(parsed.records.map(function (r) { return r.kind; }),
    ["user", "think", "tool", "toolResult", "assistant", "usage"]);
  assert.equal(parsed.records[2].name, "get_time");
  assert.equal(parsed.records[3].name, "get_time");
  assert.equal(parsed.records[4].content, "现在是 14:20。");
  assert.equal(parsed.records[5].usage.total_tokens, 15);
});

test("parseHistory: 坏行/非 chat_round 行忽略", function () {
  const text = "not-json{{{\n" + JSON.stringify({ event: "other", data: 1 }) + "\n" + fakeRound([
    { timestamp: "2026-08-08 18:00:00", role: "user", content: "q" },
  ]);
  const parsed = historyParser.parseHistory(text);
  assert.equal(parsed.records.length, 1);
  assert.equal(parsed.records[0].content, "q");
});

test("parseHistory: context_compaction 事件行解析为 compaction 记录", function () {
  const start = {
    event: "context_compaction",
    scope: "round",
    phase: "start",
    role: "assistant",
    compress_context: "【上下文摘要】\n节选内容",
    timestamp: "2026-08-08 18:00:10",
  };
  const done = {
    event: "context_compaction",
    scope: "session",
    phase: "done",
    role: "assistant",
    summary_text: "【任务目标】完成压缩功能",
    summary_usage: {
      prompt_tokens: 10,
      completion_tokens: 5,
      total_tokens: 15,
      compressed_rounds: 2,
      fallback: false,
    },
    timestamp: "2026-08-08 18:00:11",
  };
  const parsed = historyParser.parseHistory(JSON.stringify(start) + "\n" + JSON.stringify(done));
  assert.deepEqual(parsed.records.map(function (r) { return r.kind; }), ["compaction", "compaction"]);
  assert.equal(parsed.records[0].scope, "round");
  assert.equal(parsed.records[0].phase, "start");
  assert.equal(parsed.records[0].content, "【上下文摘要】\n节选内容");
  assert.equal(parsed.records[1].scope, "session");
  assert.equal(parsed.records[1].phase, "done");
  assert.equal(parsed.records[1].summary_text, "【任务目标】完成压缩功能");
  assert.equal(parsed.records[1].usage.compressed_rounds, 2);
});

test("parseHistory: chat_round.events 中的单轮压缩按实际顺序展开且合并摘要 usage", function () {
  const round = fakeRound([
    { timestamp: "2026-08-08 18:00:00", role: "user", content: "查文件" },
    { timestamp: "2026-08-08 18:00:01", role: "assistant", tool_calls: [{ function: { name: "list", arguments: "{}" } }] },
    { timestamp: "2026-08-08 18:00:02", role: "tool", tool_name: "list", result: "大量结果" },
    {
      timestamp: "2026-08-08 18:00:03",
      event: "context_compaction",
      scope: "round",
      phase: "start",
      role: "assistant",
      compress_context: "待压缩轨迹",
    },
    {
      timestamp: "2026-08-08 18:00:04",
      event: "context_compaction",
      scope: "round",
      phase: "done",
      role: "assistant",
      summary_text: "【已完成工作】已完成文件扫描",
      before_tokens: 5000,
      after_tokens: 1200,
      compress_index: 1,
      compress_usage: { prompt_tokens: 10, completion_tokens: 5, total_tokens: 15 },
    },
    { timestamp: "2026-08-08 18:00:05", role: "assistant", content: "继续处理" },
  ], { prompt_tokens: 20, completion_tokens: 10, total_tokens: 30 });
  const parsed = historyParser.parseHistory(round);
  assert.deepEqual(parsed.records.map(function (r) { return r.kind; }),
    ["user", "tool", "toolResult", "compaction", "compaction", "assistant", "usage"]);
  assert.equal(parsed.records[3].phase, "start");
  assert.equal(parsed.records[4].summary_text, "【已完成工作】已完成文件扫描");
  assert.equal(parsed.records[4].before_tokens, 5000);
  assert.equal(parsed.records[4].usage.total_tokens, 15);
});

test("parseHistory: aborted 压缩事件把同 scope 的 start 置为中断态", function () {
  const roundStart = {
    event: "context_compaction",
    scope: "round",
    phase: "start",
    role: "assistant",
    compress_context: "待压缩轨迹",
    timestamp: "2026-08-08 18:00:10",
  };
  const sessionStart = {
    event: "context_compaction",
    scope: "session",
    phase: "start",
    role: "assistant",
    context_summary: "跨轮待压缩",
    timestamp: "2026-08-08 18:00:11",
  };
  const aborted = {
    event: "context_compaction",
    scope: "round",
    phase: "aborted",
    role: "assistant",
    reason: "task_interrupted",
    timestamp: "2026-08-08 18:00:12",
  };
  const parsed = historyParser.parseHistory(
    JSON.stringify(roundStart) + "\n" + JSON.stringify(sessionStart) + "\n" + JSON.stringify(aborted)
  );
  const compactions = parsed.records.filter(function (r) { return r.kind === "compaction"; });
  assert.equal(compactions.length, 2);
  assert.equal(compactions[0].scope, "round");
  assert.equal(compactions[0].interrupted, true);
  assert.equal(compactions[1].scope, "session");
  assert.notEqual(compactions[1].interrupted, true);
});

test("recordsBeforeActiveRound: 只保留当前轮提问之前的记录", function () {
  const records = [
    { kind: "user", content: "第一问" },
    { kind: "assistant", content: "第一答" },
    { kind: "user", content: "第二问（流式中）" },
  ];
  assert.deepEqual(historyParser.recordsBeforeActiveRound(records, "第二问（流式中）")
    .map(function (r) { return r.content; }), ["第一问", "第一答"]);
});

test("recordsBeforeActiveRound: 未匹配到提问时返回全部", function () {
  const records = [{ kind: "user", content: "x" }];
  assert.equal(historyParser.recordsBeforeActiveRound(records, "不存在"), records);
});