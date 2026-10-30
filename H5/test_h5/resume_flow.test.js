"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { JSDOM } = require("jsdom");
const historyParser = require("../js/history_parser.js");

test("parseHistory: _internal 用户事件不渲染提问气泡", () => {
  const line = JSON.stringify({
    event: "chat_round", question: "", started_at: "2026-10-29 12:00:00",
    events: [{ role: "user", content: "上次任务被中断了，请继续。", _internal: true }],
    status: "running",
  });
  const parsed = historyParser.parseHistory(line + "\n");
  assert.deepEqual(parsed.records.filter(r => r.kind === "user"), []);
});

function composerContext(state) {
  function btn() {
    const classes = new Set(["hidden"]);
    return { classList: { toggle(name, force) { if (force) classes.add(name); else classes.delete(name); }, contains(name) { return classes.has(name); } } };
  }
  const context = {
    state, input: { value: "" }, composer: { classList: { toggle() {} } },
    sendBtn: btn(), voiceBtn: btn(), resumeBtn: btn(), scrollBottomBtn: { classList: { toggle() {} } },
    stopGroup: btn(), stopMenuBtn: btn(), queueMenu: { classList: { add() {} } },
    App: {}, requestAnimationFrame() {}, updateScrollBottomOffset() {},
  };
  const source = fs.readFileSync(path.join(__dirname, "../js/app/composer.js"), "utf8").replace(/\r\n/g, "\n");
  const start = source.indexOf("  function refreshComposerButtons() {");
  const end = source.indexOf("\n  }\n", start) + 4;
  context.__box = {};
  vm.runInNewContext(source.slice(start, end) + "\n__box.fn = refreshComposerButtons;", context);
  context.refreshComposerButtons = context.__box.fn;
  return context;
}

test("refreshComposerButtons: 空闲可继续且无文本时显示 ▶", () => {
  const context = composerContext({ sessionId: "s", streaming: false, streamingSession: null, pendingMedia: [], resumeAvailable: true, resumeSessionId: "s" });
  context.refreshComposerButtons();
  assert.equal(context.resumeBtn.classList.contains("hidden"), false);
});

test("refreshComposerButtons: 有文本/流式中/他会话可继续时隐藏 ▶", () => {
  let context = composerContext({ sessionId: "s", streaming: false, streamingSession: null, pendingMedia: [], resumeAvailable: true, resumeSessionId: "s" });
  context.input.value = "hi";
  context.refreshComposerButtons();
  assert.equal(context.resumeBtn.classList.contains("hidden"), true);
  context = composerContext({ sessionId: "s", streaming: true, streamingSession: "s", pendingMedia: [], resumeAvailable: false, resumeSessionId: null });
  context.refreshComposerButtons();
  assert.equal(context.resumeBtn.classList.contains("hidden"), true);
  context = composerContext({ sessionId: "s", streaming: false, streamingSession: null, pendingMedia: [], resumeAvailable: true, resumeSessionId: "other" });
  context.refreshComposerButtons();
  assert.equal(context.resumeBtn.classList.contains("hidden"), true);
  context = composerContext({ sessionId: "s", streaming: false, streamingSession: null, pendingMedia: [], resumeAvailable: false, resumeSessionId: null });
  context.refreshComposerButtons();
  assert.equal(context.resumeBtn.classList.contains("hidden"), true);
});

function resumeContext({ resumeImpl, uncertain = false, refreshImpl } = {}) {
  const dom = new JSDOM("<div></div>");
  const document = dom.window.document;
  const calls = { resumeChat: [], avail: [], refreshed: 0, flushed: 0, toasts: [] };
  const chatInner = document.createElement("div");
  const state = {
    sessionId: "s", streaming: false, streamingSession: null, abort: null,
    selectedTools: new Set(["read_file"]), manualCompactRunning: false,
    resumeAvailable: true, resumeSessionId: "s", activeStream: null,
  };
  const context = {
    state,
    el(tag, cls, text) { const n = document.createElement(tag); if (cls) n.className = cls; if (text) n.textContent = text; return n; },
    toast(m) { calls.toasts.push(m); },
    chatInner,
    scrollToBottom() {}, setEmpty() {},
    SessionUtils: { sanitizeSessionId: sid => sid },
    API: {
      resumeChat: resumeImpl || (async payload => { calls.resumeChat.push(payload); return { completed: true }; }),
      streamStatus: async () => ({ running: false, can_resume: false }),
    },
    App: {
      refreshComposerButtons() {}, refreshContextTokenStats() {},
      refreshSessionUsage: async () => true, addSessionUsage() {}, clearContextStatsTimer() {},
      scheduleContextTokenStatsRefresh() {}, refreshAskBlockStates() {},
      flushPendingMessages() { calls.flushed++; },
      setResumeAvailable(sid, ok) { calls.avail.push([sid, ok]); state.resumeAvailable = ok; state.resumeSessionId = ok ? sid : null; },
      refreshResumeAvailability: refreshImpl || (async sid => { calls.refreshed++; return false; }),
      getChatModelVision: () => true,
      taskStatus: { begin: () => ({}), connected() {}, end: async () => true },
      CONTEXT_STATS_EVENT_DEBOUNCE_MS: 300, openSession() {},
    },
    createStreamPipeline: () => ({ handle() {}, finish: () => ({ empty: false }) }),
    detachAttachedStream() {}, ensureSendable: async () => !uncertain,
    streamErrorText: err => String((err && err.message) || err),
    reconcileStreamInterruption: async () => {},
    setTimeout: () => 0, AbortController: dom.window.AbortController,
  };
  const source = fs.readFileSync(path.join(__dirname, "../js/app/chat.js"), "utf8");
  const start = source.indexOf("  async function resume() {");
  const end = source.indexOf("\n  sendBtn.addEventListener(", start);
  context.__box = {};
  vm.runInNewContext(source.slice(start, end) + "\n__box.fn = resume;", context);
  return { calls, state, chatInner, document, resume: context.__box.fn, close: () => dom.window.close() };
}

