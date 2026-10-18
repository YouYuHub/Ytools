"use strict";
// 加载实际语音区，用识别器桩覆盖中文空格、英文分词及用户操作/迟到事件的竞争。
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const SpeechLanguage = require('../js/speech_language.js');

function setup(value = '', options = {}) {
  const instances = [], timers = new Map(), messages = [];
  let timerId = 0, now = 0;
  function target(props = {}) {
    const handlers = {};
    return Object.assign(props, {
      addEventListener(type, fn) { (handlers[type] ||= []).push(fn); },
      dispatchEvent(event) { let result; for (const fn of handlers[event.type] || []) result = fn(event); return result; },
      emit(type, props = {}) { return this.dispatchEvent({ type, ...props }); },
    });
  }
  class Recognition {
    constructor() { if (options.constructorError) throw Error('unavailable'); instances.push(this); this.starts = 0; }
    start() {
      this.starts++;
      if (options.startError) throw Error('permission required');
      if (options.syncError) { this.onerror({ error: options.syncError }); return; }
      if (!options.hangStart) this.onstart();
    }
    stop() { this.stopped = true; if (options.syncEnd) this.onend(); }
    abort() { this.aborted = true; if (!options.manualEnd || options.syncEnd) this.onend(); }
    result(...texts) {
      this.onresult({ resultIndex: 0, results: texts.map(([text, final]) => Object.assign([{ transcript: text }], { isFinal: final })) });
    }
  }
  const input = target({ value, selectionStart: value.length, selectionEnd: value.length,
    setSelectionRange(start, end) { this.selectionStart = start; this.selectionEnd = end; },
  });
  const button = target({ classes: new Set(), attributes: {},
    setAttribute(key, value) { this.attributes[key] = value; }, click() { this.emit('click'); },
  });
  button.classList = { add: c => button.classes.add(c), remove: c => button.classes.delete(c) };
  const languageSelect = target({ value: '', children: [], appendChild(node) { this.children.push(node); } });
  const languageBadge = { textContent: '' };
  const languageStatus = { textContent: '' };
  const store = options.store || new Map();
  if (options.language) store.set(SpeechLanguage.STORAGE_KEY, options.language);
  const storage = {
    getItem(key) { if (options.storageError) throw Error('blocked'); return store.get(key); },
    setItem(key, value) { if (options.storageError) throw Error('blocked'); store.set(key, value); },
  };
  const document = target({ hidden: false,
    getElementById(id) { return id === 'speechLanguage' ? languageSelect : id === 'voiceLanguageBadge' ? languageBadge : id === 'speechLanguageStatus' ? languageStatus : null; },
    createElement() { return {}; },
  });
  const window = target({ SpeechRecognition: Recognition, SpeechLanguage, localStorage: storage });
  if (options.storageAccessError) Object.defineProperty(window, 'localStorage', { get() { throw Error('access denied'); } });
  const context = { input, voiceBtn: button, state: {}, refreshComposerButtons() {}, toast: value => messages.push(value),
    window, document, API: options.api, AbortController, App: {}, Event: class { constructor(type) { this.type = type; } },
    setTimeout(fn, delay = 0) { timers.set(++timerId, { fn, due: now + delay }); return timerId; }, clearTimeout(id) { timers.delete(id); },
  };
  const source = fs.readFileSync(path.join(__dirname, '../js/app/composer.js'), 'utf8');
  const start = source.indexOf('  let speechRec = null;');
  const end = source.indexOf('  // ---------- 运行中发送选项', start);
  vm.createContext(context); vm.runInContext(source.slice(start, end), context);
  return { context, input, instances, button, document, window, messages, timers, store, languageSelect, languageBadge, languageStatus,
    language(code) { languageSelect.value = code; return languageSelect.emit('change'); },
    runTimers() { for (const [id, timer] of [...timers]) if (timer.due <= now) { timers.delete(id); timer.fn(); } },
    advanceTime(ms) { now += ms; this.runTimers(); },
    edit(text) { input.value = text; input.selectionStart = input.selectionEnd = text.length; input.emit('input'); },
  };
}

test('中文语音合并汉字空格，跨识别结果边界也清理，保留换行', () => {
  const { input, instances, button } = setup(); button.click(); const rec = instances[0];
  rec.result([' 当 我 的 鼠 标 ', true], [' 在 那 个 导 航 条 空 隙 之 间 ， 可 以 优 化 吗 ？', false]);
  assert.equal(input.value, '当我的鼠标在那个导航条空隙之间，可以优化吗？');
  rec.result(['你\t好\u00a0世\u3000界\n中 文 （ 测 试 ）', true]);
  assert.equal(input.value, '你好世界\n中文（测试）');
  rec.result(['𠀀 𠀁 汉 字', true]);
  assert.equal(input.value, '𠀀𠀁汉字', '汉字扩展区也识别为汉字');
});

