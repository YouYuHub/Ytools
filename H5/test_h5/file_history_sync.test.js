const assert = require("node:assert");
const test = require("node:test");

const path = require("path");
const Sync = require(path.join(__dirname, "..", "js", "file_history_sync.js"));

// 原始全局引用：每个用例后恢复，避免 mock 泄漏到其他测试文件
const ORIGINAL = {
  localStorage: globalThis.localStorage,
  BroadcastChannel: globalThis.BroadcastChannel,
  addEventListener: globalThis.addEventListener,
  removeEventListener: globalThis.removeEventListener,
};

function restore(name) {
  if (ORIGINAL[name] === undefined) delete globalThis[name];
  else globalThis[name] = ORIGINAL[name];
}

function cleanup() {
  restore("localStorage");
  restore("BroadcastChannel");
  restore("addEventListener");
  restore("removeEventListener");
  Sync.consumeDirty(); // 重置模块内 memoryDirty 降级状态
}

function mockLocalStorage() {
  const store = new Map();
  globalThis.localStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => { store.set(k, String(v)); },
    removeItem: (k) => { store.delete(k); },
  };
  return store;
}

function mockBroadcastChannel() {
  class FakeBC {
    constructor(name) {
      this.name = name;
      this.onmessage = null;
      this.closed = false;
      FakeBC.instances.push(this);
    }
    postMessage(data) { FakeBC.posted.push(data); }
    close() { this.closed = true; }
  }
  FakeBC.instances = [];
  FakeBC.posted = [];
  globalThis.BroadcastChannel = FakeBC;
  return FakeBC;
}

test("初始状态无标记", function () {
  mockLocalStorage();
  try {
    assert.strictEqual(Sync.peekDirty(), false);
  } finally { cleanup(); }
});

test("markDirty 写标记，peekDirty 读取（不消费）", function () {
  mockLocalStorage();
  try {
    Sync.markDirty();
    assert.strictEqual(Sync.peekDirty(), true);
    assert.strictEqual(Sync.peekDirty(), true); // 多次读取不影响
  } finally { cleanup(); }
});

test("consumeDirty 消费标记并返回存在性", function () {
  mockLocalStorage();
  try {
    Sync.markDirty();
    assert.strictEqual(Sync.consumeDirty(), true);
    assert.strictEqual(Sync.peekDirty(), false);
    assert.strictEqual(Sync.consumeDirty(), false);
  } finally { cleanup(); }
});

test("markDirty 多次后一次 consume 全部清除", function () {
  mockLocalStorage();
  try {
    Sync.markDirty();
    Sync.markDirty();
    assert.strictEqual(Sync.consumeDirty(), true);
    assert.strictEqual(Sync.peekDirty(), false);
  } finally { cleanup(); }
});

test("无 localStorage 时内存降级仍可用", function () {
  try {
    assert.strictEqual(Sync.peekDirty(), false);
    Sync.markDirty();
    assert.strictEqual(Sync.peekDirty(), true);
    assert.strictEqual(Sync.consumeDirty(), true);
    assert.strictEqual(Sync.peekDirty(), false);
  } finally { cleanup(); }
});

test("subscribe 收到 BroadcastChannel 广播（dirty 类型）", function () {
  const FakeBC = mockBroadcastChannel();
  try {
    const received = [];
    const unsub = Sync.subscribe(function (d) { received.push(d); });
    const channel = FakeBC.instances[FakeBC.instances.length - 1];
    assert.ok(channel, "订阅应创建 BroadcastChannel");
    channel.onmessage({ data: { type: "dirty", at: 1 } });
    assert.strictEqual(received.length, 1);
    assert.strictEqual(received[0].type, "dirty");
    // 非 dirty 类型与空消息忽略
    channel.onmessage({ data: { type: "other" } });
    channel.onmessage({ data: null });
    channel.onmessage(null);
    assert.strictEqual(received.length, 1);
    unsub();
    assert.strictEqual(channel.closed, true, "unsubscribe 应关闭通道");
  } finally { cleanup(); }
});

test("subscribe 的 handler 异常被隔离（不影响后续消息）", function () {
  const FakeBC = mockBroadcastChannel();
  try {
    const received = [];
    const unsub = Sync.subscribe(function (d) {
      received.push(d);
      throw new Error("handler boom");
    });
    const channel = FakeBC.instances[FakeBC.instances.length - 1];
    channel.onmessage({ data: { type: "dirty", at: 1 } });
    channel.onmessage({ data: { type: "dirty", at: 2 } });
    assert.strictEqual(received.length, 2, "异常不应中断后续消息");
    unsub();
  } finally { cleanup(); }
});

