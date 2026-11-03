const assert = require("node:assert");
const test = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { stripTypeScriptTypes } = require("node:module");

try {
  global.katex = require(path.join(__dirname, "..", "js", "vendor", "katex", "katex.min.js"));
} catch (_) { /* 无 katex：降级路径 */ }
const { JSDOM } = require("jsdom");
global.DOMPurify = require("dompurify")(new JSDOM("").window);
const Markdown = require(path.join(__dirname, "..", "js", "markdown.js"));
const WidgetReuse = require(path.join(__dirname, "..", "js", "widget_reuse.js"));

function loadRender() {
  const source = stripTypeScriptTypes(
    fs.readFileSync(path.join(__dirname, "../src/app/messages_render.ts"), "utf8"));
  const start = source.indexOf("  function renderPreservingWidgets(");
  const end = source.indexOf("  // 用户气泡", start);
  const dom = new JSDOM("<!DOCTYPE html><body></body>");
  const context = { document: dom.window.document, Markdown, WidgetReuse, console };
  vm.createContext(context);
  vm.runInContext(source.slice(start, end), context);
  const host = context.document.createElement("div");
  context.document.body.appendChild(host);
  return { render: context.renderPreservingWidgets, host, document: context.document };
}

const TABLE_A = "| 建议 | 优先级 |\n| --- | --- |\n| 平移 | P0 |\n| 历史栈 | P0 |";
const TABLE_B = "| 功能 | 状态 |\n| --- | --- |\n| 复制 | 好 |\n";

test("已完成表格追加正文时保留原节点", () => {
  const h = loadRender();
  h.render(h.host, Markdown.render("前言\n\n" + TABLE_A));
  const table = h.host.querySelector(".md-table-block");
  assert.ok(table);
  h.render(h.host, Markdown.render("前言\n\n" + TABLE_A + "\n\n后续段落追加"));
  assert.strictEqual(h.host.querySelector(".md-table-block"), table, "同一表格应复用原节点");
  assert.match(h.host.textContent, /后续段落追加/);
});

test("表格横向滚动位置在重渲染后保留", () => {
  const h = loadRender();
  h.render(h.host, Markdown.render(TABLE_A + "\n\n尾巴"));
  const box = h.host.querySelector(".md-table-scroll");
  box.scrollLeft = 120;
  h.render(h.host, Markdown.render(TABLE_A + "\n\n尾巴更长了"));
  const box2 = h.host.querySelector(".md-table-scroll");
  assert.strictEqual(box2, box);
  assert.strictEqual(box2.scrollLeft, 120);
});

test("展开中的「更多」菜单在重渲染后保持展开", () => {
  const h = loadRender();
  h.render(h.host, Markdown.render(TABLE_A + "\n\n尾巴"));
  const block = h.host.querySelector(".md-table-block");
  const menu = block.querySelector(".md-table-menu");
  const btn = block.querySelector(".md-table-more-btn");
  menu.classList.remove("hidden");
  btn.setAttribute("aria-expanded", "true");
  h.render(h.host, Markdown.render(TABLE_A + "\n\n尾巴更长了"));
  const block2 = h.host.querySelector(".md-table-block");
  assert.strictEqual(block2, block);
  assert.ok(!block2.querySelector(".md-table-menu").classList.contains("hidden"), "菜单应保持展开");
  assert.strictEqual(block2.querySelector(".md-table-more-btn").getAttribute("aria-expanded"), "true");
});

test("生长中的表格重建并显示新行，滚动/菜单走兜底恢复", () => {
  const h = loadRender();
  h.render(h.host, Markdown.render(TABLE_A));
  const old = h.host.querySelector(".md-table-block");
  old.querySelector(".md-table-scroll").scrollLeft = 88;
  old.querySelector(".md-table-menu").classList.remove("hidden");
  const grown = TABLE_A + "\n| 自动保存 | P0 |";
  h.render(h.host, Markdown.render(grown));
  const now = h.host.querySelector(".md-table-block");
  assert.notStrictEqual(now, old, "源码变化应重建节点");
  assert.match(now.textContent, /自动保存/);
  assert.strictEqual(now.querySelector(".md-table-scroll").scrollLeft, 88, "滚动兜底恢复");
  assert.ok(!now.querySelector(".md-table-menu").classList.contains("hidden"), "菜单兜底恢复展开");
});

test("多表格按序配对，互不串扰", () => {
  const h = loadRender();
  h.render(h.host, Markdown.render(TABLE_A + "\n\n中间\n\n" + TABLE_B));
  const blocks = Array.from(h.host.querySelectorAll(".md-table-block"));
  assert.strictEqual(blocks.length, 2);
  blocks[1].querySelector(".md-table-scroll").scrollLeft = 66;
  h.render(h.host, Markdown.render(TABLE_A + "\n\n中间更长\n\n" + TABLE_B + "\n\n结尾"));
  const after = Array.from(h.host.querySelectorAll(".md-table-block"));
  assert.strictEqual(after[0], blocks[0]);
  assert.strictEqual(after[1], blocks[1]);
  assert.strictEqual(after[1].querySelector(".md-table-scroll").scrollLeft, 66);
  assert.strictEqual(after[0].querySelector(".md-table-scroll").scrollLeft, 0);
});

test("表格与 svg 控件混合时各自复用", () => {
  const h = loadRender();
  const svg = "```svg\n<svg xmlns=\"http://www.w3.org/2000/svg\"></svg>\n```";
  h.render(h.host, Markdown.render(TABLE_A + "\n\n" + svg + "\n\n尾巴"));
  const table = h.host.querySelector(".md-table-block");
  const svgBlock = h.host.querySelector(".md-svg-block");
  assert.ok(table && svgBlock);
  h.render(h.host, Markdown.render(TABLE_A + "\n\n" + svg + "\n\n尾巴更长"));
  assert.strictEqual(h.host.querySelector(".md-table-block"), table);
  assert.strictEqual(h.host.querySelector(".md-svg-block"), svgBlock);
});

test("代码块滚动恢复不受影响", () => {
  const h = loadRender();
  h.render(h.host, Markdown.render("```js\nconst a = 1;\n```\n\n" + TABLE_A));
  const pre = h.host.querySelector(".codeblock pre");
  assert.ok(pre);
  pre.scrollTop = 33;
  h.render(h.host, Markdown.render("```js\nconst a = 1;\n```\n\n" + TABLE_A + "\n\n尾"));
  assert.strictEqual(h.host.querySelector(".codeblock pre").scrollTop, 33);
});