test('手动选择的各语种传给识别器，状态与语言角标一致', () => {
  const x = setup();
  assert.equal(x.languageSelect.children.length, SpeechLanguage.languages.length);
  for (const language of SpeechLanguage.languages) {
    x.language(language.code); x.button.click();
    const rec = x.instances.at(-1);
    assert.equal(rec.lang, language.code);
    assert.equal(x.languageSelect.value, language.code);
    assert.equal(x.languageBadge.textContent, language.badge);
    assert.ok(x.button.attributes['aria-label'].includes(language.label));
    x.context.App.stopVoiceInput();
  }
});

test('语言偏好跨页面初始化保留，损坏配置回退中文，选择不会自行开启麦克风', () => {
  const store = new Map(); const first = setup('', { store }); first.language('en-US');
  assert.equal(first.instances.length, 0); assert.equal(store.get(SpeechLanguage.STORAGE_KEY), 'en-US');
  const second = setup('', { store }); second.button.click(); assert.equal(second.instances[0].lang, 'en-US');
  store.set(SpeechLanguage.STORAGE_KEY, 'invalid'); const fallback = setup('', { store }); fallback.button.click();
  assert.equal(fallback.instances[0].lang, 'zh-CN');
});

test('项目配置优先于旧浏览器缓存，清除缓存后仍能从后端恢复', async () => {
  let writes = 0;
  const api = {
    async getSpeechInputConfig() { return { language: 'en-US', persisted: true }; },
    async updateSpeechInputConfig() { writes++; },
  };
  const x = setup('', { api, language: 'fr-FR' });
  await x.context.App.loadSpeechInputSettings();
  assert.equal(x.languageSelect.value, 'en-US');
  assert.equal(x.store.get(SpeechLanguage.STORAGE_KEY), 'en-US');
  assert.equal(writes, 0); assert.equal(x.instances.length, 0);
  const fresh = setup('', { api }); await fresh.context.App.loadSpeechInputSettings();
  assert.equal(fresh.languageSelect.value, 'en-US');
  assert.match(fresh.languageStatus.textContent, /清除浏览器/);
  fresh.button.click(); assert.equal(fresh.instances[0].lang, 'en-US');
});

test('未配置的后端有条件迁移旧偏好，已迁移的缓存不再次写入', async () => {
  const writes = [];
  const api = {
    async getSpeechInputConfig() { return { language: 'zh-CN', persisted: false }; },
    async updateSpeechInputConfig(language, onlyIfUnset) {
      writes.push([language, onlyIfUnset]); return { language, persisted: true };
    },
  };
  const x = setup('', { api, language: 'ja-JP' });
  await x.context.App.loadSpeechInputSettings();
  assert.deepEqual(writes, [['ja-JP', true]]);
  const reopened = setup('', { api, store: x.store });
  await reopened.context.App.loadSpeechInputSettings();
  assert.equal(writes.length, 1); assert.equal(reopened.languageSelect.value, 'zh-CN');
  assert.equal(reopened.instances.length, 0);
});

test('迁移期间其他浏览器已保存时采纳服务端返回的值', async () => {
  const x = setup('', { language: 'fr-FR', api: {
    async getSpeechInputConfig() { return { language: 'zh-CN', persisted: false }; },
    async updateSpeechInputConfig(language, onlyIfUnset) {
      assert.equal(onlyIfUnset, true); return { language: 'de-DE', persisted: true, updated: false };
    },
  } });
  await x.context.App.loadSpeechInputSettings();
  assert.equal(x.languageSelect.value, 'de-DE');
  assert.equal(x.store.get(SpeechLanguage.STORAGE_KEY), 'de-DE');
});

test('迟到的初始化读取不覆盖新选择，保存过程禁止重叠写入', async () => {
  let finishRead, finishWrite;
  const writes = [];
  const x = setup('', { api: {
    getSpeechInputConfig() { return new Promise(resolve => { finishRead = resolve; }); },
    updateSpeechInputConfig(language, onlyIfUnset) {
      writes.push([language, onlyIfUnset]); return new Promise(resolve => { finishWrite = resolve; });
    },
  } });
  const reading = x.context.App.loadSpeechInputSettings();
  const saving = x.language('fr-FR');
  assert.equal(x.languageSelect.disabled, true);
  await x.language('de-DE'); await x.context.App.loadSpeechInputSettings();
  assert.deepEqual(writes, [['fr-FR', false]]);
  finishWrite({ language: 'fr-FR', persisted: true }); await saving;
  finishRead({ language: 'en-US', persisted: true }); await reading;
  assert.equal(x.languageSelect.value, 'fr-FR'); assert.equal(x.languageSelect.disabled, false);
  assert.equal(x.instances.length, 0);
});

test('写入失败保留原配置与草稿，随后允许再次选择保存', async () => {
  let failed = true;
  const x = setup('draft', { language: 'en-US', api: {
    async updateSpeechInputConfig(language) {
      if (failed) throw Error('disk unavailable'); return { language, persisted: true };
    },
  } });
  x.button.click(); await x.language('fr-FR');
  assert.equal(x.input.value, 'draft'); assert.equal(x.instances[0].aborted, true);
  assert.equal(x.languageSelect.value, 'en-US'); assert.equal(x.languageSelect.disabled, false);
  assert.equal(x.store.get(SpeechLanguage.STORAGE_KEY), 'en-US');
  assert.match(x.languageStatus.textContent, /保存未确认成功/);
  failed = false; await x.language('fr-FR');
  assert.equal(x.languageSelect.value, 'fr-FR'); assert.equal(x.instances.length, 1);
  assert.equal(x.store.get(SpeechLanguage.STORAGE_KEY), 'fr-FR');
});

