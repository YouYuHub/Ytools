"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { stripTypeScriptTypes } = require("node:module");
const { JSDOM } = require("jsdom");

// 回归：暂停后编辑重发（原地 regen），旧轮内容残留到新气泡上方。
// 根因链：doSend 预移除被编辑气泡 → send() 删除循环按 data-round 找不到锚点
// （暂停早于 round_started 补打标、续跑新容器等 msg 未打标场景）→ removed 永假
// → 清扫整体跳过 → 旧 msg（含 svg 等内容）残留，新节点追加在其后。
// 修复：doSend 改传引用（_regenDropNode），删除循环按引用匹配触发清扫。
const CHAT = stripTypeScriptTypes(fs.readFileSync(path.join(__dirname, "../src/app/chat.ts"), "utf8"));
const SWEEP_START = CHAT.indexOf("  if (direct && direct.targetRound) {");
const SWEEP_END = CHAT.indexOf("  // 原地重发/插入重答时视口停留在发起位置", SWEEP_START);

function setup() {
  const dom = new JSDOM("<div id=\"chatInner\"></div>", { runScripts: "outside-only" });
  const { window } = dom;
  const { document } = window;
  const chatInner = document.querySelector("#chatInner");
  window.FormatUtils = { fmtTime: ts => String(ts || "") };
  window.App = {
    state: { sessionId: "s" },
    chatInner,
    chatScroll: { style: {}, scrollTop: 0 },
    pauseAutoScroll() {},
    historyScroll: { refresh() {} },
    updateCodeblockCopyButtons() {},
    attachUserEditAction() {},
    el(tag, cls, text) {
      const n = document.createElement(tag);
      if (cls) n.className = cls;
      if (text != null) n.textContent = text;
      return n;
    },
  };
  window.eval(fs.readFileSync(path.join(__dirname, "../js/app/process_groups.js"), "utf8"));
  window.eval(fs.readFileSync(path.join(__dirname, "../js/app/round_fold.js"), "utf8"));
  return { window, document, chatInner, App: window.App, close: () => window.close() };
}

function div(doc, cls, text) {
  const n = doc.createElement("div");
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}

function stamp(node, round) {
  node.dataset.round = String(round);
  return node;
}

function mkUser(x, round, text) {
  const u = div(x.document, "msg msg-user");
  const b = div(x.document, "msg-bubble", text);
  u.appendChild(b);
  if (round != null) stamp(u, round);
  u._editData = { content: text, round: round, quotes: [] };
  return u;
}

// 执行 send() 内的手术切片（与 edit_resend_folds.test.js 同口径）
function runSurgery(x, { direct, liveRound }) {
  assert.ok(SWEEP_START >= 0 && SWEEP_END > SWEEP_START, "切片锚点存在");
  const userNode = div(x.document, "msg msg-user");
  const msg = div(x.document, "msg");
  const context = {
    direct, chatInner: x.chatInner, userNode, msg, liveRound,
    userContent: "新问题", requestQuotes: [], App: x.App,
  };
  vm.createContext(context);
  vm.runInContext("let insertedMidway = false;\n" + CHAT.slice(SWEEP_START, SWEEP_END), context);
  return { userNode, msg };
}

function flatRound(x, round) {
  const user = mkUser(x, round, "q" + round);
  const m = stamp(div(x.document, "msg"), round);
  m.appendChild(div(x.document, "msg-assistant-body", "a" + round));
  x.chatInner.append(user, m);
  return { user, msg: m };
}

