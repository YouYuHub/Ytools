/**
 * 消息渲染与聊天区交互
 * - 历史记录回放（用户/思考/回答/工具/压缩/usage）与流式分块构建器
 * - 用户消息多模态部件渲染、md 表格复制（TSV+HTML/图片）
 * - 右侧问题导航、回到底部、代码块复制按钮滚动钉住
 * 依赖：app/core.js、Markdown、FormatUtils、Prism；App.*：builtin/media 模块
 */
(function (App) {
  "use strict";
  const {
    el, scrollToBottom, QNAV_MAX_DASHES, chatScroll,
    chatInner, qnav, qnavRail, qnavPanel,
    scrollBottomBtn
  } = App;

  // 渲染历史记录（工具调用与结果按名称顺序配对）
  function renderRecords(records) {
    const pendingTools = {}; // name -> [tool ui]
    const pendingCompactions = {}; // scope -> [尚未完成的压缩 ui]

    records.forEach(function (rec) {
      if (rec.kind === "user") {
        appendUserMessage(rec.content, rec.ts);
        return;
      }
      if (rec.kind === "think") {
        appendTime(rec.ts);
        const think = buildThinkBlock();
        think.setText(rec.content);
        chatInner.appendChild(think.wrap);
        return;
      }
      if (rec.kind === "assistant") {
        appendTime(rec.ts);
        appendStaticAssistant(rec.content);
        return;
      }
      if (rec.kind === "tool") {
        appendTime(rec.ts);
        const ui = buildToolBlock(rec.name);
        ui.setInput(FormatUtils.prettyJson(rec.args));
        chatInner.appendChild(ui.wrap);
        // ask_user 历史记录附一张可点击的提问卡片：刷新后仍能重新打开回答窗口
        if (rec.name === App.ASK_USER_TOOL_NAME) {
          const askQuestions = App.parseAskQuestionsFromArgs(rec.args);
          if (askQuestions) chatInner.appendChild(App.buildAskBlock(askQuestions));
        }
        (pendingTools[rec.name] = pendingTools[rec.name] || []).push(ui);
        return;
      }
      if (rec.kind === "toolResult") {
        const queue = pendingTools[rec.name];
        let ui = queue && queue.shift();
        if (!ui) {
          appendTime(rec.ts);
          ui = buildToolBlock(rec.name);
          ui.setInput(FormatUtils.prettyJson(rec.args));
          chatInner.appendChild(ui.wrap);
        }
        ui.setOutput(rec.result);
        ui.finish();
        return;
      }
      if (rec.kind === "usage") {
        chatInner.appendChild(el("div", "round-usage", FormatUtils.usageText(rec.usage)));
        return;
      }
      if (rec.kind === "notice") {
        chatInner.appendChild(el("div", "notice-bar", rec.content));
        return;
      }
      if (rec.kind === "compaction") {
        // 任务中断遗留的未完成压缩（有 start、无 done）：置为中断态展示，
        // 未覆盖的轮次由后端在下次请求按需重新压缩
        if (rec.interrupted) {
          appendTime(rec.ts);
          const interruptedBar = el("div", "compaction-bar compaction-interrupted is-open");
          const head = el("div", "compaction-head");
          head.appendChild(el("span", "compaction-status-icon", "!"));
          head.appendChild(el("span", "compaction-title",
            (rec.scope === "session" ? "跨轮历史" : "本轮工具轨迹") + "压缩已中断，将在下次请求重新执行"));
          interruptedBar.appendChild(head);
          if (rec.content) {
            const body = el("div", "compaction-body");
            const pre = el("pre", "compaction-preview", rec.content);
            body.appendChild(pre);
            interruptedBar.appendChild(body);
          }
          chatInner.appendChild(interruptedBar);
          return;
        }
        const compactionPayload = {
          scope: rec.scope,
          phase: rec.phase,
          compress_context: rec.phase === "start" ? rec.content : "",
          context_summary: rec.phase === "start" ? rec.content : "",
          // done 记录携带最终摘要全文，刷新后仍可在压缩块中回放
          summary_text: rec.phase === "done" ? (rec.summary_text || "") : "",
          compress_usage: rec.scope === "round" ? rec.usage : null,
          summary_usage: rec.scope === "session" ? rec.usage : null,
          before_tokens: rec.before_tokens,
          after_tokens: rec.after_tokens,
          compress_index: rec.compress_index,
          block_count: rec.block_count,
          error: rec.error || "",
        };
        const scope = compactionPayload.scope === "session" ? "session" : "round";
        const pendingQueue = pendingCompactions[scope];
        const compactionUsage = compactionPayload.compress_usage || compactionPayload.summary_usage;
        const isMergeEvent = compactionUsage && Number(compactionUsage.merge_block_count) > 0;
        if (compactionPayload.phase === "aborted") {
          // 失败记录：把此前未闭合的运行块转为失败态（无则直接新建失败块）
          appendTime(rec.ts);
          if (pendingQueue && pendingQueue.length) {
            pendingQueue.shift().update({ phase: "aborted", error: compactionPayload.error });
            pendingCompactions[scope] = [];
          } else {
            const failedUi = buildCompactionBlock(compactionPayload);
            chatInner.appendChild(failedUi.wrap);
          }
          return;
        }
        if (compactionPayload.phase === "done" && !isMergeEvent && pendingQueue && pendingQueue.length) {
          pendingQueue.shift().update(compactionPayload);
          if (!pendingQueue.length) delete pendingCompactions[scope];
          return;
        }
        appendTime(rec.ts);
        const compactionUi = buildCompactionBlock(compactionPayload);
        chatInner.appendChild(compactionUi.wrap);
        if (compactionPayload.phase === "start") {
          (pendingCompactions[scope] = pendingCompactions[scope] || []).push(compactionUi);
        }
        return;
      }
    });

    // 没有等到结果的工具（历史中断）也标记结束
    Object.keys(pendingTools).forEach(function (name) {
      pendingTools[name].forEach(function (ui) { ui.finish(); });
    });
    // 历史渲染完成后刷新提问卡片可答性（带后续对话的旧提问不可再答）
    App.refreshAskBlockStates();
  }

  function appendTime(ts) {
    if (!ts) return;
    chatInner.appendChild(el("div", "msg-time stage-time", FormatUtils.fmtTime(ts)));
  }

  // 压缩进度块：与思考/工具块保持一致。开始态可展开查看待压缩内容预览，
  // 并实时追加压缩模型的流式输出（思考过程 / 摘要正文逐帧增量）；
  // 完成态显示最终摘要全文、压缩前后 token、摘要块/轮次和降级信息。
  // SSE delta/done 事件更新同一块；刷新页面后由历史 done 记录的
  // summary_text 重建摘要展示。
  function buildCompactionBlock(compaction) {
    const scope = compaction.scope === "session" ? "session" : "round";
    const scopeLabel = scope === "session" ? "跨轮历史" : "本轮工具轨迹";
    const box = el("div", "compaction-bar");
    const head = el("button", "compaction-head");
    head.type = "button";
    const iconWrap = el("span", "compaction-status-icon");
    const title = el("span", "compaction-title");
    const chevron = el("span", "compaction-chevron");
    chevron.innerHTML = '<svg class="icon" viewBox="0 0 24 24"><path d="m6 9 6 6 6-6"/></svg>';
    head.appendChild(iconWrap);
    head.appendChild(title);
    head.appendChild(chevron);
    const body = el("div", "compaction-body");
    let isOpen = compaction.phase !== "done";
    let latestCompaction = Object.assign({}, compaction);
    let previewText = String(
      latestCompaction.compress_context || latestCompaction.context_summary || ""
    );
    // 运行态累计文本（delta 追加；render 重建 DOM 时据此恢复）
    let reasoningText = "";
    let liveSummaryText = "";
    let latestUsage = null;
    // 增量 DOM 引用（render 重建时重置）
    let thinkPre = null;
    let summaryPre = null;
    head.addEventListener("click", function () {
      if (!body.children.length) return;
      box.classList.toggle("open");
      isOpen = box.classList.contains("open");
      head.setAttribute("aria-expanded", isOpen ? "true" : "false");
    });
    // 展开态下双击显示区域即可折叠（展开仍走头部按钮）
    body.title = "双击折叠";
    body.addEventListener("dblclick", function () {
      if (!body.children.length) return;
      if (!box.classList.contains("open")) return;
      box.classList.remove("open");
      isOpen = false;
      head.setAttribute("aria-expanded", "false");
    });
    box.appendChild(head);
    box.appendChild(body);

    function safeNumber(value) {
      const number = Number(value);
      return Number.isFinite(number) ? number : null;
    }

    function appendLabeledPre(labelText, extraClass) {
      body.appendChild(el("div", "compaction-label", labelText));
      const pre = document.createElement("pre");
      pre.className = "compaction-preview" + (extraClass ? " " + extraClass : "");
      body.appendChild(pre);
      return pre;
    }

    function scrollToPreBottom(pre) {
      if (pre) {
        pre.scrollTop = pre.scrollHeight;
        pre.scrollLeft = 0;
      }
    }

    /**
     * 追加压缩模型流式增量（phase=delta 事件）：
     * - reasoning_content -> “压缩模型思考”区（弱化色，独立滚动）
     * - content           -> “摘要正文（生成中）”区
     * 首个增量到达时自动展开块体；done 后到达的残留 delta 直接忽略。
     */
    function appendDelta(delta) {
      if (!delta || latestCompaction.phase === "done") return;
      const rc = typeof delta.reasoning_content === "string" ? delta.reasoning_content : "";
      const ct = typeof delta.content === "string" ? delta.content : "";
      if (!rc && !ct) return;
      if (!box.classList.contains("open")) {
        isOpen = true;
        box.classList.add("open");
        head.setAttribute("aria-expanded", "true");
      }
      if (rc) {
        reasoningText += rc;
        if (!thinkPre || !thinkPre.parentNode) {
          thinkPre = appendLabeledPre("压缩模型思考", "compaction-think");
        }
        thinkPre.textContent += rc;
        scrollToPreBottom(thinkPre);
      }
      if (ct) {
        liveSummaryText += ct;
        if (!summaryPre || !summaryPre.parentNode) {
          summaryPre = appendLabeledPre("摘要正文（生成中）", "compaction-summary");
        }
        summaryPre.textContent += ct;
        scrollToPreBottom(summaryPre);
      }
    }

    function render(next) {
      latestCompaction = Object.assign({}, latestCompaction, next || {});
      const isAborted = latestCompaction.phase === "aborted";
      const nextPhase = !isAborted && latestCompaction.phase === "done" ? "done" : (isAborted ? "aborted" : "start");
      const usage = next && (next.compress_usage || next.summary_usage || next.usage);
      const preview = next && (next.compress_context || next.context_summary);
      if (preview) previewText = String(preview);
      if (usage && typeof usage === "object") {
        latestUsage = Object.assign({}, latestUsage || {}, usage);
      }
      thinkPre = null;
      summaryPre = null;
      box.classList.toggle("is-running", nextPhase === "start");
      box.classList.toggle("is-done", nextPhase === "done");
      box.classList.toggle("is-failed", isAborted);
      iconWrap.innerHTML = nextPhase === "start"
        ? '<svg class="icon compaction-icon" viewBox="0 0 24 24"><path d="M21 12a9 9 0 1 1-6.2-8.56"/></svg>'
        : nextPhase === "aborted"
          ? '<svg class="icon compaction-icon" viewBox="0 0 24 24"><path d="M12 8v5"/><circle cx="12" cy="16.5" r="0.6" fill="currentColor"/><circle cx="12" cy="12" r="9.2"/></svg>'
          : '<svg class="icon compaction-icon" viewBox="0 0 24 24"><path d="M20 6 9 17l-5-5"/></svg>';
      title.textContent = nextPhase === "start"
        ? "正在压缩 · " + scopeLabel
        : nextPhase === "aborted"
          ? "压缩失败 · " + scopeLabel
          : "上下文压缩完成 · " + scopeLabel;
      body.innerHTML = "";

      if (previewText) {
        body.appendChild(el("div", "compaction-label",
          nextPhase === "start" ? "待压缩内容预览" : "压缩前内容预览"));
        const pre = document.createElement("pre");
        pre.className = "compaction-preview";
        pre.textContent = previewText;
        body.appendChild(pre);
      }

      if (isAborted) {
        // 失败态：展示错误信息与已生成部分（若有），原始对话未受影响
        body.appendChild(el("div", "compaction-detail compaction-error-detail",
          String(latestCompaction.error || "压缩模型调用失败，本次未写入任何摘要；历史对话保持不变，可稍后重试")));
        if (reasoningText) {
          appendLabeledPre("已生成的思考（未生效）", "compaction-think").textContent = reasoningText;
        }
        if (liveSummaryText) {
          appendLabeledPre("已生成的摘要（未生效）", "compaction-summary").textContent = liveSummaryText;
        }
        return;
      }

      if (nextPhase === "start") {
        // 运行态：已收到的思考 / 摘要增量原样重建；尚无输出时显示处理中提示
        if (reasoningText) {
          thinkPre = appendLabeledPre("压缩模型思考", "compaction-think");
          thinkPre.textContent = reasoningText;
          scrollToPreBottom(thinkPre);
        }
        if (liveSummaryText) {
          summaryPre = appendLabeledPre("摘要正文（生成中）", "compaction-summary");
          summaryPre.textContent = liveSummaryText;
          scrollToPreBottom(summaryPre);
        }
        if (!reasoningText && !liveSummaryText) {
          body.appendChild(el("div", "compaction-detail", "压缩模型处理中…"));
        }
      } else {
        // 完成态：优先显示 done 事件/历史记录携带的最终摘要全文
        const finalSummary = String(
          latestCompaction.summary_text || liveSummaryText || ""
        ).trim();
        if (finalSummary) {
          appendLabeledPre("摘要正文", "compaction-summary").textContent = finalSummary;
        }
        const usageText = FormatUtils.compactionUsageText(latestUsage);
        if (usageText) body.appendChild(el("div", "compaction-usage", usageText));
        const detail = [];
        const before = safeNumber(latestCompaction.before_tokens != null
          ? latestCompaction.before_tokens : latestUsage && latestUsage.before_tokens);
        const after = safeNumber(latestCompaction.after_tokens != null
          ? latestCompaction.after_tokens : latestUsage && latestUsage.after_tokens);
        if (before !== null && after !== null) {
          detail.push("上下文 " + FormatUtils.fmtNum(before) + " → " + FormatUtils.fmtNum(after) +
            "（节省 " + FormatUtils.fmtNum(Math.max(0, before - after)) + "）");
        }
        const compressedRounds = safeNumber(latestUsage && latestUsage.compressed_rounds);
        const blockCount = safeNumber(latestCompaction.block_count != null
          ? latestCompaction.block_count : latestUsage && latestUsage.block_count);
        const mergeBlockCount = safeNumber(latestUsage && latestUsage.merge_block_count);
        const compressIndex = safeNumber(latestCompaction.compress_index != null
          ? latestCompaction.compress_index : latestUsage && latestUsage.compress_index);
        if (compressedRounds !== null && compressedRounds > 0) {
          detail.push("压缩旧轮次 " + FormatUtils.fmtNum(compressedRounds));
        }
        if (blockCount !== null && blockCount > 0) {
          detail.push("摘要块 " + FormatUtils.fmtNum(blockCount));
        }
        if (mergeBlockCount !== null && mergeBlockCount > 0) {
          detail.push("合并 " + FormatUtils.fmtNum(mergeBlockCount) + " 个摘要块");
        }
        if (compressIndex !== null && compressIndex > 0) {
          detail.push("已覆盖工具结果 " + FormatUtils.fmtNum(compressIndex) + " 个");
        }
        if (latestUsage && latestUsage.fallback) detail.push("使用降级摘要");
        if (detail.length) body.appendChild(el("div", "compaction-detail", detail.join(" · ")));
        if (!finalSummary && !usageText && !detail.length) {
          body.appendChild(el("div", "compaction-detail", "压缩已完成"));
        }
      }
      head.setAttribute("aria-expanded", isOpen ? "true" : "false");
      box.classList.toggle("open", isOpen && body.children.length > 0);
    }

    render(compaction);
    return {
      wrap: box,
      update: render,
      appendDelta: appendDelta,
    };
  }

  // ---------- 消息渲染 ----------
  // Prism 语法高亮：仅处理容器内 language-* 的代码元素（diff 组合语言由插件处理）
  function highlightCodeBlocks(container) {
    if (window.Prism && container) Prism.highlightAllUnder(container);
  }

  // 用户气泡：content 可为字符串或多部件列表（多模态消息与其历史回放）。
  // 列表取 text 部件作为正文；image_url 渲染图片缩略图；video_url 渲染视频
  // 首帧（浏览器原生 <video>，#t=0.1 确保首帧绘制）；input_audio 渲染固定
  // 格式音频徽标；docs 为发送时随消息的会话文档快照（file_memory，仅前端
  // 展示，不进入消息内容部件）
  function appendUserMessage(content, ts, sessionId, docs, container) {
    const msg = el("div", "msg msg-user");
    if (ts) msg.appendChild(el("div", "msg-time", FormatUtils.fmtTime(ts)));
    const bubble = el("div", "msg-bubble");
    const parts = Array.isArray(content) ? content : null;
    let text = String(content == null ? "" : content);
    if (parts) {
      text = parts
        .map(function (part) {
          return part && part.type === "text" && typeof part.text === "string" ? part.text : "";
        })
        .filter(function (t) { return t.trim(); })
        .join("\n");
    }
    // 布局：媒体（图片/视频/音频/文档）在上、文字在下——媒体是视觉主体先入眼，
    // 配文紧随其后（GPT Web / Claude 同款顺序）
    if (parts) {
      const mediaRow = el("div", "msg-user-media");
      parts.forEach(function (part) {
        if (!part || typeof part !== "object") return;
        const type = part.type || "";
        if (type === "image_url") {
          const url = part.image_url && typeof part.image_url.url === "string" ? part.image_url.url : "";
          if (!url) return;
          const src = App.resolveMediaSrc(url, sessionId);
          if (!src) return;
          const thumb = el("img", "msg-user-media-thumb", "");
          thumb.src = src;
          thumb.alt = "附件图片";
          thumb.title = "点击预览";
          thumb.addEventListener("click", function () {
            App.openMediaPreview({ type: "image", src: src, title: "图片" });
          });
          mediaRow.appendChild(thumb);
          return;
        }
        if (type === "video_url") {
          // 与图片一致：渲染视频首帧缩略图，点击后在预览模态框中播放
          const url = part.video_url && typeof part.video_url.url === "string" ? part.video_url.url : "";
          if (!url) return;
          const src = App.resolveMediaSrc(url, sessionId);
          if (!src) return;
          const video = el("video", "msg-user-media-thumb", "");
          video.src = src + "#t=0.1";
          video.muted = true;
          video.preload = "metadata";
          video.playsInline = true;
          video.title = "点击播放";
          video.addEventListener("click", function () {
            App.openMediaPreview({ type: "video", src: src, title: "视频" });
          });
          mediaRow.appendChild(video);
          return;
        }
        // 音频：固定格式徽标（不内联播放），点击在预览模态框中收听
        if (type === "input_audio") {
          const mediaRef = part.input_audio && typeof part.input_audio.data === "string" ? part.input_audio.data : "";
          const audioSrc = App.resolveMediaSrc(mediaRef, sessionId);
          const chip = el("div", "msg-user-media-audio", mediaChipLabel(mediaRef, "🎵 音频"));
          chip.title = "点击收听";
          if (audioSrc) {
            chip.classList.add("clickable");
            chip.addEventListener("click", function () {
              App.openMediaPreview({ type: "audio", src: audioSrc, title: "音频" });
            });
          }
          mediaRow.appendChild(chip);
          return;
        }
      });
      // 随消息发送的会话文档（前端展示块；内容经系统提示词注入，不进 content）
      (docs || []).forEach(function (doc) {
        if (!doc || !doc.filename) return;
        const chip = el("div", "msg-user-media-doc", "📄 " + doc.filename);
        chip.title = "点击预览";
        chip.classList.add("clickable");
        chip.addEventListener("click", function () {
          App.openMediaPreviewForDocument(doc);
        });
        mediaRow.appendChild(chip);
      });
      if (mediaRow.childNodes.length) bubble.appendChild(mediaRow);
    }
    if (text) bubble.appendChild(el("div", "msg-user-text", text));
    msg.appendChild(bubble);
    // container 缺省追加到会话流末尾；传入正在流式的助手消息节点时，
    // 气泡嵌入其当前内容之后（工具结果位置），保持注入消息的时间顺序
    (container || chatInner).appendChild(msg);
    scrollToBottom();
    return msg;
  }

  // 媒体徽标文案：固定格式（图标+类型+名称），名称取 media:// 引用的存储文件名
  function mediaChipLabel(mediaRef, icon) {
    if (mediaRef && mediaRef.indexOf("media://") === 0) {
      const stored = mediaRef.slice("media://".length);
      const dot = stored.lastIndexOf(".");
      return icon + " · " + (dot > 0 ? stored.slice(0, dot) : stored);
    }
    return icon;
  }

  function appendStaticAssistant(content) {
    const body = el("div", "msg-assistant-body");
    body.innerHTML = Markdown.render(content);
    highlightCodeBlocks(body);
    chatInner.appendChild(body);
    updateCodeblockCopyButtons();
  }

  function buildThinkBlock() {
    const wrap = el("div", "think-block");
    const toggle = el("button", "think-toggle");
    toggle.innerHTML = '<span class="think-dots"><i></i><i></i><i></i></span><svg class="icon" viewBox="0 0 24 24"><path d="m9 18 6-6-6-6"/></svg><span>思考过程</span>';
    const content = el("div", "think-content");
    toggle.addEventListener("click", function () { wrap.classList.toggle("open"); });
    // 展开态下双击显示区域即可折叠（展开仍走头部按钮）
    content.title = "双击折叠";
    content.addEventListener("dblclick", function () { wrap.classList.remove("open"); });
    wrap.appendChild(toggle);
    wrap.appendChild(content);
    return {
      wrap: wrap,
      setText: function (t) { content.textContent = t; },
      add: function (delta) { content.textContent += delta; },
      streaming: function () { wrap.classList.add("is-streaming"); },
      done: function () { wrap.classList.remove("is-streaming"); },
    };
  }

  // 工具调用块：头部（名称+状态）+ 可展开主体（输入 JSON / 输出文本）
  // 默认折叠，不自动展开（避免流式期间反复撑高页面）；用户点开即可看到
  // 逐帧流入的参数数据。上游把 arguments 整段一次性下发时（部分供应商
  // 不分片），由打字机揭示器按节奏逐步显示，模拟逐帧生成的观感。
  function buildToolBlock(name) {
    const wrap = el("div", "tool-block");
    const head = el("button", "tool-block-head");
    const statusIcon = '<svg class="icon tool-spin" viewBox="0 0 24 24"><path d="M21 12a9 9 0 1 1-6.2-8.56"/></svg>';
    head.innerHTML =
      statusIcon +
      "<span>调用工具</span>" +
      '<span class="tool-block-name"></span>' +
      '<svg class="icon tool-block-chevron" viewBox="0 0 24 24"><path d="m6 9 6 6 6-6"/></svg>';
    head.querySelector(".tool-block-name").textContent = name || "tool";

    const body = el("div", "tool-block-body");
    const inLabel = el("span", "tool-io-label", "输入");
    const inPre = el("pre", "tool-io", "");
    const outLabel = el("span", "tool-io-label", "输出");
    const outPre = el("pre", "tool-io", "");
    body.appendChild(inLabel);
    body.appendChild(inPre);
    body.appendChild(outLabel);
    body.appendChild(outPre);

    let streaming = false;
    // 打字机揭示器：小增量（真分片流式）直接追加；大增量（整段下发）进入
    // 揭示队列按固定节奏逐步显示。折叠态下照常消费，用户展开时看到的是
    // 进行中的生成过程。
    let revealQueue = "";      // 待揭示的剩余文本
    let revealTimer = null;
    const REVEAL_CHARS_PER_TICK = 6;   // 每 tick 揭示的字符数
    const REVEAL_INTERVAL_MS = 16;     // tick 间隔（约每秒 375 字符）

    function stopReveal() {
      if (revealTimer) {
        clearInterval(revealTimer);
        revealTimer = null;
      }
    }

    function startReveal() {
      if (revealTimer) return;
      revealTimer = setInterval(function () {
        if (!revealQueue.length) {
          stopReveal();
          return;
        }
        const piece = revealQueue.slice(0, REVEAL_CHARS_PER_TICK);
        revealQueue = revealQueue.slice(REVEAL_CHARS_PER_TICK);
        inPre.textContent += piece;
        inPre.scrollTop = inPre.scrollHeight;
      }, REVEAL_INTERVAL_MS);
    }

    function flushReveal() {
      stopReveal();
      if (revealQueue) {
        inPre.textContent += revealQueue;
        revealQueue = "";
        inPre.scrollTop = inPre.scrollHeight;
      }
    }

    head.addEventListener("click", function () { wrap.classList.toggle("open"); });
    // 展开态下双击主体区域即可折叠（展开仍走头部按钮）
    body.title = "双击折叠";
    body.addEventListener("dblclick", function () { wrap.classList.remove("open"); });
    wrap.appendChild(head);
    wrap.appendChild(body);

    return {
      wrap: wrap,
      setName: function (n) { head.querySelector(".tool-block-name").textContent = n || "tool"; },
      setInput: function (t) {
        // 整体重设（结果返回/历史回放路径）：清空揭示队列直接显示最终文本
        stopReveal();
        revealQueue = "";
        inPre.textContent = t || "{}";
      },
      setOutput: function (t) { outPre.textContent = t || "(无输出)"; },
      // 流式参数生成开始：标记状态并清空输出占位（不改变折叠态）
      beginStream: function () {
        streaming = true;
        wrap.classList.add("is-streaming");
        outPre.textContent = "";
        inPre.textContent = "";
        revealQueue = "";
      },
      // 追加一帧参数增量（模型逐 token 生成的 arguments 文本）
      addInputDelta: function (delta) {
        if (!delta) return;
        if (delta.length <= REVEAL_CHARS_PER_TICK * 2) {
          inPre.textContent += delta;
          inPre.scrollTop = inPre.scrollHeight;
        } else {
          revealQueue += delta;
          startReveal();
        }
      },
      // 整体重设输入文本（对象参数路径），同样走揭示队列保持观感一致
      setInputText: function (t) {
        const text = t || "";
        if (!streaming) {
          inPre.textContent = text;
          return;
        }
        stopReveal();
        revealQueue = text;
        inPre.textContent = "";
        startReveal();
      },
      // 参数已完整、工具开始执行：等待结果阶段。若揭示动画未播完，
      // 立即放行剩余文本（参数实际已完整）
      executing: function () {
        streaming = false;
        flushReveal();
        wrap.classList.remove("is-streaming");
        wrap.classList.add("is-executing");
        outPre.textContent = "执行中…";
      },
      finish: function () {
        streaming = false;
        flushReveal();
        wrap.classList.remove("is-streaming");
        wrap.classList.remove("is-executing");
        head.querySelector(".tool-spin").outerHTML =
          '<svg class="icon" viewBox="0 0 24 24"><path d="M20 6 9 17l-5-5"/></svg>';
        if (!outPre.textContent || outPre.textContent === "执行中…") {
          outPre.textContent = "(无输出)";
        }
      },
    };
  }

  // 代码块复制（事件委托）
  chatInner.addEventListener("click", function (e) {
    const btn = e.target.closest(".copy-btn");
    if (!btn) return;
    const code = btn.closest(".codeblock").querySelector("code").textContent;
    navigator.clipboard.writeText(code).then(function () {
      btn.querySelector(".copy-label").textContent = "已复制";
      setTimeout(function () { btn.querySelector(".copy-label").textContent = "复制"; }, 1500);
    });
  });

  // ---------- 媒体伪标签控件（<image>/<audio>/<video>/<pdf>）交互 ----------
  // 删除：把会话历史 JSONL 中该标签原文替换为"用户已删除/文件不存在"，
  // 之后历史回放与回传给模型的内容都不再引用该文件
  async function deleteMediaWidget(widget) {
    const rawTag = widget.dataset.mediaRaw || "";
    if (!rawTag) {
      App.toast("缺少标签原文，无法从历史中移除");
      return;
    }
    if (!window.confirm("确定删除该媒体引用吗？\n历史记录中将替换为「用户已删除/文件不存在」。")) return;
    try {
      const result = await API.removeMediaTag(App.state.sessionId, rawTag);
      if (result && result.state === "succeed") {
        const placeholder = el("div", "md-media-removed", "用户已删除/文件不存在");
        widget.replaceWith(placeholder);
        App.toast("已从历史记录中移除该媒体引用");
      } else {
        App.toast((result && result.describe) || "移除失败");
      }
    } catch (err) {
      App.toast("移除失败：" + err.message);
    }
  }

  // 事件委托：markdown 渲染会重建节点，点击行为必须挂在容器上
  chatInner.addEventListener("click", function (e) {
    const mediaBtn = e.target.closest("[data-media-action]");
    if (!mediaBtn) return;
    const widget = mediaBtn.closest(".md-media");
    if (!widget) return;
    const action = mediaBtn.dataset.mediaAction;
    if (action === "delete") {
      deleteMediaWidget(widget);
      return;
    }
    if (action === "preview") {
      const kind = widget.dataset.mediaKind || "image";
      const url = widget.dataset.mediaUrl || "";
      const src = widget.dataset.mediaSrc || "";
      if (!url) {
        App.toast("文件不存在或路径无法解析：" + src);
        return;
      }
      App.openMediaPreview({ type: kind, src: url, title: src });
    }
  });

  // 媒体元素加载失败（404/路径失效等）：capture 捕获 media error 事件
  // （error 不冒泡），控件整体标记为不可用并显示占位说明
  document.addEventListener("error", function (e) {
    const target = e.target;
    if (!target || !target.classList || !target.classList.contains("md-media-el")) return;
    const widget = target.closest(".md-media");
    if (widget) widget.classList.add("is-broken");
  }, true);

  // ---------- md 表格：复制文本（TSV+HTML 双格式）与复制为图片 ----------
  function tableToMatrix(table) {
    return Array.from(table.rows).map(function (row) {
      return Array.from(row.cells).map(function (cell) { return cell.textContent.trim(); });
    });
  }

  function tableToTsv(matrix) {
    return matrix.map(function (row) { return row.join("\t"); }).join("\n");
  }

  async function copyTableText(table) {
    const matrix = tableToMatrix(table);
    const tsv = tableToTsv(matrix);
    // 优先双格式（text/html 保留表格结构，粘贴到 Excel/文档即成表格）；降级纯 TSV
    if (navigator.clipboard && window.ClipboardItem) {
      await navigator.clipboard.write([
        new ClipboardItem({
          "text/plain": new Blob([tsv], { type: "text/plain" }),
          "text/html": new Blob(["<table>" + table.innerHTML + "</table>"], { type: "text/html" }),
        }),
      ]);
      return;
    }
    await navigator.clipboard.writeText(tsv);
  }

  // 把表格绘制为 PNG canvas（手工网格绘制，无外部依赖）；超大表返回 null
  function drawTableCanvas(table) {
    const matrix = tableToMatrix(table);
    if (!matrix.length) return null;
    const probe = document.createElement("canvas").getContext("2d");
    const font = "12px " + (getComputedStyle(document.body).fontFamily || "system-ui");
    probe.font = font;
    const padding = 10;
    const rowHeight = 24;
    const maxColWidth = 320;
    const colCount = Math.max.apply(null, matrix.map(function (row) { return row.length; }));
    const colWidths = [];
    for (let c = 0; c < colCount; c++) {
      let width = 0;
      matrix.forEach(function (row) {
        width = Math.max(width, probe.measureText(row[c] || "").width);
      });
      colWidths.push(Math.min(Math.ceil(width) + padding * 2, maxColWidth));
    }
    const width = colWidths.reduce(function (sum, w) { return sum + w; }, 0) + 1;
    const height = rowHeight * matrix.length + 1;
    if (width > 8000 || height > 8000) return null;
    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    const ctx = canvas.getContext("2d");
    // 白底黑字：粘贴到浅色文档/聊天中最通用，不随主题变化
    ctx.fillStyle = "#ffffff";
    ctx.fillRect(0, 0, width, height);
    ctx.font = font;
    ctx.textBaseline = "middle";
    ctx.strokeStyle = "#d0d0d0";
    matrix.forEach(function (row, r) {
      const y = r * rowHeight;
      if (r === 0) {
        ctx.fillStyle = "#f0f0f0";
        ctx.fillRect(0, y, width, rowHeight);
      }
      ctx.fillStyle = r === 0 ? "#111111" : "#222222";
      ctx.font = (r === 0 ? "600 " : "") + font;
      let x = 0;
      for (let c = 0; c < colCount; c++) {
        let text = row[c] || "";
        const maxWidth = colWidths[c] - padding;
        if (ctx.measureText(text).width > maxWidth) {
          while (text.length > 1 && ctx.measureText(text + "…").width > maxWidth) text = text.slice(0, -1);
          text += "…";
        }
        ctx.fillText(text, x + padding, y + rowHeight / 2);
        x += colWidths[c];
      }
      ctx.beginPath();
      ctx.moveTo(0.5, y + 0.5);
      ctx.lineTo(width - 0.5, y + 0.5);
      ctx.stroke();
    });
    let x = 0;
    for (let c = 0; c <= colCount; c++) {
      ctx.beginPath();
      ctx.moveTo(x + 0.5, 0);
      ctx.lineTo(x + 0.5, height - 0.5);
      ctx.stroke();
      x += colWidths[c] || 0;
    }
    ctx.strokeRect(0.5, 0.5, width - 1, height - 1);
    return canvas;
  }

  async function copyTableImage(table) {
    const canvas = drawTableCanvas(table);
    if (!canvas) throw new Error("表格过大，无法生成图片");
    const blob = await new Promise(function (resolve) { canvas.toBlob(resolve, "image/png"); });
    if (navigator.clipboard && window.ClipboardItem) {
      await navigator.clipboard.write([new ClipboardItem({ "image/png": blob })]);
      return "已复制表格图片";
    }
    // 剪贴板写图片不可用（非安全上下文等）：降级为保存图片
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "table.png";
    link.click();
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
    return "已保存 table.png";
  }

  // 表格操作按钮（事件委托：markdown 渲染会重建节点）
  chatInner.addEventListener("click", async function (e) {
    const btn = e.target.closest(".md-table-btn");
    if (!btn || btn.disabled) return;
    const table = btn.closest(".md-table-block")?.querySelector("table");
    if (!table) return;
    const original = btn.textContent;
    btn.disabled = true;
    btn.textContent = "…";
    try {
      const message = btn.dataset.tableAction === "copy-image"
        ? await copyTableImage(table)
        : (await copyTableText(table), "已复制表格");
      btn.textContent = "✓ " + message;
    } catch (err) {
      btn.textContent = "复制失败";
    }
    setTimeout(function () {
      btn.textContent = original;
      btn.disabled = false;
    }, 1500);
  });

  // ---------- 右侧问题导航 ----------
  let qnavUsers = [];

  function rebuildQnav() {
    qnavUsers = Array.from(chatInner.querySelectorAll(".msg-user"));
    qnavRail.innerHTML = "";
    qnavPanel.innerHTML = "";
    if (qnavUsers.length < 2) {
      qnav.classList.add("hidden");
      return;
    }
    qnav.classList.remove("hidden");

    qnavUsers.forEach(function (userEl, i) {
      const bubble = userEl.querySelector(".msg-bubble");
      const text = bubble ? bubble.textContent : "问题 " + (i + 1);

      if (i < QNAV_MAX_DASHES) {
        const dash = el("button", "qnav-dash");
        dash.title = text;
        dash.addEventListener("click", function () { jumpToQuestion(i); });
        qnavRail.appendChild(dash);
      }
      const item = el("button", "qnav-item", i + 1 + ". " + text);
      item.addEventListener("click", function () { jumpToQuestion(i); });
      qnavPanel.appendChild(item);
    });
    updateQnavActive();
  }

  function jumpToQuestion(i) {
    const target = qnavUsers[i];
    if (!target) return;
    // 瞬跳：.chat-scroll 的 CSS 是 scroll-behavior: smooth，
    // 而 scrollTop 赋值/scrollTo(behavior:"auto") 都会继承该属性触发动画，
    // 因此先临时覆盖为 auto，再恢复，保证点击即瞬跳
    const prev = chatScroll.style.scrollBehavior;
    chatScroll.style.scrollBehavior = "auto";
    const delta = target.getBoundingClientRect().top - chatScroll.getBoundingClientRect().top;
    chatScroll.scrollTop += delta;
    chatScroll.style.scrollBehavior = prev;
  }

  function updateQnavActive() {
    if (!qnavUsers.length) return;
    const scrollRect = chatScroll.getBoundingClientRect();
    let current = 0;
    qnavUsers.forEach(function (userEl, i) {
      if (userEl.getBoundingClientRect().top - scrollRect.top < 120) current = i;
    });
    qnavRail.querySelectorAll(".qnav-dash").forEach(function (d, i) {
      d.classList.toggle("active", i === current);
    });
    qnavPanel.querySelectorAll(".qnav-item").forEach(function (d, i) {
      d.classList.toggle("active", i === current);
    });
  }

  // ---------- 滚动：回到底部 + 导航高亮 ----------
  let scrollTick = false;
  chatScroll.addEventListener("scroll", function () {
    const far = chatScroll.scrollHeight - chatScroll.scrollTop - chatScroll.clientHeight > 240;
    scrollBottomBtn.classList.toggle("show", far);
    // 钉住按钮同步更新：若放进 rAF 会晚一帧，滚动时按钮相对页面内容产生滞后抖动
    updateCodeblockCopyButtons();
    if (scrollTick) return;
    scrollTick = true;
    requestAnimationFrame(function () {
      updateQnavActive();
      scrollTick = false;
    });
  });

  // ---------- 代码块复制按钮：滚动钉住 ----------
  // 垂直钉住由原生 position: sticky 完成；正常态按钮在右侧，钉住态移到代码块中线，
  // 以避开 topbar 右上角固定分享按钮。流式渲染后由调用方同步更新，避免异步回调闪切。
  const COPY_PIN_OFFSET = 8;

  function updateCodeblockCopyButtons() {
    const scrollRect = chatScroll.getBoundingClientRect();
    const pinY = scrollRect.top + COPY_PIN_OFFSET;
    chatInner.querySelectorAll(".codeblock .copy-btn").forEach(function (btn) {
      const block = btn.closest(".codeblock");
      const blockRect = block.getBoundingClientRect();
      const btnH = btn.offsetHeight || 28;
      // 代码块在视口内的可见高度（含下边框可见部分）
      const visible = Math.min(blockRect.bottom, scrollRect.bottom) - Math.max(blockRect.top, scrollRect.top);
      if (visible < btnH) {
        btn.classList.add("is-hidden");
        return;
      }
      btn.classList.remove("is-hidden");

      // 直接依据代码块顶部判断，避免读取 sticky 按钮自身位置造成临界态误判。
      const pinned = blockRect.top < pinY;
      const wasPinned = btn.classList.contains("is-pinned");
      if (pinned) {
        // 正常态右边距跟随窄屏规则变化，按实际 margin 计算居中位移；
        // 窗口缩放导致按钮宽度变化时，仅在位移真的变化时写入，避免滚动中重复触发布局。
        const rightGap = parseFloat(getComputedStyle(btn).marginRight) || 12;
        const shift = btn.offsetWidth / 2 + rightGap - blockRect.width / 2;
        const currentShift = parseFloat(btn.style.getPropertyValue("--pin-shift"));
        if (!wasPinned || !Number.isFinite(currentShift) || Math.abs(currentShift - shift) > 0.5) {
          btn.style.setProperty("--pin-shift", shift + "px");
        }
        btn.classList.add("is-pinned");
      } else {
        btn.classList.remove("is-pinned");
        btn.style.removeProperty("--pin-shift");
      }
    });
  }

  scrollBottomBtn.addEventListener("click", scrollToBottom);


  // ---------- 导出（供其它模块经 App.* 调用） ----------
  App.renderRecords = renderRecords;
  App.appendTime = appendTime;
  App.buildCompactionBlock = buildCompactionBlock;
  App.highlightCodeBlocks = highlightCodeBlocks;
  App.appendUserMessage = appendUserMessage;
  App.buildThinkBlock = buildThinkBlock;
  App.buildToolBlock = buildToolBlock;
  App.rebuildQnav = rebuildQnav;
  App.updateCodeblockCopyButtons = updateCodeblockCopyButtons;
})(window.App);
