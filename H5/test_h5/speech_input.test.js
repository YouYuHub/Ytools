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
test('连续识别重启保留上一周期文本，发送取消待重启定时器', () => {
  const { context, input, instances, button, timers, restart } = setup(); button.click(); const rec = instances[0];
  rec.result(['第一周期', true]); rec.onend(); restart(); rec.result(['第二周期', true]);
  assert.equal(input.value, '第一周期第二周期');
  rec.onend(); context.App.stopVoiceInput(); assert.equal(timers.size, 0);
  restart(); assert.equal(rec.starts, 2);
});
test('光标选区替换保留尾部，手动编辑停止旧听写并保留编辑结果', () => {
  const { context, input, instances, button } = setup('前选中尾');
  input.selectionStart = 1; input.selectionEnd = 3; button.click();
  instances[0].result(['语音', true]); assert.equal(input.value, '前 语音尾');
  input.value = '用户修改'; input.dispatchEvent(new context.Event('input'));
  instances[0].result(['旧词', true]); assert.equal(input.value, '用户修改');
});
test('麦克风错误即时复位并提示，连续无语音重启有限次', () => {
  const { button, instances, messages, restart } = setup(); button.click();
  instances[0].onerror({ error: 'audio-capture' });
  assert.equal(button.attributes['aria-pressed'], 'false'); assert.match(messages[0], /麦克风/);
  button.click(); const rec = instances[1];
  for (let i = 0; i < 5; i++) { rec.onend(); restart(); }
  assert.equal(rec.starts, 5); assert.match(messages.at(-1), /未识别到语音/);
});