test("subscribe 的 storage 事件兜底（仅目标键触发）", function () {
  let storageListener = null;
  globalThis.addEventListener = function (type, fn) {
    if (type === "storage") storageListener = fn;
  };
  globalThis.removeEventListener = function () {};
  try {
    const received = [];
    const unsub = Sync.subscribe(function (d) { received.push(d); });
    assert.ok(storageListener, "应注册 storage 监听");
    storageListener({ key: Sync.STORAGE_KEY });
    assert.strictEqual(received.length, 1);
    storageListener({ key: "other-key" });
    assert.strictEqual(received.length, 1, "非目标键不应触发");
    unsub();
  } finally { cleanup(); }
});

test("markDirty 经 BroadcastChannel 广播（type=dirty）", function () {
  const FakeBC = mockBroadcastChannel();
  try {
    Sync.markDirty();
    assert.strictEqual(FakeBC.posted.length, 1);
    assert.strictEqual(FakeBC.posted[0].type, "dirty");
    assert.ok(FakeBC.instances.every((ch) => ch.closed), "广播后应关闭通道");
  } finally { cleanup(); }
});

test("无 BroadcastChannel 时 markDirty 不报错（仅标记）", function () {
  mockLocalStorage();
  try {
    delete globalThis.BroadcastChannel;
    Sync.markDirty();
    assert.strictEqual(Sync.peekDirty(), true);
  } finally { cleanup(); }
});

test("subscribe 非函数参数返回空取消函数", function () {
  try {
    const unsub = Sync.subscribe(null);
    assert.strictEqual(typeof unsub, "function");
    unsub(); // 不应抛错
  } finally { cleanup(); }
});

test("导出常量与 API 完整", function () {
  assert.strictEqual(typeof Sync.STORAGE_KEY, "string");
  assert.strictEqual(typeof Sync.CHANNEL_NAME, "string");
  assert.strictEqual(typeof Sync.peekDirty, "function");
  assert.strictEqual(typeof Sync.markDirty, "function");
  assert.strictEqual(typeof Sync.consumeDirty, "function");
  assert.strictEqual(typeof Sync.subscribe, "function");
});

test("集成：编辑器窗口 markDirty → 主页面窗口订阅即时收到 + 标记跨窗口共享", function () {
  // 共享 localStorage + 广播注册中心（模拟同一浏览器的两个窗口）
  mockLocalStorage();
  class FakeBC {
    constructor(name) {
      this.name = name;
      this.onmessage = null;
      this.closed = false;
      FakeBC.all.push(this);
    }
    postMessage(data) {
      for (const ch of FakeBC.all) {
        if (ch !== this && !ch.closed && ch.onmessage) ch.onmessage({ data: data });
      }
    }
    close() { this.closed = true; }
  }
  FakeBC.all = [];
  globalThis.BroadcastChannel = FakeBC;

  const syncPath = require.resolve(path.join(__dirname, "..", "js", "file_history_sync.js"));
  try {
    // 两个模块实例 = 两个"窗口"（各自独立的模块状态）
    delete require.cache[syncPath];
    const editorSync = require(syncPath); // 编辑器窗口
    delete require.cache[syncPath];
    const mainSync = require(syncPath);   // 主页面窗口

    const received = [];
    const unsub = mainSync.subscribe(function (d) { received.push(d); });

    // 编辑器写操作成功 → markDirty
    editorSync.markDirty();
    assert.strictEqual(received.length, 1, "主页面应即时收到广播（无需等 focus）");

    // 主页面 focus 兜底路径：读标记（跨窗口共享）→ 有 → 刷新成功后消费
    assert.strictEqual(mainSync.peekDirty(), true, "标记应跨窗口可见");
    assert.strictEqual(mainSync.consumeDirty(), true);
    assert.strictEqual(mainSync.peekDirty(), false, "消费后应无标记（后续 focus 零请求）");

    // 再次写操作 → 再次广播
    editorSync.markDirty();
    assert.strictEqual(received.length, 2);
    unsub();
  } finally {
    delete require.cache[syncPath]; // 恢复干净模块缓存
    cleanup();
  }
});
