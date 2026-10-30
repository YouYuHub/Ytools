"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM } = require("jsdom");

const SOURCE = fs.readFileSync(path.join(__dirname, "../js/app/round_fold.js"), "utf8");

function setup() {
  const dom = new JSDOM("<div id=\"chatInner\"></div>", { runScripts: "outside-only" });
  const { window } = dom;
  const { document } = window;
  const calls = { paused: 0, refreshed: 0, codebuttons: 0 };
  const chatInner = document.querySelector("#chatInner");
  window.App = {
    state: { sessionId: "s" },
    chatInner,
    chatScroll: { style: {}, scrollTop: 0 },
    pauseAutoScroll() { calls.paused++; },
    historyScroll: { refresh() { calls.refreshed++; } },
    updateCodeblockCopyButtons() { calls.codebuttons++; },
    el(tag, cls, text) {
      const n = document.createElement(tag);
      if (cls) n.className = cls;
      if (text != null) n.textContent = text;
      return n;
    },
  };
  window.eval(SOURCE);
  return { window, document, chatInner, calls, App: window.App, close: () => window.close() };
}

function div(doc, cls, text) {
  const n = doc.createElement("div");
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}

// 直播轮容器：time/body1/think/tool/time/body2/usage
function liveMsg(x, round) {
  const msg = div(x.document, "msg");
  if (round != null) msg.dataset.round = String(round);
  msg.append(
    div(x.document, "msg-time stage-time", "10:00"),
    div(x.document, "msg-assistant-body", "第一段正文"),
    div(x.document, "think-block", "思考"),
    div(x.document, "tool-block", "工具"),
    div(x.document, "msg-time stage-time", "10:01"),
    div(x.document, "msg-assistant-body", "最新一段正文"),
    div(x.document, "round-usage", "本轮用量"),
  );
  x.chatInner.appendChild(msg);
  return msg;
}

test("直播轮：只保留最新正文，其余收进大折叠", async t => {
  const x = setup();
  t.after(x.close);
  const msg = liveMsg(x);
  x.App.roundFold.sync(msg);
  const header = msg.querySelector(".round-fold-summary");
  assert.ok(header, "应建折叠头");
  const bodies = Array.from(msg.querySelectorAll(".msg-assistant-body"));
  assert.equal(bodies[0].classList.contains("is-roundfold-hidden"), true);
  assert.equal(bodies[1].classList.contains("is-roundfold-hidden"), false);
  assert.equal(msg.querySelector(".think-block").classList.contains("is-roundfold-hidden"), true);
  assert.equal(msg.querySelector(".tool-block").classList.contains("is-roundfold-hidden"), true);
  assert.equal(msg.querySelector(".round-usage").classList.contains("is-roundfold-hidden"), false);
  // 折叠头紧贴首个成员之前
  assert.equal(header.nextElementSibling, msg.querySelector(".msg-time"));
  assert.match(header.querySelector(".round-fold-counts").textContent, /1 次工具调用/);
  assert.match(header.querySelector(".round-fold-counts").textContent, /1 段正文/);
});

test("单段正文 / 纯工具轮不建折叠", async t => {
  const x = setup();
  t.after(x.close);
  const single = div(x.document, "msg");
  single.append(div(x.document, "msg-assistant-body", "只有一段"));
  x.chatInner.appendChild(single);
  x.App.roundFold.sync(single);
  assert.equal(single.querySelector(".round-fold-summary"), null);
  const toolsOnly = div(x.document, "msg");
  toolsOnly.append(div(x.document, "tool-block", "t1"), div(x.document, "tool-block", "t2"));
  x.chatInner.appendChild(toolsOnly);
  x.App.roundFold.sync(toolsOnly);
  assert.equal(toolsOnly.querySelector(".round-fold-summary"), null);
});

test("流式新段（空尾）可见，旧段即时折叠", async t => {
  const x = setup();
  t.after(x.close);
  const msg = div(x.document, "msg");
  const first = div(x.document, "msg-assistant-body", "第一段");
  const streaming = div(x.document, "msg-assistant-body msg-cursor", "");
  msg.append(first, streaming);
  x.chatInner.appendChild(msg);
  x.App.roundFold.sync(msg);
  assert.equal(first.classList.contains("is-roundfold-hidden"), true);
  assert.equal(streaming.classList.contains("is-roundfold-hidden"), false);
});

