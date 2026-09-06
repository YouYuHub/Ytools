/**
 * 会话分享 / 加载 JSONL / 会话文件
 * - 顶栏分享与加载入口、JSONL 导入（后端优先、本地预览兜底）
 * - 会话文档上传解析（file_memory）与会话文件列表加载
 * 依赖：app/core.js、API、HistoryParser、SessionUtils；App.*：media/compaction 等模块
 */
(function (App) {
  "use strict";
  const {
    state, toast, $, setEmpty,
    scrollToBottom, closeMenus, docKindOf, MAX_DOC_FILE_SIZE,
    historyFileInput, fileInput, sessionActionsBtn, sessionActionsMenu,
    shareSessionItem, compactSessionItem, chatInner
  } = App;

  // ---------- 分享 / 加载 / 压缩会话 ----------
  /**
   * 顶栏图标按钮：空态点击直接进入加载（保持原“加载”行为）；
   * 有会话内容时展开三选项菜单（分享 / 加载 / 压缩对话），此处只负责
   * 按会话状态同步菜单项显隐。
   */
  function updateExportButton() {
    const hasContent = state.hasConversation || state.streaming;
    shareSessionItem.classList.toggle("hidden", !hasContent);
    compactSessionItem.classList.toggle("hidden", !hasContent);
  }

  function downloadText(filename, text) {
    const blob = new Blob([text], { type: "application/jsonl;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 0);
  }

  async function downloadSession(sessionId) {
    sessionId = SessionUtils.sanitizeSessionId(sessionId);
    if (!sessionId || !state.hasConversation) {
      toast("当前没有可分享的对话内容");
      return;
    }
    if (sessionId === state.sessionId && state.importedHistoryText) {
      downloadText(sessionId + "_chat.jsonl", state.importedHistoryText);
      return;
    }
    try {
      const response = await fetch(API.BASE + "/chat_history/file?session_id=" + encodeURIComponent(sessionId));
      if (!response.ok) throw new Error("导出失败");
      downloadText(sessionId + "_chat.jsonl", await response.text());
    } catch (err) {
      toast(err.message);
    }
  }

  function openHistoryPicker() {
    try {
      if (typeof historyFileInput.showPicker === "function") historyFileInput.showPicker();
      else historyFileInput.click();
    } catch (_) {
      historyFileInput.click();
    }
  }

  sessionActionsBtn.addEventListener("click", function (event) {
    event.stopPropagation();
    // 初始/空态：按钮即“加载”
    if (!state.hasConversation && !state.streaming) {
      openHistoryPicker();
      return;
    }
    const willOpen = sessionActionsMenu.classList.contains("hidden");
    closeMenus();
    if (willOpen) sessionActionsMenu.classList.remove("hidden");
  });
  shareSessionItem.addEventListener("click", function () {
    closeMenus();
    downloadSession(state.sessionId);
  });
  $("#loadHistoryItem").addEventListener("click", function () {
    closeMenus();
    openHistoryPicker();
  });
  compactSessionItem.addEventListener("click", function () {
    closeMenus();
    App.startManualCompaction();
  });

  // 本地预览 jsonl（后端不可用 / 无合法轮次时兜底）
  function previewLocalHistory(text, parsed, sessionId, toastMsg) {
    App.resetContextTokenStats();
    state.sessionId = sessionId;
    state.importedHistoryText = text;
    state.hasConversation = true;
    chatInner.innerHTML = "";
    setEmpty(false);
    App.setSessionUsage(parsed.metaUsage);
    App.renderRecords(parsed.records);
    updateExportButton();
    App.rebuildQnav();
    scrollToBottom();
    if (toastMsg) toast(toastMsg);
  }

  historyFileInput.addEventListener("change", async function () {
    const file = historyFileInput.files && historyFileInput.files[0];
    historyFileInput.value = "";
    if (!file) return;
    try {
      const text = await file.text();
      const parsed = HistoryParser.parseHistory(text);
      if (!parsed.records.length) {
        toast("未找到可显示的会话记录");
        return;
      }
      // 会话标识以文件名为准：先本地校验内容，再上传到后端持久化
      const baseId = SessionUtils.sanitizeSessionId(file.name);
      const localId = baseId !== "default" ? baseId : ("imported-" + Date.now().toString(36));
      try {
        const res = await API.uploadChatHistory(baseId, file, false);
        if (!res || res.imported_rounds === 0) {
          previewLocalHistory(text, parsed, localId, "文件中没有可导入的会话记录，已本地预览");
          return;
        }
        const actualId = SessionUtils.sanitizeSessionId(res.session_id || baseId);
        await App.loadSessions();
        await App.openSession(actualId);
        toast(res.collision ? "会话已导入（与已有会话重名，已另存新文件）" : "会话已导入");
      } catch (uploadErr) {
        // 后端不可用时退回本地预览
        previewLocalHistory(text, parsed, localId, "上传失败，已本地预览：" + uploadErr.message);
      }
    } catch (err) {
      toast("加载失败：" + err.message);
    }
  });

  fileInput.addEventListener("change", async function () {
    let files = Array.from(fileInput.files || []);
    fileInput.value = "";
    if (!files.length) return;
    if (files.length > 10) {
      toast("最多上传 10 个文件，已截取前 10 个");
      files = files.slice(0, 10);
    }
    // 按类型分流，两种附件统一显示在输入框上方附件区：
    // - 媒体（图片/音频/视频）：与剪贴板粘贴同款——先进待发区，随消息上传，
    //   以 media:// 引用进入消息内容部件；
    // - 可解析文档：立即上传解析（file_memory），解析文本由后端注入系统
    //   提示词（跨消息生效），不进入消息内容部件。
    const mediaFiles = files.filter(function (f) { return App.mediaKindOf(f.name); });
    const docFiles = files.filter(function (f) { return !App.mediaKindOf(f.name); });
    if (mediaFiles.length) App.addPendingMediaFiles(mediaFiles);

    const unsupported = docFiles.filter(function (f) { return !docKindOf(f.name); });
    if (unsupported.length) {
      toast("不支持的文件类型：" + unsupported.map(function (f) { return f.name; }).join("、"));
    }
    const oversizeDocs = docFiles.filter(function (f) { return docKindOf(f.name) && f.size > MAX_DOC_FILE_SIZE; });
    if (oversizeDocs.length) {
      toast("已跳过超过 10MB 的文件：" + oversizeDocs.map(function (f) { return f.name; }).join("、"));
    }
    const parseable = docFiles.filter(function (f) {
      return docKindOf(f.name) && f.size <= MAX_DOC_FILE_SIZE;
    });
    if (!parseable.length) return;

    // 上传属于产生会话内容的动作：此时才分配会话 ID（与发送消息同一时机规则）
    const uploadSessionId = App.ensureSessionId();
    try {
      const res = await API.uploadSessionFiles(uploadSessionId, parseable);
      toast("上传完成：成功 " + (res.success || 0) + " 个，失败 " + (res.failed || 0) + " 个");
      loadSessionFiles();
    } catch (err) {
      toast("上传失败：" + err.message);
    }
  });

  async function loadSessionFiles() {
    // 待开始会话没有文件记录可查，直接清空，不发请求
    if (!state.sessionId) {
      state.sessionDocs = [];
      App.renderComposerAttachments();
      return;
    }
    let files = [];
    try {
      const data = await API.getSessionFiles(state.sessionId);
      files = data.files || [];
    } catch (_) { /* 读取失败不阻塞 */ }
    state.sessionDocs = files.map(function (f) {
      return { filename: f.filename || "未命名", type: f.type || "", stored_name: f.stored_name || "" };
    });
    App.renderComposerAttachments();
  }


  // ---------- 导出（供其它模块经 App.* 调用） ----------
  App.updateExportButton = updateExportButton;
  App.downloadSession = downloadSession;
  App.loadSessionFiles = loadSessionFiles;
})(window.App);
