"use strict";
// 回归：历史 user 消息编辑面板是输入框式卡片（自带底色/边框/圆角/柔光），
// 不再透出用户气泡的强调色底（深色+粉下曾是浅粉底配浅灰字，对比度不足）。
// 守护点：面板卡片声明、深色气泡底色排除面板、textarea 融入卡片、
// 方块网格与输入框同构、引用浮层下展、旧芯片/提示无残留、
// 面板内按钮引用 --edit-fg 前景变量。jsdom 不做布局，这里守护编译后 CSS
// （与 qnav_center_align 同口径）。
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const CSS = fs.readFileSync(path.join(__dirname, "../css/main.css"), "utf8");

function ruleBodies(selector) {
  const re = new RegExp(selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "\\s*\\{([^}]*)\\}", "g");
  const out = [];
  let m;
  while ((m = re.exec(CSS))) out.push(m[1]);
  assert.ok(out.length, "应存在规则 " + selector);
  return out;
}

function ruleBody(selector) {
  return ruleBodies(selector)[0];
}

test("面板是输入框式卡片：自带底色/边框/圆角/柔光 + 页面文字色", () => {
  const body = ruleBody(".msg-user .msg-edit-bubble");
  assert.match(body, /background:\s*var\(--bg-composer\)/);
  assert.match(body, /border:\s*1px solid var\(--border\)/);
  assert.match(body, /border-radius:\s*18px/);
  assert.match(body, /box-shadow:\s*var\(--shadow-soft\)/);
  assert.match(body, /color:\s*var\(--text\)/);
  assert.match(body, /--edit-fg:\s*var\(--text\)/);
  assert.match(body, /--edit-fg-secondary:\s*var\(--text-secondary\)/);
});

test("深色气泡底色排除编辑面板（:not），普通气泡不受影响", () => {
  const body = ruleBody('html[data-theme=dark] .msg .msg-bubble:not(.msg-edit-bubble)');
  assert.match(body, /background:\s*var\(--bg-bubble-user\)/);
});

test("textarea 透明融入卡片（不再半透明叠加致黑上加黑）", () => {
  const body = ruleBody(".msg-user .msg-edit-textarea");
  assert.match(body, /background:\s*transparent/);
  assert.doesNotMatch(body, /color-mix/);
  assert.match(ruleBody(".msg-user .msg-edit-textarea::placeholder"), /color:\s*var\(--text-tertiary\)/);
});

test("附件区是输入框同构方块网格（52px，列数由 fit 写入）", () => {
  const body = ruleBody(".msg-user .msg-edit-attachment-blocks");
  assert.match(body, /display:\s*grid/);
  assert.match(body, /grid-template-columns:\s*repeat\(var\(--attachment-block-columns,\s*1\),\s*var\(--attachment-block-size,\s*52px\)\)/);
  assert.match(body, /grid-auto-rows:\s*var\(--attachment-block-size,\s*52px\)/);
});

test("引用原文浮层向下展开（面板在消息流中部，向上弹会被裁）", () => {
  const pop = ruleBody(".msg-user .msg-edit-attachment-blocks .composer-quotes-popover");
  assert.match(pop, /top:\s*calc\(100% \+ 8px\)/);
  assert.match(pop, /bottom:\s*auto/);
  const bridge = ruleBody(".msg-user .msg-edit-attachment-blocks .composer-quotes-tile::before");
  assert.match(bridge, /top:\s*100%/);
  assert.match(bridge, /bottom:\s*auto/);
});

test("旧芯片/旧行/多余提示无残留样式", () => {
  for (const sel of [
    ".msg-edit-chip", ".msg-edit-chip-thumb", ".msg-edit-chip-name",
    ".msg-edit-chip-remove", ".msg-edit-quote-chip", ".msg-edit-quote-badge",
    ".msg-edit-quote-text", ".msg-edit-quote-row", ".msg-edit-media-row",
    ".msg-edit-upload-hint",
  ]) {
    assert.doesNotMatch(CSS, new RegExp(sel.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "\\s*\\{"), sel);
  }
});

test("面板内按钮引用 --edit-fg 前景变量，hover 加深字 + 淡底；上传按钮有禁用态", () => {
  assert.match(ruleBody(".msg-user .msg-edit-upload"), /color:\s*var\(--edit-fg-secondary\)/);
  assert.match(ruleBody(".msg-user .msg-edit-mode"), /color:\s*var\(--edit-fg-secondary\)/);
  assert.match(ruleBody(".msg-user .msg-edit-cancel"), /color:\s*var\(--edit-fg-secondary\)/);
  for (const sel of [
    ".msg-user .msg-edit-upload:hover",
    ".msg-user .msg-edit-mode:hover",
    ".msg-user .msg-edit-cancel:hover",
  ]) {
    const hover = ruleBody(sel);
    assert.match(hover, /color:\s*var\(--edit-fg\)/, sel);
    assert.match(hover, /var\(--edit-fg-secondary\)\s*12%/, sel);
  }
  const disabled = ruleBody(".msg-user .msg-edit-upload:disabled");
  assert.match(disabled, /opacity:\s*0\.5/);
  assert.match(disabled, /cursor:\s*not-allowed/);
});

test("选中态与发送按钮保持 accent 反白（不受前景变量影响）", () => {
  const selected = ruleBody(".msg-user .msg-edit-mode.selected");
  assert.match(selected, /background:\s*var\(--accent\)/);
  assert.match(selected, /color:\s*var\(--accent-contrast\)/);
  // 发送按钮另有一条与取消共用的 padding 规则，取最后一条专用规则断言。
  const send = ruleBodies(".msg-user .msg-edit-send").pop();
  assert.match(send, /background:\s*var\(--accent\)/);
  assert.match(send, /color:\s*var\(--accent-contrast\)/);
});
