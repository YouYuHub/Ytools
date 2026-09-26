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
    el, state, toast, scrollToBottom, stickToBottom, QNAV_MAX_DASHES, chatScroll,
    chatInner, qnav, qnavRail, qnavPanel,
    scrollBottomBtn
  } = App;

  // ---------- 分段渲染 ----------
  // 初始渲染只画末尾 RENDER_TAIL_RECORDS 条记录（最近对话），顶部留一张
  // 「加载更早消息」细条，向上滚动到底自动追加更早的分段——长会话打开时
  // 只构建可视尾部，主线程阻塞时长与记录总数解耦。分段边界不允许落在
  // 「欠账记录」上（见 isDependentRecord），保证工具调用与结果、压缩
  // start/done 的配对不被切开；record.round 非空时新节点统一打 data-round。
  const RENDER_TAIL_RECORDS = 240;
  const RENDER_OLDER_BATCH = 160;
  // 后台渐进回填的批间空闲间隔（ms）：批间让出主线程，浏览器的输入响应、
  // 流式渲染、动画不受影响；期间用户上滚也不会看到空白（等 800ms 就有）
  const BACKFILL_IDLE_DELAY = 120;
  // loadOlder: { records, start, loading, timer } —— 待分段渲染的完整记录
  // 表/下一个待渲染下标/批量渲染并发锁/后台回填调度句柄（细条由
  // flushRenderedTail 创建插入；初始显示末尾后由后台渐进补全剩余记录）
  const loadOlderState = { records: null, start: 0, loading: false, timer: null, button: null };

  // 欠账记录：渲染它需要其配对记录位于同段更早位置——toolResult 依赖同名
  // tool 调用；压缩 done/aborted 依赖 start（interrupted 中断态独立展示，
  // 不进配对）。段边界切开它们会产生「有结果无调用」的孤儿块
  function isDependentRecord(rec) {
    if (!rec) return false;
    if (rec.kind === "toolResult") return true;
    return rec.kind === "compaction" && !rec.interrupted && rec.phase !== "start";
  }

  // 段尾安全消费数量：从 start 起最多 budget 条，段尾若恰好压在欠账记录上
  // 则后移（把欠账记录连着前面的配对拉进本段）。返回本段消费的记录数
  // （注意：此前版本把绝对下标当数量返回，初始切割 start 被算成 0，
  // 表现为打开长会话只显示开头一段、后续内容无加载路径——已修复）
  function pendingQueueSafeEnd(records, start, budget) {
    let end = Math.min(records.length, start + budget);
    while (end < records.length && isDependentRecord(records[end])) end += 1;
    return end - start;
  }

  // 段头安全下标：段头若落在欠账记录上则前移（把欠账记录及其配对调用
  // 拉进本段）。跨段边界的尾部安全由已渲染段的头边界规则保证
  function pendingQueueSafeStart(records, start) {
    while (start > 0 && isDependentRecord(records[start])) start -= 1;
    return start;
  }

  // 本段渲染后通用收尾：未被结果配对的工具块（分段尾部/历史中断遗留）统一
  // finish 置为完成态；初始渲染把末尾分段固定在屏幕底部（flushRenderedTail
  // 内部处理），并对分段尾与新段头部共同组成的边界闭环 finish
  function finishUnmatchedTools(pendingTools) {
    Object.keys(pendingTools).forEach(function (name) {
      pendingTools[name].forEach(function (ui) { ui.finish(); });
    });
  }

  // 渲染历史记录（工具调用与结果按名称顺序配对）
  // 每条 record 渲染产生的新节点统一打 data-round（1-based 轮次号，
  // 与后端 delete_rounds 的 start_round 同口径），供用户消息编辑/删除定位

  // 单条记录渲染（容器参数化，主渲染与「加载更早消息」分段共用同一实现，
  // 避免双份渲染逻辑漂移）。container 为 chatInner 时行为与历史版本一致；
  // 配对队列 pendingTools/pendingCompactions 由调用方提供（主渲染与分段
  // 回灌各自持有一套，互不串扰）
  function renderOneRecordInto(container, rec, pendingTools, pendingCompactions) {
    if (rec.kind === "user") {
      appendUserMessage(rec.content, rec.ts, null, null, container, rec.round, rec.quotes,
        rec.source_event_index);
      return;
    }
    if (rec.kind === "think") {
      appendTimeInto(container, rec.ts);
      const think = buildThinkBlock();
      think.setText(rec.content);
      container.appendChild(think.wrap);
      return;
    }
    if (rec.kind === "assistant") {
      appendTimeInto(container, rec.ts);
      const body = el("div", "msg-assistant-body");
      if (Number.isInteger(rec.source_event_index) && rec.source_event_index >= 0) {
        body.dataset.sourceEvent = String(rec.source_event_index);
      }
      body.innerHTML = Markdown.render(rec.content);
      highlightCodeBlocks(body);
      container.appendChild(body);
      return;
    }
    if (rec.kind === "tool") {
      appendTimeInto(container, rec.ts);
      const ui = buildToolBlock(rec.name);
      ui.setInput(FormatUtils.prettyJson(rec.args));
      container.appendChild(ui.wrap);
      // ask_user 历史记录附一张可点击的提问卡片：刷新后仍能重新打开回答窗口
      if (rec.name === App.ASK_USER_TOOL_NAME) {
        const askQuestions = App.parseAskQuestionsFromArgs(rec.args);
        if (askQuestions) container.appendChild(App.buildAskBlock(askQuestions));
      }
      (pendingTools[rec.name] = pendingTools[rec.name] || []).push(ui);
      return;
    }
    if (rec.kind === "toolResult") {
      const queue = pendingTools[rec.name];
      let ui = queue && queue.shift();
      if (!ui) {
        appendTimeInto(container, rec.ts);
        ui = buildToolBlock(rec.name);
        ui.setInput(FormatUtils.prettyJson(rec.args));
        container.appendChild(ui.wrap);
      }
      applyToolResult(ui, rec.name, rec.result, rec.file_diff, rec.args);
      ui.finish();
      return;
    }
    if (rec.kind === "usage") {
      container.appendChild(el("div", "round-usage", FormatUtils.usageText(rec.usage)));
      return;
    }
    if (rec.kind === "agentBlock") {
      // 子任务块：任务/计划在头，轨迹按轮次，尾部最终回复引用条
      appendTimeInto(container, rec.started_at);
      const agentUi = buildSubAgentBlock();
      agentUi.applyEvent({ phase: "start", task: rec.task, todo: rec.todo, rounds_limit: rec.rounds_limit });
      agentUi.hydrate(rec);
      agentUi.markReturned(rec);
      container.appendChild(agentUi.wrap);
      return;
    }
    if (rec.kind === "notice") {
      container.appendChild(el("div", "notice-bar", rec.content));
      return;
    }
    if (rec.kind === "compaction") {
      // 任务中断遗留的未完成压缩（有 start、无 done）：置为中断态展示，
      // 未覆盖的轮次由后端在下次请求按需重新压缩
      if (rec.interrupted) {
        appendTimeInto(container, rec.ts);
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
        container.appendChild(interruptedBar);
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
        target_tokens: rec.target_tokens,
        budget_scope: rec.budget_scope,
        token_limit: rec.token_limit,
        trigger_reason: rec.trigger_reason,
        trigger_context_tokens: rec.trigger_context_tokens,
        trigger_threshold: rec.trigger_threshold,
        batch_source_tokens: rec.batch_source_tokens,
        source_budget: rec.source_budget,
        tail_rounds: rec.tail_rounds,
        output_token_limit: rec.output_token_limit,
        source_was_truncated: rec.source_was_truncated,
        source_was_chunked: rec.source_was_chunked,
        source_chunk_count: rec.source_chunk_count,
        warnings: rec.warnings,
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
        appendTimeInto(container, rec.ts);
        if (pendingQueue && pendingQueue.length) {
          pendingQueue.shift().update({ phase: "aborted", error: compactionPayload.error });
          pendingCompactions[scope] = [];
        } else {
          const failedUi = buildCompactionBlock(compactionPayload);
          container.appendChild(failedUi.wrap);
        }
        return;
      }
      if (compactionPayload.phase === "done" && !isMergeEvent && pendingQueue && pendingQueue.length) {
        pendingQueue.shift().update(compactionPayload);
        if (!pendingQueue.length) delete pendingCompactions[scope];
        return;
      }
      appendTimeInto(container, rec.ts);
      const compactionUi = buildCompactionBlock(compactionPayload);
      container.appendChild(compactionUi.wrap);
      if (compactionPayload.phase === "start") {
        (pendingCompactions[scope] = pendingCompactions[scope] || []).push(compactionUi);
      }
      return;
    }
  }

  function renderRecords(records, replayState) {
    // 渲染上下文（分段渲染状态）：replayState 为 null/缺省表示流式收尾、
    // 编辑重发等对已打开会话的增量回放（记录数少、直接全量渲染，不贴底）；
    // 初始全量渲染走分段并承担贴底/加载更早消息职责
    const ctx = replayState === null ? { initial: false } : { initial: true };
    if (replayState) Object.assign(ctx, replayState);
    const pendingTools = {}; // name -> [tool ui]
    const pendingCompactions = {}; // scope -> [尚未完成的压缩 ui]
    function renderOneRecord(rec) {
      renderOneRecordInto(chatInner, rec, pendingTools, pendingCompactions);
    }

    // 初始全量渲染走分段：只画末尾一段，之前的记录留给「加载更早消息」；
    // 增量渲染（replayState === null）记录数一般很小，直接全量且不贴底。
    const isInitialRender = ctx.initial;
    let slice = records;
    if (isInitialRender && records.length > RENDER_TAIL_RECORDS) {
      const tailCount = pendingQueueSafeEnd(records, records.length - RENDER_TAIL_RECORDS, RENDER_TAIL_RECORDS);
      const start = pendingQueueSafeStart(records, records.length - tailCount);
      ctx.loadOlder = {
        records: records,
        start: start,
      };
      slice = records.slice(start);
      loadOlderState.records = records;
      loadOlderState.start = start;
    } else {
      // 增量回放 / 记录数少的初始渲染：清掉上一会话的分段状态（进行中的
      // 后台回填循环据此在下一次调度校验时发现 records 已更换并停止）
      loadOlderState.records = null;
      loadOlderState.start = 0;
    }

    let rendered = 0;
    const doRenderBatch = function (batchSize) {
      const count = rendered >= slice.length ? 0 : pendingQueueSafeEnd(slice, rendered, batchSize);
      for (let i = 0; i < count; i += 1) {
        const rec = slice[rendered + i];
        const before = chatInner.childNodes.length;
        renderOneRecord(rec);
        if (rec.round == null) continue;
        for (let k = before; k < chatInner.childNodes.length; k += 1) {
          const node = chatInner.childNodes[k];
          if (node.nodeType === 1) node.setAttribute("data-round", String(rec.round));
        }
      }
      rendered += count;
      return count;
    };

    // 段头可能为配对工具结果而向前扩展；选出的尾段必须全部渲染，
    // 否则多出的记录会挤掉末尾的助手回复，刷新后看起来像历史未落盘。
    doRenderBatch(slice.length);

    // 没有等到结果的工具（历史中断）也标记结束
    finishUnmatchedTools(pendingTools);
    // 历史渲染完成后刷新提问卡片可答性（带后续对话的旧提问不可再答）
    App.refreshAskBlockStates();
    if (isInitialRender) {
      flushRenderedTail(ctx);
    }
  }

  // 「加载更早消息」细条（renderRecords 分段时插在聊天流顶部，styles 见
  // _chat.scss .qnav-load-older）：初始显示末尾对话后，**后台渐进回填**会
  // 自动把更早记录全部补齐（批间让出主线程保持流畅）；点击/向上滚动触及
  // 细条可立即优先触发下一批，无需等待后台节奏
  function createLoadOlderButton() {
    const button = el("button", "qnav-load-older");
    button.type = "button";
    button.title = "加载更早的消息";
    button.setAttribute("aria-label", "加载更早的消息");
    button.addEventListener("click", function () {
      scheduleBackfill(0);
    });
    if (typeof IntersectionObserver !== "undefined") {
      const observer = new IntersectionObserver(function (entries) {
        entries.forEach(function (entry) {
          if (entry.isIntersecting) scheduleBackfill(0);
        });
      }, { root: chatScroll, rootMargin: "120px 0px 0px 0px" });
      observer.observe(button);
    }
    return button;
  }

  // 触发一次更早分段的渲染（共享并发锁；细条 loading 态在这里统一管理）。
  // 全部补齐后细条隐藏
  async function runOlderBatch() {
    const button = loadOlderState.button;
    if (loadOlderState.loading || !loadOlderState.records) return;
    if (loadOlderState.start <= 0) {
      if (button) button.classList.add("is-hidden");
      return;
    }
    loadOlderState.loading = true;
    if (button) button.classList.add("is-loading");
    try {
      await renderOlderBatch();
    } finally {
      loadOlderState.loading = false;
      if (button) button.classList.remove("is-loading");
      if (loadOlderState.start <= 0 && button) button.classList.add("is-hidden");
    }
  }

  // 后台渐进回填调度：批次间隔 idle 延时（批间让出主线程），链式直到全部
  // 记录渲染完——用户无需反复上滚，最终整段历史全部可见。会话切换的判定：
  // 调度闭包持有发起时的 records 引用，renderRecords 换会话会整体替换该
  // 引用，二者不同即说明已切走，立即停止（新会话自会启动自己的回填）
  function scheduleBackfill(delay) {
    if (loadOlderState.timer != null) return;
    if (!loadOlderState.records || loadOlderState.start <= 0) {
      const button = loadOlderState.button;
      if (button && loadOlderState.start <= 0) button.classList.add("is-hidden");
      return;
    }
    const myRecords = loadOlderState.records;
    loadOlderState.timer = window.setTimeout(async function () {
      loadOlderState.timer = null;
      if (loadOlderState.records !== myRecords) return; // 已切走
      const remainBefore = loadOlderState.start;
      await runOlderBatch();
      if (loadOlderState.records !== myRecords) return;
      if (loadOlderState.start < remainBefore && loadOlderState.start > 0) {
        scheduleBackfill(BACKFILL_IDLE_DELAY);
      }
    }, delay || BACKFILL_IDLE_DELAY);
  }

  // 追加更早的一段记录：插到当前聊天流最前面（保持时间顺序），以原首条
  // 消息为锚回拨视口保持阅读位置不跳变；分段边界按 pendingQueueSafeEnd
  // 前置回灌，保证调用/结果配对不被切开
  async function renderOlderBatch() {
    if (!loadOlderState.records) return;
    // 段头回退：落点若是欠账记录（toolResult/压缩 done），连同其配对调用
    // 一起拉进本段（只向更早方向回退，不会越过已渲染区间，无重复渲染）
    const targetStart = pendingQueueSafeStart(
      loadOlderState.records,
      Math.max(0, loadOlderState.start - RENDER_OLDER_BATCH)
    );
    if (targetStart >= loadOlderState.start) return;
    const count = pendingQueueSafeEnd(loadOlderState.records, targetStart, loadOlderState.start - targetStart);
    // 锚点：当前聊天流首条真实消息（跳过顶部的「加载更早消息」细条）
    let anchorNode = chatInner.firstElementChild;
    while (anchorNode && anchorNode.classList.contains("qnav-load-older")) {
      anchorNode = anchorNode.nextElementSibling;
    }
    const pendingTools = {}; // 本段内部配对用（与主渲染队列隔离）
    const pendingCompactions = {};
    const segment = loadOlderState.records.slice(targetStart, targetStart + count);
    const frag = document.createDocumentFragment();
    segment.forEach(function (rec) {
      const before = frag.childNodes.length;
      renderOneRecordInto(frag, rec, pendingTools, pendingCompactions);
      if (rec.round == null) return;
      for (let k = before; k < frag.childNodes.length; k += 1) {
        const node = frag.childNodes[k];
        if (node.nodeType === 1) node.setAttribute("data-round", String(rec.round));
      }
    });
    // 分段尾部未配对的工具块置完成态
    finishUnmatchedTools(pendingTools);
    chatInner.insertBefore(frag, anchorNode || chatInner.firstChild);
    // 分段回灌时用户消息先在离屏 DocumentFragment 中创建，插入后再测量
    // 附件网格宽度，确保引用/媒体/文档方块按当前聊天区宽度排成一到两行。
    if (App.syncMessageAttachmentBlocks) App.syncMessageAttachmentBlocks();
    loadOlderState.start = targetStart;
    // 插入把锚点（及其下方内容）下推：按锚点位移回拨 scrollTop，视口内
    // 阅读位置保持不变；content-visibility 下新内容进入视口附近即真实渲染
    if (anchorNode && anchorNode.parentNode === chatInner) {
      const delta = anchorNode.getBoundingClientRect().top - chatScroll.getBoundingClientRect().top;
      if (delta > 0) {
        const prev = chatScroll.style.scrollBehavior;
        chatScroll.style.scrollBehavior = "auto";
        chatScroll.scrollTop += delta;
        chatScroll.style.scrollBehavior = prev;
      }
    }
    App.rebuildQnav();
    App.updateCodeblockCopyButtons();
    // 回灌段可能包含 ask_user 卡片：与主渲染收尾同口径刷新可答性
    App.refreshAskBlockStates();
  }

  // 初始渲染收尾：有分段时先在聊天流顶部插入「加载更早消息」细条，再瞬跳
  // 到底——并启动后台渐进回填（无需滚动/点击，剩余记录自动分批补全直到
  // 整段历史全部显示；scrollToBottom(true) 内部已临时覆盖 scroll-behavior，
  // content-visibility 懒高度回填由其内部下一帧复位兜底）
  function flushRenderedTail(ctx) {
    if (ctx.loadOlder) {
      const button = createLoadOlderButton();
      loadOlderState.button = button;
      chatInner.insertBefore(button, chatInner.firstChild);
      scheduleBackfill(BACKFILL_IDLE_DELAY);
    }
    scrollToBottom(true);
  }

  function appendTime(ts) {
    if (!ts) return;
    chatInner.appendChild(el("div", "msg-time stage-time", FormatUtils.fmtTime(ts)));
  }

  // 时间行（容器参数化版本，renderOneRecordInto 配套）
  function appendTimeInto(container, ts) {
    if (!ts) return;
    container.appendChild(el("div", "msg-time stage-time", FormatUtils.fmtTime(ts)));
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

    // 压缩块内部 pre 的滚动守卫：delta 高频追加时默认贴底，用户上滚阅读即暂停，
    // 滚回底部附近自动恢复。不做会表现为"生成中拖动滚动条立刻被拉回底部"。
    function isNearBottom(pre) {
      return pre.scrollHeight - pre.scrollTop - pre.clientHeight < 24;
    }
    function attachScrollGuard(pre) {
      if (!pre || pre.__scrollGuarded) return;
      pre.__scrollGuarded = true;
      pre.__stickUser = true;
      pre.addEventListener("wheel", function (e) {
        if (e.deltaY < 0) pre.__stickUser = false;
        else if (isNearBottom(pre)) pre.__stickUser = true;
      }, { passive: true });
      pre.addEventListener("scroll", function () {
        // 程序贴底后此事件同样触发，但此时已在底部，判定结果不变（幂等）
        pre.__stickUser = isNearBottom(pre);
      }, { passive: true });
    }
    function scrollToPreBottom(pre) {
      if (!pre) return;
      attachScrollGuard(pre);
      if (pre.__stickUser === false) return; // 用户上滚阅读中：不拉回
      pre.scrollTop = pre.scrollHeight;
      pre.scrollLeft = 0;
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
          : '<svg class="icon compaction-icon" viewBox="0 0 24 24"><path d="M5 4h14M12 4v6m-3-3 3 3 3-3M5 20h14M12 20v-6m-3 3 3-3 3 3"/></svg>';
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
          // session 压缩的 before/after 为"历史部分"规模（触发判断口径），
          // round 压缩为全量上下文——标签区分，避免与压缩模型输入混淆
          const contextLabel = scope === "session" ? "历史上下文" : "上下文";
          detail.push(contextLabel + " " + FormatUtils.fmtNum(before) + " → " + FormatUtils.fmtNum(after) +
            "（节省 " + FormatUtils.fmtNum(Math.max(0, before - after)) + "）");
        }
        // 触发阈值与触发依据：task/first_call 路径的触发判断是全量上下文
        // （含当前轮轨迹/首调用请求）超阈值，而 before/after 只是历史部分——
        // trigger_context_tokens/trigger_threshold 呈现真实触发比较，消除
        // "历史没到阈值为何压缩"的困惑；auto/manual 路径无这些字段，
        // 此时本批预算即触发阈值，以 before 对照 token_limit 即可
        const tokenLimit = safeNumber(latestCompaction.token_limit);
        const triggerContextTokens = safeNumber(latestCompaction.trigger_context_tokens);
        const triggerThreshold = safeNumber(latestCompaction.trigger_threshold);
        const targetTokens = safeNumber(latestCompaction.target_tokens);
        const budgetScope = latestCompaction.budget_scope
          || (scope === "round" ? "full_request" : "history");
        if (targetTokens !== null && targetTokens > 0) {
          let targetLabel = "历史压缩目标";
          if (budgetScope === "full_request") targetLabel = "完整请求目标";
          else if (budgetScope === "request_history_component") {
            targetLabel = "完整请求目标（本条仅显示历史部分）";
          }
          let targetText = targetLabel + " " + FormatUtils.fmtNum(targetTokens);
          if (budgetScope !== "request_history_component" && after !== null && after > targetTokens) {
            targetText += "（受保留内容或摘要预算下限影响，未完全达到）";
          }
          detail.push(targetText);
        }
        if (triggerThreshold !== null && triggerThreshold > 0) {
          // 强制路径：显示真实触发阈值 + 本批预算（两者不同才有意义）
          detail.push("触发阈值 " + FormatUtils.fmtNum(triggerThreshold));
          if (tokenLimit !== null && tokenLimit > 0 && tokenLimit !== triggerThreshold) {
            detail.push((scope === "session" ? "本批历史预算 " : "本轮压缩预算 ") +
              FormatUtils.fmtNum(tokenLimit));
          }
        } else if (tokenLimit !== null && tokenLimit > 0) {
          const triggered = before !== null && before > tokenLimit;
          const limitLabel = scope === "session"
            ? "历史压缩预算"
            : (triggered ? "触发阈值" : "本轮压缩预算");
          detail.push(limitLabel + " " + FormatUtils.fmtNum(tokenLimit));
        }
        // 触发来源（auto=任务开始检查、task=任务内检查点、
        // first_call=首调用超窗降级、post=任务收尾、manual=手动）：历史回放同样可见
        const triggerReason = latestCompaction.trigger_reason != null
          ? String(latestCompaction.trigger_reason) : "";
        const REASON_LABELS = {
          auto: "任务开始历史检查",
          task: "任务内检查点",
          first_call: "首调用超窗降级",
          post: "任务收尾",
          manual: "手动",
        };
        if (REASON_LABELS[triggerReason]) {
          let reasonText = "触发来源 " + REASON_LABELS[triggerReason];
          const compareThreshold = triggerThreshold !== null && triggerThreshold > 0
            ? triggerThreshold : tokenLimit;
          if (triggerContextTokens !== null && compareThreshold !== null && compareThreshold > 0) {
            reasonText += "（全量上下文 " + FormatUtils.fmtNum(triggerContextTokens) +
              " > 阈值 " + FormatUtils.fmtNum(compareThreshold) + "）";
          }
          detail.push(reasonText);
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
        // 批次诊断（P2-1）：本批喂入压缩模型的源规模 / 批源预算 / 保留为原始
        // 对话的尾部轮次 / 单段输出上限——用于核对"每批是否吃满、压缩比是否
        // 保真"，而不是只看历史整体 before/after（后者无法反映单批行为）
        const batchSourceTokens = safeNumber(latestCompaction.batch_source_tokens != null
          ? latestCompaction.batch_source_tokens : latestUsage && latestUsage.batch_source_tokens);
        const sourceBudget = safeNumber(latestCompaction.source_budget != null
          ? latestCompaction.source_budget : latestUsage && latestUsage.source_budget);
        const tailRounds = safeNumber(latestCompaction.tail_rounds != null
          ? latestCompaction.tail_rounds : latestUsage && latestUsage.tail_rounds);
        const outputTokenLimit = safeNumber(latestCompaction.output_token_limit != null
          ? latestCompaction.output_token_limit : latestUsage && latestUsage.output_token_limit);
        const sourceChunkCount = safeNumber(latestCompaction.source_chunk_count != null
          ? latestCompaction.source_chunk_count : latestUsage && latestUsage.source_chunk_count);
        const sourceWasChunked = latestCompaction.source_was_chunked
          || (latestUsage && latestUsage.source_was_chunked);
        if (batchSourceTokens !== null && batchSourceTokens > 0) {
          let batchText = "本批压缩源 " + FormatUtils.fmtNum(batchSourceTokens);
          if (sourceBudget !== null && sourceBudget > 0) {
            batchText += " / 预算 " + FormatUtils.fmtNum(sourceBudget);
          }
          if (sourceWasChunked && sourceChunkCount !== null && sourceChunkCount > 1) {
            batchText += " · 完整分段 " + FormatUtils.fmtNum(sourceChunkCount) + " 段";
          }
          if (outputTokenLimit !== null && outputTokenLimit > 0) {
            batchText += " · 摘要上限 " + FormatUtils.fmtNum(outputTokenLimit);
          }
          detail.push(batchText);
        }
        if (tailRounds !== null && tailRounds > 0) {
          detail.push("保留原始对话 " + FormatUtils.fmtNum(tailRounds) + " 轮");
        }
        // 源截断警告：单批源超过压缩模型输入预算时中段被丢弃（保真风险），
        // 显式提示，避免"摘要看起来正常但细节已缺失"的静默降级
        const warnings = latestCompaction.warnings
          || (latestUsage && latestUsage.warnings) || null;
        const truncated = latestCompaction.source_was_truncated
          || (latestUsage && latestUsage.source_was_truncated)
          || (Array.isArray(warnings) && warnings.indexOf("batch_source_truncated") >= 0);
        if (truncated) {
          detail.push("⚠ 本批源超输入预算，已头尾截断（中段未进摘要）");
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

  // ---------- 保活渲染：innerHTML 重渲染时保留 md-svg 控件内部状态 ----------
  // 流式期间正文按 Markdown.render 全量重建，```svg/mermaid/canvas 控件会随节点
  // 一起被销毁重建：代码视图滚动位置每帧丢失回顶、运行中的 canvas iframe 被丢弃
  // （表现为"生成中点运行无反应"、切到代码视图拖动滚动条立刻回滚）。
  // 修复采用「锚点挖洞」整块移植：
  //   1. 配对决策走 WidgetReuse.plan（kind+源码精确 → svg/canvas 流式前缀 →
  //      运行中 canvas iframe 兜底）；
  //   2. 保留的旧块**绝不脱离文档**——HTML5 规范 iframe 摘下再插回会重建
  //      browsing context 导致重载重跑；因此新树里对应位置换成 marker 占位，
  //      以保留块为锚把新内容分段 insertBefore，最后清理旧残留节点；
  //   3. 配不上的控件按新建处理（正常升级/重建）。
  function renderPreservingWidgets(host, html) {
    if (!host || typeof html !== "string") return;
    const oldBlocks = Array.prototype.slice.call(host.querySelectorAll(".md-svg-block"));
    if (!oldBlocks.length) {
      host.innerHTML = html;
      return;
    }
    const fresh = document.createElement("div");
    fresh.innerHTML = html;
    const newBlocks = Array.prototype.slice.call(fresh.querySelectorAll(".md-svg-block"));
    if (!newBlocks.length) {
      // 新渲染里已无控件（源码被改掉/删除）：正常替换，控件随之消失
      host.innerHTML = html;
      return;
    }
    const describe = function (block) {
      return {
        kind: block.dataset.svgKind || "",
        code: Markdown.unescapeHtml(block.dataset.svgCode || ""),
        hasFrame: Boolean(block.querySelector(".md-canvas-frame")),
      };
    };
    const plan = WidgetReuse.plan(
      oldBlocks.map(describe),
      newBlocks.map(describe)
    );
    // 新树中配对成功的位置换成 marker 占位（保留块原地不动）；
    // 同时把最新源码同步到保留节点（复制代码/重跑按钮始终用当前版本）
    const markerFor = new Map(); // marker element -> reuseIndex
    const keepSet = new Set();
    for (let k = 0; k < newBlocks.length; k++) {
      const reuseIndex = plan[k].reuseIndex;
      if (reuseIndex < 0) continue;
      const keep = oldBlocks[reuseIndex];
      if (newBlocks[k].dataset.svgCode != null) {
        keep.dataset.svgCode = newBlocks[k].dataset.svgCode;
      }
      const marker = document.createElement("span");
      marker.className = "md-rpw-slot";
      marker.hidden = true;
      markerFor.set(marker, reuseIndex);
      keepSet.add(keep);
      fresh.replaceChild(marker, newBlocks[k]);
    }
    if (!markerFor.size) {
      host.innerHTML = html;
      return;
    }
    // 分段搬移：以最靠前的保留块为锚插入其前内容；遇 marker 则推进锚点到
    // 该保留块的下一个兄弟（保留块本身永不脱离文档，iframe 不重载）；
    // 尾部剩余内容追加到最后。
    const nodes = Array.prototype.slice.call(fresh.childNodes);
    let refNode = null;
    for (let i = 0; i < oldBlocks.length; i++) {
      if (keepSet.has(oldBlocks[i])) { refNode = oldBlocks[i]; break; }
    }
    for (let i = 0; i < nodes.length; i++) {
      const node = nodes[i];
      const reuseIndex = markerFor.get(node);
      if (reuseIndex !== undefined) {
        refNode = oldBlocks[reuseIndex].nextSibling;
        continue;
      }
      node.__rpwFresh = true;
      host.insertBefore(node, refNode);
    }
    // 清理旧残留：顶层节点既非本轮新插入、也非保留块的一律移除
    // （快照后遍历，避免 live childNodes 边删边移位）
    Array.prototype.slice.call(host.childNodes).forEach(function (n) {
      if (n.__rpwFresh || keepSet.has(n)) return;
      if (n.parentNode) n.parentNode.removeChild(n);
    });
  }

  // 用户气泡：content 可为字符串或多部件列表（多模态消息与其历史回放）。
  // 列表取 text 部件作为正文；image_url 渲染图片缩略图；video_url 渲染视频
  // 首帧（浏览器原生 <video>，#t=0.1 确保首帧绘制）；input_audio 渲染固定
  // 格式音频徽标；docs 为发送时随消息的会话文档快照（file_memory，仅前端
  // 展示，不进入消息内容部件）；quotes 为选中文本引用快照（结构化数据，
  // 卡片置顶、正文在下；渲染走 textContent，绝不把引用原文交给 innerHTML）
  function buildUserBubble(content, sessionId, docs, quotes) {
    const bubble = el("div", "msg-bubble");
    const attachmentBlocks = el("div", "msg-user-attachment-blocks");
    // 引用卡片（引用 1/引用 2…）：置于问题正文之前（GPT 同款信息层级）
    if (App.buildQuoteList) {
      const quoteList = App.buildQuoteList(quotes);
      if (quoteList) attachmentBlocks.appendChild(quoteList);
    }
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
          const thumb = el("img", "msg-user-media-thumb clickable", "");
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
          const video = el("video", "msg-user-media-thumb clickable", "");
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
          const chip = el("div", "msg-user-media-audio");
          chip.appendChild(el("span", "msg-user-media-icon", "🎵"));
          chip.appendChild(el("span", "msg-user-media-label", "音频"));
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
        const filename = String(doc.filename);
        const extension = (filename.split(".").pop() || "FILE").toUpperCase();
        const chip = el("div", "msg-user-media-doc clickable");
        chip.appendChild(el("span", "msg-user-media-icon", "📄"));
        chip.appendChild(el("span", "msg-user-media-label", extension.slice(0, 5)));
        chip.title = filename + "（点击预览）";
        chip.setAttribute("aria-label", "文档：" + filename + "，点击预览");
        chip.classList.add("clickable");
        chip.addEventListener("click", function () {
          App.openMediaPreviewForDocument(doc);
        });
        mediaRow.appendChild(chip);
      });
      if (mediaRow.childNodes.length) attachmentBlocks.appendChild(mediaRow);
    }
    if (attachmentBlocks.querySelector(
      ".msg-quote-tile, .msg-user-media-thumb, .msg-user-media-audio, .msg-user-media-doc"
    )) bubble.appendChild(attachmentBlocks);
    if (text) bubble.appendChild(el("div", "msg-user-text", text));
    return bubble;
  }

  function appendUserMessage(content, ts, sessionId, docs, container, round, quotes, sourceEventIndex) {
    const msg = el("div", "msg msg-user");
    if (ts) msg.appendChild(el("div", "msg-time", FormatUtils.fmtTime(ts)));
    msg.appendChild(buildUserBubble(content, sessionId, docs, quotes));
    // 历史轮次的用户消息：记录原始内容供编辑态回填，并挂 hover「编辑」入口。
    // round 为 null（实时发送）时编辑入口在收尾重载后随历史回放出现
    if (round != null) {
      msg.dataset.round = String(round);
      if (Number.isInteger(sourceEventIndex) && sourceEventIndex >= 0) {
        msg.dataset.sourceEvent = String(sourceEventIndex);
      }
      msg._editData = { content: content, round: round, quotes: quotes || [] };
      attachUserEditAction(msg);
    }
    // container 缺省追加到会话流末尾；传入正在流式的助手消息节点时，
    // 气泡嵌入其当前内容之后（工具结果位置），保持注入消息的时间顺序
    (container || chatInner).appendChild(msg);
    const attachmentBlocks = msg.querySelector(".msg-user-attachment-blocks");
    if (attachmentBlocks && App.fitAttachmentBlockGrid) {
      App.fitAttachmentBlockGrid(attachmentBlocks);
    }
    // 流式注入（attachment 指向流节点）时尊重自动贴底暂停：用户上滚阅读
    // 时不被拉回；整段历史回放后由 history.js 的 scrollToBottom 置底复位
    stickToBottom();
    return msg;
  }

  // ---------- 用户消息编辑（GPT 网页同款：hover 编辑 → textarea 重发） ----------
  // 与 GPT 的差异：重发时提供两种删除范围——「重新生成该轮」（原地替换该轮，
  // 之后的轮次保留）与「删除该轮及之后」（GPT 语义，后续轮次一并删除）。
  // 后端按 data-round 精确删除 JSONL 轮次并清理不再引用的用户上传附件。

  // ---------- 用户消息操作行（独立一行，SVG 图标按钮）：复制 / 编辑 ----------
  // 复制：把该消息原始文本回填到剪贴板；编辑：进入编辑态。
  // 与 GPT 网页的差异：重发时提供两种删除范围（重新生成该轮 / 删除该轮及之后）。

  const EDIT_COPY_ICON =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>';
  const EDIT_PENCIL_ICON =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z"/></svg>';
  const EDIT_CHECK_ICON =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M20 6 9 17l-5-5"/></svg>';
  const EDIT_TRASH_ICON =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M3 6h18"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M19 6l-.9 14a2 2 0 0 1-2 1.9H7.9a2 2 0 0 1-2-1.9L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/></svg>';

  function attachUserEditAction(msg) {
    const actions = el("div", "msg-user-actions");

    const copyBtn = el("button", "msg-user-action-btn msg-user-copy-btn");
    copyBtn.type = "button";
    copyBtn.title = "复制";
    copyBtn.innerHTML = EDIT_COPY_ICON + '<span class="msg-user-action-label">复制</span>';
    copyBtn.addEventListener("click", function () {
      const data = msg._editData || {};
      // content 可能是字符串（纯文本消息）或多部件列表（多模态消息）；
      // 带引用时输出人可读文本（「引用 1：…\n\n问题：…」），不输出协议标签
      const plain = contentToPlainText(data.content);
      const text = (data.quotes && data.quotes.length && window.QuoteUtils)
        ? QuoteUtils.composeCopyText(data.quotes, plain)
        : plain;
      if (!text) { App.toast("该消息没有可复制的文本"); return; }
      const done = function () {
        copyBtn.classList.add("copied");
        const label = copyBtn.querySelector(".msg-user-action-label");
        if (label) label.textContent = "已复制";
        setTimeout(function () {
          copyBtn.classList.remove("copied");
          if (label) label.textContent = "复制";
        }, 1400);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done, function () {
          App.toast("复制失败（浏览器未授权剪贴板）");
        });
      } else {
        // 兼容降级：临时 textarea + execCommand
        const ta = document.createElement("textarea");
        ta.value = text;
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        try { document.execCommand("copy"); done(); } catch (_) { App.toast("复制失败"); }
        ta.remove();
      }
    });

    const editBtn = el("button", "msg-user-action-btn msg-user-edit-btn");
    editBtn.type = "button";
    editBtn.title = "编辑这条消息并重发";
    editBtn.innerHTML = EDIT_PENCIL_ICON + '<span class="msg-user-action-label">编辑</span>';
    editBtn.addEventListener("click", function () { beginUserMessageEdit(msg); });

    // 删除该轮（用户消息与回复一并删除，后续轮次保留、轮次号自动前移）：
    // hover 显示，点击弹确认框，确认后走 single 删除并重载会话
    const deleteBtn = el("button", "msg-user-action-btn msg-user-delete-btn");
    deleteBtn.type = "button";
    deleteBtn.title = "删除该轮对话（含该轮回复，后续轮次保留）";
    deleteBtn.innerHTML = EDIT_TRASH_ICON + '<span class="msg-user-action-label">删除</span>';
    deleteBtn.addEventListener("click", function () { requestDeleteRound(msg); });

    // 显示顺序：删除在左（危险操作与内容区隔离），复制/编辑靠右——
    // 常用的复制/编辑位于视觉末端，减少删除按钮的误触概率
    actions.appendChild(deleteBtn);
    actions.appendChild(copyBtn);
    actions.appendChild(editBtn);
    msg.appendChild(actions);
  }

  // 从历史 content 部件提取附件项（media:// 引用，直接复用无需重新上传）
  function extractUserMediaItems(content) {
    const items = [];
    (Array.isArray(content) ? content : []).forEach(function (part) {
      if (!part || typeof part !== "object") return;
      const type = part.type || "";
      let ref = "";
      if (type === "image_url" && part.image_url) ref = part.image_url.url || "";
      else if (type === "video_url" && part.video_url) ref = part.video_url.url || "";
      else if (type === "input_audio" && part.input_audio) ref = part.input_audio.data || "";
      if (typeof ref !== "string" || ref.indexOf("media://") !== 0) return;
      const stored = ref.slice("media://".length);
      const dot = stored.lastIndexOf(".");
      items.push({
        kind: type === "image_url" ? "image" : type === "video_url" ? "video" : "audio",
        media_ref: ref,
        stored_name: stored,
        format: dot > 0 ? stored.slice(dot + 1).toLowerCase() : "",
      });
    });
    return items;
  }

  // content 正文提取（单一入口）：字符串原样返回；多部件列表取 text 部件
  // 拼接。编辑回填、复制按钮共用——纯文本消息 content 是字符串而非数组，
  // 此前两处分别各自实现导致字符串分支漏掉（编辑框不回填/复制按钮失效）
  function contentToPlainText(content) {
    if (typeof content === "string") return content;
    if (!Array.isArray(content)) return content == null ? "" : String(content);
    return content
      .map(function (part) {
        return part && part.type === "text" && typeof part.text === "string" ? part.text : "";
      })
      .join("\n");
  }

  /**
   * 单轮删除编排（hover 删除按钮入口）：确认框 → single 删除该轮整轮
   * （用户消息与回复一并删除，后续轮次保留、轮次号自动前移）→ 重载会话。
   * 运行中由后端 delete_rounds 拒绝（409），前端仅提示；此处不主动停止
   * 生成——删除是破坏性操作且无"顺带重发"的诉求，等待完成或手动停止。
   */
  function requestDeleteRound(msg) {
    if (!msg || !msg._editData || msg.classList.contains("is-editing")) return;
    const data = msg._editData;
    const round = Number(data.round);
    if (!round || round < 1) return;
    if (App.state.streaming && App.state.streamingSession === App.state.sessionId) {
      App.toast("会话正在生成回复，请等待完成或停止后再删除");
      return;
    }
    if (typeof App.openConfirmDialog !== "function") {
      if (!window.confirm("确认删除第 " + round + " 轮对话？该轮回复将一并删除，后续轮次保留。")) return;
      performDeleteRound(round);
      return;
    }
    App.openConfirmDialog({
      title: "删除该轮对话",
      message: "将删除第 " + round + " 轮（该轮用户消息与回复一并删除，" +
        "不再引用的附件会被清理），后续轮次保留且轮次号自动前移。确认删除？",
      confirmText: "确认删除",
      onConfirm: function () { performDeleteRound(round); },
    });
  }

  async function performDeleteRound(round) {
    const sessionId = App.state.sessionId;
    if (!sessionId) return;
    try {
      await API.deleteRounds(sessionId, round, { mode: "single", deleteFiles: true });
    } catch (err) {
      App.toast("删除轮次失败：" + err.message);
      return;
    }
    // 删除成功：整段重载会话（data-round 与编辑数据随新解析自动刷新）
    await App.openSession(sessionId);
    App.toast("已删除第 " + round + " 轮对话");
  }

  /**
   * 在流生成的提问气泡上就地补挂轮次标记与操作入口（编辑/复制/删除）。
   * round_started 事件到达时调用：普通发送的 userNode 此前 round=null 没有
   * 挂入口（收尾不重载就永远没有），后端在任务启动后立即推送本轮最终
   * 轮次号，前端补上 data-round/_editData/操作行；answer 节点同步打标。
   * 附接回放（刷新重连）同样调用：replay marker 携带轮次号与提问原始部件，
   * 复用历史节点或新建气泡后即可挂完整入口（旧后端缺部件时 userContent
   * 为空，跳过打标等收尾重载随历史回放补挂）。
   * targetRound/insertRound 路径已在 send() 内打标，此函数幂等跳过。
   * @param {object} activeStream 活动流对象（含 userNode/messageNode/userContent）
   * @param {number} round 本轮最终轮次号（1-based）
   */
  function attachLiveRoundEntry(activeStream, round) {
    if (!activeStream || typeof round !== "number" || round < 1) return;
    const userNode = activeStream.userNode;
    if (!userNode) return;
    // 已有轮次标记（历史渲染节点 / send() 内编辑重发路径已打标）：视为幂等
    // 命中直接返回，不改写——节点归属与入口数据以先打的标为准，避免错轮
    if (userNode.dataset.round) {
      return;
    }
    // 提问完整 content 未知（旧后端 replay 无部件、纯媒体提问无文本）时
    // 不挂入口：编辑重发会丢失多模态附件，等收尾重载随历史回放补挂
    if (activeStream.userContent == null) return;
    userNode.dataset.round = String(round);
    userNode._editData = {
      content: activeStream.userContent,
      round: round,
      quotes: activeStream.quotes || [],
    };
    attachUserEditAction(userNode);
    if (activeStream.messageNode) {
      activeStream.messageNode.dataset.round = String(round);
    }
  }

  function beginUserMessageEdit(msg) {
    if (!msg || !msg._editData || msg.classList.contains("is-editing")) return;
    // 运行中也允许编辑：发送编排（confirmEditResend）在用户二次确认后会
    // 停止进行中的生成任务再删除/重发；此处仅提示用户存在未完成生成
    if (App.state.streaming && App.state.streamingSession === App.state.sessionId) {
      App.toast("会话正在生成回复：确认发送后会先停止生成再重发");
    }
    const data = msg._editData;
    const originalText = contentToPlainText(data.content);
    const mediaItems = extractUserMediaItems(data.content);
    // 引用快照（选中文本引用到提问）：编辑态可移除、保留，随重发一起上送
    const originalQuotes = (data.quotes || []).slice();
    const keptQuotes = originalQuotes.slice();

    msg.classList.add("is-editing");
    msg._editRestore = msg.querySelector(".msg-bubble");
    if (msg._editRestore) msg._editRestore.style.display = "none";

    const panel = el("div", "msg-bubble msg-edit-bubble");
    const textarea = el("textarea", "msg-edit-textarea");
    textarea.value = originalText;
    textarea.rows = Math.min(10, Math.max(2, originalText.split("\n").length));
    textarea.placeholder = "编辑消息内容…";

    // 引用芯片（编辑态）：短预览 + 移除按钮；空则整行隐藏
    const quoteRow = el("div", "msg-edit-quote-row");
    function renderQuoteRow() {
      quoteRow.innerHTML = "";
      keptQuotes.forEach(function (quote, index) {
        const chip = el("span", "msg-edit-quote-chip");
        chip.title = quote.text;
        chip.appendChild(el("span", "msg-edit-quote-badge", "引用 " + (index + 1)));
        chip.appendChild(el("span", "msg-edit-quote-text",
          window.QuoteUtils ? QuoteUtils.quotePreview(quote.text, 60) : quote.text));
        const remove = el("button", "msg-edit-chip-remove", "×");
        remove.type = "button";
        remove.title = "移除该引用";
        remove.addEventListener("click", function () {
          keptQuotes.splice(index, 1);
          renderQuoteRow();
        });
        chip.appendChild(remove);
        quoteRow.appendChild(chip);
      });
      quoteRow.style.display = keptQuotes.length ? "" : "none";
    }
    renderQuoteRow();

    // 附件芯片（在输入框上方，GPT 编辑态同款）：可移除，原样复用 media:// 引用
    // 不重新上传；图片/视频显示缩略图，音频显示徽标
    const keptMedia = mediaItems.slice();
    const mediaRow = el("div", "msg-edit-media-row");
    function renderMediaRow() {
      mediaRow.innerHTML = "";
      keptMedia.forEach(function (item, index) {
        const chip = el("span", "msg-edit-chip");
        const remove = el("button", "msg-edit-chip-remove", "×");
        remove.type = "button";
        remove.title = "移除该附件";
        remove.addEventListener("click", function () {
          keptMedia.splice(index, 1);
          renderMediaRow();
        });
        const src = App.resolveMediaSrc(item.media_ref, App.state.sessionId);
        if (item.kind === "image" && src) {
          const thumb = el("img", "msg-edit-chip-thumb");
          thumb.src = src;
          thumb.alt = "附件图片";
          chip.appendChild(thumb);
          chip.appendChild(el("span", "msg-edit-chip-name", chipShortName(item.stored_name)));
          // 点击预览（编辑态同样支持，与消息气泡缩略图一致）
          chip.classList.add("clickable");
          chip.title = "点击预览";
          chip.addEventListener("click", function () {
            App.openMediaPreview({ type: "image", src: src, title: "图片" });
          });
        } else if (item.kind === "video" && src) {
          const thumb = el("video", "msg-edit-chip-thumb");
          thumb.src = src + "#t=0.1";
          thumb.muted = true;
          thumb.preload = "metadata";
          thumb.playsInline = true;
          chip.appendChild(thumb);
          chip.appendChild(el("span", "msg-edit-chip-name", "🎬 " + chipShortName(item.stored_name)));
          chip.classList.add("clickable");
          chip.title = "点击播放";
          chip.addEventListener("click", function () {
            App.openMediaPreview({ type: "video", src: src, title: "视频" });
          });
        } else if (item.kind === "audio" && src) {
          chip.appendChild(el("span", "msg-edit-chip-name", mediaChipLabel(item.media_ref, "🎵")));
          chip.classList.add("clickable");
          chip.title = "点击收听";
          chip.addEventListener("click", function () {
            App.openMediaPreview({ type: "audio", src: src, title: "音频" });
          });
        } else {
          chip.appendChild(el("span", "msg-edit-chip-name", mediaChipLabel(item.media_ref, "🎵")));
        }
        chip.appendChild(remove);
        mediaRow.appendChild(chip);
      });
      mediaRow.style.display = keptMedia.length ? "" : "none";
    }
    renderMediaRow();
    panel.appendChild(mediaRow);
    // 引用芯片行在附件行下方（两者都不占 textarea 文本值）
    panel.appendChild(quoteRow);
    panel.appendChild(textarea);

    const foot = el("div", "msg-edit-foot");
    const modes = el("div", "msg-edit-modes");
    let mode = "regen";
    const regenBtn = el("button", "msg-edit-mode selected", "重新生成该轮");
    const truncateBtn = el("button", "msg-edit-mode", "删除该轮及之后");
    regenBtn.type = "button";
    truncateBtn.type = "button";
    regenBtn.title = "删除该轮回复后原位重新生成，之后的轮次保留（其上下文不含旧回复）";
    truncateBtn.title = "删除该轮及其后所有轮次（新回复追加在末尾，GPT 同款语义）";
    regenBtn.addEventListener("click", function () {
      mode = "regen";
      regenBtn.classList.add("selected");
      truncateBtn.classList.remove("selected");
    });
    truncateBtn.addEventListener("click", function () {
      mode = "truncate";
      truncateBtn.classList.add("selected");
      regenBtn.classList.remove("selected");
    });
    modes.appendChild(regenBtn);
    modes.appendChild(truncateBtn);
    foot.appendChild(modes);

    const buttons = el("div", "msg-edit-buttons");
    const cancelBtn = el("button", "msg-edit-cancel", "取消");
    const sendBtn = el("button", "msg-edit-send", "发送");
    cancelBtn.type = "button";
    sendBtn.type = "button";

    // 面板内确认条（复用 plans 区域）：发送/取消防误触的统一入口
    function showPlanConfirm(message, confirmLabel, onConfirm) {
      const area = panel._planArea;
      if (!area) { onConfirm(); return; }
      panel._confirmShown = true;
      area.innerHTML = "";
      const box = el("div", "msg-edit-confirm");
      box.appendChild(el("div", "msg-edit-confirm-desc", message));
      const actions = el("div", "msg-edit-confirm-actions");
      const cancel = el("button", "msg-edit-cancel", "取消");
      const ok = el("button", "msg-edit-send", confirmLabel);
      cancel.type = "button";
      ok.type = "button";
      cancel.addEventListener("click", function () {
        panel._confirmShown = false;
        area.innerHTML = "";
      });
      ok.addEventListener("click", function () {
        ok.disabled = true;
        onConfirm();
      });
      actions.appendChild(cancel);
      actions.appendChild(ok);
      box.appendChild(actions);
      area.appendChild(box);
    }

    // 编辑是否产生过改动（文本/附件集合/引用集合任一变化即视为已编辑）
    function isEdited() {
      if (textarea.value !== originalText) return true;
      if (keptMedia.length !== mediaItems.length) return true;
      if (keptQuotes.length !== originalQuotes.length) return true;
      if (keptQuotes.some(function (quote, index) {
        return !originalQuotes[index] || originalQuotes[index].text !== quote.text;
      })) return true;
      return keptMedia.some(function (item, index) {
        return !mediaItems[index] || mediaItems[index].stored_name !== item.stored_name;
      });
    }

    cancelBtn.addEventListener("click", function () {
      if (!isEdited()) {
        // 内容未变：直接退出，不打扰
        exitUserMessageEdit(msg);
        return;
      }
      // 内容已改：防误触确认（放弃将丢失本次编辑）
      showPlanConfirm("修改尚未发送，确认放弃本次修改？", "放弃修改", function () {
        exitUserMessageEdit(msg);
      });
    });
    // Esc 等全局取消入口共用同一防误触规则（未改直接退出，改了先确认）；
    // 确认条已弹出时再按 Esc 视为「确认放弃」直接退出，避免二次弹窗死循环
    panel._requestCancel = function () {
      if (panel._confirmShown) { exitUserMessageEdit(msg); return; }
      if (!isEdited()) { exitUserMessageEdit(msg); return; }
      showPlanConfirm("修改尚未发送，确认放弃本次修改？", "放弃修改", function () {
        exitUserMessageEdit(msg);
      });
    };
    sendBtn.addEventListener("click", function () {
      const text = textarea.value.trim();
      if (!text && !keptMedia.length) {
        App.toast("消息内容不能为空");
        return;
      }
      if (mode === "truncate") {
        // 删除该轮及之后：confirmEditResend 内部先 dry_run 展示明细再确认
        App.confirmEditResend({
          msg: msg,
          round: data.round,
          text: text,
          media: keptMedia,
          quotes: keptQuotes.slice(),
          mode: mode,
        });
        return;
      }
      // 重新生成该轮：发送前轻量确认（旧回复将被替换，防手抖误触）
      showPlanConfirm("将以当前内容重新生成该轮，旧回复将被替换。", "确认发送", function () {
        App.confirmEditResend({
          msg: msg,
          round: data.round,
          text: text,
          media: keptMedia,
          quotes: keptQuotes.slice(),
          mode: mode,
        });
      });
    });
    buttons.appendChild(cancelBtn);
    buttons.appendChild(sendBtn);
    foot.appendChild(buttons);
    panel.appendChild(foot);

    const planArea = el("div", "msg-edit-plans");
    panel.appendChild(planArea);
    // 预演结果展示区（confirmEditResend 填充）：确认后执行真实删除
    panel._planArea = planArea;
    panel._getText = function () { return textarea.value.trim(); };
    panel._getMedia = function () { return keptMedia.slice(); };

    msg.appendChild(panel);
    textarea.focus();
    textarea.selectionStart = textarea.value.length;
    App.autosize && App.autosize();
  }

  function exitUserMessageEdit(msg) {
    if (!msg || !msg.classList.contains("is-editing")) return;
    const panel = msg.querySelector(".msg-edit-bubble");
    if (panel) panel.remove();
    if (msg._editRestore) msg._editRestore.style.display = "";
    msg._editRestore = null;
    msg.classList.remove("is-editing");
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

  // 附件芯片短名：stored_name 去扩展名，超长截断（缩略图芯片用）
  function chipShortName(stored) {
    if (!stored) return "附件";
    const dot = stored.lastIndexOf(".");
    const base = dot > 0 ? stored.slice(0, dot) : stored;
    return base.length > 24 ? base.slice(0, 24) + "…" : base;
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
    // 实时预览行：流式期间在「思考过程」右侧显示最新一行思考（0.2s 节流，
    // 新行替换旧行）；思考结束/权威回放时清除，收尾保持折叠头原样
    const preview = el("span", "think-preview");
    toggle.appendChild(preview);
    const content = el("div", "think-content");
    toggle.addEventListener("click", function () { wrap.classList.toggle("open"); });
    // 展开态下双击显示区域即可折叠（展开仍走头部按钮）
    content.title = "双击折叠";
    content.addEventListener("dblclick", function () { wrap.classList.remove("open"); });
    wrap.appendChild(toggle);
    wrap.appendChild(content);

    let lastPreview = "";
    let lastPreviewAt = 0;
    let previewTimer = null;
    const PREVIEW_THROTTLE_MS = 200;

    // 思考文本双缓冲：textContent += / getter 在长思考时是 O(全长) 的
    // 序列化+重排热路径（高速模型逐帧追加时页面卡顿主源之一）。
    // 权威文本留在内存缓冲，80ms 批量落 DOM 一次；读取走缓冲（零 DOM 开销）
    let fullText = "";      // 权威累积文本（含未落 DOM 部分）
    let dirtyText = false;  // 缓冲有未落 DOM 增量
    let bufTimer = null;
    function flushText() {
      if (bufTimer) { clearTimeout(bufTimer); bufTimer = null; }
      if (!dirtyText) return;
      dirtyText = false;
      content.textContent = fullText;
      content.scrollTop = content.scrollHeight;
    }
    function scheduleTextFlush() {
      dirtyText = true;
      if (bufTimer) return;
      bufTimer = setTimeout(flushText, 80);
    }

    // 取累积文本的最后一个非空行作为预览（新行替代旧行）；全部空白则清空。
    // 截断到 160 字符兜底：即使环境样式异常（按钮内容宽度计算不受控），
    // DOM 文本也不会超长
    function latestLine() {
      // 走内存缓冲：避免每次渲染预览都对整个思考块做 textContent 序列化
      const text = fullText;
      let tail = "";
      for (let i = text.length - 1; i >= 0; i--) {
        const ch = text[i];
        if (ch === "\n" || ch === "\r") {
          if (tail) break;
          continue;
        }
        tail = ch + tail;
      }
      return tail.trim().slice(0, 160);
    }

    function renderPreview() {
      const line = latestLine();
      if (!line) {
        preview.textContent = "";
        preview.title = "";
        return;
      }
      preview.textContent = line;
      preview.title = line;
      lastPreview = line;
      lastPreviewAt = Date.now();
    }

    function schedulePreview() {
      const elapsed = Date.now() - lastPreviewAt;
      if (elapsed >= PREVIEW_THROTTLE_MS) {
        renderPreview();
        return;
      }
      if (previewTimer) return;
      previewTimer = setTimeout(function () {
        previewTimer = null;
        renderPreview();
      }, PREVIEW_THROTTLE_MS - elapsed);
    }

    function clearPreview() {
      if (previewTimer) {
        clearTimeout(previewTimer);
        previewTimer = null;
      }
      preview.textContent = "";
      preview.title = "";
      lastPreview = "";
      lastPreviewAt = 0;
    }

    return {
      wrap: wrap,
      setText: function (t) {
        fullText = String(t || "");
        if (bufTimer) { clearTimeout(bufTimer); bufTimer = null; }
        dirtyText = false;
        content.textContent = fullText;
        content.scrollTop = content.scrollHeight;
        schedulePreview();
      },
      add: function (delta) {
        fullText += delta;
        scheduleTextFlush();
        schedulePreview();
      },
      textContent: function () { return fullText; },
      streaming: function () { wrap.classList.add("is-streaming"); },
      done: function () {
        clearPreview();
        flushText(); // 收尾强制刷出缓冲增量（不能丢最后 80ms 的思考内容）
        wrap.classList.remove("is-streaming");
      },
    };
  }

  // 工具调用块：头部（名称+状态）+ 可展开主体（输入 JSON / 输出文本）
  // 默认折叠，不自动展开（避免流式期间反复撑高页面）；用户点开即可看到
  // 逐帧流入的参数数据。上游把 arguments 整段一次性下发时（部分供应商
  // 不分片），由打字机揭示器按节奏逐步显示，模拟逐帧生成的观感。
  function buildToolBlock(name) {
    const wrap = el("div", "tool-block");
    const head = el("button", "tool-block-head");
    const toolIconClass = "icon tool-block-tool-icon" +
      (App.isBuiltinToolName(name) ? " is-builtin-tool-icon" : "");
    const toolIcon = App.toolIconSvg(name, toolIconClass);
    const statusIcon = '<svg class="icon tool-status-icon tool-spin" viewBox="0 0 24 24" aria-hidden="true"><path d="M21 12a9 9 0 1 1-6.2-8.56"/></svg>';
    head.innerHTML =
      toolIcon + statusIcon +
      '<span class="tool-block-generic-action">调用工具</span>' +
      '<span class="tool-block-action" hidden></span>' +
      '<span class="tool-block-name" hidden></span>' +
      '<span class="tool-block-file" hidden></span>' +
      '<span class="tool-block-diff" hidden></span>' +
      '<svg class="icon tool-block-chevron" viewBox="0 0 24 24"><path d="m6 9 6 6 6-6"/></svg>';

    const body = el("div", "tool-block-body");
    const inLabel = el("span", "tool-io-label", "输入");
    const inPre = el("pre", "tool-io", "");
    const outLabel = el("span", "tool-io-label", "输出");
    const outPre = el("pre", "tool-io tool-io-out", "");
    let currentToolName = "";

    function updateToolIdentity(nextName) {
      currentToolName = String(nextName || "tool");
      const isBuiltin = App.isBuiltinToolName(currentToolName);
      const actionName = App.toolDisplayName(currentToolName);
      const icon = head.querySelector(".tool-block-tool-icon");
      if (icon) {
        icon.outerHTML = App.toolIconSvg(
          currentToolName,
          "icon tool-block-tool-icon" + (isBuiltin ? " is-builtin-tool-icon" : "")
        );
      }
      const genericAction = head.querySelector(".tool-block-generic-action");
      const action = head.querySelector(".tool-block-action");
      const nameNode = head.querySelector(".tool-block-name");
      genericAction.hidden = Boolean(actionName);
      action.hidden = !actionName;
      action.textContent = actionName;
      nameNode.hidden = Boolean(actionName);
      nameNode.textContent = currentToolName;
    }
    updateToolIdentity(name);

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

    // 输出区上方的展示用 diff 视图（由 setDiff 注入；clearDiff 在文本重设时移除）
    let diffView = null;
    function clearDiff() {
      if (diffView && diffView.parentNode) {
        diffView.parentNode.removeChild(diffView);
      }
      diffView = null;
    }

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

    // 头部文件名：完整路径（相对路径按会话工作目录解析），一行放不下
    // 自动换行（CSS 钳制最多两行，超出省略；完整路径始终在 hover title
    // 与点击复制内容里）
    function setFileTagValue(path) {
      const fileTag = head.querySelector(".tool-block-file");
      if (!fileTag) return;
      const full = resolveToolPath(path);
      if (!full) return;
      fileTag.textContent = full;
      fileTag.title = full + "（点击复制）";
      fileTag.onclick = function () {
        try {
          navigator.clipboard.writeText(full).then(function () {}, function () {});
        } catch (_) { /* 忽略 */ }
      };
      fileTag.hidden = false;
    }

    return {
      wrap: wrap,
      setFileTag: setFileTagValue,
      setName: updateToolIdentity,
      setInput: function (t) {
        // 整体重设（结果返回/历史回放路径）：清空揭示队列直接显示最终文本
        stopReveal();
        revealQueue = "";
        inPre.textContent = t || "{}";
      },
      setOutput: function (t) { outPre.textContent = t || "(无输出)"; },
      // 附带展示用 diff 视图（write_file/edit_file 结果）：渲染在输出区上方，
      // 头部显示完整文件路径（可换行、垂直居中）+ "+N绿 / -M红" 双徽标
      setDiff: function (diff) {
        clearDiff();
        if (!diff) return;
        diffView = buildDiffView(diff);
        body.insertBefore(diffView, outLabel);
        const badge = head.querySelector(".tool-block-diff");
        if (badge) {
          badge.innerHTML = "";
          const added = Number(diff.lines_added) || 0;
          const removed = Number(diff.lines_removed) || 0;
          const skipped = diff.diff_skipped || "";
          if (skipped === "unchanged") {
            badge.appendChild(el("span", "diff-num is-add", "无变化"));
          } else if (skipped === "file_too_large") {
            badge.appendChild(el("span", "diff-num is-del", "文件过大"));
          } else {
            // +N 恒绿、-M 恒红（不再按 add-only/del-only 整体变色）
            badge.appendChild(el("span", "diff-num is-add", "+" + added));
            badge.appendChild(el("span", "diff-num is-del", "-" + removed));
          }
          badge.hidden = false;
          wrap.classList.add("has-diff");
        }
        // 头部文件名：完整路径（与参数提取路径共用同一渲染）
        setFileTagValue(diff.path);
      },
      clearDiff: function () {
        if (diffView && diffView.parentNode) {
          diffView.parentNode.removeChild(diffView);
        }
        diffView = null;
        const badge = head.querySelector(".tool-block-diff");
        if (badge) {
          badge.innerHTML = "";
          badge.hidden = true;
        }
        const fileTag = head.querySelector(".tool-block-file");
        if (fileTag) {
          fileTag.textContent = "";
          fileTag.hidden = true;
        }
        wrap.classList.remove("has-diff");
      },
      // 流式参数生成开始：标记状态并清空输出占位（不改变折叠态）
      beginStream: function () {
        streaming = true;
        wrap.classList.add("is-streaming");
        outPre.textContent = "";
        inPre.textContent = "";
        revealQueue = "";
        clearDiff();
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
        const statusIcon = head.querySelector(".tool-status-icon");
        if (statusIcon) statusIcon.remove();
        if (!outPre.textContent || outPre.textContent === "执行中…") {
          outPre.textContent = "(无输出)";
        }
      },
    };
  }

  // ---------- 展示用 diff 视图（write_file / edit_file 结果） ----------
  // 解析 unified diff 文本：@@ hunk 头（淡化分隔）、+ 添加（绿）、- 删除（红）、
  // 空格行上下文；首字符规则解析，与后端 difflib 输出格式强约定。
  // 行内语法高亮：从 ---/+++ 头推断文件语言 → 行文本经 Prism 高亮后注入
  // .diff-text（Prism 输出已 HTML 转义，textContent 先行、高亮成功才替换）；
  // 无 Prism / 语言不支持 / 高亮异常自动回退纯文本。

  // diff 文件名后缀 → Prism 语言名（与 markdown.js DIFF_EXTS 同口径；未收录返回空串）
  const DIFF_FILE_EXTS = {
    py: "python",
    js: "javascript", mjs: "javascript", jsx: "javascript",
    ts: "typescript", tsx: "typescript",
    cpp: "cpp", cc: "cpp", cxx: "cpp", hpp: "cpp", h: "cpp", c: "cpp",
    java: "java", cs: "csharp",
    css: "css", less: "less", scss: "scss", sass: "sass", qss: "css",
    qml: "qml", html: "markup", htm: "markup", xml: "markup", svg: "markup",
    go: "go", rs: "rust",
    sh: "bash", bash: "bash",
    json: "json", jsonc: "json", jsonl: "json",
    yaml: "yaml", yml: "yaml",
    ini: "ini", env: "ini",
    cmake: "cmake", makefile: "makefile", dockerfile: "docker",
  };

  // 从 diff 头部（--- a/xxx / +++ b/xxx）推断内层语言；缓存按 diffObj 引用
  const diffLangCache = new WeakMap();
  function inferDiffFileLang(diffObj, diffText) {
    if (diffObj && diffLangCache.has(diffObj)) return diffLangCache.get(diffObj);
    let lang = "";
    const head = (diffText || "").split("\n", 4);
    for (let i = 0; i < head.length && !lang; i++) {
      // difflib 头：`--- a/相对路径` / `+++ b/相对路径`；兼容裸路径写法
      const m = head[i].match(/^--- (?:a\/)?(\S+)|^\+\+\+ (?:b\/)?(\S+)/);
      if (!m) continue;
      const file = m[1] || m[2] || "";
      const base = file.replace(/\\/g, "/").split("/").pop().toLowerCase();
      const ext = base.indexOf(".") >= 0 ? base.split(".").pop() : "";
      if (ext && DIFF_FILE_EXTS[ext]) lang = DIFF_FILE_EXTS[ext];
      else if (base.startsWith("dockerfile")) lang = "docker";
      else if (base.startsWith("makefile") || base.startsWith("gnumakefile")) lang = "makefile";
      else if (base.startsWith("cmakelists")) lang = "cmake";
      else if (base.startsWith(".env")) lang = "ini";
    }
    if (diffObj) diffLangCache.set(diffObj, lang);
    return lang;
  }

  // 行文本 → Prism 高亮 HTML（未高亮时返回 null，调用方回退 textContent）。
  // 与官方 highlight() 同链路：tokenize 后必须先 util.encode 转义再 stringify
  //（Token.stringify 自身不转义字符串内容，漏 encode 会把 <script> 裸注入）。
  function highlightDiffLine(text, lang) {
    if (!window.Prism || !Prism.languages || !Prism.languages[lang]) return null;
    try {
      const encoded = Prism.util.encode(Prism.tokenize(text, Prism.languages[lang]));
      return Prism.Token.stringify(encoded, lang);   // 输出已 HTML 转义
    } catch (_) {
      return null;
    }
  }

  function buildDiffView(diffObj) {
    const box = el("div", "diff-view");
    const statsBits = [];
    const added = Number(diffObj && diffObj.lines_added) || 0;
    const removed = Number(diffObj && diffObj.lines_removed) || 0;
    if (added) statsBits.push('+' + added);
    if (removed) statsBits.push('-' + removed);
    const truncated = !!(diffObj && diffObj.diff_truncated);
    const skipped = (diffObj && diffObj.diff_skipped) || "";

    const bar = el("div", "diff-bar");
    bar.appendChild(el("span", "diff-title", "文件变更"));
    const stats = el("span", "diff-stats");
    if (statsBits.length) {
      statsBits.forEach(function (bit) {
        stats.appendChild(el("span", bit.charAt(0) === "+" ? "diff-num is-add" : "diff-num is-del", bit));
      });
    } else if (skipped === "unchanged") {
      stats.appendChild(el("span", "diff-num", "内容无变化"));
    } else if (skipped === "file_too_large") {
      stats.appendChild(el("span", "diff-num", "文件过大，跳过 diff"));
    }
    if (truncated) {
      stats.appendChild(el("span", "diff-num", "diff 已截断"));
    }
    bar.appendChild(stats);
    box.appendChild(bar);

    const text = String((diffObj && diffObj.diff) || "");
    if (text) {
      // 语言推断一次并缓存（WeakMap 随 diffObj 生命周期，历史回放同样命中）
      const lineLang = inferDiffFileLang(diffObj, text);
      const pre = el("pre", "diff-code" + (lineLang ? " language-" + lineLang : ""));
      const lines = text.split("\n");
      let oldNo = 0;
      let newNo = 0;
      lines.forEach(function (line) {
        if (line.indexOf("@@") === 0) {
          const hunk = el("div", "diff-line is-hunk", line);
          const match = /@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)?/.exec(line);
          if (match) {
            oldNo = parseInt(match[1], 10);
            newNo = parseInt(match[2], 10);
          }
          pre.appendChild(hunk);
          return;
        }
        if (line.indexOf("--- ") === 0 || line.indexOf("+++ ") === 0) {
          pre.appendChild(el("div", "diff-line is-file", line));
          return;
        }
        const kind = line.charAt(0);
        const cls = kind === "+" ? "is-add" : (kind === "-" ? "is-del" : "is-ctx");
        const row = el("div", "diff-line " + cls);
        // 双行号列：左列=旧行号（+ 行留空）、右列=新行号（- 行留空），
        // 两个固定宽度右对齐子列，删除/新增的号码不再挤在同一位置
        const gutter = el("span", "diff-no");
        gutter.appendChild(el("span", "diff-no-old", kind === "+" ? "" : String(oldNo)));
        gutter.appendChild(el("span", "diff-no-new", kind === "-" ? "" : String(newNo)));
        row.appendChild(gutter);
        row.appendChild(el("span", "diff-sign", kind === " " ? "" : kind));
        const cell = el("span", "diff-text");
        const content = line.slice(1);
        if (lineLang) {
          const html = highlightDiffLine(content, lineLang);
          if (html != null) {
            cell.innerHTML = html;   // Prism Token.stringify 输出已转义，安全
          } else {
            cell.textContent = content;
          }
        } else {
          cell.textContent = content;
        }
        row.appendChild(cell);
        if (kind === "+") {
          newNo += 1;
        } else if (kind === "-") {
          oldNo += 1;
        } else {
          oldNo += 1;
          newNo += 1;
        }
        pre.appendChild(row);
      });
      box.appendChild(pre);
    }
    return box;
  }

  // 从工具参数（对象或 JSON 文本均可）提取标题栏路径信息：
  // read_file/write_file/edit_file 用 full_file_name；search_files 用搜索目录 dir_path；
  // 兜底 _file_diff 无 path 字段的旧数据。参数可能是流式未闭合 JSON，宽松正则兜底。
  function extractPathFromArgsValue(args, name) {
    let obj = args;
    if (typeof args === "string") {
      args = args.trim();
      if (!args) return "";
      try {
        obj = JSON.parse(args);
      } catch (_) {
        const m = /"full_file_name"\s*:\s*"([^"]+)"/.exec(args)
          || /"dir_path"\s*:\s*"([^"]+)"/.exec(args)
          || /"path"\s*:\s*"([^"]+)"/.exec(args);
        return m ? m[1].replace(/\\\\/g, "\\") : "";
      }
    }
    if (!obj || typeof obj !== "object") return "";
    if (name === "search_files") {
      // 目录 + 搜索模式组合展示；未指定目录时回退显示模式（保证有识别信息）
      const dir = String(obj.dir_path || "").trim();
      const pattern = String(obj.pattern || "").trim();
      if (dir && pattern) return dir + "（" + pattern + "）";
      return dir || pattern;
    }
    return String(obj.full_file_name || obj.path || "").trim();
  }

  // 相对路径按会话工作目录解析为完整路径（模型常传相对路径，标题栏展示
  // 完整定位）；已是绝对路径（盘符/根/~）或无法得知工作目录时原样返回
  function resolveToolPath(path) {
    const raw = String(path || "").trim();
    if (!raw) return "";
    if (/^([A-Za-z]:[\\/]|[\\/]|~)/.test(raw)) return raw;
    const workDir = String((App.state && App.state.workDir) || "").trim();
    if (!workDir) return raw;
    const rel = raw.replace(/^\.[\\/]/, "");
    const sep = workDir.indexOf("\\") !== -1 ? "\\" : "/";
    return workDir.replace(/[\\/]+$/, "") + sep + rel;
  }

  // 从 unified diff 文件头（--- a/xxx / +++ b/xxx）提取展示路径
  function extractPathFromDiffText(diffText) {
    if (!diffText) return "";
    const m = /^--- (?:a\/)?(.+)$/m.exec(diffText);
    return m ? m[1].trim() : "";
  }

  /** 工具输出统一入口：带 file_diff 的结构化结果渲染 diff 视图 + 统计摘要文本，
   * 其余一律按原文纯文本输出（MCP 版同名工具 / 旧历史无 diff 自动回退）。
   * args（可选，对象或 JSON 文本）：工具原始参数——用于在 _file_diff 无 path
   * 字段的旧数据里兜底提取完整文件路径（full_file_name），以及为 read_file /
   * write_file / search_files 等无 diff 结果在标题栏显示路径/目录信息。 */
  function applyToolResult(block, name, resultText, fileDiff, args) {
    if (name === "edit_file" || name === "write_file" || name === "run_command") {
      const toolIcon = block && block.wrap && block.wrap.querySelector(".tool-block-tool-icon");
      if (toolIcon) {
        const failed = name === "run_command"
          ? isCommandFailureResult(resultText)
          : isToolFailureResult(resultText);
        toolIcon.classList.toggle("is-failed", failed);
      }
    }
    let parsed = null;
    if (fileDiff && typeof fileDiff === "object") {
      try {
        const obj = JSON.parse(resultText);
        if (obj && typeof obj === "object" && obj.path != null) parsed = obj;
      } catch (_) { /* 回退纯文本 */ }
      // 旧数据 _file_diff 无 path 字段：从参数或 diff 文件头兜底提取
      if (parsed && !fileDiff.path) {
        fileDiff.path = extractPathFromArgsValue(args, name)
          || extractPathFromDiffText(fileDiff.diff) || "";
      }
    }
    if (parsed) {
      const statsBits = [];
      if (parsed.lines_added || parsed.lines_removed) {
        statsBits.push("diff +" + parsed.lines_added + " -" + parsed.lines_removed + " 行");
      } else if (parsed.diff_skipped) {
        statsBits.push(parsed.diff_skipped === "unchanged" ? "内容无变化" : "文件过大，跳过 diff");
      } else if (parsed.created != null) {
        statsBits.push(parsed.created ? "新建文件" : "全文覆盖");
      }
      const head = String(parsed.message || "[write_file] 已写入");
      const note = statsBits.length ? head + "（" + statsBits.join("，") + "）" : head;
      block.setOutput(note);
      block.setDiff(fileDiff);
    } else {
      block.setOutput(resultText);
    }
    // 标题栏路径：diff 未带路径（含无 diff 的 read_file / search_files /
    // 旧数据 write_file）时从参数补齐；diff 已带路径时 setDiff 已设置
    if (!fileDiff || !fileDiff.path) {
      block.setFileTag(extractPathFromArgsValue(args, name));
    }
  }

  function isToolFailureResult(resultText) {
    let value = resultText;
    if (typeof value === "string") {
      try { value = JSON.parse(value); } catch (_) { /* 按普通文本错误识别 */ }
    }
    if (value && typeof value === "object") {
      if (value.error != null && value.error !== "") return true;
      if (value.ok === false || value.success === false || value.isError === true || value.is_error === true) return true;
      if (typeof value.status === "string" && /^(error|failed|failure|aborted)$/i.test(value.status.trim())) return true;
      return false;
    }
    return /^(?:error|failed|failure|错误|失败)(?:\b|[:：\s])/i.test(String(value || "").trim());
  }

  function isCommandFailureResult(resultText) {
    const text = typeof resultText === "string"
      ? resultText
      : JSON.stringify(resultText == null ? "" : resultText);
    // run_command 的首行是工具生成的元数据，包含退出码和超时状态；只检查
    // 这一行，避免 stdout/stderr 正文（例如读取文件中的“stderr”）造成误判。
    const header = text.split(/\r?\n/, 1)[0];
    const exitCode = /\bexit=(-?\d+)\b/i.exec(header);
    if (/命令超时|已超过 timeout=/i.test(header)) return true;
    if (exitCode) return Number(exitCode[1]) !== 0;
    return /^(?:\[run_command\]\s*)?(?:error|failed|failure|错误|失败)(?:\b|[:：\s])/i.test(header.trim());
  }

  // ---------- 子任务块（sub_agent） ----------
  // 父智能体经 sub_agent 工具派发的子任务独立渲染块：完整保存思考/正文/
  // 工具轨迹/todo，按 agent_id 聚合。SSE（start/delta/model_call/tool_start/
  // tool_result/todo/done）与历史回放（history_parser 聚合的 agentBlock
  // 记录）共用同一状态机；done 后折叠为摘要态，点击头部可再展开。

  const SUB_AGENT_STATUS_TEXT = {
    done: "已完成",
    error: "出错",
    stopped: "已停止",
    interrupted: "已中断",
    timeout: "超时",
    max_rounds: "轮次上限",
  };
  const SUB_AGENT_ERROR_STATUSES = ["error", "stopped", "interrupted", "timeout", "max_rounds"];

  function subAgentTaskSummary(task) {
    const text = String(task || "");
    const firstLine = text.split("\n").find(function (line) { return line.trim(); }) || "";
    return firstLine.trim().slice(0, 80) || "子任务";
  }

  function buildSubAgentBlock() {
    const wrap = el("div", "agent-block is-running");
    const head = el("button", "agent-head");
    head.type = "button";
    const iconWrap = el("span", "agent-icon");
    const title = el("span", "agent-title", "子任务");
    const summary = el("span", "agent-summary");
    const badge = el("span", "agent-badge", "运行中");
    const chevron = el("span", "agent-chevron");
    chevron.innerHTML = '<svg class="icon" viewBox="0 0 24 24"><path d="m6 9 6 6 6-6"/></svg>';
    head.appendChild(iconWrap);
    head.appendChild(title);
    head.appendChild(summary);
    head.appendChild(badge);
    head.appendChild(chevron);
    const body = el("div", "agent-body");
    wrap.appendChild(head);
    wrap.appendChild(body);

    let taskSummary = "";
    let todoItems = null;
    let rounds = [];          // [{seq, el, thinkBlock, contentLabel, contentEl, toolEntries: Map}]
    let status = "running";
    let finalReplyText = "";
    let usageTotal = null;
    let errorText = "";
    let roundsLimit = null;
    let isDone = false;

    head.addEventListener("click", function () {
      if (!body.children.length) return;
      wrap.classList.toggle("open");
    });
    body.title = "双击折叠";
    body.addEventListener("dblclick", function () {
      if (!wrap.classList.contains("open")) return;
      wrap.classList.remove("open");
    });

    function appendLabeledPre(labelText, cls) {
      body.appendChild(el("div", "agent-label", labelText));
      const pre = el("pre", "agent-pre" + (cls ? " " + cls : ""));
      body.appendChild(pre);
      return pre;
    }

    function autoOpen() {
      if (!wrap.classList.contains("open")) {
        wrap.classList.add("open");
      }
    }

    function scrollToPreBottom(pre) {
      if (pre) pre.scrollTop = pre.scrollHeight;
    }

    /** 状态徽标与块级状态类。 */
    function applyStatus(nextStatus) {
      status = nextStatus;
      isDone = nextStatus !== "running";
      wrap.classList.remove("is-running", "is-done", "is-error");
      if (nextStatus === "running") wrap.classList.add("is-running");
      else if (SUB_AGENT_ERROR_STATUSES.indexOf(nextStatus) >= 0) wrap.classList.add("is-error");
      else wrap.classList.add("is-done");
      badge.textContent = nextStatus === "running" ? "运行中" : (SUB_AGENT_STATUS_TEXT[nextStatus] || nextStatus);
    }

    function findRound(seq) {
      for (let i = rounds.length - 1; i >= 0; i -= 1) {
        if (rounds[i].seq === seq) return rounds[i];
      }
      return null;
    }

    /** model_call：新开一轮分区。思考复用主区 think-block（默认折叠+流式
     * 三点动画），正文区始终显示（空时隐藏标题），工具行复用
     * 主区 tool-block（默认折叠）。authoritative=true 表示来自 model_call
     * 权威整轮数据（或历史回放）：思考/正文按整轮数据替换（delta 流式期间
     * 已实时显示同内容），避免重复叠加。 */
    function beginRound(evt, authoritative) {
      let round = findRound(evt.seq);
      if (!round && evt.seq != null && rounds.length) {
        const last = rounds[rounds.length - 1];
        // 兼容旧流：无 seq 的 delta 先建隐式轮，后续 model_call 认领归位
        if (last.seq == null && !last.hydrated) {
          last.seq = evt.seq;
          const labelEl = last.el.querySelector(".agent-round-label");
          if (labelEl) labelEl.textContent = "第 " + evt.seq + " 轮";
          round = last;
        }
      }
      if (!round) {
        const section = el("div", "agent-round");
        if (rounds.length) section.appendChild(el("div", "agent-round-divider"));
        section.appendChild(el("div", "agent-round-label", "第 " + (evt.seq != null ? evt.seq : rounds.length + 1) + " 轮"));
        const thinkBlock = buildThinkBlock();          // 思考：默认折叠 + 流式三点
        const contentLabel = el("div", "agent-label is-empty", "正文"); // 正文始终显示，无内容隐藏标题
        const contentEl = el("div", "agent-content");
        const toolsWrap = el("div", "agent-tools");
        section.appendChild(thinkBlock.wrap);
        section.appendChild(contentLabel);
        section.appendChild(contentEl);
        section.appendChild(toolsWrap);
        round = {
          seq: evt.seq,
          el: section,
          thinkBlock: thinkBlock,
          contentLabel: contentLabel,
          contentEl: contentEl,
          contentText: "",
          toolsWrap: toolsWrap,
          toolEntries: new Map(),
          hydrated: false,
        };
        rounds.push(round);
        body.appendChild(section);
      }
      if (typeof evt.reasoning_content === "string" && evt.reasoning_content) {
        // 替换式写入：与 delta 流式期间累积的内容一致，防止重复叠加
        round.thinkBlock.setText(evt.reasoning_content);
        round.thinkBlock.wrap.classList.remove("is-empty");
      } else if (authoritative && !round.thinkBlock.textContent()) {
        round.thinkBlock.wrap.classList.add("is-empty");
      }
      if (typeof evt.content === "string" && evt.content) {
        round.contentText = evt.content;
        renderPreservingWidgets(round.contentEl, Markdown.render(evt.content));
        round.contentLabel.classList.remove("is-empty");
        App.highlightCodeBlocks(round.contentEl);
        App.updateCodeblockCopyButtons();
      } else if (authoritative && !round.contentText) {
        round.contentLabel.classList.add("is-empty");
      }
      (evt.tool_calls || []).forEach(function (call) {
        const fn = call.function || {};
        const entry = addToolLine(round, fn.name || call.name || "tool", fn.arguments || "", null);
        // tool_calls 声明行登记：tool_start/tool_result 按 id 回填，避免重复建行
        if (call.id && !round.toolEntries.has(call.id)) round.toolEntries.set(call.id, entry);
      });
      if (authoritative) {
        round.hydrated = true;
        round.thinkBlock.done(); // 权威数据到达：清除流式三点指示
      }
      return round;
    }

    /** 参数文本统一序列化：tool_start.arguments / model_call tool_calls 可能是对象。 */
    function normalizeArgsText(argsText) {
      if (argsText == null) return "";
      if (typeof argsText === "string") return argsText;
      try { return JSON.stringify(argsText, null, 2); } catch (e) { return String(argsText); }
    }

    /** 轮内工具行：复用主区 tool-block（头部折叠，主体=输入/输出），返回
     * 与主区一致的 ui 控制器（tool_start 标执行中 / tool_result 填结果）。 */
    function addToolLine(round, name, argsText, resultText) {
      const block = buildToolBlock(name);
      if (argsText != null && argsText !== "") block.setInput(normalizeArgsText(argsText));
      if (resultText != null) block.setOutput(resultText);
      round.toolsWrap.appendChild(block.wrap);
      return block;
    }

    function findToolEntry(round, toolCallId) {
      if (toolCallId && round.toolEntries.has(toolCallId)) return round.toolEntries.get(toolCallId);
      return null;
    }

    function currentRound() {
      return rounds.length ? rounds[rounds.length - 1] : null;
    }

    /** 流式期间正文 md 节流重渲染：与主消息区一致用 Markdown.render 全量重建，
     * 但按帧直接渲染会高频重排（每块含代码高亮/复制按钮重建），40ms 合并一次。 */
    function scheduleContentRender(round) {
      if (round.contentRenderTimer) return;
      round.contentRenderTimer = setTimeout(function () {
        round.contentRenderTimer = null;
        if (round.contentText) {
          renderPreservingWidgets(round.contentEl, Markdown.render(round.contentText));
          App.highlightCodeBlocks(round.contentEl);
          App.updateCodeblockCopyButtons();
        }
      }, 40);
    }

    /** SSE phase=delta：实时追加到所属轮；seq 已知但轮未建时先建隐式轮占位
     * （新一轮 delta 先于 model_call 到达，不能落进上一轮）。 */
    function appendLiveDelta(evt) {
      const round = currentRoundBySeq(evt) || beginRound({ seq: evt.seq != null ? evt.seq : null });
      const rc = typeof evt.reasoning_delta === "string" ? evt.reasoning_delta : "";
      const ct = typeof evt.content_delta === "string" ? evt.content_delta : "";
      if (rc) {
        round.thinkBlock.wrap.classList.remove("is-empty");
        round.thinkBlock.add(rc);
        round.thinkBlock.streaming();
      }
      if (ct) {
        round.contentText = (round.contentText || "") + ct;
        round.contentLabel.classList.remove("is-empty");
        round.contentEl.classList.remove("is-empty");
        scheduleContentRender(round);
      }
      autoOpen();
    }

    function currentRoundBySeq(evt) {
      // seq 已知：严格匹配所属轮（找不到交给调用方建隐式轮），避免串轮
      if (evt.seq != null) return findRound(evt.seq);
      // 旧流兼容：无 seq 的 delta 落到最新轮（无轮由调用方建隐式轮）
      return rounds.length ? rounds[rounds.length - 1] : null;
    }

    /** tool_start：执行中占位行（tool_result 到达时回填）。
     * 后端字段为 function_name（旧数据兼容 tool_name）；若 model_call 已按
     * tool_calls 预建声明行，则复用该块并标执行中，避免重复。 */
    function addToolEntry(evt) {
      const round = currentRoundBySeq(evt) || beginRound({ seq: evt.seq });
      const toolCallId = evt.tool_call_id || "";
      let entry = findToolEntry(round, toolCallId);
      if (entry) {
        entry.executing();
        return;
      }
      entry = addToolLine(round, evt.function_name || evt.tool_name || "tool", evt.arguments, null);
      entry.executing();
      if (toolCallId) round.toolEntries.set(toolCallId, entry);
    }

    /** tool_result：按 tool_call_id 回填结果（缺 start 时补建整行）。 */
    function fillToolEntry(evt) {
      const round = currentRoundBySeq(evt) || beginRound({ seq: evt.seq });
      let entry = findToolEntry(round, evt.tool_call_id);
      if (!entry) {
        entry = addToolLine(round, evt.tool_name || "tool", evt.arguments, null);
        if (evt.tool_call_id) round.toolEntries.set(evt.tool_call_id, entry);
      }
      entry.finish();
      const resultText = typeof evt.result === "string" ? evt.result : JSON.stringify(evt.result, null, 2);
      applyToolResult(entry, evt.tool_name, resultText, evt.file_diff, evt.arguments);
    }

    function renderTodo(items) {
      if (!items || !items.length) return null;
      const box = el("div", "agent-todo");
      items.forEach(function (item) {
        const row = el("div", "agent-todo-item is-" + (item.status || "pending"));
        const mark = item.status === "done" ? "✓" : (item.status === "in_progress" ? "○" : "·");
        row.appendChild(el("span", "agent-todo-mark", mark));
        row.appendChild(el("span", "agent-todo-text", String(item.content || item.text || "")));
        box.appendChild(row);
      });
      return box;
    }

    /** start / todo / done 统一重渲染头部与任务/计划/收尾区。 */
    function renderBody() {
      // 任务区 + 计划区固定在最前：重建时保留 rounds 区与最终回复区
      const keep = [];
      while (body.firstChild) {
        keep.push(body.removeChild(body.firstChild));
      }
      if (taskSummary) {
        appendLabeledPre("任务", "agent-task").textContent = taskSummary;
      }
      if (todoItems && todoItems.length) {
        body.appendChild(el("div", "agent-label", "计划"));
        const todoBox = renderTodo(todoItems);
        if (todoBox) body.appendChild(todoBox);
      }
      keep.forEach(function (node) { body.appendChild(node); });
    }

    function applyDone(evt) {
      if (isDone) return;
      if (evt && evt.status) applyStatus(evt.status);
      finalReplyText = evt && typeof evt.final_reply === "string" ? evt.final_reply : finalReplyText;
      usageTotal = evt && evt.usage_total && typeof evt.usage_total === "object" ? evt.usage_total : usageTotal;
      errorText = evt && typeof evt.error === "string" ? evt.error : errorText;
      // 收尾清理：未到达 model_call 的轮次清掉思考流式三点；
      // 待渲染的正文 md 定时器立即刷出（避免 done 先于 40ms 节流到达丢字）
      rounds.forEach(function (r) {
        r.thinkBlock.done();
        if (r.contentRenderTimer) {
          clearTimeout(r.contentRenderTimer);
          r.contentRenderTimer = null;
          if (r.contentText) {
            renderPreservingWidgets(r.contentEl, Markdown.render(r.contentText));
            App.highlightCodeBlocks(r.contentEl);
            App.updateCodeblockCopyButtons();
          }
        }
      });
      // 收尾区：最终回复（按 md 渲染；模型产出多为结构化文本，可读性更好）
      if (finalReplyText) {
        const finalPre = appendLabeledPre("最终回复 → 父智能体", "agent-final-reply");
        finalPre.innerHTML = Markdown.render(finalReplyText);
        App.highlightCodeBlocks(finalPre);
        App.updateCodeblockCopyButtons();
      }
      if (usageTotal && Number(usageTotal.total_tokens) > 0) {
        body.appendChild(el("div", "agent-usage", FormatUtils.usageText(usageTotal)));
      }
      if (errorText) {
        body.appendChild(el("div", "agent-error-detail", errorText));
      }
      // 头部徽标补充轮次（已完成 · 3 轮）
      if (evt && evt.rounds != null) {
        badge.textContent = badge.textContent + " · " + evt.rounds + " 轮";
      }
      wrap.classList.remove("open"); // done 后折叠为摘要态（可点头部展开）
      head.setAttribute("aria-expanded", "false");
    }

    /** tool_return.sub_agent 到达：块尾追加"已返回父智能体"引用条。 */
    function markReturned(subInfo) {
      if (wrap.querySelector(".agent-returned")) return;
      const bar = el("div", "agent-returned", "✔ 最终回复已返回父智能体");
      wrap.appendChild(bar);
      const info = subInfo && subInfo.usage_total && Number(subInfo.usage_total.total_tokens) > 0
        ? " · " + FormatUtils.usageText(subInfo.usage_total)
        : "";
      if (info) bar.title = "子任务消耗 " + info;
    }

    return {
      wrap: wrap,
      applyEvent: function (evt) {
        if (!evt || typeof evt !== "object") return;
        const phase = evt.phase;
        if (phase === "start") {
          taskSummary = subAgentTaskSummary(evt.task);
          summary.textContent = taskSummary;
          if (Array.isArray(evt.todo) && evt.todo.length) todoItems = evt.todo;
          if (evt.rounds_limit != null) roundsLimit = Number(evt.rounds_limit);
          renderBody();
          autoOpen();
          return;
        }
        if (phase === "delta") { appendLiveDelta(evt); return; }
        if (phase === "model_call") { beginRound(evt, true); return; }
        if (phase === "tool_start") { addToolEntry(evt); return; }
        if (phase === "tool_result") { fillToolEntry(evt); return; }
        if (phase === "todo") {
          todoItems = Array.isArray(evt.todos) ? evt.todos : todoItems;
          renderBody();
          return;
        }
        if (phase === "done") { applyDone(evt); return; }
        if (phase === "notice") {
          // 交付保障重试提示（空收尾重试/断流续跑/todo 提醒）：
          // 以浅色条目追加到块内；历史回放侧 history_parser 对该 phase
          // 走通用条目兜底，hydrate 忽略（仅实时流展示）
          const message = typeof evt.message === "string" ? evt.message : "";
          if (message) {
            body.appendChild(el("div", "agent-notice", "↻ " + message));
            autoOpen();
          }
          return;
        }
      },
      /** 历史回放：entries 逐条重放 + done 聚合字段。 */
      hydrate: function (record) {
        if (!record) return;
        (record.entries || []).forEach(function (entry) {
          const evt = Object.assign({}, entry, { phase: entry.phase });
          if (entry.phase === "model_call") {
            beginRound(evt, true);
          } else if (entry.phase === "tool_start") {
            addToolEntry(evt);
          } else if (entry.phase === "tool_result") {
            fillToolEntry(evt);
          } else if (entry.phase === "todo") {
            applyEventTodo(evt);
          } else if (entry.phase === "notice") {
            const message = typeof entry.message === "string" ? entry.message : "";
            if (message) body.appendChild(el("div", "agent-notice", "↻ " + message));
          }
        });
        applyDone({
          status: record.status || "interrupted",
          final_reply: record.final_reply || "",
          rounds: record.rounds,
          usage_total: record.usage_total,
          error: record.error || "",
          ended_at: record.ended_at,
        });
      },
      markReturned: markReturned,
      isDone: function () { return isDone; },
      /** 流中断兜底：未收到 done 的块标记中断态。 */
      finalizeInterrupted: function () {
        if (!isDone) applyDone({ status: "interrupted" });
      },
    };

    function applyEventTodo(evt) {
      todoItems = Array.isArray(evt.todos) ? evt.todos : todoItems;
      renderBody();
    }
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

  // ---------- 三控件（svg/mermaid/canvas）显示缩放 ----------
  // 缩放状态 = 块的 data-zoom（倍率）+ CSS 变量 --md-zoom：
  // - svg/mermaid 图片视图：内联宽度表达式乘 var(--md-zoom)，放大后由视图
  //   容器（overflow:auto）滚动查看、缩小则居中（margin:0 auto）；
  // - canvas 沙箱：iframe 尺寸不变，倍率经 postMessage 下发给沙箱，stage
  //   元素尺寸乘倍率（object-fit:contain 等比放大 + body 滚动）。
  // 导出 PNG 不受查看缩放影响：svgBlockToPngBlob 除以当前倍率还原基准尺寸。
  const ZoomUtils = window.ZoomUtils || {
    normalizeScale: function (s) {
      const n = Number(s);
      return Number.isFinite(n) && n > 0 ? n : 1;
    },
    stepScale: function (s, d) {
      const n = Number(s) > 0 ? Number(s) : 1;
      return d > 0 ? n * 1.1 : n / 1.1;
    },
    canStep: function () { return true; },
  };

  function zoomScaleOf(block) {
    const s = block && block.dataset ? parseFloat(block.dataset.zoom) : NaN;
    return Number.isFinite(s) && s > 0 ? s : 1;
  }

  function updateZoomControls(block) {
    const scale = zoomScaleOf(block);
    const resetBtn = block.querySelector(".md-zoom-reset");
    if (resetBtn) resetBtn.textContent = Math.round(scale * 100) + "%";
    const outBtn = block.querySelector('[data-zoom-action="out"]');
    const inBtn = block.querySelector('[data-zoom-action="in"]');
    if (outBtn) outBtn.disabled = !ZoomUtils.canStep(scale, -1);
    if (inBtn) inBtn.disabled = !ZoomUtils.canStep(scale, +1);
  }

  // 应用倍率：写 dataset 与 CSS 变量；canvas 运行中同步下发沙箱；
  // 全屏中的 svg/mermaid 由 syncSvgFullscreenLayout 按新倍率重算 px 宽度
  function applyZoomScale(block, scale) {
    if (!block) return;
    const next = ZoomUtils.normalizeScale(scale);
    block.dataset.zoom = String(next);
    block.style.setProperty("--md-zoom", String(next));
    block.classList.toggle("is-zoomed", next !== 1);
    updateZoomControls(block);
    if (document.fullscreenElement === block) syncSvgFullscreenLayout();
    if (block.classList.contains("md-canvas-block")) {
      const frame = block.querySelector(".md-canvas-frame");
      if (frame && frame.contentWindow) {
        frame.contentWindow.postMessage({
          type: "canvas-zoom",
          id: block.dataset.canvasId || "",
          zoom: next,
        }, "*");
      }
    }
  }

  function stepZoom(block, dir) {
    applyZoomScale(block, ZoomUtils.stepScale(zoomScaleOf(block), dir));
  }

  // 缩放按钮（三控件共用）：独立委托，早于 SVG/Canvas 两套 action 委托判断
  // （缩放按钮无 data-svg-action/data-canvas-action，天然不冲突）
  chatInner.addEventListener("click", function (e) {
    const btn = e.target.closest("[data-zoom-action]");
    if (!btn) return;
    const block = btn.closest(".md-svg-block");
    if (!block) return;
    const action = btn.dataset.zoomAction;
    if (action === "in") stepZoom(block, +1);
    else if (action === "out") stepZoom(block, -1);
    else if (action === "reset") applyZoomScale(block, 1);
  });

  // Ctrl+滚轮缩放（仅 svg/mermaid 图片视图；canvas 在跨源 iframe 内，
  // 滚轮事件到不了父文档，其缩放走按钮/沙箱消息）
  chatInner.addEventListener("wheel", function (e) {
    if (!e.ctrlKey) return;
    const view = e.target.closest ? e.target.closest(".md-svg-view") : null;
    if (!view) return;
    const block = view.closest(".md-svg-block");
    if (!block) return;
    e.preventDefault();
    stepZoom(block, e.deltaY < 0 ? +1 : -1);
  }, { passive: false });

  // ---------- SVG 生成控件（```svg 双视图块）交互 ----------
  // 视图切换/复制代码/复制图片全部事件委托：markdown 流式重渲染会重建节点
  chatInner.addEventListener("click", async function (e) {
    const btn = e.target.closest("[data-svg-action]");
    if (!btn) return;
    const block = btn.closest(".md-svg-block");
    if (!block) return;
    const action = btn.dataset.svgAction;
    // Mermaid 失败/显示异常时的手动重渲染（仅 mermaid 控件有此按钮）：
    // 重新走调度链路（命中缓存瞬时复用，未命中则重新 mermaid.render）
    if (action === "rerender") {
      rerenderMermaid(block);
      return;
    }
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
    // 全屏（SVG/Mermaid 控件共用）：作用于整个控件块，当前视图（图片/代码）
    // 铺满全屏视口；Esc 或再点一次退出。fullscreenchange 统一刷新按钮文案
    if (action === "full") {
      if (document.fullscreenElement === block) {
        document.exitFullscreen();
        return;
      }
      if (!document.fullscreenEnabled) {
        App.toast("当前环境不支持全屏显示");
        return;
      }
      block.requestFullscreen().catch(function (err) {
        App.toast("进入全屏失败：" + (err && err.message || err));
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
      let blob = null;
      let firstErr = "";
      try {
        blob = await svgBlockToPngBlob(block);
      } catch (err) {
        firstErr = (err && err.message) || String(err);
      }
      if (blob) {
        try {
          if (navigator.clipboard && window.ClipboardItem) {
            await navigator.clipboard.write([new ClipboardItem({ "image/png": blob })]);
            btn.textContent = "✓ 已复制图片";
          } else {
            throw new Error("剪贴板图片接口不可用");
          }
        } catch (err) {
          // 写剪贴板失败（焦点丢失/权限拒绝等）：导出本身成功，降级为下载 PNG
          const url = URL.createObjectURL(blob);
          const link = document.createElement("a");
          link.href = url;
          link.download = "svg-image.png";
          link.click();
          setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
          btn.textContent = "✓ 已下载 PNG";
          App.toast("写入剪贴板失败（" + ((err && err.message) || err) + "），已降级为下载");
        }
      } else {
        // 导出环节失败（图未渲染/解码失败等）：按钮 + toast 透出具体原因
        btn.textContent = "复制失败";
        App.toast("复制图片失败：" + (firstErr || "未知错误"));
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

  // 导出专用净化：foreignObject → <text>/<tspan>。mermaid 12 用 foreignObject
  // 承载节点/边标签（HTML 内容），SVG 一经 Image 解码 + canvas 绘制就会被浏览器
  // 判定为「污染画布」，toBlob/toDataURL 抛 SecurityError（headless Chrome 实测：
  // 去 foreignObject 即干净，仅去 <style> 无效，htmlLabels:false 配置在 12 上也不
  // 生效）。把 HTML 标签替换为等价纯 SVG 文本后即可安全导出——只处理导出克隆，
  // 页面显示用的 SVG 不动；产物样式表里的 .label text 规则会为生成文本补齐
  // 颜色/字号，视觉与显示基本一致。多行标签（多个 <p>）转多个 <tspan> 分行。
  const SVG_NS = "http://www.w3.org/2000/svg";
  function sanitizeSvgForExport(svgEl) {
    svgEl.querySelectorAll("foreignObject").forEach(function (fo) {
      const w = parseFloat(fo.getAttribute("width")) || 0;
      const h = parseFloat(fo.getAttribute("height")) || 0;
      const ps = fo.querySelectorAll("p");
      let lines;
      if (ps.length) {
        lines = [];
        ps.forEach(function (p) { lines.push((p.textContent || "").trim()); });
      } else {
        lines = (fo.textContent || "").trim().split(/\n+/).map(function (s) { return s.trim(); });
      }
      const text = document.createElementNS(SVG_NS, "text");
      text.setAttribute("x", w / 2);
      text.setAttribute("y", h / 2);
      text.setAttribute("text-anchor", "middle");
      text.setAttribute("dominant-baseline", "middle");
      const lineH = 18; // 16px 默认字号的近似行高
      const startY = -(lineH * (lines.length - 1)) / 2;
      lines.forEach(function (line, i) {
        const tspan = document.createElementNS(SVG_NS, "tspan");
        tspan.setAttribute("x", w / 2);
        tspan.setAttribute("y", h / 2 + startY + i * lineH);
        tspan.textContent = line;
        text.appendChild(tspan);
      });
      fo.parentNode.replaceChild(text, fo);
    });
    // 其它潜在脏源（iframe/object/embed/script）一并移除，防二次污染
    svgEl.querySelectorAll("iframe,object,embed,script").forEach(function (n) { n.remove(); });
    return svgEl;
  }

  // 把 SVG 双视图块的图片视图光栅化为 PNG：
  // 内联 SVG → Blob(data:image/svg+xml) → Image 解码 → canvas 绘制 → PNG Blob。
  // 尺寸优先取实际渲染的显示尺寸（getBoundingClientRect）——mermaid 产物的
  // svg 根是 width="100%" 且 height 已被移除，直接读属性会得到 100×viewBox
  // 的崩坏比例；不可见（代码视图激活）时回退 viewBox 基准尺寸。
  // 按 2x~3x（跟随 devicePixelRatio）超采样导出，粘贴出去不发虚。
  async function svgBlockToPngBlob(block) {
    const view = block.querySelector('[data-svg-view="image"] svg');
    if (!view) {
      // mermaid 渲染失败/被中断时图片视图没有 svg——给出可操作的提示
      if (block.dataset.svgKind === "mermaid" && block.dataset.mermaidState !== "done") {
        throw new Error("图片尚未渲染成功，点「↻ 重渲染」恢复后再试");
      }
      throw new Error("找不到可导出的 SVG");
    }
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
      // 除当前显示缩放：导出始终按 100% 基准尺寸，不随查看倍率放大/缩小
      const zoom = zoomScaleOf(block);
      baseW = baseW / zoom;
      baseH = baseH / zoom;
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
    // 总像素上限：超宽图长边钳制后面积仍可能爆表，canvas 尺寸过大会让
    // toBlob 静默返回 null（表现为复制失败无报错）——按面积再钳一次
    const MAX_PIXELS = 40000000; // 40MP，远超常规图，仅拦截极端长条图
    if (w * h > MAX_PIXELS) {
      const k = Math.sqrt(MAX_PIXELS / (w * h));
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
    // 两段式导出：先按原始克隆保真导出（保留 foreignObject 的 HTML 标签）；
    // toBlob 抛 SecurityError（画布被污染）时做净化（foreignObject→<text>）
    // 重导一次——净化牺牲一点排版保真，换取完整文字与干净画布。
    let clean = false;
    for (;;) {
      const source = clean ? sanitizeSvgForExport(clone) : clone;
      const blobSrc = new Blob([new XMLSerializer().serializeToString(source)], { type: "image/svg+xml;charset=utf-8" });
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
        const blob = await new Promise(function (resolve, reject) {
          canvas.toBlob(function (b) {
            b ? resolve(b) : reject(new Error("PNG 导出失败"));
          }, "image/png");
        });
        return blob;
      } catch (err) {
        const msg = String((err && err.message) || err);
        if (!clean && /taint|secur/i.test(msg)) {
          clean = true; // 画布污染：净化克隆后重导一次
          continue;
        }
        throw err;
      } finally {
        setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
      }
    }
  }

  // ---------- Canvas 沙箱运行时（```canvas 程序块） ----------
  // 每块独立 <iframe sandbox="allow-scripts">（不给 allow-same-origin →
  // opaque origin：父页读不到它的 contentDocument，它也访问不到页面
  // DOM/cookie/存储/同源接口）。通信全走 postMessage：
  //   1) iframe BOOT 完成 → 回发 canvas-ready；
  //   2) 父页按 ev.source 匹配 frame，下发 canvas-run{id,code}；
  //   3) iframe 内 new Function('stage','console',code) 执行，console 逐行
  //      实时回传（canvas-log{id,line}）；
  //   4) 同步体结束后"结算探测"：包装 rAF/定时器统计待执行回调数——归零则
  //      脚本是一次性绘制，回传 canvas-done{logs,shot}（shot 为沙箱内
  //      stage.toDataURL 自报快照——跨源读不到画布像素）；仍有任务在跑
  //      （动画/游戏循环）则回发 canvas-live，父页保持运行态：用户可与
  //      画布交互（点击/键盘），父页可随时下发 canvas-shot 索要当前帧；
  //      脚本自身收尾（live 归零）时自动补发 canvas-done 携带最终快照；
  //      异步回调抛错经 __onasync 上报 canvas-error，父页复位状态。
  // 用户代码不经 srcdoc 注入，避免 HTML 解析转义风险；流式重建的旧
  // iframe 随节点一起被丢弃，无需显式回收。

  const CANVAS_RUNTIME_CSS =
    // height:100%：body 高度必须显式铺满 iframe，#stage 的百分比高度才有
    // 确定的解析基准（否则随内容收缩，画布位图与点击坐标全部失真）；
    // html 锁定不滚动、body 承担滚动——显示缩放（stage 尺寸乘倍率）放大后
    // 舞台超出 iframe 视口时由 body 滚动查看
    "html,body{margin:0;padding:0;width:100%;height:100%;background:transparent;}" +
    "html{overflow:hidden;}" +
    "body{overflow:auto;}" +
    // object-fit:contain：画布位图等比缩放居中，常规/全屏下都不拉伸变形；
    // margin:0 auto 让未溢出时水平居中，放大溢出时 auto 归零（从左起边可滚）
    "#stage{display:block;width:100%;height:100%;object-fit:contain;margin:0 auto;}";

  // 沙箱内宿主脚本：监听父页指令，执行用户脚本并回传日志/异常/快照
  const CANVAS_BOOT_JS = [
    "var stage=document.getElementById('stage');",
    // 保留原生定时器引用（包装前的），探测逻辑自身不走被包装的全局
    "var __raf=window.requestAnimationFrame.bind(window),",
    "  __craf=window.cancelAnimationFrame.bind(window),",
    "  __st=window.setTimeout.bind(window),",
    "  __si=window.setInterval.bind(window),",
    "  __cst=window.clearTimeout.bind(window),",
    "  __csi=window.clearInterval.bind(window);",
    // 活跃度追踪：__live = 待执行的 rAF/timeout/interval 回调数
    "var __live=0,__pend={r:{},t:{},i:{}},__sentLive=false,__runId=null,logs=[];",
    "function shot(){",
    "  try{return stage.toDataURL('image/png');}catch(e){",
    "    return 'ERR:'+String(e&&e.message||e);",
    "  }",
    "}",
    "function fmt(v){",
    "  try{return typeof v==='string'?v:JSON.stringify(v);}catch(_){return String(v);}",
    "}",
    "function join(args){return Array.prototype.map.call(args,fmt).join(' ');}",
    "function push(line){",
    "  logs.push(line);if(logs.length>400)logs.splice(0,logs.length-400);",
    "  parent.postMessage({type:'canvas-log',id:__runId,line:line},'*');",
    "}",
    // 独活中 live 归零 → 脚本自然收尾：补发 done 与最终快照（宏任务里复查，
    // 给 rAF 回调"先减计数再调度下一帧"的过程留出重新累加的机会）
    "function __checkDone(){",
    "  if(!__sentLive||__live>0)return;",
    "  __sentLive=false;",
    "  parent.postMessage({type:'canvas-done',id:__runId,logs:logs.slice(-200),shot:shot()},'*');",
    "}",
    "function __rel(kind,h){",
    "  if(__pend[kind][h]){delete __pend[kind][h];__live--;}",
    "  if(__sentLive)__st(__checkDone,0);",
    "}",
    // 异步回调抛错不静默：上报 canvas-error，父页复位状态（日志行由父页
    // 在 canvas-error 分支统一追加，这里不再重复 push）
    "function __onasync(e){",
    "  parent.postMessage({type:'canvas-error',id:__runId,logs:logs.slice(-200),shot:shot(),error:String(e&&e.message||e)},'*');",
    "}",
    "window.requestAnimationFrame=function(cb){",
    "  var h;h=__raf(function(t){__rel('r',h);try{cb(t);}catch(e){__onasync(e);}});",
    "  __pend.r[h]=1;__live++;return h;",
    "};",
    "window.cancelAnimationFrame=function(h){__rel('r',h);return __craf(h);};",
    "window.setTimeout=function(f,ms){",
    "  var a=Array.prototype.slice.call(arguments,2),h;",
    "  h=__st(function(){__rel('t',h);try{f.apply(null,a);}catch(e){__onasync(e);}},ms);",
    "  __pend.t[h]=1;__live++;return h;",
    "};",
    "window.clearTimeout=function(h){__rel('t',h);return __cst(h);};",
    "window.setInterval=function(f,ms){",
    "  var a=Array.prototype.slice.call(arguments,2),h;",
    "  h=__si(function(){try{f.apply(null,a);}catch(e){__onasync(e);}},ms);",
    "  __pend.i[h]=1;__live++;return h;",
    "};",
    "window.clearInterval=function(h){__rel('i',h);return __csi(h);};",
    "window.addEventListener('message',function(ev){",
    "  var d=ev.data||{};",
    "  if(d.type==='canvas-shot'){",
    "    parent.postMessage({type:'canvas-shot-data',id:d.id,shot:shot()},'*');",
    "    return;",
    "  }",
    // 显示缩放：父页把倍率下发为 stage 元素尺寸百分比（object-fit:contain
    // 等比放大位图），body 滚动查看；toDataURL 快照不受影响（位图原始尺寸）
    "  if(d.type==='canvas-zoom'){",
    "    try{stage.style.width=(d.zoom*100)+'%';stage.style.height=(d.zoom*100)+'%';}catch(_){}",
    "    return;",
    "  }",
    "  if(d.type!=='canvas-run')return;",
    "  __runId=d.id;",
    "  if(d.zoom&&d.zoom!==1){try{stage.style.width=(d.zoom*100)+'%';stage.style.height=(d.zoom*100)+'%';}catch(_){}}",
    "  var sc={log:function(){push(join(arguments));},",
    "    info:function(){push(join(arguments));},",
    "    warn:function(){push('[warn] '+join(arguments));},",
    "    error:function(){push('[error] '+join(arguments));}};",
    "  try{",
    "    new Function('stage','console',d.code)(stage,sc);",
    "  }catch(err){",
    "    push('[异常] '+String(err&&err.message||err));",
    "    parent.postMessage({type:'canvas-error',id:d.id,logs:logs.slice(-200),shot:shot(),error:String(err&&err.message||err)},'*');",
    "    return;",
    "  }",
    // 结算探测：同步体结束后等一帧再判定（期间脚本已把自己的首轮 rAF/
    // 定时任务挂上）；仍有任务 → 独活模式，父页保持运行态供交互
    "  __st(function(){__raf(function(){",
    "    if(__live>0){",
    "      __sentLive=true;",
    "      parent.postMessage({type:'canvas-live',id:d.id},'*');",
    "    }else{",
    "      parent.postMessage({type:'canvas-done',id:d.id,logs:logs.slice(-200),shot:shot()},'*');",
    "    }",
    "  });});",
    "});",
    // 未捕获异常兜底（用户直接挂在 stage/window 上的监听器抛错不走包装）
    "window.onerror=function(msg,src,line){",
    "  __onasync(new Error(String(msg)+(line?('（第'+line+'行）'):'')));",
    "  return true;",
    "};",
    // 事件监听支持：
    // 1) stage 指针事件的 offsetX/offsetY 换算为画布位图坐标——沙箱画布
    //    以 object-fit:contain 等比缩放居中，CSS 坐标 ≠ 位图坐标，不换算
    //    会导致点击命中有系统性偏移；facade 继承原事件，preventDefault 等
    //    方法显式转发；
    // 2) 监听器计入活跃度：纯点击交互的小游戏没有帧循环，也要保持运行态；
    //    removeEventListener 对应回落（按原监听器函数映射到包装函数）。
    "var __liveListeners=new WeakMap();",
    "function __wrapEvent(e){",
    "  if(!e||typeof e.clientX!=='number')return e;",
    "  try{",
    "    var r=stage.getBoundingClientRect();",
    "    var s=Math.min(r.width/stage.width,r.height/stage.height)||1;",
    "    var ox=(r.width-stage.width*s)/2,oy=(r.height-stage.height*s)/2;",
    // Proxy 而非 Object.create 继承：原生事件访问器（clientX/target 等）
    // 依赖内部槽位，原型链继承后 this 指向 facade 会抛 Illegal invocation；
    // Proxy 的 get 先拦截 offset 坐标，其余属性回退原事件（函数绑定 this）
    "    return new Proxy(e,{",
    "      get:function(t,p){",
    "        if(p==='offsetX')return (t.clientX-r.left-ox)/s;",
    "        if(p==='offsetY')return (t.clientY-r.top-oy)/s;",
    "        var v=t[p];",
    "        return typeof v==='function'?v.bind(t):v;",
    "      }",
    "    });",
    "  }catch(_){return e;}",
    "}",
    "var __stageAen=stage.addEventListener.bind(stage),__stageRen=stage.removeEventListener.bind(stage);",
    "stage.addEventListener=function(t,fn,opt){",
    "  if(typeof fn!=='function')return __stageAen(t,fn,opt);",
    "  __live++;",
    "  var wrapped=function(e){return fn.call(this,__wrapEvent(e));};",
    "  __liveListeners.set(fn,wrapped);",
    "  return __stageAen(t,wrapped,opt);",
    "};",
    "stage.removeEventListener=function(t,fn,opt){",
    "  var wrapped=__liveListeners.get(fn);",
    "  if(wrapped){__liveListeners.delete(fn);__live--;return __stageRen(t,wrapped,opt);}",
    "  return __stageRen(t,fn,opt);",
    "};",
    "var __winAen=window.addEventListener.bind(window),__winRen=window.removeEventListener.bind(window);",
    "window.addEventListener=function(t,fn,opt){",
    "  if(typeof fn!=='function')return __winAen(t,fn,opt);",
    "  __live++;",
    "  __liveListeners.set(fn,fn);",
    "  return __winAen(t,fn,opt);",
    "};",
    "window.removeEventListener=function(t,fn,opt){",
    "  if(__liveListeners.get(fn)===fn){__liveListeners.delete(fn);__live--;}",
    "  return __winRen(t,fn,opt);",
    "};",
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

  // 画布快照缓存：canvasId -> {dataUrl, error}（canvas-done/error 时写入，
  // 独活模式下由 canvas-shot-data 实时更新）
  const canvasShots = new Map();
  // 独活模式"截图"请求中标记：canvasId -> true（响应到达后触发导出）
  const pendingCanvasExport = new Map();

  function findCanvasBlock(canvasId) {
    return chatInner.querySelector(
      '.md-canvas-block[data-canvas-id="' + canvasId + '"]');
  }

  // 沙箱消息总线：ready → 下发该块源码；log → 日志实时追加；
  // live → 保持运行态（画布可交互）；done/error → 状态复位 + 快照缓存
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
              zoom: zoomScaleOf(block),
            }, "*");
          }
          return;
        }
      }
      return;
    }
    if (data.type === "canvas-log") {
      const block = findCanvasBlock(data.id || "");
      if (block) appendCanvasLog(block, [String(data.line || "")]);
      return;
    }
    if (data.type === "canvas-live") {
      // 动画/游戏脚本仍在运行：保持运行态（■ 停止），画布可交互
      const block = findCanvasBlock(data.id || "");
      if (block) setCanvasState(block, "running");
      return;
    }
    if (data.type === "canvas-shot-data") {
      // 独活模式实时快照：缓存后若有挂起的导出请求则继续导出
      // （导出的是缓存里的 dataUrl 字符串——此前误传 block DOM 元素，
      // fetch("[object HTMLDivElement]") 直接 "Failed to fetch"）
      const id = data.id || "";
      storeCanvasShot(id, data.shot);
      if (pendingCanvasExport.get(id)) {
        pendingCanvasExport.delete(id);
        const rec = canvasShots.get(id);
        if (rec && rec.dataUrl) {
          exportCanvasShot(rec.dataUrl);
        } else if (rec && rec.error) {
          App.toast(rec.error);
        } else {
          App.toast("快照未就绪，请重试截图");
        }
      }
      return;
    }
    if (data.type === "canvas-done" || data.type === "canvas-error") {
      const block = findCanvasBlock(data.id || "");
      if (!block) return;
      storeCanvasShot(data.id || "", data.shot);
      // 日志已由 canvas-log 逐行实时到达（postMessage 同通道 FIFO 有序），
      // done/error 不再重复追加
      if (data.type === "canvas-error" && data.error) {
        appendCanvasLog(block, ["[异常] " + String(data.error)]);
      }
      // 同步脚本瞬间完成（或独活脚本自然收尾）：状态复位，按钮回到「▶ 运行」
      setCanvasState(block, "idle");
    }
  }
  window.addEventListener("message", onCanvasMessage);

  // 快照缓存写入：只接受 PNG dataURL；污染失败转为可读错误说明
  function storeCanvasShot(id, shotData) {
    const shot = String(shotData || "");
    canvasShots.set(id || "", {
      dataUrl: shot.indexOf("data:image/png") === 0 ? shot : "",
      error: shot.indexOf("data:image/png") === 0 ? "" :
        (shot ? shot.replace(/^ERR:/, "画布被浏览器标记污染，无法导出：") : ""),
    });
  }

  // 日志区：画布下方等宽文本框（textContent 注入，防日志内容注入 HTML）。
  // 行缓冲挂在块元素属性上（流式逐行追加，上限 300 行，超出丢最旧）
  function appendCanvasLog(block, lines) {
    const view = block.querySelector(".md-canvas-view");
    if (!view) return;
    let logBox = view.querySelector(".md-canvas-log");
    if (!logBox) {
      logBox = document.createElement("div");
      logBox.className = "md-canvas-log";
      view.appendChild(logBox);
    }
    const buffer = block.__canvasLogLines || (block.__canvasLogLines = []);
    for (let i = 0; i < lines.length; i++) buffer.push(lines[i]);
    while (buffer.length > 300) buffer.shift();
    logBox.textContent = buffer.length ? buffer.join("\n") : "(无输出)";
    logBox.scrollTop = logBox.scrollHeight;
  }

  function resetCanvasLog(block) {
    block.__canvasLogLines = [];
    const view = block.querySelector(".md-canvas-view");
    const logBox = view && view.querySelector(".md-canvas-log");
    if (logBox) logBox.remove();
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
      resetCanvasLog(block);
      view.innerHTML = "";
      view.appendChild(buildCanvasFrame());
      setCanvasState(block, "running");
      return;
    }

    // 「重置」：任意状态下清空画布回到未运行占位（区别于「停止」：不要求
    // 处于运行态），同时清掉上次运行的截图缓存，防止重置后截到旧图
    if (action === "reset") {
      const frame = block.querySelector(".md-canvas-frame");
      if (frame) frame.remove();
      canvasShots.delete(block.dataset.canvasId || "");
      resetCanvasLog(block);
      setCanvasState(block, "idle");
      const view = block.querySelector(".md-canvas-view");
      if (view) {
        view.innerHTML = '<div class="md-canvas-placeholder">未运行 · 点击「▶ 运行」在沙箱中执行脚本</div>';
      }
      return;
    }

    if (action === "full") {
      // 全屏：作用于整个控件块（含操作按钮，全屏内仍可停止/截图/退出），
      // 沙箱 iframe 随容器铺满，沙箱内 #stage 等比缩放。全屏的是父页元素
      // 而非 iframe 自身，无需给沙箱加 allow-fullscreen
      if (document.fullscreenElement === block) {
        document.exitFullscreen();
        return;
      }
      if (!document.fullscreenEnabled) {
        App.toast("当前环境不支持全屏显示");
        return;
      }
      block.requestFullscreen().catch(function (err) {
        App.toast("进入全屏失败：" + (err && err.message || err));
      });
      return;
    }

    if (action === "shot") {
      // 截图导出：同步绘制用 done 时自报的快照（canvasShots 缓存）；
      // 独活模式（动画/游戏）向沙箱实时索要当前帧（跨源读不到 iframe
      // 像素，截图只能由沙箱内 stage.toDataURL 自报）
      const id = block.dataset.canvasId || "";
      if (block.dataset.canvasState === "running") {
        const frame = block.querySelector(".md-canvas-frame");
        if (!frame || !frame.contentWindow) {
          App.toast("沙箱未在运行，先点「▶ 运行」");
          return;
        }
        pendingCanvasExport.set(id, true);
        frame.contentWindow.postMessage({ type: "canvas-shot", id: id }, "*");
        return;
      }
      const rec = canvasShots.get(id);
      if (!rec || (!rec.dataUrl && !rec.error)) {
        App.toast("尚未运行，先点「▶ 运行」");
        return;
      }
      if (rec.error) { App.toast(rec.error); return; }
      exportCanvasShot(rec.dataUrl);
      return;
    }
  });

  // data URL → Blob：不走 fetch（Chromium 对 URL 有 2MB 上限，大画布的
  // PNG data URL 超限后 fetch 直接抛 "Failed to fetch"；file:// 源下同样
  // 受限），用 atob 手动解码，无长度限制
  function dataUrlToBlob(dataUrl) {
    const comma = dataUrl.indexOf(",");
    const mime = ((/^data:([^;,]*)/.exec(dataUrl.slice(0, comma)) || [])[1]) || "image/png";
    const bin = atob(dataUrl.slice(comma + 1));
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return new Blob([bytes], { type: mime });
  }

  // 快照导出：优先复制到剪贴板，剪贴板不可用/写入失败降级为下载 PNG
  async function exportCanvasShot(dataUrl) {
    let blob;
    try {
      blob = /^data:/i.test(dataUrl) ? dataUrlToBlob(dataUrl) : await (await fetch(dataUrl)).blob();
    } catch (err) {
      App.toast("截图失败：" + (err && err.message || err));
      return;
    }
    if (navigator.clipboard && window.ClipboardItem) {
      try {
        await navigator.clipboard.write([new ClipboardItem({ "image/png": blob })]);
        App.toast("已复制画布截图");
        return;
      } catch (err) {
        // 剪贴板被策略/权限拒绝：走下载兜底，不再直接报错
      }
    }
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "canvas-shot.png";
    link.click();
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
    App.toast("已下载画布截图 PNG");
  }

  // SVG/Mermaid 全屏尺寸同步：图片视图的 svg 带流式渲染注入的内联样式
  // （width:min(100%,170vh*宽高比)，内联优先级高于 CSS 且每图比例不同），
  // 全屏"整图限高完整可见"的宽度必须按各图比例在 JS 里算：
  // 进入全屏 → 改写内联 width = min(块宽, (视口高-96px)*比例)；退出 → 还原。
  // 显示缩放（--md-zoom）生效时基准宽/高上限同乘倍率（放大后容器滚动查看）。
  function syncSvgFullscreenLayout() {
    const fsEl = document.fullscreenElement;
    chatInner.querySelectorAll(".md-svg-block").forEach(function (block) {
      const svg = block.querySelector(".md-svg-view svg");
      if (!svg) return;
      if (fsEl === block) {
        if (block.__svgStyleBackup == null) {
          block.__svgStyleBackup = svg.getAttribute("style") || "";
        }
        const m = /aspect-ratio:\s*([\d.]+)/.exec(svg.getAttribute("style") || "");
        const ar = m ? parseFloat(m[1]) : 0;
        const maxH = Math.max(240, window.innerHeight - 96);
        const zoom = zoomScaleOf(block);
        const w = (ar > 0 ? Math.min(block.clientWidth, maxH * ar) : block.clientWidth) * zoom;
        svg.style.width = Math.floor(w) + "px";
        svg.style.height = "auto";
        svg.style.maxHeight = Math.floor(maxH * zoom) + "px";
      } else if (block.__svgStyleBackup != null) {
        svg.setAttribute("style", block.__svgStyleBackup);
        block.__svgStyleBackup = null;
      }
    });
  }
  // 全屏状态同步按钮文案（Esc 退出 / 多块同时存在时统一刷新）：
  // canvas 与 SVG/Mermaid 控件各有一套 full 按钮，两套委托都在此统一刷新
  document.addEventListener("fullscreenchange", function () {
    syncSvgFullscreenLayout();
    const fullscreen = Boolean(document.fullscreenElement);
    chatInner.querySelectorAll('[data-canvas-action="full"]').forEach(function (btn) {
      btn.textContent = fullscreen ? "⤡ 退出全屏" : "⛶ 全屏";
      btn.title = fullscreen ? "退出全屏（Esc）" : "全屏显示画布（Esc 退出）";
    });
    chatInner.querySelectorAll('.md-svg-block [data-svg-action="full"]').forEach(function (btn) {
      btn.textContent = fullscreen ? "⤡ 退出全屏" : "⛶ 全屏";
      btn.title = fullscreen ? "退出全屏（Esc）" : "全屏显示（Esc 退出）";
    });
  });
  // 全屏期间窗口尺寸变化：重算当前全屏块的图片尺寸
  window.addEventListener("resize", function () {
    const fsEl = document.fullscreenElement;
    if (fsEl && fsEl.classList && fsEl.classList.contains("md-svg-block")) {
      syncSvgFullscreenLayout();
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
    if (!id || (state !== "pending" && state !== "rerender") || mermaidPending.has(id)) return;
    const holder = widget.querySelector('[data-mermaid-holder="' + id + '"]');
    if (!holder) return;
    const code = Markdown.unescapeHtml(widget.dataset.svgCode || "");
    if (!code.trim()) return;
    widget.dataset.mermaidState = "rendering";
    holder.classList.remove("md-mermaid-error");
    // 渲染参数：换用当前控件 id 调 mermaid.render（流式重建后同图已换新 id，
    // 沿用旧临时节点 id 可能撞上上次的孤儿节点导致渲染不出）；源码原文做
    // 缓存 key——同 code 的重建命中缓存直接复用上次 SVG，不再重复 render。
    const opts = { renderId: id, cachedCode: code };
    const attempt = function (isRetry) {
      const p = Markdown.renderMermaidInto(id, code, holder, opts)
        .then(function () {
          widget.dataset.mermaidState = "done";
          mermaidPending.delete(id);
        })
        .catch(function () {
          mermaidPending.delete(id);
          // 失败自动重试一次（mermaid.render 偶发瞬时失败，重试常能成功）；
          // 重试仍失败才落 failed 态，交给「↻ 重渲染」按钮手动恢复
          if (!isRetry && widget.isConnected &&
            widget.dataset.mermaidState === "rendering") {
            return attempt(true);
          }
          widget.dataset.mermaidState = "failed";
        });
      mermaidPending.set(id, p);
      return p;
    };
    attempt(false);
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

  // 流结束兜底：finish() 里清掉仍处于渲染中的控件状态残留。
  // 竞态修复：只处理 promise 不在飞且无 svg 的孤儿控件——流结束的瞬间
  // 渲染 promise 往往仍在异步执行（懒加载/解析耗时），直接强置 failed
  // 会误杀几秒后正常完成的渲染（表现：提示失败但刷新后正常）。
  function finalizeMermaidBlocks() {
    chatInner.querySelectorAll('.md-mermaid-block[data-mermaid-state="rendering"]').forEach(function (widget) {
      if (mermaidPending.has(widget.dataset.mermaidId)) return; // 在飞：等回填/重试收尾
      const holder = widget.querySelector("[data-mermaid-holder]");
      if (holder && holder.querySelector("svg")) {
        widget.dataset.mermaidState = "done"; // 已有结果：仅纠正状态标记
        return;
      }
      widget.dataset.mermaidState = "failed";
      if (holder && !holder.classList.contains("md-mermaid-error")) {
        holder.classList.add("md-mermaid-error");
        holder.textContent = "Mermaid 渲染未完成（流式被中断），点「↻ 重渲染」恢复";
      }
    });
    // pending（未开始渲染）的控件：正常情况下流结束后 MutationObserver 会补
    // 渲染，但若最终 DOM 重建早于观察回调则可能永远停留——这里补一枪。
    chatInner.querySelectorAll('.md-mermaid-block[data-mermaid-state="pending"]').forEach(function (widget) {
      scheduleMermaidRender(widget);
    });
  }

  // 手动重渲染（↻ 重渲染按钮）：失败态或显示异常时重建渲染
  function rerenderMermaid(block) {
    if (!block) return;
    const id = block.dataset.mermaidId;
    if (!id) return;
    const holder = block.querySelector('[data-mermaid-holder="' + id + '"]');
    if (!holder) return;
    // 去掉上次失败文案，回占位态后按 pending 重新调度（若 promise 在飞则跳过）
    holder.classList.remove("md-mermaid-error");
    if (!holder.querySelector("svg")) {
      holder.innerHTML = "Mermaid 渲染中…";
    }
    block.dataset.mermaidState = "pending";
    scheduleMermaidRender(block);
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

    const total = qnavUsers.length;
    // 指示条均匀采样：问题数超过上限时不再截断（此前超过 40 条的完全不显示），
    // 而是把全部问题均匀映射到 QNAV_MAX_DASHES 条指示条上——
    // 第 j 条负责问题段 [floor(j*n/D), floor((j+1)*n/D))，点击跳到段首问题；
    // 问题数不超过上限时一一对应，行为与原先完全一致。面板列表仍列出全部问题。
    const dashCount = Math.min(QNAV_MAX_DASHES, total);
    for (let j = 0; j < dashCount; j++) {
      const start = Math.floor(j * total / dashCount);
      const end = Math.floor((j + 1) * total / dashCount);
      const bubble = qnavUsers[start].querySelector(".msg-bubble");
      let text = bubble ? bubble.textContent : "问题 " + (start + 1);
      // 一条指示条对应多个问题时，标题标注问题序号区间
      if (end - start > 1) text = (start + 1) + "-" + end + ". " + text;
      const dash = el("button", "qnav-dash");
      dash.title = text;
      dash.addEventListener("click", function () { jumpToQuestion(start); });
      qnavRail.appendChild(dash);
    }

    qnavUsers.forEach(function (userEl, i) {
      const bubble = userEl.querySelector(".msg-bubble");
      const text = bubble ? bubble.textContent : "问题 " + (i + 1);
      const item = el("button", "qnav-item", i + 1 + ". " + text);
      item.addEventListener("click", function () { jumpToQuestion(i); });
      qnavPanel.appendChild(item);
    });
    updateQnavActive();
  }

  // ---------- 从用户引用卡片定位到原文 ----------
  let highlightedQuoteNode = null;
  let quoteHighlightTimer = null;

  function quoteMatchText(value) {
    return String(value == null ? "" : value)
      .replace(/\u00a0/g, " ")
      .replace(/\s+/g, " ")
      .trim();
  }

  function quoteRecordRoleMatches(record, role) {
    return record && (role === "user" ? record.kind === "user"
      : role === "assistant" ? record.kind === "assistant"
        : record.kind === "user" || record.kind === "assistant");
  }

  function quoteRecordText(record) {
    return quoteMatchText(contentToPlainText(record && record.content));
  }

  function findQuoteRecordIndex(records, source, quoteText) {
    if (!Array.isArray(records)) return -1;
    const role = source && source.role;
    const round = Number(source && source.round);
    const eventIndex = Number(source && source.event_index);
    const wanted = quoteMatchText(quoteText);
    const candidates = [];
    records.forEach(function (record, index) {
      if (quoteRecordRoleMatches(record, role)) candidates.push({ record: record, index: index });
    });
    if (!candidates.length) return -1;

    if (Number.isInteger(round) && round > 0 && Number.isInteger(eventIndex) && eventIndex >= 0) {
      const exact = candidates.find(function (item) {
        return Number(item.record.round) === round
          && Number(item.record.source_event_index) === eventIndex;
      });
      if (exact && (!wanted || quoteRecordText(exact.record).includes(wanted))) return exact.index;
    }

    const sameRound = Number.isInteger(round) && round > 0
      ? candidates.filter(function (item) { return Number(item.record.round) === round; })
      : [];
    function findText(items) {
      if (!wanted) return null;
      return items.find(function (item) { return quoteRecordText(item.record).includes(wanted); }) || null;
    }
    const matching = findText(sameRound) || findText(candidates);
    return matching ? matching.index : -1;
  }

  function findRenderedQuoteSource(source, quoteText, allowUnscoped) {
    const role = source && source.role;
    const selector = role === "user" ? ".msg-user-text"
      : role === "assistant" ? ".msg-assistant-body"
        : ".msg-user-text, .msg-assistant-body";
    const candidates = Array.from(chatInner.querySelectorAll(selector));
    if (!candidates.length) return null;

    const round = Number(source && source.round);
    const eventIndex = Number(source && source.event_index);
    const wanted = quoteMatchText(quoteText);
    const sameRound = Number.isInteger(round) && round > 0
      ? candidates.filter(function (node) {
        const host = node.closest("[data-round]");
        return host && Number(host.getAttribute("data-round")) === round;
      })
      : [];
    if (Number.isInteger(round) && round > 0 && !sameRound.length && !allowUnscoped) return null;
    const scoped = sameRound.length ? sameRound : candidates;

    if (Number.isInteger(eventIndex) && eventIndex >= 0) {
      const exact = scoped.find(function (node) {
        const host = node.closest("[data-source-event]");
        return host && Number(host.getAttribute("data-source-event")) === eventIndex;
      });
      if (exact && (!wanted || quoteMatchText(exact.innerText || exact.textContent).includes(wanted))) {
        return exact;
      }
    }

    if (!wanted) return null;
    const matching = function (items) {
      return items.find(function (node) {
        return quoteMatchText(node.innerText || node.textContent).includes(wanted);
      }) || null;
    };
    return matching(scoped) || (allowUnscoped && scoped !== candidates ? matching(candidates) : null);
  }

  function normalizedTextOffsets(rawText) {
    let text = "";
    const starts = [];
    const ends = [];
    for (let index = 0; index < rawText.length;) {
      if (/\s|\u00a0/.test(rawText[index])) {
        const start = index;
        while (index < rawText.length && (/\s|\u00a0/.test(rawText[index]))) index += 1;
        if (text && text[text.length - 1] !== " ") {
          text += " ";
          starts.push(start);
          ends.push(index);
        }
        continue;
      }
      text += rawText[index];
      starts.push(index);
      ends.push(index + 1);
      index += 1;
    }
    if (text.endsWith(" ")) {
      text = text.slice(0, -1);
      starts.pop();
      ends.pop();
    }
    return { text: text, starts: starts, ends: ends };
  }

  // 尽量把滚动锚点落在引用文字起始处；跨 Markdown 内联节点时，
  // 逐步缩短匹配前缀，仍可定位到引用所在的同一句/段落。
  function quoteTextAnchorRect(target, quoteText) {
    const wanted = quoteMatchText(quoteText);
    if (!wanted || typeof document.createTreeWalker !== "function") return null;
    const walker = document.createTreeWalker(target, NodeFilter.SHOW_TEXT);
    const nodes = [];
    let node;
    while ((node = walker.nextNode())) {
      if (node.nodeValue && node.nodeValue.trim()) {
        nodes.push({ node: node, mapped: normalizedTextOffsets(node.nodeValue) });
      }
    }
    const minLength = Math.min(8, wanted.length);
    for (let length = Math.min(32, wanted.length); length >= minLength; length -= 1) {
      const prefix = wanted.slice(0, length);
      for (let i = 0; i < nodes.length; i += 1) {
        const item = nodes[i];
        const at = item.mapped.text.indexOf(prefix);
        if (at < 0) continue;
        const range = document.createRange();
        range.setStart(item.node, item.mapped.starts[at]);
        range.setEnd(item.node, item.mapped.ends[at + prefix.length - 1]);
        const rect = range.getBoundingClientRect();
        if (rect && (rect.width || rect.height)) return rect;
      }
    }
    return null;
  }

  function revealQuoteSource(target, quoteText) {
    if (quoteHighlightTimer != null) window.clearTimeout(quoteHighlightTimer);
    if (highlightedQuoteNode) highlightedQuoteNode.classList.remove("quote-source-highlight");
    highlightedQuoteNode = target;
    target.classList.remove("quote-source-highlight");
    // 重复点击同一引用时重播一次闪烁动画。
    void target.offsetWidth;
    target.classList.add("quote-source-highlight");
    quoteHighlightTimer = window.setTimeout(function () {
      if (highlightedQuoteNode) highlightedQuoteNode.classList.remove("quote-source-highlight");
      highlightedQuoteNode = null;
      quoteHighlightTimer = null;
    }, 1900);

    const targetRect = quoteTextAnchorRect(target, quoteText) || target.getBoundingClientRect();
    const scrollRect = chatScroll.getBoundingClientRect();
    const top = chatScroll.scrollTop + targetRect.top - scrollRect.top
      - Math.max(0, (chatScroll.clientHeight - targetRect.height) / 2);
    chatScroll.scrollTo({ top: Math.max(0, top), behavior: "smooth" });
  }

  async function jumpToQuoteSource(source, quoteText) {
    const locator = source && typeof source === "object" ? source : {};
    const sourceSession = typeof locator.session_id === "string" ? locator.session_id : "";
    if (sourceSession && state.sessionId !== sourceSession) {
      if (typeof App.openSession !== "function") {
        toast("无法打开引用所属的会话");
        return;
      }
      try {
        await App.openSession(sourceSession);
      } catch (_) {
        toast("无法打开引用所属的会话");
        return;
      }
      if (state.sessionId !== sourceSession) {
        toast("引用所属的会话未能打开");
        return;
      }
    }

    const records = loadOlderState.records;
    const recordIndex = findQuoteRecordIndex(records, locator, quoteText);
    let target = findRenderedQuoteSource(locator, quoteText, false);
    let renderLocator = locator;
    if (!target && records && recordIndex >= 0) {
      const sourceRecord = records[recordIndex];
      renderLocator = Object.assign({}, locator, { round: sourceRecord.round });
      if (Number.isInteger(sourceRecord.source_event_index)) {
        renderLocator.event_index = sourceRecord.source_event_index;
      } else {
        delete renderLocator.event_index;
      }
      target = findRenderedQuoteSource(renderLocator, quoteText, false);
    }
    if (!target && records && recordIndex >= 0) {
      while (loadOlderState.records === records && recordIndex < loadOlderState.start) {
        const previousStart = loadOlderState.start;
        if (loadOlderState.loading) {
          await new Promise(function (resolve) { window.setTimeout(resolve, 20); });
        } else {
          await runOlderBatch();
        }
        if (loadOlderState.start === previousStart && !loadOlderState.loading) break;
        target = findRenderedQuoteSource(renderLocator, quoteText, false);
        if (target) break;
      }
      if (!target && loadOlderState.records === records) {
        target = findRenderedQuoteSource(renderLocator, quoteText, false);
      }
    }
    if (!target && recordIndex < 0) target = findRenderedQuoteSource(locator, quoteText, true);

    if (!target) {
      toast("未找到引用原文，可能已被编辑或删除");
      return;
    }
    revealQuoteSource(target, quoteText);
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
    // 高亮当前问题所属的采样段：与 rebuildQnav 同口径计算每段起点，
    // 找最大的 start_j ≤ current 即当前段；一一对应时直接用下标
    const dashes = qnavRail.querySelectorAll(".qnav-dash");
    let activeDash = -1;
    if (dashes.length && dashes.length === qnavUsers.length) {
      activeDash = current;
    } else {
      for (let j = dashes.length - 1; j >= 0; j--) {
        if (Math.floor(j * qnavUsers.length / dashes.length) <= current) {
          activeDash = j;
          break;
        }
      }
    }
    dashes.forEach(function (d, i) {
      d.classList.toggle("active", i === activeDash);
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
    const buttons = chatInner.querySelectorAll(".codeblock .copy-btn");
    // 读写分离：先对全部按钮一口气收集 rect（阶段一，纯读不触发写回布局），
    // 再统一计算并写 class/样式（阶段二，纯写）。避免此前「逐个 rect 读 →
    // 写 class → 下一个再读」交替造成的强制同步布局抖动（长会话滚动卡顿主因）
    const rects = new Array(buttons.length);
    for (let i = 0; i < buttons.length; i++) {
      rects[i] = buttons[i].closest(".codeblock").getBoundingClientRect();
    }
    for (let i = 0; i < buttons.length; i++) {
      const btn = buttons[i];
      const blockRect = rects[i];
      const btnH = btn.offsetHeight || 28;
      // 代码块在视口内的可见高度（含下边框可见部分）
      const visible = Math.min(blockRect.bottom, scrollRect.bottom) - Math.max(blockRect.top, scrollRect.top);
      if (visible < btnH) {
        btn.classList.add("is-hidden");
        continue;
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
    }
  }

  scrollBottomBtn.addEventListener("click", scrollToBottom);


  // ---------- 导出（供其它模块经 App.* 调用） ----------
  App.finalizeMermaidBlocks = finalizeMermaidBlocks;
  App.renderRecords = renderRecords;
  App.appendTime = appendTime;
  App.buildCompactionBlock = buildCompactionBlock;
  App.highlightCodeBlocks = highlightCodeBlocks;
  App.appendUserMessage = appendUserMessage;
  App.attachUserEditAction = attachUserEditAction;
  App.exitUserMessageEdit = exitUserMessageEdit;
  App.attachLiveRoundEntry = attachLiveRoundEntry;
  // confirmEditResend 定义在 chat.js（发送编排）并由其导出，此处不重复赋值：
  // 若引用本模块不存在的函数名会在加载期抛 ReferenceError 中断整个 IIFE，
  // 导致后续所有 App.* 导出丢失（历史加载报 xxx is not a function）
  App.cancelAnyUserEdit = function () {
    // Esc 等全局入口：取消当前会话所有处于编辑态的用户消息。
    // 有未发送修改时走面板内确认（panel._requestCancel，与取消按钮同规则），
    // 无修改直接退出不打扰
    document.querySelectorAll(".msg.msg-user.is-editing").forEach(function (msg) {
      const panel = msg.querySelector(".msg-edit-panel");
      if (panel && typeof panel._requestCancel === "function") {
        panel._requestCancel();
        return;
      }
      exitUserMessageEdit(msg);
    });
  };
  App.buildThinkBlock = buildThinkBlock;
  App.buildToolBlock = buildToolBlock;
  App.applyToolResult = applyToolResult;
  App.buildSubAgentBlock = buildSubAgentBlock;
  App.rebuildQnav = rebuildQnav;
  App.jumpToQuoteSource = jumpToQuoteSource;
  App.updateCodeblockCopyButtons = updateCodeblockCopyButtons;
  App.renderPreservingWidgets = renderPreservingWidgets;
})(window.App);
