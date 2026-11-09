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

test("同轮引导切分段：前段独立折叠（不再同 key 覆盖丢失）", async t => {
  const x = setup();
  t.after(x.close);
  function user(round, text) {
    const u = div(x.document, "msg msg-user");
    u.dataset.round = String(round);
    u.append(div(x.document, "msg-bubble msg-user-text", text));
    return u;
  }
  const tool1 = div(x.document, "tool-block", "工具一");
  const body1 = div(x.document, "msg-assistant-body", "旧段正文");
  const user2 = user(7, "引导");
  const tool2 = div(x.document, "tool-block", "工具二");
  const body2 = div(x.document, "msg-assistant-body", "最新正文");
  x.chatInner.append(user(7, "问题"), tool1, body1, user2, tool2, body2);
  x.App.roundFold.sync(x.chatInner);
  const headers = x.chatInner.querySelectorAll(".round-fold-summary");
  // 前段独立成折（旧 bug：同 key 覆盖导致前段无折叠）；末段单个工具按单块规则不折
  assert.equal(headers.length, 1);
  assert.equal(tool1.classList.contains("is-roundfold-hidden"), true);
  assert.equal(body1.classList.contains("is-roundfold-hidden"), true);
  assert.equal(tool2.classList.contains("is-roundfold-hidden"), false);
  assert.equal(body2.classList.contains("is-roundfold-hidden"), false);
  // 引导气泡保持可见
  assert.equal(user2.classList.contains("is-roundfold-hidden"), false);
  assert.match(headers[0].querySelector(".round-fold-counts").textContent, /1 次工具调用/);
});

test("引导前后各成折叠且计时分段", async t => {
  const x = setup();
  t.after(x.close);
  const now = Date.now();
  const at = ms => new Date(ms).toISOString();
  function user(round, text, ts) {
    const u = div(x.document, "msg msg-user");
    u.dataset.round = String(round);
    u.dataset.stageTimestamp = at(ts);
    u.append(div(x.document, "msg-bubble msg-user-text", text));
    return u;
  }
  function body(text, ts) {
    const b = div(x.document, "msg-assistant-body", text);
    b.dataset.stageTimestamp = at(ts);
    return b;
  }
  function tool(text, ts) {
    const b = div(x.document, "tool-block", text);
    b.dataset.stageTimestamp = at(ts);
    return b;
  }
  const t0 = now - 200000;
  x.chatInner.append(
    user(7, "问题", t0),
    tool("工具一", t0 + 10000), body("旧段", t0 + 20000),
    user(7, "引导", t0 + 100000),
    tool("工具二", t0 + 110000), body("中段", t0 + 120000), body("最新", t0 + 130000),
  );
  x.App.roundFold.sync(x.chatInner);
  const headers = x.chatInner.querySelectorAll(".round-fold-summary");
  assert.equal(headers.length, 2);
  // 前段停表封存（发送→段末 20s，不含引导后内容）；后段从引导重计 30s
  assert.equal(headers[0].querySelector(".round-fold-duration").textContent, "用时 20s");
  assert.equal(headers[1].querySelector(".round-fold-duration").textContent, "用时 30s");
});

test("直播容器内引导同样分段（无轮号气泡）", async t => {
  const x = setup();
  t.after(x.close);
  const now = Date.now();
  const at = ms => new Date(ms).toISOString();
  const t0 = now - 200000;
  const user = div(x.document, "msg msg-user");
  user.dataset.round = "8";
  user.dataset.stageTimestamp = at(t0);
  const msg = div(x.document, "msg");
  msg.dataset.round = "8";
  const think = div(x.document, "think-block", "思考");
  think.dataset.stageTimestamp = at(t0 + 10000);
  const tool = div(x.document, "tool-block", "工具");
  tool.dataset.stageTimestamp = at(t0 + 20000);
  const steer = div(x.document, "msg msg-user");
  steer.dataset.stageTimestamp = at(t0 + 100000);
  const body = div(x.document, "msg-assistant-body", "中段");
  body.dataset.stageTimestamp = at(t0 + 110000);
  const tail = div(x.document, "msg-assistant-body", "最新");
  tail.dataset.stageTimestamp = at(t0 + 120000);
  msg.append(think, tool, steer, body, tail);
  x.chatInner.append(user, msg);
  x.App.roundFold.sync(msg);
  const headers = msg.querySelectorAll(".round-fold-summary");
  assert.equal(headers.length, 2);
  assert.equal(headers[0].querySelector(".round-fold-duration").textContent, "用时 20s");
  assert.equal(headers[1].querySelector(".round-fold-duration").textContent, "用时 20s");
  assert.equal(think.classList.contains("is-roundfold-hidden"), true);
  assert.equal(steer.classList.contains("is-roundfold-hidden"), false);
  assert.equal(tail.classList.contains("is-roundfold-hidden"), false);
});