test("点击切换 + 选择跨重建记忆 + 刷新高度", async t => {
  const x = setup();
  t.after(x.close);
  const msg = liveMsg(x, 7);
  x.App.roundFold.sync(msg);
  const header = msg.querySelector(".round-fold-summary");
  const body1 = msg.querySelectorAll(".msg-assistant-body")[0];
  assert.equal(body1.classList.contains("is-roundfold-hidden"), true);
  header.querySelector("button").click();
  assert.equal(body1.classList.contains("is-roundfold-hidden"), false);
  assert.equal(header.classList.contains("is-open"), true);
  assert.equal(x.calls.paused, 1);
  assert.equal(x.calls.refreshed, 1);
  const rev = x.App.roundFold.revision();
  // 同轮重建后仍保持展开
  const msg2 = liveMsg(x, 7);
  msg.remove();
  x.App.roundFold.sync(msg2);
  const body1b = msg2.querySelectorAll(".msg-assistant-body")[0];
  assert.equal(body1b.classList.contains("is-roundfold-hidden"), false);
  assert.equal(msg2.querySelector(".round-fold-summary").classList.contains("is-open"), true);
  assert.ok(x.App.roundFold.revision() >= rev);
});

test("待确认成员强制展开；交互/压缩节点永不折叠，单条用量保留", async t => {
  const x = setup();
  t.after(x.close);
  const msg = div(x.document, "msg");
  msg.dataset.round = "9";
  const waiting = div(x.document, "tool-block is-waiting", "待确认工具");
  const ask = div(x.document, "ask-block", "提问卡");
  const usage = div(x.document, "round-usage", "用量");
  const compaction = div(x.document, "compaction-bar", "压缩条");
  msg.append(
    div(x.document, "msg-assistant-body", "旧正文"),
    waiting, ask, compaction,
    div(x.document, "msg-assistant-body", "最新正文"),
    usage,
  );
  x.chatInner.appendChild(msg);
  x.App.roundFold.sync(msg);
  const header = msg.querySelector(".round-fold-summary");
  assert.ok(header);
  assert.equal(header.classList.contains("is-open"), true);
  assert.equal(header.classList.contains("is-waiting"), true);
  for (const node of [ask, usage, compaction]) {
    assert.equal(node.classList.contains("is-roundfold-hidden"), false);
    assert.equal(node._roundFold, undefined);
  }
});

test("合并轮次只保留最后一条用量行", async t => {
  const x = setup();
  t.after(x.close);
  const chat = x.chatInner;
  const user = div(x.document, "msg msg-user");
  user.dataset.round = "11";
  const usage1 = div(x.document, "round-usage", "用量旧");
  const usage2 = div(x.document, "round-usage", "用量新");
  chat.append(
    user,
    div(x.document, "msg-assistant-body", "旧段"),
    usage1,
    div(x.document, "msg-assistant-body", "新段"),
    usage2,
  );
  x.App.roundFold.sync(chat);
  assert.equal(usage1.classList.contains("is-roundfold-hidden"), true);
  assert.equal(usage2.classList.contains("is-roundfold-hidden"), false);
});

test("孤儿摘要头被清扫（窗口恢复残留不堆积）", async t => {
  const x = setup();
  t.after(x.close);
  const msg = liveMsg(x, 12);
  x.App.roundFold.sync(msg);
  assert.equal(msg.querySelectorAll(".round-fold-summary").length, 1);
  // 模拟窗口恢复塞回的旧头（无 fold 认领）
  const ghost = div(x.document, "round-fold-summary");
  msg.appendChild(ghost);
  assert.equal(msg.querySelectorAll(".round-fold-summary").length, 2);
  x.App.roundFold.sync(msg);
  const headers = msg.querySelectorAll(".round-fold-summary");
  assert.equal(headers.length, 1);
  assert.equal(ghost.isConnected, false);
});

test("跨容器移动节点被新轮认领（用量行收养不隐身）", async t => {
  const x = setup();
  t.after(x.close);
  const msgA = div(x.document, "msg");
  msgA.dataset.round = "20";
  const bodyA = div(x.document, "msg-assistant-body", "A旧段");
  msgA.append(bodyA, div(x.document, "msg-assistant-body", "A新段"));
  const msgB = div(x.document, "msg");
  msgB.dataset.round = "21";
  const bodyB = div(x.document, "msg-assistant-body", "B正文");
  msgB.append(bodyB);
  x.chatInner.append(msgA, msgB);
  x.App.roundFold.sync(msgA);
  assert.equal(bodyA.classList.contains("is-roundfold-hidden"), true);
  // 节点搬到另一容器（如用量行被直播流收养）
  msgB.insertBefore(bodyA, bodyB);
  x.App.roundFold.sync(msgB);
  // 旧 fold 被掏空就地解散，节点归属新 fold 且按新规则显隐
  assert.equal(msgA.querySelector(".round-fold-summary"), null);
  assert.equal(bodyA.classList.contains("is-roundfold-hidden"), true);
  assert.equal(bodyB.classList.contains("is-roundfold-hidden"), false);
  assert.ok(msgB.querySelector(".round-fold-summary"));
});