test('后端读取断网不抹掉浏览器缓存，保存超时可重试且不启动麦克风', async () => {
  const x = setup('', { language: 'ja-JP', api: {
    async getSpeechInputConfig() { throw Error('offline'); },
    updateSpeechInputConfig(language, onlyIfUnset, options) {
      return new Promise((resolve, reject) => options.signal.addEventListener('abort', () => {
        const error = Error('aborted'); error.name = 'AbortError'; reject(error);
      }));
    },
  } });
  await x.context.App.loadSpeechInputSettings();
  assert.equal(x.languageSelect.value, 'ja-JP'); assert.match(x.languageStatus.textContent, /浏览器缓存/);
  const saving = x.language('en-US'); x.advanceTime(8000); await saving;
  assert.equal(x.languageSelect.disabled, false); assert.equal(x.languageSelect.value, 'ja-JP');
  assert.match(x.languageStatus.textContent, /保存超时/); assert.equal(x.instances.length, 0);
  assert.equal(x.timers.size, 0);
});

test('从后端同步新默认不会中断正在听写的语种与文字处理，下次显式启动生效', async () => {
  const x = setup('', { language: 'en-US', api: {
    async getSpeechInputConfig() { return { language: 'zh-CN', persisted: true }; },
  } });
  x.button.click(); const old = x.instances[0];
  await x.context.App.loadSpeechInputSettings();
  assert.equal(old.lang, 'en-US'); assert.equal(old.aborted, undefined);
  assert.equal(x.languageSelect.value, 'zh-CN'); assert.equal(x.languageBadge.textContent, 'EN');
  old.result(['中 文 hello world', true]); assert.equal(x.input.value, '中 文 hello world');
  x.context.App.stopVoiceInput(); x.button.click();
  assert.equal(x.instances[1].lang, 'zh-CN'); assert.equal(x.languageBadge.textContent, '中');
  x.instances[1].result(['新 语 音', true]); assert.equal(x.input.value, '中 文 hello world新语音');
});

test('缓存被禁用仍能保存并读取 .env 偏好，无效响应不会冒充成功', async () => {
  let invalid = true;
  const x = setup('', { storageError: true, api: {
    async getSpeechInputConfig() { return { language: 'de-DE', persisted: true }; },
    async updateSpeechInputConfig(language) { return invalid ? {} : { language, persisted: true }; },
  } });
  await x.context.App.loadSpeechInputSettings(); assert.equal(x.languageSelect.value, 'de-DE');
  await x.language('en-US'); assert.equal(x.languageSelect.value, 'de-DE');
  assert.match(x.languageStatus.textContent, /未确认成功/);
  invalid = false; await x.language('en-US'); assert.equal(x.languageSelect.value, 'en-US');
  assert.match(x.languageStatus.textContent, /已保存到项目/);
});

for (const options of [{ storageError: true }, { storageAccessError: true }]) {
  test(`存储不可用时本页仍能切换语言（${JSON.stringify(options)}）`, () => {
    const x = setup('', options); x.language('fr-FR'); x.button.click();
    assert.equal(x.instances[0].lang, 'fr-FR'); assert.equal(x.languageBadge.textContent, 'FR');
    assert.ok(x.messages.some(message => message.includes('未能保存偏好')));
  });
}

test('听写中切换语言保留草稿，取消输入法缓存与旧事件，不自动重启', () => {
  const x = setup(); x.button.click(); const old = x.instances[0];
  old.result(['中文草稿', true]); x.input.emit('compositionstart'); x.edit('中文草稿输入法');
  old.result(['中文草稿', true], ['不应回填', true]); x.input.emit('compositionend');
  x.language('ja-JP'); x.runTimers();
  assert.equal(x.input.value, '中文草稿输入法'); assert.equal(old.aborted, true);
  assert.equal(x.button.attributes['aria-pressed'], 'false'); assert.equal(x.instances.length, 1);
  x.button.click(); old.result(['旧语音', true]); old.onend();
  assert.equal(x.instances[1].lang, 'ja-JP'); assert.equal(x.input.value, '中文草稿输入法');
  x.instances[1].result(['日 本 語 です', true]); assert.equal(x.input.value, '中文草稿输入法日本語です');
});

test('语言不可用时提示当前选择，旧实例错误不污染新语言', () => {
  const x = setup('', { language: 'de-DE' }); x.button.click(); const old = x.instances[0];
  old.onerror({ error: 'language-not-supported' });
  assert.ok(x.messages.at(-1).includes('德语')); assert.equal(x.button.attributes['aria-pressed'], 'false');
  x.language('en-US'); x.button.click(); old.onerror({ error: 'language-not-supported' });
  assert.equal(x.button.attributes['aria-pressed'], 'true'); assert.equal(x.messages.filter(m => m.includes('不支持')).length, 1);
});

