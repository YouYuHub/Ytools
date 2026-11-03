/** 账户命名空间形态（security_client 挂载的 YtoolsAuth 子集）。 */
interface YtoolsAuthLike {
  storageKey?: unknown;
  rawStorage?: RawStorageLike | null;
}

/**
 * 原生 Storage 方法的裸引用集合（Storage.prototype.getItem 等已脱离原型）。
 * 注意：必须用 fn.call(localStorage, ...) 显式绑定 this，直接调用会抛
 * Illegal invocation——与 clipboard getAsFile 同一类坑。
 */
interface RawStorageLike {
  get: (...args: any[]) => any;
  set: (...args: any[]) => any;
  remove: (...args: any[]) => any;
}

/** 本模块用到的全局环境子集（浏览器 window / Node globalThis / 测试替身）。 */
interface SyncGlobal {
  YtoolsAuth?: YtoolsAuthLike | null;
  localStorage?: Storage | null;
  BroadcastChannel?: unknown;
  addEventListener?: unknown;
  removeEventListener?: unknown;
}

interface DirtyNotice {
  type: string;
  at: number;
}

type DirtyHandler = (notice: DirtyNotice) => void;

interface FileHistorySyncApi {
  STORAGE_KEY: string;
  CHANNEL_NAME: string;
  peekDirty: () => boolean;
  markDirty: () => void;
  consumeDirty: () => boolean;
  subscribe: (handler: unknown) => () => void;
}

/*
 * file_history_sync.ts — 文件版本链跨窗口变更标记（纯逻辑，无 DOM 依赖）
 *
 * 背景：主页面「文件变更」徽标刷新原有一条 focus 盲刷路径（切窗口/切标签
 * 回来即请求 /file_diff/list），静默期也会持续产生轮询式请求。改为变更标记
 * 驱动：
 *   - 编辑器页（editor.html）每次写操作成功（保存/撤回/保留/回退/刷新并入）
 *     调用 markDirty() 写 localStorage 标记，并经 BroadcastChannel 即时通知；
 *   - 主页面订阅通知即时刷新徽标；focus/visibilitychange 仅在标记存在时才
 *     刷新（拉取成功后 consumeDirty() 清除标记）——静默期零请求。
 *
 * 双通道 + 单兜底：
 *   1) BroadcastChannel：即时（编辑器操作 → 主页面立即收到）；
 *   2) storage 事件：跨窗口兜底（BroadcastChannel 不可用时）；
 *   3) 标记本身：跨窗口共享（localStorage），focus 时读标记兜底——
 *      即使双通道全部失效，编辑器操作后回到主页面仍会刷新一次。
 * 主页面同窗口写入不触发自身 storage 事件，靠 BroadcastChannel 覆盖；
 * 两者同时到达时由订阅方防抖合并。
 *
 * 存储键与通道名按账号隔离（不区分会话）：标记只表示"发生过文件链写操作"，
 * 刷新时按当前会话拉取，天然多会话安全。
 */
