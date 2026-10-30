/**
 * 聊天流：发送 / 停止 / 后台流附接
 * - 经 stream_pipeline.js 渲染 SSE，task_status.js 独立维护任务阶段
 * - 发送（附件上传、多模态部件、会话惰性建号）、停止、断线重连续看、标题刷新
 * 依赖：app/core.js、API、Markdown、FormatUtils、SessionUtils；App.*：多个模块
 */
(function (App) {
  "use strict";
  const {
    state, el, toast, input,
    chatInner, sessionList, scrollToBottom, nearBottom, stickToBottom,
    setEmpty, sendBtn, stopBtn, resumeBtn
  } = App;

  // ---------- 发送 / 停止 ----------

  /**
   * 构造多模态消息内容部件：纯文本保持字符串；带附件时为 OpenAI 兼容部件列表。
   * pendingMedia 为 [{file,name,kind,media_ref...}] 快照（须已上传，含 media_ref）。
   */
  function buildUserContent(text, uploadedMedia) {
    const contentParts = [];
    if (text) contentParts.push({ type: "text", text: text });
    (uploadedMedia || []).forEach(function (m) {
      if (m.kind === "image") {
        contentParts.push({ type: "image_url", image_url: { url: m.media_ref } });
      } else if (m.kind === "audio") {
        contentParts.push({
          type: "input_audio",
          input_audio: { data: m.media_ref, format: (m.stored_name.split(".").pop() || "").toLowerCase() },
        });
      } else if (m.kind === "video") {
        contentParts.push({ type: "video_url", video_url: { url: m.media_ref } });
      }
    });
    return contentParts.length === 1 && contentParts[0].type === "text" ? text : contentParts;
  }

  /**
   * 上传媒体附件到 history_files/session_files/<session>/media/，换回 media:// 引用；
   * 后端发送上游前会把引用解析为 data URL / base64（兼容 Chat Completions 格式）
   */
  async function uploadMediaFiles(streamSessionId, items, signal) {
    const uploadedMedia = [];
    const uploadRes = await API.uploadSessionMedia(
      streamSessionId,
      items.map(function (item) { return item.file; }),
      function (index, progress) {
        Object.assign(items[index], progress);
        if (state.sessionId === streamSessionId) App.renderComposerAttachments();
      },
      signal
    );
    (uploadRes.results || []).forEach(function (result, index) {
      const item = items[index];
      if (result.status === "success") {
        if (item) Object.assign(item, result);
        uploadedMedia.push(result);
      } else {
        if (item) item.uploadError = result.message || "未知原因";
        toast("附件上传失败：" + result.filename + "：" + (result.message || "未知原因"));
      }
    });
    return uploadedMedia;
  }

  // 引导与普通发送复用媒体上传和 content 构造，上传成功后仅投递 media:// 引用。
  App.prepareSteerContent = async function (message) {
    const media = message.media || [];
    const needUpload = media.filter(function (m) { return m && !m.media_ref && m.file; });
    if (needUpload.length) await uploadMediaFiles(message.sessionId, needUpload);
    if (media.some(function (m) { return !m || !m.media_ref; })) {
      throw new Error("部分附件上传失败，请重试；已上传的附件不会重复上传");
    }
    return {
      content: buildUserContent(message.text || "", media),
      quotes: QuoteUtils.quotesForRequest(message.quotes || []),
    };
  };

  // V2 文件版本链徽标刷新（防抖）：文件工具结果到达后延迟拉一次统计。
  // 仅文件写型工具才可能产生版本链变更（版本链 tool 角色=write_file/edit_file/
  // sub_agent.*）——搜索/命令/问答等工具结果不再触发 /file_diff/list 请求，
  // 避免多工具任务期间每个工具结果一次接口调用的风暴
  let fileChangesTimer = null;
  const FILE_BADGE_TOOLS = ["write_file", "edit_file", "apply_patch", "sub_agent"];
  function scheduleFileChangesBadgeRefresh(sessionId) {
    if (state.sessionId !== sessionId) return;
    if (fileChangesTimer) clearTimeout(fileChangesTimer);
    fileChangesTimer = setTimeout(function () {
      fileChangesTimer = null;
      if (App.fileHistory && typeof App.fileHistory.refreshBadge === "function") {
        // 工具结果是数据变更源：force 不受 refreshBadge 的节流窗口限制
        App.fileHistory.refreshBadge(undefined, true);
      }
    }, 400);
  }

  /**
   * 构建一条 SSE 流式事件的 DOM 渲染管线（思考/回答/工具/usage 分块处理）。
   * send() 与 attachStreamSession() 共用，保证刷新重连后渲染行为一致。
   */
  function createStreamPipeline(msg, sessionId, activeStream) {
    return App.createStreamPipeline(msg, sessionId, activeStream, {
      pickTitlePreview, requestSessionTitle, scheduleFileChangesBadgeRefresh,
      titlePreviewChars: TITLE_PREVIEW_CHARS, fileBadgeTools: FILE_BADGE_TOOLS,
    });
  }

  // 流错误的展示文案：网络层中断（无 HTTP 状态码）大概率是后端进程退出/
  // 断网，明确提示用户检查后端，而不是只显示裸英文错误。
  function streamErrorText(err) {
    const message = String((err && err.message) || err || "未知错误");
    if (err && err.name !== "AbortError" && err.status == null) {
      return "网络连接中断，后端服务可能已停止（" + message + "）";
    }
    return message;
  }

  // 流异常中断后的即时确认：不再停留在“连接已中断”要求手动处理，
  // 而是立即向后端查询一次；后端已无任务则收尾为已结束并重载对齐，
  // 用户可直接继续发送。后端仍不可用时保持原状，发送时再自动确认。
  async function reconcileStreamInterruption(sessionId) {
    if (!App.taskStatus || typeof App.taskStatus.reconcileUncertain !== "function") return;
    if (!App.taskStatus.isUncertain(sessionId)) return;
    try {
      const resolved = await App.taskStatus.reconcileUncertain(sessionId);
      if (resolved && state.sessionId === sessionId) {
        App.refreshComposerButtons();
        setTimeout(function () { App.openSession(sessionId); }, 0);
      }
    } catch (_) { /* 后端仍不可用：保持“连接已中断”，发送时再自动确认 */ }
  }

  // 发送前的不确定态处理：后端中断/重启后旧任务已消失时自动确认并收尾，
  // 返回 true=可继续发送，false=已拦截（重连中/后端不可用，输入保留）。
  async function ensureSendable() {
    if (!App.taskStatus || !App.taskStatus.isUncertain(state.sessionId)) return true;
    if (typeof App.taskStatus.reconcileUncertain !== "function") {
      toast("请先重新连接，确认当前任务状态后再发送新消息"); return false;
    }
    try {
      const resolved = await App.taskStatus.reconcileUncertain(state.sessionId);
      if (!resolved) {
        // 任务仍在后台运行：自动重连续看，输入保留，等待完成或用引导/队列。
        toast("检测到后台任务仍在运行，已重新连接；请等待完成或使用引导/队列");
        await App.attachStreamSession(state.sessionId);
        return false;
      }
      await App.openSession(state.sessionId);
      return true;
    } catch (error) {
      if (error && error.name === "AbortError") return false;
      toast("后端服务不可用，请确认后端已启动后重试");
      return false;
    }
  }

  /**
   * 发送一条用户消息并开启新一轮回复。
   * @param {object} [direct] 直接载荷（引导/队列 flush/编辑重发调用）：
   *   { text, media, docs, quotes, targetRound?, insertAfterRound?, strictInsertContext? } —— 编辑态
   *   media/docs 为待上传附件快照；缺省时从输入框读取文本、媒体和引用。
   */
  async function send(direct) {
    // 防御：事件监听器直接传引用时，MouseEvent 等非普通对象不算直接载荷
    if (direct && (typeof direct !== "object" || direct instanceof Event)) direct = undefined;
    const fromInput = !direct;
    // 先冻结当前语音预览，再取发送快照；旧识别回调不能写回已清空的输入框。
    if (fromInput && App.stopVoiceInput) App.stopVoiceInput();
    const text = direct ? (direct.text || "") : input.value.trim();
    const pendingMedia = direct ? (direct.media || []).slice() : state.pendingMedia.slice();
    const pendingDocs = direct
      ? (direct.docs || []).slice()
      : (state.pendingDocs || []).filter(function (item) {
        return item.staged && item.sessionId === state.sessionId;
      });
    // 引用快照（选中文本引用到提问）：与消息一起上送，不作为独立用户消息
    const pendingQuotes = direct
      ? (direct.quotes || []).slice()
      : (state.pendingQuotes || []).slice();
    // 有附件时允许无文本发送；引用本身不构成提问（仍要求正文或附件）
    if (!text && !pendingMedia.length) return;
    // 仅当“当前会话”正在流式时禁止发送；其他会话在后台流式不影响本会话发送
    if (state.streaming && state.streamingSession === state.sessionId) return;
    // “连接已中断”不确定态：发送前先向后端确认，后端无任务则自动收尾并
    // 重载对齐后直接继续发送，不必手动停止；仍在运行则自动重连（输入保留）。
    if (!(await ensureSendable())) return;
    // 手动压缩进行中：仅压缩会话禁止发送（发送会打断压缩任务并交错写入会话历史）
    if (state.manualCompactRunning && state.manualCompactSession === state.sessionId) {
      toast("正在压缩对话上下文，请稍后再发送");
      return;
    }
    // 其他会话的流仍在监听：先放弃本地监听（其后端任务继续在后台生成，可随时切回续看）
    if (state.streaming) detachAttachedStream();

    // 首次发送才分配会话 ID（"新对话"后一直处于待开始状态）
    const streamSessionId = App.ensureSessionId();
    // 先置流式态再上传附件：上传期间同样锁住发送入口
    state.streaming = true;
    state.streamingSession = streamSessionId;
    state.abort = new AbortController();
    state.hasConversation = true;
    state.importedHistoryText = null;
    App.updateExportButton();
    setEmpty(false);
    App.refreshComposerButtons();

    // 上传媒体附件（输入框路径用 state.pendingMedia 的 file；flush 路径同构；
    // 编辑重发路径的元素已带 media_ref/stored_name 无 file —— 直接复用原引用，不重新上传）
    const needUploadMedia = [];
    pendingMedia.forEach(function (m) {
      if (m && !m.media_ref && m.file) needUploadMedia.push(m);
    });

    function resetPreflightState() {
      state.streaming = false;
      state.streamingSession = null;
      state.abort = null;
      App.refreshComposerButtons();
    }

    function queueDirectRetry() {
      if (fromInput) return;
      state.pendingQueue.unshift({
        text: text,
        media: pendingMedia,
        docs: pendingDocs,
        quotes: pendingQuotes,
        targetRound: direct && direct.targetRound,
        insertAfterRound: direct && direct.insertAfterRound,
        strictInsertContext: direct && direct.strictInsertContext,
        sessionId: streamSessionId,
      });
      App.renderPendingOutbox();
    }

    let failedMedia = [];
    if (needUploadMedia.length) {
      try {
        await uploadMediaFiles(streamSessionId, needUploadMedia, state.abort.signal);
      } catch (err) {
        toast("附件上传失败：" + err.message);
        resetPreflightState();
        queueDirectRetry();
        return;
      }
      // 编辑重发/队列消息中可能有部分文件成功、部分失败；保存成功文件引用，
      // 只把失败文件放回重试队列，避免重复上传已成功的文件。
      failedMedia = needUploadMedia.filter(function (item) { return !item.media_ref; });
      if (!fromInput && failedMedia.length) {
        failedMedia.forEach(function (item) {
          toast("附件上传失败：" + (item.name || item.file.name));
        });
        resetPreflightState();
        queueDirectRetry();
        return;
      }
    }

    // 按编辑框中的附件顺序组装，避免新增文件被挪到旧附件末尾。
    const uploadedMedia = pendingMedia.filter(function (item) { return item && item.media_ref; });

    // 历史编辑框中的新文档在用户点击发送前只保存在浏览器内存；
    // 确认发送后才进入会话 file_memory（解析内容对本会话后续轮次共享）。
    if (pendingDocs.length) {
      const needUploadDocs = pendingDocs.filter(function (item) { return !item.uploaded; });
      try {
        const docRes = needUploadDocs.length
          ? await API.uploadSessionFiles(
            streamSessionId,
            needUploadDocs.map(function (item) { return item.file; }),
            function () {},
            state.abort.signal
          )
          : { success: 0, failed: 0, results: [] };
        (docRes.results || []).forEach(function (result, index) {
          if (result.status === "success" && needUploadDocs[index]) {
            needUploadDocs[index].uploaded = true;
          }
        });
        const failedDocs = (docRes.results || []).filter(function (item) {
          return item.status !== "success";
        });
        if (docRes.success) {
          if (typeof App.loadSessionFiles === "function") {
            await App.loadSessionFiles(streamSessionId);
          }
        }
        if (failedDocs.length) {
          failedDocs.slice(0, 3).forEach(function (item) {
            toast("文档上传失败：" + (item.filename || "文件") + "：" + (item.message || "解析失败"));
          });
          if (!fromInput) {
            resetPreflightState();
            queueDirectRetry();
            return;
          }
        }
        if (fromInput) {
          const uploadedStagedDocs = pendingDocs.filter(function (item) { return item.uploaded; });
          if (uploadedStagedDocs.length) {
            state.pendingDocs = state.pendingDocs.filter(function (item) {
              return uploadedStagedDocs.indexOf(item) < 0;
            });
            App.renderComposerAttachments();
          }
          if (failedDocs.length) {
            resetPreflightState();
            return;
          }
        }
      } catch (err) {
        toast("文档上传失败：" + err.message);
        resetPreflightState();
        queueDirectRetry();
        return;
      }
    }

    // 输入框暂存文档在此时已经上传完毕；不再保留待发副本。
    if (fromInput && pendingDocs.length) {
      state.pendingDocs = state.pendingDocs.filter(function (item) {
        return pendingDocs.indexOf(item) < 0;
      });
      App.renderComposerAttachments();
    }

    // 预检上传成功后释放待发媒体的本地预览 URL；点击取消或重试之前都保留。
    if (!fromInput) {
      pendingMedia.forEach(function (item) {
        if (!item.objUrl) return;
        try { URL.revokeObjectURL(item.objUrl); } catch (_) { /* ignore */ }
        item.objUrl = "";
      });
    }

    const userContent = buildUserContent(text, uploadedMedia);
    // 请求用引用快照（去掉本地 UI 字段 id）：无引用时为空数组，不上送该字段
    const requestQuotes = QuoteUtils.quotesForRequest(pendingQuotes);

    const userNode = App.appendUserMessage(
      userContent, null, streamSessionId, state.sessionDocs, document.createDocumentFragment(), null, requestQuotes
    );
    App.upsertLocalSession(streamSessionId, { userText: text || (uploadedMedia.length ? "[图片/附件]" : text) });
    // 问题导航重建已移到下方 DOM 手术（原地重发删旧轮+原位插入）之后：
    // 若在手术前重建，旧第 N 轮节点仍在（旧文本占第 N 位），新气泡又临时
    // 挂在末尾（新文本占第 total+1 位），导航面板会出现新旧两条重复条目
    if (fromInput) {
      // 若媒体 API 逐文件返回部分失败，保留失败文件供下一条消息重试；
      // 已成功文件已换成 media:// 并进入本轮，不留在输入区重复发送。
      failedMedia = pendingMedia.filter(function (item) { return item.file && !item.media_ref; });
      if (failedMedia.length) {
        pendingMedia.filter(function (item) { return item.media_ref && item.objUrl; })
          .forEach(function (item) {
            try { URL.revokeObjectURL(item.objUrl); } catch (_) { /* ignore */ }
            item.objUrl = "";
          });
        state.pendingMedia = failedMedia;
        App.renderComposerAttachments();
      } else {
        App.clearPendingMedia();
      }
      // 引用随输入框一起清空（快照已进本轮消息，失败重发由气泡上的编辑入口承担）
      App.clearPendingQuotes();
      input.value = "";
      if (App.closeSlashCommands) App.closeSlashCommands();
    }
    App.autosize();
    App.refreshComposerButtons();
    // 只做一次首轮刷新，让刚提交的用户消息尽快出现在上下文统计中；
    // 后续更新由 SSE usage / 压缩完成事件驱动，不再持续轮询。
    App.refreshContextTokenStats(streamSessionId);

    // 本轮回复容器：思考/回答/工具按到达顺序分块
    const msg = el("div", "msg");
    // insertedMidway：原地重发/插入重答把新节点放在会话中部（非末尾追加）时为真，
    // 用于滚动分叉（停在发起位置而非贴底）；
    // liveRound：本轮在历史中的最终轮次号（重发=第 N 轮，插入=第 N+1 轮），
    // 流式节点补打 data-round 用；普通发送为 null（收尾重载后随历史回放打标）
    let insertedMidway = false;
    const liveRound = direct && direct.targetRound != null ? direct.targetRound
      : direct && direct.insertAfterRound != null ? direct.insertAfterRound + 1
      : null;
    // 先登记新流，再挂载和滚动。置底可能同步切换历史窗口，窗口恢复必须
    // 持有本次回复节点，不能仍指向上一轮或等到节点被移除之后才登记。
    const activeStream = {
      sessionId: streamSessionId,
      userText: text,
      replayComplete: true,
      round: liveRound,
      userContent: userContent,
      questionParts: Array.isArray(userContent) ? userContent : null,
      quotes: requestQuotes,
      userNode: userNode,
      messageNode: msg,
      completed: false,
      usage: null,
    };
    state.activeStream = activeStream;
    if (App.taskStatus) activeStream.taskHandle = App.taskStatus.begin(streamSessionId);
    // 编辑重发（重新生成该轮）：屏幕锚点插入——删除旧第 N 轮的屏幕节点，
    // 新回复插入其原位（第 N+1 轮首节点之前），保持与历史结构一致；
    // ask_user 卡片再答（insertAfterRound）：新回复插入第 N+1 轮首节点之前
    // （即原第 N 轮之后），后续轮次节点整体后推——与后端插入收尾一一对应；
    // 普通发送直接追加末尾
    if (direct && direct.targetRound) {
      const oldRound = String(direct.targetRound);
      const firstNext = chatInner.querySelector('[data-round="' + (direct.targetRound + 1) + '"]');
      let removed = false;
      let sweepDone = false;
      // 旧轮节点可能被中间压缩等无 data-round 的节点分隔，统一遍历删除；
      // 命中下一轮已标记节点即停止——旧轮区间之后的无标记节点（后续轮次的
      // 时间分隔条等）不属于旧轮，不能清
      Array.from(chatInner.childNodes).forEach(function (node) {
        if (node.nodeType !== 1) return;
        if (node.getAttribute("data-round") === oldRound) {
          node.remove();
          removed = true;
          return;
        }
        if (!removed || sweepDone) return;
        if (node.getAttribute("data-round")) {
          sweepDone = true;
          return;
        }
        // 旧轮区间内的时间戳等无标记节点一并清掉（停止于旧轮边界）
        if (node.classList && (node.classList.contains("msg-time") || node.classList.contains("round-usage"))) {
          node.remove();
        }
      });
      if (firstNext && firstNext.parentNode === chatInner) {
        chatInner.insertBefore(userNode, firstNext);
        chatInner.insertBefore(msg, firstNext);
        insertedMidway = true;
      } else {
        // 找不到下一轮锚点（编辑的是最后一轮）：直接追加
        chatInner.appendChild(userNode);
        chatInner.appendChild(msg);
      }
    } else if (direct && direct.insertAfterRound != null) {
      // 回答轮插入：锚点为旧编号第 N+1 轮首节点（其后所有轮次在屏幕上自然后推）。
      // 先取锚点引用再重编号：后端插入语义会把后续轮次编号整体 +1，屏幕节点
      // data-round 同步重编，否则新节点补打 data-round=N+1 后与旧编号碰撞
      // （编辑/删除按编号定位会错轮）
      const firstNext = chatInner.querySelector('[data-round="' + (direct.insertAfterRound + 1) + '"]');
      Array.from(chatInner.querySelectorAll("[data-round]")).forEach(function (node) {
        const r = parseInt(node.getAttribute("data-round"), 10);
        if (r >= direct.insertAfterRound + 1) {
          node.setAttribute("data-round", String(r + 1));
          if (node._editData) node._editData.round = r + 1;
        }
      });
      if (firstNext && firstNext.parentNode === chatInner) {
        chatInner.insertBefore(userNode, firstNext);
        chatInner.insertBefore(msg, firstNext);
        insertedMidway = true;
      } else {
        chatInner.appendChild(userNode);
        chatInner.appendChild(msg);
      }
    } else {
      chatInner.appendChild(userNode);
      chatInner.appendChild(msg);
    }
    // 原地重发/插入重答：新节点已在会话中部就位，补上历史节点才有的
    // data-round 标记与编辑数据——否则本轮没有编辑/复制入口，且后续再次
    // 编辑本轮时删除循环匹配不到旧回复节点。必须在手术完成后打标：
    // 新 userNode 若先带上 data-round=N，会被上面的删除循环误删
    if (liveRound != null) {
      userNode.dataset.round = String(liveRound);
      if (direct && direct.targetRound != null) userNode.dataset.replacesRound = String(liveRound);
      userNode._editData = { content: userContent, round: liveRound, quotes: requestQuotes };
      App.attachUserEditAction(userNode);
      msg.dataset.round = String(liveRound);
    }
    // 原地重发/插入重答时视口停留在发起位置：对齐新提问气泡到视口顶部
    // （瞬跳，覆盖 smooth 滚动动画），不滚到底部——底部往往是更靠后的
    // 旧轮次；末尾追加语义（普通发送/编辑最后一轮）保持贴底跟随生成
    if (insertedMidway) {
      const prevBehavior = chatScroll.style.scrollBehavior;
      chatScroll.style.scrollBehavior = "auto";
      const delta = userNode.getBoundingClientRect().top - chatScroll.getBoundingClientRect().top;
      chatScroll.scrollTop += delta;
      chatScroll.style.scrollBehavior = prevBehavior;
      // 向下跳转不触发用户上滚暂停机制，显式暂停自动贴底：
      // 流式期间视口不被 delta 高频贴底拉回，用户滚回底部可自动恢复跟随
      App.pauseAutoScroll();
    } else {
      scrollToBottom();
    }
    App.rebuildQnav();
    const pipe = createStreamPipeline(msg, streamSessionId, activeStream);

    const payload = {
      messages: [{ role: "user", content: userContent }],
      session_id: streamSessionId,
    };
    // 引用快照（选中文本引用到提问）：与用户消息同级字段，后端落盘并序列化
    // 为模型视图的 <quote_list>；无引用不携带（旧行为完全一致）
    if (requestQuotes.length) payload.messages[0].quotes = requestQuotes;
    // 编辑重发「重新生成该轮」：携带 target_round，后端把本轮回复原地替换
    // 历史第 N 轮（上下文截到第 N-1 轮）；ask_user 再答及手动插入携带 insert_round，
    // 后端把回答轮插入历史第 N 轮之后（上下文截到第 N 轮）；普通发送不带
    // 该字段走追加语义
    if (direct && direct.targetRound) {
      payload.target_round = direct.targetRound;
    } else if (direct && direct.insertAfterRound != null) {
      payload.insert_round = direct.insertAfterRound;
      if (direct.strictInsertContext) payload.strict_insert_context = true;
    }
    // 内置工具（todo_write/ask_user）并入 selectedTools，与 MCP 工具一起随 tool_names 上送，
    // 后端按名称识别注入；未选择任何工具时不携带该字段，由后端回退会话/全局默认选择
    if (state.selectedTools.size > 0) {
      // 模型不支持视觉时 read_media 不上送（工具弹窗已禁选，此处为兜底过滤；
      // 后端请求侧还有一层拦截，前端/后端双保险）
      let toolNames = Array.from(state.selectedTools);
      if (!App.getChatModelVision()) {
        toolNames = toolNames.filter(function (name) { return name !== "read_media"; });
      }
      if (toolNames.length > 0) payload.tool_names = toolNames;
    }
    // 生成参数（temperature/top_p/presence_penalty/reasoning_effort/extra_body 等）
    // 不显式传递，由服务端按 model_selection.chat_model.parameter 填充（面板“参数”设置）

    // 原地重发/插入重答的屏幕状态（删旧轮/重编号/打标）发生在发送瞬间，
    // 而后端替换/插入收尾发生在流结束——失败/中止/空回复时屏幕与落盘可能失配，
    // 收尾后强制重载会话恢复真实状态；成功路径不重载，保持视口停在发起位置
    let streamFailed = false;
    let streamResult = null, streamError = null;
    // 新轮开跑：▶ 让位（预检已过，此处必发起请求；失败后收尾重同步）
    if (App.setResumeAvailable) App.setResumeAvailable(streamSessionId, false);
    try {
      streamResult = await API.chatStream(payload, pipe.handle, state.abort.signal,
        {onOpen: () => { if (App.taskStatus) App.taskStatus.connected(activeStream.taskHandle); }});
    } catch (err) {
      streamFailed = true;
      streamError = err;
      if (err.name !== "AbortError") {
        msg.appendChild(el("div", "notice-bar", "请求失败：" + streamErrorText(err)));
      }
    } finally {
      const completed = !streamError && (!streamResult || streamResult.completed !== false);
      const canFlush = App.taskStatus
        ? await App.taskStatus.end(activeStream.taskHandle, completed, streamError, activeStream.detached)
        : completed;
      await reconcileStreamInterruption(streamSessionId);
      const fin = pipe.finish();
      if (typeof App.finalizeMermaidBlocks === "function") App.finalizeMermaidBlocks();
      // 编辑重发"停止生成并重发"接管本流时会打 detached 标记：节点被新轮
      // DOM 手术移除属预期行为，旧流收尾的重载兜底必须跳过，否则会把新
      // 一轮流式渲染的节点整段重建掉
      const wasDetached = activeStream.detached === true;
      const shouldReloadVisibleSession = !wasDetached
        && state.sessionId === streamSessionId
        && activeStream.messageNode.parentNode !== chatInner;
      if (fin.empty && !msg.querySelector(".msg-user")) msg.remove();
      const usageReconciled = await App.refreshSessionUsage(streamSessionId);
      if (!usageReconciled && fin.roundUsage && fin.roundUsage.total_tokens) {
        // 后端暂不可用时才使用本地累计值作为临时回退，避免正常情况下重复累加。
        if (state.sessionId === streamSessionId) App.addSessionUsage(fin.roundUsage);
      }
      activeStream.completed = true;
      // 仅当本流仍是当前被监听的流时才清理全局标志，避免误清切换后新会话的流状态
      const wasAttached = state.activeStream === activeStream;
      if (wasAttached) {
        state.activeStream = null;
        state.streaming = false;
        state.streamingSession = null;
        state.abort = null;
      }
      if (state.sessionId === streamSessionId) App.clearContextStatsTimer();
      // 可用性重同步：失败/中断后末轮仍可继续则恢复 ▶；自然完成保持隐藏
      // （用户停止已确认时跳过：停止按钮已直接同步，后端 stopped 落盘窗口
      // 内的查询会把 true 覆盖回 false；切走会话由查询守卫丢弃）
      if (activeStream.stopConfirmed !== true) {
        if (streamFailed && App.refreshResumeAvailability) await App.refreshResumeAvailability(streamSessionId);
        else if (App.setResumeAvailable) App.setResumeAvailable(streamSessionId, false);
      }
      App.refreshComposerButtons();
      if (shouldReloadVisibleSession) {
        setTimeout(function () { App.openSession(streamSessionId); }, 0);
      } else if (!wasDetached && liveRound != null && (streamFailed || fin.empty)) {
        // 原地重发/插入重答失败（网络中断/停止/空回复等）：屏幕手术已做过但
        // 后端可能未收尾提交，重载会话与 JSONL 对齐
        setTimeout(function () { App.openSession(streamSessionId); }, 0);
      }
      // 监听结束（含 [DONE]/断线/停止）：查询统计，不据此推断后台任务已结束。
      // 用防抖合并：与 usage 事件触发的刷新合并成一个请求
      if (state.sessionId === streamSessionId) {
        App.scheduleContextTokenStatsRefresh(App.CONTEXT_STATS_EVENT_DEBOUNCE_MS, streamSessionId);
      }
      // 流结束后刷新提问卡片可答性：出现过普通用户消息/更新提问后旧卡片转为过期
      App.refreshAskBlockStates();
      // 引导/队列消息派发：本轮结束（含停止/出错）后自动发送下一条。
      // 延迟到下一轮事件循环，确保 state.streaming 已清理完毕
      if (canFlush) setTimeout(function () { App.flushPendingMessages(streamSessionId); }, 0);
    }
  }

  // ---------- 暂停后继续（无用户消息续跑） ----------

  // 设置 ▶ 继续可用性并刷新输入区按钮。后端是真源（空闲 + 末轮
  // stopped/interrupted）；暂停成功/任务完成等本地确定态直接同步，
  // 中断等不确定态经 refreshResumeAvailability 向后端查询。
  function setResumeAvailable(sessionId, available) {
    state.resumeAvailable = !!available;
    state.resumeSessionId = sessionId;
    App.refreshComposerButtons();
  }

  // 向后端查询 ▶ 可用性（会话打开/任务异常结束/继续被拒后重同步）。
  // 竞态守卫：只接受仍停留在查询会话时的结果，切走则丢弃不碰状态。
  function refreshResumeAvailability(sessionId) {
    const sid = SessionUtils.sanitizeSessionId(sessionId || state.sessionId);
    if (!sid) return Promise.resolve(false);
    return API.streamStatus(sid).then(function (status) {
      if (state.sessionId !== sid) return false;
      const ok = !!(status && status.can_resume);
      setResumeAvailable(sid, ok);
      return ok;
    }).catch(function () { return false; });
  }

  // 暂停/中断后继续：不渲染用户气泡、不写新问题，后端以内部 nudge
  // 另起一轮续跑（等价于服务端代发的"继续"）。流式消费与收尾同 send()。
  async function resume() {
    const streamSessionId = state.sessionId;
    if (!streamSessionId) return;
    // 仅当"当前会话"空闲时可继续；其他会话在后台流式先放弃本地监听
    if (state.streaming) {
      if (state.streamingSession === streamSessionId) return;
      detachAttachedStream();
    }
    // "连接已中断"不确定态：先向后端确认；仍在运行则自动重连并放弃继续
    if (!(await ensureSendable())) return;
    // 手动压缩进行中：仅压缩会话禁止继续（续跑会打断压缩并交错写历史）
    if (state.manualCompactRunning && state.manualCompactSession === streamSessionId) {
      toast("正在压缩对话上下文，请稍后再继续");
      return;
    }
    // 先落可用性再开跑：防重复点击；409 被拒后经重同步恢复
    if (App.setResumeAvailable) App.setResumeAvailable(streamSessionId, false);
    state.streaming = true;
    state.streamingSession = streamSessionId;
    state.abort = new AbortController();
    setEmpty(false);
    App.refreshComposerButtons();
    App.refreshContextTokenStats(streamSessionId);

    // 本轮回复容器（无用户气泡）：思考/回答/工具按到达顺序分块
    const msg = el("div", "msg");
    const activeStream = {
      sessionId: streamSessionId,
      userText: "",
      replayComplete: true,
      round: null,
      userContent: null,
      questionParts: null,
      quotes: null,
      userNode: null,
      messageNode: msg,
      completed: false,
      usage: null,
      resumed: true,
    };
    state.activeStream = activeStream;
    if (App.taskStatus) activeStream.taskHandle = App.taskStatus.begin(streamSessionId);
    chatInner.appendChild(msg);
    scrollToBottom();
    const pipe = createStreamPipeline(msg, streamSessionId, activeStream);

    const payload = { session_id: streamSessionId };
    // 工具选择与发送同口径：随本轮 tool_names 上送（视觉兜底过滤一致）
    if (state.selectedTools.size > 0) {
      let toolNames = Array.from(state.selectedTools);
      if (!App.getChatModelVision()) {
        toolNames = toolNames.filter(function (name) { return name !== "read_media"; });
      }
      if (toolNames.length > 0) payload.tool_names = toolNames;
    }

    let streamFailed = false;
    let streamResult = null, streamError = null;
    try {
      streamResult = await API.resumeChat(payload, pipe.handle, state.abort.signal,
        {onOpen: () => { if (App.taskStatus) App.taskStatus.connected(activeStream.taskHandle); }});
    } catch (err) {
      streamFailed = true;
      streamError = err;
      if (err.name !== "AbortError") {
        msg.appendChild(el("div", "notice-bar", "请求失败：" + streamErrorText(err)));
      }
    } finally {
      const completed = !streamError && (!streamResult || streamResult.completed !== false);
      const canFlush = App.taskStatus
        ? await App.taskStatus.end(activeStream.taskHandle, completed, streamError, activeStream.detached)
        : completed;
      await reconcileStreamInterruption(streamSessionId);
      const fin = pipe.finish();
      if (typeof App.finalizeMermaidBlocks === "function") App.finalizeMermaidBlocks();
      const wasDetached = activeStream.detached === true;
      const shouldReloadVisibleSession = !wasDetached
        && state.sessionId === streamSessionId
        && activeStream.messageNode.parentNode !== chatInner;
      if (fin.empty && !msg.querySelector(".msg-user")) msg.remove();
      const usageReconciled = await App.refreshSessionUsage(streamSessionId);
      if (!usageReconciled && fin.roundUsage && fin.roundUsage.total_tokens) {
        if (state.sessionId === streamSessionId) App.addSessionUsage(fin.roundUsage);
      }
      activeStream.completed = true;
      const wasAttached = state.activeStream === activeStream;
      if (wasAttached) {
        state.activeStream = null;
        state.streaming = false;
        state.streamingSession = null;
        state.abort = null;
      }
      if (state.sessionId === streamSessionId) App.clearContextStatsTimer();
      // 可用性重同步：失败/中断后末轮仍可继续则恢复 ▶；自然完成保持隐藏
      // （用户停止已确认时跳过，原因同发送流收尾）
      if (activeStream.stopConfirmed !== true) {
        if (streamFailed && App.refreshResumeAvailability) await App.refreshResumeAvailability(streamSessionId);
        else if (App.setResumeAvailable) App.setResumeAvailable(streamSessionId, false);
      }
      App.refreshComposerButtons();
      if (shouldReloadVisibleSession) {
        setTimeout(function () { App.openSession(streamSessionId); }, 0);
      }
      if (state.sessionId === streamSessionId) {
        App.scheduleContextTokenStatsRefresh(App.CONTEXT_STATS_EVENT_DEBOUNCE_MS, streamSessionId);
      }
      App.refreshAskBlockStates();
      if (canFlush) setTimeout(function () { App.flushPendingMessages(streamSessionId); }, 0);
    }
  }

  sendBtn.addEventListener("click", function () { send(); });

  resumeBtn.addEventListener("click", function () { resume(); });

  stopBtn.addEventListener("click", async function () {
    // 手动压缩进行中：显式停止后台任务（先调 stop 接口再断开本地 SSE，
    // 单纯断线不再取消任务，刷新后可重连）；终止作用于压缩会话本身，
    // 切到其他会话后此按钮属于该会话的流式任务
    if (state.manualCompactRunning && state.manualCompactSession === state.sessionId) {
      if (App.stopManualCompaction) await App.stopManualCompaction();
      else if (state.manualCompactAbort) state.manualCompactAbort.abort();
      return;
    }
    const streamSessionId = state.sessionId;
    if (App.taskStatus && App.taskStatus.isStopping && App.taskStatus.isStopping(streamSessionId)) return;
    // 用户主动停止：本轮结束后不自动派发引导/队列消息（消费一次后复位）
    state.suppressFlushOnce = true;
    const active = state.activeStream && state.activeStream.sessionId === streamSessionId ? state.activeStream : null;
    const abort = active ? state.abort : null;
    if (streamSessionId) {
      const taskHandle = App.taskStatus && App.taskStatus.stopping(streamSessionId);
      try {
        await API.stopChat(streamSessionId);
        if (App.taskStatus) App.taskStatus.stopped(streamSessionId, taskHandle);
        // 暂停成功：末轮即 stopped，可继续（本地确定态，直接同步免查询）；
        // 同时给本地流打停止确认标记，其中断收尾跳过重同步，避免落盘
        // 窗口内的查询把 true 覆盖回 false（停止按钮不显示恢复按钮的根因）。
        if (active) active.stopConfirmed = true;
        if (App.setResumeAvailable) App.setResumeAvailable(streamSessionId, true);
      } catch (error) {
        // 后端不可用（网络层错误）且本地已无活动流：任务大概率已随后端中断
        // 而消失，本地收尾为已停止以便重启后端后直接继续；若后端任务实际仍在，
        // 下一次发送会按“新消息打断旧任务”语义平滑接管，不会产生双任务。
        if (!active && error && error.name !== "AbortError" && error.status == null) {
          if (App.taskStatus) App.taskStatus.stopped(streamSessionId, taskHandle);
          if (App.setResumeAvailable) App.setResumeAvailable(streamSessionId, true);
          toast("后端无响应，已在本地停止；请确认后端状态后继续");
          App.refreshComposerButtons();
          return;
        }
        state.suppressFlushOnce = false;
        if (App.taskStatus) App.taskStatus.stopFailed(streamSessionId, error, taskHandle);
        toast("停止失败，请重试：" + error.message); return;
      }
    }
    if (abort && state.activeStream === active) abort.abort();
    App.refreshComposerButtons();
  });

  /**
   * 放弃当前监听的流：仅中止本地连接（后端生成任务继续在后台运行，随时可切回续看），
   * 并清理全局流状态。切换会话/在别处新发送时调用，保证不会误停其他会话的任务。
   */
  function detachAttachedStream() {
    if (App.taskStatus && state.activeStream) App.taskStatus.detach(state.activeStream.taskHandle);
    if (state.abort) state.abort.abort();
    state.abort = null;
    state.streaming = false;
    state.streamingSession = null;
    if (state.activeStream) {
      state.activeStream.completed = true;
      state.activeStream = null;
    }
    // 切换会话放弃监听：该轮结束不自动派发引导/队列（消费一次后复位）
    state.suppressFlushOnce = true;
    App.refreshComposerButtons();
  }

  /**
   * 刷新/重新打开会话时若后端仍在为同一会话生成，则附接该后台流：
   * 收到 replay 标记后补建“当前轮提问”气泡，再回放当前轮事件并持续推流。
   */
  async function attachStreamSession(sessionId) {
    if (!sessionId) return;
    if (state.sessionId !== sessionId) return;
    if (state.streaming && state.streamingSession === sessionId) return;
    if (state.streaming) detachAttachedStream();
    state.streaming = true;
    state.streamingSession = sessionId;
    state.abort = new AbortController();
    App.refreshComposerButtons();
    App.refreshContextTokenStats(sessionId);
    setEmpty(false);

    const msg = el("div", "msg");
    const activeStream = {
      sessionId: sessionId,
      userText: "",
      // 附接回放后回填：提问原始 content（部件列表/文本）与本轮最终轮次号，
      // openSession 的历史裁剪/去重与编辑入口补挂都按这两个字段精确匹配
      userContent: null,
      replayComplete: false,
      round: null,
      userNode: null,
      messageNode: msg,
      completed: false,
      usage: null,
    };
    state.activeStream = activeStream;
    if (App.taskStatus) activeStream.taskHandle = App.taskStatus.begin(sessionId);
    chatInner.appendChild(msg);
    // 窗口恢复要先看到本流；搜索跳转已暂停跟随时仍保留阅读位置。
    stickToBottom();
    const pipe = createStreamPipeline(msg, sessionId, activeStream);
    let sawReplay = false;
    let streamResult = null, streamError = null;

    try {
      streamResult = await API.chatStream(
        { session_id: sessionId, messages: [] },
        function (evt) {
          const data = evt.data || {};
          if (data && data.replay_end) {
            // 回放耗尽：此后均为实时帧，关闭去重窗口
            pipe.setReplayWindow(false);
            return;
          }
          if (data && data.replay) {
            sawReplay = true;
            activeStream.replayComplete = data.replay_complete === true;
            // 新后端随 marker 声明回放结束标记支持：打开去重窗口，窗口内
            // 的正文/思考/工具调用先与历史已渲染节点匹配（命中则收养/丢弃）
            pipe.setReplayWindow(Boolean(data.replay_end_supported));
            // 本轮最终轮次号（后端 round_started 推送时同步记录、随 marker 下发）：
            // 历史裁剪/去重/补挂编辑入口统一按轮次精确匹配——同文本历史提问
            // 不再被误判成本轮、多模态（部件数组）提问也能命中
            if (typeof data.round === "number" && data.round > 0) {
              activeStream.round = data.round;
            }
            // question_parts 为提问原始 content 部件（多模态时含 media:// 引用；
            // 纯文本提问后端归一为单个 text 部件）：用多部件气泡重建（缩略图
            // 可点开预览），缺失时才退回纯文本
            const parts = Array.isArray(data.question_parts) ? data.question_parts : null;
            if (data.question_text) activeStream.userText = data.question_text;
            else if (parts) {
              activeStream.userText = parts
                .map(function (p) { return p && p.type === "text" && typeof p.text === "string" ? p.text : ""; })
                .filter(function (t) { return t.trim(); })
                .join("\n");
            }
            // 原始 content（重建/复用气泡后补挂编辑入口的数据源）：部件优先，
            // 缺失时退化为文本字符串（编辑重发按文本处理）
            if (parts) activeStream.userContent = parts;
            else if (activeStream.userContent == null && activeStream.userText) {
              activeStream.userContent = activeStream.userText;
            }
            // 引用快照（选中文本引用到提问）：随 marker 下发，重建/复用气泡
            // 时绘制引用卡片，并进 _editData 供编辑回填
            if (Array.isArray(data.question_quotes) && data.question_quotes.length) {
              activeStream.quotes = QuoteUtils.quotesForRequest(data.question_quotes);
            }
            if (activeStream.replayComplete && App.prepareActiveRoundReplay) {
              App.prepareActiveRoundReplay(activeStream);
            }
            // 本轮生效聊天模型（round_model 帧在回放起点之前，消费端收不到，
            // 随 marker 下发）：usage 行模型名文案的数据源
            if (data.round_usage_total) pipe.setRoundUsageBaseline(data.round_usage_total);
            if (data.model) pipe.setRoundModel(data.model);
            if (activeStream.userText || parts) {
              // 历史文件已落盘时可能已经渲染过本轮提问（生成中刷新，提问事件
              // 已随检查点/收尾写入）：优先按轮次精确匹配并复用该节点补挂
              // 编辑入口。轮次号有效但未命中 = 本轮尚未落盘（生成中），直接
              // 新建气泡——不回退文本匹配，否则会误复用历史中同文本的旧提问
              // 节点（后续编辑/删除定位错轮）。仅无轮次号（旧后端）时回退
              // 文本全等比较（两侧 trim，与历史回放同口径）
              let existing = null;
              if (activeStream.round != null) {
                existing = chatInner.querySelector(
                  '.msg-user[data-round="' + activeStream.round + '"]'
                );
              } else {
                const wanted = String(activeStream.userText || "").trim();
                existing = Array.from(chatInner.querySelectorAll(".msg-user"))
                  .find(function (node) {
                    const bubble = node.querySelector(".msg-bubble");
                    return Boolean(bubble) && bubble.textContent.trim() === wanted;
                  }) || null;
              }
              if (existing) {
                activeStream.userNode = existing;
              } else {
                // 提问气泡必须排在回答上方：回答容器 msg 已先入列，把气泡插入到它之前
                const userNode = parts
                  ? App.appendUserMessage(parts, null, sessionId, null, null, null, activeStream.quotes)
                  : App.appendUserMessage(data.question_text, null, sessionId, null, null, null, activeStream.quotes);
                chatInner.insertBefore(userNode, msg);
                activeStream.userNode = userNode;
              }
              // 补挂轮次标记与编辑/复制/删除入口（复用节点时幂等跳过）
              App.attachLiveRoundEntry(activeStream, activeStream.round);
              App.rebuildQnav();
            }
            return;
          }
          pipe.handle(evt);
        },
        state.abort.signal,
        {onOpen: () => { if (App.taskStatus) App.taskStatus.connected(activeStream.taskHandle); }}
      );
    } catch (err) {
      streamError = err;
      if (err.name !== "AbortError") {
        msg.appendChild(el("div", "notice-bar", "请求失败：" + streamErrorText(err)));
      }
    } finally {
      const completed = !streamError && (!streamResult || streamResult.completed !== false);
      const canFlush = App.taskStatus
        ? await App.taskStatus.end(activeStream.taskHandle, completed, streamError, activeStream.detached)
        : completed;
      await reconcileStreamInterruption(sessionId);
      const fin = pipe.finish();
      if (typeof App.finalizeMermaidBlocks === "function") App.finalizeMermaidBlocks();
      if (fin.empty && !sawReplay) msg.remove();
      const usageReconciled = await App.refreshSessionUsage(sessionId);
      if (!usageReconciled && fin.roundUsage && fin.roundUsage.total_tokens) {
        if (state.sessionId === sessionId) App.addSessionUsage(fin.roundUsage);
      }
      activeStream.completed = true;
      // 仅当本流仍是当前被监听的流时才清理全局标志，避免误清切换后新会话的流状态
      const wasAttached = state.activeStream === activeStream;
      if (wasAttached) {
        state.activeStream = null;
        state.streaming = false;
        state.streamingSession = null;
        state.abort = null;
      }
      if (state.sessionId === sessionId) App.clearContextStatsTimer();
      // 可用性重同步（附接流收尾同口径）：失败/中断恢复 ▶，自然完成隐藏
      // （用户停止已确认时跳过，原因同发送流收尾）
      if (activeStream.stopConfirmed !== true) {
        if (!completed && App.refreshResumeAvailability) await App.refreshResumeAvailability(sessionId);
        else if (App.setResumeAvailable) App.setResumeAvailable(sessionId, false);
      }
      App.refreshComposerButtons();
      // 附接流结束：同样用防抖合并刷新一次统计增量
      if (state.sessionId === sessionId) {
        App.scheduleContextTokenStatsRefresh(App.CONTEXT_STATS_EVENT_DEBOUNCE_MS, sessionId);
      }
      // 附接的后台流结束：派发该会话暂存的引导/队列消息（用户仍停留在该会话时）
      if (canFlush) setTimeout(function () { App.flushPendingMessages(sessionId); }, 0);
    }
  }

  /** 打开会话后：若该会话后台仍在生成，自动附接续看。 */
  function maybeAttachRunningStream(sessionId) {
    if (!sessionId) return;
    const sid = SessionUtils.sanitizeSessionId(sessionId);
    if (state.streaming && state.streamingSession === sid) return;
    API.streamStatus(sid)
      .then(function (status) {
        if (state.sessionId !== sid) return;
        if (App.taskStatus) App.taskStatus.checked(sid, status);
        // 会话打开时零成本同步 ▶ 可用性（状态接口已捎带 can_resume）
        if (App.setResumeAvailable) App.setResumeAvailable(sid, !!(status && status.can_resume));
        if (status && status.running && !(state.streaming && state.streamingSession === sid)) {
          attachStreamSession(sid);
        } else if (!status || !status.running) {
          // 后台无生成任务：补发该会话暂存的引导/队列消息（切走期间错过的派发）。
          // flush 内部校验用户仍停留在该会话，切走则留在暂存区
          setTimeout(function () { App.flushPendingMessages(sid); }, 0);
        }
      })
      .catch(function () { /* 状态查询失败则跳过续接与补发 */ });
  }

  // ---------- 会话标题（前端驱动，零轮询） ----------
  // 采集与触发：流式管线累计思考/正文 delta，任一达 TITLE_PREVIEW_CHARS 即
  // POST /chat_config/generate_title **一次**；流结束时不足 100 字符以已有
  // 全文触发。后端生成写盘并把标题同步返回（已生成过/失败标记/未配置时
  // 返回 skipped + 当前盘上标题，同样一次往返）——前端收到即替换侧栏标题，
  // 不再轮询 /chat_history/meta。标题生成与聊天主链路完全解耦。
  const TITLE_PREVIEW_CHARS = 100;
  let titleInflight = null; // 进行中的标题请求 {sessionId}（防并发重复）

  function pickTitlePreview(reasoning, content) {
    // 思考与正文取较长者（正文语义更相关），截到 100 字符
    const source = content.length >= reasoning.length ? content : reasoning;
    return source.slice(0, TITLE_PREVIEW_CHARS).trim();
  }

  async function requestSessionTitle(sessionId, questionText, questionParts, preview) {
    const sid = SessionUtils.sanitizeSessionId(sessionId);
    if (!sid || (titleInflight && titleInflight.sessionId === sid)) return;
    titleInflight = { sessionId: sid };
    try {
      const res = await API.generateSessionTitle({
        session_id: sid,
        cost_task_id: App.taskCosts && App.taskCosts.activeId(sid),
        question_text: String(questionText || "").slice(0, 300),
        model_preview: preview || "",
        question_parts: Array.isArray(questionParts) && questionParts.length ? questionParts : undefined,
      });
      const title = res && res.title || "";
      if (title) applySessionTitle(sid, title);
    } catch (_) { /* 请求失败静默：标题保持现状 */ }
    finally {
      if (titleInflight && titleInflight.sessionId === sid) titleInflight = null;
      if (App.taskCosts && state.sessionId === sid) App.taskCosts.refresh();
    }
  }

  function applySessionTitle(sessionId, title) {
    const sid = SessionUtils.sanitizeSessionId(sessionId);
    // 记入可信标题表：后续发消息时沿用该标题（不再退回"提问前 40 字"占位）
    if (App.rememberSessionTitle) App.rememberSessionTitle(sid, title);
    // 本地兜底数据源同步（后端落盘慢时 loadSessions 重建列表用的 recency 标题）
    if (state.sessionRecency[sid]) state.sessionRecency[sid].title = title;
    // 分组区内的同名行同步（组内展开时存在副本；未归组时为空操作）
    if (App.sessionGroups && App.sessionGroups.updateRowTitle) {
      App.sessionGroups.updateRowTitle(sid, title);
    }
    const node = sessionList.querySelector('[data-session="' + sid + '"] .session-name');
    if (!node) {
      // 侧栏还没有该会话条目（极端时序）：重载会话列表建立条目后再替换
      App.loadSessions().then(function () {
        App.markActiveSession();
        const retry = sessionList.querySelector('[data-session="' + sid + '"] .session-name');
        if (retry && App.setSessionNameText(retry, title)) {
          App.refreshSessionFades();
        }
      }).catch(function () { /* ignore */ });
      return;
    }
    if (App.setSessionNameText(node, title)) {
      App.refreshSessionFades();
    }
  }


  // ---------- 用户消息编辑重发编排（编辑 UI 在 messages.js） ----------

  /**
   * 编辑重发专用：会话正在生成时先弹确认框（再次提醒任务正在进行），
   * 确认返回 true 并立即停止生成——与停止按钮同款链路（后端 stop_chat
   * 会等待任务完全退出、按"手动停止"把已生成内容收尾落盘），同时复位
   * 本地流式状态；取消返回 false（用户放弃本次编辑重发）。
   * @returns {Promise<boolean>} true=已停止可继续删除/重发；false=用户取消
   */
  async function requestStopRunningGeneration(sessionId, suspended) {
    const message = suspended
      ? "当前会话有服务中断后保留的任务，实际未在运行。确认后将保存已有轨迹、结束旧任务的恢复状态，并以编辑后的消息重新发起。"
      : "当前会话正在生成回复。确认后将立刻停止本次生成" +
        "（已生成内容按中断收尾落盘），并以编辑后的消息重新发起。";
    const confirmed = await new Promise(function (resolve) {
      if (typeof App.openConfirmDialog !== "function") {
        resolve(window.confirm(message + "确认继续？"));
        return;
      }
      let settled = false;
      App.openConfirmDialog({
        title: suspended ? "任务已中断" : "任务正在进行中",
        message: message,
        confirmText: suspended ? "结束旧任务并重发" : "停止生成并重发",
        onConfirm: function () { settled = true; resolve(true); },
        onCancel: function () { if (!settled) { settled = true; resolve(false); } },
      });
    });
    if (!confirmed) return false;
    // 用户主动停止：本轮结束后不自动派发引导/队列消息（消费一次后复位）
    state.suppressFlushOnce = true;
    try {
      await API.stopChat(sessionId);
    } catch (error) {
      state.suppressFlushOnce = false;
      App.toast("停止原任务失败：" + error.message);
      return false;
    }
    // 中止本地监听（后端任务已由 stopChat 收尾；abort 触发旧 send 的
    // finally 兜底清理，节点已脱管不会误删新内容）
    if (state.activeStream && state.activeStream.sessionId === sessionId) {
      if (state.abort) state.abort.abort();
      // detached 标记：旧流收尾的失败重载兜底跳过（本轮即将被删除/重发，
      // 新一轮流式渲染已在路上，重载会把新节点整段重建掉）
      state.activeStream.detached = true;
      state.activeStream.completed = true;
      state.activeStream = null;
    }
    if (state.streamingSession === sessionId) {
      state.streaming = false;
      state.streamingSession = null;
      // 本地监听已中止：清掉所属控制器（旧 send 的 finally 因 wasAttached
      // 已为 false 不会清理；下次 send 会重新创建）
      state.abort = null;
    }
    App.refreshComposerButtons();
    return true;
  }

  /**
   * 编辑重发总编排：按模式执行删除/预演确认 → 重载会话 → 发送。
   * @param {{msg: HTMLElement, round: number, text: string, media: Array, mode: string}} plan
   *   mode: "regen"=重新生成该轮（原地替换，保留后续轮次）；
   *         "truncate"=删除该轮及之后（GPT 语义）；
   *         "insert"=在该轮之前插入新对话
   */
  async function confirmEditResend(plan) {
    const sessionId = state.sessionId;
    if (!sessionId || !plan || !plan.round) return;
    // 会话正在生成：先经用户二次确认停止任务（再次提醒"任务正在进行"），
    // 停止收尾落盘后才允许删除轮次，随后按计划重发
    const pendingPermissions = await API.pendingApprovals(sessionId).catch(error => { App.toast("任务状态读取失败：" + error.message); return null; });
    if (!pendingPermissions || state.sessionId !== sessionId) return;
    const hasSavedTask = (pendingPermissions.tasks || []).length > 0 || (pendingPermissions.approvals || []).some(a => a.agent_id !== "preview");
    if ((state.streaming && state.streamingSession === sessionId) || hasSavedTask) {
      const suspended = hasSavedTask && (pendingPermissions.running === false ||
        (pendingPermissions.running == null && !(state.streaming && state.streamingSession === sessionId)));
      const proceed = await requestStopRunningGeneration(sessionId, suspended);
      if (!proceed || state.sessionId !== sessionId) return;
    }
    if (plan.mode === "insert") {
      App.exitUserMessageEdit(plan.msg);
      App.send({
        text: plan.text, media: plan.media, docs: plan.docs || [],
        quotes: plan.quotes || [], insertAfterRound: plan.round - 1,
        strictInsertContext: true,
      });
      return;
    }
    // 复用附件：编辑重发不重新上传，删除清理时后端按 keep_media_refs 排除
    const keptRefs = (plan.media || [])
      .map(function (m) { return m.stored_name || ""; })
      .filter(Boolean);

    const doSend = function (targetRound) {
      App.exitUserMessageEdit(plan.msg);
      App.send({
        text: plan.text,
        media: plan.media,
        docs: plan.docs || [],
        // 引用快照：编辑态保留/移除后的最终列表（无则空数组）
        quotes: plan.quotes || [],
        targetRound: targetRound,
      });
    };

    const reloadAndSend = async function (mode, targetRound, keepRefs) {
      try {
        await API.deleteRounds(sessionId, plan.round, {
          mode: mode,
          deleteFiles: true,
          keepMediaRefs: keepRefs,
        });
      } catch (err) {
        App.toast("删除轮次失败：" + err.message);
        return;
      }
      // 删除成功：强制整段重放（data-round 随新解析自动刷新），然后发送。
      // 删除接口要求会话非运行中；仅复位本会话的本地流式态（不影响其他会话后台流监听）
      if (state.streamingSession === sessionId) {
        state.streaming = false;
        state.streamingSession = null;
      }
      await App.openSession(sessionId);
      doSend(targetRound);
    };

    if (plan.mode === "regen") {
      // 重新生成该轮：不预删！直接带 target_round 发送，后端轮次收尾时把
      // 新回复整轮替换到历史第 N 轮位置（替换即删旧插新）。
      // 若先删后发，后续轮次会前移导致 target_round 与删后编号错位，
      // 新回复会错误替换到原第 N+1 轮的位置。旧轮附件等新轮原子写入后，
      // 后端再按剩余历史引用差集清理；重发失败时不会提前删除旧附件。
      doSend(plan.round);
      return;
    }
    // 删除该轮及之后：破坏面较大（含后续轮次与其附件），先预演展示明细
    let planned;
    try {
      planned = await API.deleteRounds(sessionId, plan.round, {
        mode: "truncate",
        deleteFiles: true,
        dryRun: true,
      });
    } catch (err) {
      App.toast("删除预演失败：" + err.message);
      return;
    }
    // ask_user 卡片再答等无编辑面板的调用方（plan.msg 为 null）：降级用
    // 原生 confirm 展示预演明细（ dry_run 已执行，确认后直接发送）
    const plansArea = plan.msg ? plan.msg.querySelector(".msg-edit-plans") : null;
    if (!plansArea) {
      const summary = planned.planned_rounds || [];
      const mediaFiles = (planned.planned_files && planned.planned_files.media_files) || [];
      const docFiles = (planned.planned_files && planned.planned_files.doc_files) || [];
      const lines = ["将删除 " + summary.length + " 个轮次"];
      if (summary.length && summary[0].round) lines.push("（第 " + summary[0].round + " 轮起）");
      if (mediaFiles.length) lines.push("、" + mediaFiles.length + " 个附件文件");
      if (docFiles.length) lines.push("、" + docFiles.length + " 个上传文档");
      if (window.confirm(lines.join("") + "。确认删除并发送？")) {
        doSend(null);
      }
      return;
    }
    plansArea.innerHTML = "";
    const box = el("div", "msg-edit-confirm");
    const summary = planned.planned_rounds || [];
    const mediaFiles = (planned.planned_files && planned.planned_files.media_files) || [];
    const docFiles = (planned.planned_files && planned.planned_files.doc_files) || [];
    const lines = [];
    lines.push("将删除 " + summary.length + " 个轮次"
      + (summary.length && summary[0].round ? "（第 " + summary[0].round + " 轮起）" : "")
      + "。");
    if (mediaFiles.length) lines.push("将删除 " + mediaFiles.length + " 个附件文件。");
    if (docFiles.length) lines.push("将删除 " + docFiles.length + " 个上传文档。");
    const desc = el("div", "msg-edit-confirm-desc", lines.join(" "));
    box.appendChild(desc);
    const actions = el("div", "msg-edit-confirm-actions");
    const cancelBtn = el("button", "msg-edit-cancel", "取消");
    const confirmBtn = el("button", "msg-edit-send", "确认删除并发送");
    cancelBtn.type = "button";
    confirmBtn.type = "button";
    cancelBtn.addEventListener("click", function () {
      // 复位面板确认条状态（showPlanConfirm 与预演条共用 plans 区域）
      const panel = plan.msg.querySelector(".msg-edit-panel");
      if (panel) panel._confirmShown = false;
      plansArea.innerHTML = "";
    });
    confirmBtn.addEventListener("click", async function () {
      confirmBtn.disabled = true;
      await reloadAndSend("truncate", null, keptRefs);
    });
    actions.appendChild(cancelBtn);
    actions.appendChild(confirmBtn);
    box.appendChild(actions);
    plansArea.appendChild(box);
  }

  // ---------- 导出（供其它模块经 App.* 调用） ----------
  App.send = send;
  App.resume = resume;
  App.setResumeAvailable = setResumeAvailable;
  App.refreshResumeAvailability = refreshResumeAvailability;
  App.confirmEditResend = confirmEditResend;
  App.maybeAttachRunningStream = maybeAttachRunningStream;
  App.attachStreamSession = attachStreamSession;
})(window.App);