test('日语、韩语、法语和阿拉伯语按各自规则保留分词和文字', () => {
  for (const [language, spoken, expected] of [
    ['ja-JP', '私 は 日 本 語 の テ ス ト です。 OpenAI API', '私は日本語のテストです。 OpenAI API'],
    ['ko-KR', '한국어 음성 입력 테스트입니다.', '한국어 음성 입력 테스트입니다.'],
    ['fr-FR', 'Bonjour le monde\u00a0! C’est déjà prêt.', 'Bonjour le monde\u00a0! C’est déjà prêt.'],
    ['de-DE', 'Guten Tag! Das ist schön.', 'Guten Tag! Das ist schön.'],
    ['ar-SA', 'مرحبا بالعالم كيف حالك', 'مرحبا بالعالم كيف حالك'],
  ]) {
    const x = setup('', { language }); x.button.click(); x.instances[0].result([spoken, true]);
    assert.equal(x.input.value, expected);
  }
  const ja = setup('こんにちは', { language: 'ja-JP' }); ja.button.click(); ja.instances[0].result(['世界', true]);
  assert.equal(ja.input.value, 'こんにちは世界');
});

test('英文词间空格、数字、小数和标点保留；中英混合只清理汉字空格', () => {
  const { input, instances, button } = setup(); button.click(); const rec = instances[0];
  rec.result(['Hello, world! Use OpenAI API v2.5.\n', true], ['Next line.', false]);
  assert.equal(input.value, 'Hello, world! Use OpenAI API v2.5.\nNext line.');
  rec.result(['使 用 OpenAI API 查 询 hello world', true]);
  assert.equal(input.value, '使用 OpenAI API 查询 hello world');
});

test('只处理语音文本，手动输入的中文空格及换行保持原样', () => {
  const { input, instances, button } = setup('手 动 文 本\n');
  button.click(); instances[0].result(['语 音 结 果', true]);
  assert.equal(input.value, '手 动 文 本\n语音结果');
});

test('光标选区替换保留尾部，中文不补空格，英文边界正确分词', () => {
  const zh = setup('前选中尾'); zh.input.setSelectionRange(1, 3); zh.button.click();
  zh.instances[0].result(['语 音', true]); assert.equal(zh.input.value, '前语音尾');
  const en = setup('Hello replaced world.'); en.input.setSelectionRange(5, 14); en.button.click();
  en.instances[0].result(['beautiful', true]); assert.equal(en.input.value, 'Hello beautiful world.');
  const punct = setup('Hello,'); punct.button.click(); punct.instances[0].result(['world!', true]);
  assert.equal(punct.input.value, 'Hello, world!');
});

test('重复 final 不重复追加，interim 修订/移除使用完整快照', () => {
  const { input, instances, button } = setup(); button.click(); const rec = instances[0];
  rec.result(['Hello ', true], ['wor', false]);
  rec.result(['Hello ', true], ['world', true]);
  rec.result(['Hello ', true], ['world', true]);
  assert.equal(input.value, 'Hello world');
  rec.result(['Hello ', true], ['world', true], [' draft', false]);
  assert.equal(input.value, 'Hello world draft');
  rec.result(['Hello ', true], ['world', true]); assert.equal(input.value, 'Hello world');
});

test('空结果不会删除原选区；全部临时结果被撤回时恢复原稿', () => {
  const { input, instances, button } = setup('前选中尾'); input.setSelectionRange(1, 3); button.click();
  const rec = instances[0]; rec.result(); assert.equal(input.value, '前选中尾');
  rec.result(['替换', false]); assert.equal(input.value, '前替换尾');
  rec.result(); assert.equal(input.value, '前选中尾');
  assert.equal(input.selectionStart, 1); assert.equal(input.selectionEnd, 3);
});

test('手动停止允许收尾最后结果，但不再后台重启', () => {
  const { input, instances, button, timers, runTimers } = setup(); button.click(); const rec = instances[0];
  rec.result(['预 览', false]); button.click(); rec.result(['最 终', true]); rec.onend();
  assert.equal(input.value, '最终'); assert.equal(rec.stopped, true);
  assert.equal(button.attributes['aria-pressed'], 'false');
  assert.equal(timers.size, 0); runTimers(); assert.equal(instances.length, 1);
});

for (const event of ['beforeinput', 'input']) {
  test(`用户 ${event} 编辑后继续听写，不覆盖草稿、不重放已显示结果`, () => {
    const { input, instances, button, timers, runTimers, edit } = setup(); button.click(); const rec = instances[0];
    rec.result(['语 音 草 稿', false]); input.emit(event); edit('用户修改');
    rec.result(['语 音 草 稿', true], ['继续 听 写', true]); runTimers();
    assert.equal(input.value, '用户修改继续听写'); assert.equal(rec.aborted, undefined);
    assert.equal(button.attributes['aria-pressed'], 'true');
    assert.equal(timers.size, 0); assert.equal(instances.length, 1); assert.equal(rec.starts, 1);
  });
}

