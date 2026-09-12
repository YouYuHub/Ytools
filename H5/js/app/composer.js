/**
 * 输入区与设置面板
 * - 发送/停止按钮状态、输入框自适应高度、快捷发送、建议 chips
 * - 参数面板定位、“+”功能菜单、聊天设置模态框（回传长度/压缩策略/超时重试）
 * 依赖：app/core.js、API；App.*：chat/model_panel/tools 模块
 */
(function (App) {
  "use strict";
  const {
    state, toast, $, closeMenus, el,
    updateScrollBottomOffset, input, composer, composerWrap,
    sendBtn, voiceBtn, stopBtn, app,
    stopGroup, stopMenuBtn, queueMenu, pendingOutbox,
    enhancePanel, plusMenu, plusBtn, boostBtn,
    themeMenu, fileInput, CHAT_SETTINGS_DEFAULTS, chatSettingsModal,
    chatSettingsBackdrop, chatSettingsClose, chatSettingsCancel, chatSettingsConfirm,
    chatSettingsReset, reasoningMaxLength, toolResultMaxLength, toolCallTimeoutSeconds,
    toolStreamTimeoutSeconds, networkRetryMaxAttempts, keepRounds, triggerRatio, summaryBudgetRatio,
    oversizedRejectFactor, maxOversizedRejections, effectiveThresholdHint
  } = App;

  // ---------- 输入区 ----------
  function refreshComposerButtons() {
    const hasText = input.value.trim() !== "" || state.pendingMedia.length > 0;
    // 按钮只反映“当前会话”的任务状态，其他会话在后台流式不影响本会话的发送/停止按钮
    const currentStreaming = state.streaming && state.streamingSession === state.sessionId;
    // 手动压缩进行中：发送会打断压缩任务并交错写入会话历史，隐藏发送入口
    // （键盘发送由 send() 内的同名守卫拦截）；终止按钮接管，点击可中止压缩。
    // 压缩期间引导/队列也不可用：必须等压缩完成后才能发消息
    const compacting = state.manualCompactRunning;
    // 运行中组合按钮：仅当前会话流式/压缩时显示；上拉钮只在流式（非压缩）时可用
    const showStopGroup = currentStreaming || compacting;
    composer.classList.toggle("has-text", hasText);
    sendBtn.classList.toggle("hidden", !hasText || currentStreaming || compacting);
    voiceBtn.classList.toggle("hidden", hasText || currentStreaming || compacting);
    stopGroup.classList.toggle("hidden", !showStopGroup);
    stopGroup.classList.toggle("compact-only", compacting);
    stopMenuBtn.classList.toggle("hidden", !currentStreaming || compacting);
    if (!showStopGroup) {
      queueMenu.classList.add("hidden");
      state.composerSendMode = "";
    }
    requestAnimationFrame(updateScrollBottomOffset);
  }

  // 单行视口高度：scrollHeight ≤ 此值视为一行（padding 上下 1px + 33px 行高 + 3px 容差）
  var AUTOSIZE_ONE_LINE_HEIGHT = 38;
  // 最大可见行数：5 行（5×33px 行高 + 上下 2px padding = 167px，取整 168px）
  var AUTOSIZE_MAX_HEIGHT = 168;

  function autosize() {
    // 先记住当前可视首行位置：textarea 在“文本恰好填满一行”的边界状态下，
    // 输入会瞬时把视口滚到第 2 行（autosize 还未把高度算出来），然后又因
    // 高度不足以展示第 2 行而回滚——浏览器内部状态会停在第 2 行的滚动值
    // 上，视觉呈现为“行末多出一个空白行”。保存/恢复 scrollTop 可把视口
    // 钉回真实首行，彻底消除幽灵空行。
    var savedScrollTop = input.scrollTop;
    input.style.height = "auto";
    if (composer.classList.contains("grow")) {
      // 已处于“输入在上、按钮在下”布局：用单行（窄）宽度测量决定是否回退，
      // 只有单行宽度也明确放得下 1 行才回退，避免宽度变宽/变窄造成上下抖动
      composer.classList.remove("grow");
      const narrowH = input.scrollHeight;
      composer.classList.add("grow");
      if (narrowH <= AUTOSIZE_ONE_LINE_HEIGHT) composer.classList.remove("grow");
    } else {
      // 单行：输入第一行不变高（scrollHeight=36px），换行放不下才切列布局
      composer.classList.toggle("grow", input.scrollHeight > AUTOSIZE_ONE_LINE_HEIGHT);
    }
    // 高度始终按最终布局下的实际宽度测量，紧贴内容，底部不留空白行
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, AUTOSIZE_MAX_HEIGHT) + "px";
    // 高度始终按最终布局下的实际宽度测量，紧贴内容，底部不留空白行
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, AUTOSIZE_MAX_HEIGHT) + "px";
    // 双保险：只要全部内容都装进了当前视口（无滚动必要），就把视口钉回首行。
    // 行末“恰好装下”的边界状态下，Chromium 会在 caret 定位瞬间产生一次向下的
    // 幽灵滚动/空行盒（下一次按键即恢复正常），导致视觉上多出一个空白行；
    // 内容可完整展示时置顶滚动可直接消除该状态。
    if (input.scrollHeight <= input.clientHeight + 1) {
      input.scrollTop = 0;
    } else if (input.scrollTop !== savedScrollTop) {
      input.scrollTop = savedScrollTop;
    }
    refreshComposerButtons();
  }

  if (window.ResizeObserver) {
    new ResizeObserver(updateScrollBottomOffset).observe(composerWrap);
  }
  window.addEventListener("resize", function () {
    updateScrollBottomOffset();
    App.updateCodeblockCopyButtons();
  });

  input.addEventListener("input", autosize);
  input.addEventListener("keydown", function (e) {
    if (e.key !== "Enter" || e.shiftKey || e.isComposing) return;
    e.preventDefault();
    // 流式进行中（当前会话、非压缩）：Enter=消息引导，Alt+Enter=加入队列
    const currentStreaming = state.streaming && state.streamingSession === state.sessionId;
    if (currentStreaming && !state.manualCompactRunning) {
      submitSteerOrQueue(e.altKey ? "queue" : "steer");
      return;
    }
    App.send();
  });

  // ---------- 流式期间的引导 / 队列消息 ----------
  /**
   * 提交引导或队列消息：快照当前输入与附件后清空输入框。
   * - steer：任务运行中优先走后端注入接口（下一轮检查点立即生效）；
   *   注入不可用时回退本地暂存（当前 SSE 流结束后派发）
   * - queue：FIFO 累积，当前任务完成后逐条自动发送
   */
  async function submitSteerOrQueue(mode) {
    const currentStreaming = state.streaming && state.streamingSession === state.sessionId;
    if (!currentStreaming) return;
    if (state.manualCompactRunning) {
      toast("正在压缩对话上下文，请等待压缩完成后再发送");
      return;
    }
    const text = input.value.trim();
    const mediaSnapshot = state.pendingMedia.map(function (m) { return Object.assign({}, m); });
    if (!text && !mediaSnapshot.length) return;
    const message = { text: text, media: mediaSnapshot, sessionId: state.sessionId };
    // 从待发区移除但保留快照中的 objUrl（移除放回输入框后预览仍可用）；
    // objUrl 的最终释放在消息成功上传后（send 内）
    state.pendingMedia = [];
    App.renderComposerAttachments();
    input.value = "";
    App.autosize();
    if (mode === "queue") {
      state.pendingQueue.push(message);
      toast("已加入队列，当前任务完成后自动发送");
      renderPendingOutbox();
      return;
    }
    // 消息引导：附件需要先上传才能注入，纯文本直接注入；带附件时回退暂存
    if (!mediaSnapshot.length && state.sessionId) {
      try {
        const res = await API.injectMessage(state.sessionId, text);
        if (res && res.ok) {
          // 先在消息框上方挂"待注入"提示；气泡等后端 SSE message_injected
          // 事件（检查点消费）再插入——那一刻节点末尾恰好在当前轮工具结果
          // 之后，位置与后端 messages 顺序一致
          state.injectedPending = { sessionId: state.sessionId, text: text };
          renderPendingOutbox();
          toast("已注入为下一轮用户消息，模型将在本轮工具结果处理后看到");
          return;
        }
        // 任务未运行/注入失败：回退本地暂存（流结束后派发）
      } catch (_) { /* 网络异常同样回退暂存 */ }
    }
    const replaced = Boolean(state.steerMessage);
    state.steerMessage = message;
    toast(replaced ? "已替换消息引导内容，回复结束后发送" : "已设为消息引导，当前回复结束后立即发送");
    renderPendingOutbox();
  }

  /** 暂存指示器：仅显示属于当前会话的引导/队列条目，可移除并放回输入框。 */
  function renderPendingOutbox() {
    pendingOutbox.innerHTML = "";
    const rows = [];
    if (state.steerMessage && state.steerMessage.sessionId === state.sessionId) {
      rows.push({ kind: "steer", label: "引导", message: state.steerMessage });
    }
    state.pendingQueue.forEach(function (m, i) {
      if (m.sessionId === state.sessionId) {
        rows.push({ kind: "queue", label: "队列 " + (i + 1), message: m });
      }
    });
    if (!rows.length && !state.injectedPending) {
      pendingOutbox.classList.add("hidden");
      composer.classList.remove("has-outbox");
      return;
    }
    rows.forEach(function (row) {
      const item = el("div", "pending-outbox-item");
      item.appendChild(el("span", "pending-outbox-badge", row.label));
      item.appendChild(el("span", "pending-outbox-text", row.message.text || "[图片/附件]"));
      // 立即发送：当前会话流式中则打断该轮（后端收尾 + 本地中止），以本条消息
      // 立即开启新一轮；无进行中的流时直接派发（与自动 flush 同链路）
      const sendNow = el("button", "pending-outbox-send", "发送");
      sendNow.type = "button";
      sendNow.title = "立即发送该条消息（生成中则打断当前回复并以此消息重发）";
      sendNow.addEventListener("click", async function () {
        if (state.manualCompactRunning) {
          toast("正在压缩对话上下文，请稍后再发送");
          return;
        }
        const interrupting = state.streaming && state.streamingSession === state.sessionId;
        // 先从暂存区移除再派发，避免 flush 内部重复取
        if (row.kind === "steer") {
          state.steerMessage = null;
        } else {
          const idx = state.pendingQueue.indexOf(row.message);
          if (idx >= 0) state.pendingQueue.splice(idx, 1);
        }
        renderPendingOutbox();
        if (interrupting) {
          // 抑制旧流结束时的自动派发：本次点击接管发送次序，
          // 防止被打断轮次的 finally 抢先把队列头发出
          state.suppressFlushOnce = true;
          try {
            await API.stopChat(state.streamingSession);
          } catch (_) { /* 后端停止失败也继续本地中止 */ }
          if (state.abort) state.abort.abort();
          // 等待旧流的 send() finally 清理完流状态（AbortError 异步传播）；
          // 超时兜底：放回暂存区原位，不丢消息
          const deadline = Date.now() + 5000;
          while (state.streaming && state.streamingSession === state.sessionId && Date.now() < deadline) {
            await new Promise(function (r) { setTimeout(r, 50); });
          }
          if (state.streaming && state.streamingSession === state.sessionId) {
            if (row.kind === "steer") {
              state.steerMessage = row.message;
            } else {
              state.pendingQueue.unshift(row.message);
            }
            renderPendingOutbox();
            toast("当前任务未能及时停止，消息已放回暂存区");
            return;
          }
        }
        await App.send({ text: row.message.text || "", media: row.message.media || [] });
      });
      item.appendChild(sendNow);
      const remove = el("button", "pending-outbox-remove", "×");
      remove.type = "button";
      remove.title = "移除并放回输入框修改";
      remove.addEventListener("click", function () { cancelPendingMessage(row); });
      item.appendChild(remove);
      pendingOutbox.appendChild(item);
    });
    // 已提交后端、等待检查点消费的引导消息：可点 × 撤回（从后端注入
    // 队列移除）并放回输入框最前；已被消费时后端拒绝，仅提示
    if (state.injectedPending && state.injectedPending.sessionId === state.sessionId) {
      const item = el("div", "pending-outbox-item injected");
      item.title = "已提交后端，将在本轮工具结果处理后注入上下文";
      item.appendChild(el("span", "pending-outbox-badge", "引导"));
      item.appendChild(el("span", "pending-outbox-text", state.injectedPending.text || ""));
      const remove = el("button", "pending-outbox-remove", "×");
      remove.type = "button";
      remove.title = "撤回并放回输入框修改";
      remove.addEventListener("click", async function () {
        const pending = state.injectedPending;
        if (!pending) return;
        let cancelled = false;
        try {
          const res = await API.cancelInjectMessage(pending.sessionId, pending.text);
          cancelled = Boolean(res && res.ok);
        } catch (_) { /* 网络异常按撤回失败处理 */ }
        if (!cancelled) {
          toast("引导消息已被模型消费（或后端不可达），无法撤回");
          return;
        }
        if (state.injectedPending === pending) {
          state.injectedPending = null;
          prependToInput(pending.text);
          renderPendingOutbox();
        }
      });
      item.appendChild(remove);
      pendingOutbox.appendChild(item);
    }
    pendingOutbox.classList.remove("hidden");
    composer.classList.add("has-outbox");
  }

  /** 把文本放回输入框最前面（撤回/移除暂存消息时，先看到早提交的内容）。 */
  function prependToInput(text) {
    if (!text) return;
    input.value = input.value ? text + "\n" + input.value : text;
    App.autosize();
  }

  // 注入消费信号（chat.js 收到 message_injected SSE 时调用）：
  // 清除待注入提示；气泡由 chat.js 在该时机插入消息流正确位置
  App.clearInjectedPending = function (sessionId) {
    const pending = state.injectedPending;
    if (!pending) return;
    if (sessionId && pending.sessionId !== sessionId) return;
    state.injectedPending = null;
    renderPendingOutbox();
  };

  function cancelPendingMessage(row) {
    if (row.kind === "steer") {
      state.steerMessage = null;
    } else {
      const idx = state.pendingQueue.indexOf(row.message);
      if (idx >= 0) state.pendingQueue.splice(idx, 1);
    }
    // 放回输入框最前/附件区最前（先提交的内容排在前面），便于修改后重新提交
    const restored = row.message;
    if (restored.text) {
      prependToInput(restored.text);
    }
    if (restored.media.length) {
      state.pendingMedia = restored.media.concat(state.pendingMedia);
      App.renderComposerAttachments();
    }
    App.autosize();
    renderPendingOutbox();
  }

  /**
   * 派发暂存消息：流结束/压缩结束时调用。
   * 引导优先于队列；只派发属于 finishedSessionId 会话的消息；
   * 压缩进行中或存在待回答提问时跳过（等待下一次触发）；
   * 用户已切到其他会话时也跳过（等重新打开该会话再派发，避免发错目标）。
   * 每次只发一条，后续条目由该轮 send 结束时的 finally 链式继续派发。
   */
  async function flushPendingMessages(finishedSessionId) {
    if (state.suppressFlushOnce) {
      state.suppressFlushOnce = false;
      return;
    }
    if (state.manualCompactRunning) return;
    if (state.pendingAskQuestions) return; // ask_user 等待用户回答，不抢占
    const target = finishedSessionId || state.sessionId;
    // 用户已切走：留在暂存区，重新打开该会话时再派发
    if (state.sessionId !== target) return;
    if (state.streaming && state.streamingSession === target) return;
    let next = null;
    if (state.steerMessage && state.steerMessage.sessionId === target) {
      next = state.steerMessage;
      state.steerMessage = null;
    } else {
      const idx = state.pendingQueue.findIndex(function (m) { return m.sessionId === target; });
      if (idx >= 0) next = state.pendingQueue.splice(idx, 1)[0];
    }
    renderPendingOutbox();
    if (!next) return;
    await App.send(next);
  }

  document.querySelectorAll(".suggest-chip").forEach(function (chip) {
    chip.addEventListener("click", function () {
      input.value = chip.dataset.prompt || chip.textContent.trim();
      autosize();
      App.send();
    });
  });

  // ---------- 参数面板 ----------
  // 弹层锚定触发按钮而非输入框容器：多行输入时 composer 变高，CSS 的
  // bottom: calc(100%+8px) 会把弹层推到容器另一端甚至推出视口；
  // 打开时按按钮的视口坐标 fixed 定位，与容器高度彻底解耦。
  function placeAbove(el, anchor) {
    const r = anchor.getBoundingClientRect();
    el.classList.add("fixed-flyout");
    el.style.top = "auto";
    el.style.bottom = (window.innerHeight - r.top + 8) + "px";
    el.style.left = Math.max(8, Math.min(r.left, window.innerWidth - el.offsetWidth - 8)) + "px";
    el.style.right = "auto";
  }

  // 参数面板垂直锚定参数按钮（空态向下展开/聊天态向上展开），水平保持与 composer 同宽对齐
  function placeEnhancePanel() {
    const empty = app.classList.contains("empty");
    const btnRect = boostBtn.getBoundingClientRect();
    const composerRect = composer.getBoundingClientRect();
    enhancePanel.classList.add("fixed-flyout");
    enhancePanel.style.left = Math.max(8, composerRect.left) + "px";
    enhancePanel.style.width = composerRect.width + "px";
    enhancePanel.style.right = "auto";
    if (empty) {
      enhancePanel.style.top = (btnRect.bottom + 8) + "px";
      enhancePanel.style.bottom = "auto";
    } else {
      enhancePanel.style.top = "auto";
      enhancePanel.style.bottom = (window.innerHeight - btnRect.top + 8) + "px";
    }
  }

  function fitEnhancePanel() {
    placeEnhancePanel();
    const gap = 8;
    const empty = app.classList.contains("empty");
    // 面板定位锚点是 .composer（输入框）而非 composer-wrap（wrap 还含输入框
    // 上方状态栏 / 下方建议行），按面板自身渲染位置推算才能贴满可视区：
    // 空态向下展开用锚定的 top，聊天态向上展开用锚定的 bottom
    const panelRect = enhancePanel.getBoundingClientRect();
    const height = empty
      ? Math.max(120, window.innerHeight - panelRect.top - gap)
      : Math.max(200, panelRect.bottom - gap);
    enhancePanel.style.maxHeight = height + "px";
  }

  function toggleEnhancePanel() {
    const opening = enhancePanel.classList.contains("hidden");
    plusMenu.classList.add("hidden");
    enhancePanel.classList.toggle("hidden");
    if (opening) {
      fitEnhancePanel();
      App.openModelPanel();
    }
  }

  boostBtn.addEventListener("click", function (event) {
    event.stopPropagation();
    toggleEnhancePanel();
  });
  window.addEventListener("resize", function () {
    if (!enhancePanel.classList.contains("hidden")) fitEnhancePanel();
    if (!plusMenu.classList.contains("hidden")) placeAbove(plusMenu, plusBtn);
  });

  // 语音类按钮：占位
  voiceBtn.addEventListener("click", function () { toast("语音输入暂未开放"); });

  // ---------- 运行中发送选项（上拉菜单） ----------
  // 上拉钮点击弹出菜单；菜单项整体可点击，直接执行对应动作
  function toggleQueueMenu(mode) {
    const opening = queueMenu.classList.contains("hidden");
    closeMenus();
    if (opening) {
      queueMenu.classList.remove("hidden");
      state.composerSendMode = mode;
    }
  }
  stopMenuBtn.addEventListener("click", function (e) {
    e.stopPropagation();
    toggleQueueMenu("steer");
  });
  $("#steerMenuItem").addEventListener("click", function () {
    closeMenus();
    submitSteerOrQueue("steer");
  });
  $("#queueMenuItem").addEventListener("click", function () {
    closeMenus();
    submitSteerOrQueue("queue");
  });

  // ---------- “+” 功能菜单 ----------
  plusBtn.addEventListener("click", function (e) {
    e.stopPropagation();
    themeMenu.classList.add("hidden");
    plusMenu.classList.toggle("hidden");
    if (!plusMenu.classList.contains("hidden")) placeAbove(plusMenu, plusBtn);
  });

  $("#uploadItem").addEventListener("click", function () {
    closeMenus();
    fileInput.click();
  });

  $("#toolsItem").addEventListener("click", function () {
    closeMenus();
    App.openToolModal();
  });

  // ---------- 聊天设置模态框 ----------
  $("#chatSettingsItem").addEventListener("click", function () {
    closeMenus();
    openChatSettings();
  });

  function closeChatSettings() {
    chatSettingsModal.classList.add("hidden");
    chatSettingsModal.setAttribute("aria-hidden", "true");
  }

  // 两个接口同属“聊天设置”，共用一组确定/取消：打开时并行加载，确定时并行提交
  async function openChatSettings() {
    chatSettingsModal.classList.remove("hidden");
    chatSettingsModal.setAttribute("aria-hidden", "false");
    chatSettingsConfirm.disabled = true;
    const results = await Promise.all([
      API.getContextReturnConfig().catch(function () { return null; }),
      // 传 session_id：模型窗口按会话生效模型口径计算，与顶部 token 统计一致
      API.getHistoryCompactionConfig(state.sessionId).catch(function () { return null; }),
      API.getMcpToolConfig().catch(function () { return null; }),
      API.getNetworkRetryConfig().catch(function () { return null; }),
    ]);
    chatSettingsConfirm.disabled = false;
    const ctx = results[0];
    const comp = results[1];
    const mcp = results[2];
    const retry = results[3];
    const defaults = Object.assign({}, CHAT_SETTINGS_DEFAULTS, comp && comp.defaults || {},
      mcp && mcp.defaults || {}, retry && retry.defaults || {});
    if (ctx && ctx.defaults) {
      Object.assign(defaults, ctx.defaults);
    }
    state.chatSettingsDefaults = defaults;
    // 加载失败时回退到后端文档默认值，用户仍可编辑保存
    reasoningMaxLength.value = ctx && ctx.reasoning_max_length != null ? ctx.reasoning_max_length : defaults.reasoning_max_length;
    toolResultMaxLength.value = ctx && ctx.tool_result_max_length != null ? ctx.tool_result_max_length : defaults.tool_result_max_length;
    toolCallTimeoutSeconds.value = mcp && mcp.call_timeout_seconds != null
      ? mcp.call_timeout_seconds : defaults.call_timeout_seconds;
    toolStreamTimeoutSeconds.value = mcp && mcp.stream_timeout_seconds != null
      ? mcp.stream_timeout_seconds : defaults.stream_timeout_seconds;
    networkRetryMaxAttempts.value = retry && retry.max_attempts != null
      ? retry.max_attempts : defaults.network_retry_max_attempts;
    keepRounds.value = comp && comp.keep_rounds != null ? comp.keep_rounds : defaults.keep_rounds;
    triggerRatio.value = comp && comp.trigger_ratio != null ? comp.trigger_ratio : defaults.trigger_ratio;
    summaryBudgetRatio.value = comp && comp.summary_budget_ratio != null ? comp.summary_budget_ratio : defaults.summary_budget_ratio;
    oversizedRejectFactor.value = comp && comp.oversized_reject_factor != null ? comp.oversized_reject_factor : defaults.oversized_reject_factor;
    maxOversizedRejections.value = comp && comp.max_oversized_rejections != null ? comp.max_oversized_rejections : defaults.max_oversized_rejections;
    renderEffectiveThresholdHint(comp);
    if (!ctx || !comp || !mcp || !retry) {
      toast((ctx ? "" : "回传长度配置加载失败；") +
        (comp ? "" : "压缩策略配置加载失败；") +
        (mcp ? "" : "MCP 工具超时配置加载失败；") +
        (retry ? "" : "网络重试配置加载失败"));
    }
  }

  function readSettingNumber(input) {
    const value = Number(input.value);
    return Number.isFinite(value) ? value : null;
  }

  // 有效压缩阈值提示：阈值取聊天/压缩模型窗口较小者×触发比例，
  // 压缩模型窗口更小时实际触发点会低于"聊天窗口×比例"的直觉预期。
  function renderEffectiveThresholdHint(comp) {
    if (!effectiveThresholdHint) { return; }
    const detail = comp && comp.effective_threshold;
    if (!detail || !detail.value) {
      effectiveThresholdHint.textContent = "";
      effectiveThresholdHint.hidden = true;
      return;
    }
    const fmt = function (n) {
      return n >= 1000000 ? (n / 1000000).toFixed(n % 1000000 ? 1 : 0) + "M"
        : n >= 1000 ? Math.round(n / 1000) + "k" : String(n);
    };
    let text = "有效压缩阈值≈" + fmt(detail.value) + " tokens（"
      + fmt(detail.window) + " × " + detail.trigger_ratio + "）";
    if (detail.compaction_window < detail.chat_window) {
      text += "；受压缩模型窗口限制（聊天 " + fmt(detail.chat_window)
        + " / 压缩 " + fmt(detail.compaction_window) + " 取较小者）";
    }
    effectiveThresholdHint.textContent = text;
    effectiveThresholdHint.hidden = false;
  }

  function applyChatSettingsDefaults() {
    const defaults = Object.assign({}, CHAT_SETTINGS_DEFAULTS, state.chatSettingsDefaults || {});
    reasoningMaxLength.value = defaults.reasoning_max_length;
    toolResultMaxLength.value = defaults.tool_result_max_length;
    toolCallTimeoutSeconds.value = defaults.call_timeout_seconds;
    toolStreamTimeoutSeconds.value = defaults.stream_timeout_seconds;
    networkRetryMaxAttempts.value = defaults.network_retry_max_attempts;
    keepRounds.value = defaults.keep_rounds;
    triggerRatio.value = defaults.trigger_ratio;
    summaryBudgetRatio.value = defaults.summary_budget_ratio;
    oversizedRejectFactor.value = defaults.oversized_reject_factor;
    maxOversizedRejections.value = defaults.max_oversized_rejections;
    toast("已恢复聊天设置默认值，点击确定后生效");
  }

  chatSettingsReset.addEventListener("click", applyChatSettingsDefaults);

  // 聊天设置字段校验：返回无效字段名列表。校验域与后端一致：
  // - 回传长度：任意整数（0=不回传，负数=全部回传，正数=截断）
  // - 历史轮数窗口：>=0（0=无限窗口，仅按阈值压缩）
  // - 超长结果拒绝系数：>=0（0=关闭该功能）
  // - 工具执行超时/工具调用流超时/网络重试次数：>=0（0=不限制）
  // - 触发比例/摘要预算比例/连续拒绝上限：>0
  function collectInvalidChatSettings(ctxConfig, compConfig, mcpConfig, retryConfig) {
    const isNum = function (v) { return v != null && Number.isFinite(v); };
    const rows = [
      ["思考过程回传长度", ctxConfig.reasoning_max_length, function (v) { return isNum(v); }],
      ["工具结果回传长度", ctxConfig.tool_result_max_length, function (v) { return isNum(v); }],
      ["工具执行超时", mcpConfig.call_timeout_seconds, function (v) { return isNum(v) && v >= 0; }],
      ["工具调用流超时", mcpConfig.stream_timeout_seconds, function (v) { return isNum(v) && v >= 0; }],
      ["网络失败重试次数", retryConfig.max_attempts, function (v) { return isNum(v) && v >= 0; }],
      ["历史轮数窗口", compConfig.keep_rounds, function (v) { return isNum(v) && v >= 0; }],
      ["触发比例", compConfig.trigger_ratio, function (v) { return isNum(v) && v > 0; }],
      ["摘要预算比例", compConfig.summary_budget_ratio, function (v) { return isNum(v) && v > 0; }],
      ["超长结果拒绝系数", compConfig.oversized_reject_factor, function (v) { return isNum(v) && v >= 0; }],
      ["连续拒绝上限", compConfig.max_oversized_rejections, function (v) { return isNum(v) && v > 0; }],
    ];
    return rows.filter(function (row) { return !row[2](row[1]); }).map(function (row) { return row[0]; });
  }

  chatSettingsConfirm.addEventListener("click", async function () {
    const ctxConfig = {
      reasoning_max_length: readSettingNumber(reasoningMaxLength),
      tool_result_max_length: readSettingNumber(toolResultMaxLength),
    };
    const compConfig = {
      keep_rounds: readSettingNumber(keepRounds),
      trigger_ratio: readSettingNumber(triggerRatio),
      summary_budget_ratio: readSettingNumber(summaryBudgetRatio),
      oversized_reject_factor: readSettingNumber(oversizedRejectFactor),
      max_oversized_rejections: readSettingNumber(maxOversizedRejections),
    };
    const mcpConfig = {
      call_timeout_seconds: readSettingNumber(toolCallTimeoutSeconds),
      stream_timeout_seconds: readSettingNumber(toolStreamTimeoutSeconds),
    };
    const retryConfig = {
      max_attempts: readSettingNumber(networkRetryMaxAttempts),
    };
    const invalid = collectInvalidChatSettings(ctxConfig, compConfig, mcpConfig, retryConfig);
    if (invalid.length) {
      toast("请填写有效数值：" + invalid.join("、"));
      return;
    }
    chatSettingsConfirm.disabled = true;
    const results = await Promise.allSettled([
      API.updateContextReturnConfig(ctxConfig),
      API.updateHistoryCompactionConfig(compConfig, state.sessionId),
      API.updateMcpToolConfig(mcpConfig),
      API.updateNetworkRetryConfig(retryConfig),
    ]);
    chatSettingsConfirm.disabled = false;
    const failed = results.filter(function (r) { return r.status === "rejected"; });
    if (failed.length === 0) {
      App.refreshContextTokenStats(state.sessionId);
      toast("聊天设置已保存");
      closeChatSettings();
    } else {
      toast("保存失败：" + failed.map(function (r) { return r.reason.message; }).join("；"));
    }
  });

  chatSettingsCancel.addEventListener("click", closeChatSettings);
  chatSettingsClose.addEventListener("click", closeChatSettings);
  chatSettingsBackdrop.addEventListener("click", closeChatSettings);


  // ---------- 每会话独立的输入草稿 ----------
  /**
   * 保存当前输入到该会话的草稿（文本 + 待发附件快照，附件保留 objUrl 引用）。
   * 切换会话前调用；新对话（sessionId=null）存到 "" 键。
   */
  function saveSessionDraft() {
    const key = state.sessionId || "";
    const text = input.value;
    const media = state.pendingMedia.map(function (m) { return Object.assign({}, m); });
    if (!text.trim() && !media.length) {
      delete state.sessionDrafts[key];
      return;
    }
    state.sessionDrafts[key] = { text: text, media: media };
  }

  /**
   * 恢复指定会话的草稿到输入框/待发区；无草稿时清空输入框与待发区。
   * 切换会话后调用。注意：不吊销任何 objUrl——被替换掉的旧会话草稿
   * 仍持有其附件引用，切回时还要用。
   */
  function restoreSessionDraft(sessionId) {
    const key = sessionId || "";
    const draft = state.sessionDrafts[key];
    // 先把当前待发区从渲染中摘除（不动 state.pendingMedia 的对象）
    composerAttachments.innerHTML = "";
    if (draft) {
      input.value = draft.text || "";
      state.pendingMedia = (draft.media || []).map(function (m) { return Object.assign({}, m); });
    } else {
      input.value = "";
      state.pendingMedia = [];
    }
    App.renderComposerAttachments();
    App.autosize();
  }

  // ---------- 导出（供其它模块经 App.* 调用） ----------
  App.refreshComposerButtons = refreshComposerButtons;
  App.autosize = autosize;
  App.fitEnhancePanel = fitEnhancePanel;
  App.closeChatSettings = closeChatSettings;
  App.submitSteerOrQueue = submitSteerOrQueue;
  App.renderPendingOutbox = renderPendingOutbox;
  App.flushPendingMessages = flushPendingMessages;
  App.saveSessionDraft = saveSessionDraft;
  App.restoreSessionDraft = restoreSessionDraft;
})(window.App);
