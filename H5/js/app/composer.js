/**
 * 输入区与设置面板
 * - 发送/停止按钮状态、输入框自适应高度、快捷发送、建议 chips
 * - 参数面板定位、“+”功能菜单、聊天设置模态框（回传长度/压缩策略/超时重试）
 * 依赖：app/core.js、API；App.*：chat/model_panel/tools 模块
 */
(function (App) {
  "use strict";
  const {
    state, toast, $, closeMenus,
    updateScrollBottomOffset, input, composer, composerWrap,
    sendBtn, voiceBtn, stopBtn, app,
    enhancePanel, plusMenu, plusBtn, boostBtn,
    themeMenu, fileInput, CHAT_SETTINGS_DEFAULTS, chatSettingsModal,
    chatSettingsBackdrop, chatSettingsClose, chatSettingsCancel, chatSettingsConfirm,
    chatSettingsReset, reasoningMaxLength, toolResultMaxLength, toolCallTimeoutSeconds,
    networkRetryMaxAttempts, keepRounds, triggerRatio, summaryBudgetRatio,
    oversizedRejectFactor, maxOversizedRejections, effectiveThresholdHint
  } = App;

  // ---------- 输入区 ----------
  function refreshComposerButtons() {
    const hasText = input.value.trim() !== "" || state.pendingMedia.length > 0;
    // 按钮只反映“当前会话”的任务状态，其他会话在后台流式不影响本会话的发送/停止按钮
    const currentStreaming = state.streaming && state.streamingSession === state.sessionId;
    // 手动压缩进行中：发送会打断压缩任务并交错写入会话历史，隐藏发送入口
    // （键盘发送由 send() 内的同名守卫拦截）
    const compacting = state.manualCompactRunning;
    composer.classList.toggle("has-text", hasText);
    sendBtn.classList.toggle("hidden", !hasText || currentStreaming || compacting);
    voiceBtn.classList.toggle("hidden", hasText || currentStreaming);
    stopBtn.classList.toggle("hidden", !currentStreaming);
    requestAnimationFrame(updateScrollBottomOffset);
  }

  function autosize() {
    input.style.height = "auto";
    if (composer.classList.contains("grow")) {
      // 已处于“输入在上、按钮在下”布局：用单行（窄）宽度测量决定是否回退，
      // 只有单行宽度也明确放得下 1 行才回退，避免宽度变宽/变窄造成上下抖动
      composer.classList.remove("grow");
      const narrowH = input.scrollHeight;
      composer.classList.add("grow");
      if (narrowH <= 38) composer.classList.remove("grow");
    } else {
      // 单行：输入第一行不变高（scrollHeight=36px），换行放不下才切列布局
      composer.classList.toggle("grow", input.scrollHeight > 38);
    }
    // 高度始终按最终布局下的实际宽度测量，紧贴内容，底部不留空白行
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 200) + "px";
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
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      App.send();
    }
  });

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
      API.getHistoryCompactionConfig().catch(function () { return null; }),
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
  // - 工具执行超时/网络重试次数：>=0（0=不限制）
  // - 触发比例/摘要预算比例/连续拒绝上限：>0
  function collectInvalidChatSettings(ctxConfig, compConfig, mcpConfig, retryConfig) {
    const isNum = function (v) { return v != null && Number.isFinite(v); };
    const rows = [
      ["思考过程回传长度", ctxConfig.reasoning_max_length, function (v) { return isNum(v); }],
      ["工具结果回传长度", ctxConfig.tool_result_max_length, function (v) { return isNum(v); }],
      ["工具执行超时", mcpConfig.call_timeout_seconds, function (v) { return isNum(v) && v >= 0; }],
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
      API.updateHistoryCompactionConfig(compConfig),
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


  // ---------- 导出（供其它模块经 App.* 调用） ----------
  App.refreshComposerButtons = refreshComposerButtons;
  App.autosize = autosize;
  App.fitEnhancePanel = fitEnhancePanel;
  App.closeChatSettings = closeChatSettings;
})(window.App);
