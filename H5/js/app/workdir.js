/**
 * 工作路径（状态栏右侧）：桌面双击内联编辑，手机单击弹窗编辑
 * - 新对话编辑全局默认目录；已有会话编辑会话独立目录（清空恢复跟随默认）
 * 依赖：app/core.js、API
 */
(function (App) {
  "use strict";
  const {
    state, toast, contextTokenWorkdir, toolTip,
    hideToolTip
  } = App;

  // ---------- 工作路径（composer 底部右侧；会话级配置） ----------
  let workDirEditBusy = false;
  let workDirEditing = false;

  function useMobileWorkDirEditor() {
    return window.matchMedia("(max-width: 600px)").matches;
  }

  function renderWorkDir(path, overridden) {
    state.workDir = path || "";
    state.workDirOverridden = Boolean(overridden);
    if (!state.workDir) {
      contextTokenWorkdir.textContent = "（未设置）";
    } else {
      // 未覆盖时显示"默认 ·"前缀，提示当前跟随全局默认目录
      contextTokenWorkdir.textContent = state.workDirOverridden
        ? state.workDir
        : "默认 · " + state.workDir;
    }
    contextTokenWorkdir.setAttribute("aria-label",
      "工作路径：" + (state.workDir || "未设置") + "；激活以编辑");
  }

  function loadWorkDir() {
    if (App.permissions) App.permissions.refresh();
    // 会话级目录：随会话切换刷新；新对话（无 sessionId）显示全局默认
    const sid = state.sessionId || "";
    API.getWorkDirConfig(sid || undefined).then(function (data) {
      if (sid && state.sessionId !== sid) return; // 等待期间已切换会话
      renderWorkDir(
        (data && (data.effective_dir || data.current_dir)) || "",
        Boolean(data && data.is_overridden)
      );
    }).catch(function () {
      contextTokenWorkdir.textContent = state.workDir || "（未设置）";
    });
  }

  function startWorkDirEdit() {
    if (workDirEditBusy || workDirEditing) return;
    const current = state.workDir || "";
    const overridden = state.workDirOverridden;
    // 新对话（尚未产生 sessionId）：编辑的是全局默认目录（新会话初始目录）；
    // 已有会话：编辑的是本会话的独立目录（清空恢复跟随默认）
    const editingGlobalDefault = !state.sessionId;
    const mobile = useMobileWorkDirEditor();
    const editor = document.createElement("input");
    editor.className = "context-token-workdir-editor";
    editor.type = "text";
    editor.value = current;
    editor.setAttribute("aria-label", "编辑会话工作路径");
    editor.setAttribute(
      "placeholder",
      editingGlobalDefault
        ? "输入目录路径，将设为新会话的默认工作目录"
        : "输入目录路径；清空恢复跟随默认"
    );
    hideToolTip();
    let backdrop = null;
    let saveButton = null;
    if (mobile) {
      backdrop = document.createElement("div");
      backdrop.className = "workdir-edit-backdrop";
      const dialog = document.createElement("div");
      dialog.className = "workdir-edit-dialog";
      dialog.setAttribute("role", "dialog");
      dialog.setAttribute("aria-modal", "true");
      const heading = document.createElement("strong");
      heading.className = "workdir-edit-title";
      heading.textContent = editingGlobalDefault ? "修改默认工作路径" : "修改本会话工作路径";
      dialog.setAttribute("aria-label", heading.textContent);
      const hint = document.createElement("p");
      hint.className = "workdir-edit-hint";
      hint.textContent = editingGlobalDefault
        ? "新会话会使用此目录。"
        : "留空并保存可恢复跟随默认目录。";
      const actions = document.createElement("div");
      actions.className = "workdir-edit-actions";
      const cancelButton = document.createElement("button");
      cancelButton.type = "button";
      cancelButton.textContent = "取消";
      saveButton = document.createElement("button");
      saveButton.type = "button";
      saveButton.className = "primary";
      saveButton.textContent = "保存";
      actions.append(cancelButton, saveButton);
      dialog.append(heading, hint, editor, actions);
      backdrop.appendChild(dialog);
      document.body.appendChild(backdrop);
      cancelButton.addEventListener("click", function () { finish(false); });
      saveButton.addEventListener("click", function () { finish(true); });
      backdrop.addEventListener("click", function (event) {
        if (event.target === backdrop) finish(false);
      });
    } else {
      contextTokenWorkdir.textContent = "";
      contextTokenWorkdir.appendChild(editor);
    }
    workDirEditing = true;
    editor.focus();
    editor.select();

    let finished = false;
    function closeMobileEditor() {
      if (!backdrop) return;
      backdrop.remove();
      contextTokenWorkdir.focus();
    }
    async function finish(save) {
      if (finished) return;
      finished = true;
      workDirEditing = false;
      const next = editor.value.trim();
      // 未保存、或未覆盖时清空输入：视为取消，恢复原显示
      if (!save || (!next && !overridden) || (next && next === current)) {
        renderWorkDir(current, overridden);
        closeMobileEditor();
        return;
      }
      workDirEditBusy = true;
      editor.disabled = true;
      if (saveButton) saveButton.disabled = true;
      try {
        if (editingGlobalDefault) {
          // 新对话：写全局默认 DEFAULT_CHAT_WORK_DIR，不在此时创建会话
          const data = await API.changeChatDir(next);
          if (data && data.state === "succeed") {
            renderWorkDir(data.current_dir || next, false);
            toast(data.message || "新会话默认工作目录已更新");
          } else {
            renderWorkDir(current, overridden);
            toast("更改默认工作目录失败：" + ((data && data.message) || "请确认路径存在且可访问"));
          }
          return;
        }
        // 已有会话：空输入 + 已覆盖 → 清除覆盖恢复默认；否则写入会话目录
        const data = await API.setSessionWorkDir(state.sessionId, next);
        if (data && data.state === "succeed") {
          renderWorkDir(
            (data.effective_dir || data.session_dir || next),
            Boolean(data.session_dir)
          );
          toast(data.message || "会话工作路径已更新");
        } else {
          renderWorkDir(current, overridden);
          toast("修改会话工作路径失败：" + (data && data.message || "未知错误"));
        }
      } catch (err) {
        renderWorkDir(current, overridden);
        toast("修改会话工作路径失败：" + err.message);
      } finally {
        workDirEditBusy = false;
        closeMobileEditor();
      }
    }

    editor.addEventListener("keydown", function (event) {
      if (event.key === "Enter") {
        event.preventDefault();
        finish(true);
      }
      if (event.key === "Escape") {
        event.preventDefault();
        finish(false);
      }
    });
    if (!mobile) editor.addEventListener("blur", function () { finish(true); });
  }

  contextTokenWorkdir.addEventListener("dblclick", startWorkDirEdit);
  contextTokenWorkdir.addEventListener("click", function () {
    if (useMobileWorkDirEditor()) startWorkDirEdit();
  });
  contextTokenWorkdir.addEventListener("keydown", function (event) {
    if (event.target !== contextTokenWorkdir) return;
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    startWorkDirEdit();
  });

  // hover 显示完整路径：显示在常态行上方（不遮挡），窄窗口右对齐不出界；
  // tooltip 本身也支持双击进入编辑（双保险）
  function positionWorkDirTip() {
    const rect = contextTokenWorkdir.getBoundingClientRect();
    const w = toolTip.offsetWidth;
    const h = toolTip.offsetHeight;
    let top = rect.top - h - 6;
    if (top < 8) top = rect.bottom + 6;
    let left = rect.right - w;
    if (left < 8) left = 8;
    if (left + w > window.innerWidth - 8) left = window.innerWidth - 8 - w;
    toolTip.style.left = left + "px";
    toolTip.style.top = top + "px";
  }

  contextTokenWorkdir.addEventListener("mouseenter", function () {
    if (useMobileWorkDirEditor() || workDirEditing || !state.workDir) return;
    toolTip.innerHTML = "";
    const tipBody = document.createElement("div");
    const pathLine = document.createElement("div");
    pathLine.textContent = state.workDir;
    const hintLine = document.createElement("div");
    hintLine.className = "tool-tip-hint";
    hintLine.textContent = state.sessionId
      ? "双击修改本会话工作路径（清空恢复跟随默认）"
      : "双击修改新会话的默认工作路径";
    tipBody.appendChild(pathLine);
    tipBody.appendChild(hintLine);
    tipBody.style.cursor = "pointer";
    tipBody.addEventListener("dblclick", function (event) {
      event.stopPropagation();
      hideToolTip();
      startWorkDirEdit();
    });
    toolTip.appendChild(tipBody);
    toolTip.style.display = "block";
    positionWorkDirTip();
  });
  contextTokenWorkdir.addEventListener("mouseleave", function (e) {
    const to = e.relatedTarget;
    if (to && (toolTip.contains(to) || to === toolTip)) return;
    hideToolTip();
  });


  // ---------- 导出（供其它模块经 App.* 调用） ----------
  App.loadWorkDir = loadWorkDir;
  App.startWorkDirEdit = startWorkDirEdit;
})(window.App);
