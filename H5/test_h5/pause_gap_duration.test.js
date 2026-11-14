"use strict";
// 暂停→长时间空闲→继续：用时不得把空闲计入（秒表语义）。
// 复现真实会话：r58 09:32:29→09:43:40 stopped，r59（内部 nudge、无用户气泡）
// 21:51:15→22:01:47 done。续跑轮无气泡 → 历史节点被并入上一轮折叠，
// 折叠跨度横跨 12h 空闲；刷新后内存态间隙丢失，必须靠轮次权威时间窗扣除。
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM } = require("jsdom");
const HistoryParser = require("../js/history_parser.js");

const SOURCE = fs.readFileSync(path.join(__dirname, "../js/app/round_fold.js"), "utf8");
const T = s => Date.parse("2026-11-14T" + s);

function setup() {
  const dom = new JSDOM('<div id="chatInner"></div>', { runScripts: "outside-only" });
  const { window } = dom;
  const chatInner = window.document.querySelector("#chatInner");
  window.App = {
    state: { sessionId: "s" },
    chatInner,
    chatScroll: { style: {}, scrollTop: 0 },
    pauseAutoScroll() {},
    historyScroll: { refresh() {} },
    updateCodeblockCopyButtons() {},
    el(tag, cls, text) {
      const n = window.document.createElement(tag);
      if (cls) n.className = cls;
      if (text != null) n.textContent = text;
      return n;
    },
  };
  window.eval(SOURCE);
  return { window, document: window.document, chatInner, App: window.App, close: () => window.close() };
}

// 历史扁平节点：带轮次号 + 阶段起止 + 轮次权威运行区间
function hist(x, cls, round, startStr, endStr, win) {
  const n = x.document.createElement("div");
  n.className = cls;
  n.dataset.round = String(round);
  n.dataset.stageTimestamp = "2026-11-14T" + startStr;
  n.dataset.stageEndTimestamp = "2026-11-14T" + (endStr || startStr);
  if (win) {
    n.dataset.roundWindowStart = String(T(win[0]));
    n.dataset.roundWindowEnd = String(T(win[1]));
  }
  x.chatInner.appendChild(n);
  return n;
}
function userBubble(x, round, ts) {
  const n = x.document.createElement("div");
  n.className = "msg msg-user";
  n.dataset.round = String(round);
  n.dataset.stageTimestamp = "2026-11-14T" + ts;
  x.chatInner.appendChild(n);
  return n;
}
function durationOf(x) {
  const head = x.chatInner.querySelector(".round-fold-summary .round-fold-duration");
  return head ? head.textContent : "";
}

// 暂停轮 + 续跑轮（无用户气泡）合并成一个折叠的真实布局
function pausedThenResumed(x, withWindow) {
  const w58 = withWindow ? ["09:32:29", "09:43:40"] : null;
  const w59 = withWindow ? ["21:51:15", "22:01:47"] : null;
  userBubble(x, 58, "09:32:29");
  hist(x, "msg-assistant-body", 58, "09:33:00", "09:35:00", w58);
  hist(x, "think-block", 58, "09:36:00", "09:40:00", w58);
  hist(x, "tool-block", 58, "09:41:00", "09:43:30", w58);
  // 续跑轮：无用户气泡，节点被 collectRounds 并入上一轮
  hist(x, "think-block", 59, "21:52:00", "21:55:00", w59);
  hist(x, "tool-block", 59, "21:56:00", "21:59:00", w59);
  hist(x, "msg-assistant-body", 59, "22:00:00", "22:01:40", w59);
}

test("暂停 12h 后续跑：折叠用时扣除空闲（约 21m，不是 12h）", async t => {
  const x = setup();
  t.after(x.close);
  pausedThenResumed(x, true);
  x.App.roundFold.sync(x.chatInner);
  const text = durationOf(x);
  assert.ok(text, "应有用时");
  assert.ok(!/h /.test(text), "不应出现小时级读数，实际：" + text);
  // 跨度按节点时间戳：09:32:29→22:01:40，扣除 09:43:40→21:51:15 的 12h7m35s
  // 空闲 = 21m36s（而非 12h29m）。
  assert.equal(text, "用时 21m 36s");
});

test("缺少轮次时间窗时退回旧口径（不误扣，保持向后兼容）", async t => {
  const x = setup();
  t.after(x.close);
  pausedThenResumed(x, false);
  x.App.roundFold.sync(x.chatInner);
  assert.match(durationOf(x), /h /, "旧数据无时间窗，跨度仍按节点时间戳");
});

test("连续运行（无暂停）不因时间窗而少算", async t => {
  const x = setup();
  t.after(x.close);
  userBubble(x, 1, "10:00:00");
  const w = ["10:00:00", "10:05:00"];
  hist(x, "msg-assistant-body", 1, "10:00:30", "10:02:00", w);
  hist(x, "tool-block", 1, "10:02:30", "10:04:00", w);
  hist(x, "msg-assistant-body", 1, "10:04:30", "10:05:00", w);
  x.App.roundFold.sync(x.chatInner);
  assert.equal(durationOf(x), "用时 5m");
});

