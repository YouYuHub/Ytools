"use strict";
// 回归：MCP 测试弹窗宽屏左列（服务连接 + 工具清单）内容必须永远可达。
// 历史缺陷：.mcp-test-side 用 sticky + max-height 但无 overflow 兜底；视口偏矮、
// 两卡下限之和超过上限时，清单卡溢出侧栏盒被 body 裁掉，而 sticky 列不随 body
// 滚动——超出的清单内容永远够不着（“滚动无效”）。
// 修复：侧栏 overflow-y:auto（放不下时左列自身内部滚动）+ min-height:0；
// 窄屏（≤960px）回退静态单列，侧栏 overflow 可见、随 body 滚动。
// jsdom 不做布局，这里守护编译后 CSS 的关键声明（与 qnav_center_align 同口径）。
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const CSS = fs.readFileSync(path.join(__dirname, "../css/main.css"), "utf8");

function ruleBody(selector) {
  const m = CSS.match(new RegExp(selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "\\s*\\{([^}]*)\\}"));
  assert.ok(m, "应存在规则 " + selector);
  return m[1];
}

function mediaBlock(query, mustContain) {
  // expanded 风格：媒体块以顶格 "}\n" 收尾，内部规则缩进，不会提前截断。
  // 同一查询可能有多个媒体块（如各组件的 640 断点），按 mustContain 挑选。
  const re = new RegExp("@media \\(" + query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "\\) \\{([\\s\\S]*?)\\n\\}", "g");
  for (const m of CSS.matchAll(re)) {
    if (!mustContain || m[1].includes(mustContain)) return m[1];
  }
  assert.fail("应存在包含 " + mustContain + " 的媒体查询 " + query);
}

test("左列 sticky 常驻且有内部滚动兜底（矮视口清单可达）", () => {
  const body = ruleBody(".mcp-test-side");
  assert.match(body, /position:\s*sticky/);
  assert.match(body, /overflow-y:\s*auto/, "无此兜底则溢出内容被裁且够不着");
  assert.match(body, /min-height:\s*0/);
  assert.match(body, /height:\s*calc\(100dvh - 230px\)/, "固定填满可用高度，清单卡才能吃到剩余空间");
});

test("工具清单列表自身可内部滚动", () => {
  const body = ruleBody(".mcp-test-tool-options");
  assert.match(body, /overflow:\s*auto/);
});

test("工具清单卡有足够最小高度（约 3 行工具，不被连接卡挤到只剩 1 行）", () => {
  const body = ruleBody(".mcp-test-tools-card");
  const m = body.match(/min-height:\s*(\d+)px/);
  assert.ok(m, "清单卡应有 min-height 下限");
  assert.ok(Number(m[1]) >= 300, "下限至少 300px（标题+搜索+按钮约 150 + 3 行工具）");
});

test("宽屏连接提示收起为 2 行（纵向空间让给清单；窄屏显示全文）", () => {
  const block = mediaBlock("min-width: 961px", "mcpTestConnectionHint");
  const m = block.match(/#mcpTestConnectionHint\s*\{([^}]*)\}/);
  assert.ok(m, "宽屏媒体块内应收起 #mcpTestConnectionHint");
  assert.match(m[1], /-webkit-line-clamp:\s*2/);
  assert.match(m[1], /overflow:\s*hidden/);
});

test("窄屏回退静态单列：侧栏不 sticky、不内部滚动，随 body 走", () => {
  const block = mediaBlock("max-width: 960px", ".mcp-test-side");
  const m = block.match(/\.mcp-test-side\s*\{([^}]*)\}/);
  assert.ok(m, "窄屏媒体块内应覆盖 .mcp-test-side");
  assert.match(m[1], /position:\s*static/);
  assert.match(m[1], /overflow-y:\s*visible/);
  assert.match(m[1], /height:\s*auto/);
});

test("连接区紧凑两行（grid 强制，服务+超时同行，按钮下行），纵向让给清单", () => {
  const body = ruleBody(".mcp-test-connection");
  assert.match(body, /display:\s*grid/);
  assert.match(body, /grid-template-columns:/);
});

test("工具清单行高收紧（纵向 padding 4px，不再 8px 大行）", () => {
  const body = ruleBody(".mcp-test-tool-option");
  assert.match(body, /padding:\s*4px/);
});

test("工具选择弹窗服务栏收紧（min-height 44px，不再 56px）", () => {
  const body = ruleBody(".tool-config-modal .tool-groups .tool-group .tool-group-head");
  assert.match(body, /min-height:\s*44px/);
});

test("窄屏连接按钮只显示图标（文字隐藏+对称两列，大字体下不重叠）", () => {
  const block = mediaBlock("max-width: 640px", "mcp-test-btn-label");
  const label = block.match(/mcp-test-btn-label\s*\{([^}]*)\}/);
  assert.ok(label, "窄屏媒体块内应隐藏按钮文字");
  assert.match(label[1], /display:\s*none/);
  const conn = block.match(/\.mcp-test-connection\s*\{([^}]*)\}/);
  assert.ok(conn, "窄屏媒体块内应覆盖连接区列定义");
  assert.match(conn[1], /grid-template-columns:\s*1fr 1fr/);
});

test("连接区按钮图标尺寸固定 16px（宽屏图标+文字）", () => {
  const body = ruleBody(".mcp-test-btn-icon");
  assert.match(body, /width:\s*16px/);
  assert.match(body, /height:\s*16px/);
});
