const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function setup() {
  let document;
  function node(id, classes = '') {
    const names = new Set(classes.split(' ').filter(Boolean));
    return { id, tagName: 'DIV', inert: false, isConnected: true, disabled: false, hidden: false,
      tabIndex: 0, value: '', scrollTop: 0, attrs: {}, handlers: {}, children: [],
      classList: { add: s => names.add(s), remove: s => names.delete(s), contains: s => names.has(s) },
      style: { values: {}, setProperty(k, v) { this.values[k] = v; } },
      setAttribute(k, v) { this.attrs[k] = v; }, getAttribute(k) { return this.attrs[k]; },
      addEventListener(k, fn) { this.handlers[k] = fn; },
      contains(n) { return this === n || this.children.some(c => c.contains(n)); },
      getClientRects() { return this.hidden ? [] : [{}]; },
      closest(selector) { return selector === '[role="tabpanel"]' ? this.panel : this.inert ? this : null; },
      focus() { document.activeElement = this; }, querySelectorAll() { return this.children; },
    };
  }
  const body = node('body'), background = node('background'), alreadyInert = node('other-modal');
  alreadyInert.inert = true;
  const modal = node('modal', 'hidden'), content = node('content'), nav = node('nav');
  const tabs = [0, 1, 2, 3].map(i => node('tab' + i));
  const panels = tabs.map((tab, i) => { tab.attrs['aria-controls'] = 'panel' + i; return node('panel' + i); });
  const close = node('close'), confirm = node('confirm'), cancel = node('cancel'), reset = node('reset'), launch = node('launch');
  const field = node('subAgentMaxRounds'); field.panel = panels[2]; field.value = '42';
  nav.children = tabs;
  modal.children = [close, ...tabs, field, reset, cancel, confirm];
  modal.querySelector = selector => selector === '.chat-settings-body' ? content : nav;
  body.children = [background, modal, alreadyInert];
  document = { body, activeElement: launch, handlers: {},
    getElementById(id) { return [...panels, field].find(n => n.id === id); },
    addEventListener(k, fn) { this.handlers[k] = fn; },
  };
  const viewport = { height: 700, offsetTop: 0, handlers: {}, addEventListener(k, fn) { this.handlers[k] = fn; } };
  const App = { chatSettingsModal: modal, chatSettingsClose: close, chatSettingsConfirm: confirm,
    chatSettingsCancel: cancel, chatSettingsReset: reset, plusBtn: launch };
  const window = { App, visualViewport: viewport, addEventListener() {} };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../js/app/chat_settings_modal.js'), 'utf8'), { window, document });
  return { App, body, modal, content, nav, tabs, panels, close, confirm, reset, cancel, launch,
    field, background, alreadyInert, document, viewport };
}

test('设置切页保留字段草稿与各页滚动位置，方向键循环及 Home/End 导航', () => {
  const x = setup(); x.App.showChatSettingsModal();
  x.content.scrollTop = 140;
  x.tabs[2].handlers.click();
  assert.deepEqual(x.panels.map(p => p.hidden), [true, true, false, true]);
  x.content.scrollTop = 90; x.tabs[0].handlers.click();
  assert.equal(x.content.scrollTop, 140);
  x.tabs[2].handlers.click(); assert.equal(x.content.scrollTop, 90);
  assert.equal(x.field.value, '42');
  const key = (index, value) => x.tabs[index].handlers.keydown({ key: value, preventDefault() {} });
  key(2, 'End'); assert.equal(x.document.activeElement, x.tabs[3]);
  key(3, 'ArrowRight'); assert.equal(x.document.activeElement, x.tabs[0]);
  key(0, 'ArrowLeft'); assert.equal(x.document.activeElement, x.tabs[3]);
  key(3, 'Home'); assert.equal(x.document.activeElement, x.tabs[0]);
  assert.deepEqual(x.tabs.map(t => t.tabIndex), [0, -1, -1, -1]);
});

test('隔离背景、恢复焦点与原有 inert，适配软键盘可视区域', () => {
  const x = setup(); x.App.showChatSettingsModal(); x.App.showChatSettingsModal();
  assert.equal(x.background.inert, true);
  assert.equal(x.body.classList.contains('chat-settings-open'), true);
  assert.equal(x.document.activeElement, x.close);
  x.viewport.height = 290; x.viewport.offsetTop = 40; x.viewport.handlers.resize();
  assert.equal(x.modal.style.values['--settings-viewport-height'], '290px');
  assert.equal(x.modal.style.values['--settings-viewport-top'], '40px');
  x.App.hideChatSettingsModal();
  assert.equal(x.background.inert, false); assert.equal(x.alreadyInert.inert, true);
  assert.equal(x.body.classList.contains('chat-settings-open'), false);
  assert.equal(x.document.activeElement, x.launch);
});

test('保存时锁定编辑、关闭与恢复默认，解锁后保留原先禁用的字段', () => {
  const x = setup(); x.field.disabled = true;
  x.App.setChatSettingsBusy(true);
  assert.equal(x.App.chatSettingsSaving, true);
  assert.equal(x.content.inert, true); assert.equal(x.nav.inert, true);
  assert.equal(x.close.disabled, true); assert.equal(x.reset.disabled, true);
  assert.equal(x.confirm.textContent, '保存中…');
  x.App.setChatSettingsBusy(false);
  assert.equal(x.content.inert, false); assert.equal(x.cancel.disabled, false);
  assert.equal(x.field.disabled, true); assert.equal(x.confirm.textContent, '确定');
});

test('校验错误切到隐藏字段；Tab 在弹窗循环，输入法期间 Esc 不关闭', () => {
  const x = setup(); x.App.showChatSettingsModal();
  x.App.revealInvalidChatSetting('子智能体轮次上限');
  assert.equal(x.panels[2].hidden, false); assert.equal(x.document.activeElement, x.field);
  x.confirm.focus();
  x.modal.handlers.keydown({ key: 'Tab', preventDefault() {} });
  assert.equal(x.document.activeElement, x.close);
  x.modal.handlers.keydown({ key: 'Tab', shiftKey: true, preventDefault() {} });
  assert.equal(x.document.activeElement, x.confirm);
  let closes = 0;
  x.App.closeChatSettings = () => closes++;
  x.modal.handlers.keydown({ key: 'Escape', isComposing: true });
  assert.equal(closes, 0);
  x.modal.handlers.keydown({ key: 'Escape', preventDefault() {}, stopPropagation() {} });
  assert.equal(closes, 1);
});