test("多次暂停续跑：每段空闲都扣除", async t => {
  const x = setup();
  t.after(x.close);
  userBubble(x, 1, "10:00:00");
  hist(x, "msg-assistant-body", 1, "10:00:10", "10:01:00", ["10:00:00", "10:01:00"]);
  hist(x, "think-block", 2, "11:00:10", "11:02:00", ["11:00:00", "11:02:00"]);
  hist(x, "tool-block", 3, "12:00:10", "12:03:00", ["12:00:00", "12:03:00"]);
  hist(x, "msg-assistant-body", 3, "12:02:00", "12:03:00", ["12:00:00", "12:03:00"]);
  x.App.roundFold.sync(x.chatInner);
  // 1m + 2m + 3m = 6m
  assert.equal(durationOf(x), "用时 6m");
});

test("内存暂停间隙与历史时间窗描述同一段空闲时不重复扣除", async t => {
  const x = setup();
  t.after(x.close);
  const msg = x.document.createElement("div");
  msg.className = "msg";
  msg.dataset.round = "1";
  msg._foldPauseGaps = [{ pauseAt: T("10:01:00"), resumeAt: T("11:00:00") }];
  x.chatInner.appendChild(msg);
  function inner(cls, s, e, win) {
    const n = x.document.createElement("div");
    n.className = cls;
    n.dataset.stageTimestamp = "2026-11-14T" + s;
    n.dataset.stageEndTimestamp = "2026-11-14T" + e;
    if (win) {
      n.dataset.roundWindowStart = String(T(win[0]));
      n.dataset.roundWindowEnd = String(T(win[1]));
    }
    msg.appendChild(n);
    return n;
  }
  inner("msg-assistant-body", "10:00:10", "10:01:00", ["10:00:00", "10:01:00"]);
  inner("think-block", "11:00:10", "11:02:00", ["11:00:00", "11:02:00"]);
  inner("msg-assistant-body", "11:01:00", "11:02:00", ["11:00:00", "11:02:00"]);
  x.App.roundFold.sync(msg);
  // 跨度 10:00:10→11:02:00 = 61m50s，扣一次 59m 空闲 = 2m50s；
  // 若内存间隙与历史空档被重复扣除，读数会变成负值/空。
  assert.equal(msg.querySelector(".round-fold-duration").textContent, "用时 2m 50s");
});

test("直播计时行口径：containerGapMs 扣除容器内历史空档", async t => {
  const x = setup();
  t.after(x.close);
  const msg = x.document.createElement("div");
  msg.className = "msg";
  // 续跑新容器：起点=继续时刻（11:00），容器内还带着暂停段的历史区间
  msg._foldRoundStartedAt = T("11:00:00");
  x.chatInner.appendChild(msg);
  const n = x.document.createElement("div");
  n.className = "tool-block";
  n.dataset.roundWindowStart = String(T("10:00:00"));
  n.dataset.roundWindowEnd = String(T("10:01:00"));
  msg.appendChild(n);
  const now = T("11:05:00");
  const realNow = Date.now;
  Date.now = () => now;
  try {
    // 暂停段 10:00→10:01 运行；10:01→11:00 空闲；11:00 起继续。
    // 计时窗口取续跑段 [11:00, 11:05]，其中的空闲为 0（暂停已固定）。
    assert.equal(x.App.roundFold.containerGapMs(msg, T("11:00:00"), now), 0);
    // 若窗口从轮首算起（旧行为），则应扣掉整段 59m 空闲
    assert.equal(x.App.roundFold.containerGapMs(msg, T("10:00:00"), now), 59 * 60 * 1000);
  } finally {
    Date.now = realNow;
  }
});

test("历史解析：轮次时间窗随记录下发（续跑轮同样带）", () => {
  const lines = [
    JSON.stringify({ _meta: { title: "t" } }),
    JSON.stringify({
      event: "chat_round", question: "做事", started_at: "2026-11-14 09:32:29",
      ended_at: "2026-11-14 09:43:40", status: "stopped",
      events: [
        { role: "user", content: "做事", timestamp: "2026-11-14 09:32:29" },
        { role: "assistant", content: "部分结果", timestamp: "2026-11-14 09:43:00" },
      ],
    }),
    JSON.stringify({
      event: "chat_round", question: "", started_at: "2026-11-14 21:51:15",
      ended_at: "2026-11-14 22:01:47", status: "done",
      events: [
        { role: "user", content: "继续", _internal: true, timestamp: "2026-11-14 21:51:15" },
        { role: "assistant", content: "完成", timestamp: "2026-11-14 22:01:40" },
      ],
    }),
  ];
  const { records } = HistoryParser.parseHistory(lines.join("\n"));
  const r58 = records.filter(r => r.round === 1 && r.roundWindow);
  const r59 = records.filter(r => r.round === 2 && r.roundWindow);
  assert.ok(r58.length > 0 && r59.length > 0, "两轮记录都应带时间窗");
  assert.equal(r58[0].roundWindow.start, Date.parse("2026-11-14T09:32:29"));
  assert.equal(r58[0].roundWindow.end, Date.parse("2026-11-14T09:43:40"));
  assert.equal(r59[0].roundWindow.start, Date.parse("2026-11-14T21:51:15"));
  assert.equal(r59[0].roundWindow.end, Date.parse("2026-11-14T22:01:47"));
  // 内部 nudge 不产生用户气泡（这是折叠合并、跨度横跨空闲的根因）
  assert.equal(records.filter(r => r.kind === "user").length, 1);
});
