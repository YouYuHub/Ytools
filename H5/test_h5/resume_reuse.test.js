"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { stripTypeScriptTypes } = require("node:module");
const { JSDOM } = require("jsdom");

// chat.ts 已迁移 TS：读 .ts 源码剥离类型后切片（只取 findReusableLiveContainer）
const source = stripTypeScriptTypes(fs.readFileSync(path.join(__dirname, "../src/app/chat.ts"), "utf8"));

function setup() {
  const dom = new JSDOM("<div id=\"chatInner\"></div>", { runScripts: "outside-only" });
  const { window } = dom;
  const chatInner = window.document.querySelector("#chatInner");
  const context = { chatInner, window, document: window.document };
  vm.createContext(context);
  const start = source.indexOf("  function findReusableLiveContainer(");
  const end = source.indexOf("  async function resume()", start);
  assert.ok(start >= 0 && end > start, "切片锚点存在");
  vm.runInContext(source.slice(start, end), context);
  const div = cls => {
    const n = window.document.createElement("div");
    if (cls) n.className = cls;
    return n;
  };
  return { chatInner, find: context.findReusableLiveContainer, div, close: () => window.close() };
}

test("复用末尾仍挂载的回复容器", t => {
  const x = setup();
  t.after(x.close);
  const user = x.div("msg msg-user");
  const msg = x.div("msg");
  msg.dataset.round = "5";
  x.chatInner.append(user, msg);
  assert.equal(x.find(), msg);
});

test("末尾无回复容器 / 只有用户节点时不复用", t => {
  const x = setup();
  t.after(x.close);
  assert.equal(x.find(), null);
  x.chatInner.append(x.div("msg msg-user"));
  assert.equal(x.find(), null);
});

test("用户节点之后的旧容器不复用（末轮无直播残留）", t => {
  const x = setup();
  t.after(x.close);
  const oldMsg = x.div("msg");
  x.chatInner.append(oldMsg, x.div("msg msg-user"));
  assert.equal(x.find(), null);
});

test("跳过末尾非消息节点（加载条/压缩条等）", t => {
  const x = setup();
  t.after(x.close);
  const user = x.div("msg msg-user");
  const msg = x.div("msg");
  const tailBtn = x.div("qnav-load-older");
  x.chatInner.append(user, msg, tailBtn);
  assert.equal(x.find(), msg);
});
