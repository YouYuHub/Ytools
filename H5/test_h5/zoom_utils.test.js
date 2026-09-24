"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const ZoomUtils = require("../js/zoom_utils.js");

test("ZOOM_STEPS: 严格单调递增且包含基准 100", function () {
  const steps = ZoomUtils.ZOOM_STEPS;
  assert.ok(steps.length >= 10, "档位数量足够细");
  for (let i = 1; i < steps.length; i++) {
    assert.ok(steps[i] > steps[i - 1], "严格递增：" + steps[i - 1] + " -> " + steps[i]);
  }
  assert.ok(steps.indexOf(100) >= 0, "含 100 基准档");
  assert.equal(ZoomUtils.MIN_PERCENT, steps[0]);
  assert.equal(ZoomUtils.MAX_PERCENT, steps[steps.length - 1]);
});

test("percentFromScale: 倍率转整数百分比，非法归 100", function () {
  assert.equal(ZoomUtils.percentFromScale(1), 100);
  assert.equal(ZoomUtils.percentFromScale(2), 200);
  assert.equal(ZoomUtils.percentFromScale(0.5), 50);
  assert.equal(ZoomUtils.percentFromScale(1.25), 125);
  assert.equal(ZoomUtils.percentFromScale(0), 100);
  assert.equal(ZoomUtils.percentFromScale(-3), 100);
  assert.equal(ZoomUtils.percentFromScale(NaN), 100);
  assert.equal(ZoomUtils.percentFromScale("abc"), 100);
});

test("normalizePercent: 越界钳制 + 吸附最近档位", function () {
  assert.equal(ZoomUtils.normalizePercent(100), 100);
  assert.equal(ZoomUtils.normalizePercent(10), 50, "低于下限钳到 50");
  assert.equal(ZoomUtils.normalizePercent(9999), 800, "高于上限钳到 800");
  // 60 距离 50(10) 比 67(7) 远 -> 吸附 67；65 距 67(2) 比 50(15) 近 -> 67
  assert.equal(ZoomUtils.normalizePercent(65), 67);
  assert.equal(ZoomUtils.normalizePercent(58), 50);
  assert.equal(ZoomUtils.normalizePercent(0), 100, "0 视为非法按基准");
});

test("normalizeScale: 任意数值 -> 合法档位倍率", function () {
  assert.equal(ZoomUtils.normalizeScale(1), 1);
  assert.equal(ZoomUtils.normalizeScale(2.05), 2, "2.05 吸附 200%");
  assert.equal(ZoomUtils.normalizeScale(0.49), 0.5, "0.49 吸附 50%");
  assert.equal(ZoomUtils.normalizeScale(-1), 1);
  assert.equal(ZoomUtils.normalizeScale(100), 8, "超上限钳到 800%");
});

test("stepScale: 相邻档位步进，边界保持不动", function () {
  assert.equal(ZoomUtils.stepScale(1, +1), 1.1);
  assert.equal(ZoomUtils.stepScale(1, -1), 0.9);
  assert.equal(ZoomUtils.stepScale(0.9, +1), 1);
  // 非法起点按基准 100% 起步
  assert.equal(ZoomUtils.stepScale(NaN, +1), 1.1);
  assert.equal(ZoomUtils.stepScale(NaN, -1), 0.9);
  // 边界：最大档继续放大保持不动；最小档继续缩小保持不动
  const maxScale = ZoomUtils.MAX_PERCENT / 100;
  const minScale = ZoomUtils.MIN_PERCENT / 100;
  assert.equal(ZoomUtils.stepScale(maxScale, +1), maxScale);
  assert.equal(ZoomUtils.stepScale(minScale, -1), minScale);
});

test("canStep: 边界判定供按钮禁用", function () {
  assert.equal(ZoomUtils.canStep(1, +1), true);
  assert.equal(ZoomUtils.canStep(1, -1), true);
  assert.equal(ZoomUtils.canStep(ZoomUtils.MAX_PERCENT / 100, +1), false);
  assert.equal(ZoomUtils.canStep(ZoomUtils.MIN_PERCENT / 100, -1), false);
});

test("roundtrip: 倍率 <-> 百分比换算幂等（经 normalizePercent 归一）", function () {
  [0.5, 0.75, 1, 1.5, 2, 3, 5, 8].forEach(function (s) {
    const percent = ZoomUtils.percentFromScale(s);          // 倍率 -> 百分比
    const normalized = ZoomUtils.normalizePercent(percent); // 百分比 -> 合法档位
    assert.equal(normalized, percent, "scale " + s + " 已是合法档位");
    const scale = ZoomUtils.normalizeScale(normalized / 100); // 百分比/100 -> 倍率
    assert.equal(scale, s, "scale " + s);
  });
});
