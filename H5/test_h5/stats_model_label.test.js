"use strict";
/**
 * 状态条模型名单一入口回归（js/app/stats.js）
 *
 * 背景：状态条中部显示当前会话生效的聊天模型名。历史上模型面板保存后
 * 依赖「先手动更新 state.modelConfigs.chat_model 再调用 refreshChatModelLabel」
 * 的隐式时序约定——漏更新缓存就会显示旧模型名。现在收敛为：
 * - App.setChatModelConfig(data)：写缓存 + 原子重绘（唯一写入入口）；
 * - App.renderChatModelLabel()：纯渲染（不请求），供流程整体写缓存后调用；
 * - App.refreshChatModelLabel(sessionId?, force?)：拉取 + 渲染（序号防竞态）。
 *
 * 覆盖：
 * - setChatModelConfig：写缓存、状态条文本/title/hidden、vision 能力同步；
 * - setChatModelConfig：空值不写缓存仅重绘；写入使在途旧请求失效（不被回写）；
 * - renderChatModelLabel：纯渲染不发请求；
 * - refreshChatModelLabel：拉取渲染；已有缓存且未显式切换会话时复用缓存；
 * - refreshChatModelLabel：快速连续切换会话时丢弃过期响应（防竞态）。
 */
const { test } = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

// ---------- 最小节点桩（classList/textContent/title 足够覆盖断言） ----------
function makeClassList() {
  const set = new Set();
  return {
    add: function (c) { set.add(c); },
    remove: function (c) { set.delete(c); },
    contains: function (c) { return set.has(c); },
    toggle: function (c, on) {
      const next = on === undefined ? !set.has(c) : Boolean(on);
      if (next) set.add(c); else set.delete(c);
      return next;
    },
  };
}

function makeNode() {
  return { textContent: "", title: "", classList: makeClassList() };
}

// ---------- 加载被测模块（IIFE 直接写 window.App，每次清缓存重载） ----------
function loadStatsModule(state) {
  const app = {
    state: state,
    tokenTotal: makeNode(),
    contextTokenStatus: makeNode(),
    contextTokenSummary: makeNode(),
    contextTokenModel: makeNode(),
  };
  global.window = { App: app };

  const calls = { getModels: 0 };
  const pending = []; // 手动控制的在途请求：{ role, sessionId, force, resolve, reject }
  global.API = {
    getModels: function (role, sessionId, force) {
      calls.getModels += 1;
      return new Promise(function (resolve, reject) {
        pending.push({ role: role, sessionId: sessionId, force: force, resolve: resolve, reject: reject });
      });
    },
  };
  global.SessionUtils = {
    sanitizeSessionId: function (id) { return id == null ? "" : String(id); },
  };
  global.FormatUtils = {
    normalizeUsage: function () {
      return { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 };
    },
    fmtNum: function (n) { return String(n); },
  };

  const target = path.join(__dirname, "..", "js", "app", "stats.js");
  delete require.cache[require.resolve(target)];
  require(target);

  return { app: app, state: state, calls: calls, pending: pending };
}

// 构造 GET /chat_config/models 响应形态（role_info.selection 为模型名数据源）
function cfg(modelName, vision) {
  return {
    role_info: {
      selection: { provider: "供应商", model: modelName },
      vision: vision !== undefined ? vision : true,
    },
  };
}

test("setChatModelConfig: 写缓存并原子重绘状态条（文本/title/可见性）", function () {
  const env = loadStatsModule({ sessionId: null });
  const ok = env.app.setChatModelConfig(cfg("模型 A"));

  assert.equal(ok, true);
  assert.equal(env.state.modelConfigs.chat_model.role_info.selection.model, "模型 A");
  assert.equal(env.app.contextTokenModel.textContent, "模型 A");
  assert.equal(env.app.contextTokenModel.title, "当前聊天模型：模型 A");
  assert.equal(env.app.contextTokenModel.classList.contains("hidden"), false);
  // 全程零网络请求：单一入口只做本地写入与渲染
  assert.equal(env.calls.getModels, 0);
});

test("setChatModelConfig: vision 能力同步（false 保守禁用 / true 恢复）", function () {
  const env = loadStatsModule({ sessionId: null });

  env.app.setChatModelConfig(cfg("视觉模型", true));
  assert.equal(env.app.getChatModelVision(), true);

  env.app.setChatModelConfig(cfg("纯文本模型", false));
  assert.equal(env.app.getChatModelVision(), false);

  env.app.setChatModelConfig(cfg("视觉模型", true));
  assert.equal(env.app.getChatModelVision(), true);
});

