"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { stripTypeScriptTypes } = require("node:module");
const { JSDOM } = require("jsdom");

// 回归：编辑重发/插入的屏幕手术（删旧轮节点/轮次重编）后必须全量同步折叠，
// 否则折叠头（无 data-round，不会被删除循环带走）变成点不开的孤儿，
// 且旧 key 与新编号错位（后续流式只同步直播容器，清扫不到 chatInner）。
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

// 执行 send() 内的手术切片（删除/重编/打标/同步），上下文按需供给闭包变量
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

function flatRound(x, round, { withFold = true } = {}) {
  const user = stamp(div(x.document, "msg msg-user"), round);
  const nodes = [user];
  if (withFold) {
    nodes.push(
      stamp(div(x.document, "think-block", "思考"), round),
      stamp(div(x.document, "tool-block", "工具"), round),
      stamp(div(x.document, "msg-assistant-body", "旧段"), round),
      stamp(div(x.document, "msg-assistant-body", "新段"), round),
      stamp(div(x.document, "round-usage", "用量"), round),
    );
  } else {
    nodes.push(stamp(div(x.document, "msg-assistant-body", "单段"), round));
  }
  x.chatInner.append(...nodes);
  return nodes;
}

test("原地重发：旧轮折叠头随成员一起消失，不留孤儿", async t => {
  const x = setup();
  t.after(x.close);
  flatRound(x, 1);
  flatRound(x, 2, { withFold: false });
  x.App.processGroups.sync(x.chatInner);
  const oldHeader = x.chatInner.querySelector(".round-fold-summary");
  assert.ok(oldHeader, "手术前旧轮有折叠头");
  runSurgery(x, { direct: { targetRound: 1 }, liveRound: 1 });
  assert.equal(oldHeader.isConnected, false, "旧折叠头必须被移除");
  const strays = Array.from(x.chatInner.querySelectorAll(".round-fold-summary"))
    .filter(h => !h._roundFold);
  assert.deepEqual(strays, [], "无认领的孤儿头");
  assert.equal(x.chatInner.querySelectorAll(".round-fold-summary").length, 0);
  // 新节点就位且打标正确
  assert.equal(x.chatInner.querySelectorAll(".msg-user").length, 2);
});

test("插入重答：轮次重编后折叠 key 跟着走，不错位", async t => {
  const x = setup();
  t.after(x.close);
  flatRound(x, 1, { withFold: false });
  flatRound(x, 2);
  x.App.processGroups.sync(x.chatInner);
  const header = x.chatInner.querySelector(".round-fold-summary");
  assert.ok(header);
  assert.equal(header._roundFold.key, "s:r2");
  runSurgery(x, { direct: { insertAfterRound: 1 }, liveRound: 2 });
  const headers = x.chatInner.querySelectorAll(".round-fold-summary");
  assert.equal(headers.length, 1);
  assert.equal(header.isConnected, false, "旧 key 的折叠已解散重建");
  assert.equal(headers[0]._roundFold.key, "s:r3", "重编后 key 跟随新轮次号");
  const strays = Array.from(headers).filter(h => !h._roundFold);
  assert.deepEqual(strays, [], "无认领的孤儿头");
});
