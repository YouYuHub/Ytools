/**
 * Skills 提示词库：管理 prompt/md_files/ 下的可复用 Markdown 提示词
 * - 「+ 更多功能 → Skills 提示词」打开可拖拽对话框：左侧文件列表，右侧
 *   预览 / 编辑单栏切换（Typora 风格合并视图）：编辑视图带格式工具栏，
 *   选中文本即可加粗/斜体/行内代码、设置 H1-H3、插入表格/代码块/列表/引用/链接
 * - 新建 / 保存（Ctrl+S）/ 删除（二次确认）/ 加载到输入框（填进消息框直接发起对话）
 * - 未保存修改暂存在内存草稿 drafts：切换文件、关闭弹窗都不丢，保存后清掉
 * 依赖：app/core.js、Markdown、API；App.*：core（closeMenus/toast）
 */
(function (App) {
  "use strict";
  const { $, el, toast, closeMenus, input } = App;

  // ---------- 元素 ----------
  const modal = $("#skillsModal");
  const dialog = $("#skillsDialog");
  const dragBar = $("#skillsDragBar");
  const backdrop = $("#skillsBackdrop");
  const closeBtn = $("#skillsClose");
  const dirtyFlag = $("#skillsDirtyFlag");
  const listEl = $("#skillsList");
  const newBtn = $("#skillsNewBtn");
  const newRow = $("#skillsNewRow");
  const newNameInput = $("#skillsNewName");
  const newOkBtn = $("#skillsNewOk");
  const newCancelBtn = $("#skillsNewCancel");
  const fileNameLabel = $("#skillsFileName");
  const tabPreview = $("#skillsTabPreview");
  const tabEdit = $("#skillsTabEdit");
  const deleteBtn = $("#skillsDeleteBtn");
  const saveBtn = $("#skillsSaveBtn");
  const loadBtn = $("#skillsLoadBtn");
  const uploadBtn = $("#skillsUploadBtn");
  const uploadInput = $("#skillsUploadInput");

  // 上传大小上限：与后端 MAX_CONTENT_BYTES 一致
  const MAX_UPLOAD_BYTES = 512 * 1024;
  const previewPane = $("#skillsPreview");
  const editWrap = $("#skillsEditWrap");
  const editor = $("#skillsEditor");
  const mdBar = $("#skillsMdBar");
  const tablePicker = $("#skillsTablePicker");
  const tableGrid = $("#skillsTableGrid");
  const tableSizeLabel = $("#skillsTableSize");

  // ---------- 模块状态 ----------
  let opened = false;            // 弹窗是否可见
  let files = [];                // 服务端文件列表缓存
  let currentName = null;        // 当前文件全名（含 .md）
  let baseline = "";             // 当前文件「已保存」内容基线（脏检测）
  const drafts = new Map();      // name -> 未保存内容（切文件/关弹窗不丢）
  let viewMode = "preview";      // preview | edit
  let deleteArmed = false;       // 删除按钮二次确认
  let deleteArmTimer = null;
  let dragPos = null;            // 拖拽后的窗口位置 {left, top}（会话内记忆）
  let pickRows = 3, pickCols = 3; // 表格尺寸选择器当前选中的行列（含表头行）
  const PICK_MAX_ROWS = 6, PICK_MAX_COLS = 8;
  const isMobile = function () { return window.matchMedia("(max-width: 768px)").matches; };

  // ---------- 工具函数 ----------
  function stemOf(name) {
    return name.replace(/\.md$/i, "");
  }

  function shortTime(ts) {
    // "2026-09-06 16:00:33" → "09-06 16:00"
    return typeof ts === "string" && ts.length >= 16 ? ts.slice(5, 16) : ts;
  }

  function currentContent() {
    // editor 始终持有当前文件内容（selectFile 写入，仅编辑模式修改）
    return currentName == null ? "" : editor.value;
  }

  // ---------- 渲染 ----------
  function renderMarkdownInto(node, text) {
    node.innerHTML = Markdown.render(text || "");
    // 提示词库不支持图片：渲染结果里不会出现（渲染器无图片语法），这里兜底清理
    node.querySelectorAll("img").forEach(function (img) { img.remove(); });
  }

  function refreshDirtyUi() {
    const dirty = currentName != null && (viewMode === "edit" ? editor.value !== baseline : drafts.has(currentName));
    dirtyFlag.classList.toggle("hidden", !dirty);
    listEl.querySelectorAll(".skills-file-item").forEach(function (node) {
      const mark = node.querySelector(".skills-dirty-mark");
      if (mark) mark.classList.toggle("hidden", node.dataset.file !== currentName || !dirty);
    });
  }

  function renderList() {
    listEl.innerHTML = "";
    if (!files.length) {
      listEl.appendChild(el("li", "skills-list-empty", "还没有提示词文件，点上方「＋ 新建」创建"));
    }
    files.forEach(function (item) {
      const li = el("li", "skills-file-item" + (item.name === currentName ? " active" : ""));
      li.dataset.file = item.name;
      const nameRow = el("div", "skills-file-name", stemOf(item.name));
      nameRow.title = item.name;
      const metaRow = el("div", "skills-file-meta", shortTime(item.updated_at));
      const dot = el("span", "skills-dirty-mark hidden", "● 未保存");
      metaRow.appendChild(dot);
      li.appendChild(nameRow);
      li.appendChild(metaRow);
      li.addEventListener("click", function () { selectFile(item.name); });
      listEl.appendChild(li);
    });
  }

  function setViewMode(mode) {
    if (mode === "preview") stashDraft(); // 退出编辑时先把未保存内容存入内存草稿
    hideTablePicker();
    viewMode = mode;
    tabPreview.classList.toggle("active", mode === "preview");
    tabEdit.classList.toggle("active", mode === "edit");
    previewPane.classList.toggle("hidden", mode !== "preview");
    editWrap.classList.toggle("hidden", mode !== "edit");
    if (mode === "preview") {
      renderMarkdownInto(previewPane, currentContent());
    }
    refreshDirtyUi();
  }

  function showEmptyPane() {
    previewPane.innerHTML = "";
    previewPane.appendChild(el("div", "skills-empty", "← 从左侧选择或新建一个提示词"));
  }

  function updateToolbar() {
    const none = currentName == null;
    fileNameLabel.textContent = none ? "未选择文件" : currentName;
    saveBtn.disabled = none;
    loadBtn.disabled = none;
    deleteBtn.disabled = none;
  }

  // ---------- 数据动作 ----------
  async function refreshList(keepSelection) {
    try {
      const data = await API.listPrompts();
      files = data.prompts || [];
      if (!keepSelection && files.length) {
        await selectFile(files[0].name);
      } else if (currentName && !files.some(function (f) { return f.name === currentName; })) {
        currentName = null;
        baseline = "";
      }
      renderList();
      if (!currentName) {
        showEmptyPane();
        editor.value = "";
      }
      updateToolbar();
      refreshDirtyUi();
    } catch (err) {
      toast("加载提示词列表失败：" + err.message);
    }
  }

  async function selectFile(name) {
    stashDraft(); // 保存当前文件的未保存修改（无修改时是 no-op）
    try {
      let content;
      if (drafts.has(name)) {
        content = drafts.get(name);
      } else {
        const data = await API.readPrompt(name);
        content = data.content || "";
      }
      currentName = name;
      baseline = content;
      editor.value = content;
      fileNameLabel.textContent = name;
      renderList();
      updateToolbar();
      setViewMode("preview");
    } catch (err) {
      toast("读取提示词失败：" + err.message);
    }
  }

  function stashDraft() {
    if (currentName == null) return;
    if (editor.value !== baseline) drafts.set(currentName, editor.value);
    else drafts.delete(currentName);
  }

  // ---------- Markdown 格式工具栏（选中文本应用，保留原生撤销栈） ----------
  // 用 execCommand("insertText") 替换选区：Chromium 下会进入 textarea 原生
  // undo 历史，Ctrl+Z 可以一步步回退；不支持的环境回退 setRangeText（丢撤销）。
  function replaceSelection(text, selectStartOffset, selectLength) {
    editor.focus();
    let ok = false;
    try {
      ok = document.execCommand("insertText", false, text);
    } catch (_) { ok = false; }
    if (!ok) {
      const s = editor.selectionStart, e = editor.selectionEnd;
      editor.setRangeText(text, s, e, "end");
    }
    if (selectLength != null) {
      // 把光标/选区定位到刚插入文本的指定片段（如加粗符号内部、链接 URL 处）
      const end = editor.selectionStart;
      editor.selectionStart = end - selectStartOffset;
      editor.selectionEnd = editor.selectionStart + selectLength;
    }
  }

  // 行内包裹：**加粗** / *斜体* / `代码`；已包裹时取消包裹（toggle）
  function toggleInlineWrap(marker) {
    const s = editor.selectionStart, e = editor.selectionEnd;
    const value = editor.value;
    const selected = value.slice(s, e);
    const before = value.slice(Math.max(0, s - marker.length), s);
    const after = value.slice(e, e + marker.length);
    if (selected.startsWith(marker) && selected.endsWith(marker) && selected.length >= marker.length * 2) {
      replaceSelection(selected.slice(marker.length, selected.length - marker.length));
      return;
    }
    if (before === marker && after === marker) {
      // 选中区两侧已有包裹符：删除两侧符号
      editor.selectionStart = s - marker.length;
      editor.selectionEnd = e + marker.length;
      replaceSelection(selected);
      return;
    }
    const inner = selected || "文本";
    replaceSelection(marker + inner + marker, marker.length, inner.length);
  }

  // 逐行前缀（标题/列表/引用）：无选区时作用于光标所在行；再次应用取消前缀
  function toggleLinePrefix(mode) {
    const value = editor.value;
    let s = editor.selectionStart, e = editor.selectionEnd;
    if (s === e) { // 无选区：扩展到光标所在整行
      s = value.lastIndexOf("\n", s - 1) + 1;
      const next = value.indexOf("\n", e);
      e = next === -1 ? value.length : next;
    } else { // 有选区：扩展到覆盖的整行
      s = value.lastIndexOf("\n", s - 1) + 1;
      const next = value.indexOf("\n", e);
      e = next === -1 ? value.length : next;
    }
    const block = value.slice(s, e);
    const lines = block.split("\n");

    const prefixes = {
      h1: "# ", h2: "## ", h3: "### ",
      ul: "- ", ol: "1. ", quote: "> ",
    };
    const headingRe = /^#{1,6} /;
    const want = prefixes[mode];

    let allHave = false;
    if (mode === "h1" || mode === "h2" || mode === "h3") {
      // 标题：替换任意既有标题级别为当前级别；已全是该级别则去掉标题
      allHave = lines.length > 0 && lines.every(function (line) { return line.startsWith(want); });
      const stripped = lines.map(function (line) { return line.replace(headingRe, ""); });
      const out = allHave ? stripped : stripped.map(function (line) { return want + line; });
      editor.selectionStart = s;
      editor.selectionEnd = e;
      replaceSelection(out.join("\n"));
      return;
    }
    allHave = lines.length > 0 && lines.every(function (line) { return line.startsWith(want); });
    const out = lines.map(function (line, idx) {
      if (allHave) return line.slice(want.length);
      // 混合列表切换：先清掉其他列表/引用前缀再加目标前缀
      const cleaned = line.replace(/^\s*(?:- |\d+\. |> )/, "");
      return (mode === "ol" ? (idx + 1) + ". " : want) + cleaned;
    });
    editor.selectionStart = s;
    editor.selectionEnd = e;
    replaceSelection(out.join("\n"));
  }

  function insertBlock(text) {
    // 块级插入（表格/代码块/分割线）：保证与前后内容空行分隔
    const s = editor.selectionStart, e = editor.selectionEnd;
    const before = editor.value.slice(0, s);
    const lead = before && !before.endsWith("\n\n") ? (before.endsWith("\n") ? "\n" : "\n\n") : "";
    editor.selectionStart = s;
    editor.selectionEnd = e;
    replaceSelection(lead + text);
  }

  function applyMdAction(action) {
    if (currentName == null) { toast("未选择提示词文件"); return; }
    const sel = editor.value.slice(editor.selectionStart, editor.selectionEnd);
    switch (action) {
      case "h1": case "h2": case "h3":
      case "ul": case "ol": case "quote":
        toggleLinePrefix(action);
        break;
      case "bold": toggleInlineWrap("**"); break;
      case "italic": toggleInlineWrap("*"); break;
      case "code": toggleInlineWrap("`"); break;
      case "table":
        toggleTablePicker();
        break;
      case "codeblock": {
        const lang = "";
        const body = sel || "代码";
        insertBlock("```" + lang + "\n" + body + "\n```\n");
        break;
      }
      case "hr":
        insertBlock("---\n");
        break;
      case "link": {
        const text = sel || "链接文字";
        // 插入后选中 URL 占位，方便直接输入地址（跳过结尾右括号 1 字符）
        replaceSelection("[" + text + "](https://)", 9, "https://".length);
        break;
      }
    }
  }

  function initMdBar() {
    mdBar.addEventListener("click", function (e) {
      const btn = e.target.closest("button[data-md]");
      if (!btn) return;
      e.preventDefault();
      applyMdAction(btn.dataset.md);
    });
  }

  // ---------- 表格尺寸选择器（网格划选行列，含表头行） ----------
  function buildTable(rows, cols) {
    const head = "| " + Array.from({ length: cols }, function (_, i) { return "列" + (i + 1); }).join(" | ") + " |";
    const divider = "| " + Array.from({ length: cols }, function () { return "----"; }).join(" | ") + " |";
    const body = Array.from({ length: Math.max(0, rows - 1) }, function () {
      return "| " + Array.from({ length: cols }, function () { return "内容"; }).join(" | ") + " |";
    });
    return [head, divider].concat(body).join("\n") + "\n";
  }

  function buildTableGrid() {
    tableGrid.innerHTML = "";
    for (let r = 1; r <= PICK_MAX_ROWS; r++) {
      for (let c = 1; c <= PICK_MAX_COLS; c++) {
        const cell = document.createElement("div");
        cell.className = "skills-grid-cell";
        cell.dataset.r = r;
        cell.dataset.c = c;
        tableGrid.appendChild(cell);
      }
    }
  }

  function lightTableCells() {
    tableGrid.querySelectorAll(".skills-grid-cell").forEach(function (cell) {
      cell.classList.toggle("lit", Number(cell.dataset.r) <= pickRows && Number(cell.dataset.c) <= pickCols);
    });
    tableSizeLabel.textContent = pickRows + " 行 × " + pickCols + " 列";
  }

  function positionTablePicker() {
    const btn = mdBar.querySelector('button[data-md="table"]');
    const wrapRect = editWrap.getBoundingClientRect();
    const btnRect = btn.getBoundingClientRect();
    tablePicker.classList.remove("hidden");
    tablePicker.style.left = "0px";
    const pw = tablePicker.offsetWidth;
    const left = Math.min(Math.max(btnRect.left - wrapRect.left, 8), Math.max(wrapRect.width - pw - 8, 8));
    tablePicker.style.left = left + "px";
    tablePicker.style.top = (btnRect.bottom - wrapRect.top + 4) + "px";
  }

  function toggleTablePicker() {
    if (tablePicker.classList.contains("hidden")) {
      pickRows = 3;
      pickCols = 3;
      lightTableCells();
      positionTablePicker();
    } else {
      hideTablePicker();
    }
  }

  function hideTablePicker() {
    tablePicker.classList.add("hidden");
  }

  async function saveCurrent() {
    if (currentName == null) { toast("未选择提示词文件"); return; }
    const content = editor.value;
    try {
      await API.savePrompt(currentName, content);
      baseline = content;
      drafts.delete(currentName);
      toast("已保存 " + currentName);
      await refreshList(true);
      renderList();
      setViewMode(viewMode); // 刷新当前视图内容
      refreshDirtyUi();
    } catch (err) {
      toast("保存失败：" + err.message);
    }
  }

  function resetDeleteArm() {
    deleteArmed = false;
    clearTimeout(deleteArmTimer);
    deleteBtn.classList.remove("armed");
    deleteBtn.textContent = "删除";
  }

  async function deleteCurrent() {
    if (currentName == null) return;
    if (!deleteArmed) {
      // 二次确认：第一次点击变红「确认删除？」，3 秒内再点才真正删除
      deleteArmed = true;
      deleteBtn.classList.add("armed");
      deleteBtn.textContent = "确认删除？";
      deleteArmTimer = setTimeout(resetDeleteArm, 3000);
      return;
    }
    resetDeleteArm();
    const target = currentName;
    try {
      await API.deletePrompt(target);
      drafts.delete(target);
      currentName = null;
      baseline = "";
      editor.value = "";
      toast("已删除 " + target);
      await refreshList(false);
    } catch (err) {
      toast("删除失败：" + err.message);
    }
  }

  async function createNew() {
    const raw = newNameInput.value.trim();
    if (!raw) { toast("请先输入提示词名称"); newNameInput.focus(); return; }
    try {
      const data = await API.createPrompt(raw, "# " + raw.replace(/\.md$/i, "") + "\n\n");
      newRow.classList.add("hidden");
      newNameInput.value = "";
      toast("已创建 " + data.name);
      await refreshList(true);
      await selectFile(data.name);
      setViewMode("edit");
      editor.focus();
    } catch (err) {
      toast("创建失败：" + err.message);
    }
  }

  function loadToComposer() {
    const content = currentContent();
    if (!content.trim()) { toast("当前提示词内容为空"); return; }
    const existing = input.value.trim();
    input.value = existing ? existing + "\n\n" + content : content;
    input.dispatchEvent(new Event("input")); // 触发输入框自适应高度
    input.selectionStart = input.selectionEnd = input.value.length;
    input.focus();
    closeSkills();
    toast("已加载到输入框，可直接发送");
  }

  // ---------- 拖拽 ----------
  function applyDragPos() {
    if (!dragPos) return;
    dialog.style.position = "absolute";
    dialog.style.left = dragPos.left + "px";
    dialog.style.top = dragPos.top + "px";
    dialog.style.margin = "0";
  }

  function resetDragPos() {
    dragPos = null;
    dialog.style.position = "";
    dialog.style.left = "";
    dialog.style.top = "";
    dialog.style.margin = "";
  }

  function initDrag(e) {
    if (isMobile() || e.button !== 0) return;
    if (e.target.closest("button") || e.target.closest("input")) return;
    const rect = dialog.getBoundingClientRect();
    dragPos = { left: rect.left, top: rect.top };
    const offsetX = e.clientX - rect.left;
    const offsetY = e.clientY - rect.top;
    applyDragPos();
    dragBar.setPointerCapture(e.pointerId);

    function onMove(ev) {
      const pad = 8;
      const maxLeft = window.innerWidth - rect.width - pad;
      const maxTop = window.innerHeight - 46 - pad;
      dragPos.left = Math.min(Math.max(ev.clientX - offsetX, pad), Math.max(maxLeft, pad));
      dragPos.top = Math.min(Math.max(ev.clientY - offsetY, pad), Math.max(maxTop, pad));
      applyDragPos();
    }
    function onUp() {
      dragBar.removeEventListener("pointermove", onMove);
      dragBar.removeEventListener("pointerup", onUp);
      dragBar.removeEventListener("pointercancel", onUp);
    }
    dragBar.addEventListener("pointermove", onMove);
    dragBar.addEventListener("pointerup", onUp);
    dragBar.addEventListener("pointercancel", onUp);
  }

  // ---------- 开关 ----------
  function openSkills() {
    closeMenus();
    opened = true;
    modal.classList.remove("hidden");
    modal.setAttribute("aria-hidden", "false");
    // 记忆的拖拽位置仍完整可见时恢复，否则回到居中
    if (dragPos && !isMobile()) {
      const maxLeft = window.innerWidth - dialog.offsetWidth - 8;
      const maxTop = window.innerHeight - dialog.offsetHeight - 8;
      if (dragPos.left <= maxLeft && dragPos.top <= maxTop) applyDragPos();
      else resetDragPos();
    }
    refreshList(currentName != null);
  }

  function closeSkills() {
    stashDraft(); // 关闭也不丢未保存修改，下次打开仍显示草稿
    opened = false;
    resetDeleteArm();
    modal.classList.add("hidden");
    modal.setAttribute("aria-hidden", "true");
  }

  // 窗口尺寸变化时把记忆的拖拽位置拉回可视区
  window.addEventListener("resize", function () {
    if (!opened || !dragPos) return;
    const pad = 8;
    dragPos.left = Math.min(dragPos.left, Math.max(window.innerWidth - dialog.offsetWidth - pad, pad));
    dragPos.top = Math.min(dragPos.top, Math.max(window.innerHeight - 46 - pad, pad));
    applyDragPos();
  });

  // ---------- 事件绑定 ----------
  $("#skillsItem").addEventListener("click", function (e) {
    e.stopPropagation();
    openSkills();
  });
  closeBtn.addEventListener("click", closeSkills);
  backdrop.addEventListener("click", closeSkills);
  dragBar.addEventListener("pointerdown", initDrag);

  newBtn.addEventListener("click", function () {
    newRow.classList.toggle("hidden");
    if (!newRow.classList.contains("hidden")) newNameInput.focus();
  });
  newCancelBtn.addEventListener("click", function () {
    newRow.classList.add("hidden");
    newNameInput.value = "";
  });
  newOkBtn.addEventListener("click", createNew);
  newNameInput.addEventListener("keydown", function (e) {
    if (e.key === "Enter") createNew();
    if (e.key === "Escape") { newRow.classList.add("hidden"); newNameInput.value = ""; }
  });

  tabPreview.addEventListener("click", function () { setViewMode("preview"); });
  tabEdit.addEventListener("click", function () {
    if (currentName == null) { toast("未选择提示词文件"); return; }
    setViewMode("edit");
    editor.focus();
  });
  saveBtn.addEventListener("click", saveCurrent);
  deleteBtn.addEventListener("click", deleteCurrent);
  loadBtn.addEventListener("click", loadToComposer);

  // ---------- 上传本地文件为提示词 ----------
  // 选择后读取文本内容，以「同名 + .md」新建提示词；成功后立即选中该文件
  // 并进入预览视图（selectFile 内部 setViewMode("preview")）
  uploadBtn.addEventListener("click", function () {
    uploadInput.click();
  });
  uploadInput.addEventListener("change", async function () {
    const file = uploadInput.files && uploadInput.files[0];
    uploadInput.value = ""; // 清空以便下次可重复选择同一文件
    if (!file) return;
    if (file.size > MAX_UPLOAD_BYTES) {
      toast("文件超过大小上限（512KB）");
      return;
    }
    const stem = file.name.replace(/\.(md|markdown|txt)$/i, "").trim();
    if (!stem) {
      toast("无法从文件名生成提示词名称");
      return;
    }
    try {
      const content = await file.text();
      const data = await API.createPrompt(stem, content);
      toast("已上传 " + data.name);
      await refreshList(true);
      await selectFile(data.name);
      setViewMode("preview"); // 上传后马上预览
    } catch (err) {
      toast("上传失败：" + err.message);
    }
  });

  editor.addEventListener("input", function () {
    refreshDirtyUi();
  });
  editor.addEventListener("keydown", function (e) {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
      e.preventDefault();
      saveCurrent();
      return;
    }
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "b") {
      e.preventDefault();
      applyMdAction("bold");
      return;
    }
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "i") {
      e.preventDefault();
      applyMdAction("italic");
    }
  });

  // 表格选择器：划过格子点亮选区，点击插入
  tableGrid.addEventListener("mouseover", function (e) {
    const cell = e.target.closest(".skills-grid-cell");
    if (!cell) return;
    pickRows = Number(cell.dataset.r);
    pickCols = Number(cell.dataset.c);
    lightTableCells();
  });
  tableGrid.addEventListener("click", function (e) {
    const cell = e.target.closest(".skills-grid-cell");
    if (!cell) return;
    insertBlock(buildTable(pickRows, pickCols));
    hideTablePicker();
  });
  // 选择器外的点击关闭（表格按钮本身由 toggle 处理）
  document.addEventListener("click", function (e) {
    if (tablePicker.classList.contains("hidden")) return;
    if (e.target.closest("#skillsTablePicker") || e.target.closest('button[data-md="table"]')) return;
    hideTablePicker();
  });

  initMdBar();
  buildTableGrid();

  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && opened) {
      if (!tablePicker.classList.contains("hidden")) {
        hideTablePicker();
        return;
      }
      closeSkills();
    }
  });

  // ---------- 导出 ----------
  App.openSkills = openSkills;
  App.closeSkills = closeSkills;
})(App);
