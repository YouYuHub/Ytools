const test = require('node:test');
const assert = require('node:assert/strict');
const Model = require('../js/history_scroll_model');

test('global height lookup handles empty/zero-height dependent records and the final boundary', () => {
  const offsets = Model.prefix([100,0,0,200,50]);
  assert.deepEqual(Array.from(offsets),[0,100,100,100,300,350]);
  assert.equal(Model.locate(offsets,-10),0);
  assert.equal(Model.locate(offsets,100),3);
  assert.equal(Model.locate(offsets,299),3);
  assert.equal(Model.locate(offsets,10000),4);
  assert.equal(Model.locate(Model.prefix([]),0),0);
});

test('height estimates use text rather than binary attachment payloads and avoid duplicating tool results', () => {
  assert.equal(Model.estimate({kind:'toolResult',content:'large result'},800),0);
  assert.equal(Model.estimate({kind:'approval'},800),0);
  const text = [{type:'text',text:'hello'}];
  assert.equal(Model.estimate({kind:'user',content:[...text,{type:'image_url',image_url:'x'}]},800),Model.estimate({kind:'user',content:[...text,{type:'image_url',image_url:'x'.repeat(100000)}]},800));
  assert.ok(Model.estimate({kind:'assistant',content:'long text '.repeat(500)},300)>Model.estimate({kind:'assistant',content:'long text '.repeat(500)},1000));
});

test('frozen placement keeps total scroll extent fixed while the actual mounted window changes', () => {
  for (const height of [1000,5000,9000]) {
    const fitted = Model.fit(20000,64,160,height,7000);
    assert.equal(fitted.before+height+fitted.after+64+160,20000);
    assert.equal(fitted.shift,0);
  }
  const head = Model.fit(20000,64,160,3000,0);
  const tail = Model.fit(20000,64,160,3000,20000);
  assert.equal(head.before,0);
  assert.equal(tail.after,0);
});

test('an oversized single message can show its tail without changing the native drag range', () => {
  const fitted = Model.fit(2000,64,160,10000,2000-64-160-10000);
  assert.equal(fitted.before,0);
  assert.equal(fitted.after,0);
  assert.equal(64+10000+fitted.shift,2000-160);
});
