"use strict";
/**
 * 标题 hover 跑马灯可复用化回归（sessions.js）
 * 覆盖：
 * - bindTitleMarquee 对任意行容器可用、同一容器只绑一次
 * - 溢出标题 hover 延迟后开始滚动（内层 .session-name-text 上写 transform）
 * - 未溢出标题不触发
 * - stopTitleMarqueeIn：容器整体重建前停止其内部的滚动
 * 说明：用最小 DOM 桩加载 sessions.js（模块只依赖 window.App 字段的注入，
 * 运行时 DOM 由桩对象提供），不引入 jsdom。
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

function makeStyle() {
  const style = {};
  style.removeProperty = function (prop) { delete style[prop]; };
  return style;
}

function makeName(opts) {
  const inner = { style: makeStyle() };
  const item = { dataset: { session: opts.session || "s-1" } };
  const name = {
    isConnected: true,
    scrollWidth: opts.scrollWidth,
    clientWidth: opts.clientWidth,
    classList: makeClassList(),
    style: makeStyle(),
    inner: inner,
    closest: function (sel) {
      if (sel === ".session-name") return name;
      if (sel === ".session-item") return item;
      return null;
    },
    querySelector: function (sel) {
      return sel === ".session-name-text" ? inner : null;
    },
  };
  return name;
}

function makeContainer(descendants) {
  const listeners = {};
  const el = {
    dataset: {},
    isConnected: true,
    addEventListener: function (type, fn) {
      (listeners[type] = listeners[type] || []).push(fn);
    },
    emit: function (type, target) {
      (listeners[type] || []).forEach(function (fn) { fn({ target: target }); });
    },
    listenerCount: function (type) { return (listeners[type] || []).length; },
    contains: function (node) {
      if (node === el) return true;
      return Boolean(descendants && descendants.indexOf(node) !== -1);
    },
  };
  return el;
}

// ---------- 加载被测模块 ----------
function loadSessionsModule(sessionListStub) {
  global.window = { App: {}, CSS: undefined };
  global.document = {
    addEventListener: function () {},
    querySelectorAll: function () { return []; },
    querySelector: function () { return null; },
    createElement: function () { return { style: {}, classList: makeClassList(), appendChild: function () {} }; },
    body: { appendChild: function () {} },
  };
  global.requestAnimationFrame = function (cb) { return setTimeout(function () { cb(Date.now()); }, 16); };
  global.cancelAnimationFrame = function (id) { clearTimeout(id); };

  const app = global.window.App;
  app.state = { sessionId: "", sessionRecency: {} };
  // 模块加载期会读取若干固定 id 的按钮并绑定监听：返回通用桩节点
  app.$ = function () {
    return {
      dataset: {},
      classList: makeClassList(),
      addEventListener: function () {},
      appendChild: function () {},
      querySelector: function () { return null; },
      querySelectorAll: function () { return []; },
    };
  };
  app.el = function (tag, cls, text) {
    return { tagName: tag, className: cls, textContent: text, style: makeStyle(), classList: makeClassList() };
  };
  app.toast = function () {};
  app.sessionList = sessionListStub;
  app.input = { value: "" };

  // 每次加载都清掉模块缓存：sessions.js 是 IIFE 直接写入 window.App，
  // 缓存复用会让后续用例拿到上一轮绑定的旧 App 实例
  const target = path.join(__dirname, "..", "js", "app", "sessions.js");
  delete require.cache[require.resolve(target)];
  // eslint-disable-next-line global-require
  require(target);
  return app;
}

const sleep = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };

// ---------- 用例 ----------
test("bindTitleMarquee: 同一容器重复绑定只注册一次监听", function () {
  const list = makeContainer([]);
  const app = loadSessionsModule(list);
  assert.equal(typeof app.bindTitleMarquee, "function");
  const extra = makeContainer([]);
  app.bindTitleMarquee(extra);
  app.bindTitleMarquee(extra);
  app.bindTitleMarquee(extra);
  assert.equal(extra.listenerCount("mouseover"), 1);
  assert.equal(extra.listenerCount("mouseout"), 1);
  assert.equal(extra.dataset.marqueeBound, "1");
});

test("bindTitleMarquee: 溢出标题 hover 250ms 后开始滚动", async function () {
  const list = makeContainer([]);
  const app = loadSessionsModule(list);
  const box = makeContainer([]);
  app.bindTitleMarquee(box);
  const name = makeName({ session: "s-a", scrollWidth: 220, clientWidth: 100 });

  box.emit("mouseover", name);
  assert.equal(name.classList.has("is-marquee"), false, "延迟期内不应立即滚动");
  await sleep(340);
  assert.equal(name.classList.has("is-marquee"), true, "hover 延迟后应进入滚动态");
  assert.match(String(name.inner.style.transform || ""), /^translateX\(-\d/, "内层文本应被左移");

  box.emit("mouseout", name);
  app.stopTitleMarqueeIn(box);
});

test("bindTitleMarquee: 未溢出标题不触发滚动", async function () {
  const list = makeContainer([]);
  const app = loadSessionsModule(list);
  const box = makeContainer([]);
  app.bindTitleMarquee(box);
  const name = makeName({ session: "s-b", scrollWidth: 102, clientWidth: 100 });

  box.emit("mouseover", name);
  await sleep(320);
  assert.equal(name.classList.has("is-marquee"), false);
  assert.equal(name.inner.style.transform, undefined);
});

test("stopTitleMarqueeIn: 祖先容器重建前停止其后代容器内的滚动", async function () {
  const list = makeContainer([]);
  const app = loadSessionsModule(list);
  const inner = makeContainer([]);
  const outer = makeContainer([inner]);
  app.bindTitleMarquee(inner);
  const name = makeName({ session: "s-c", scrollWidth: 260, clientWidth: 100 });

  inner.emit("mouseover", name);
  await sleep(340);
  assert.equal(name.classList.has("is-marquee"), true);

  app.stopTitleMarqueeIn(outer);
  assert.equal(name.classList.has("is-marquee"), false, "停止后应移除滚动类");
  assert.equal(name.inner.style.transform, undefined, "停止后应清除 transform");
});

test("stopTitleMarqueeIn: 无关容器不受影响", async function () {
  const list = makeContainer([]);
  const app = loadSessionsModule(list);
  const box = makeContainer([]);
  const other = makeContainer([]);
  app.bindTitleMarquee(box);
  const name = makeName({ session: "s-d", scrollWidth: 300, clientWidth: 100 });

  box.emit("mouseover", name);
  await sleep(340);
  assert.equal(name.classList.has("is-marquee"), true);

  app.stopTitleMarqueeIn(other);   // 另一个容器重建：不应波及当前滚动
  assert.equal(name.classList.has("is-marquee"), true);
  app.stopTitleMarqueeIn(box);
});
