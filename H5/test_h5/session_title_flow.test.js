"use strict";
/**
 * 发送消息时的侧边栏标题策略回归（sessions.js + session_list_utils.js）
 * 需求：发消息后标题不再被"本轮提问前 40 字"顶掉——一直使用模型生成/后端
 * 已有标题；仅全新会话（无任何已知标题）才用 40 字临时占位。
 * 覆盖：
 * - 全新会话首条消息 → 40 字占位
 * - 登记可信标题后再次发送 → 沿用旧标题（关键修复点）
 * - 重命名（opts.title）→ 立即生效且后续发送沿用
 * - 可信标题等于会话 ID（后端兜底值）→ 视为无标题，退回提问占位
 */
const { test } = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

// ---------- 最小 DOM 桩 ----------
function makeClassList() {
  const set = new Set();
  return {
    add: function (c) { set.add(c); },
    remove: function (c) { set.delete(c); },
    contains: function (c) { return set.has(c); },
    toggle: function (c, on) {
      const next = on === undefined ? !set.has(c) : Boolean(on);
      if (next) set.add(c); else set.delete(c);
      return next;
    },
  };
}

function matchesClass(node, token) {
  return String(node.className || "").split(/\s+/).indexOf(token) !== -1;
}

function makeEl(tag, cls, text) {
  const node = {
    tagName: tag,
    className: cls || "",
    textContent: text == null ? "" : String(text),
    dataset: {},
    style: {},
    classList: makeClassList(),
    children: [],
    parentNode: null,
    innerHTML: "",
    type: "",
    title: "",
    disabled: false,
    appendChild: function (child) {
      node.children.push(child);
      child.parentNode = node;
      return child;
    },
    addEventListener: function () {},
    removeEventListener: function () {},
    setAttribute: function () {},
    focus: function () {},
    select: function () {},
    querySelector: function (sel) {
      return findOne(node, sel);
    },
    querySelectorAll: function () { return []; },
  };
  return node;
}

// 支持 ".class" / "tag" / "[data-session=\"x\"]" 三种选择器（够本测试用）
function findOne(root, sel) {
  for (let i = 0; i < root.children.length; i++) {
    const child = root.children[i];
    if (matchSel(child, sel)) return child;
    const deep = findOne(child, sel);
    if (deep) return deep;
  }
  return null;
}

function matchSel(node, sel) {
  if (sel.charAt(0) === ".") return matchesClass(node, sel.slice(1));
  const m = /^\[data-session="(.*)"\]$/.exec(sel);
  if (m) return String(node.dataset.session || "") === m[1];
  return String(node.tagName || "").toLowerCase() === sel.toLowerCase();
}

function makeSessionList() {
  const list = makeEl("div", "session-list");
  list.querySelector = function (sel) {
    if (sel === ".empty-tip" || sel === ".session-label") return null;
    return findOne(list, sel);
  };
  list.insertBefore = function (node, ref) {
    if (!ref) {
      list.children.push(node);
    } else {
      const idx = list.children.indexOf(ref);
      if (idx === -1) list.children.push(node);
      else list.children.splice(idx, 0, node);
    }
    node.parentNode = list;
    return node;
  };
  Object.defineProperty(list, "firstChild", {
    get: function () { return list.children[0] || null; },
  });
  return list;
}

// ---------- 加载被测模块 ----------
function boot() {
  global.window = { App: {}, CSS: undefined };
  global.document = {
    addEventListener: function () {},
    createElement: function (tag) { return makeEl(tag); },
    querySelector: function () { return null; },
    querySelectorAll: function () { return []; },
    body: makeEl("body"),
  };
  global.requestAnimationFrame = function (cb) { return setTimeout(function () { cb(Date.now()); }, 16); };
  global.cancelAnimationFrame = function (id) { clearTimeout(id); };
  global.SessionListUtils = require("../js/session_list_utils.js");

  const app = global.window.App;
  app.state = { sessionId: "s-1", sessionRecency: {} };
  app.$ = function () { return makeEl("div"); };
  app.el = makeEl;
  app.toast = function () {};
  app.sessionList = makeSessionList();
  app.input = { value: "", focus: function () {}, scrollTop: 0 };

  const target = path.join(__dirname, "..", "js", "app", "sessions.js");
  delete require.cache[require.resolve(target)];
  require(target);
  return app;
}