test('点击聚焦、方向键和选择复制不中断听写，也不重建识别实例', () => {
  const { input, instances, button } = setup(); button.click(); const rec = instances[0];
  input.emit('pointerdown'); input.emit('focus'); input.emit('click'); input.emit('keydown');
  assert.equal(button.attributes['aria-pressed'], 'true');
  rec.result(['中文 听 写', true]);
  assert.equal(input.value, '中文听写');
  input.setSelectionRange(0, 2); input.emit('select'); input.emit('copy');
  rec.result(['中文 听 写 正 常', true]);
  assert.equal(input.value, '中文听写正常');
  assert.equal(input.selectionStart, 0); assert.equal(input.selectionEnd, 2);
  assert.equal(button.attributes['aria-pressed'], 'true'); assert.equal(rec.aborted, undefined);
  assert.equal(instances.length, 1); assert.equal(rec.starts, 1);
});

test('选择前缀不抢光标，键盘编辑后从新光标继续听写', () => {
  const { input, instances, button, edit, runTimers } = setup('原稿：'); button.click(); const rec = instances[0];
  rec.result(['临时', false]); input.emit('pointerdown'); input.setSelectionRange(0, 2);
  rec.result(['最终 语 音', true]);
  assert.equal(input.value, '原稿：最终语音');
  assert.equal(input.selectionStart, 0); assert.equal(input.selectionEnd, 2);
  assert.equal(button.attributes['aria-pressed'], 'true');
  input.emit('beforeinput'); edit('修改：最终语音');
  rec.result(['最终 语 音', true], ['续写', true]); runTimers();
  assert.equal(input.value, '修改：最终语音续写'); assert.equal(rec.aborted, undefined);
  assert.equal(button.attributes['aria-pressed'], 'true'); assert.equal(instances.length, 1);
});

test('尾部选区随语音片段增长移动，重复回调仍保留原尾部选区', () => {
  const { input, instances, button } = setup('前尾部'); input.setSelectionRange(1, 1); button.click(); const rec = instances[0];
  rec.result(['好', false]); assert.equal(input.value, '前好尾部');
  input.setSelectionRange(2, 4); input.emit('select');
  rec.result(['你 好 世 界', true]); assert.equal(input.value, '前你好世界尾部');
  assert.equal(input.selectionStart, 5); assert.equal(input.selectionEnd, 7);
  rec.result(['你 好 世 界', true]);
  assert.equal(input.selectionStart, 5); assert.equal(input.selectionEnd, 7);
  assert.equal(button.attributes['aria-pressed'], 'true');
});

test('语音片段内移动光标不取消，临时文本缩短时选区安全收缩', () => {
  const { input, instances, button } = setup('前尾'); input.setSelectionRange(1, 1); button.click(); const rec = instances[0];
  rec.result(['一 二 三 四', false]); input.setSelectionRange(3, 5); input.emit('select');
  rec.result(['一', true]); assert.equal(input.value, '前一尾');
  assert.equal(input.selectionStart, 2); assert.equal(input.selectionEnd, 2);
  assert.equal(button.attributes['aria-pressed'], 'true');
});

test('手动停止后的收尾期间编辑也隔离迟到结果，删除后恢复相同文本不误接收', () => {
  const { input, instances, button, edit } = setup(); button.click(); const rec = instances[0];
  rec.result(['原稿', false]); button.click(); edit('原'); edit('原稿');
  rec.result(['不应覆盖', true]); rec.onend();
  assert.equal(input.value, '原稿'); assert.equal(rec.aborted, true);
});

test('未发送 input 的外部文本修改也优先于语音结果', () => {
  const { input, instances, button } = setup(); button.click(); const rec = instances[0];
  input.value = '粘贴的文本'; input.setSelectionRange(input.value.length, input.value.length); rec.result(['语音', true]);
  assert.equal(input.value, '粘贴的文本语音'); assert.equal(rec.aborted, undefined);
  assert.equal(button.attributes['aria-pressed'], 'true');
});

test('输入法组合期间不写回文本和光标，提交后接入缓存的新增语音', () => {
  const { input, instances, button, edit, runTimers } = setup(); button.click(); const rec = instances[0];
  rec.result(['之前', true]);
  input.emit('compositionstart'); input.emit('beforeinput', { isComposing: true }); edit('之前zhong');
  const caret = input.selectionStart;
  rec.result(['之前', true], ['语 音 临 时', false]);
  assert.equal(input.value, '之前zhong'); assert.equal(input.selectionStart, caret);
  edit('之前中文'); rec.result(['之前', true], ['语 音 最 终', true]);
  assert.equal(input.value, '之前中文'); assert.equal(button.attributes['aria-pressed'], 'true');
  input.emit('compositionend'); input.emit('input', { isComposing: false });
  assert.equal(input.value, '之前中文', '最终 input 完成前不改写');
  runTimers();
  assert.equal(input.value, '之前中文语音最终');
  rec.result(['之前', true], ['语 音 最 终', true]); assert.equal(input.value, '之前中文语音最终');
  assert.equal(instances.length, 1); assert.equal(rec.starts, 1); assert.equal(rec.aborted, undefined);
});