test("暂停态未打标 msg：按引用触发清扫，旧内容不残留（症状1根因）", t => {
  const x = setup();
  t.after(x.close);
  flatRound(x, 1);
  flatRound(x, 2);
  // round3 暂停态：user 打标 + msg 未打标（含 svg 块，模拟续跑新容器/打标缺失）
  const u3 = mkUser(x, 3, "q3 orig");
  x.chatInner.appendChild(u3);
  const m3 = div(x.document, "msg");
  m3.appendChild(div(x.document, "think-block", "thinking..."));
  const body3 = div(x.document, "msg-assistant-body");
  const svg = div(x.document, "md-svg-block", "SVG-CONTENT");
  body3.appendChild(svg);
  m3.appendChild(body3);
  m3.appendChild(div(x.document, "round-usage", "usage r3"));
  x.chatInner.appendChild(m3);
  // 注意：不再预移除 u3（doSend 改传引用），手术切片按引用匹配并清扫
  runSurgery(x, { direct: { targetRound: 3, _regenDropNode: u3 }, liveRound: 3 });
  assert.equal(u3.isConnected, false, "被编辑气泡必须被移除");
  assert.equal(m3.isConnected, false, "未打标旧 msg 必须被清扫");
  assert.equal(x.chatInner.querySelector(".md-svg-block"), null, "旧 svg 不得残留");
  const users = Array.from(x.chatInner.querySelectorAll(".msg-user"));
  assert.equal(users.length, 3, "三轮各剩一个气泡");
  assert.equal(users[2].dataset.round, "3");
  assert.equal(users[2].dataset.replacesRound, "3", "新气泡打 replacesRound");
  // 新气泡之前不得有旧轮残留（顺序：r1 user/msg, r2 user/msg, 新 user/msg）
  const kinds = Array.from(x.chatInner.children).map(n => n.className + "#" + (n.dataset.round || "-"));
  assert.deepEqual(kinds, [
    "msg msg-user#1", "msg#1", "msg msg-user#2", "msg#2",
    "msg msg-user#3", "msg#3",
  ]);
});

test("已打标 msg：原有路径不受影响（回归保护）", t => {
  const x = setup();
  t.after(x.close);
  flatRound(x, 1);
  const u2 = mkUser(x, 2, "q2 orig");
  x.chatInner.appendChild(u2);
  const m2 = stamp(div(x.document, "msg"), 2);
  m2.appendChild(div(x.document, "msg-assistant-body", "a2"));
  x.chatInner.appendChild(m2);
  runSurgery(x, { direct: { targetRound: 2, _regenDropNode: u2 }, liveRound: 2 });
  assert.equal(u2.isConnected, false);
  assert.equal(m2.isConnected, false);
  assert.equal(x.chatInner.querySelectorAll(".msg-user").length, 2);
});

test("引用已游离：回退纯 data-round 匹配（队列重试等延迟发送）", t => {
  const x = setup();
  t.after(x.close);
  flatRound(x, 1);
  const u2 = mkUser(x, 2, "q2 orig");
  x.chatInner.appendChild(u2);
  const m2 = stamp(div(x.document, "msg"), 2);
  m2.appendChild(div(x.document, "msg-assistant-body", "a2"));
  x.chatInner.appendChild(m2);
  u2.remove(); // 模拟延迟发送前节点已游离
  runSurgery(x, { direct: { targetRound: 2, _regenDropNode: u2 }, liveRound: 2 });
  assert.equal(m2.isConnected, false, "游离引用不命中时仍按 data-round 清扫");
  assert.equal(x.chatInner.querySelectorAll(".msg-user").length, 2);
});

test("中轮重发：新节点插回原位，后续轮次不受影响", t => {
  const x = setup();
  t.after(x.close);
  flatRound(x, 1);
  const u2 = mkUser(x, 2, "q2 orig");
  x.chatInner.appendChild(u2);
  const m2 = stamp(div(x.document, "msg"), 2);
  m2.appendChild(div(x.document, "msg-assistant-body", "a2"));
  x.chatInner.appendChild(m2);
  flatRound(x, 3);
  runSurgery(x, { direct: { targetRound: 2, _regenDropNode: u2 }, liveRound: 2 });
  assert.equal(u2.isConnected, false);
  assert.equal(m2.isConnected, false);
  const kinds = Array.from(x.chatInner.children).map(n => n.className + "#" + (n.dataset.round || "-"));
  assert.deepEqual(kinds, [
    "msg msg-user#1", "msg#1", "msg msg-user#2", "msg#2",
    "msg msg-user#3", "msg#3",
  ]);
});
