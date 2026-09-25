/**
 * 会话分享 / 加载 JSONL / 会话文件
 * - 顶栏分享与加载入口、JSONL 导入（后端优先、本地预览兜底）
 * - 会话文档上传解析（file_memory）与会话文件列表加载
 * 依赖：app/core.js、API、HistoryParser、SessionUtils；App.*：media/compaction 等模块
 */
(function (App) {
  "use strict";
  const {
    state, toast, $, el, setEmpty,
    scrollToBottom, closeMenus, docKindOf, MAX_DOC_FILE_SIZE,
    historyFileInput, fileInput, sessionActionsBtn, sessionActionsMenu,
    shareSessionItem, compactSessionItem, chatInner,
    importConflictModal, importConflictList, importConflictHint,
    importConflictSummary, importConflictSubmit
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

  /**
   * 分享当前会话：统一走 /chat_history/export_zip——
   * 后端在「单会话且 session_files 下无附件数据」时直接回 jsonl 明文，
   * 有附件（media/图片视频音频、files/文档、diffs/版本链等）时打 zip。
   * 按 Content-Type 分流落地：zip → blob 下载；jsonl → 文本下载。
   */
  async function downloadSession(sessionId) {
    sessionId = SessionUtils.sanitizeSessionId(sessionId);
    if (!sessionId || !state.hasConversation) {
      toast("当前没有可分享的对话内容");
      return;
    }
    // 本地导入尚未落盘的预览内容：仍下载内存里的 jsonl 文本
    if (sessionId === state.sessionId && state.importedHistoryText) {
      downloadText(sessionId + "_chat.jsonl", state.importedHistoryText);
      return;
    }
    try {
      const res = await fetch(API.BASE + "/chat_history/export_zip?session_ids=" + encodeURIComponent(sessionId));
      if (!res.ok) {
        let detail = "导出失败 " + res.status;
        try { detail = (await res.json()).detail || detail; } catch (_) { /* ignore */ }
        throw new Error(detail);
      }
      const contentType = res.headers.get("Content-Type") || "";
      const dispo = res.headers.get("Content-Disposition") || "";
      const m = dispo.match(/filename\*=UTF-8''([^;]+)/i);
      // 文件名 fallback 按内容类型推导：zip 分支不再退成 .jsonl 名。
      // 背景：Content-Disposition 不是 CORS 安全白名单头，file:// 等跨源
      // 场景读不到它（需后端 Access-Control-Expose-Headers 配合），此时
      // 按 Content-Type（CORS 白名单头，始终可读）推导正确的下载名
      const isZip = contentType.indexOf("zip") >= 0;
      const name = m ? decodeURIComponent(m[1]) : sessionId + (isZip ? "_chat.zip" : "_chat.jsonl");
      if (isZip) {
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = name;
        document.body.appendChild(link);
        link.click();
        link.remove();
        setTimeout(function () { URL.revokeObjectURL(url); }, 0);
        toast("已打包分享（含附件/分组数据）：" + name);
      } else {
        downloadText(name, await res.text());
      }
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

  // ---------- 导入（zip / jsonl 两阶段：预检 → 冲突决策 → 提交） ----------
  // 待导入上下文：预检结果缓存（提交时按需重传文件）
  let pendingImport = null;

  /**
   * 入口分流：.jsonl 走单会话两阶段导入；.zip 走多会话两阶段导入。
   * 本地解析兜底（后端不可用/无记录）保持原有 previewLocalHistory 行为。
   */
  async function handleHistoryFile(file) {
    const name = (file.name || "").toLowerCase();
    if (name.endsWith(".zip")) {
      await importPackageFlow(file);
      return;
    }
    try {
      const text = await file.text();
      const parsed = HistoryParser.parseHistory(text);
      if (!parsed.records.length) {
        toast("未找到可显示的会话记录");
        return;
      }
      // 会话标识以文件名为准：先本地校验内容，再走两阶段导入持久化
      const baseId = SessionUtils.sanitizeSessionId(file.name);
      const localId = baseId !== "default" ? baseId : ("imported-" + Date.now().toString(36));
      let preview;
      try {
        preview = await API.previewImport(file);
      } catch (previewErr) {
        // 后端不可用时退回本地预览
        previewLocalHistory(text, parsed, localId, "后端不可用，已本地预览：" + previewErr.message);
        return;
      }
      if (!preview.sessions.length || preview.sessions[0].imported_rounds === 0) {
        previewLocalHistory(text, parsed, localId, "文件中没有可导入的会话记录，已本地预览");
        return;
      }
      openImportConflict(preview, file);
    } catch (err) {
      toast("加载失败：" + err.message);
    }
  }

  /**
   * 打开冲突决策弹窗（无冲突的会话默认「导入」，冲突的默认「重命名」）。
   * 用户逐项选择后确认提交（import_package 第二阶段）。
   */
  function openImportConflict(preview, file) {
    const conflicts = preview.conflicts || [];
    pendingImport = { preview: preview, file: file };
    importConflictList.innerHTML = "";
    const groups = preview.groups || [];
    const groupNote = groups.length
      ? "，包含 " + groups.length + " 个分组（" + groups.map(function (g) { return g.name; })
          .slice(0, 3).join("、") + (groups.length > 3 ? " 等" : "")
        + "，导入后按分组名自动还原归属）"
      : "";
    if (preview.type === "jsonl") {
      importConflictHint.textContent = "该会话与本地已有会话同名，请选择处理方式：";
    } else {
      importConflictHint.textContent = "包内 " + preview.total + " 个会话" + groupNote
        + (conflicts.length ? "，其中 " + conflicts.length + " 个与本地同名：" : "，均可直接导入：");
    }
    preview.sessions.forEach(function (s) {
      const isConflict = s.exists;
      const row = document.createElement("div");
      row.className = "import-conflict-item" + (isConflict ? " conflict" : "");
      row.dataset.sessionId = s.session_id;
      const main = document.createElement("div");
      main.className = "ic-main";
      const title = document.createElement("span");
      title.className = "ic-title";
      title.textContent = s.title || s.session_id;
      title.title = s.session_id;
      const info = document.createElement("span");
      info.className = "ic-info";
      const bits = [];
      if (s.imported_rounds != null) bits.push(s.imported_rounds + " 轮");
      if (s.media_count) bits.push(s.media_count + " 个媒体文件");
      if (s.group_name) bits.push("分组：" + s.group_name);
      if (isConflict) bits.push("本地已存在同名会话");
      info.textContent = bits.join(" · ") || (isConflict ? "本地已存在同名会话" : "新会话");
      main.appendChild(title);
      main.appendChild(info);
      row.appendChild(main);
      if (isConflict) {
        const choice = document.createElement("select");
        choice.className = "ic-choice";
        choice.dataset.sessionId = s.session_id;
        [
          ["rename", "重命名另存"],
          ["overwrite", "覆盖本地会话"],
          ["skip", "跳过该会话"],
        ].forEach(function (pair) {
          const opt = document.createElement("option");
          opt.value = pair[0];
          opt.textContent = pair[1];
          choice.appendChild(opt);
        });
        row.appendChild(choice);
      } else {
        row.appendChild(el("span", "ic-new-tag", "新会话"));
      }
      importConflictList.appendChild(row);
    });
    importConflictSummary.textContent = conflicts.length
      ? "冲突 " + conflicts.length + " 个"
      : "共 " + preview.total + " 个会话";
    importConflictModal.classList.remove("hidden");
    importConflictModal.setAttribute("aria-hidden", "false");
  }

  /** 收集弹窗中的逐会话决策并提交导入。 */
  async function confirmImport() {
    if (!pendingImport) return;
    const ctx = pendingImport;
    importConflictSubmit.disabled = true;
    const decisions = {};
    importConflictList.querySelectorAll("select.ic-choice").forEach(function (sel) {
      decisions[sel.dataset.sessionId] = sel.value;
    });
    try {
      const result = await API.submitImport(ctx.file, "ask", decisions);
      closeImportConflictFn();
      const importedList = result.imported || [];
      const skippedList = result.skipped || [];
      const failedList = result.failed || [];
      const failedMsg = failedList.length
        ? "，失败 " + failedList.length + " 个（" + (failedList[0].session_id || "") + "…）"
        : "";
      if (!importedList.length) {
        toast("没有导入任何会话（冲突已跳过" + failedMsg + "）");
        return;
      }
      await App.loadSessions();
      // 导入可能新建/复用分组并写入归属：刷新侧边栏分组区数据（loadSessions
      // 已注入最新会话行，此处再拉注册表与归属映射并重渲染）
      if (App.sessionGroups && App.sessionGroups.refresh) await App.sessionGroups.refresh();
      // 打开最后一个导入成功的会话（多选导入时通常就是最新项）
      const last = importedList[importedList.length - 1];
      await App.openSession(SessionUtils.sanitizeSessionId(last.session_id));
      const collisionCount = importedList.filter(function (r) { return r.collision; }).length;
      const groupedCount = importedList.filter(function (r) { return r.group_name; }).length;
      let msg = "已导入 " + importedList.length + " 个会话";
      if (collisionCount) msg += "（" + collisionCount + " 个重名已另存）";
      if (groupedCount) msg += "，" + groupedCount + " 个已归入分组";
      if (skippedList.length) msg += "，跳过 " + skippedList.length + " 个";
      toast(msg + failedMsg);
    } catch (err) {
      toast("导入失败：" + err.message);
    } finally {
      importConflictSubmit.disabled = false;
    }
  }

  /** zip 分享包导入流程：预检 → （有冲突时）决策弹窗 → 提交。 */
  async function importPackageFlow(file) {
    let preview;
    try {
      preview = await API.previewImport(file);
    } catch (err) {
      toast("读取分享包失败：" + err.message);
      return;
    }
    if (!preview.total) {
      toast("分享包中没有可导入的会话");
      return;
    }
    openImportConflict(preview, file);
  }

  function closeImportConflictFn() {
    importConflictModal.classList.add("hidden");
    importConflictModal.setAttribute("aria-hidden", "true");
    importConflictList.innerHTML = "";
    pendingImport = null;
  }

  // 本地预览 jsonl（后端不可用 / 无合法轮次时兜底）
  function previewLocalHistory(text, parsed, sessionId, toastMsg) {
    App.resetContextTokenStats();
    state.sessionId = sessionId;
    // 预览内容已直接铺进聊天区：标记视图就绪，重复点击该会话行不再重载
    if (App.markSessionViewReady) App.markSessionViewReady(sessionId);
    if (App.fileHistory && App.fileHistory.noteSessionChanged) App.fileHistory.noteSessionChanged();
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
    // 支持一次选择多个文件（批量 zip 分享包 / 多个 jsonl），逐个走导入流程
    const files = Array.from(historyFileInput.files || []);
    historyFileInput.value = "";
    if (!files.length) return;
    if (files.length === 1) {
      await handleHistoryFile(files[0]);
      return;
    }
    toast("正在导入 " + files.length + " 个文件…");
    let okCount = 0;
    let failCount = 0;
    for (const f of files) {
      try {
        await handleHistoryFile(f);
        okCount++;
      } catch (_) {
        failCount++;
      }
    }
    if (failCount) toast("批量导入完成：成功 " + okCount + " 个，失败 " + failCount + " 个");
  });

  importConflictSubmit.addEventListener("click", function () { confirmImport(); });

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
    const pendingDocs = parseable.map(function (file) {
      return { id: Date.now().toString(36) + Math.random().toString(36).slice(2),
        sessionId: uploadSessionId, filename: file.name, phase: "upload", percent: 0 };
    });
    state.pendingDocs.push.apply(state.pendingDocs, pendingDocs);
    App.renderComposerAttachments();
    try {
      const res = await API.uploadSessionFiles(uploadSessionId, parseable, function (index, progress) {
        Object.assign(pendingDocs[index], progress);
        if (state.sessionId === uploadSessionId) App.renderComposerAttachments();
      });
      toast("上传完成：成功 " + (res.success || 0) + " 个，失败 " + (res.failed || 0) + " 个");
      await loadSessionFiles();
    } catch (err) {
      toast("上传失败：" + err.message);
    } finally {
      state.pendingDocs = state.pendingDocs.filter(function (item) { return pendingDocs.indexOf(item) < 0; });
      App.renderComposerAttachments();
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
  // 导入冲突弹窗的关闭入口（core.js 的 Esc / 背景点击经此调用）
  App.closeImportConflict = closeImportConflictFn;
})(window.App);