test('compositionend 先于最终 input 时等待最终候选词提交，不重复原临时前缀', () => {
  const { input, instances, button, edit, runTimers } = setup(); button.click(); const rec = instances[0];
  rec.result(['你 好', false]); input.emit('compositionstart'); edit('你好ping');
  rec.result(['你好世界', true]); input.emit('compositionend');
  edit('你好拼音'); runTimers();
  assert.equal(input.value, '你好拼音世界');
  rec.result(['你好世界', true], ['后续', true]);
  assert.equal(input.value, '你好拼音世界后续');
  assert.equal(button.attributes['aria-pressed'], 'true'); assert.equal(instances.length, 1);
});

test('连续多次输入法组合和删除不停止，已删除的语音不重放', () => {
  const { input, instances, button, edit, runTimers } = setup(); button.click(); const rec = instances[0];
  rec.result(['你好世界', true]); edit('你好');
  rec.result(['你好世界', true], ['新内容', true]); assert.equal(input.value, '你好新内容');
  input.emit('compositionstart'); edit('你好新内容yi'); rec.result(['你好世界', true], ['新内容', true], ['声音', false]);
  input.emit('compositionend'); edit('你好新内容一'); runTimers(); assert.equal(input.value, '你好新内容一声音');
  input.emit('compositionstart'); edit('你好新内容一声音er');
  rec.result(['你好世界', true], ['新内容', true], ['声音', true], ['更多', true]);
  input.emit('compositionend'); edit('你好新内容一声音二'); runTimers();
  assert.equal(input.value, '你好新内容一声音二更多');
  assert.equal(button.attributes['aria-pressed'], 'true'); assert.equal(instances.length, 1);
});

test('英文输入和语音并行仍保留词间空格，粘贴后新位置插入并保留尾部', () => {
  const { input, instances, button, edit, runTimers } = setup(); button.click(); const rec = instances[0];
  rec.result(['Hello', true]); input.emit('beforeinput'); edit('Hello typed');
  rec.result(['Hello', true], ['new words', true]); runTimers(); assert.equal(input.value, 'Hello typed new words');
  input.emit('beforeinput'); input.value = 'Head tail'; input.setSelectionRange(4, 4); input.emit('input');
  rec.result(['Hello', true], ['new words', true], ['middle', true]); runTimers();
  assert.equal(input.value, 'Head middle tail'); assert.equal(button.attributes['aria-pressed'], 'true');
});

test('输入法期间自然断开保留缓存，提交后仅写入一次并续听', () => {
  const x = setup();
  const { input, instances, button, edit, runTimers, timers } = x; button.click(); const rec = instances[0];
  input.emit('compositionstart'); edit('zhong'); rec.result(['缓存 的 语 音', true]); rec.onend();
  assert.equal(button.attributes['aria-pressed'], 'true'); assert.equal(input.value, 'zhong');
  input.emit('compositionend'); edit('中文'); runTimers();
  assert.equal(input.value, '中文缓存的语音'); assert.equal(instances.length, 1);
  x.advanceTime(300); assert.equal(instances.length, 2); assert.equal(timers.size, 0);
  instances[1].result(['继续', true]); assert.equal(input.value, '中文缓存的语音继续');
  input.emit('input'); runTimers(); assert.equal(input.value, '中文缓存的语音继续');
});

for (const action of ['send', 'stop', 'new-session']) {
  test(`输入法待提交时 ${action} 取消缓存写回，不污染发送/新草稿`, () => {
    const { context, input, instances, button, edit, runTimers, timers } = setup(); button.click(); const rec = instances[0];
    input.emit('compositionstart'); edit('用户候选词'); rec.result(['未写回语音', true]);
    input.emit('compositionend');
    if (action === 'stop') button.click();
    else context.App.stopVoiceInput();
    input.value = '新草稿'; input.setSelectionRange(3, 3);
    if (action === 'new-session') button.click();
    runTimers(); rec.result(['迟到', true]); rec.onend();
    assert.equal(input.value, '新草稿'); assert.equal(rec.aborted, true); assert.equal(timers.size, 0);
    assert.equal(instances.length, action === 'new-session' ? 2 : 1);
  });
}

test('取消 beforeinput 时不会永远暂停写回；旧临时前缀被引擎改写不覆盖草稿', () => {
  const { input, instances, button, edit, runTimers } = setup(); button.click(); const rec = instances[0];
  rec.result(['临时结果', false]); input.emit('beforeinput'); runTimers();
  rec.result(['临时结果继续', true]); assert.equal(input.value, '临时结果继续');
  input.emit('beforeinput'); edit('我修改过'); rec.result(['引擎改写旧前缀', true], ['新话语', true]); runTimers();
  assert.equal(input.value, '我修改过新话语'); assert.equal(button.attributes['aria-pressed'], 'true');
});

test('自然断开后自动续听，键盘修改保留且旧结果不能覆盖', () => {
  const x = setup();
  const { input, instances, button, edit } = x; button.click();
  const old = instances[0]; old.result(['第一段', true]); old.onend();
  assert.equal(button.attributes['data-speech-state'], 'starting');
  edit('修改后'); x.advanceTime(300); assert.equal(instances.length, 2);
  old.result(['旧结果', true]); old.onend(); instances[1].result(['第二段', true]);
  assert.equal(input.value, '修改后第二段'); assert.equal(button.attributes['aria-pressed'], 'true');
});