function nameText(app, sessionId) {
  const item = app.sessionList.querySelector('[data-session="' + sessionId + '"]');
  if (!item) return null;
  const inner = item.querySelector(".session-name-text");
  return inner ? inner.textContent : null;
}

// ---------- 用例 ----------
test("全新会话首条消息：使用提问前 40 字临时占位", function () {
  const app = boot();
  app.upsertLocalSession("s-1", { userText: "第一条提问内容" });
  assert.equal(nameText(app, "s-1"), "第一条提问内容");

  const long = "\u4e00".repeat(60);
  app.upsertLocalSession("s-2", { userText: long });
  assert.equal(nameText(app, "s-2"), long.slice(0, 40));
});

test("已有可信标题时再次发送：沿用旧标题（不被本轮提问覆盖）", function () {
  const app = boot();
  app.upsertLocalSession("s-1", { userText: "第一条提问" });
  // 标题模型生成结果回填（chat.js applySessionTitle → rememberSessionTitle）
  app.rememberSessionTitle("s-1", "模型生成的标题");
  app.upsertLocalSession("s-1", { userText: "第二轮的新提问，不该成为标题" });
  assert.equal(nameText(app, "s-1"), "模型生成的标题");

  // 第三次发送仍然保持
  app.upsertLocalSession("s-1", { userText: "第三轮提问" });
  assert.equal(nameText(app, "s-1"), "模型生成的标题");
});

test("重命名（opts.title）：立即生效且后续发送沿用", function () {
  const app = boot();
  app.upsertLocalSession("s-1", { userText: "第一条提问" });
  app.upsertLocalSession("s-1", { title: "手动重命名标题" });
  assert.equal(nameText(app, "s-1"), "手动重命名标题");

  app.upsertLocalSession("s-1", { userText: "新的提问" });
  assert.equal(nameText(app, "s-1"), "手动重命名标题");
});

test("可信标题等于会话 ID（后端兜底值）：视为无标题，退回提问占位", function () {
  const app = boot();
  app.rememberSessionTitle("s-1", "s-1");
  app.upsertLocalSession("s-1", { userText: "真正的第一条提问" });
  assert.equal(nameText(app, "s-1"), "真正的第一条提问");
});

test("后端兜底标题 session_<id>：不登记，退回提问占位", function () {
  const app = boot();
  app.rememberSessionTitle("s-1", "session_s-1");
  app.upsertLocalSession("s-1", { userText: "第一条提问" });
  assert.equal(nameText(app, "s-1"), "第一条提问");
});

test("占位标题会登记：第二轮发送沿用第一轮的占位（本轮未生成则用之前的）", function () {
  const app = boot();
  app.upsertLocalSession("s-1", { userText: "第一轮提问内容" });
  assert.equal(nameText(app, "s-1"), "第一轮提问内容");
  // 标题模型尚未生成时，第二轮提问不应顶掉标题
  app.upsertLocalSession("s-1", { userText: "第二轮完全不同的新提问" });
  assert.equal(nameText(app, "s-1"), "第一轮提问内容");
});

test("多选模式：数据源标题同样沿用可信标题", function () {
  const app = boot();
  app.enterBulkMode();
  app.rememberSessionTitle("s-1", "模型标题");
  app.upsertLocalSession("s-1", { userText: "新提问" });
  // 多选分支不建行，但 sessionRecency 里记录的应是可信标题
  assert.equal(app.state.sessionRecency["s-1"].title, "模型标题");
  app.exitBulkMode();
});