test("尾正文之后的进行中内容保持可见（不收进大折叠）", async t => {
  const x = setup();
  t.after(x.close);
  const msg = div(x.document, "msg");
  msg.dataset.round = "30";
  const before = div(x.document, "msg-assistant-body", "旧段");
  const tail = div(x.document, "msg-assistant-body", "进行中正文");
  const runningTool = div(x.document, "tool-block is-executing", "运行中工具");
  const groupHead = div(x.document, "process-group-summary", "处理过程");
  msg.append(before, tail, groupHead, runningTool);
  x.chatInner.appendChild(msg);
  x.App.roundFold.sync(msg);
  assert.equal(before.classList.contains("is-roundfold-hidden"), true);
  assert.equal(tail.classList.contains("is-roundfold-hidden"), false);
  assert.equal(groupHead.classList.contains("is-roundfold-hidden"), false);
  assert.equal(runningTool.classList.contains("is-roundfold-hidden"), false);
  // 头统计只计尾前成员
  const counts = msg.querySelector(".round-fold-counts").textContent;
  assert.match(counts, /1 段正文/);
  assert.doesNotMatch(counts, /工具调用/);
});

test("扁平历史按用户节点分轮，各轮独立折叠", async t => {
  const x = setup();
  t.after(x.close);
  function user(round, text) {
    const u = div(x.document, "msg msg-user");
    u.dataset.round = String(round);
    u.append(div(x.document, "msg-bubble msg-user-text", text));
    return u;
  }
  x.chatInner.append(
    user(1, "问题一"),
    div(x.document, "msg-assistant-body", "回答一旧段"),
    div(x.document, "tool-block", "工具一"),
    div(x.document, "msg-assistant-body", "回答一新段"),
    div(x.document, "round-usage", "用量一"),
    user(2, "问题二"),
    div(x.document, "msg-assistant-body", "回答二只有一段"),
  );
  x.App.roundFold.sync(x.chatInner);
  const headers = x.chatInner.querySelectorAll(":scope > .round-fold-summary");
  assert.equal(headers.length, 1);
  const bodies = Array.from(x.chatInner.querySelectorAll(".msg-assistant-body"));
  assert.equal(bodies[0].classList.contains("is-roundfold-hidden"), true);
  assert.equal(bodies[1].classList.contains("is-roundfold-hidden"), false);
  assert.equal(bodies[2].classList.contains("is-roundfold-hidden"), false);
  // 用户节点与用量行不受影响
  assert.equal(x.chatInner.querySelector(".msg-user").classList.contains("is-roundfold-hidden"), false);
  assert.equal(x.chatInner.querySelector(".round-usage").classList.contains("is-roundfold-hidden"), false);
});

test("小折叠状态在展开大折叠后保持", async t => {
  const x = setup();
  t.after(x.close);
  const msg = div(x.document, "msg");
  msg.dataset.round = "5";
  const groupHead = div(x.document, "process-group-summary", "处理过程");
  const groupedTool = div(x.document, "tool-block is-process-hidden", "组内工具");
  msg.append(
    groupHead, groupedTool,
    div(x.document, "msg-assistant-body", "尾正文"),
  );
  x.chatInner.appendChild(msg);
  x.App.roundFold.sync(msg);
  assert.equal(groupHead.classList.contains("is-roundfold-hidden"), true);
  msg.querySelector(".round-fold-summary button").click();
  assert.equal(groupHead.classList.contains("is-roundfold-hidden"), false);
  // 小折叠自己的隐藏标记不受影响
  assert.equal(groupedTool.classList.contains("is-process-hidden"), true);
  assert.equal(groupedTool.classList.contains("is-roundfold-hidden"), false);
});

test("用时按成员时间戳计算；异常标红", async t => {
  const x = setup();
  t.after(x.close);
  const msg = div(x.document, "msg");
  const old = div(x.document, "msg-assistant-body", "旧");
  old.dataset.stageTimestamp = "2026-10-30T10:00:00.000Z";
  const bad = div(x.document, "tool-block is-failed", "失败工具");
  bad.dataset.stageTimestamp = "2026-10-30T10:01:30.000Z";
  const tail = div(x.document, "msg-assistant-body", "新");
  tail.dataset.stageTimestamp = "2026-10-30T10:01:30.000Z";
  msg.append(old, bad, tail);
  x.chatInner.appendChild(msg);
  x.App.roundFold.sync(msg);
  const header = msg.querySelector(".round-fold-summary");
  assert.equal(header.querySelector(".round-fold-duration").textContent, "用时 1m 30s");
  assert.match(header.querySelector(".round-fold-counts").textContent, /1 项异常/);
  assert.equal(header.classList.contains("has-error"), true);
});