test('英文跨自动续听保留词边界', () => {
  const x = setup(); x.button.click(); x.instances[0].result(['Hello', true]); x.instances[0].onend();
  x.advanceTime(300); x.instances[1].result(['world', true]); assert.equal(x.input.value, 'Hello world');
});

test('服务确认启动前显示等待态，取消或超时后不残留假录音状态', () => {
  const x = setup('草稿', { hangStart: true }); x.button.click();
  assert.equal(x.button.attributes['data-speech-state'], 'starting');
  assert.equal(x.button.attributes['aria-busy'], 'true');
  assert.equal(x.button.classes.has('is-recording'), false);
  x.advanceTime(30000);
  assert.equal(x.button.attributes['data-speech-state'], 'idle');
  assert.equal(x.button.attributes['aria-busy'], 'false');
  assert.equal(x.input.value, '草稿'); assert.equal(x.instances[0].aborted, true);
  assert.ok(x.messages.at(-1).includes('启动超时')); assert.equal(x.timers.size, 0);
  x.advanceTime(600000); assert.equal(x.instances.length, 1);
});

test('audiostart 或 result 确认识别可用，清除启动超时', () => {
  for (const event of ['onstart', 'onaudiostart', 'result']) {
    const x = setup('', { hangStart: true }); x.button.click(); const rec = x.instances[0];
    if (event === 'result') rec.result(['测试', true]); else rec[event]();
    assert.equal(x.button.attributes['data-speech-state'], 'listening');
    assert.equal(x.button.classes.has('is-recording'), true);
    x.advanceTime(30001); assert.equal(x.button.attributes['aria-pressed'], 'true');
    assert.equal(rec.aborted, undefined); assert.equal(x.timers.size, 0);
  }
});

test('停止后立即重开须等旧实例 end，再开始新的识别', () => {
  const x = setup('', { manualEnd: true }); x.button.click(); const old = x.instances[0];
  old.result(['第一段', true]); x.button.click(); assert.equal(old.stopped, true);
  x.button.click();
  assert.equal(old.aborted, true); assert.equal(x.instances.length, 1);
  assert.equal(x.button.attributes['data-speech-state'], 'waiting');
  old.result(['旧段迟到', true]); assert.equal(x.input.value, '第一段');
  old.onend(); assert.equal(x.instances.length, 2);
  assert.equal(x.button.attributes['data-speech-state'], 'listening');
  x.instances[1].result(['第二段', true]); assert.equal(x.input.value, '第一段第二段');
  old.onend(); old.onerror({ error: 'aborted' }); old.onstart();
  assert.equal(x.button.attributes['data-speech-state'], 'listening'); assert.equal(x.instances.length, 2);
});

for (const action of ['cancel', 'send', 'hidden', 'switch-language']) {
  test(`等旧实例释放期间 ${action} 取消启动，不迟到开启麦克风`, () => {
    const x = setup('', { manualEnd: true }); x.button.click(); const old = x.instances[0];
    x.button.click(); x.button.click();
    if (action === 'cancel') x.button.click();
    else if (action === 'hidden') { x.document.hidden = true; x.document.emit('visibilitychange'); }
    else if (action === 'switch-language') x.language('en-US');
    else x.context.App.stopVoiceInput();
    old.onend(); x.advanceTime(60000);
    assert.equal(x.instances.length, 1); assert.equal(x.button.attributes['aria-pressed'], 'false');
    assert.equal(x.timers.size, 0);
  });
}

test('旧服务不发 end 时等待超时后兑现点击，迟到 end 不干扰新识别', () => {
  const x = setup('', { manualEnd: true }); x.button.click(); const old = x.instances[0];
  x.button.click(); x.button.click(); x.advanceTime(2000);
  assert.equal(x.instances.length, 2); assert.equal(x.button.attributes['data-speech-state'], 'listening');
  old.onend(); assert.equal(x.instances.length, 2);
  x.instances[1].result(['恢复听写', true]); assert.equal(x.input.value, '恢复听写');
});

test('报错后缺失 end 也不会永久卡住再次点击听写', () => {
  const x = setup('草稿', { manualEnd: true }); x.button.click(); const old = x.instances[0];
  old.onerror({ error: 'network' });
  assert.equal(x.button.attributes['data-speech-state'], 'idle');
  x.button.click(); assert.equal(x.button.attributes['data-speech-state'], 'waiting');
  x.advanceTime(2000);
  assert.equal(x.instances.length, 2);
  x.instances[1].result(['继续', true]);
  assert.equal(x.input.value, '草稿继续');
  old.onend(); assert.equal(x.button.attributes['data-speech-state'], 'listening');
});

for (const action of ['stop', 'send', 'hidden']) {
  test(`自动续听等待期间 ${action} 可取消，草稿保留且不再开启识别`, () => {
    const x = setup(); x.button.click(); const old = x.instances[0];
    old.result(['已听写', true]); old.onend();
    if (action === 'stop') x.button.click();
    else if (action === 'send') x.context.App.stopVoiceInput();
    else { x.document.hidden = true; x.document.emit('visibilitychange'); }
    x.advanceTime(60000);
    assert.equal(x.instances.length, 1);
    assert.equal(x.input.value, '已听写');
    assert.equal(x.button.attributes['data-speech-state'], 'idle');
  });
}

