"use strict";
// 回归：Canvas 沙箱 BOOT 必须是合法 JS。
// 历史缺陷：TS 迁移把 `: any` 类型注解误写进 CANVAS_BOOT_JS 的字符串字面量，
// tsc 不剥离字符串内容 → 沙箱 <script> 语法错误 → canvas-ready 永不发出 →
// 点「运行」后画布空白（五子棋等所有 canvas 块全部失效）。
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM } = require("jsdom");

const SRC = fs.readFileSync(path.join(__dirname, "../js/app/messages_canvas.js"), "utf8");
function bootSource() {
  const m = SRC.match(/const CANVAS_BOOT_JS = \[([\s\S]*?)\]\.join\(/);
  assert.ok(m, "应能定位 CANVAS_BOOT_JS");
  // eslint-disable-next-line no-eval
  return eval("[" + m[1] + "]").join("\n");
}

test("CANVAS_BOOT_JS 可被解析为合法 JavaScript", () => {
  const boot = bootSource();
  assert.doesNotThrow(() => new Function(boot), "BOOT 含语法错误（字符串内混入类型注解？）");
});

test("BOOT 字符串字面量不含 TS 类型注解残留", () => {
  const lines = SRC.split("\n").filter(l => /^\s*"/.test(l));
  const bad = lines.filter(l => /:\s*any\b/.test(l));
  assert.deepEqual(bad, [], "字符串内残留 : any → " + bad.join(" | "));
});

// 在真实运行时里跑通五子棋：ready→live→落子→五连获胜→重开。
function harness() {
  const dom = new JSDOM('<!doctype html><body><canvas id="stage" width="720" height="420"></canvas></body>',
    { runScripts: "outside-only", pretendToBeVisual: true });
  const { window } = dom;
  const sent = [];
  // canvas 2d 上下文桩：所有方法 no-op、属性可写（jsdom 无真实 canvas）
  const ctxStub = new Proxy({}, {
    get(t, p) {
      if (p === "createRadialGradient" || p === "createLinearGradient")
        return () => ({ addColorStop() {} });
      if (p === "measureText") return () => ({ width: 10 });
      if (typeof p === "string" && !(p in t)) return function () {};
      return t[p];
    },
    set(t, p, v) { t[p] = v; return true; },
  });
  window.HTMLCanvasElement.prototype.getContext = function () { return ctxStub; };
  window.HTMLCanvasElement.prototype.toDataURL = function () { return "data:image/png;base64,AAAA"; };
  const stage = window.document.getElementById("stage");
  stage.getBoundingClientRect = () => ({ left: 0, top: 0, width: stage.width, height: stage.height, right: stage.width, bottom: stage.height });
  // parent === window（顶层）；捕获 BOOT 的出站 postMessage
  window.postMessage = (msg) => { sent.push(msg); };
  window.eval(bootSource());
  return { window, stage, sent, close: () => window.close() };
}
function runCode(h, code) {
  h.window.dispatchEvent(new h.window.MessageEvent("message", { data: { type: "canvas-run", id: "c1", code } }));
}
function click(h, x, y) {
  h.stage.dispatchEvent(new h.window.MouseEvent("click", { bubbles: true, clientX: x, clientY: y }));
}
const tick = () => new Promise(r => setTimeout(r, 40));
const GOMOKU = fs.readFileSync(path.join(__dirname, "_gomoku_fixture.js"), "utf8");

test("运行时执行五子棋：ready/live/日志齐全，五连判胜，重开复位", async t => {
  const h = harness();
  t.after(h.close);
  runCode(h, GOMOKU);
  await tick();
  const types = h.sent.map(m => m.type);
  assert.ok(types.includes("canvas-ready"), "应发 ready");
  assert.ok(types.includes("canvas-live"), "点击监听应保持运行态 live");
  assert.ok(h.sent.some(m => m.type === "canvas-log" && /游戏开始/.test(m.line)), "应有开局日志");

  // 几何：W640 H720 CELL38 MARGIN54 TOP70 → toPixel(r,c)=(54+38c,70+38r)
  const P = (r, c) => [54 + c * 38, 70 + r * 38];
  const moves = [[7,3],[0,0],[7,4],[0,1],[7,5],[0,2],[7,6],[0,3],[7,7]];
  for (const [r, c] of moves) { click(h, ...P(r, c)); await tick(); }
  assert.ok(h.sent.some(m => m.type === "canvas-log" && /黑棋获胜/.test(m.line)), "黑棋五连应判胜");

  // 重开按钮 (320,684)：复位后应再发开局日志且不再报胜
  const before = h.sent.filter(m => m.type === "canvas-log" && /已重新开始/.test(m.line)).length;
  click(h, 320, 684);
  await tick();
  const after = h.sent.filter(m => m.type === "canvas-log" && /已重新开始/.test(m.line)).length;
  assert.equal(after, before + 1, "点击重新开始应复位");
});

test("一次性绘制脚本（无监听/无循环）结算为 done 并带快照", async t => {
  const h = harness();
  t.after(h.close);
  runCode(h, "ctx.fillStyle='#123';ctx.fillRect(0,0,W,H);console.log('drawn');");
  await tick(); await tick();
  const done = h.sent.find(m => m.type === "canvas-done");
  assert.ok(done, "一次性脚本应结算 done");
  assert.ok(/^data:image\/png/.test(done.shot), "done 应携带快照");
  assert.ok(h.sent.some(m => m.type === "canvas-log" && m.line === "drawn"));
});

test("用户脚本同步抛错 → canvas-error 上报，不静默", async t => {
  const h = harness();
  t.after(h.close);
  runCode(h, "throw new Error('boom');");
  await tick();
  const err = h.sent.find(m => m.type === "canvas-error");
  assert.ok(err, "应上报 canvas-error");
  assert.match(err.error, /boom/);
});