(function (global: typeof globalThis) {
  "use strict";

  const g = global as unknown as SyncGlobal;
  const STORAGE_KEY = "ytools-file-history-dirty";
  const scopedKey = (name: string): string => g.YtoolsAuth && typeof g.YtoolsAuth.storageKey === "function"
    ? (g.YtoolsAuth as { storageKey: (key: string) => string }).storageKey(name) : name;
  const STORAGE_EVENT_KEY = scopedKey(STORAGE_KEY);
  const CHANNEL_NAME = scopedKey("ytools-file-history");
  // 显式经账户命名空间读写，不依赖 Storage 包装链的隐式前缀。
  const rawOf = (): RawStorageLike | null | undefined => g.YtoolsAuth && g.YtoolsAuth.rawStorage;
  function rawGet(key: string): string | null {
    const raw = rawOf();
    return raw ? raw.get.call(g.localStorage, key) : (g.localStorage as Storage).getItem(key);
  }
  function rawSet(key: string, value: string): void {
    const raw = rawOf();
    if (raw) raw.set.call(g.localStorage, key, value);
    else (g.localStorage as Storage).setItem(key, value);
  }
  function rawRemove(key: string): void {
    const raw = rawOf();
    if (raw) raw.remove.call(g.localStorage, key);
    else (g.localStorage as Storage).removeItem(key);
  }

  let memoryDirty = false; // localStorage 不可用（Node 单测/隐私模式）时的内存降级

  function storageAvailable(): boolean {
    try {
      return !!g.localStorage;
    } catch (_) {
      return false;
    }
  }

  /** 读取标记（不清除）：true=存在未消费的变更标记。 */
  function peekDirty(): boolean {
    if (storageAvailable()) {
      try {
        return rawGet(STORAGE_EVENT_KEY) != null;
      } catch (_) {
        return memoryDirty;
      }
    }
    return memoryDirty;
  }

  /** 写入变更标记（编辑器页写操作成功后调用）+ 即时广播。 */
  function markDirty(): void {
    memoryDirty = true;
    if (storageAvailable()) {
      try {
        rawSet(STORAGE_EVENT_KEY, String(Date.now()));
      } catch (_) {
        /* 存储满/隐私模式：内存标记仍生效（同窗口），跨窗口由广播承担 */
      }
    }
    broadcast();
  }

  /** 消费标记：返回是否存在，存在则清除（主页面拉取成功后调用）。 */
  function consumeDirty(): boolean {
    const dirty = peekDirty();
    memoryDirty = false;
    if (storageAvailable()) {
      try {
        rawRemove(STORAGE_EVENT_KEY);
      } catch (_) {
        /* 忽略 */
      }
    }
    return dirty;
  }

  function broadcast(): void {
    try {
      if (typeof g.BroadcastChannel === "function") {
        const channel = new (g.BroadcastChannel as new (name: string) => BroadcastChannel)(CHANNEL_NAME);
        channel.postMessage({ type: "dirty", at: Date.now() });
        channel.close();
      }
    } catch (_) {
      /* 通道不可用：storage 事件与标记兜底 */
    }
  }

  /**
   * 订阅变更通知（主页面调用）。
   * @param handler 收到 {type:"dirty", at} 时调用（内部异常被隔离）
   * @returns unsubscribe 取消订阅（关闭通道/移除监听）
   */
  function subscribe(handler: unknown): () => void {
    if (typeof handler !== "function") return function () {};
    const onDirty = handler as DirtyHandler;
    let channel: BroadcastChannel | null = null;
    try {
      if (typeof g.BroadcastChannel === "function") {
        channel = new (g.BroadcastChannel as new (name: string) => BroadcastChannel)(CHANNEL_NAME);
        channel.onmessage = function (event: MessageEvent) {
          if (!event || !event.data || event.data.type !== "dirty") return;
          try {
            onDirty(event.data);
          } catch (_) {
            /* 订阅方异常不影响通道 */
          }
        };
      }
    } catch (_) {
      channel = null;
    }
    let storageListener: ((event: StorageEvent) => void) | null = null;
    if (typeof g.addEventListener === "function") {
      storageListener = function (event: StorageEvent) {
        if (!event || event.key !== STORAGE_EVENT_KEY || event.newValue === null) return;
        if (event.storageArea && storageAvailable() && event.storageArea !== g.localStorage) return;
        try {
          onDirty({ type: "dirty", at: Date.now() });
        } catch (_) {
          /* 忽略 */
        }
      };
      try {
        (g as unknown as { addEventListener: (type: string, listener: (event: StorageEvent) => void) => void }).addEventListener("storage", storageListener);
      } catch (_) {
        /* 忽略 */
      }
    }
    return function unsubscribe() {
      try {
        if (channel) channel.close();
      } catch (_) {
        /* 忽略 */
      }
      if (storageListener) {
        try {
          (g as unknown as { removeEventListener: (type: string, listener: (event: StorageEvent) => void) => void }).removeEventListener("storage", storageListener);
        } catch (_) {
          /* 忽略 */
        }
      }
    };
  }

  const api: FileHistorySyncApi = {
    STORAGE_KEY: STORAGE_KEY,
    CHANNEL_NAME: CHANNEL_NAME,
    peekDirty: peekDirty,
    markDirty: markDirty,
    consumeDirty: consumeDirty,
    subscribe: subscribe,
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  } else {
    (global as unknown as { FileHistorySync: FileHistorySyncApi }).FileHistorySync = api;
  }
})(typeof self !== "undefined" ? self : globalThis);