test('连续多段自然断开后自动续听，不继承旧结果或输入法缓存', () => {
  const x = setup(); let expected = '';
  x.button.click();
  for (let i = 1; i <= 12; i++) {
    const rec = x.instances.at(-1);
    rec.result([String(i), true]); expected += (i > 1 ? ' ' : '') + i;
    assert.equal(x.input.value, expected);
    rec.onend(); assert.equal(x.button.attributes['data-speech-state'], 'starting');
    rec.result(['旧结果', true]); x.advanceTime(300);
    assert.equal(x.input.value, expected); assert.equal(x.instances.length, i + 1);
  }
  x.button.click(); assert.equal(x.button.attributes['data-speech-state'], 'idle');
});

test('输入法期间自然断开，提交后自动续听且旧事件不阻塞新语音', () => {
  const x = setup(); x.button.click(); const old = x.instances[0];
  x.input.emit('compositionstart'); x.edit('用户草稿'); old.result(['旧缓存', true]); old.onend();
  x.advanceTime(300); assert.equal(x.instances.length, 1);
  x.input.emit('compositionend'); x.runTimers(); x.advanceTime(300);
  x.instances[1].result(['新一段', true]); assert.equal(x.input.value, '用户草稿旧缓存新一段');
  assert.equal(x.button.attributes['data-speech-state'], 'listening'); assert.equal(x.timers.size, 0);
});

test('空 transcript 和 nomatch 提示但不抹掉可见草稿，之后仍可写入正常结果', () => {
  const x = setup('原稿'); x.button.click(); const rec = x.instances[0];
  rec.result(['临时语音', false]); rec.result(['', true]); rec.onnomatch();
  assert.equal(x.input.value, '原稿临时语音'); assert.equal(x.messages.length, 1);
  rec.result(['最终语音', true]); assert.equal(x.input.value, '原稿最终语音');
  assert.equal(x.button.attributes['data-speech-state'], 'listening');
});

test('同步 start 抛错不占用资源，随后一次显式点击可立即启动', () => {
  const options = { startError: true }; const x = setup('', options); x.button.click();
  options.startError = false; x.button.click(); x.instances[1].result(['成功', true]);
  assert.equal(x.input.value, '成功'); assert.equal(x.button.attributes['data-speech-state'], 'listening');
});

test('连续空白断开只有限次续听，结束后点击仍能听写', () => {
  const x = setup('保留原稿'); x.button.click();
  for (let i = 0; i < 2; i++) {
    x.instances[i].onend(); x.advanceTime(300);
    assert.equal(x.instances.length, i + 2);
  }
  x.instances[2].onend();
  assert.equal(x.button.attributes['data-speech-state'], 'idle');
  assert.equal(x.timers.size, 0);
  x.button.click(); x.instances[3].result(['新语音', true]);
  assert.equal(x.input.value, '保留原稿新语音'); assert.equal(x.button.attributes['data-speech-state'], 'listening');
});

for (const error of ['no-speech', 'aborted', 'network', 'audio-capture', 'not-allowed', 'service-not-allowed']) {
  test(`${error} 后停止并复位，无延迟重启/权限重试`, () => {
    const { input, instances, button, timers, messages, runTimers } = setup(); button.click(); const rec = instances[0];
    rec.result(['保留草稿', false]); rec.onerror({ error }); rec.onend(); runTimers();
    assert.equal(input.value, '保留草稿'); assert.equal(instances.length, 1);
    assert.equal(button.attributes['aria-pressed'], 'false'); assert.equal(timers.size, 0);
    if (error !== 'no-speech' && error !== 'aborted') assert.equal(messages.length, 1);
  });
}

for (const options of [{ startError: true }, { constructorError: true }, { syncError: 'not-allowed' }]) {
  test(`启动失败不自动重试，按钮不残留录音态（${JSON.stringify(options)}）`, () => {
    const { instances, button, timers, messages, runTimers } = setup('', options); button.click(); runTimers();
    assert.equal(button.attributes['aria-pressed'], 'false'); assert.equal(button.classes.has('is-recording'), false);
    assert.equal(timers.size, 0); assert.ok(instances.length <= 1); assert.equal(messages.length, 1);
  });
}

for (const action of ['send', 'pagehide', 'hidden']) {
  test(`${action} 隔离识别器，迟到事件不回填或重启`, () => {
    const { context, input, instances, button, window, document, runTimers } = setup('', { syncEnd: true });
    button.click(); const old = instances[0]; old.result(['发出的消息', false]);
    if (action === 'send') context.App.stopVoiceInput();
    else if (action === 'pagehide') window.emit('pagehide');
    else { document.hidden = true; document.emit('visibilitychange'); }
    input.value = ''; old.result(['迟到消息', true]); old.onend(); runTimers();
    assert.equal(input.value, ''); assert.equal(old.aborted, true); assert.equal(instances.length, 1);
    assert.equal(button.attributes['aria-pressed'], 'false');
  });
}
