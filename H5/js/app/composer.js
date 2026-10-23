/**
 * 输入区与设置面板
 * - 发送/停止按钮状态、输入框自适应高度、快捷发送、建议 chips
 * - 参数面板定位、“+”功能菜单、聊天设置模态框（压缩策略/超时重试）
 * 依赖：app/core.js、API；App.*：chat/model_panel/tools 模块
 */
(function (App) {
  "use strict";
  const {
    state, toast, $, closeMenus, el,
    updateScrollBottomOffset, input, composer, composerWrap,
    sendBtn, voiceBtn, stopBtn, scrollBottomBtn, app,
    stopGroup, stopMenuBtn, queueMenu, pendingOutbox,
    enhancePanel, plusMenu, plusBtn, boostBtn,
    fileInput, CHAT_SETTINGS_DEFAULTS, chatSettingsModal,
    chatSettingsBackdrop, chatSettingsClose, chatSettingsCancel, chatSettingsConfirm,
    chatSettingsReset, toolCallTimeoutSeconds,
    networkRetryMaxAttempts, compactionRetryMaxAttempts, videoReadMaxSeconds, mcpToolWorkers, subAgentMaxConcurrent, triggerRatio, summaryBudgetRatio, historyTargetTokens, historyTargetHint,
    subAgentFinalReplyRetryMax, subAgentStreamErrorRetryMax, subAgentTodoRemindMax,
    subAgentMaxRounds, subAgentTimeoutSeconds,
    oversizedRejectFactor, maxOversizedRejections, effectiveThresholdHint,
    settingsRetitleRow, settingsRetitleToggle, settingsRetitleStatus
  } = App;

  // ---------- 输入区 ----------
  function refreshComposerButtons() {
    if (App.taskStatus) App.taskStatus.refresh();
    const hasText = input.value.trim() !== "" || state.pendingMedia.length > 0;
    // 按钮只反映“当前会话”的任务状态，其他会话在后台流式不影响本会话的发送/停止按钮
    const currentStreaming = state.streaming && state.streamingSession === state.sessionId
      || !!(App.taskStatus && App.taskStatus.isUncertain(state.sessionId));
    scrollBottomBtn.classList.toggle("is-generating", currentStreaming);
    // 手动压缩进行中：发送会打断压缩任务并交错写入会话历史，隐藏发送入口
    // （键盘发送由 send() 内的同名守卫拦截）；终止按钮接管，点击可中止压缩。
    // 压缩期间引导/队列也不可用：必须等压缩完成后才能发消息。
    // 仅压缩发起会话受影响：切到其他会话时按钮状态不受后台压缩影响
    const compacting = state.manualCompactRunning && state.manualCompactSession === state.sessionId;
    // 运行中组合按钮：仅当前会话流式/压缩时显示；上拉钮只在流式（非压缩）时可用
    const showStopGroup = currentStreaming || compacting;
    // 语音录音中：识别文本实时写入输入框，麦克风保持显示并进入录音态。
    // 有文本时语音钮不隐藏：与发送钮并排，可继续补充录入，
    // 仅当前会话流式/压缩时随发送钮一起让位给停止钮
    composer.classList.toggle("has-text", hasText);
    sendBtn.classList.toggle("hidden", !hasText || currentStreaming || compacting);
    voiceBtn.classList.toggle("hidden", currentStreaming || compacting);
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
    if (App.syncComposerAttachmentBlocks) App.syncComposerAttachmentBlocks();
    updateScrollBottomOffset();
    App.updateCodeblockCopyButtons();
  });

  input.addEventListener("input", autosize);
  input.addEventListener("keydown", function (e) {
    if (e.key !== "Enter" || e.shiftKey || e.isComposing || speechComposing || e.keyCode === 229) return;
    e.preventDefault();
    // 回车发送前先结束录音（录音中写入的文本随消息发出，避免发送后
    // 识别结果继续写回已清空的输入框）
    if (typeof stopSpeechInput === "function" && speechRec) stopSpeechInput();
    // 流式进行中（当前会话、非压缩）：Enter=消息引导，Alt+Enter=加入队列
    const currentStreaming = state.streaming && state.streamingSession === state.sessionId;
    const compactingHere = state.manualCompactRunning &&
      state.manualCompactSession === state.sessionId;
    if (currentStreaming && !compactingHere) {
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
    if (state.manualCompactRunning && state.manualCompactSession === state.sessionId) {
      toast("正在压缩对话上下文，请等待压缩完成后再发送");
      return;
    }
    if (App.stopVoiceInput) App.stopVoiceInput();
    const text = input.value.trim();
    const mediaSnapshot = state.pendingMedia.map(function (m) { return Object.assign({}, m); });
    const quoteSnapshot = (state.pendingQuotes || []).map(function (q) { return Object.assign({}, q); });
    if (!text && !mediaSnapshot.length) return;
    const message = { text: text, media: mediaSnapshot, quotes: quoteSnapshot, sessionId: state.sessionId };
    // 从待发区移除但保留快照中的 objUrl（移除放回输入框后预览仍可用）；
    // objUrl 的最终释放在消息成功上传后（send 内）
    state.pendingMedia = [];
    state.pendingQuotes = [];
    App.renderComposerAttachments();
    App.renderComposerQuotes();
    input.value = "";
    if (App.closeSlashCommands) App.closeSlashCommands();
    App.autosize();
    if (mode === "queue") {
      state.pendingQueue.push(message);
      toast("已加入队列，当前任务完成后自动发送");
      renderPendingOutbox();
      return;
    }
    // 文本、图片等媒体和引用都走检查点注入，逐条消费，避免合并与撤回之间的竞态。
    let injected = false;
    if (message.sessionId) {
      message.id = "steer_" + Date.now().toString(36) + Math.random().toString(36).slice(2);
      try {
        const payload = await App.prepareSteerContent(message);
        // 先登记，再提交接口：SSE 消费事件可能先于 HTTP 响应到达。
        state.injectedPending.push(message);
        renderPendingOutbox();
        const res = await API.injectMessage(message.sessionId, payload.content, {
          quotes: payload.quotes, messageId: message.id,
        });
        if (res && res.ok) {
          toast("已提交引导，模型将在下一次请求前看到正文与附件");
          injected = true;
        }
      } catch (err) {
        toast("引导提交失败：" + err.message + "；消息已保留供重试");
      }
      if (!injected) {
        state.injectedPending = state.injectedPending.filter(function (m) { return m !== message; });
      }
    }
    if (injected) { renderPendingOutbox(); return; }
    // 本地暂存合并：同会话已有引导未派发时，新输入以空行拼进同一条、附件与
    // 引用一并并入（引用卡片按添加顺序拼接）
    const prevSteer = (state.steerMessage && state.steerMessage.sessionId === message.sessionId)
      ? state.steerMessage : null;
    state.steerMessage = {
      text: prevSteer && prevSteer.text ? prevSteer.text + "\n\n" + text : text,
      media: (prevSteer && prevSteer.media ? prevSteer.media : []).concat(mediaSnapshot),
      quotes: (prevSteer && prevSteer.quotes ? prevSteer.quotes : []).concat(quoteSnapshot),
      sessionId: message.sessionId,
    };
    toast(prevSteer
      ? "引导内容已合并（空行分隔），当前回复结束后一起发送"
      : "已设为消息引导，当前回复结束后立即发送");
    renderPendingOutbox();
    // 上传期间任务可能已结束；此时立即走普通发送，不等下一次任务结束事件。
    if (!state.streaming || state.streamingSession !== message.sessionId) {
      await flushPendingMessages(message.sessionId);
    }
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
    const injectedHere = state.injectedPending.filter(function (m) { return m.sessionId === state.sessionId; });
    if (!rows.length && !injectedHere.length) {
      pendingOutbox.classList.add("hidden");
      composer.classList.remove("has-outbox");
      return;
    }
    rows.forEach(function (row) {
      const item = el("div", "pending-outbox-item");
      item.appendChild(el("span", "pending-outbox-badge", row.label));
      // 收尾空白会进入省略号前的可见文本（看起来内容很长），显示前裁掉
      item.appendChild(el("span", "pending-outbox-text", (row.message.text || "").trim() || "[图片/附件]"));
      // 引用快照提示（选中文本引用到提问）：随消息一起暂存与派发
      if (row.message.quotes && row.message.quotes.length) {
        item.appendChild(el("span", "pending-outbox-quotes", "引用 " + row.message.quotes.length));
      }
      // 立即发送：当前会话流式中则打断该轮（后端收尾 + 本地中止），以本条消息
      // 立即开启新一轮；无进行中的流时直接派发（与自动 flush 同链路）
      const sendNow = el("button", "pending-outbox-send", "发送");
      sendNow.type = "button";
      sendNow.title = "立即发送该条消息（生成中则打断当前回复并以此消息重发）";
      sendNow.addEventListener("click", async function () {
        if (state.manualCompactRunning && state.manualCompactSession === state.sessionId) {
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
        await App.send({
          text: row.message.text || "",
          media: row.message.media || [],
          docs: row.message.docs || [],
          quotes: row.message.quotes || [],
          targetRound: row.message.targetRound,
          insertAfterRound: row.message.insertAfterRound,
          strictInsertContext: row.message.strictInsertContext,
        });
      });
      item.appendChild(sendNow);
      const remove = el("button", "pending-outbox-remove", "×");
      remove.type = "button";
      remove.title = "移除并放回输入框修改";
      remove.addEventListener("click", function () { cancelPendingMessage(row); });
      item.appendChild(remove);
      pendingOutbox.appendChild(item);
    });
    // 以 ID 跟踪多条待消费消息，纯图片也能撤回，同文本消息互不混淆。
    injectedHere.forEach(function (pending) {
      const item = el("div", "pending-outbox-item injected");
      item.title = "已提交后端，将在下一次模型请求前注入上下文";
      item.appendChild(el("span", "pending-outbox-badge", "引导"));
      item.appendChild(el("span", "pending-outbox-text", (pending.text || "").trim() || "[图片/附件]"));
      if (pending.media.length) item.appendChild(el("span", "pending-outbox-quotes", "附件 " + pending.media.length));
      if (pending.quotes.length) item.appendChild(el("span", "pending-outbox-quotes", "引用 " + pending.quotes.length));
      const remove = el("button", "pending-outbox-remove", "×");
      remove.type = "button";
      remove.title = "撤回并放回输入框修改";
      remove.addEventListener("click", async function () {
        let cancelled = false;
        try {
          const res = await API.cancelInjectMessage(pending.sessionId, pending.text, pending.id);
          cancelled = Boolean(res && res.ok);
        } catch (_) { /* 网络异常按撤回失败处理 */ }
        if (!cancelled) {
          toast("引导消息已被模型消费（或后端不可达），无法撤回");
          return;
        }
        if (state.injectedPending.includes(pending)) {
          state.injectedPending = state.injectedPending.filter(function (m) { return m !== pending; });
          cancelPendingMessage({ kind: "injected", message: pending });
        }
      });
      item.appendChild(remove);
      pendingOutbox.appendChild(item);
    });
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
  App.clearInjectedPending = function (sessionId, messageId) {
    const index = state.injectedPending.findIndex(function (pending) {
      return (!sessionId || pending.sessionId === sessionId) && (!messageId || pending.id === messageId);
    });
    if (index < 0) return;
    const pending = state.injectedPending.splice(index, 1)[0];
    (pending.media || []).forEach(function (item) {
      if (item.objUrl) { try { URL.revokeObjectURL(item.objUrl); } catch (_) {} item.objUrl = ""; }
    });
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
    const restorableDocs = (restored.docs || []).filter(function (doc) {
      return doc && doc.file && !doc.uploaded;
    });
    if (restorableDocs.length) {
      const stagedDocs = restorableDocs.map(function (doc) {
        return Object.assign({}, doc, {
          id: doc.id || (Date.now().toString(36) + Math.random().toString(36).slice(2, 8)),
          sessionId: restored.sessionId || state.sessionId,
          filename: doc.filename || doc.name || (doc.file && doc.file.name) || "未命名",
          staged: true,
        });
      });
      state.pendingDocs = state.pendingDocs.concat(stagedDocs);
      App.renderComposerAttachments();
    }
    // 引用快照随消息一起放回草稿（多段引用按原顺序拼在最前）
    if (restored.quotes && restored.quotes.length) {
      state.pendingQuotes = restored.quotes.concat(state.pendingQuotes || []);
      App.renderComposerQuotes();
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
    // 压缩进行中仅拦截压缩会话的派发（其余会话不受影响）
    if (state.manualCompactRunning && state.manualCompactSession === (finishedSessionId || state.sessionId)) return;
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

  // ---------- 弹层锚定工具（"+"功能菜单等仍留在 .composer 内的下拉复用） ----------
  // 锚定触发按钮而非容器：多行输入时 composer 变高，CSS 的 bottom: calc(100%+8px)
  // 会把弹层推到容器另一端甚至推出视口；打开时按按钮的视口坐标 fixed 定位，
  // 与容器高度彻底解耦。
  function placeAbove(el, anchor) {
    const r = anchor.getBoundingClientRect();
    el.classList.add("fixed-flyout");
    el.style.top = "auto";
    el.style.bottom = (window.innerHeight - r.top + 8) + "px";
    el.style.left = Math.max(8, Math.min(r.left, window.innerWidth - el.offsetWidth - 8)) + "px";
    el.style.right = "auto";
  }

  // 参数设置使用独立模态框，空白页和聊天页共用同一布局。
  boostBtn.addEventListener("click", function (event) {
    event.stopPropagation();
    plusMenu.classList.add("hidden");
    if (enhancePanel.classList.contains("hidden")) App.openModelPanel();
    else App.closeModelPanel();
  });
  window.addEventListener("resize", function () {
    if (!plusMenu.classList.contains("hidden")) placeAbove(plusMenu, plusBtn);
  });

  // ---------- 语音输入（Web Speech API，Chrome / Edge） ----------
  // 点麦克风开始听写，识别文本实时写入输入框：以点击时的光标为插入点，
  // 每次结果快照全量重建（final + interim），避免重复追加。只清理语音片段
  // 内汉字之间的水平空格，不修改手动输入的文本、英文词间空格和换行。
  // 手动编辑时重定位插入点，输入法组合期间只缓存识别结果，避免打断候选词。
  // 点击麦克风建立听写意图；服务自然断开时用新实例续听，错误、主动停止、
  // 页面隐藏和发送消息均结束该意图。continuous 只控制单次连接内的结果。
  let speechRec = null;
  let speechActive = false;
  let speechBase = "";
  let speechTail = "";
  let speechSelected = "";
  let speechLastWritten = null;
  let speechWriting = false;
  let speechLastSelectionStart = 0;
  let speechLastSelectionEnd = 0;
  let speechFollowCaret = true;
  let speechResults = []; // 已显示过的完整结果快照
  let speechConsumed = []; // 手动编辑前冻结的语音前缀，之后不重放
  let speechEditBaseline = null;
  let speechPendingResults = null;
  let speechComposing = false;
  let speechEditing = false;
  let speechEditTimer = null;
  let speechEnded = false; // 自然结束时，仍可在输入法提交后写入已缓存的结果
  let speechStarting = false;
  let speechStartRequested = false;
  let speechStartTimer = null;
  let speechReleaseTimer = null;
  let speechResumeTimer = null;
  let speechEmptyResumes = 0;
  let speechResumeEditChecks = 0;
  let speechResumeNoticeShown = false;
  const speechClosing = new Set(); // abort/stop 返回不代表音频服务已释放，须等待 end。
  const SPEECH_START_TIMEOUT_MS = 30000;
  const SPEECH_RELEASE_TIMEOUT_MS = 2000;
  const SPEECH_RESUME_DELAY_MS = 300;
  const SPEECH_MAX_EMPTY_RESUMES = 2;
  const speechLanguages = window.SpeechLanguage;
  const speechLanguageSelect = document.getElementById("speechLanguage");
  const voiceLanguageBadge = document.getElementById("voiceLanguageBadge");
  const speechLanguageStatus = document.getElementById("speechLanguageStatus");
  const speechMigrationKey = speechLanguages.STORAGE_KEY + "-migrated";
  let speechDeferredLanguage = null;
  let speechConfigVersion = 0;
  let speechConfigSaving = false;
  let speechMigrationDone = false;
  let speechLegacyLanguage = null;
  let speechLanguage = speechLanguages.DEFAULT_LANGUAGE;
  try {
    speechLanguage = speechLanguages.read(window.localStorage);
    const legacy = window.localStorage.getItem(speechLanguages.STORAGE_KEY);
    if (legacy && speechLanguages.get(legacy).code === legacy) speechLegacyLanguage = legacy;
    speechMigrationDone = window.localStorage.getItem(speechMigrationKey) === "1";
  } catch (_) { /* 缓存被禁用时使用默认，再从后端同步 */ }

  function normalizeSpeechText(text) {
    return speechLanguages.normalize(text, speechLanguage);
  }

  function speechSeparator(left, right) {
    return speechLanguages.separator(left, right, speechLanguage);
  }

  function syncSpeechLanguageUI() {
    const language = speechLanguages.get(speechActive ? speechLanguage : speechDeferredLanguage || speechLanguage);
    if (speechLanguageSelect) speechLanguageSelect.value = speechDeferredLanguage || speechLanguage;
    if (voiceLanguageBadge) voiceLanguageBadge.textContent = language.badge;
    const action = speechStartRequested ? "等待上次语音结束，点击取消"
      : speechStarting ? "正在启动语音输入，点击取消" : speechActive ? "停止语音输入" : "语音输入";
    voiceBtn.title = action + " · " + language.label + "（在聊天设置中切换语言）";
    voiceBtn.setAttribute("aria-pressed", String(speechActive));
    voiceBtn.setAttribute("aria-busy", String(speechStarting || speechStartRequested));
    voiceBtn.setAttribute("data-speech-state", speechStartRequested ? "waiting" : speechStarting ? "starting" : speechActive ? "listening" : "idle");
    voiceBtn.setAttribute("aria-label", action + " · " + language.label);
  }

  function setSpeechConfigStatus(message) {
    if (speechLanguageStatus) speechLanguageStatus.textContent = message;
  }

  function cacheSpeechLanguage(language) {
    try { return speechLanguages.save(window.localStorage, language); }
    catch (_) { return false; }
  }

  function markSpeechMigrated() {
    speechMigrationDone = true;
    try { window.localStorage.setItem(speechMigrationKey, "1"); } catch (_) { /* 缓存可选 */ }
  }

  async function speechConfigRequest(method, ...args) {
    const controller = new AbortController();
    const timer = setTimeout(function () { controller.abort(); }, 8000);
    try {
      const data = await API[method](...args, { signal: controller.signal });
      if (!data || typeof data.language !== "string" || speechLanguages.get(data.language).code !== data.language) {
        throw new Error("后端返回了无效的语音语言配置");
      }
      return data;
    } finally { clearTimeout(timer); }
  }

  function adoptSpeechConfig(data) {
    // 正在识别的实例及其临时/输入法结果始终使用启动时的语言规则。
    if (speechActive || speechRec || speechEditing || speechComposing) {
      speechDeferredLanguage = data.language === speechLanguage ? null : data.language;
    } else {
      speechLanguage = data.language;
      speechDeferredLanguage = null;
    }
    cacheSpeechLanguage(data.language);
    if (data.persisted) markSpeechMigrated();
    syncSpeechLanguageUI();
  }

  async function loadSpeechInputSettings() {
    if (speechConfigSaving) return;
    if (typeof API === "undefined" || !API.getSpeechInputConfig) {
      setSpeechConfigStatus("后端暂未提供语言配置，当前使用浏览器缓存");
      return;
    }
    const version = ++speechConfigVersion;
    setSpeechConfigStatus("正在同步项目默认语种…");
    try {
      let data = await speechConfigRequest("getSpeechInputConfig");
      if (version !== speechConfigVersion) return;
      // 旧浏览器只可填补未配置的后端，不能覆盖其他浏览器已经保存的值。
      if (!data.persisted && !speechMigrationDone && speechLegacyLanguage) {
        data = await speechConfigRequest("updateSpeechInputConfig", speechLegacyLanguage, true);
        if (version !== speechConfigVersion) return;
      }
      adoptSpeechConfig(data);
      setSpeechConfigStatus(speechDeferredLanguage
        ? "已同步项目默认；当前听写保持原语种，下次启动生效"
        : data.persisted ? "已从项目 .env 同步；清除浏览器网站数据后仍可恢复"
          : "项目未设置语种，使用默认普通话；选择后会保存到 .env");
    } catch (_) {
      if (version === speechConfigVersion) {
        setSpeechConfigStatus("后端配置暂不可用，使用浏览器缓存；重新打开设置可再次同步");
      }
    }
  }

  async function saveSpeechInputLanguage(next) {
    if (speechConfigSaving) { syncSpeechLanguageUI(); return; }
    if (next === (speechDeferredLanguage || speechLanguage)) return;
    ++speechConfigVersion; // 较晚返回的初始化读取不得覆盖用户刚刚选择的值。
    const wasActive = speechActive;
    stopSpeechInput();
    if (typeof API === "undefined" || !API.updateSpeechInputConfig) {
      speechLanguage = next;
      speechDeferredLanguage = null;
      const saved = cacheSpeechLanguage(next);
      syncSpeechLanguageUI();
      setSpeechConfigStatus("后端不支持保存语言，当前仅保存于浏览器");
      if (!saved) toast("语音语言已用于本页，浏览器未能保存偏好");
      else if (wasActive) toast("已切换语音语言，请点击麦克风继续；草稿已保留");
      return;
    }
    speechConfigSaving = true;
    if (speechLanguageSelect) speechLanguageSelect.disabled = true;
    setSpeechConfigStatus("正在保存到项目 .env…");
    try {
      const data = await speechConfigRequest("updateSpeechInputConfig", next, false);
      adoptSpeechConfig(data);
      setSpeechConfigStatus(speechDeferredLanguage
        ? "已保存到项目 .env；下次启动听写时使用新语种"
        : "已保存到项目 .env，所有浏览器共用默认语种");
      if (wasActive) toast("语音语言已保存，请点击麦克风继续；草稿已保留");
    } catch (error) {
      syncSpeechLanguageUI();
      const message = error && error.name === "AbortError" ? "保存超时" : "保存未确认成功";
      setSpeechConfigStatus(message + "，仍使用原语种；可重新选择或重新打开设置核对后端状态");
      toast("语音语言" + message + "，请检查后端连接后重试");
    } finally {
      speechConfigSaving = false;
      if (speechLanguageSelect) speechLanguageSelect.disabled = false;
    }
  }

  function applySpeechText(text) {
    const selectionStart = input.selectionStart == null ? input.value.length : input.selectionStart;
    const selectionEnd = input.selectionEnd == null ? selectionStart : input.selectionEnd;
    const selectionDirection = input.selectionDirection;
    // 点击、方向键和选区操作只改变光标，不结束听写。识别仍更新开启时的
    // 语音片段；用户移动过光标后保留其选区，不再每次把光标拉回语音末尾。
    if (selectionStart !== speechLastSelectionStart || selectionEnd !== speechLastSelectionEnd) speechFollowCaret = false;
    const oldEnd = input.value.length - speechTail.length;
    const before = speechBase + speechSeparator(speechBase, text) + text;
    const next = text ? before + speechSeparator(text, speechTail) + speechTail : speechBase + speechSelected + speechTail;
    if (input.value !== next) input.value = next;
    speechLastWritten = input.value;
    if (speechFollowCaret) {
      input.setSelectionRange(text ? before.length : speechBase.length, text ? before.length : speechBase.length + speechSelected.length);
    } else {
      const newEnd = next.length - speechTail.length;
      const mapSelection = function (pos) {
        if (pos <= speechBase.length) return pos;
        if (pos >= oldEnd) return pos + newEnd - oldEnd;
        return Math.min(pos, newEnd);
      };
      input.setSelectionRange(mapSelection(selectionStart), mapSelection(selectionEnd), selectionDirection);
    }
    speechLastSelectionStart = input.selectionStart;
    speechLastSelectionEnd = input.selectionEnd;
    speechWriting = true;
    try { input.dispatchEvent(new Event("input", { bubbles: true })); }
    finally { speechWriting = false; }
  }

  function resetVoiceButton() {
    voiceBtn.classList.remove("is-recording");
    voiceBtn.classList.remove("is-starting");
    syncSpeechLanguageUI();
    refreshComposerButtons();
  }

  // 默认先隔离旧实例，再取消识别，迟到事件不能覆盖手动编辑或另一会话。
  // 仅点麦克风停止允许收尾最后一段；随后编辑/发送仍立即隔离收尾事件。
  function stopSpeechInput(finishLastResult) {
    // 用户在组合输入中停止/发送时，保留输入法文本，不再收尾改写。
    if (speechComposing || speechEditing) finishLastResult = false;
    speechActive = false;
    speechStarting = false;
    speechStartRequested = false;
    if (speechStartTimer != null) clearTimeout(speechStartTimer);
    if (speechReleaseTimer != null) clearTimeout(speechReleaseTimer);
    if (speechResumeTimer != null) clearTimeout(speechResumeTimer);
    speechStartTimer = null;
    speechReleaseTimer = null;
    speechResumeTimer = null;
    const rec = speechRec;
    if (!finishLastResult) {
      speechRec = null;
      speechLastWritten = null;
      if (speechEditTimer != null) clearTimeout(speechEditTimer);
      speechEditTimer = null;
      speechEditBaseline = null;
      speechPendingResults = null;
      speechComposing = false;
      speechEditing = false;
      speechEnded = false;
    }
    if (rec) {
      speechClosing.add(rec);
      try {
        if (finishLastResult) rec.stop();
        else rec.abort();
      } catch (_) {
        speechClosing.delete(rec);
        speechRec = null;
        speechLastWritten = null;
      }
    }
    resetVoiceButton();
  }

  function releaseSpeechSession(rec) {
    speechClosing.delete(rec);
    if (!speechStartRequested || speechClosing.size) return;
    speechStartRequested = false;
    if (speechReleaseTimer != null) clearTimeout(speechReleaseTimer);
    speechReleaseTimer = null;
    if (!speechActive || document.hidden) { stopSpeechInput(); return; }
    // 仅兑现仍有效的启动请求；旧识别器的迟到事件不会接管新会话。
    startSpeechSession();
  }

  function scheduleSpeechResume() {
    if (!speechActive || document.hidden) { stopSpeechInput(); return; }
    speechResumeTimer = setTimeout(function () {
      speechResumeTimer = null;
      if (!speechActive) return;
      if (document.hidden ||
          (state.streaming && state.streamingSession === state.sessionId) ||
          (state.manualCompactRunning && state.manualCompactSession === state.sessionId)) {
        stopSpeechInput();
        return;
      }
      if (speechComposing || speechEditing) {
        // 等输入法完成提交与编辑回写，再以最新草稿和光标为新片段锚点。
        if (++speechResumeEditChecks > 40) {
          stopSpeechInput();
          toast("输入法尚未完成提交，听写已暂停；草稿已保留，可点击麦克风继续");
          return;
        }
        scheduleSpeechResume();
        return;
      }
      speechResumeEditChecks = 0;
      startSpeechInput(true);
    }, SPEECH_RESUME_DELAY_MS);
  }

  function writeSpeechResults(results) {
    speechResults = results;
    const text = results.map(function (part, index) {
      if (index >= speechConsumed.length) return part;
      const consumed = normalizeSpeechText(speechConsumed[index]);
      const current = normalizeSpeechText(part);
      if (!consumed) return part;
      // 编辑前已显示的部分成为普通草稿，后续只接入新增后缀。引擎若改写
      // 已冻结的临时前缀，忽略该条修订，避免覆盖用户编辑或重放旧语音。
      return current.startsWith(consumed) ? current.slice(consumed.length) : "";
    }).join("");
    applySpeechText(normalizeSpeechText(text));
  }

  function flushSpeechEdit() {
    if (speechComposing || speechEditBaseline == null) return;
    if (speechEditTimer != null) clearTimeout(speechEditTimer);
    speechEditTimer = null;
    const pos = input.selectionStart == null ? input.value.length : input.selectionStart;
    const end = input.selectionEnd == null ? pos : input.selectionEnd;
    speechBase = input.value.slice(0, pos);
    speechTail = input.value.slice(end);
    speechSelected = input.value.slice(pos, end);
    speechLastSelectionStart = pos;
    speechLastSelectionEnd = end;
    speechFollowCaret = true;
    speechLastWritten = input.value;
    speechConsumed = speechEditBaseline;
    speechEditBaseline = null;
    speechEditing = false;
    const pending = speechPendingResults;
    speechPendingResults = null;
    writeSpeechResults(pending || speechResults);
    if (speechEnded) {
      speechEnded = false;
      speechLastWritten = null;
    }
  }

  function scheduleSpeechEditFlush() {
    if (speechEditTimer != null) clearTimeout(speechEditTimer);
    speechEditTimer = setTimeout(function () {
      speechEditTimer = null;
      flushSpeechEdit();
    }, 0);
  }

  function beginSpeechEdit() {
    if (speechWriting || speechLastWritten == null) return false;
    if (!speechActive && !speechEnded) {
      stopSpeechInput(); // 显式停止后的迟到结果不应覆盖后续编辑。
      return false;
    }
    if (speechEditBaseline == null) speechEditBaseline = speechResults.slice();
    speechEditing = true;
    return true;
  }
  input.addEventListener("beforeinput", function () {
    if (beginSpeechEdit() && !speechComposing) scheduleSpeechEditFlush();
  });
  input.addEventListener("compositionstart", function () {
    if (beginSpeechEdit()) speechComposing = true;
  });
  input.addEventListener("compositionend", function () {
    if (!speechComposing) return;
    speechComposing = false;
    // 部分浏览器先触发 compositionend，再触发最终 input；等这一组事件
    // 提交完成后再写回，避免候选词被软件赋值打断。
    scheduleSpeechEditFlush();
  });
  input.addEventListener("input", function (event) {
    if (!beginSpeechEdit()) return;
    if (speechComposing || event.isComposing || speechEditTimer != null) return;
    flushSpeechEdit();
  });

  // 每次显式启动使用新实例；旧实例的迟到事件通过身份比对丢弃。
  function startSpeechSession() {
    if (!speechActive) return;
    if (speechClosing.size) {
      speechStartRequested = true;
      voiceBtn.classList.add("is-starting");
      syncSpeechLanguageUI();
      speechReleaseTimer = setTimeout(function () {
        speechReleaseTimer = null;
        if (!speechStartRequested) return;
        // 某些浏览器报错/abort 后不会再发 end；不能让旧实例永久阻塞麦克风。
        speechClosing.clear();
        speechStartRequested = false;
        if (!speechActive || document.hidden) { stopSpeechInput(); return; }
        startSpeechSession();
      }, SPEECH_RELEASE_TIMEOUT_MS);
      return;
    }
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) {
      stopSpeechInput();
      toast("当前浏览器不支持语音识别，请使用 Edge 或 Chrome");
      return;
    }
    let rec;
    try { rec = new SR(); }
    catch (err) { stopSpeechInput(); toast("语音识别启动失败：" + (err && err.message || err)); return; }
    speechRec = rec;
    speechStarting = true;
    rec.lang = speechLanguage;
    rec.continuous = true;
    rec.interimResults = true;
    let emptyNoticeShown = false;
    let receivedSpeechText = false;
    const ready = function () {
      if (speechRec !== rec || !speechActive) return;
      speechStarting = false;
      if (speechStartTimer != null) clearTimeout(speechStartTimer);
      speechStartTimer = null;
      voiceBtn.classList.remove("is-starting");
      voiceBtn.classList.add("is-recording");
      syncSpeechLanguageUI();
    };
    const emptyResult = function () {
      if (speechRec !== rec || !speechActive || emptyNoticeShown) return;
      emptyNoticeShown = true;
      toast("语音服务未返回可用文字，请检查麦克风和所选识别语言；草稿已保留");
    };
    rec.onstart = ready;
    rec.onaudiostart = ready;
    rec.onnomatch = emptyResult;
    rec.onresult = function (event) {
      if (speechRec !== rec) return;
      ready();
      const results = Array.from(event.results).map(function (result) {
        return result[0] && result[0].transcript || "";
      });
      if (results.length && results.every(part => !part.trim())) {
        // 某些服务会返回空 transcript；不能把可见的临时文本或草稿清掉。
        if (Array.from(event.results).some(result => result.isFinal)) emptyResult();
        return;
      }
      if (results.some(part => part.trim())) {
        receivedSpeechText = true;
        emptyNoticeShown = false;
        speechEmptyResumes = 0;
      }
      if (speechComposing || speechEditing) {
        speechPendingResults = results;
        return;
      }
      if (input.value !== speechLastWritten) {
        // 兼容未分发 input 的外部修改；保留新草稿，不重放之前的结果。
        if (beginSpeechEdit()) {
          speechPendingResults = results;
          flushSpeechEdit();
        }
        return;
      }
      // 先按规范连接全部结果，再清理中文空格，覆盖跨结果边界的空格。
      writeSpeechResults(results);
    };
    rec.onerror = function (event) {
      if (speechRec !== rec) return;
      const err = event && event.error || "";
      const wasActive = speechActive;
      stopSpeechInput();
      if (!wasActive) return; // 手动停止后的服务收尾/aborted 不重复打扰用户。
      const errors = {
        "no-speech": "暂未识别到语音，听写已结束；请点击麦克风重新开始",
        "aborted": "语音识别被浏览器中断，草稿已保留；可点击麦克风重新开始",
        "network": "语音识别服务网络异常，已停止录音；请检查网络后点击麦克风重试",
        "not-allowed": "麦克风权限被拒绝，请在浏览器地址栏允许麦克风访问",
        "service-not-allowed": "浏览器禁止使用语音识别服务，请检查浏览器设置",
        "audio-capture": "无法访问麦克风，请检查设备连接及是否被其他程序占用",
        "language-not-supported": "当前浏览器的语音服务不支持「" + speechLanguages.get(speechLanguage).label + "」，请在聊天设置中选择其他语言",
      };
      toast(errors[err] || "语音识别出错：" + err);
    };
    rec.onend = function () {
      if (speechRec === rec) {
        const unexpected = speechActive;
        const wasStarting = speechStarting;
        const canResume = unexpected && !wasStarting && !document.hidden &&
          !(state.streaming && state.streamingSession === state.sessionId) &&
          !(state.manualCompactRunning && state.manualCompactSession === state.sessionId) &&
          (receivedSpeechText || speechEmptyResumes++ < SPEECH_MAX_EMPTY_RESUMES);
        speechActive = canResume;
        speechStarting = canResume;
        speechRec = null;
        if (speechStartTimer != null) clearTimeout(speechStartTimer);
        speechStartTimer = null;
        if (speechEditing) {
          speechEnded = true;
          if (!speechComposing) scheduleSpeechEditFlush();
        } else {
          speechLastWritten = null;
        }
        if (canResume) {
          voiceBtn.classList.remove("is-recording");
          voiceBtn.classList.add("is-starting");
          syncSpeechLanguageUI();
          refreshComposerButtons();
          if (!speechResumeNoticeShown) {
            speechResumeNoticeShown = true;
            toast("语音服务短暂断开，正在继续听写；草稿已保留");
          }
          scheduleSpeechResume();
        } else {
          resetVoiceButton();
          if (unexpected) toast(wasStarting ? "语音服务未能完成启动，请点击麦克风重试"
            : "语音服务已结束，草稿已保留；可点击麦克风继续");
        }
      }
      // 即使旧实例已被隔离，也必须释放占用，才能安全处理下次显式启动。
      releaseSpeechSession(rec);
    };
    voiceBtn.classList.add("is-starting");
    syncSpeechLanguageUI();
    refreshComposerButtons();
    speechStartTimer = setTimeout(function () {
      speechStartTimer = null;
      if (speechRec !== rec || !speechStarting) return;
      stopSpeechInput();
      toast("语音服务启动超时，请确认麦克风权限后点击重试；草稿已保留");
    }, SPEECH_START_TIMEOUT_MS);
    try { rec.start(); }
    catch (err) {
      // start 同步抛错表示本次根本没有进入识别，不等待一个不会到来的 end。
      speechRec = null;
      speechClosing.delete(rec);
      stopSpeechInput();
      toast("语音识别启动失败：" + (err && err.message || err));
    }
  }

  function startSpeechInput(resume) {
    if ((state.streaming && state.streamingSession === state.sessionId) ||
        (state.manualCompactRunning && state.manualCompactSession === state.sessionId)) return;
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) {
      toast("当前浏览器不支持语音识别，请使用 Edge 或 Chrome");
      return;
    }
    stopSpeechInput();
    if (!resume) {
      speechEmptyResumes = 0;
      speechResumeNoticeShown = false;
    }
    if (speechDeferredLanguage) {
      speechLanguage = speechDeferredLanguage;
      speechDeferredLanguage = null;
    }
    const pos = input.selectionStart == null ? input.value.length : input.selectionStart;
    const end = input.selectionEnd == null ? pos : input.selectionEnd;
    speechBase = input.value.slice(0, pos);
    speechTail = input.value.slice(end);
    speechSelected = input.value.slice(pos, end);
    speechLastSelectionStart = pos;
    speechLastSelectionEnd = end;
    speechFollowCaret = true;
    speechResults = [];
    speechConsumed = [];
    speechLastWritten = input.value;
    speechActive = true;
    startSpeechSession();
  }

  voiceBtn.addEventListener("click", function () {
    if (speechActive) stopSpeechInput(true);
    else startSpeechInput(false);
  });
  window.addEventListener("pagehide", function () { stopSpeechInput(); });
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) stopSpeechInput();
  });
  if (speechLanguageSelect) {
    speechLanguages.languages.forEach(function (language) {
      const option = document.createElement("option");
      option.value = language.code;
      option.textContent = language.label;
      speechLanguageSelect.appendChild(option);
    });
    speechLanguageSelect.addEventListener("change", function () {
      const next = speechLanguages.get(speechLanguageSelect.value).code;
      return saveSpeechInputLanguage(next);
    });
  }
  syncSpeechLanguageUI();
  App.stopVoiceInput = function () { stopSpeechInput(); };
  App.loadSpeechInputSettings = loadSpeechInputSettings;

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
    if (App.chatSettingsSaving) return;
    App.chatSettingsLoadVersion = (App.chatSettingsLoadVersion || 0) + 1;
    chatSettingsModal.classList.add("hidden");
    chatSettingsModal.setAttribute("aria-hidden", "true");
    if (App.hideChatSettingsModal) App.hideChatSettingsModal();
  }

  // 两个接口同属“聊天设置”，共用一组确定/取消：打开时并行加载，确定时并行提交
  // ---------- 「每条消息重新标题」开关（会话独立配置，切换即保存） ----------
  // 与弹窗内三组数值配置的"确定才保存"语义分离，避免误改；打开弹窗时拉取回填
  async function loadSettingsRetitle() {
    if (!settingsRetitleRow || !settingsRetitleToggle) return;
    const sid = state.sessionId || "";
    if (!sid) {
      settingsRetitleRow.hidden = true; // 新对话（无会话文件）不显示该开关
      return;
    }
    settingsRetitleRow.hidden = false;
    settingsRetitleToggle.disabled = true;
    settingsRetitleToggle.checked = false;
    if (settingsRetitleStatus) settingsRetitleStatus.textContent = "读取中…";
    try {
      const data = await API.getRetitleSetting(sid);
      if (state.sessionId !== sid) return; // 等待期间已切换会话
      settingsRetitleToggle.checked = Boolean(data.enabled);
      if (settingsRetitleStatus) {
        const st = data.title_state || {};
        settingsRetitleStatus.textContent =
          "标题状态：" + (st.title_generated ? "已生成" : st.attempted ? "已尝试（失败不再自动重试）" : "未生成");
      }
    } catch (_) {
      if (settingsRetitleStatus) settingsRetitleStatus.textContent = "读取失败";
    } finally {
      settingsRetitleToggle.disabled = false;
    }
  }

  async function onSettingsRetitleChange() {
    const sid = state.sessionId || "";
    if (!sid) return;
    const enabled = settingsRetitleToggle.checked;
    settingsRetitleToggle.disabled = true;
    try {
      const res = await API.updateRetitleSetting(sid, enabled);
      toast(res.message || (enabled ? "已开启每条消息重新标题" : "已关闭每条消息重新标题"));
      if (settingsRetitleStatus) settingsRetitleStatus.textContent = "";
      await loadSettingsRetitle();
    } catch (err) {
      toast("保存失败：" + err.message);
      settingsRetitleToggle.checked = !enabled; // 保存失败回滚 UI
      await loadSettingsRetitle();
    } finally {
      settingsRetitleToggle.disabled = false;
    }
  }

  if (settingsRetitleToggle) {
    settingsRetitleToggle.addEventListener("change", onSettingsRetitleChange);
  }

  async function openChatSettings() {
    if (App.chatSettingsSaving) return;
    const version = App.chatSettingsLoadVersion = (App.chatSettingsLoadVersion || 0) + 1;
    const sid = state.sessionId;
    chatSettingsModal.classList.remove("hidden");
    chatSettingsModal.setAttribute("aria-hidden", "false");
    if (App.showChatSettingsModal) App.showChatSettingsModal();
    loadSpeechInputSettings();
    chatSettingsConfirm.disabled = true;
    loadSettingsRetitle();
    const permissionLoad = App.permissions ? App.permissions.loadSettings() : Promise.resolve();
    const results = await Promise.all([
      // 传 session_id：模型窗口按会话生效模型口径计算，与顶部 token 统计一致
      API.getHistoryCompactionConfig(state.sessionId).catch(function () { return null; }),
      API.getMcpToolConfig().catch(function () { return null; }),
      API.getNetworkRetryConfig().catch(function () { return null; }),
      API.getCompactionRetryConfig().catch(function () { return null; }),
      API.getVideoReadLimitConfig().catch(function () { return null; }),
      API.getToolConcurrencyConfig().catch(function () { return null; }),
      API.getSubAgentRetryConfig().catch(function () { return null; }),
      API.getSubAgentLimitsConfig().catch(function () { return null; }),
      permissionLoad,
    ]);
    if (version !== App.chatSettingsLoadVersion || state.sessionId !== sid) return;
    chatSettingsConfirm.disabled = false;
    const comp = results[0];
    const mcp = results[1];
    const retry = results[2];
    const compRetry = results[3];
    const videoLimit = results[4];
    const conc = results[5];
    const subRetry = results[6];
    const subLimits = results[7];
    const defaults = Object.assign({}, CHAT_SETTINGS_DEFAULTS, comp && comp.defaults || {},
      mcp && mcp.defaults || {}, retry && retry.defaults || {},
      compRetry && compRetry.defaults || {},
      videoLimit && videoLimit.defaults || {}, conc && conc.defaults || {},
      subRetry && subRetry.defaults || {});
    if (subLimits && subLimits.defaults) {
      defaults.sub_agent_max_rounds = subLimits.defaults.max_rounds;
      defaults.sub_agent_timeout_seconds = subLimits.defaults.timeout_seconds;
    }
    state.chatSettingsDefaults = defaults;
    // 加载失败时回退到后端文档默认值，用户仍可编辑保存
    toolCallTimeoutSeconds.value = mcp && mcp.call_timeout_seconds != null
      ? mcp.call_timeout_seconds : defaults.call_timeout_seconds;
    networkRetryMaxAttempts.value = retry && retry.max_attempts != null
      ? retry.max_attempts : defaults.network_retry_max_attempts;
    compactionRetryMaxAttempts.value = compRetry && compRetry.max_attempts != null
      ? compRetry.max_attempts : defaults.compaction_retry_max_attempts;
    videoReadMaxSeconds.value = videoLimit && videoLimit.max_seconds != null
      ? videoLimit.max_seconds : defaults.video_read_max_seconds;
    mcpToolWorkers.value = conc && conc.mcp_tool_workers != null
      ? conc.mcp_tool_workers : defaults.mcp_tool_workers;
    subAgentMaxConcurrent.value = conc && conc.sub_agent_max_concurrent != null
      ? conc.sub_agent_max_concurrent : defaults.sub_agent_max_concurrent;
    subAgentMaxRounds.value = subLimits && subLimits.max_rounds != null
      ? subLimits.max_rounds : defaults.sub_agent_max_rounds;
    subAgentTimeoutSeconds.value = subLimits && subLimits.timeout_seconds != null
      ? subLimits.timeout_seconds : defaults.sub_agent_timeout_seconds;
    subAgentFinalReplyRetryMax.value = subRetry && subRetry.final_reply_max_attempts != null
      ? subRetry.final_reply_max_attempts : defaults.sub_agent_final_reply_retry_max;
    subAgentStreamErrorRetryMax.value = subRetry && subRetry.stream_error_max_attempts != null
      ? subRetry.stream_error_max_attempts : defaults.sub_agent_stream_error_retry_max;
    subAgentTodoRemindMax.value = subRetry && subRetry.todo_remind_max != null
      ? subRetry.todo_remind_max : defaults.sub_agent_todo_remind_max;
    triggerRatio.value = comp && comp.trigger_ratio != null ? comp.trigger_ratio : defaults.trigger_ratio;
    summaryBudgetRatio.value = comp && comp.summary_budget_ratio != null ? comp.summary_budget_ratio : defaults.summary_budget_ratio;
    historyTargetTokens.value = comp && comp.target_tokens != null ? comp.target_tokens : defaults.target_tokens;
    renderHistoryTargetHint(comp);
    oversizedRejectFactor.value = comp && comp.oversized_reject_factor != null ? comp.oversized_reject_factor : defaults.oversized_reject_factor;
    maxOversizedRejections.value = comp && comp.max_oversized_rejections != null ? comp.max_oversized_rejections : defaults.max_oversized_rejections;
    renderEffectiveThresholdHint(comp);
    if (!comp || !mcp || !retry || !compRetry || !videoLimit || !conc || !subRetry || !subLimits) {
      toast((comp ? "" : "压缩策略配置加载失败；") +
        (mcp ? "" : "MCP 工具超时配置加载失败；") +
        (retry ? "" : "网络重试配置加载失败；") +
        (compRetry ? "" : "压缩重试配置加载失败；") +
        (videoLimit ? "" : "视频读取上限配置加载失败；") +
        (conc ? "" : "工具并发配置加载失败；") +
        (subRetry ? "" : "子智能体重试配置加载失败；") +
        (subLimits ? "" : "子智能体限制配置加载失败"));
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

  // 压缩目标提示：显示该模型窗口下的可设区间（后端会把越界值夹取到区间内），
  // 并区分"用户设定值"与"实际生效值"（被夹取时明确提示）。
  function renderHistoryTargetHint(comp) {
    if (!historyTargetHint) { return; }
    const limits = comp && comp.target_limits;
    const fmt = function (n) {
      return n >= 1000000 ? (n / 1000000).toFixed(n % 1000000 ? 1 : 0) + "M"
        : n >= 1000 ? Math.round(n / 1000) + "k" : String(n);
    };
    if (!limits || !limits.max) {
      historyTargetHint.textContent = "";
      historyTargetHint.hidden = true;
      return;
    }
    const raw = readSettingNumber(historyTargetTokens);
    let text = "可设区间 " + fmt(limits.min) + " – " + fmt(limits.max) + " tokens"
      + "（窗口 " + fmt(limits.window) + " × 8%/40% 下限、40% 上限）";
    if (raw != null && raw > 0 && (raw < limits.min || raw > limits.max)) {
      const clamped = Math.max(limits.min, Math.min(raw, limits.max));
      text += "；当前 " + fmt(raw) + " 越界，将按 " + fmt(clamped) + " 生效";
    }
    historyTargetHint.textContent = text;
    historyTargetHint.hidden = false;
  }

  function applyChatSettingsDefaults() {
    const defaults = Object.assign({}, CHAT_SETTINGS_DEFAULTS, state.chatSettingsDefaults || {});
    toolCallTimeoutSeconds.value = defaults.call_timeout_seconds;
    networkRetryMaxAttempts.value = defaults.network_retry_max_attempts;
    compactionRetryMaxAttempts.value = defaults.compaction_retry_max_attempts;
    videoReadMaxSeconds.value = defaults.video_read_max_seconds;
    mcpToolWorkers.value = defaults.mcp_tool_workers;
    subAgentMaxConcurrent.value = defaults.sub_agent_max_concurrent;
    subAgentMaxRounds.value = defaults.sub_agent_max_rounds;
    subAgentTimeoutSeconds.value = defaults.sub_agent_timeout_seconds;
    subAgentFinalReplyRetryMax.value = defaults.sub_agent_final_reply_retry_max;
    subAgentStreamErrorRetryMax.value = defaults.sub_agent_stream_error_retry_max;
    subAgentTodoRemindMax.value = defaults.sub_agent_todo_remind_max;
    triggerRatio.value = defaults.trigger_ratio;
    summaryBudgetRatio.value = defaults.summary_budget_ratio;
    historyTargetTokens.value = defaults.target_tokens;
    oversizedRejectFactor.value = defaults.oversized_reject_factor;
    maxOversizedRejections.value = defaults.max_oversized_rejections;
    toast("已恢复聊天设置默认值，点击确定后生效");
  }

  chatSettingsReset.addEventListener("click", applyChatSettingsDefaults);

  // 聊天设置字段校验：返回无效字段名列表。校验域与后端一致：
  // - 超长结果拒绝系数：>=0（0=关闭该功能）
  // - 工具执行超时/网络重试次数：>=0（0=不限制）
  // - 压缩失败重试次数：任意整数（0 或负数=不限制，一直重试）
  // - 视频最大读取秒数：5–3600（越界/非法值读取端自动钳制，此处前置校验）
  // - 工具并发执行两项：>=1
  // - 触发比例/摘要预算比例/连续拒绝上限：>0
  function collectInvalidChatSettings(compConfig, mcpConfig, retryConfig, compRetryConfig, videoLimitConfig, concConfig, subRetryConfig, subLimitsConfig) {
    const isNum = function (v) { return v != null && Number.isFinite(v); };
    const rows = [
      ["工具执行超时", mcpConfig.call_timeout_seconds, function (v) { return isNum(v) && v >= 0; }],
      ["网络失败重试次数", retryConfig.max_attempts, function (v) { return isNum(v) && v >= 0; }],
      ["压缩失败重试次数", compRetryConfig.max_attempts, function (v) { return isNum(v); }],
      ["视频最大读取秒数", videoLimitConfig.max_seconds, function (v) { return isNum(v) && v >= 5 && v <= 3600; }],
      ["MCP 工具并发线程数", concConfig.mcp_tool_workers, function (v) { return isNum(v) && v >= 1; }],
      ["子智能体并发上限", concConfig.sub_agent_max_concurrent, function (v) { return isNum(v) && v >= 1; }],
      ["子智能体轮次上限", subLimitsConfig.max_rounds, function (v) { return isNum(v) && Number.isInteger(v) && v >= 0; }],
      ["子智能体整体超时", subLimitsConfig.timeout_seconds, function (v) { return isNum(v) && v >= 0; }],
      ["子任务空收尾重试次数", subRetryConfig.final_reply_max_attempts, function (v) { return isNum(v); }],
      ["子任务断流续跑次数", subRetryConfig.stream_error_max_attempts, function (v) { return isNum(v); }],
      ["子任务计划未完成提醒次数", subRetryConfig.todo_remind_max, function (v) { return isNum(v); }],
      ["触发比例", compConfig.trigger_ratio, function (v) { return isNum(v) && v > 0; }],
      ["摘要预算比例", compConfig.summary_budget_ratio, function (v) { return isNum(v) && v > 0; }],
      ["压缩目标 tokens", compConfig.target_tokens, function (v) { return isNum(v) && v >= 0; }],
      ["超长结果拒绝系数", compConfig.oversized_reject_factor, function (v) { return isNum(v) && v >= 0; }],
      ["连续拒绝上限", compConfig.max_oversized_rejections, function (v) { return isNum(v) && v > 0; }],
    ];
    return rows.filter(function (row) { return !row[2](row[1]); }).map(function (row) { return row[0]; });
  }

  chatSettingsConfirm.addEventListener("click", async function () {
    if (chatSettingsConfirm.disabled || App.chatSettingsSaving) return;
    const compConfig = {
      trigger_ratio: readSettingNumber(triggerRatio),
      summary_budget_ratio: readSettingNumber(summaryBudgetRatio),
      target_tokens: readSettingNumber(historyTargetTokens),
      oversized_reject_factor: readSettingNumber(oversizedRejectFactor),
      max_oversized_rejections: readSettingNumber(maxOversizedRejections),
    };
    const mcpConfig = {
      call_timeout_seconds: readSettingNumber(toolCallTimeoutSeconds),
    };
    const retryConfig = {
      max_attempts: readSettingNumber(networkRetryMaxAttempts),
    };
    const compRetryConfig = {
      max_attempts: readSettingNumber(compactionRetryMaxAttempts),
    };
    const videoLimitConfig = {
      max_seconds: readSettingNumber(videoReadMaxSeconds),
    };
    const concConfig = {
      mcp_tool_workers: readSettingNumber(mcpToolWorkers),
      sub_agent_max_concurrent: readSettingNumber(subAgentMaxConcurrent),
    };
    const subRetryConfig = {
      final_reply_max_attempts: readSettingNumber(subAgentFinalReplyRetryMax),
      stream_error_max_attempts: readSettingNumber(subAgentStreamErrorRetryMax),
      todo_remind_max: readSettingNumber(subAgentTodoRemindMax),
    };
    const subLimitsConfig = {
      max_rounds: readSettingNumber(subAgentMaxRounds),
      timeout_seconds: readSettingNumber(subAgentTimeoutSeconds),
    };
    const invalid = collectInvalidChatSettings(compConfig, mcpConfig, retryConfig, compRetryConfig, videoLimitConfig, concConfig, subRetryConfig, subLimitsConfig);
    if (invalid.length) {
      toast("请填写有效数值：" + invalid.join("、"));
      if (App.revealInvalidChatSetting) App.revealInvalidChatSetting(invalid[0]);
      return;
    }
    chatSettingsConfirm.disabled = true;
    if (App.setChatSettingsBusy) App.setChatSettingsBusy(true);
    const results = await Promise.allSettled([
      API.updateHistoryCompactionConfig(compConfig, state.sessionId),
      API.updateMcpToolConfig(mcpConfig),
      API.updateNetworkRetryConfig(retryConfig),
      API.updateCompactionRetryConfig(compRetryConfig),
      API.updateVideoReadLimitConfig(videoLimitConfig),
      API.updateToolConcurrencyConfig(concConfig),
      API.updateSubAgentRetryConfig(subRetryConfig),
      API.updateSubAgentLimitsConfig(subLimitsConfig),
      ...(App.permissions ? [App.permissions.saveSettings()] : []),
    ]);
    chatSettingsConfirm.disabled = false;
    if (App.setChatSettingsBusy) App.setChatSettingsBusy(false);
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
   * 保存当前输入到该会话的草稿（文本 + 待发附件 + 引用快照，附件保留 objUrl 引用）。
   * 切换会话前调用；新对话（sessionId=null）存到 "" 键。
   */
  function saveSessionDraft() {
    const key = state.sessionId || "";
    const text = input.value;
    const media = state.pendingMedia.map(function (m) { return Object.assign({}, m); });
    const quotes = (state.pendingQuotes || []).map(function (q) { return Object.assign({}, q); });
    if (!text.trim() && !media.length && !quotes.length) {
      delete state.sessionDrafts[key];
      return;
    }
    state.sessionDrafts[key] = { text: text, media: media, quotes: quotes };
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
      state.pendingQuotes = (draft.quotes || []).map(function (q) { return Object.assign({}, q); });
    } else {
      input.value = "";
      state.pendingMedia = [];
      state.pendingQuotes = [];
    }
    if (App.closeSlashCommands) App.closeSlashCommands();
    App.renderComposerAttachments();
    App.renderComposerQuotes();
    App.autosize();
  }

  // ---------- 导出（供其它模块经 App.* 调用） ----------
  App.refreshComposerButtons = refreshComposerButtons;
  App.autosize = autosize;
  App.closeChatSettings = closeChatSettings;
  App.openChatSettings = openChatSettings;
  App.submitSteerOrQueue = submitSteerOrQueue;
  App.renderPendingOutbox = renderPendingOutbox;
  App.flushPendingMessages = flushPendingMessages;
  App.saveSessionDraft = saveSessionDraft;
  App.restoreSessionDraft = restoreSessionDraft;
})(window.App);
