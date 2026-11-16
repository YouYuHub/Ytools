"use strict";
// 回归：diff 编辑器页（editor.html）顶栏三件事——
// 1) 宽屏（>=769px）文件名与基线/版本状态同行，长路径单行溢出省略；窄屏保持竖排独占行；
// 2) 窄屏操作按钮行可收缩换行（历史缺陷：.eh-bar flex:none 时子项宽=整行
//    max-content，超出视口被裁切、内部 flex-wrap 永不触发，右侧按钮显示不全）；
// 3) 强调色下拉与主应用共用 ThemeManager 偏好，选区/焦点环跟随 --accent-swatch/--focus-ring。
// jsdom 不做布局，布局部分守护编译后 CSS 的关键声明不被回退（与 widget_zoom_centering 同口径）。
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM } = require("jsdom");

const HTML = fs.readFileSync(path.join(__dirname, "../editor.html"), "utf8");
const CSS = fs.readFileSync(path.join(__dirname, "../css/main.css"), "utf8");
const EDITOR_JS = fs.readFileSync(path.join(__dirname, "../js/app/editor.js"), "utf8");

// 取包含指定选择器的 @media (min-width: 769px) 块（花括号配平截取）
function wideMediaBlock() {
  const re = /@media\s*\(min-width:\s*769px\)\s*\{/g;
  let m;
  while ((m = re.exec(CSS))) {
    let depth = 1, i = re.lastIndex;
    while (i < CSS.length && depth > 0) {
      const ch = CSS[i];
      if (ch === "{") depth++;
      else if (ch === "}") depth--;
      i++;
    }
    const block = CSS.slice(re.lastIndex, i - 1);
    if (block.includes(".eh-title-wrap")) return block;
  }
  assert.fail("应存在包含 .eh-title-wrap 的宽屏媒体块");
}

function ruleBody(scope, selector) {
  const m = scope.match(new RegExp(selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "\\s*\\{([^}]*)\\}"));
  assert.ok(m, "应存在规则 " + selector);
  return m[1];
}

test("HTML：强调色下拉六选项与 ThemeManager.ACCENTS 同序", () => {
  const dom = new JSDOM(HTML);
  const sel = dom.window.document.getElementById("ehAccentSelect");
  assert.ok(sel, "editor.html 应有 #ehAccentSelect");
  const values = Array.from(sel.querySelectorAll("option")).map(o => o.value);
  assert.deepEqual(values, ["blue", "green", "purple", "pink", "yellow", "none"]);
  dom.window.close();
});

test("宽屏：标题横排、文件名溢出省略、状态不压缩、操作栏按内容取宽", () => {
  const block = wideMediaBlock();
  const wrap = ruleBody(block, ".eh-title-wrap");
  assert.match(wrap, /flex-direction:\s*row/);
  assert.match(wrap, /flex:\s*1 1 0/);
  assert.match(wrap, /min-width:\s*240px/);
  const strong = ruleBody(block, ".eh-title-wrap strong");
  assert.match(strong, /white-space:\s*nowrap/);
  assert.match(strong, /overflow:\s*hidden/);
  assert.match(strong, /text-overflow:\s*ellipsis/);
  const sub = ruleBody(block, ".eh-sub");
  assert.match(sub, /flex:\s*none/);
  const bar = ruleBody(block, ".eh-bar");
  assert.match(bar, /flex:\s*0 1 auto/);
  assert.match(bar, /min-width:\s*0/);
});

test("窄屏：标题竖排独占行；操作栏占满可用宽且可换行（不再 flex:none 裁切）", () => {
  // 基础规则（媒体块之外）：取第一个 .eh-bar / .eh-title-wrap 规则体
  const bar = ruleBody(CSS, ".eh-bar");
  assert.match(bar, /flex:\s*1 1 100%/);
  assert.match(bar, /min-width:\s*0/);
  assert.match(bar, /flex-wrap:\s*wrap/);
  assert.doesNotMatch(bar, /flex:\s*none/);
  const wrap = ruleBody(CSS, ".eh-title-wrap");
  assert.match(wrap, /flex-direction:\s*column/);
  assert.match(wrap, /flex:\s*1 1 100%/);
});

test("选区/整行高亮跟随强调色：color-mix(--accent-swatch)，硬编码深蓝清零", () => {
  const sel = ruleBody(CSS, ".eh-input::selection");
  assert.match(sel, /color-mix\(in srgb, var\(--accent-swatch/);
  assert.match(ruleBody(CSS, ".eh-line.is-selected"), /color-mix\(in srgb, var\(--accent-swatch/);
  assert.match(ruleBody(CSS, ".eh-hl .eh-sel"), /color-mix\(in srgb, var\(--accent-swatch/);
  assert.ok(!CSS.includes("rgba(49, 93, 140"), "旧硬编码选区蓝不应残留");
  // 键盘焦点环跟随 --focus-ring
  assert.match(CSS, /body\.eh-page \.fh-btn:focus-visible[\s\S]{0,120}outline:\s*2px solid var\(--focus-ring\)/);
});

// ---------- 强调色下拉接线（真实编译产物 editor.js + jsdom） ----------
function bootEditor(t, themeManager) {
  const dom = new JSDOM(HTML, {
    runScripts: "outside-only", url: "http://localhost/editor.html?session_id=s&key=12345678",
  });
  t.after(() => dom.window.close());
  const w = dom.window, d = w.document;
  w.ThemeManager = themeManager;
  w.API = {
    fileFullView: async () => ({ snapshot_only: true, truncated: true, rows: [], baseline_v: 0, current_v: 1 }),
    fileContent: async () => ({ snapshot_only: true, content: "partial", path: "C:/large.txt", display_path: "large.txt" }),
    fileVersions: async () => ({ versions: [{ v: 1, round: 1 }] }),
    fileSave: async () => {},
    fileRollback: async () => {},
  };
  w.eval(EDITOR_JS);
  return new Promise(resolve => setImmediate(() => resolve({ w, d })));
}

test("接线：初始回填偏好、change 写偏好、accentchange 外部同步回填", async t => {
  let accent = "pink";
  const writes = [];
  const { w, d } = await bootEditor(t, {
    getPreference: () => "dark",
    setPreference() {},
    getAccentPreference: () => accent,
    setAccentPreference(v) {
      accent = v; writes.push(v);
      d.dispatchEvent(new w.CustomEvent("accentchange", { detail: { accent: v } }));
    },
    ACCENTS: ["blue", "green", "purple", "pink", "yellow", "none"],
  });
  const sel = d.getElementById("ehAccentSelect");
  assert.equal(sel.value, "pink", "初始应回填当前强调色偏好");
  sel.value = "green";
  sel.dispatchEvent(new w.Event("change", { bubbles: true }));
  assert.deepEqual(writes, ["green"], "change 应写入 ThemeManager 偏好");
  // 其它页面改偏好 → ThemeManager 派发 accentchange → 下拉回填
  accent = "yellow";
  d.dispatchEvent(new w.CustomEvent("accentchange", { detail: { accent: "yellow" } }));
  assert.equal(sel.value, "yellow");
});

test("接线：旧版 ThemeManager 桩（无强调色 API）不抛错、下拉保持默认", async t => {
  const { w, d } = await bootEditor(t, { getPreference: () => "dark", setPreference() {} });
  const sel = d.getElementById("ehAccentSelect");
  assert.equal(sel.value, "blue", "无 API 时不回填，保持 HTML 默认首项");
  sel.dispatchEvent(new w.Event("change", { bubbles: true })); // 不应抛错
});