test("reveal 打开祖先大折叠", async t => {
  const x = setup();
  t.after(x.close);
  const msg = liveMsg(x, 3);
  x.App.roundFold.sync(msg);
  const body1 = msg.querySelectorAll(".msg-assistant-body")[0];
  assert.equal(body1.classList.contains("is-roundfold-hidden"), true);
  x.App.roundFold.reveal(body1);
  assert.equal(body1.classList.contains("is-roundfold-hidden"), false);
});

test("adjustHeights：折叠轮未渲染成员记 0，首个记头高度", async t => {
  const x = setup();
  t.after(x.close);
  const records = [
    { round: 1, kind: "user" },
    { round: 1, kind: "assistant" },
    { round: 1, kind: "tool" },
    { round: 1, kind: "assistant" },
    { round: 2, kind: "user" },
    { round: 2, kind: "assistant" },
  ];
  const heights = [50, 100, 120, 200, 50, 80];
  x.App.roundFold.adjustHeights(records, heights, new Map());
  assert.deepEqual(heights, [50, 36, 0, 200, 50, 80]);
});

test("snapBounds：切进折叠成员区时扩到整轮，展开不吸附", async t => {
  const x = setup();
  t.after(x.close);
  const records = [
    { round: 1, kind: "user" },
    { round: 1, kind: "assistant" },
    { round: 1, kind: "tool" },
    { round: 1, kind: "assistant" },
    { round: 2, kind: "user" },
  ];
  // 跨 vm realm 的对象原型不同：JSON 归一化后再断言结构
  const snap = (s, e) => JSON.parse(JSON.stringify(x.App.roundFold.snapBounds(records, s, e)));
  // 默认折叠：窗口 [2,4) 切进成员 → 扩到 [0,4)
  assert.deepEqual(snap(2, 4), { start: 0, end: 4 });
  // 切在尾正文上不吸附
  assert.deepEqual(snap(3, 5), { start: 3, end: 5 });
  // 空窗口原样返回
  assert.deepEqual(snap(2, 2), { start: 2, end: 2 });
});

function tickingMsg(x, round, active) {
  const now = Date.now();
  const msg = div(x.document, "msg");
  msg.dataset.round = String(round);
  const old = div(x.document, "msg-assistant-body", "旧段");
  old.dataset.stageTimestamp = new Date(now - 125000).toISOString();
  const tool = div(x.document, active ? "tool-block is-executing" : "tool-block", "工具");
  tool.dataset.stageTimestamp = new Date(now - 120000).toISOString();
  const tail = div(x.document, active ? "msg-assistant-body msg-cursor" : "msg-assistant-body", "新段");
  tail.dataset.stageTimestamp = new Date(now - 120000).toISOString();
  msg.append(old, tool, tail);
  x.chatInner.appendChild(msg);
  return msg;
}

test("运行中折叠 tick 按当前时间更新用时", async t => {
  const x = setup();
  t.after(x.close);
  const msg = tickingMsg(x, 40, true);
  x.App.roundFold.sync(msg);
  const duration = msg.querySelector(".round-fold-duration");
  assert.equal(duration.textContent, "用时 5s");
  x.App.roundFold.tick();
  assert.equal(duration.textContent, "用时 2m 5s");
});

test("已结束/等待确认的折叠不计时（冻结）", async t => {
  const x = setup();
  t.after(x.close);
  const done = tickingMsg(x, 41, false);
  x.App.roundFold.sync(done);
  const frozen = done.querySelector(".round-fold-duration");
  assert.equal(frozen.textContent, "用时 5s");
  x.App.roundFold.tick();
  assert.equal(frozen.textContent, "用时 5s");
  const waiting = tickingMsg(x, 42, true);
  waiting.querySelector(".tool-block").classList.add("is-waiting");
  x.App.roundFold.sync(waiting);
  const held = waiting.querySelector(".round-fold-duration");
  x.App.roundFold.tick();
  assert.equal(held.textContent, "用时 5s");
});

test("1s 计时器真实触发（端到端）", async t => {
  const x = setup();
  t.after(x.close);
  const msg = div(x.document, "msg");
  msg.dataset.round = "43";
  const start = div(x.document, "msg-assistant-body", "旧段");
  start.dataset.stageTimestamp = new Date(Date.now() - 1000).toISOString();
  const tail = div(x.document, "msg-assistant-body msg-cursor", "");
  tail.dataset.stageTimestamp = new Date(Date.now() - 1000).toISOString();
  msg.append(start, tail);
  x.chatInner.appendChild(msg);
  x.App.roundFold.sync(msg);
  const duration = msg.querySelector(".round-fold-duration");
  assert.equal(duration.textContent, "");
  const deadline = Date.now() + 3000;
  while (!duration.textContent && Date.now() < deadline) {
    await new Promise(r => setTimeout(r, 100));
  }
  assert.match(duration.textContent, /^用时 \d+s$/);
});
