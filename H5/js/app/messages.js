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
    el, scrollToBottom, stickToBottom, QNAV_MAX_DASHES, chatScroll,
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
    // 流式注入（attachment 指向流节点）时尊重自动贴底暂停：用户上滚阅读
    // 时不被拉回；整段历史回放后由 history.js 的 scrollToBottom 置底复位
    stickToBottom();
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

  // ---------- SVG 生成控件（```svg 双视图块）交互 ----------
  // 视图切换/复制代码/复制图片全部事件委托：markdown 流式重渲染会重建节点
  chatInner.addEventListener("click", async function (e) {
    const btn = e.target.closest("[data-svg-action]");
    if (!btn) return;
    const block = btn.closest(".md-svg-block");
    if (!block) return;
    const action = btn.dataset.svgAction;
    if (action === "view-image" || action === "view-code") {
      const want = action === "view-image" ? "image" : "code";
      block.querySelectorAll("[data-svg-view]").forEach(function (view) {
        view.hidden = view.dataset.svgView !== want;
      });
      block.querySelectorAll(".md-svg-tab").forEach(function (tab) {
        tab.classList.toggle("is-active", tab === btn);
      });
      return;
    }
    const original = btn.textContent;
    btn.disabled = true;
    if (action === "copy-code") {
      try {
        const code = Markdown.unescapeHtml(block.dataset.svgCode || "");
        await navigator.clipboard.writeText(code);
        btn.textContent = "✓ 已复制";
      } catch (_) {
        btn.textContent = "复制失败";
      }
    } else if (action === "copy-image") {
      try {
        const blob = await svgBlockToPngBlob(block);
        if (navigator.clipboard && window.ClipboardItem) {
          await navigator.clipboard.write([new ClipboardItem({ "image/png": blob })]);
          btn.textContent = "✓ 已复制图片";
        } else {
          // 剪贴板写图片不可用（非安全上下文等）：降级为下载 PNG
          const url = URL.createObjectURL(blob);
          const link = document.createElement("a");
          link.href = url;
          link.download = "svg-image.png";
          link.click();
          setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
          btn.textContent = "✓ 已下载 PNG";
        }
      } catch (_) {
        btn.textContent = "复制失败";
      }
    } else {
      btn.disabled = false;
      return;
    }
    setTimeout(function () {
      btn.textContent = original;
      btn.disabled = false;
    }, 1500);
  });

  // 把 SVG 双视图块的图片视图光栅化为 PNG：
  // 内联 SVG → Blob(data:image/svg+xml) → Image 解码 → canvas 绘制 → PNG Blob。
  // 尺寸优先取实际渲染的显示尺寸（getBoundingClientRect）——mermaid 产物的
  // svg 根是 width="100%" 且 height 已被移除，直接读属性会得到 100×viewBox
  // 的崩坏比例；不可见（代码视图激活）时回退 viewBox 基准尺寸。
  // 按 2x~3x（跟随 devicePixelRatio）超采样导出，粘贴出去不发虚。
  async function svgBlockToPngBlob(block) {
    const view = block.querySelector('[data-svg-view="image"] svg');
    if (!view) throw new Error("svg not found");
    // 基准尺寸（CSS 像素）：元素盒在 max-height 触顶时会水平 letterbox
    // （内容按 preserveAspectRatio 居中缩小、两侧留透明），直接用元素盒
    // 导出会把留白框进 PNG（表现为白色外框）——按 viewBox 比例从元素盒
    // 中「contain 裁剪」出实际绘制内容的尺寸
    let baseW = 0, baseH = 0;
    const visible = !!(view.getClientRects && view.getClientRects().length);
    const rect = visible ? view.getBoundingClientRect() : null;
    const vb = (view.getAttribute("viewBox") || "").split(/[\s,]+/).map(parseFloat);
    const vbOk = vb.length === 4 && isFinite(vb[2]) && isFinite(vb[3]) && vb[2] > 0 && vb[3] > 0;
    if (rect && rect.width > 2 && rect.height > 2) {
      if (vbOk) {
        const ar = vb[2] / vb[3];
        baseW = Math.min(rect.width, rect.height * ar);
        baseH = Math.min(rect.height, rect.width / ar);
      } else {
        baseW = rect.width;
        baseH = rect.height;
      }
    }
    if (!(baseW > 2 && baseH > 2)) {
      if (vbOk) {
        baseW = vb[2];
        baseH = vb[3];
      } else {
        baseW = 640;
        baseH = 480;
      }
    }
    baseW = Math.max(2, Math.round(baseW));
    baseH = Math.max(2, Math.round(baseH));
    // 高清倍率：2x 起、跟随 devicePixelRatio、上限 3x；长边 6000px 兜底防爆内存
    let scale = Math.min(Math.max(2, window.devicePixelRatio || 1), 3);
    let w = Math.round(baseW * scale);
    let h = Math.round(baseH * scale);
    const MAX_EDGE = 6000;
    if (Math.max(w, h) > MAX_EDGE) {
      const k = MAX_EDGE / Math.max(w, h);
      w = Math.round(w * k);
      h = Math.round(h * k);
      scale = w / baseW;
    }
    const clone = view.cloneNode(true);
    // 去内联样式（mermaid 的 max-width / 自适应 width:100% 在独立解码语境
    // 无意义且会干扰固有尺寸），改用显式像素尺寸 + viewBox 保证比例
    clone.removeAttribute("style");
    clone.setAttribute("xmlns", "http://www.w3.org/2000/svg");
    clone.setAttribute("xmlns:xlink", "http://www.w3.org/1999/xlink");
    clone.setAttribute("width", baseW);
    clone.setAttribute("height", baseH);
    if (!vbOk) clone.setAttribute("viewBox", "0 0 " + baseW + " " + baseH);
    const blobSrc = new Blob([new XMLSerializer().serializeToString(clone)], { type: "image/svg+xml;charset=utf-8" });
    const url = URL.createObjectURL(blobSrc);
    try {
      const img = await new Promise(function (resolve, reject) {
        const image = new Image();
        image.onload = function () { resolve(image); };
        image.onerror = function () { reject(new Error("SVG 解码失败")); };
        image.src = url;
      });
      const canvas = document.createElement("canvas");
      canvas.width = w;
      canvas.height = h;
      canvas.getContext("2d").drawImage(img, 0, 0, w, h);
      return await new Promise(function (resolve, reject) {
        canvas.toBlob(function (b) {
          b ? resolve(b) : reject(new Error("PNG 导出失败"));
        }, "image/png");
      });
    } finally {
      setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
    }
  }

  // ---------- Canvas 沙箱运行时（```canvas 程序块） ----------
  // 每块独立 <iframe sandbox="allow-scripts">（不给 allow-same-origin →
  // opaque origin：父页读不到它的 contentDocument，它也访问不到页面
  // DOM/cookie/存储/同源接口）。通信全走 postMessage：
  //   1) iframe BOOT 完成 → 回发 canvas-ready；
  //   2) 父页按 ev.source 匹配 frame，下发 canvas-run{id,code}；
  //   3) iframe 内 new Function('stage','console',code) 执行，console 收集，
  //      结束时 canvas-done/canvas-error{logs, shot} 一次性回传（shot 为
  //      沙箱内 stage.toDataURL 的自报快照——跨源读不到画布像素，截图
  //      必须由 iframe 自己导出）。
  // 用户代码不经 srcdoc 注入，避免 HTML 解析转义风险；流式重建的旧
  // iframe 随节点一起被丢弃，无需显式回收。

  const CANVAS_RUNTIME_CSS =
    "html,body{margin:0;padding:0;background:transparent;overflow:hidden;}" +
    "#stage{display:block;width:100%;height:100%;}";

  // 沙箱内宿主脚本：监听父页指令，执行用户脚本并回传日志/异常/快照
  const CANVAS_BOOT_JS = [
    "var stage=document.getElementById('stage');",
    "function shot(){",
    "  try{return stage.toDataURL('image/png');}catch(e){",
    "    return 'ERR:'+String(e&&e.message||e);",
    "  }",
    "}",
    "function fmt(v){",
    "  try{return typeof v==='string'?v:JSON.stringify(v);}catch(_){return String(v);}",
    "}",
    "function join(args){return Array.prototype.map.call(args,fmt).join(' ');}",
    "window.addEventListener('message',function(ev){",
    "  var d=ev.data||{};",
    "  if(d.type!=='canvas-run')return;",
    "  var logs=[];",
    "  var sc={log:function(){logs.push(join(arguments));},",
    "    info:function(){logs.push(join(arguments));},",
    "    warn:function(){logs.push('[warn] '+join(arguments));},",
    "    error:function(){logs.push('[error] '+join(arguments));}};",
    "  try{",
    "    new Function('stage','console',d.code)(stage,sc);",
    "    parent.postMessage({type:'canvas-done',id:d.id,logs:logs,shot:shot()},'*');",
    "  }catch(err){",
    "    logs.push('[异常] '+String(err&&err.message||err));",
    "    parent.postMessage({type:'canvas-error',id:d.id,logs:logs,shot:shot(),error:String(err&&err.message||err)},'*');",
    "  }",
    "});",
    "parent.postMessage({type:'canvas-ready'},'*');",
  ].join("\n");

  // 生成沙箱 iframe：srcdoc 为受控运行时模板（仅画布 + BOOT，代码走消息）
  function buildCanvasFrame() {
    const frame = document.createElement("iframe");
    frame.className = "md-canvas-frame";
    frame.setAttribute("sandbox", "allow-scripts");
    frame.setAttribute("title", "Canvas 沙箱");
    frame.srcdoc =
      "<!doctype html><html><head><meta charset=\"utf-8\"><style>" +
      CANVAS_RUNTIME_CSS + "</style></head><body>" +
      "<canvas id=\"stage\" width=\"720\" height=\"420\"></canvas>" +
      "<script>" + CANVAS_BOOT_JS + "<\/script></body></html>";
    return frame;
  }

  // 画布快照缓存：canvasId -> {dataUrl, error}（canvas-done/error 时写入）
  const canvasShots = new Map();

  // 沙箱消息总线：ready → 下发该块源码；done/error → 状态复位 + 日志渲染 + 快照缓存
  function onCanvasMessage(ev) {
    const data = ev.data || {};
    if (data.type === "canvas-ready") {
      // 按 source 匹配发起方 iframe，取其所在块的源码下发
      const frames = chatInner.querySelectorAll(".md-canvas-frame");
      for (let i = 0; i < frames.length; i++) {
        if (frames[i].contentWindow === ev.source) {
          const block = frames[i].closest(".md-canvas-block");
          if (block && frames[i].contentWindow) {
            frames[i].contentWindow.postMessage({
              type: "canvas-run",
              id: block.dataset.canvasId || "",
              code: Markdown.unescapeHtml(block.dataset.svgCode || ""),
            }, "*");
          }
          return;
        }
      }
      return;
    }
    if (data.type === "canvas-done" || data.type === "canvas-error") {
      const block = chatInner.querySelector(
        '.md-canvas-block[data-canvas-id="' + data.id + '"]');
      if (!block) return;
      const shotData = String(data.shot || "");
      canvasShots.set(data.id || "", {
        dataUrl: shotData.indexOf("data:image/png") === 0 ? shotData : "",
        error: shotData.indexOf("data:image/png") === 0 ? "" :
          (shotData ? shotData.replace(/^ERR:/, "画布被浏览器标记污染，无法导出：") : ""),
      });
      renderCanvasLog(block, data.logs || []);
      // 同步脚本瞬间完成：状态复位，按钮回到「▶ 运行」（再点即重跑）
      setCanvasState(block, "idle");
    }
  }
  window.addEventListener("message", onCanvasMessage);

  // 日志区：画布下方等宽文本框（textContent 注入，防日志内容注入 HTML）
  function renderCanvasLog(block, logs) {
    const view = block.querySelector(".md-canvas-view");
    if (!view) return;
    let logBox = view.querySelector(".md-canvas-log");
    if (!logBox) {
      logBox = document.createElement("div");
      logBox.className = "md-canvas-log";
      view.appendChild(logBox);
    }
    logBox.textContent = logs.length ? logs.join("\n") : "(无输出)";
    logBox.scrollTop = logBox.scrollHeight;
  }

  // 运行状态辅助：更新 data-canvas-state 并同步「运行/停止」按钮文案
  function setCanvasState(block, stateName) {
    block.dataset.canvasState = stateName;
    const runBtn = block.querySelector("[data-canvas-action='run']");
    if (!runBtn) return;
    if (stateName === "running") {
      runBtn.textContent = "■ 停止";
      runBtn.classList.add("is-running");
    } else {
      runBtn.textContent = "▶ 运行";
      runBtn.classList.remove("is-running");
    }
  }

  // Canvas 控件交互（事件委托，与 SVG 控件同容器挂载）
  chatInner.addEventListener("click", async function (e) {
    const btn = e.target.closest("[data-canvas-action]");
    if (!btn) return;
    const block = btn.closest(".md-canvas-block");
    if (!block) return;
    const action = btn.dataset.canvasAction;

    // 「运行/停止」同一颗按钮：未运行 → 确认后在沙箱执行；运行中 → 停止。
    // 实际执行由沙箱 ready 握手触发（onCanvasMessage 收到 canvas-ready 后
    // 下发源码），这里只负责确认 + 建 iframe + 置运行态。
    if (action === "run") {
      if (block.dataset.canvasState === "running") {
        setCanvasState(block, "idle");
        const frame = block.querySelector(".md-canvas-frame");
        if (frame) frame.remove();
        const view = block.querySelector(".md-canvas-view");
        if (view) {
          view.innerHTML = '<div class="md-canvas-placeholder">已停止 · 点击「▶ 运行」重新执行</div>';
        }
        return;
      }
      // 运行确认：沙箱只禁 DOM 访问，脚本体本身无法预检
      if (!window.confirm("在该沙箱中运行此 Canvas 脚本？\n（脚本在隔离 iframe 中执行，无法访问本页数据，但请注意其绘制/计算内容）")) {
        return;
      }
      const view = block.querySelector(".md-canvas-view");
      if (!view) return;
      view.innerHTML = "";
      view.appendChild(buildCanvasFrame());
      setCanvasState(block, "running");
      return;
    }

    if (action === "shot") {
      // 截图导出：使用沙箱执行完成时自报的快照（canvasShots 缓存）——
      // 沙箱是 opaque origin，父页读不到 iframe 内画布像素，截图只能由
      // iframe 内 stage.toDataURL 自报；未运行/被污染时给出明确提示
      const id = block.dataset.canvasId || "";
      const rec = canvasShots.get(id);
      if (!rec || (!rec.dataUrl && !rec.error)) {
        App.toast("尚未运行，先点「▶ 运行」");
        return;
      }
      if (rec.error) { App.toast(rec.error); return; }
      try {
        const blob = await (await fetch(rec.dataUrl)).blob();
        if (navigator.clipboard && window.ClipboardItem) {
          await navigator.clipboard.write([new ClipboardItem({ "image/png": blob })]);
          App.toast("已复制画布截图");
        } else {
          const url = URL.createObjectURL(blob);
          const link = document.createElement("a");
          link.href = url;
          link.download = "canvas-shot.png";
          link.click();
          setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
          App.toast("已下载画布截图 PNG");
        }
      } catch (err) {
        App.toast("截图失败：" + (err && err.message || err));
      }
      return;
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

  // ---------- Mermaid 异步渲染接线 ----------
  // ```mermaid 控件在 Markdown.render()（同步纯函数）里只产出「渲染中」占位，
  // mermaid.render 是异步 API 且库 5.5MB 需懒加载——这里在节点挂载后异步回填：
  // MutationObserver 捕获流式整块重建后的新控件，按 data-mermaid-id 去重渲染。
  // pending 集合做并发合并：同一控件流式期间重复渲染只保留最后一次。
  const mermaidPending = new Map(); // id → Promise

  function scheduleMermaidRender(widget) {
    const id = widget.dataset.mermaidId;
    const state = widget.dataset.mermaidState;
    if (!id || state !== "pending" || mermaidPending.has(id)) return;
    const holder = widget.querySelector('[data-mermaid-holder="' + id + '"]');
    if (!holder) return;
    const code = Markdown.unescapeHtml(widget.dataset.svgCode || "");
    if (!code.trim()) return;
    widget.dataset.mermaidState = "rendering";
    const p = Markdown.renderMermaidInto(id, code, holder)
      .then(function () {
        widget.dataset.mermaidState = "done";
        mermaidPending.delete(id);
      })
      .catch(function () {
        widget.dataset.mermaidState = "failed";
        mermaidPending.delete(id);
      });
    mermaidPending.set(id, p);
  }

  function mountMermaidBlocks(root) {
    if (!root) return;
    if (root.nodeType === 1 && root.matches && root.matches(".md-mermaid-block")) {
      scheduleMermaidRender(root);
      return;
    }
    const nodes = root.querySelectorAll ? root.querySelectorAll(".md-mermaid-block") : [];
    for (let i = 0; i < nodes.length; i++) scheduleMermaidRender(nodes[i]);
  }

  // 挂载触发：markdown 重渲染是整体替换 innerHTML/appendChild 混用，
  // MutationObserver 统一捕获（配置 childList+subtree 足够，无需 attributes）
  if (typeof MutationObserver !== "undefined") {
    new MutationObserver(function (mutations) {
      for (let i = 0; i < mutations.length; i++) {
        const added = mutations[i].addedNodes;
        for (let k = 0; k < added.length; k++) {
          if (added[k].nodeType === 1) mountMermaidBlocks(added[k]);
        }
      }
    }).observe(chatInner, { childList: true, subtree: true });
  }

  // 流结束兜底：finish() 里清掉仍处于 rendering/pending 的控件状态残留
  //（渲染失败已在 catch 内落态；这里只把孤儿的"渲染中"文案转错误说明）
  function finalizeMermaidBlocks() {
    chatInner.querySelectorAll('.md-mermaid-block[data-mermaid-state="rendering"]').forEach(function (widget) {
      widget.dataset.mermaidState = "failed";
      const holder = widget.querySelector("[data-mermaid-holder]");
      if (holder && !holder.querySelector("svg")) {
        holder.classList.add("md-mermaid-error");
        holder.textContent = "Mermaid 渲染未完成（流式被中断），可展开代码视图查看源码";
      }
    });
  }

  // ---------- md 表格：复制（原始 Markdown）与复制为图片 / 下载 Excel ----------
  // 矩阵转换与 canvas 网格绘制抽到 js/table_canvas.js（TableCanvas 全局，纯函数可单测）
  function tableToMatrix(table) {
    return TableCanvas.tableToMatrix(table);
  }

  // 复制原始 markdown 表格文本（渲染时保存在 data-table-raw，含 \\| 转义与行内代码原貌）
  async function copyTableMarkdown(rawMarkdown) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(rawMarkdown);
      return;
    }
    // 兜底：隐藏 textarea + execCommand（非安全上下文等场景）
    const helper = document.createElement("textarea");
    helper.value = rawMarkdown;
    helper.setAttribute("readonly", "");
    helper.style.position = "fixed";
    helper.style.opacity = "0";
    document.body.appendChild(helper);
    helper.select();
    try { document.execCommand("copy"); } finally { helper.remove(); }
  }

  // 下载表格为 Excel（后端解析原始 md 表格并生成 xlsx，见 /export/table/xlsx）
  async function downloadTableXlsx(rawMarkdown) {
    const res = await fetch(API.BASE + "/export/table/xlsx", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ markdown: rawMarkdown }),
    });
    if (!res.ok) {
      let detail = res.status + " " + res.statusText;
      try {
        const body = await res.json();
        detail = body.detail || body.message || detail;
      } catch (_) { /* 非 JSON 响应 */ }
      throw new Error(detail);
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "table.xlsx";
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
  }

  // 把表格绘制为 PNG canvas：实现抽到 js/table_canvas.js（TableCanvas 全局）。
  // 列宽两轮收敛 + 行高自适应，长文本逐字换行完整可见，不再截断加省略号；
  // 换行/布局纯函数在 Node 单测覆盖（test_h5/table_export.test.js）。
  function drawTableCanvas(table) {
    return TableCanvas.drawTableCanvas(table);
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

  // 关闭所有表格「更多」菜单
  function closeAllTableMenus(exceptWrap) {
    document.querySelectorAll(".md-table-menu").forEach(function (menu) {
      if (menu !== exceptWrap) menu.classList.add("hidden");
    });
    document.querySelectorAll(".md-table-more-btn").forEach(function (btn) {
      if (btn.closest(".md-table-actions") === exceptWrap?.closest(".md-table-actions")) return;
      btn.setAttribute("aria-expanded", "false");
    });
  }

  // 表格操作按钮（事件委托：markdown 渲染会重建节点）
  chatInner.addEventListener("click", async function (e) {
    const btn = e.target.closest(".md-table-btn");
    if (btn && btn.dataset.tableAction === "more") {
      // 「更多」菜单展开/收起；同时互斥关闭其它表格的菜单
      const menu = btn.parentElement.querySelector(".md-table-menu");
      const willOpen = menu.classList.contains("hidden");
      closeAllTableMenus();
      if (willOpen) {
        menu.classList.remove("hidden");
        btn.setAttribute("aria-expanded", "true");
      } else {
        btn.setAttribute("aria-expanded", "false");
      }
      return;
    }
    // 点击菜单外的其它区域时收起全部菜单
    closeAllTableMenus();
    let target = btn;
    if (!target) {
      const menuBtn = e.target.closest('[role="menuitem"]');
      if (!menuBtn) return;
      target = menuBtn;
      // 点击菜单项后收起本表格菜单
      target.closest(".md-table-more")?.querySelector(".md-table-menu")?.classList.add("hidden");
      target.closest(".md-table-more")?.querySelector(".md-table-more-btn")?.setAttribute("aria-expanded", "false");
    }
    if (target.disabled) return;
    const block = target.closest(".md-table-block");
    if (!block) return;
    const table = block.querySelector(".md-table-scroll table");
    const rawAttr = target.getAttribute("data-table-raw") || "";
    const rawMarkdown = Markdown.unescapeHtml(rawAttr);
    if (!table && !rawMarkdown) return;
    const action = target.dataset.tableAction;
    const original = target.textContent;
    target.disabled = true;
    target.textContent = "…";
    try {
      let message = "";
      if (action === "copy-md") {
        await copyTableMarkdown(rawMarkdown);
        message = "已复制 Markdown";
      } else if (action === "download-xlsx") {
        await downloadTableXlsx(rawMarkdown);
        message = "已下载 table.xlsx";
      } else if (action === "copy-image") {
        message = await copyTableImage(table);
      } else {
        const tsv = tableToMatrix(table).map(function (row) { return row.join("\t"); }).join("\n");
        // 兜底「复制」：优先纯文本 TSV + HTML 双格式（文档/Excel 粘贴仍保留表格）
        if (navigator.clipboard && window.ClipboardItem) {
          await navigator.clipboard.write([
            new ClipboardItem({
              "text/plain": new Blob([tsv], { type: "text/plain" }),
              "text/html": new Blob(["<table>" + table.innerHTML + "</table>"], { type: "text/html" }),
            }),
          ]);
        } else {
          await navigator.clipboard.writeText(tsv);
        }
        message = "已复制表格";
      }
      target.textContent = "✓ " + message;
    } catch (err) {
      target.textContent = action === "download-xlsx" ? "下载失败" : "复制失败";
    }
    setTimeout(function () {
      target.textContent = original;
      target.disabled = false;
    }, 1500);
  });

  // 点击表格区外时收起「更多」菜单（捕获阶段，避免先命中其它交互）
  document.addEventListener("click", function (e) {
    if (e.target.closest(".md-table-more")) return;
    closeAllTableMenus();
  }, true);
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") closeAllTableMenus();
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
  App.finalizeMermaidBlocks = finalizeMermaidBlocks;
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