test("resume: 无用户气泡、只带 session_id/tool_names 调 /resume_chat", async t => {
  const x = resumeContext();
  t.after(x.close);
  await x.resume();
  assert.equal(x.calls.resumeChat.length, 1);
  // 跨 vm  realm 的对象原型不同：JSON 归一化后再断言结构
  assert.deepEqual(JSON.parse(JSON.stringify(x.calls.resumeChat[0])), { session_id: "s", tool_names: ["read_file"] });
  assert.equal(x.chatInner.querySelectorAll(".msg-user").length, 0);
  assert.equal(x.chatInner.querySelectorAll(".msg").length, 1);
  assert.equal(x.state.streaming, false);
  assert.equal(x.state.activeStream, null);
  assert.deepEqual(x.calls.avail[0], ["s", false]);
});

test("resume: 409 被拒后提示并重同步可用性", async t => {
  const x = resumeContext({
    resumeImpl: async payload => {
      const err = new Error("末轮已完成或无可继续的任务"); err.status = 409; throw err;
    },
  });
  t.after(x.close);
  await x.resume();
  const notice = x.chatInner.querySelector(".notice-bar");
  assert.match(notice.textContent, /无可继续/);
  assert.equal(x.calls.refreshed, 1);
  assert.equal(x.state.streaming, false);
});

test("stopBtn: 暂停成功后同步 ▶ 可用性", async () => {
  const calls = { avail: [] };
  const state = { sessionId: "s", activeStream: null, abort: null };
  const source = fs.readFileSync(path.join(__dirname, "../js/app/chat.js"), "utf8");
  const start = source.indexOf("  stopBtn.addEventListener(");
  const end = source.indexOf("\n  });", start) + 6;
  let handler;
  const context = {
    state, stopBtn: { addEventListener(name, fn) { handler = fn; } }, toast() {},
    App: {
      refreshComposerButtons() {},
      taskStatus: { stopping: () => ({}), stopped() {} },
      setResumeAvailable: (sid, ok) => calls.avail.push([sid, ok]),
    },
    API: { stopChat: async () => ({}) },
  };
  vm.runInNewContext(source.slice(start, end), context);
  await handler();
  assert.deepEqual(calls.avail, [["s", true]]);
});

test("stopBtn→中断收尾：停止确认后跳过重同步，不覆盖 ▶", async t => {
  const started = [];
  const x = resumeContext({
    resumeImpl: (payload, onEvent, signal) => {
      started.push(payload);
      return new Promise((_, reject) => {
        signal.addEventListener("abort", () => {
          const err = new Error("aborted"); err.name = "AbortError"; reject(err);
        });
      });
    },
    // 模拟后端 stopped 落盘窗口：中断收尾的查询返回不可继续
    refreshImpl: async sid => {
      x.calls.refreshed++;
      x.calls.avail.push([sid, false]);
      x.state.resumeAvailable = false;
      x.state.resumeSessionId = null;
      return false;
    },
  });
  t.after(x.close);
  const running = x.resume();
  while (started.length === 0) await new Promise(r => setImmediate(r));
  // 停止按钮切片：与续跑共享 state，模拟用户点击暂停
  const source = fs.readFileSync(path.join(__dirname, "../js/app/chat.js"), "utf8");
  const start = source.indexOf("  stopBtn.addEventListener(");
  const end = source.indexOf("\n  });", start) + 6;
  let stopHandler;
  vm.runInNewContext(source.slice(start, end), {
    state: x.state,
    stopBtn: { addEventListener(name, fn) { stopHandler = fn; } },
    toast: m => x.calls.toasts.push(m),
    App: {
      refreshComposerButtons() {},
      taskStatus: { stopping: () => ({}), stopped() {} },
      setResumeAvailable: (sid, ok) => {
        x.calls.avail.push([sid, ok]);
        x.state.resumeAvailable = ok;
        x.state.resumeSessionId = ok ? sid : null;
      },
    },
    API: { stopChat: async () => ({}) },
  });
  await stopHandler();
  await running;
  assert.equal(x.calls.refreshed, 0);
  assert.deepEqual(x.calls.avail[x.calls.avail.length - 1], ["s", true]);
  assert.equal(x.state.resumeAvailable, true);
});
