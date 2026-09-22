const assert = require("node:assert");
const test = require("node:test");

const path = require("path");
const WidgetReuse = require(path.join(__dirname, "..", "js", "widget_reuse.js"));

test("plan: 无旧控件时全部新建", function () {
  const plan = WidgetReuse.plan([], [{ kind: "canvas", code: "a" }]);
  assert.strictEqual(plan.length, 1);
  assert.strictEqual(plan[0].reuseIndex, -1);
});

test("plan: 新渲染里无控件时返回空计划", function () {
  const plan = WidgetReuse.plan([{ kind: "svg", code: "a" }], []);
  assert.strictEqual(plan.length, 0);
});

test("plan: 源码相同精确复用（含顺序无关位移）", function () {
  const oldItems = [
    { kind: "svg", code: "A" },
    { kind: "canvas", code: "B" },
  ];
  const newItems = [
    { kind: "intro", code: "" },           // 插入文本不影响（仅描述列表）
    { kind: "canvas", code: "B" },
    { kind: "svg", code: "A" },
  ];
  const plan = WidgetReuse.plan(oldItems, newItems);
  assert.strictEqual(plan[0].reuseIndex, -1);
  assert.strictEqual(plan[1].reuseIndex, 1);
  assert.strictEqual(plan[2].reuseIndex, 0);
});

test("plan: svg 流式成长（旧源码是新源码前缀）按序复用", function () {
  const oldItems = [{ kind: "svg", code: "<svg>part" }];
  const newItems = [{ kind: "svg", code: "<svg>part2+more" }];
  const plan = WidgetReuse.plan(oldItems, newItems);
  assert.strictEqual(plan[0].reuseIndex, 0);
});

test("plan: canvas 流式成长同样按前缀复用", function () {
  const plan = WidgetReuse.plan(
    [{ kind: "canvas", code: "// script" }],
    [{ kind: "canvas", code: "// script more lines" }]
  );
  assert.strictEqual(plan[0].reuseIndex, 0);
});

test("plan: mermaid 不走前缀分支（源码变了必须重渲染）", function () {
  const plan = WidgetReuse.plan(
    [{ kind: "mermaid", code: "graph TD; A" }],
    [{ kind: "mermaid", code: "graph TD; A --> B" }]
  );
  assert.strictEqual(plan[0].reuseIndex, -1);
});

test("plan: 前缀不复用已消费的旧块", function () {
  // 两个新 svg 块都由同一个旧块前缀构成时，只有第一个拿到复用
  const plan = WidgetReuse.plan(
    [{ kind: "svg", code: "x" }],
    [
      { kind: "svg", code: "x1" },
      { kind: "svg", code: "x2" },
    ]
  );
  assert.strictEqual(plan[0].reuseIndex, 0);
  assert.strictEqual(plan[1].reuseIndex, -1);
});

test("plan: 运行中的 canvas（hasFrame）源码不匹配也复用", function () {
  const plan = WidgetReuse.plan(
    [{ kind: "canvas", code: "old-script", hasFrame: true }],
    [{ kind: "canvas", code: "brand new script" }]
  );
  assert.strictEqual(plan[0].reuseIndex, 0);
});

test("plan: hasFrame 仅限 canvas（svg 源码变了必须重建）", function () {
  const plan = WidgetReuse.plan(
    [{ kind: "svg", code: "old", hasFrame: false }],
    [{ kind: "svg", code: "totally different" }]
  );
  assert.strictEqual(plan[0].reuseIndex, -1);
});

test("plan: 含帧兜底与流式前缀共存", function () {
  // 新块0 "S2" 是旧块0 "S" 的成长 → 前缀阶段复用 0；
  // 新块1 "T" 无前缀关系 → 含帧兜底从后往前命中旧块1（hasFrame）
  const oldItems = [
    { kind: "canvas", code: "S" },
    { kind: "canvas", code: "running", hasFrame: true },
  ];
  const newItems = [
    { kind: "canvas", code: "S2" },
    { kind: "canvas", code: "T" },
  ];
  const plan = WidgetReuse.plan(oldItems, newItems);
  assert.strictEqual(plan[0].reuseIndex, 0);
  assert.strictEqual(plan[1].reuseIndex, 1);
});

test("plan: 含帧兜底不抢占可前缀复用的块", function () {
  // 新块0 源码全新无前缀 → 含帧兜底拿到旧块1；
  // 新块1 是旧块0 的成长，但旧块0 尚在（未消费），前缀按位对不上 → 新建
  const oldItems = [
    { kind: "canvas", code: "S" },
    { kind: "canvas", code: "running", hasFrame: true },
  ];
  const newItems = [
    { kind: "canvas", code: "totally-new" },
    { kind: "canvas", code: "S-grown" },
  ];
  const plan = WidgetReuse.plan(oldItems, newItems);
  assert.strictEqual(plan[0].reuseIndex, 1);
  assert.strictEqual(plan[1].reuseIndex, -1);
});
