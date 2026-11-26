"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM } = require("jsdom");

// 回归：会话菜单「分组 → 新建分组并移入」后，分组栏须立即出现新分组。
// 旧缺陷：选择器路径只调 assign()（更新 assignments/rows），没把新分组补进
// 本地 groups 数组，而 render() 按 groups 建块 → 分组栏看不到，刷新才出现。
const SOURCE = fs.readFileSync(path.join(__dirname, "../js/app/session_groups.js"), "utf8");
const UTILS = require("../js/session_group_utils.js");

function setup(api) {
  const dom = new JSDOM(
    "<div id=\"sessionGroups\">" +
      "<div id=\"sessionGroupsHead\"><span id=\"sessionGroupsCount\"></span>" +
      "<button id=\"sessionGroupAddBtn\"></button></div>" +
      "<div id=\"sessionGroupsBody\"></div>" +
    "</div><div id=\"sessionList\"></div>",
    { runScripts: "outside-only", url: "http://localhost/" },
  );
  const { window } = dom;
  const { document } = window;
  const toasts = [];
  window.App = {
    state: { sessionId: "s" },
    sessionList: document.querySelector("#sessionList"),
    $: function (sel) { return document.querySelector(sel); },
    el: function (tag, cls, text) {
      const n = document.createElement(tag);
      if (cls) n.className = cls;
      if (text != null) n.textContent = text;
      return n;
    },
    toast: function (msg) { toasts.push(String(msg)); },
    buildSessionItem: function (id, title) {
      const item = document.createElement("div");
      item.className = "session-item";
      item.dataset.session = id;
      const name = document.createElement("span");
      name.className = "session-name";
      name.textContent = title || id;
      item.appendChild(name);
      return item;
    },
    bindTitleMarquee: function () {},
    refreshBulkGroupedView: function () {},
    stopTitleMarqueeIn: function () {},
    closeSessionMenus: function () {},
  };
  window.API = api;
  window.SessionGroupUtils = UTILS;
  window.eval(SOURCE);
  return {
    window, document, toasts,
    App: window.App,
    body: document.querySelector("#sessionGroupsBody"),
    close: () => window.close(),
  };
}

function apiStub(overrides) {
  return Object.assign({
    listSessionGroups: async function () { return { groups: [], assignments: {} }; },
    createSessionGroup: async function () { throw new Error("unexpected create"); },
    assignSessionGroup: async function () { return {}; },
    updateSessionGroup: async function () { return {}; },
    deleteSessionGroup: async function () { return {}; },
  }, overrides || {});
}

async function settle(ticks) {
  for (let i = 0; i < (ticks || 20); i++) await Promise.resolve();
}

function clickPickerItem(menu, label, window) {
  const items = Array.from(menu.querySelectorAll(".session-picker-item"));
  const target = items.find(function (n) {
    const lab = n.querySelector(".menu-item-label");
    return lab && lab.textContent === label;
  });
  assert.ok(target, "应找到选择器项：" + label);
  target.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
}

async function typeAndEnter(editor, text, window) {
  editor.value = text;
  editor.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  await settle(30);
}

test("选择器新建分组：分组栏立即出现新分组并含成员", async t => {
  const created = { id: "g-new", name: "新分组" };
  const x = setup(apiStub({
    createSessionGroup: async function () { return { group: created }; },
  }));
  t.after(x.close);
  x.App.sessionGroups.render([{ id: "s1", title: "会话1" }]);
  assert.equal(x.body.querySelectorAll(".session-group").length, 0);

  const menu = x.document.createElement("div");
  const anchor = x.document.createElement("button");
  x.document.body.appendChild(menu);
  x.App.sessionGroups.openPicker("s1", menu, anchor);
  clickPickerItem(menu, "新建分组…", x.window);
  const editor = menu.querySelector(".session-group-name-editor");
  assert.ok(editor, "应出现名称输入框");
  await typeAndEnter(editor, "新分组", x.window);

  const blocks = x.body.querySelectorAll(".session-group");
  assert.equal(blocks.length, 1, "分组栏应立即出现新分组（无需刷新）");
  assert.equal(blocks[0].querySelector(".session-group-name").textContent, "新分组");
  assert.equal(blocks[0].querySelector(".session-group-count").textContent, "1");
  assert.equal(blocks[0].querySelector(".session-group-sessions .session-name").textContent, "会话1");
  assert.deepEqual(x.App.sessionGroups.getGroups().map(function (g) { return g.id; }), ["g-new"]);
  assert.equal(x.App.sessionGroups.getAssignment("s1"), "g-new");
});

test("同名幂等返回既有分组：不重复建块", async t => {
  const x = setup(apiStub({
    listSessionGroups: async function () {
      return { groups: [{ id: "g-old", name: "旧分组" }], assignments: {} };
    },
    createSessionGroup: async function () { return { group: { id: "g-old", name: "旧分组" } }; },
  }));
  t.after(x.close);
  x.App.sessionGroups.render([{ id: "s1", title: "会话1" }]);
  await x.App.sessionGroups.refresh();
  await settle();
  assert.equal(x.body.querySelectorAll(".session-group").length, 1);

  const menu = x.document.createElement("div");
  const anchor = x.document.createElement("button");
  x.document.body.appendChild(menu);
  x.App.sessionGroups.openPicker("s1", menu, anchor);
  clickPickerItem(menu, "新建分组…", x.window);
  await typeAndEnter(menu.querySelector(".session-group-name-editor"), "旧分组", x.window);

  assert.equal(x.body.querySelectorAll(".session-group").length, 1, "既有分组不重复建块");
  assert.equal(x.App.sessionGroups.getAssignment("s1"), "g-old", "会话仍正常归组");
});
