const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');

const source = fs.readFileSync(path.join(__dirname, '../js/theme.js'), 'utf8');

function setup({ stored = null, search = '' } = {}) {
  const dom = new JSDOM('', { url: 'https://example.test/' + search, runScripts: 'outside-only' });
  const w = dom.window;
  w.matchMedia = () => ({ matches: true, addEventListener() {} });
  if (stored != null) w.localStorage.setItem('ytools-accent-preference', stored);
  w.eval(source);
  return { w, close: () => w.close() };
}

test('accent defaults to blue and is applied to <html data-accent>', t => {
  const x = setup(); t.after(x.close);
  assert.equal(x.w.ThemeManager.getAccentPreference(), 'blue');
  assert.equal(x.w.document.documentElement.dataset.accent, 'blue');
});

test('stored accent is restored; invalid values fall back to blue', t => {
  const ok = setup({ stored: 'purple' }); t.after(ok.close);
  assert.equal(ok.w.ThemeManager.getAccentPreference(), 'purple');
  assert.equal(ok.w.document.documentElement.dataset.accent, 'purple');
  const bad = setup({ stored: 'orange' }); t.after(bad.close);
  assert.equal(bad.w.ThemeManager.getAccentPreference(), 'blue');
  assert.equal(bad.w.document.documentElement.dataset.accent, 'blue');
});

test('setAccentPreference persists, applies and notifies; invalid input ignored', t => {
  const x = setup(); t.after(x.close);
  const events = [];
  x.w.document.addEventListener('accentchange', e => events.push(e.detail));
  x.w.ThemeManager.setAccentPreference('green');
  assert.equal(x.w.localStorage.getItem('ytools-accent-preference'), 'green');
  assert.equal(x.w.document.documentElement.dataset.accent, 'green');
  assert.deepEqual(events.map(e => e.accent), ['green']);
  x.w.ThemeManager.setAccentPreference('orange');
  assert.equal(x.w.ThemeManager.getAccentPreference(), 'green');
  assert.equal(events.length, 1);
});

test('?accent= preview applies without persisting', t => {
  const x = setup({ search: '?accent=pink' }); t.after(x.close);
  assert.equal(x.w.document.documentElement.dataset.accent, 'pink');
  assert.equal(x.w.localStorage.getItem('ytools-accent-preference'), null);
  assert.equal(x.w.ThemeManager.getAccentPreference(), 'blue');
});
