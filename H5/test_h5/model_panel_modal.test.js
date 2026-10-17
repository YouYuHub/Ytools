const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function setup(locked = false) {
  let document;
  function node(name, classes = '') {
    const set = new Set(classes.split(' ').filter(Boolean));
    return { name, tagName: 'DIV', inert: false, isConnected: true, tabIndex: 0,
      attrs: {}, styles: {}, handlers: {}, children: [], visible: true,
      classList: { add: n => set.add(n), remove: n => set.delete(n), contains: n => set.has(n) },
      style: { setProperty(key, value) { this.values[key] = value; }, values: {} },
      setAttribute(key, value) { this.attrs[key] = value; },
      addEventListener(key, fn) { this.handlers[key] = fn; },
      contains(target) { return this === target || this.children.some(n => n.contains(target)); },
      querySelectorAll() { return this.children; },
      closest() { return this.inert ? this : null; },
      getClientRects() { return this.visible ? [{}] : []; },
      focus() { document.activeElement = this; },
    };
  }
  const body = node('body', locked ? 'model-settings-open' : '');
  const app = node('app'), modal = node('modal', 'hidden'), panel = node('panel', 'hidden');
  const boost = node('boost'), close = node('close'), confirm = node('confirm');
  const alreadyInert = node('other-modal'); alreadyInert.inert = true;
  app.children = [boost]; panel.children = [close, confirm]; modal.children = [panel];
  body.children = [app, modal, alreadyInert];
  document = { body, activeElement: boost, handlers: {}, addEventListener(key, fn) { this.handlers[key] = fn; } };
  const viewport = { height: 720, offsetTop: 0, handlers: {}, addEventListener(key, fn) { this.handlers[key] = fn; } };
  const App = { enhanceModal: modal, enhancePanel: panel, enhanceClose: close, boostBtn: boost };
  const window = { App, innerHeight: 800, visualViewport: viewport, addEventListener() {} };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../js/app/model_panel_modal.js'), 'utf8'), { window, document });
  return { App, document, app, body, modal, panel, boost, close, confirm, viewport, alreadyInert, node };
}

test('模态框打开隔离背景、锁定滚动；重复打开后仍恢复原焦点和 inert', () => {
  const x = setup();
  x.App.showModelPanelModal();
  assert.equal(x.modal.classList.contains('hidden'), false);
  assert.equal(x.panel.classList.contains('hidden'), false);
  assert.equal(x.modal.attrs['aria-hidden'], 'false');
  assert.equal(x.boost.attrs['aria-expanded'], 'true');
  assert.equal(x.app.inert, true);
  assert.equal(x.document.activeElement, x.close);
  x.App.showModelPanelModal();
  x.App.hideModelPanelModal();
  assert.equal(x.modal.classList.contains('hidden'), true);
  assert.equal(x.panel.classList.contains('hidden'), true);
  assert.equal(x.modal.attrs['aria-hidden'], 'true');
  assert.equal(x.boost.attrs['aria-expanded'], 'false');
  assert.equal(x.app.inert, false);
  assert.equal(x.alreadyInert.inert, true);
  assert.equal(x.body.classList.contains('model-settings-open'), false);
  assert.equal(x.document.activeElement, x.boost);
});

test('关闭时保留打开前已有的滚动锁', () => {
  const x = setup(true);
  x.App.showModelPanelModal(); x.App.hideModelPanelModal();
  assert.equal(x.body.classList.contains('model-settings-open'), true);
});

test('手机键盘改变可视窗口时调整模态框高度和偏移，不依赖输入框位置', () => {
  const x = setup();
  x.App.showModelPanelModal();
  assert.equal(x.modal.style.values['--model-viewport-height'], '720px');
  x.viewport.height = 340; x.viewport.offsetTop = 38;
  x.viewport.handlers.resize();
  assert.equal(x.modal.style.values['--model-viewport-height'], '340px');
  assert.equal(x.modal.style.values['--model-viewport-top'], '38px');
  x.App.hideModelPanelModal();
  x.viewport.height = 720; x.viewport.handlers.resize();
  assert.equal(x.modal.style.values['--model-viewport-height'], '340px');
});

test('Tab 循环在可用控件之间，跳过隐藏和禁用控件', () => {
  const x = setup(); x.App.showModelPanelModal();
  const hidden = x.node('hidden'); hidden.visible = false;
  const disabled = x.node('disabled'); disabled.disabled = true;
  x.panel.children.push(hidden, disabled);
  let prevented = 0;
  x.confirm.focus();
  x.modal.handlers.keydown({ key: 'Tab', preventDefault() { ++prevented; } });
  assert.equal(x.document.activeElement, x.close);
  x.modal.handlers.keydown({ key: 'Tab', shiftKey: true, preventDefault() { ++prevented; } });
  assert.equal(x.document.activeElement, x.confirm);
  assert.equal(prevented, 2);
});

test('Esc 使用统一关闭入口；输入法组合中不关闭，点击遮罩不丢弃草稿', () => {
  const x = setup(); x.App.showModelPanelModal();
  let closes = 0, stopped = 0;
  x.App.closeModelPanel = () => { ++closes; x.App.hideModelPanelModal(); };
  x.modal.handlers.keydown({ key: 'Escape', isComposing: true });
  x.modal.handlers.click({ stopPropagation() { ++stopped; } });
  assert.equal(closes, 0);
  assert.equal(x.modal.classList.contains('hidden'), false);
  x.modal.handlers.keydown({ key: 'Escape', preventDefault() {}, stopPropagation() { ++stopped; } });
  assert.equal(closes, 1);
  assert.equal(stopped, 2);
});

test('打开期间意外移到背景的焦点返回弹窗；关闭后不再拦截', () => {
  const x = setup(); x.App.showModelPanelModal();
  x.boost.focus(); x.document.handlers.focusin({ target: x.boost });
  assert.equal(x.document.activeElement, x.close);
  x.App.hideModelPanelModal();
  x.document.handlers.focusin({ target: x.boost });
  assert.equal(x.document.activeElement, x.boost);
});
