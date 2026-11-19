const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { stripTypeScriptTypes } = require('node:module');

// 契约回归：后端追赶期把多帧拼进一个 SSE chunk 下发（长任务切回会话的吞吐优化），
// 前端必须逐行解析出全部帧，且跨 chunk 截断的帧要能拼回。此前每帧一个 chunk，
// 该路径未被覆盖；若解析退化为"一个 chunk 只取首帧"，回放会静默丢内容。
function loadReader() {
  const src = stripTypeScriptTypes(fs.readFileSync(path.join(__dirname, '../src/api.ts'), 'utf8'));
  const start = src.indexOf('  async function readSseResponse(');
  assert.ok(start >= 0, '应存在 readSseResponse');
  const end = src.indexOf('\n  }', src.indexOf('finally {', start)) + 4;
  // 注入宿主 JSON：vm 内解析出的对象与断言同 realm，deepEqual 才能比较
  const ctx = { TextDecoder, console, JSON };
  vm.createContext(ctx);
  vm.runInContext(src.slice(start, end), ctx);
  return ctx.readSseResponse;
}

// 伪 Response：按给定分块喂字节，模拟网络分片
function fakeResponse(chunks) {
  const encoder = new TextEncoder();
  let i = 0;
  return {
    body: {
      getReader() {
        return {
          async read() {
            if (i >= chunks.length) return { done: true, value: undefined };
            return { done: false, value: encoder.encode(chunks[i++]) };
          },
          async cancel() {},
          releaseLock() {},
        };
      },
    },
  };
}

const frame = obj => 'data: ' + JSON.stringify(obj) + '\n\n';

test('一个 chunk 含多帧：全部按序派发（批量下发契约）', async () => {
  const readSseResponse = loadReader();
  const batch = frame({ content: 'a' }) + frame({ content: 'b' }) + frame({ tool_return: { function_name: 'f', result: 'ok' } });
  const got = [];
  const res = await readSseResponse(fakeResponse([batch, 'data: [DONE]\n\n']), evt => {
    if (evt.type === 'data') got.push(evt.data);
  });
  assert.equal(res.completed, true);
  assert.deepEqual(got, [{ content: 'a' }, { content: 'b' }, { tool_return: { function_name: 'f', result: 'ok' } }]);
});

test('帧跨 chunk 截断：拼回后完整派发，不丢尾帧', async () => {
  const readSseResponse = loadReader();
  const whole = frame({ content: '前半' }) + frame({ content: '后半' });
  const cut = whole.indexOf('后半');
  const got = [];
  await readSseResponse(fakeResponse([whole.slice(0, cut), whole.slice(cut), 'data: [DONE]\n\n']), evt => {
    if (evt.type === 'data') got.push(evt.data);
  });
  assert.deepEqual(got, [{ content: '前半' }, { content: '后半' }]);
});

test('心跳注释与空行混在批量帧中被忽略', async () => {
  const readSseResponse = loadReader();
  const batch = ': ping\n\n' + frame({ content: 'x' }) + '\n' + frame({ content: 'y' });
  const got = [];
  await readSseResponse(fakeResponse([batch, 'data: [DONE]\n\n']), evt => {
    if (evt.type === 'data') got.push(evt.data);
  });
  assert.deepEqual(got, [{ content: 'x' }, { content: 'y' }]);
});

test('replay marker / replay_end 与批量帧同 chunk 时顺序不变', async () => {
  const readSseResponse = loadReader();
  const batch = frame({ replay: true, round: 7 }) + frame({ content: '回放' }) + frame({ replay_end: true }) + frame({ content: '实时' });
  const got = [];
  await readSseResponse(fakeResponse([batch, 'data: [DONE]\n\n']), evt => {
    if (evt.type === 'data') got.push(evt.data);
  });
  assert.deepEqual(got.map(e => Object.keys(e)[0]), ['replay', 'content', 'replay_end', 'content']);
  assert.equal(got[1].content, '回放');
  assert.equal(got[3].content, '实时');
});

test('CJK 多字节字符被网络分片切断也能正确解码', async () => {
  const readSseResponse = loadReader();
  const bytes = new TextEncoder().encode(frame({ content: '中文分片测试' }) + 'data: [DONE]\n\n');
  const mid = 20; // 落在多字节字符中间
  const got = [];
  await readSseResponse({
    body: { getReader() {
      const parts = [bytes.slice(0, mid), bytes.slice(mid)];
      let i = 0;
      return { async read() { return i < parts.length ? { done: false, value: parts[i++] } : { done: true }; },
        async cancel() {}, releaseLock() {} };
    } },
  }, evt => { if (evt.type === 'data') got.push(evt.data); });
  assert.deepEqual(got, [{ content: '中文分片测试' }]);
});
