const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');
const plain = value => JSON.parse(JSON.stringify(value));

function setup(t, schema, values = {}) {
  const dom = new JSDOM('<body></body>', { runScripts: 'outside-only' }); t.after(() => dom.window.close());
  const w = dom.window; w.App = {};
  w.eval(fs.readFileSync(path.join(__dirname, '../js/app/mcp_schema_form.js'), 'utf8'));
  const api = w.App.mcpSchemaForm, form = api.create(schema, values); w.document.body.append(form.element);
  const field = (...parts) => Array.from(form.element.querySelectorAll('.mcp-param-field')).find(el => el.dataset.paramPath === JSON.stringify(parts));
  const control = (...parts) => field(...parts).querySelector('.mcp-param-value').firstElementChild;
  function fill(parts, value) { const input = control(...parts); input.value = value; input.dispatchEvent(new w.Event('input', { bubbles: true })); return input; }
  return { w, api, form, field, control, fill };
}

test('fields keep exact primitive types, omit unused options and include edited optional values', t => {
  const s = setup(t, { type: 'object', properties: { text: { type: 'string', description: '输入正文' }, count: { type: 'integer' }, enabled: { type: 'boolean' }, unused: { type: 'string' } }, required: ['text'] });
  assert.match(s.field('text').textContent, /必填/); assert.match(s.field('text').textContent, /输入正文/);
  assert.deepEqual(plain(s.form.read()), { text: '' });
  s.fill(['count'], '0'); s.fill(['enabled'], '1'); // false, not the string "false"
  assert.deepEqual(plain(s.form.read()), { text: '', count: 0, enabled: false });
  const include = s.field('unused').querySelector('input[type=checkbox]'); include.checked = true;
  assert.deepEqual(plain(s.form.read()), { text: '', count: 0, enabled: false, unused: '' });
  include.checked = false;
  assert.equal(Object.hasOwn(s.form.read(), 'unused'), false);
});

test('nested objects, local refs, nullable values and arrays preserve their structure', t => {
  const schema = { type: 'object', $defs: { 'escaped/key': { type: 'integer' } }, properties: {
    options: { type: 'object', properties: { retries: { $ref: '#/$defs/escaped~1key' } }, required: ['retries'] },
    limit: { anyOf: [{ type: 'number' }, { type: 'null' }] }, items: { type: 'array', items: { type: 'string' } },
  }, required: ['options'] };
  const s = setup(t, schema, { options: { retries: 0, extension: true }, limit: null, items: ['中文', ''] });
  assert.equal(s.control('options', 'retries').type, 'number');
  assert.deepEqual(plain(s.form.read()), { options: { retries: 0, extension: true }, limit: null, items: ['中文', ''] });
  const nullBox = s.field('limit').querySelectorAll('input[type=checkbox]')[1]; nullBox.checked = false; nullBox.dispatchEvent(new s.w.Event('change'));
  s.fill(['limit'], '1.25');
  assert.equal(s.form.read().limit, 1.25);
});

test('enum options retain typed values and invalid existing values stay editable without coercion', t => {
  const schema = { type: 'object', properties: { mode: { enum: [0, false, '', null, { a: 1 }] }, count: { type: 'integer' }, flag: { type: 'boolean' } } };
  const s = setup(t, schema, { mode: false, count: 'not a number', flag: null });
  assert.equal(s.control('mode').tagName, 'SELECT');
  s.fill(['mode'], '4');
  assert.deepEqual(plain(s.form.read()), { mode: { a: 1 }, count: 'not a number', flag: null });
  assert.equal(s.control('count').tagName, 'TEXTAREA');
});

test('unfinished required fields can open JSON, while malformed fields and incomplete calls are rejected', t => {
  const s = setup(t, { type: 'object', properties: { count: { type: 'integer' }, enabled: { type: 'boolean' }, data: { type: 'array' } }, required: ['count', 'enabled', 'data'] });
  assert.deepEqual(plain(s.form.read({ partial: true })), {});
  assert.throws(() => s.form.read(), error => /整数/.test(error.message) && error.control === s.control('count'));
  s.fill(['count'], '2.5'); assert.throws(() => s.form.read(), /整数/);
  s.fill(['count'], '2'); s.fill(['enabled'], '0'); s.fill(['data'], '[broken]');
  assert.throws(() => s.form.read({ partial: true }), /有效 JSON/);
  s.fill(['data'], '[false,0]');
  assert.deepEqual(plain(s.form.read()), { count: 2, enabled: true, data: [false, 0] });
});

test('unknown keys and prototype-looking property names survive without prototype pollution or HTML insertion', t => {
  const schema = JSON.parse('{"type":"object","additionalProperties":false,"properties":{"__proto__":{"type":"string"},"<img src=x>":{"type":"string"}}}');
  const values = JSON.parse('{"__proto__":"value","<img src=x>":"<script>","constructor":{"safe":true},"extra":false}');
  const s = setup(t, schema, values);
  assert.deepEqual(plain(s.form.read()), values);
  assert.equal(s.form.element.querySelector('img,script'), null);
  assert.equal(Object.prototype.safe, undefined);
  const extra = s.form.element.querySelector('.mcp-param-extra textarea'); extra.value = '{"__proto__":"duplicate"}';
  assert.throws(() => s.form.read(), /已列出的字段/);
});

test('advanced field schemas use JSON; advanced roots decline forms; recursive refs stay bounded', t => {
  const schema = { type: 'object', properties: { choice: { oneOf: [{ type: 'integer' }, { type: 'string' }] }, remote: { $ref: 'https://example.invalid/schema' }, child: { $ref: '#/$defs/child' } },
    $defs: { child: { type: 'object', properties: { next: { $ref: '#/$defs/child' } } } } };
  const s = setup(t, schema, { choice: 2, remote: { x: true } });
  assert.equal(s.control('choice').tagName, 'TEXTAREA');
  assert.equal(s.control('remote').tagName, 'TEXTAREA');
  assert.deepEqual(plain(s.form.read()), { choice: 2, remote: { x: true } });
  assert.ok(s.form.element.querySelectorAll('.mcp-param-field').length < 10);
  assert.equal(s.api.supported({ oneOf: [{ type: 'object' }, { type: 'array' }] }), false);
  assert.equal(s.api.supported({ type: 'object', allOf: [{ required: ['x'] }] }), false);
});

test('redacted defaults are never sent as values; examples resolve refs and only contain required fields', t => {
  const schema = { type: 'object', $defs: { count: { type: 'integer', minimum: 2 } }, properties: { token: { type: 'string', default: '[REDACTED]' }, count: { $ref: '#/$defs/count' }, enabled: { type: 'boolean' } }, required: ['count', 'enabled'] };
  const s = setup(t, schema);
  assert.deepEqual(plain(s.api.sample(schema)), { count: 2, enabled: false });
  assert.equal(s.control('token').value, '');
  s.fill(['count'], '2'); s.fill(['enabled'], '1');
  assert.deepEqual(plain(s.form.read()), { count: 2, enabled: false });
});
