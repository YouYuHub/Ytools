"use strict";
// 主题化下拉：原生 select 增强为自绘 listbox，颜色走页面变量（强调色）。
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM } = require("jsdom");

const SOURCE = fs.readFileSync(path.join(__dirname, "../js/select_menu.js"), "utf8");
const CSS = fs.readFileSync(path.join(__dirname, "../css/main.css"), "utf8");

function setup(html) {
  const dom = new JSDOM(html || "<body></body>", { runScripts: "outside-only", pretendToBeVisual: true });
  const { window } = dom;
  // jsdom 默认 matchMedia 缺失；桌面（fine pointer）路径
  window.matchMedia = window.matchMedia || function () { return { matches: false, addEventListener() {} }; };
  window.HTMLElement.prototype.scrollIntoView = window.HTMLElement.prototype.scrollIntoView || function () {};
  window.eval(SOURCE);
  return { window, doc: window.document, close: () => window.close() };
}
function addSelect(doc, options, selected) {
  const sel = doc.createElement("select");
  sel.id = "demo";
  options.forEach(function (text, i) {
    const o = doc.createElement("option");
    o.value = String(i);
    o.textContent = text;
    sel.appendChild(o);
  });
  if (selected != null) sel.selectedIndex = selected;
  doc.body.appendChild(sel);
  return sel;
}
function buttonFor(doc, sel) {
  return sel.nextElementSibling && sel.nextElementSibling.classList.contains("yt-select") ? sel.nextElementSibling : null;
}
function flush() { return new Promise(r => setTimeout(r, 30)); }

test("静态 select 被增强：镜像按钮 + 原生隐藏", async t => {
  const x = setup();
  t.after(x.close);
  const sel = addSelect(x.doc, ["普通话", "英语", "韩语"], 0);
  await flush();
  const btn = buttonFor(x.doc, sel);
  assert.ok(btn, "应生成镜像按钮");
  assert.equal(btn.querySelector(".yt-select-label").textContent, "普通话");
  assert.ok(sel.classList.contains("yt-select-native"));
  assert.equal(sel.tabIndex, -1);
});

test("点击打开 listbox：选中项带 is-selected，颜色走变量", async t => {
  const x = setup();
  t.after(x.close);
  const sel = addSelect(x.doc, ["普通话", "英语", "韩语"], 2);
  await flush();
  const btn = buttonFor(x.doc, sel);
  btn.click();
  const menu = x.doc.querySelector(".yt-select-menu");
  assert.ok(menu, "应打开弹层");
  assert.equal(menu.getAttribute("role"), "listbox");
  const rows = menu.querySelectorAll(".yt-select-option");
  assert.equal(rows.length, 3);
  assert.ok(rows[2].classList.contains("is-selected"));
  assert.equal(rows[2].getAttribute("aria-selected"), "true");
  // CSS 选中态使用强调色变量而非系统蓝
  assert.match(CSS, /\.yt-select-option\.is-selected\s*{[^}]*var\(--focus-ring\)/);
});

test("点击选项：写回原生 select 并派发 change，关闭弹层", async t => {
  const x = setup();
  t.after(x.close);
  const sel = addSelect(x.doc, ["普通话", "英语", "韩语"], 0);
  let changes = 0;
  sel.addEventListener("change", () => changes++);
  await flush();
  buttonFor(x.doc, sel).click();
  const rows = x.doc.querySelectorAll(".yt-select-option");
  rows[1].click();
  assert.equal(sel.selectedIndex, 1);
  assert.equal(sel.value, "1");
  assert.equal(changes, 1);
  assert.equal(x.doc.querySelector(".yt-select-menu"), null);
  assert.equal(buttonFor(x.doc, sel).querySelector(".yt-select-label").textContent, "英语");
});

test("键盘：ArrowDown/Enter 选择，Escape 关闭不改值", async t => {
  const x = setup();
  t.after(x.close);
  const sel = addSelect(x.doc, ["a", "b", "c"], 0);
  await flush();
  const btn = buttonFor(x.doc, sel);
  btn.dispatchEvent(new x.window.KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }));
  assert.ok(x.doc.querySelector(".yt-select-menu"));
  x.doc.dispatchEvent(new x.window.KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }));
  x.doc.dispatchEvent(new x.window.KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  assert.equal(sel.selectedIndex, 1);
  // Escape 不改值
  btn.dispatchEvent(new x.window.KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  x.doc.dispatchEvent(new x.window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  assert.equal(sel.selectedIndex, 1);
  assert.equal(x.doc.querySelector(".yt-select-menu"), null);
});

test("程序化赋值（value setter）同步按钮文案", async t => {
  const x = setup();
  t.after(x.close);
  const sel = addSelect(x.doc, ["a", "b", "c"], 0);
  await flush();
  sel.value = "2";
  await flush();
  assert.equal(buttonFor(x.doc, sel).querySelector(".yt-select-label").textContent, "c");
});

test("动态创建的 select 自动接管；移除后清理", async t => {
  const x = setup();
  t.after(x.close);
  const wrap = x.doc.createElement("div");
  x.doc.body.appendChild(wrap);
  await flush();
  const sel = addSelect(x.doc, ["x", "y"], 0);
  wrap.appendChild(sel);
  await flush();
  assert.ok(buttonFor(x.doc, sel), "动态 select 应被增强");
  wrap.remove();
  await flush();
  assert.equal(x.doc.querySelectorAll(".yt-select").length, 0);
});

test("disabled 同步到按钮；optgroup 渲染分组", async t => {
  const x = setup();
  t.after(x.close);
  const sel = x.doc.createElement("select");
  const g = x.doc.createElement("optgroup");
  g.label = "亚洲";
  ["中文", "韩语"].forEach(function (text) {
    const o = x.doc.createElement("option"); o.textContent = text; g.appendChild(o);
  });
  sel.appendChild(g);
  x.doc.body.appendChild(sel);
  await flush();
  const btn = buttonFor(x.doc, sel);
  sel.disabled = true;
  await new Promise(r => setTimeout(r, 600)); // 轮询兜底同步
  assert.equal(btn.disabled, true);
  sel.disabled = false;
  await new Promise(r => setTimeout(r, 600));
  btn.click();
  assert.ok(x.doc.querySelector(".yt-select-group-label"));
  assert.equal(x.doc.querySelectorAll(".yt-select-option").length, 2);
});

test("触屏（coarse）保留原生 select，不增强", async t => {
  const dom = new JSDOM("<body></body>", { runScripts: "outside-only", pretendToBeVisual: true });
  const { window } = dom;
  window.matchMedia = function () { return { matches: true, addEventListener() {} }; };
  window.eval(SOURCE);
  const sel = addSelect(window.document, ["a", "b"], 0);
  await flush();
  assert.equal(buttonFor(window.document, sel), null);
  assert.ok(!sel.classList.contains("yt-select-native"));
  window.close();
});
