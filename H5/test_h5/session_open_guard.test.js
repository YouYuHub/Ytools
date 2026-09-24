"use strict";
/**
 * 侧边栏「重复点击已打开会话」跳过重载回归（sessions.js + session_list_utils.js）
 * 需求：已打开的当前会话再次点击其侧边栏行时不再重新加载（避免无谓请求与
 * 聊天区重建）；点击其他会话照常切换。
 * 覆盖：
 * - 首次点击（无就绪标记）→ 正常打开
 * - 打开完成后重复点击同一会话 → 跳过（openSession 未被调用）
 * - 加载中重复点击 → 跳过（防并发重复加载）
 * - ensureSessionId 分配后（新会话首条消息）→ 视为就绪，点击跳过
 * - startNewChat → 清除就绪标记，再点旧会话可正常打开
 * - markSessionViewReady（本地导入预览）→ 就绪后点击跳过
 * - 显式 App.openSession 不受守卫影响（删除轮次重载等路径仍真实重载）
 * 说明：用最小 DOM 桩加载 sessions.js，通过行内 select 按钮的 click 监听
 * 观察 openSession 是否被触发（以 App.stopVoiceInput 调用计数为探针——
 * openSession 每次必调且 startNewChat 不调用）。
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
    has: function (c) { return set.has(c); },
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

function makeNode(tag, cls, text) {
  const listeners = {};
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
    isConnected: true,
    listeners: listeners,
    appendChild: function (child) {
      node.children.push(child);
      child.parentNode = node;
      return child;
    },
    addEventListener: function (type, fn) {
      (listeners[type] = listeners[type] || []).push(fn);
    },
    removeEventListener: function () {},
    setAttribute: function () {},
    remove: function () {},
    focus: function () {},
    select: function () {},
    querySelector: function (sel) { return findOne(node, sel); },
    querySelectorAll: function () { return []; },
    closest: function () { return null; },
    emit: function (type, evt) {
      (listeners[type] || []).forEach(function (fn) { fn(evt || { preventDefault: function () {}, stopPropagation: function () {} }); });
    },
  };
  return node;
}

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
  return String(node.tagName || "").toLowerCase() === sel.toLowerCase();
}

// ---------- 加载被测模块 ----------
function loadSessionsModule() {
  global.window = { App: {}, CSS: undefined };
  global.document = {
    addEventListener: function () {},
    createElement: function (tag) { return makeNode(tag); },
    querySelector: function () { return null; },
    querySelectorAll: function () { return []; },
    body: makeNode("body"),
  };
  global.requestAnimationFrame = function (cb) { return setTimeout(function () { cb(Date.now()); }, 16); };
  global.cancelAnimationFrame = function (id) { clearTimeout(id); };
  global.SessionUtils = require("../js/session_utils.js");
  global.SessionListUtils = require("../js/session_list_utils.js");
  global.HistoryParser = { parseHistory: function () { return { records: [], metaUsage: {} }; } };

  const spies = { stopVoiceInput: 0, loadWorkDir: 0 };
  global.API = {
    fetchSessionFile: function () { return Promise.resolve(""); },
    getSessionMeta: function () { return Promise.resolve({}); },
    streamStatus: function () { return Promise.resolve({ running: false }); },
  };

  const app = global.window.App;
  app.state = { sessionId: "", sessionRecency: {} };
  app.$ = function () { return makeNode("div"); };
  app.el = function (tag, cls, text) { return makeNode(tag, cls, text); };
  app.toast = function () {};
  app.sessionList = makeNode("div", "session-list");
  app.input = makeNode("textarea");
  app.deleteModal = makeNode("div");
  app.deleteCancel = makeNode("button");
  app.deleteConfirm = makeNode("button");
  app.enhancePanel = makeNode("div", "enhance-panel hidden");
  app.chatInner = makeNode("div", "chat-inner");

  // openSession 依赖的运行期方法（探针：stopVoiceInput 每次 openSession 必调）
  app.saveSessionDraft = function () {};
  app.stopVoiceInput = function () { spies.stopVoiceInput += 1; };
  app.loadWorkDir = function () { spies.loadWorkDir += 1; };
  app.loadToolSelection = function () {};
  app.openModelPanel = function () {};
  app.restoreSessionDraft = function () {};
  app.resetContextTokenStats = function () {};
  app.refreshChatModelLabel = function () {};
  app.updateExportButton = function () {};
  app.setSessionTotalTokens = function () {};
  app.renderTodoWidget = function () {};
  app.applySessionTodo = function () {};
  app.loadSessionFiles = function () {};
  app.refreshComposerButtons = function () {};
  app.renderPendingOutbox = function () {};
  app.setSessionUsage = function () {};
  app.refreshContextTokenStats = function () {};
  app.renderRecords = function () {};
  app.rebuildQnav = function () {};
  app.maybeAttachRunningStream = function () {};
  app.setEmpty = function () {};
  app.isMobile = function () { return false; };
  app.setSidebarCollapsed = function () {};
  app.scrollToBottom = function () {};

  // 每次加载都清掉模块缓存（IIFE 直接写 window.App，缓存复用会拿到旧实例）
  const target = path.join(__dirname, "..", "js", "app", "sessions.js");
  delete require.cache[require.resolve(target)];
  // eslint-disable-next-line global-require
  require(target);
  return { app: app, spies: spies };
}

// 取会话行的标题按钮（点击目标）
function selectButtonOf(item) {
  return item.children.find(function (child) { return matchesClass(child, "session-select"); });
}

test("点击会话行：首次点击（无就绪标记）正常打开", function () {
  const env = loadSessionsModule();
  const item = env.app.buildSessionItem("s-1", "标题");
  selectButtonOf(item).emit("click");
  assert.equal(env.spies.stopVoiceInput, 1);
});

test("已打开的当前会话：重复点击跳过，不再重载", async function () {
  const env = loadSessionsModule();
  await env.app.openSession("s-1");
  assert.equal(env.spies.stopVoiceInput, 1);

  const item = env.app.buildSessionItem("s-1", "标题");
  selectButtonOf(item).emit("click");
  selectButtonOf(item).emit("click");
  assert.equal(env.spies.stopVoiceInput, 1, "就绪会话重复点击不应触发 openSession");

  // 点击其他会话照常切换
  const other = env.app.buildSessionItem("s-2", "另一个");
  selectButtonOf(other).emit("click");
  assert.equal(env.spies.stopVoiceInput, 2);
});

test("加载中重复点击跳过；完成后切换行为正确", async function () {
  const env = loadSessionsModule();
  const pending = env.app.openSession("s-1"); // 不 await：处于加载中
  assert.equal(env.spies.stopVoiceInput, 1);

  const item = env.app.buildSessionItem("s-1", "标题");
  selectButtonOf(item).emit("click");
  assert.equal(env.spies.stopVoiceInput, 1, "加载中重复点击不应并发重载");

  await pending;
  selectButtonOf(item).emit("click");
  assert.equal(env.spies.stopVoiceInput, 1, "加载完成后重复点击同样跳过");
});

test("新会话首条消息（ensureSessionId）后视为就绪，点击跳过", function () {
  const env = loadSessionsModule();
  const id = env.app.ensureSessionId();
  assert.ok(id, "应生成会话 ID");
  assert.equal(env.app.state.sessionId, id);

  const item = env.app.buildSessionItem(id, "占位标题");
  selectButtonOf(item).emit("click");
  assert.equal(env.spies.stopVoiceInput, 0, "刚发送的新会话不应因点击重载");
});

test("startNewChat 清除就绪标记：再点旧会话可正常打开", async function () {
  const env = loadSessionsModule();
  await env.app.openSession("s-1");
  assert.equal(env.spies.stopVoiceInput, 1);

  env.app.startNewChat();
  const item = env.app.buildSessionItem("s-1", "标题");
  selectButtonOf(item).emit("click");
  assert.equal(env.spies.stopVoiceInput, 2, "新对话后点击旧会话应重新打开");
});

test("markSessionViewReady（本地导入预览）后就绪，点击跳过", function () {
  const env = loadSessionsModule();
  env.app.markSessionViewReady("s-9");

  const item = env.app.buildSessionItem("s-9", "导入预览");
  selectButtonOf(item).emit("click");
  assert.equal(env.spies.stopVoiceInput, 0, "预览已铺开内容时点击不应重载");

  const other = env.app.buildSessionItem("s-10", "其他会话");
  selectButtonOf(other).emit("click");
  assert.equal(env.spies.stopVoiceInput, 1, "其他会话仍应正常打开");
});

test("显式 App.openSession 不受守卫影响（删除轮次/流失败重载仍真实执行）", async function () {
  const env = loadSessionsModule();
  await env.app.openSession("s-1");
  await env.app.openSession("s-1");
  assert.equal(env.spies.stopVoiceInput, 2, "显式重载必须每次都执行");
});

test("加载失败的会话不置就绪：允许再次点击重试", async function () {
  const env = loadSessionsModule();
  global.API.fetchSessionFile = function () { return Promise.reject(new Error("加载会话失败")); };
  await env.app.openSession("s-1");
  assert.equal(env.spies.stopVoiceInput, 1);

  const item = env.app.buildSessionItem("s-1", "标题");
  selectButtonOf(item).emit("click");
  assert.equal(env.spies.stopVoiceInput, 2, "失败后再次点击应重试加载");
});

test("过期失败响应不清掉新加载的就绪态", async function () {
  const env = loadSessionsModule();
  let rejectA = null;
  global.API.fetchSessionFile = function (id) {
    if (id === "s-a") return new Promise(function (_resolve, reject) { rejectA = reject; });
    return Promise.resolve("");
  };
  const pendingA = env.app.openSession("s-a");   // 挂起：模拟慢请求
  await env.app.openSession("s-b");              // 后发先至：b 完成并置就绪
  assert.equal(env.spies.stopVoiceInput, 2);

  rejectA(new Error("加载会话失败"));
  await pendingA; // 过期请求失败返回（seq 不匹配）

  const itemB = env.app.buildSessionItem("s-b", "B");
  selectButtonOf(itemB).emit("click");
  assert.equal(env.spies.stopVoiceInput, 2, "b 的就绪态应保持，点击仍跳过");
});
