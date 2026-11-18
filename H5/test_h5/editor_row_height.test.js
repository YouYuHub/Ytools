"use strict";
// 回归：diff 编辑器行高在"窄屏换行 → 宽屏恢复"后必须回收，不留大片空白。
// 根因：.eh-input 为 position:absolute + inset:0，height:auto 会拉伸到单元格
// 旧高度（top/bottom 均 0），scrollHeight 只增不减；且 textarea 默认 rows=2。
// 修复：renderRow 设 input.rows=1；syncHeight 测量时临时 bottom:auto 让盒塌缩到
// 真实内容行数，测完恢复 inset:0 与显式高度。jsdom 不做绝对定位布局，这里守护
// 编译产物 editor.js 的关键语句与顺序不被回退（与 editor_head_layout 同口径）。
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const JS = fs.readFileSync(path.join(__dirname, "../js/app/editor.js"), "utf8");

function syncHeightBody() {
  const m = JS.match(/function syncHeight\(input, hl, cell\)\s*\{([\s\S]*?)\n    \}/);
  assert.ok(m, "应存在 syncHeight 函数");
  return m[1];
}

test("renderRow：textarea 设 rows=1（单行行测量不落到默认 rows=2 的两行高度）", () => {
  assert.match(JS, /input\.rows\s*=\s*1\s*;/, "创建输入层时应设 rows=1");
});

test("syncHeight：测量前解除 bottom 约束、测后恢复，行高可随宽度回收", () => {
  const body = syncHeightBody();
  const iBottomAuto = body.indexOf('input.style.bottom = "auto"');
  const iHeightAuto = body.indexOf('input.style.height = "auto"');
  const iMeasure = body.indexOf("input.scrollHeight");
  const iBottomReset = body.indexOf('input.style.bottom = ""');
  const iHeightSet = body.indexOf("input.style.height = h");
  assert.ok(iBottomAuto >= 0, "测量前应临时 bottom:auto 解除 inset 拉伸");
  assert.ok(iHeightAuto > iBottomAuto, "bottom:auto 应在 height:auto 之前");
  assert.ok(iMeasure > iHeightAuto, "scrollHeight 测量应在塌缩之后");
  assert.ok(iBottomReset > iMeasure, "测后应恢复 bottom（inset:0）");
  assert.ok(iHeightSet > iBottomReset, "恢复 bottom 后再写显式高度");
  // 单元格高度仍显式写入（absolute 层不参与行布局）
  assert.match(body, /cell\.style\.height\s*=\s*h\s*\+\s*"px"/);
});
