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

test("parseHistory: 用户消息携带引用快照透传给渲染层", function () {
  const quotes = [{ text: "被选中的原文", source: { role: "assistant", round: 1 } }];
  const round = fakeRound([
    { timestamp: "2026-08-08 18:00:00", role: "user", content: "问题", quotes: quotes },
    { timestamp: "2026-08-08 18:00:05", role: "assistant", content: "回答" },
  ]);
  const parsed = historyParser.parseHistory(round);
  assert.deepEqual(parsed.records[0].quotes, quotes);
  // 无 quotes 字段的旧记录：不产生该键（正常回放）
  const plainRound = fakeRound([
    { timestamp: "2026-08-08 18:00:00", role: "user", content: "问题" },
  ]);
  const plain = historyParser.parseHistory(plainRound);
  assert.equal(plain.records[0].quotes, undefined);
  // 空数组同样不携带（不显示卡片）
  const emptyRound = fakeRound([
    { timestamp: "2026-08-08 18:00:00", role: "user", content: "问题", quotes: [] },
  ]);
  assert.equal(historyParser.parseHistory(emptyRound).records[0].quotes, undefined);
});

test("recordsBeforeActiveRound: 无轮次号时文本 + 引用快照参与匹配", function () {
  const records = [
    { kind: "user", content: "同一个问题", round: 1, quotes: [{ text: "旧引用" }] },
    { kind: "assistant", content: "旧回答", round: 1 },
    { kind: "user", content: "同一个问题", round: 2, quotes: [{ text: "新引用" }] },
    { kind: "assistant", content: "新回答", round: 2 },
  ];
  // 引用不同：不能把旧同文本提问误判为本轮
  const sliced = historyParser.recordsBeforeActiveRound(
    records, "同一个问题", null, [{ text: "新引用" }]
  );
  assert.equal(sliced.length, 2);
  assert.equal(sliced[1].content, "旧回答");
  // 引用一致时命中第二条
  const matched = historyParser.recordsBeforeActiveRound(
    records, "同一个问题", null, [{ text: "旧引用" }]
  );
  assert.equal(matched.length, 0);
  // 无引用（旧调用方）：保持旧行为（命中最后一条同文本）
  const legacy = historyParser.recordsBeforeActiveRound(records, "同一个问题", null);
  assert.equal(legacy.length, 2);
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

test("parseHistory: 手动压缩独立 JSONL 行保留在旧历史与后续轮次之间", function () {
  const firstRound = JSON.stringify({
    event: "chat_round",
    started_at: "2026-08-08 18:00:00",
    ended_at: "2026-08-08 18:00:10",
    events: [
      { timestamp: "2026-08-08 18:00:00", role: "user", content: "旧问题" },
      { timestamp: "2026-08-08 18:00:05", role: "assistant", content: "旧回答" },
    ],
  });
  const start = {
    event: "context_compaction", scope: "session", phase: "start",
    timestamp: "2026-08-08 18:00:11", context_summary: "待压缩旧历史",
  };
  const done = {
    event: "context_compaction", scope: "session", phase: "done",
    timestamp: "2026-08-08 18:00:12", summary_text: "旧历史摘要",
  };
  const nextRound = JSON.stringify({
    event: "chat_round",
    started_at: "2026-08-08 18:00:20",
    ended_at: "2026-08-08 18:00:30",
    events: [
      { timestamp: "2026-08-08 18:00:20", role: "user", content: "后续问题" },
      { timestamp: "2026-08-08 18:00:25", role: "assistant", content: "后续回答" },
    ],
  });

  const parsed = historyParser.parseHistory([
    firstRound, JSON.stringify(start), JSON.stringify(done), nextRound,
  ].join("\n"));
  assert.deepEqual(parsed.records.map(function (r) {
    return r.kind === "compaction" ? r.phase : r.content;
  }), ["旧问题", "旧回答", "start", "done", "后续问题", "后续回答"]);
  assert.equal(parsed.records[2].ts, start.timestamp);
  assert.equal(parsed.records[3].summary_text, "旧历史摘要");
});

test("parseHistory: 带轮内锚点的跨轮压缩插回准确事件位置", function () {
  const start = {
    event: "context_compaction", scope: "session", phase: "start",
    display_round: 1, display_event_index: 3,
    timestamp: "2026-08-08 18:00:03", context_summary: "压缩前轨迹",
  };
  const done = {
    event: "context_compaction", scope: "session", phase: "done",
    display_round: 1, display_event_index: 3,
    timestamp: "2026-08-08 18:00:04", summary_text: "压缩后摘要",
  };
  const round = fakeRound([
    { timestamp: "2026-08-08 18:00:00", role: "user", content: "问题" },
    { timestamp: "2026-08-08 18:00:01", role: "assistant", tool_calls: [{ function: { name: "read_file", arguments: "{}" } }] },
    { timestamp: "2026-08-08 18:00:02", role: "tool", tool_name: "read_file", result: "文件内容" },
    { timestamp: "2026-08-08 18:00:05", role: "assistant", content: "继续回答" },
  ]);

  const parsed = historyParser.parseHistory([
    JSON.stringify(start), JSON.stringify(done), round,
  ].join("\n"));
  assert.deepEqual(parsed.records.map(function (r) {
    return r.kind === "compaction" ? r.phase : r.kind;
  }), ["user", "tool", "toolResult", "start", "done", "assistant"]);
  assert.equal(parsed.records[3].round, 1);
  assert.equal(parsed.records[3].ts, start.timestamp);
  assert.equal(parsed.records[4].summary_text, "压缩后摘要");
});

test("parseHistory: 锚点行位于轮次行之后（旧版后端反序文件）仍归位到轮内位置", function () {
  // 旧版后端"回答插入/编辑重发"收尾会让锚定的跨轮压缩行滞留在轮次行之后；
  // 解析器不依赖物理顺序，按 display_round 锚点归位（压缩块不再兜底到会话末尾）
  const start = {
    event: "context_compaction", scope: "session", phase: "start",
    display_round: 1, display_event_index: 3,
    timestamp: "2026-08-08 18:00:03", context_summary: "压缩前轨迹",
  };
  const done = {
    event: "context_compaction", scope: "session", phase: "done",
    display_round: 1, display_event_index: 3,
    timestamp: "2026-08-08 18:00:04", summary_text: "压缩后摘要",
  };
  const round = fakeRound([
    { timestamp: "2026-08-08 18:00:00", role: "user", content: "问题" },
    { timestamp: "2026-08-08 18:00:01", role: "assistant", tool_calls: [{ function: { name: "read_file", arguments: "{}" } }] },
    { timestamp: "2026-08-08 18:00:02", role: "tool", tool_name: "read_file", result: "文件内容" },
    { timestamp: "2026-08-08 18:00:05", role: "assistant", content: "继续回答" },
  ]);

  // 反序：轮次行在前，压缩行在其后（模拟旧版后端收尾产物）
  const parsed = historyParser.parseHistory(
    [round, JSON.stringify(start), JSON.stringify(done)].join("\n")
  );
  assert.deepEqual(parsed.records.map(function (r) {
    return r.kind === "compaction" ? r.phase : r.kind;
  }), ["user", "tool", "toolResult", "start", "done", "assistant"]);
  assert.equal(parsed.records[3].round, 1);
  assert.equal(parsed.records[4].summary_text, "压缩后摘要");
});

test("parseHistory: 无锚点的迟到压缩行按轮次时间窗回插而非追加到会话末尾", function () {
  const round = fakeRound([
    { timestamp: "2026-08-08 18:00:00", role: "user", content: "任务问题" },
    { timestamp: "2026-08-08 18:00:10", role: "tool", tool_name: "read_file", result: "内容" },
    { timestamp: "2026-08-08 18:00:20", role: "assistant", content: "继续处理" },
  ]);
  // 模拟旧 JSONL：轮次先整体落盘，session 压缩 start/done 稍后才单独追加；
  // 事件本身没有 display_round/display_event_index，但时间仍在该轮中。
  const start = {
    event: "context_compaction", scope: "session", phase: "start",
    timestamp: "2026-08-08 18:00:15", context_summary: "压缩前轨迹",
  };
  const done = {
    event: "context_compaction", scope: "session", phase: "done",
    timestamp: "2026-08-08 18:00:16", summary_text: "压缩摘要",
  };
  const parsed = historyParser.parseHistory(
    [round, JSON.stringify(start), JSON.stringify(done)].join("\n")
  );
  assert.deepEqual(parsed.records.map(function (r) {
    return r.kind === "compaction" ? r.phase : r.content || r.kind;
  }), ["任务问题", "toolResult", "start", "done", "继续处理"]);
  assert.equal(parsed.records[2].round, 1);
  assert.equal(parsed.records[3].round, 1);
});

test("parseHistory: 锚定轮次尚未收尾的锚点行保留在可见历史末尾", function () {
  // 任务进行中刷新：压缩行已落盘（带锚点），其锚定轮次行还没写（收尾才落盘）
  const round = fakeRound([
    { timestamp: "2026-08-08 18:00:00", role: "user", content: "已完成问题" },
  ]);
  const start = {
    event: "context_compaction", scope: "session", phase: "start",
    display_round: 2, display_event_index: 0,
    timestamp: "2026-08-08 18:00:05", context_summary: "压缩前轨迹",
  };
  const done = {
    event: "context_compaction", scope: "session", phase: "done",
    display_round: 2, display_event_index: 0,
    timestamp: "2026-08-08 18:00:06", summary_text: "摘要",
  };
  const parsed = historyParser.parseHistory(
    [round, JSON.stringify(start), JSON.stringify(done)].join("\n")
  );
  assert.deepEqual(parsed.records.map(function (r) {
    return r.kind === "compaction" ? r.phase : r.kind;
  }), ["user", "start", "done"]);
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
      token_limit: 4000,
      trigger_reason: "task",
      trigger_context_tokens: 123456,
      trigger_threshold: 9000,
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
  assert.equal(parsed.records[4].token_limit, 4000);
  assert.equal(parsed.records[4].trigger_reason, "task");
  assert.equal(parsed.records[4].trigger_context_tokens, 123456);
  assert.equal(parsed.records[4].trigger_threshold, 9000);
  assert.equal(parsed.records[4].usage.total_tokens, 15);
});

test("parseHistory: 压缩 done 的批次诊断字段（P2-1）解析到记录", function () {
  // 后端 done 事件新增：本批源规模 / 批源预算 / 尾部保留轮次 / 单段输出上限 /
  // 源截断标记——用于核对"每批吃了多少、是否被截断"；历史回放同样可见。
  // 注：chat_round.events 内嵌的压缩事件 scope 为 round（session 级为独立 JSONL 行）。
  const round = fakeRound([
    {
      timestamp: "2026-08-08 18:00:01",
      event: "context_compaction",
      scope: "round",
      phase: "done",
      role: "assistant",
      summary_text: "【已完成工作】已完成文件扫描",
      before_tokens: 360000,
      after_tokens: 189000,
      token_limit: 200000,
      target_tokens: 80000,
      budget_scope: "full_request",
      trigger_reason: "task",
      batch_source_tokens: 180000,
      source_budget: 450000,
      tail_rounds: 5,
      output_token_limit: 15020,
      source_was_truncated: true,
      source_was_chunked: true,
      source_chunk_count: 4,
      warnings: ["batch_source_truncated"],
    },
    { timestamp: "2026-08-08 18:00:02", role: "assistant", content: "继续处理" },
  ]);
  const rec = historyParser.parseHistory(round).records
    .filter(function (r) { return r.kind === "compaction"; })[0];
  assert.equal(rec.batch_source_tokens, 180000);
  assert.equal(rec.source_budget, 450000);
  assert.equal(rec.tail_rounds, 5);
  assert.equal(rec.output_token_limit, 15020);
  assert.equal(rec.source_was_truncated, true);
  assert.equal(rec.source_was_chunked, true);
  assert.equal(rec.source_chunk_count, 4);
  assert.equal(rec.budget_scope, "full_request");
  assert.equal(rec.target_tokens, 80000);
  assert.deepEqual(rec.warnings, ["batch_source_truncated"]);
});

test("parseHistory: 旧数据无批次诊断字段时保持空值（向后兼容）", function () {
  const round = fakeRound([
    {
      timestamp: "2026-08-08 18:00:01",
      event: "context_compaction",
      scope: "round",
      phase: "done",
      role: "assistant",
      summary_text: "旧版摘要",
      before_tokens: 100,
      after_tokens: 50,
    },
  ]);
  const rec = historyParser.parseHistory(round).records
    .filter(function (r) { return r.kind === "compaction"; })[0];
  // 旧数据无这些字段：解析为 null（未提供），前端据此不渲染该行
  assert.equal(rec.batch_source_tokens, null);
  assert.equal(rec.tail_rounds, null);
  assert.equal(rec.source_was_truncated, false);
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

test("recordsBeforeActiveRound: 按轮次号精确匹配优先于文本", function () {
  const records = [
    { kind: "user", content: "第一问", round: 1 },
    { kind: "assistant", content: "第一答", round: 1 },
    // 历史中存在与当前轮同文本的旧提问：不能按文本误删
    { kind: "user", content: "重复的问题", round: 2 },
    { kind: "assistant", content: "旧回答", round: 2 },
    { kind: "user", content: "重复的问题", round: 3 },
  ];
  const kept = historyParser.recordsBeforeActiveRound(records, "重复的问题", 3)
    .map(function (r) { return r.round; });
  assert.deepEqual(kept, [1, 1, 2, 2]);
});

test("recordsBeforeActiveRound: 多模态部件数组提问按 text 部件拼接匹配", function () {
  const records = [
    { kind: "user", content: "第一问", round: 1 },
    { kind: "assistant", content: "第一答", round: 1 },
    {
      kind: "user",
      round: 2,
      content: [
        { type: "image_url", image_url: { url: "media://a.png" } },
        { type: "text", text: "看图说话（流式中）" },
      ],
    },
  ];
  const kept = historyParser.recordsBeforeActiveRound(
    records, "看图说话（流式中）"
  ).map(function (r) { return r.round; });
  assert.deepEqual(kept, [1, 1]);
});

test("recordsBeforeActiveRound: 无 userText 时安全返回原记录", function () {
  const records = [{ kind: "user", content: "第一问", round: 1 }];
  assert.equal(historyParser.recordsBeforeActiveRound(records, ""), records);
});

test("recordsBeforeActiveRound: 轮次号有效但未命中时不回退文本匹配", function () {
  // 生成中刷新：本轮（第 3 轮）尚未落盘，历史里存在同文本旧提问（第 1 轮）
  const records = [
    { kind: "user", content: "重复的问题", round: 1 },
    { kind: "assistant", content: "旧回答", round: 1 },
    { kind: "user", content: "第二问", round: 2 },
    { kind: "assistant", content: "第二答", round: 2 },
  ];
  // 若回退文本匹配会把第 1 轮及其后整段误裁，这里必须原样返回
  assert.equal(historyParser.recordsBeforeActiveRound(records, "重复的问题", 3), records);
});

// ---------- sub_agent 子任务事件块聚合 ----------

function subAgentEvents() {
  return [
    { timestamp: "2026-08-08 18:00:01", role: "user", content: "并发查两件事" },
    {
      timestamp: "2026-08-08 18:00:02", event: "sub_agent",
      agent_id: "agent_0001aabb", parent_agent_id: "main",
      parent_tool_call_id: "call_aaa", agent_index: 0, phase: "start",
      task: "调研 A 方案\n详细目标：输出结论", todo: [{ content: "读文档", status: "in_progress" }],
      tools: ["search_files"], rounds_limit: 40,
    },
    {
      timestamp: "2026-08-08 18:00:03", event: "sub_agent",
      agent_id: "agent_0001aabb", parent_tool_call_id: "call_aaa", phase: "delta",
      reasoning_delta: "想一想", content_delta: "写一半",
    },
    {
      timestamp: "2026-08-08 18:00:04", event: "sub_agent",
      agent_id: "agent_0001aabb", parent_tool_call_id: "call_aaa", phase: "model_call",
      seq: 1, reasoning_content: "想一想", content: "写一半",
      tool_calls: [{ function: { name: "search_files", arguments: "{\"pattern\":\"x\"}" } }],
    },
    {
      timestamp: "2026-08-08 18:00:05", event: "sub_agent",
      agent_id: "agent_0001aabb", parent_tool_call_id: "call_aaa", phase: "tool_start",
      seq: 1, tool_call_id: "call_child_1", tool_name: "search_files", arguments: "{\"pattern\":\"x\"}",
    },
    {
      timestamp: "2026-08-08 18:00:06", event: "sub_agent",
      agent_id: "agent_0001aabb", parent_tool_call_id: "call_aaa", phase: "tool_result",
      seq: 1, tool_call_id: "call_child_1", tool_name: "search_files",
      arguments: "{\"pattern\":\"x\"}", result: "命中 2 处",
    },
    {
      timestamp: "2026-08-08 18:00:07", event: "sub_agent",
      agent_id: "agent_0001aabb", parent_tool_call_id: "call_aaa", phase: "todo",
      todos: [{ content: "读代码", status: "done" }, { content: "汇总", status: "in_progress" }],
    },
    {
      timestamp: "2026-08-08 18:00:08", event: "sub_agent",
      agent_id: "agent_0001aabb", parent_tool_call_id: "call_aaa", phase: "model_call",
      seq: 2, reasoning_content: null, content: "结论：选 A。",
    },
    {
      timestamp: "2026-08-08 18:00:09", event: "sub_agent",
      agent_id: "agent_0001aabb", parent_tool_call_id: "call_aaa", phase: "done",
      status: "done", final_reply: "结论：选 A。", rounds: 2,
      usage_total: { prompt_tokens: 100, completion_tokens: 50, total_tokens: 150 },
      error: null, ended_at: "2026-08-08 18:01:00",
    },
    {
      timestamp: "2026-08-08 18:00:10", event: "sub_agent",
      agent_id: "agent_0002ccdd", parent_agent_id: "main",
      parent_tool_call_id: "call_bbb", agent_index: 1, phase: "start",
      task: "调研 B 方案", tools: [], rounds_limit: 40,
    },
    // 第二个子任务中断遗留：无 done 事件
    {
      timestamp: "2026-08-08 18:00:11", role: "assistant", tool_calls: [
        { function: { name: "sub_agent", arguments: "{\"task\":\"调研 A 方案\"}" } },
        { function: { name: "sub_agent", arguments: "{\"task\":\"调研 B 方案\"}" } },
      ],
    },
    { timestamp: "2026-08-08 18:00:12", role: "tool", tool_name: "sub_agent", tool_call_id: "call_aaa", result: "结论：选 A。" },
    { timestamp: "2026-08-08 18:00:12", role: "tool", tool_name: "sub_agent", tool_call_id: "call_bbb", result: "已中断" },
    { timestamp: "2026-08-08 18:00:13", role: "assistant", content: "汇总完成" },
  ];
}

test("parseHistory: sub_agent 事件聚合为 agentBlock 块（任务/轨迹/done）", function () {
  const round = fakeRound(subAgentEvents(), { prompt_tokens: 10, completion_tokens: 5, total_tokens: 15 });
  const parsed = historyParser.parseHistory(round);
  const blocks = parsed.records.filter(function (r) { return r.kind === "agentBlock"; });
  assert.equal(blocks.length, 2);
  const first = blocks[0];
  assert.equal(first.agent_id, "agent_0001aabb");
  assert.equal(first.parent_tool_call_id, "call_aaa");
  assert.equal(first.task_summary, "调研 A 方案");
  assert.equal(first.status, "done");
  assert.equal(first.final_reply, "结论：选 A。");
  assert.equal(first.rounds, 2);
  assert.equal(first.usage_total.total_tokens, 150);
  assert.equal(first.done_emitted, true);
  // delta 事件不进 entries（实时推流专用）；todo 计划更新也归位块内
  assert.equal(first.entries.length, 5);
  assert.deepEqual(first.entries.map(function (e) { return e.phase; }),
    ["model_call", "tool_start", "tool_result", "todo", "model_call"]);
  assert.equal(first.entries[2].result, "命中 2 处");
  // 第二块：无 done 事件 → interrupted 态
  const second = blocks[1];
  assert.equal(second.agent_id, "agent_0002ccdd");
  assert.equal(second.status, "interrupted");
  assert.equal(second.done_emitted, false);
});

test("parseHistory: 父级 sub_agent 工具调用/结果不重复渲染", function () {
  const round = fakeRound(subAgentEvents());
  const parsed = historyParser.parseHistory(round);
  const kinds = parsed.records.map(function (r) { return r.kind; });
  // 无普通 tool 记录（父级 tool 角色条目被跳过），也无 sub_agent 工具调用记录
  assert.ok(kinds.indexOf("tool") === -1, "kinds=" + JSON.stringify(kinds));
  assert.equal(kinds.filter(function (k) { return k === "toolResult"; }).length, 0);
  assert.deepEqual(kinds, ["user", "agentBlock", "agentBlock", "assistant"]);
  assert.equal(parsed.records[3].content, "汇总完成");
});

test("parseHistory: agentBlock 保持 events 中的渲染位置", function () {
  const round = fakeRound([
    { timestamp: "2026-08-08 18:00:01", role: "assistant", content: "先说明计划" },
    { timestamp: "2026-08-08 18:00:02", event: "sub_agent", agent_id: "agent_x", parent_tool_call_id: "call_x", phase: "start", task: "子任务" },
    { timestamp: "2026-08-08 18:00:03", event: "sub_agent", agent_id: "agent_x", parent_tool_call_id: "call_x", phase: "done", status: "done", final_reply: "ok", rounds: 1 },
  ]);
  const parsed = historyParser.parseHistory(round);
  assert.deepEqual(parsed.records.map(function (r) { return r.kind; }), ["assistant", "agentBlock"]);
  assert.equal(parsed.records[1].started_at, "2026-08-08 18:00:02");
});

test("parseHistory: 孤儿 sub_agent 事件（缺 start）被忽略", function () {
  const round = fakeRound([
    { timestamp: "2026-08-08 18:00:01", event: "sub_agent", agent_id: "agent_y", parent_tool_call_id: "call_y", phase: "model_call", seq: 1, content: "孤儿" },
    { timestamp: "2026-08-08 18:00:02", event: "sub_agent", agent_id: "agent_y", parent_tool_call_id: "call_y", phase: "done", status: "done", final_reply: "x", rounds: 1 },
  ]);
  const parsed = historyParser.parseHistory(round);
  assert.deepEqual(parsed.records, []);
});
