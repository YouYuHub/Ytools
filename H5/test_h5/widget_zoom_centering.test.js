"use strict";
// 回归：四类控件（svg/mermaid/chart/canvas）共用的缩放钮 [-][100%][+]
// 文字必须水平+垂直居中。历史缺陷：.md-svg-btn 是 inline-flex，默认
// justify-content:flex-start，min-width 撑出的富余宽度把 "-/+/100%" 顶到
// 左侧（text-align:center 对 flex 子项无效）。修复用 justify-content:center
// + align-items:center + line-height:1。jsdom 不做布局，这里守护编译后 CSS
// 的关键声明不被回退（与 pause_gap_duration 的 CSS 断言同口径）。
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const CSS = fs.readFileSync(path.join(__dirname, "../css/main.css"), "utf8");
const MD = fs.readFileSync(path.join(__dirname, "../js/markdown.js"), "utf8");

// 取 .md-zoom-btn 规则体（编译后为 .md-svg-block .md-zoom-group .md-zoom-btn）
function zoomBtnRule() {
  const m = CSS.match(/\.md-zoom-btn\s*\{([^}]*)\}/);
  assert.ok(m, "应存在 .md-zoom-btn 规则");
  return m[1];
}

test("缩放钮水平居中：justify-content:center（flex 子项 text-align 无效）", () => {
  assert.match(zoomBtnRule(), /justify-content:\s*center/);
});

test("缩放钮垂直居中：align-items:center", () => {
  assert.match(zoomBtnRule(), /align-items:\s*center/);
});

test("缩放钮行盒收紧：line-height:1（消除基线导致的上下偏移）", () => {
  assert.match(zoomBtnRule(), /line-height:\s*1\b/);
});

test("四类控件共用同一缩放组标记（一处修复全部生效）", () => {
  // buildZoomGroupHtml 被 svg/mermaid/canvas/chart 四个 builder 调用
  const calls = (MD.match(/buildZoomGroupHtml\(\)/g) || []).length;
  assert.ok(calls >= 4, "缩放组应被四类控件共用，实际调用 " + calls + " 次");
  // 三个钮都带 md-zoom-btn 类（居中规则命中全部）
  assert.match(MD, /md-zoom-btn[^"]*"[^>]*data-zoom-action="out"/);
  assert.match(MD, /md-zoom-reset[^"]*"[^>]*data-zoom-action="reset"/);
  assert.match(MD, /md-zoom-btn[^"]*"[^>]*data-zoom-action="in"/);
});