test("记录口径：引导轮分段计高度", async t => {
  const x = setup();
  t.after(x.close);
  const heights = [50, 100, 110, 120, 130, 140, 200];
  x.App.roundFold.adjustHeights([
    { round: 6, kind: "user" }, { round: 6, kind: "tool" }, { round: 6, kind: "assistant" },
    { round: 6, kind: "user" }, { round: 6, kind: "tool" }, { round: 6, kind: "tool" },
    { round: 6, kind: "assistant" },
  ], heights, new Map());
  assert.deepEqual(heights, [50, 36, 0, 120, 36, 0, 200]);
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

test("暂停续跑：暂停容器尾后已完成残留被收纳，进行中仍可见", async t => {
  const x = setup();
  t.after(x.close);
  const user = div(x.document, "msg msg-user");
  user.dataset.round = "1";
  // 第一程：正文后冻结了已完成的思考+工具（暂停时尾后残留）
  const paused = div(x.document, "msg");
  const body1 = div(x.document, "msg-assistant-body", "第一段正文");
  const think2 = div(x.document, "think-block", "残留思考");
  const tool2 = div(x.document, "tool-block", "残留工具");
  paused.append(body1, think2, tool2);
  // 续跑容器：尾后运行中工具保持可见
  const live = div(x.document, "msg");
  const body2 = div(x.document, "msg-assistant-body", "续跑正文");
  const running = div(x.document, "tool-block is-executing", "运行中工具");
  live.append(body2, running);
  x.chatInner.append(user, paused, live);
  x.App.roundFold.sync(x.chatInner);
  assert.equal(think2.classList.contains("is-roundfold-hidden"), true);
  assert.equal(tool2.classList.contains("is-roundfold-hidden"), true);
  assert.ok(paused.querySelector(".round-fold-summary"), "暂停容器应有折叠头");
  assert.equal(body2.classList.contains("is-roundfold-hidden"), false);
  assert.equal(running.classList.contains("is-roundfold-hidden"), false);
  assert.equal(live.querySelector(".round-fold-summary"), null, "续跑容器无有效成员不建折叠");
});

test("单小块不建大折叠：单思考/单工具/单子任务/单提示/单用量", async t => {
  const x = setup();
  t.after(x.close);
  const cases = ["think-block", "tool-block", "agent-block", "notice-bar", "round-usage"];
  for (const cls of cases) {
    const msg = div(x.document, "msg");
    msg.append(div(x.document, cls, "单个"), div(x.document, "msg-assistant-body", "尾正文"));
    x.chatInner.appendChild(msg);
    x.App.roundFold.sync(msg);
    assert.equal(msg.querySelector(".round-fold-summary"), null, cls + "单个不应建折叠");
    msg.remove();
  }
  // 单段旧正文（流式进行态）仍折叠
  const msg = div(x.document, "msg");
  const first = div(x.document, "msg-assistant-body", "旧段");
  msg.append(first, div(x.document, "msg-assistant-body msg-cursor", ""));
  x.chatInner.appendChild(msg);
  x.App.roundFold.sync(msg);
  assert.ok(msg.querySelector(".round-fold-summary"));
  assert.equal(first.classList.contains("is-roundfold-hidden"), true);
});

test("单个小折叠组视为一块：不套大折叠；两块则折叠", async t => {
  const x = setup();
  t.after(x.close);
  function groupedMsg() {
    const msg = div(x.document, "msg");
    const head = div(x.document, "process-group-summary", "处理过程");
    const t1 = div(x.document, "tool-block is-process-hidden", "工具一");
    const t2 = div(x.document, "tool-block is-process-hidden", "工具二");
    const group = { header: head, members: [t1, t2] };
    head._processSummary = group;
    t1._processGroup = group; t2._processGroup = group;
    msg.append(head, t1, t2, div(x.document, "msg-assistant-body", "尾正文"));
    x.chatInner.appendChild(msg);
    return msg;
  }
  const single = groupedMsg();
  x.App.roundFold.sync(single);
  assert.equal(single.querySelector(".round-fold-summary"), null, "单个闭合组不应再套大折叠");
  // 组外再加一块：两块应折叠
  const two = groupedMsg();
  two.insertBefore(div(x.document, "think-block", "组外思考"), two.firstChild);
  x.App.roundFold.sync(two);
  assert.ok(two.querySelector(".round-fold-summary"), "组+单块应建折叠");
});

test("尾后小折叠组整体收纳：全完成连头收走；有运行则整组可见", async t => {
  const x = setup();
  t.after(x.close);
  function tailGroupMsg(running) {
    const msg = div(x.document, "msg");
    const head = div(x.document, "process-group-summary", "处理过程");
    const t1 = div(x.document, "tool-block", "工具一");
    const t2 = div(x.document, running ? "tool-block is-executing" : "tool-block", "工具二");
    const group = { header: head, members: [t1, t2] };
    head._processSummary = group;
    t1._processGroup = group; t2._processGroup = group;
    msg.append(
      div(x.document, "msg-assistant-body", "旧段"),
      div(x.document, "msg-assistant-body", "尾正文"),
      head, t1, t2,
    );
    x.chatInner.appendChild(msg);
    return { msg, head, t1, t2 };
  }
  const done = tailGroupMsg(false);
  x.App.roundFold.sync(done.msg);
  assert.ok(done.msg.querySelector(".round-fold-summary"));
  for (const n of [done.head, done.t1, done.t2]) {
    assert.equal(n.classList.contains("is-roundfold-hidden"), true, "全完成组连头收走");
  }
  const live = tailGroupMsg(true);
  x.App.roundFold.sync(live.msg);
  for (const n of [live.head, live.t1, live.t2]) {
    assert.equal(n.classList.contains("is-roundfold-hidden"), false, "有运行成员则整组可见");
  }
  // 运行组头无 linkage 时同样保持可见（已有单测覆盖进行中工具，此处只断言裸头）
  assert.equal(live.msg.querySelector(".round-fold-summary button") !== null, true);
});

test("尾后待确认与最新用量行保持可见", async t => {
  const x = setup();
  t.after(x.close);
  const msg = div(x.document, "msg");
  const waiting = div(x.document, "tool-block is-waiting", "待确认");
  const usageOld = div(x.document, "round-usage", "用量旧");
  const usageNew = div(x.document, "round-usage", "用量新");
  msg.append(
    div(x.document, "msg-assistant-body", "旧段"),
    div(x.document, "msg-assistant-body", "尾正文"),
    waiting, usageOld, usageNew,
  );
  x.chatInner.appendChild(msg);
  x.App.roundFold.sync(msg);
  assert.equal(waiting.classList.contains("is-roundfold-hidden"), false);
  assert.equal(waiting._roundFold, undefined, "待确认节点不进折叠");
  assert.ok(usageOld._roundFold, "尾后旧用量应被收纳为成员");
  assert.equal(usageNew.classList.contains("is-roundfold-hidden"), false, "最新用量保留");
  assert.equal(usageNew._roundFold, undefined);
  const header = msg.querySelector(".round-fold-summary");
  assert.ok(header);
  assert.equal(header.classList.contains("is-waiting"), true, "尾后待确认应点亮头状态");
  assert.equal(header.classList.contains("is-open"), true, "待确认应强制展开");
  // 待确认解除后恢复折叠
  waiting.classList.remove("is-waiting");
  x.App.roundFold.sync(msg);
  assert.equal(usageOld.classList.contains("is-roundfold-hidden"), true);
  assert.equal(header.classList.contains("is-open"), false);
});

test("记录口径：尾后已完成记录收纳，单条非正文不成折叠", async t => {
  const x = setup();
  t.after(x.close);
  // 尾后两条工具：收纳（首个记头高度 36，其余 0）
  const heights = [50, 100, 120, 130, 80];
  x.App.roundFold.adjustHeights([
    { round: 1, kind: "user" },
    { round: 1, kind: "assistant" },
    { round: 1, kind: "tool" },
    { round: 1, kind: "tool" },
    { round: 1, kind: "usage" },
  ], heights, new Map());
  assert.deepEqual(heights, [50, 100, 36, 0, 80]);
  // 尾后单条工具：不成折叠，不调整
  const single = [50, 100, 120];
  x.App.roundFold.adjustHeights([
    { round: 2, kind: "user" },
    { round: 2, kind: "assistant" },
    { round: 2, kind: "tool" },
  ], single, new Map());
  assert.deepEqual(single, [50, 100, 120]);
  // 尾后旧用量收纳、最新用量保留；单条旧用量不成折叠
  const usage = [50, 100, 30, 30];
  x.App.roundFold.adjustHeights([
    { round: 3, kind: "user" },
    { round: 3, kind: "assistant" },
    { round: 3, kind: "usage" },
    { round: 3, kind: "usage" },
  ], usage, new Map());
  assert.deepEqual(usage, [50, 100, 30, 30]);
});

test("记录口径：小折叠组整体算一块（经 processGroups.groupOf）", async t => {
  const x = setup();
  t.after(x.close);
  const group = { start: 1, end: 3 };
  const think1 = { round: 4, kind: "think" };
  const think2 = { round: 4, kind: "think" };
  x.App.processGroups = { groupOf: rec => (rec === think1 || rec === think2 ? group : null) };
  const heights = [50, 100, 110, 200];
  x.App.roundFold.adjustHeights([
    { round: 4, kind: "user" }, think1, think2, { round: 4, kind: "assistant" },
  ], heights, new Map());
  assert.deepEqual(heights, [50, 100, 110, 200], "单组不成大折叠");
  // 两组则折叠
  const g2 = { start: 3, end: 5 };
  const think3 = { round: 5, kind: "think" };
  const think4 = { round: 5, kind: "think" };
  const tool1 = { round: 5, kind: "tool" };
  const tool2 = { round: 5, kind: "tool" };
  x.App.processGroups = { groupOf: rec => {
    if (rec === think3 || rec === think4) return group;
    if (rec === tool1 || rec === tool2) return g2;
    return null;
  } };
  const heights2 = [50, 100, 110, 120, 130, 200];
  x.App.roundFold.adjustHeights([
    { round: 5, kind: "user" }, think3, think4, tool1, tool2, { round: 5, kind: "assistant" },
  ], heights2, new Map());
  assert.deepEqual(heights2, [50, 36, 0, 0, 0, 200], "两组应折叠");
});

test("用时超1小时显示到秒", async t => {
  const x = setup();
  t.after(x.close);
  const now = Date.now();
  const at = ms => new Date(ms).toISOString();
  const user = div(x.document, "msg msg-user");
  user.dataset.round = "9";
  user.dataset.stageTimestamp = at(now - 4810000);
  const tool = div(x.document, "tool-block", "工具");
  tool.dataset.stageTimestamp = at(now - 4805000);
  const body = div(x.document, "msg-assistant-body", "旧段");
  body.dataset.stageTimestamp = at(now - 30000);
  const tail = div(x.document, "msg-assistant-body", "最新");
  tail.dataset.stageTimestamp = at(now);
  x.chatInner.append(user, tool, body, tail);
  x.App.roundFold.sync(x.chatInner);
  const h = x.chatInner.querySelector(".round-fold-summary .round-fold-duration");
  assert.equal(h.textContent, "用时 1h 20m 10s");
});

test("长工具按结束戳计时（不吞段内时长）", async t => {
  const x = setup();
  t.after(x.close);
  const now = Date.now();
  const at = ms => new Date(ms).toISOString();
  const user = div(x.document, "msg msg-user");
  user.dataset.round = "11";
  user.dataset.stageTimestamp = at(now - 400000);
  const tool = div(x.document, "tool-block", "长工具");
  tool.dataset.stageTimestamp = at(now - 390000);
  tool.dataset.stageEndTimestamp = at(now - 100000);
  const body = div(x.document, "msg-assistant-body", "旧段");
  body.dataset.stageTimestamp = at(now - 90000);
  body.dataset.stageEndTimestamp = at(now - 80000);
  const tail = div(x.document, "msg-assistant-body", "最新");
  tail.dataset.stageTimestamp = at(now - 70000);
  tail.dataset.stageEndTimestamp = at(now - 60000);
  x.chatInner.append(user, tool, body, tail);
  x.App.roundFold.sync(x.chatInner);
  const h = x.chatInner.querySelector(".round-fold-summary .round-fold-duration");
  assert.equal(h.textContent, "用时 5m 40s");
});

test("同轮扁平+容器分裂时合并为一套折叠（不双头）", async t => {
  const x = setup();
  t.after(x.close);
  const now = Date.now();
  const at = ms => new Date(ms).toISOString();
  const user = div(x.document, "msg msg-user");
  user.dataset.round = "10";
  user.dataset.stageTimestamp = at(now - 60000);
  const tool1 = div(x.document, "tool-block", "旧工具");
  tool1.dataset.stageTimestamp = at(now - 50000);
  const body1 = div(x.document, "msg-assistant-body", "旧段");
  body1.dataset.stageTimestamp = at(now - 40000);
  const msg = div(x.document, "msg");
  msg.dataset.round = "10";
  const tool2 = div(x.document, "tool-block", "新工具");
  tool2.dataset.stageTimestamp = at(now - 30000);
  const tail = div(x.document, "msg-assistant-body", "最新");
  tail.dataset.stageTimestamp = at(now - 20000);
  msg.append(tool2, tail);
  x.chatInner.append(user, tool1, body1, msg);
  x.App.roundFold.sync(x.chatInner);
  const headers = x.chatInner.querySelectorAll(".round-fold-summary");
  assert.equal(headers.length, 1);
  assert.match(headers[0].querySelector(".round-fold-counts").textContent, /2 次工具调用/);
  assert.equal(tool1.classList.contains("is-roundfold-hidden"), true);
  assert.equal(tool2.classList.contains("is-roundfold-hidden"), true);
  assert.equal(tail.classList.contains("is-roundfold-hidden"), false);
});

test("直播中非末段不跳秒（只有末段计时）", async t => {
  const x = setup();
  t.after(x.close);
  const now = Date.now();
  const at = ms => new Date(ms).toISOString();
  const msg = div(x.document, "msg");
  const think = div(x.document, "think-block", "思考");
  think.dataset.stageTimestamp = at(now - 200000);
  const tool = div(x.document, "tool-block", "工具");
  tool.dataset.stageTimestamp = at(now - 190000);
  const steer = div(x.document, "msg msg-user");
  steer.dataset.stageTimestamp = at(now - 100000);
  const body = div(x.document, "msg-assistant-body", "中段");
  body.dataset.stageTimestamp = at(now - 90000);
  const tail = div(x.document, "msg-assistant-body", "最新");
  tail.dataset.stageTimestamp = at(now - 80000);
  msg.append(think, tool, steer, body, tail);
  x.chatInner.appendChild(msg);
  x.App.state.streaming = true;
  x.App.state.streamingSession = "s";
  x.App.state.activeStream = { messageNode: msg };
  x.App.roundFold.sync(msg);
  const headers = msg.querySelectorAll(".round-fold-summary");
  assert.equal(headers.length, 2);
  assert.equal(headers[0].classList.contains("is-running"), false, "前段应停表");
  assert.equal(headers[1].classList.contains("is-running"), true, "末段继续计时");
});

test("直播等待模型期间计时跳动（无 DOM 进行态也亮灯）", async t => {
  const x = setup();
  t.after(x.close);
  const now = Date.now();
  const msg = div(x.document, "msg");
  const old = div(x.document, "msg-assistant-body", "旧段");
  old.dataset.stageTimestamp = new Date(now - 125000).toISOString();
  const tail = div(x.document, "msg-assistant-body", "尾正文");
  tail.dataset.stageTimestamp = new Date(now - 120000).toISOString();
  msg.append(old, tail);
  x.chatInner.appendChild(msg);
  // 直播中：无任何进行态 class，但容器是当前流
  x.App.state.streaming = true;
  x.App.state.streamingSession = "s";
  x.App.state.activeStream = { messageNode: msg };
  x.App.roundFold.sync(msg);
  const header = msg.querySelector(".round-fold-summary");
  assert.ok(header);
  assert.equal(header.classList.contains("is-running"), true, "直播流应亮运行灯");
  assert.equal(header.querySelector(".round-fold-duration").textContent, "用时 5s");
  x.App.roundFold.tick();
  assert.equal(header.querySelector(".round-fold-duration").textContent, "用时 2m 5s");
});

test("暂停/结束停表并摘灯；继续后新容器恢复、旧段冻结", async t => {
  const x = setup();
  t.after(x.close);
  const now = Date.now();
  function livePair(oldText) {
    const msg = div(x.document, "msg");
    const old = div(x.document, "msg-assistant-body", oldText);
    old.dataset.stageTimestamp = new Date(now - 125000).toISOString();
    const tail = div(x.document, "msg-assistant-body", "尾");
    tail.dataset.stageTimestamp = new Date(now - 120000).toISOString();
    msg.append(old, tail);
    x.chatInner.appendChild(msg);
    return { msg, header: null };
  }
  const first = livePair("旧段一");
  x.App.state.streaming = true;
  x.App.state.streamingSession = "s";
  x.App.state.activeStream = { messageNode: first.msg };
  x.App.roundFold.sync(first.msg);
  first.header = first.msg.querySelector(".round-fold-summary");
  x.App.roundFold.tick();
  assert.equal(first.header.querySelector(".round-fold-duration").textContent, "用时 2m 5s");
  // 暂停：流结束，类已清理
  x.App.state.streaming = false;
  x.App.state.streamingSession = null;
  x.App.state.activeStream = null;
  x.App.roundFold.tick();
  assert.equal(first.header.classList.contains("is-running"), false, "停表应摘灯");
  assert.equal(first.header.querySelector(".round-fold-duration").textContent, "用时 5s", "冻结为静态跨度");
  // 继续：新容器恢复跳动，旧段保持冻结
  const second = livePair("旧段二");
  x.App.state.streaming = true;
  x.App.state.streamingSession = "s";
  x.App.state.activeStream = { messageNode: second.msg };
  x.App.roundFold.sync(second.msg);
  second.header = second.msg.querySelector(".round-fold-summary");
  assert.equal(second.header.classList.contains("is-running"), true);
  x.App.roundFold.tick();
  assert.match(second.header.querySelector(".round-fold-duration").textContent, /^用时 2m /);
  assert.equal(first.header.querySelector(".round-fold-duration").textContent, "用时 5s", "旧暂停段不跟跳");
  assert.equal(first.header.classList.contains("is-running"), false);
});

test("待确认一票否决：直播流也不计时", async t => {
  const x = setup();
  t.after(x.close);
  const now = Date.now();
  const msg = div(x.document, "msg");
  const old = div(x.document, "msg-assistant-body", "旧段");
  old.dataset.stageTimestamp = new Date(now - 125000).toISOString();
  const waiting = div(x.document, "tool-block is-waiting", "待确认");
  waiting.dataset.stageTimestamp = new Date(now - 120000).toISOString();
  const tail = div(x.document, "msg-assistant-body", "尾");
  tail.dataset.stageTimestamp = new Date(now - 120000).toISOString();
  msg.append(old, waiting, tail);
  x.chatInner.appendChild(msg);
  x.App.state.streaming = true;
  x.App.state.streamingSession = "s";
  x.App.state.activeStream = { messageNode: msg };
  x.App.roundFold.sync(msg);
  const header = msg.querySelector(".round-fold-summary");
  assert.equal(header.classList.contains("is-running"), false);
  assert.equal(header.classList.contains("is-waiting"), true);
  assert.equal(header.classList.contains("is-open"), true);
  const frozen = header.querySelector(".round-fold-duration").textContent;
  x.App.roundFold.tick();
  assert.equal(header.querySelector(".round-fold-duration").textContent, frozen, "待确认冻结");
});

test("非直播容器不跟跳（历史折叠只看 DOM 进行态）", async t => {
  const x = setup();
  t.after(x.close);
  const now = Date.now();
  const hist = div(x.document, "msg");
  hist.dataset.round = "1";
  const old = div(x.document, "msg-assistant-body", "旧段");
  old.dataset.stageTimestamp = new Date(now - 125000).toISOString();
  const tail = div(x.document, "msg-assistant-body", "尾");
  tail.dataset.stageTimestamp = new Date(now - 120000).toISOString();
  hist.append(old, tail);
  const live = div(x.document, "msg");
  live.append(div(x.document, "msg-assistant-body", "直播正文"));
  x.chatInner.append(hist, live);
  // 直播的是另一个容器
  x.App.state.streaming = true;
  x.App.state.streamingSession = "s";
  x.App.state.activeStream = { messageNode: live };
  x.App.roundFold.sync(hist);
  const header = hist.querySelector(".round-fold-summary");
  assert.equal(header.classList.contains("is-running"), false, "历史折叠不亮灯");
  x.App.roundFold.tick();
  assert.equal(header.querySelector(".round-fold-duration").textContent, "用时 5s");
});

test("尾上时间戳永不折叠（非紧贴的照常收纳）", async t => {
  const x = setup();
  t.after(x.close);
  const msg = div(x.document, "msg");
  const timeA = div(x.document, "msg-time stage-time", "10:00");
  const think1 = div(x.document, "think-block", "思考一");
  const think2 = div(x.document, "think-block", "思考二");
  const timeB = div(x.document, "msg-time stage-time", "10:01");
  const tail = div(x.document, "msg-assistant-body", "尾正文");
  msg.append(timeA, think1, think2, timeB, tail);
  x.chatInner.appendChild(msg);
  x.App.roundFold.sync(msg);
  assert.ok(msg.querySelector(".round-fold-summary"));
  assert.equal(timeA.classList.contains("is-roundfold-hidden"), true, "非紧贴时间照常收纳");
  assert.equal(think1.classList.contains("is-roundfold-hidden"), true);
  assert.equal(think2.classList.contains("is-roundfold-hidden"), true);
  assert.equal(timeB.classList.contains("is-roundfold-hidden"), false, "尾上时间戳可见");
  assert.equal(tail.classList.contains("is-roundfold-hidden"), false);
  // 只有时间戳在尾前：不成折叠，两者都可见
  const solo = div(x.document, "msg");
  const onlyTime = div(x.document, "msg-time stage-time", "10:02");
  const soloTail = div(x.document, "msg-assistant-body", "尾");
  solo.append(onlyTime, soloTail);
  x.chatInner.appendChild(solo);
  x.App.roundFold.sync(solo);
  assert.equal(solo.querySelector(".round-fold-summary"), null);
  assert.equal(onlyTime.classList.contains("is-roundfold-hidden"), false);
});

test("暂停间隙从用时中扣除（秒表语义），开口间隙忽略", async t => {
  const x = setup();
  t.after(x.close);
  const now = Date.now();
  const t0 = now - 125000, t1 = now - 5000;
  const msg = div(x.document, "msg");
  const old = div(x.document, "msg-assistant-body", "旧段");
  old.dataset.stageTimestamp = new Date(t0).toISOString();
  const tail = div(x.document, "msg-assistant-body", "尾");
  tail.dataset.stageTimestamp = new Date(t1).toISOString();
  msg.append(old, tail);
  x.chatInner.appendChild(msg);
  // 闭合间隙 60s 落在跨度内：120s - 60s = 60s
  msg._foldPauseGaps = [{ pauseAt: t0 + 30000, resumeAt: t0 + 90000 }];
  x.App.roundFold.sync(msg);
  assert.equal(msg.querySelector(".round-fold-duration").textContent, "用时 1m");
  // 开口间隙（正常结束/永不续跑）不扣
  msg._foldPauseGaps = [{ pauseAt: t0 + 30000, resumeAt: 0 }];
  x.App.roundFold.sync(msg);
  assert.equal(msg.querySelector(".round-fold-duration").textContent, "用时 2m");
  // 跳动计时同样扣减
  x.App.state.streaming = true;
  x.App.state.streamingSession = "s";
  x.App.state.activeStream = { messageNode: msg };
  msg._foldPauseGaps = [{ pauseAt: t0 + 30000, resumeAt: t0 + 90000 }];
  x.App.roundFold.sync(msg);
  x.App.roundFold.tick();
  assert.equal(msg.querySelector(".round-fold-duration").textContent, "用时 1m 5s");
});

test("recordPauseGap/closePauseGap：后进先闭合，无间隙不抛错", async t => {
  const x = setup();
  t.after(x.close);
  const msg = div(x.document, "msg");
  x.chatInner.appendChild(msg);
  x.App.roundFold.closePauseGap(msg);
  x.App.roundFold.closePauseGap(null);
  assert.equal(msg._foldPauseGaps, undefined);
  x.App.roundFold.recordPauseGap(msg);
  x.App.roundFold.recordPauseGap(msg);
  assert.equal(msg._foldPauseGaps.length, 2);
  x.App.roundFold.closePauseGap(msg);
  assert.ok(msg._foldPauseGaps[1].resumeAt > 0);
  assert.equal(msg._foldPauseGaps[0].resumeAt, 0);
});

test("附接重建后计时从轮次开始时间起算（不从刷新时间重计）", async t => {
  const x = setup();
  t.after(x.close);
  const now = Date.now();
  const msg = div(x.document, "msg");
  // 回放重建的节点时间戳都是“现在”
  const old = div(x.document, "msg-assistant-body", "旧段");
  old.dataset.stageTimestamp = new Date(now - 2000).toISOString();
  const tail = div(x.document, "msg-assistant-body msg-cursor", "尾");
  tail.dataset.stageTimestamp = new Date(now - 1000).toISOString();
  msg.append(old, tail);
  x.chatInner.appendChild(msg);
  // marker 下发的真实轮次开始（10 分钟前）
  msg._foldRoundStartedAt = now - 600000;
  x.App.state.streaming = true;
  x.App.state.streamingSession = "s";
  x.App.state.activeStream = { messageNode: msg };
  x.App.roundFold.sync(msg);
  x.App.roundFold.tick();
  assert.match(
    msg.querySelector(".round-fold-duration").textContent, /^用时 10m/,
    "应用时应从轮次开始计时，而非刷新时间"
  );
});

test("轮次开始下限：未来值/非法值被忽略", async t => {
  const x = setup();
  t.after(x.close);
  const now = Date.now();
  function livePair(floor) {
    const msg = div(x.document, "msg");
    const old = div(x.document, "msg-assistant-body", "旧段");
    old.dataset.stageTimestamp = new Date(now - 5000).toISOString();
    const tail = div(x.document, "msg-assistant-body", "尾");
    tail.dataset.stageTimestamp = new Date(now - 1000).toISOString();
    msg.append(old, tail);
    x.chatInner.appendChild(msg);
    msg._foldRoundStartedAt = floor;
    return msg;
  }
  x.App.state.streaming = true;
  x.App.state.streamingSession = "s";
  const future = livePair(now + 60000);
  x.App.state.activeStream = { messageNode: future };
  x.App.roundFold.sync(future);
  assert.equal(future.querySelector(".round-fold-duration").textContent, "用时 4s");
  const bad = livePair("not-a-number");
  x.App.state.activeStream = { messageNode: bad };
  x.App.roundFold.sync(bad);
  assert.equal(bad.querySelector(".round-fold-duration").textContent, "用时 4s");
});

test("非直播折叠不受轮次开始下限影响", async t => {
  const x = setup();
  t.after(x.close);
  const now = Date.now();
  const hist = div(x.document, "msg");
  hist.dataset.round = "1";
  const old = div(x.document, "msg-assistant-body", "旧段");
  old.dataset.stageTimestamp = new Date(now - 5000).toISOString();
  const tail = div(x.document, "msg-assistant-body", "尾");
  tail.dataset.stageTimestamp = new Date(now - 1000).toISOString();
  hist.append(old, tail);
  hist._foldRoundStartedAt = now - 600000;
  const live = div(x.document, "msg");
  live.append(div(x.document, "msg-assistant-body", "直播正文"));
  x.chatInner.append(hist, live);
  // 直播的是另一个容器
  x.App.state.streaming = true;
  x.App.state.streamingSession = "s";
  x.App.state.activeStream = { messageNode: live };
  x.App.roundFold.sync(hist);
  x.App.roundFold.tick();
  assert.equal(hist.querySelector(".round-fold-duration").textContent, "用时 4s");
});

test("流结束后直播下限缓存延续（完成瞬间不缩水）", async t => {
  const x = setup();
  t.after(x.close);
  const now = Date.now();
  const msg = div(x.document, "msg");
  const old = div(x.document, "msg-assistant-body", "旧段");
  old.dataset.stageTimestamp = new Date(now - 2000).toISOString();
  const tail = div(x.document, "msg-assistant-body", "尾");
  tail.dataset.stageTimestamp = new Date(now - 1000).toISOString();
  msg.append(old, tail);
  x.chatInner.appendChild(msg);
  msg._foldRoundStartedAt = now - 600000;
  x.App.state.streaming = true;
  x.App.state.streamingSession = "s";
  x.App.state.activeStream = { messageNode: msg };
  x.App.roundFold.sync(msg);
  x.App.roundFold.tick();
  assert.match(msg.querySelector(".round-fold-duration").textContent, /^用时 10m/);
  // 流结束：live 判定失效，但缓存下限延续，跨度不缩水到 1s
  x.App.state.streaming = false;
  x.App.state.activeStream = null;
  x.App.roundFold.sync(msg);
  assert.match(
    msg.querySelector(".round-fold-duration").textContent, /^用时 9m/,
    "完成瞬间应用时仍从轮次开始起算"
  );
});

test("历史折叠用用户发送时间兜底起点", async t => {
  const x = setup();
  t.after(x.close);
  const now = Date.now();
  const user = div(x.document, "msg msg-user");
  user.dataset.round = "9";
  user.dataset.stageTimestamp = new Date(now - 125000).toISOString();
  const old = div(x.document, "msg-assistant-body", "旧段");
  old.dataset.stageTimestamp = new Date(now - 5000).toISOString();
  const tail = div(x.document, "msg-assistant-body", "尾");
  tail.dataset.stageTimestamp = new Date(now - 1000).toISOString();
  x.chatInner.append(user, old, tail);
  x.App.roundFold.sync(x.chatInner);
  assert.equal(
    x.chatInner.querySelector(".round-fold-duration").textContent, "用时 2m 4s",
    "发送→首输出的间隙应计入（此前只算成员跨度 4s）"
  );
});

test("重发旧轮用户时间戳过早时不拉长用时", async t => {
  const x = setup();
  t.after(x.close);
  const now = Date.now();
  const user = div(x.document, "msg msg-user");
  user.dataset.round = "9";
  user.dataset.stageTimestamp = new Date(now - 7200000).toISOString();
  const old = div(x.document, "msg-assistant-body", "旧段");
  old.dataset.stageTimestamp = new Date(now - 5000).toISOString();
  const tail = div(x.document, "msg-assistant-body", "尾");
  tail.dataset.stageTimestamp = new Date(now - 1000).toISOString();
  x.chatInner.append(user, old, tail);
  x.App.roundFold.sync(x.chatInner);
  assert.equal(
    x.chatInner.querySelector(".round-fold-duration").textContent, "用时 4s",
    "超过 1 小时视为重发旧轮，退回纯成员口径"
  );
});

test("直播计时行：不计入成员，折叠接管后移除", async t => {
  const x = setup();
  t.after(x.close);
  const msg = liveMsg(x, 7);
  const timer = div(x.document, "live-round-timer", "用时 3s");
  msg.insertBefore(timer, msg.firstChild);
  x.App.roundFold.sync(msg);
  const head = msg.querySelector(".round-fold-summary");
  assert.ok(head, "有折叠头");
  assert.equal(msg.querySelector(".live-round-timer"), null, "计时行被接管移除");
  const fold = head._roundFold;
  assert.ok(fold.members.every(n => !n.classList.contains("live-round-timer")), "成员不含计时行");
});

test("直播计时行：单小块不成折叠时保留", async t => {
  const x = setup();
  t.after(x.close);
  const msg = div(x.document, "msg");
  msg.dataset.round = "8";
  msg.append(div(x.document, "tool-block", "唯一工具"));
  const timer = div(x.document, "live-round-timer", "用时 2s");
  msg.insertBefore(timer, msg.firstChild);
  x.chatInner.appendChild(msg);
  x.App.roundFold.sync(msg);
  assert.equal(msg.querySelector(".round-fold-summary"), null, "单小块不成折叠");
  assert.ok(msg.querySelector(".live-round-timer"), "计时行保留");
});

test("roundFold 导出 fmtDuration（供直播计时行复用）", async t => {
  const x = setup();
  t.after(x.close);
  assert.equal(typeof x.App.roundFold.fmtDuration, "function");
  assert.equal(x.App.roundFold.fmtDuration(5000), "5s");
  assert.equal(x.App.roundFold.fmtDuration(65000), "1m 5s");
});