test("setChatModelConfig: 空值不写缓存、仅按现有缓存重绘", function () {
  const env = loadStatsModule({ sessionId: null });
  env.app.setChatModelConfig(cfg("模型 A"));

  const ok = env.app.setChatModelConfig(null);
  assert.equal(ok, false);
  // 缓存保持旧值，状态条保持旧模型名（不会因空值被清空）
  assert.equal(env.state.modelConfigs.chat_model.role_info.selection.model, "模型 A");
  assert.equal(env.app.contextTokenModel.textContent, "模型 A");
});

test("renderChatModelLabel: 纯渲染不发请求（供整体写缓存后调用）", function () {
  const env = loadStatsModule({ sessionId: null });
  env.state.modelConfigs = { chat_model: cfg("缓存模型") };

  env.app.renderChatModelLabel();

  assert.equal(env.calls.getModels, 0);
  assert.equal(env.app.contextTokenModel.textContent, "缓存模型");
});

test("refreshChatModelLabel: 首次拉取渲染；已有缓存且未显式切换会话时复用缓存", async function () {
  const env = loadStatsModule({ sessionId: "sess-1" });

  const first = env.app.refreshChatModelLabel();
  assert.equal(env.calls.getModels, 1);
  assert.equal(env.pending[0].role, "chat_model");
  assert.equal(env.pending[0].sessionId, "sess-1"); // 按当前会话拉取生效选择
  env.pending[0].resolve(cfg("会话模型"));
  assert.equal(await first, true);
  assert.equal(env.app.contextTokenModel.textContent, "会话模型");

  // 第二次：无显式 sessionId 且缓存已在 → 不发请求，按缓存渲染
  const second = env.app.refreshChatModelLabel();
  assert.equal(env.calls.getModels, 1);
  assert.equal(await second, false);
  assert.equal(env.app.contextTokenModel.textContent, "会话模型");
});

test("refreshChatModelLabel: 快速连续切换会话时丢弃过期响应（防竞态）", async function () {
  const env = loadStatsModule({ sessionId: null });

  const p1 = env.app.refreshChatModelLabel("sess-1");
  const p2 = env.app.refreshChatModelLabel("sess-2");
  assert.equal(env.calls.getModels, 2);

  // 新会话响应先到 → 渲染 sess-2 的模型
  env.pending[1].resolve(cfg("会话 2 模型"));
  assert.equal(await p2, true);
  assert.equal(env.app.contextTokenModel.textContent, "会话 2 模型");

  // 旧会话响应后到 → 过期，不得覆盖新显示、不写缓存
  env.pending[0].resolve(cfg("会话 1 模型"));
  assert.equal(await p1, false);
  assert.equal(env.app.contextTokenModel.textContent, "会话 2 模型");
  assert.equal(env.state.modelConfigs.chat_model.role_info.selection.model, "会话 2 模型");
});

test("setChatModelConfig: 写入使在途旧请求失效（响应不回写覆盖新配置）", async function () {
  const env = loadStatsModule({ sessionId: null });

  const inflight = env.app.refreshChatModelLabel("sess-1");
  assert.equal(env.calls.getModels, 1);

  // 在途期间用户保存了新模型：单一入口立即生效
  env.app.setChatModelConfig(cfg("新模型"));
  assert.equal(env.app.contextTokenModel.textContent, "新模型");

  // 在途请求随后返回旧配置 → 被序号保护丢弃，不覆盖新配置
  env.pending[0].resolve(cfg("旧模型"));
  assert.equal(await inflight, false);
  assert.equal(env.app.contextTokenModel.textContent, "新模型");
  assert.equal(env.state.modelConfigs.chat_model.role_info.selection.model, "新模型");
});

test("refreshChatModelLabel: 拉取失败保留现有显示（不误清）", async function () {
  const env = loadStatsModule({ sessionId: null });
  env.app.setChatModelConfig(cfg("已有模型"));

  const p = env.app.refreshChatModelLabel("sess-1", true);
  env.pending[0].reject(new Error("network down"));
  assert.equal(await p, false);
  assert.equal(env.app.contextTokenModel.textContent, "已有模型");
});
