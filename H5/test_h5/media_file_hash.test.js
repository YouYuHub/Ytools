const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const nodeCrypto = require('node:crypto');

// 媒体文件哈希（粘贴/选择去重）：prepareMediaFile 给条目附 hash（最终上传字节），
// 各暂存点（主输入框/编辑面板/询问弹窗）凭 hash 判重。SHA-256 优先，非安全
// 上下文降级 FNV-1a；超大文件跳过哈希（永不判重，不阻塞添加）。
const source = () => fs.readFileSync(path.join(__dirname, '../js/app/media.js'), 'utf8');

function harness({ crypto } = {}) {
  const src = source();
  const toasts = [];
  const context = {
    Array, Object, Math, String, JSON, Number, Promise, Uint8Array, File,
    MEDIA_EXTENSIONS: { image: ['png', 'jpg'], audio: ['mp3'], video: ['mp4'] },
    MEDIA_SIZE_LIMITS: { image: 20 * 1024 * 1024, audio: 20 * 1024 * 1024, video: 600 * 1024 * 1024 },
    toast: (msg) => toasts.push(msg),
  };
  if (crypto !== undefined) context.crypto = crypto;
  vm.createContext(context);
  vm.runInContext(src.slice(src.indexOf('function mediaKindOf('), src.indexOf('async function addPendingMediaFiles(')), context);
  return { context, toasts };
}

test('SHA-256 路径：相同字节哈希一致，与 node:crypto 独立实现对照一致', async () => {
  const h = harness({ crypto: nodeCrypto.webcrypto });
  const bytes = Buffer.from('fake-image-bytes-001');
  const a = await h.context.prepareMediaFile(new File([bytes], 'a.png', { type: 'image/png' }));
  const b = await h.context.prepareMediaFile(new File([bytes], 'b.png', { type: 'image/png' }));
  assert.ok(a.hash.startsWith(bytes.length + ':sha256:'), a.hash);
  assert.equal(a.hash.length, String(bytes.length).length + 8 + 64);
  assert.equal(a.hash, b.hash, '同名不同名不影响，字节相同即同哈希');
  const expected = nodeCrypto.createHash('sha256').update(bytes).digest('hex');
  assert.ok(a.hash.endsWith(expected), '应与标准 SHA-256 一致');
});

test('不同字节哈希不同（同名也不误判）', async () => {
  const h = harness({ crypto: nodeCrypto.webcrypto });
  const a = await h.context.prepareMediaFile(new File(['aaa'], 'same.png', { type: 'image/png' }));
  const b = await h.context.prepareMediaFile(new File(['bbb'], 'same.png', { type: 'image/png' }));
  assert.notEqual(a.hash, b.hash);
});

test('非安全上下文降级 FNV-1a：确定性、可判重', async () => {
  const h = harness(); // 不提供 crypto
  const a = await h.context.prepareMediaFile(new File(['abc'], 'a.png', { type: 'image/png' }));
  const b = await h.context.prepareMediaFile(new File(['abc'], 'copy.png', { type: 'image/png' }));
  const c = await h.context.prepareMediaFile(new File(['abd'], 'c.png', { type: 'image/png' }));
  assert.match(a.hash, /^3:fnv1a:[0-9a-f]{8}$/, a.hash);
  assert.equal(a.hash, b.hash);
  assert.notEqual(a.hash, c.hash);
});

test('超大文件跳过哈希（不读字节、不阻塞添加）', async () => {
  const h = harness({ crypto: nodeCrypto.webcrypto });
  let read = false;
  const big = {
    name: 'huge.mp4', size: 65 * 1024 * 1024,
    arrayBuffer: () => { read = true; return Promise.resolve(new ArrayBuffer(0)); },
  };
  const item = await h.context.prepareMediaFile(big);
  assert.ok(item, '超大视频照常添加');
  assert.equal(item.hash, '', '无哈希');
  assert.equal(read, false, '不应读取整文件字节');
});

test('不支持的类型仍返回 null（原有行为不变）', async () => {
  const h = harness({ crypto: nodeCrypto.webcrypto });
  const item = await h.context.prepareMediaFile(new File(['x'], 'a.xyz'));
  assert.equal(item, null);
  assert.ok(h.toasts.some((m) => m.includes('不支持的媒体类型')));
});
