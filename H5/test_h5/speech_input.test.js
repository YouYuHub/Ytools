"use strict";
/**
 * 语音输入回归（composer.js 语音区）
 * 覆盖：
 * - 编辑重锚定：删除/修改文本不中断听写，以新文本与光标为新插入点继续监听
 * - 增量消费：周期结果快照全量重建（重复 final 不重复追加、interim 实时预览）
 * - 持续监听：onend 自动重启保留跨周期文本；静默/aborted/network 温和重试
 * - 停止语义：发送清空后迟到结果不回填；手动停止可收尾最后一段
 * 说明：vm 切片加载语音区，Recognition/定时器均为桩，手动触发事件推进状态机。
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
function setup(value = '') {
  const instances = [], timers = new Map(), messages = [], handlers = {};
  let timerId = 0;
  class Recognition {
    constructor() { instances.push(this); this.starts = 0; }
    start() { this.starts++; }
    stop() { this.stopped = true; }
    abort() { this.aborted = true; }
    result(...texts) { this.onresult({ resultIndex: 0, results: texts.map(([text, final]) => Object.assign([{ transcript: text }], { isFinal: final })) }); }
  }
  const input = { value, selectionStart: value.length, selectionEnd: value.length,
    setSelectionRange(start, end) { this.selectionStart = start; this.selectionEnd = end; },
    addEventListener(type, fn) { handlers[type] = fn; }, dispatchEvent(event) { if (handlers[event.type]) handlers[event.type](event); },
  };
  const button = { classList: { add() {}, remove() {} }, attributes: {},
    setAttribute(key, value) { this.attributes[key] = value; }, addEventListener(type, fn) { this.click = fn; },
  };
  const context = { input, voiceBtn: button, state: {}, refreshComposerButtons() {}, toast: value => messages.push(value),
    window: { SpeechRecognition: Recognition, addEventListener() {} }, App: {},
    Event: class { constructor(type) { this.type = type; } },
    setTimeout(fn) { timers.set(++timerId, fn); return timerId; }, clearTimeout(id) { timers.delete(id); },
  };
  const source = fs.readFileSync(path.join(__dirname, '../js/app/composer.js'), 'utf8');
  const start = source.indexOf('  let speechRec = null;');
  const end = source.indexOf('  // ---------- 运行中发送选项', start);
  vm.createContext(context); vm.runInContext(source.slice(start, end), context);
  return { context, input, instances, button, messages, timers,
    restart() { const callbacks = [...timers.values()]; timers.clear(); callbacks.forEach(fn => fn()); },
    edit(text) { input.value = text; input.selectionStart = input.selectionEnd = text.length; input.dispatchEvent(new context.Event('input')); },
  };
}
test('发送清空后迟到识别结果不回填，旧实例不污染新识别会话', () => {
  const { context, input, instances, button } = setup();
  button.click(); const old = instances[0]; old.result(['发出的消息', false]);
  context.App.stopVoiceInput(); input.value = '';
  old.result(['发出的消息', true]); old.onend();
  assert.equal(input.value, ''); assert.equal(old.aborted, true);
  button.click(); old.result(['旧结果', true]); instances[1].result(['新消息', true]);
  assert.equal(input.value, '新消息');
});
test('累计结果重复回调不重复追加，多个临时结果都保留', () => {
  const { input, instances, button } = setup(); button.click(); const rec = instances[0];
  rec.result(['第一句', true], ['第二句', false]);
  rec.result(['第一句', true], ['第二句', true]);
  rec.result(['第一句', true], ['第二句', true]);
  assert.equal(input.value, '第一句第二句');
});
test('手动停止可接受最后结果，发送后则拒绝该结果', () => {
  const { context, input, instances, button } = setup(); button.click(); const rec = instances[0];
  rec.result(['预览', false]); button.click(); rec.result(['最终', true]);
  assert.equal(input.value, '最终'); assert.equal(rec.stopped, true);
  context.App.stopVoiceInput(); input.value = ''; rec.result(['迟到', true]);
  assert.equal(input.value, '');
});
test('连续识别自动重启：新实例续听、跨周期文本保留，发送取消待重启定时器', () => {
  const { context, input, instances, button, timers, restart } = setup(); button.click(); const rec = instances[0];
  rec.result(['第一周期', true]); rec.onend();
  assert.equal(timers.size, 1, '周期结束应计划自动重启');
  restart();
  assert.equal(instances.length, 2, '重启应创建新识别实例');
  const next = instances[1]; next.result(['第二周期', true]);
  assert.equal(input.value, '第一周期第二周期');
  next.onend(); context.App.stopVoiceInput(); assert.equal(timers.size, 0);
  restart(); assert.equal(instances.length, 2, '停止后不得再重启');
});
test('删除文本不中断听写：同一实例继续监听（不重建不弹权限），编辑前结果不重放', () => {
  const { input, instances, button, edit } = setup();
  button.click(); const rec = instances[0];
  rec.result(['你好世界', true]); assert.equal(input.value, '你好世界');
  edit('你好'); // 用户删除“世界”
  assert.equal(button.attributes['aria-pressed'], 'true', '编辑后仍在录音');
  assert.equal(instances.length, 1, '编辑不得重建识别实例（file:// 下会反复弹麦克风权限）');
  assert.equal(rec.starts, 1, '编辑不得重新 start()');
  assert.equal(rec.aborted, undefined, '编辑不得 abort 当前识别');
  // 旧结果（第 1 条）已消费：下次回调仍含它，只取新增结果续写
  rec.result(['你好世界', true], ['今天天气不错', true]);
  assert.equal(input.value, '你好 今天天气不错', '编辑前结果不重放、新结果以编辑后文本为基线续写');
  // 再次编辑（继续输入/删除）：skip 基准随已消费结果数推进
  edit('你好 今天');
  rec.result(['你好世界', true], ['今天天气不错', true], ['，很好', false]);
  assert.equal(input.value, '你好 今天 ，很好');
  assert.equal(instances.length, 1, '连续编辑始终不重建实例');
});
test('待重启挂起期间编辑：不打断重启计划，新实例从零累积继续监听', () => {
  const { input, instances, button, timers, restart, edit } = setup();
  button.click(); const rec = instances[0];
  rec.result(['第一段', true]); rec.onend(); // 周期结束，重启计划挂起
  assert.equal(timers.size, 1);
  edit('第一段'); // 挂起期间用户编辑（删除字符后重新输入）
  assert.equal(timers.size, 1, '编辑不应打断已计划的重启');
  restart(); assert.equal(instances.length, 2);
  const next = instances[1];
  next.result(['第二段', true]);
  assert.equal(input.value, '第一段 第二段', '重启后从零累积，与编辑后文本拼接');
});

test('光标选区替换保留尾部，停止后编辑不误触重锚定', () => {
  const { context, input, instances, button } = setup('前选中尾');
  input.selectionStart = 1; input.selectionEnd = 3; button.click();
  instances[0].result(['语音', true]); assert.equal(input.value, '前 语音尾');
  context.App.stopVoiceInput();
  input.value = '用户修改'; input.dispatchEvent(new context.Event('input'));
  assert.equal(input.value, '用户修改', '停止后编辑仅保留用户文本');
});
test('静默（no-speech/aborted）不停止听写，空周期达上限才停止并提示', () => {
  const { button, instances, messages, restart } = setup(); button.click();
  const rec = instances[0];
  rec.onerror({ error: 'no-speech' });
  assert.equal(button.attributes['aria-pressed'], 'true', '静默不应停止听写');
  rec.onerror({ error: 'aborted' });
  assert.equal(button.attributes['aria-pressed'], 'true', 'aborted 不应停止听写');
  let cur = rec;
  for (let i = 0; i < 59; i++) { cur.onend(); restart(); cur = instances[instances.length - 1]; }
  assert.equal(button.attributes['aria-pressed'], 'true', '59 个空周期内仍持续监听');
  cur.onend(); // 第 60 个空周期达上限
  assert.equal(button.attributes['aria-pressed'], 'false');
  assert.match(messages.at(-1), /长时间未识别到语音/);
});
test('网络异常温和重试：抖动恢复后继续监听，连续多次才停止', () => {
  const { button, instances, messages } = setup(); button.click();
  const rec = instances[0];
  for (let i = 0; i < 4; i++) rec.onerror({ error: 'network' });
  assert.equal(button.attributes['aria-pressed'], 'true', '网络抖动不应立即停止');
  rec.result(['恢复后的内容', true]);
  assert.equal(button.attributes['aria-pressed'], 'true');
  for (let i = 0; i < 5; i++) rec.onerror({ error: 'network' });
  assert.equal(button.attributes['aria-pressed'], 'false');
  assert.match(messages.at(-1), /网络异常/);
});
test('麦克风错误即时复位并提示', () => {
  const { button, instances, messages } = setup(); button.click();
  instances[0].onerror({ error: 'audio-capture' });
  assert.equal(button.attributes['aria-pressed'], 'false'); assert.match(messages[0], /麦克风/);
});
