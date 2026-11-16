"use strict";
// 回归：右侧问题导航轨道必须「无论条目多少都垂直居中」。
// 历史缺陷：.qnav 用 align-items:flex-start（顶对齐），条目少时轨道贴在顶栏
// 下方，只有条目多到撑高轨道才看起来靠中部，短/长会话观感不一致。
// 修复：可用区上下对称（top:64px / bottom:64px）+ align-items:center，
// 使轨道中心恒等于视口中线；展开面板同心居中，保证 hover 桥接不闪退。
// jsdom 不做布局，这里守护编译后 CSS 的关键声明（与 widget_zoom_centering 同口径）。
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const CSS = fs.readFileSync(path.join(__dirname, "../css/main.css"), "utf8");

// 取顶层 .qnav 规则体（其后代规则以 ".qnav " 开头，不会误命中）
function qnavRule() {
  const m = CSS.match(/^\.qnav\s*\{([^}]*)\}/m);
  assert.ok(m, "应存在顶层 .qnav 规则");
  return m[1];
}

function ruleBody(selector) {
  const m = CSS.match(new RegExp(selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "\\s*\\{([^}]*)\\}"));
  assert.ok(m, "应存在规则 " + selector);
  return m[1];
}

test("轨道始终垂直居中：align-items:center（不再 flex-start 顶对齐）", () => {
  const body = qnavRule();
  assert.match(body, /align-items:\s*center/);
  assert.doesNotMatch(body, /align-items:\s*flex-start/);
});

test("可用区上下对称：top 与 bottom 同为 64px（居中点=视口中线，与条目数无关）", () => {
  const body = qnavRule();
  const top = body.match(/top:\s*(\d+)px/);
  const bottom = body.match(/bottom:\s*(\d+)px/);
  assert.ok(top && bottom, "top/bottom 应均为具体像素值");
  assert.equal(top[1], bottom[1], "上下预留必须相等，否则居中点偏移");
  assert.equal(top[1], "64");
});

test("轨道高度上限交给容器：max-height:100%（超高才内部滚动，不影响居中）", () => {
  assert.match(ruleBody(".qnav .qnav-rail"), /max-height:\s*100%/);
});

test("展开面板与轨道同心：top:50% + translateY(-50%)（hover 桥接不闪退）", () => {
  const body = ruleBody(".qnav .qnav-panel");
  assert.match(body, /top:\s*50%/);
  assert.match(body, /transform:\s*translateY\(-50%\)/);
  assert.match(body, /max-height:\s*min\(480px,\s*100%\)/);
});

test("窄屏仍走顶栏按钮 + 静态面板：清掉桌面端居中偏移", () => {
  // 窄屏容器查询块内：.qnav 恢复 stretch/bottom:auto，面板 static 且 top/transform 复位。
  // 同名容器查询在多处出现（_layout.scss 也有），需按花括号配平逐块取含 .qnav 的那个。
  const re = /@container main-area \(max-width: 680px\)\s*\{/g;
  let narrow = "", m;
  while ((m = re.exec(CSS))) {
    let depth = 1, i = re.lastIndex;
    while (i < CSS.length && depth > 0) {
      if (CSS[i] === "{") depth++;
      else if (CSS[i] === "}") depth--;
      i++;
    }
    const block = CSS.slice(re.lastIndex, i - 1);
    if (/\.qnav\s*\{/.test(block)) { narrow = block; break; }
  }
  assert.ok(narrow, "应存在包含 .qnav 覆盖的窄屏容器查询块");
  const qnavNarrow = narrow.match(/\.qnav\s*\{([^}]*)\}/);
  assert.ok(qnavNarrow, "窄屏应有 .qnav 覆盖规则");
  assert.match(qnavNarrow[1], /align-items:\s*stretch/);
  assert.match(qnavNarrow[1], /bottom:\s*auto/);
  const panelNarrow = narrow.match(/\.qnav \.qnav-panel\s*\{([^}]*)\}/);
  assert.ok(panelNarrow, "窄屏应有 .qnav-panel 覆盖规则");
  assert.match(panelNarrow[1], /position:\s*static/);
  assert.match(panelNarrow[1], /top:\s*auto/);
  assert.match(panelNarrow[1], /transform:\s*none/);
});
