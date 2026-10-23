/** 输入框开头的 / 快捷菜单：只调用现有功能入口，不把命令文本发送给模型。 */
(function (App) {
  "use strict";
  const input = App.input;
  const menu = document.getElementById("slashCommandMenu");
  const list = document.getElementById("slashCommandList");
  if (!input || !menu || !list || !window.SlashCommandUtils) return;

  const Utils = window.SlashCommandUtils;
  const svg = function (body) { return '<svg viewBox="0 0 24 24" aria-hidden="true">' + body + '</svg>'; };
  const icons = {
    file: svg('<path d="M20 12.5 11.5 21a6 6 0 0 1-8.5-8.5l9-9a4 4 0 0 1 5.7 5.7l-9 9a2 2 0 0 1-2.8-2.8l8.5-8.5"/>'),
    tools: svg('<path d="M14.7 6.3a4.5 4.5 0 0 0-6 6L3 18l3 3 5.7-5.7a4.5 4.5 0 0 0 6-6L14 13l-3-3 3.7-3.7Z"/>'),
    spark: svg('<path d="m12 3 2.1 5.3 5.7.4-4.4 3.7 1.4 5.6-4.8-3.1-4.8 3.1 1.4-5.6-4.4-3.7 5.7-.4Z"/>'),
    settings: svg('<path d="M4 3v18m8-18v18m8-18v18M1 9h6m2 6h6m2-6h6"/>'),
    search: svg('<circle cx="10.5" cy="10.5" r="7"/><path d="m16 16 5 5"/>'),
    compact: svg('<path d="M4 7h16M4 17h16M8 3l4 4 4-4M8 21l4-4 4 4"/>'),
    share: svg('<path d="M12 16V3m-5 5 5-5 5 5M4 14v6h16v-6"/>'),
    import: svg('<path d="M12 3v13m-5-5 5 5 5-5M4 18v3h16v-3"/>'),
    plus: svg('<path d="M12 4v16M4 12h16"/>'),
    folder: svg('<path d="M3 6h7l2 2h9v12H3z"/>'),
    theme: svg('<path d="M20 15.5A8 8 0 0 1 8.5 4 8 8 0 1 0 20 15.5Z"/>'),
    terminal: svg('<rect x="2" y="3" width="20" height="18" rx="2"/><path d="m6 8 4 4-4 4m7 0h5"/>'),
    changes: svg('<path d="M8 3H4v18h16V7l-4-4H8Zm8 0v4h4M7 12h10M7 16h7"/>'),
    voice: svg('<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/>'),
    stop: svg('<rect x="5" y="5" width="14" height="14" rx="2"/>'),
    status: svg('<path d="M3 12h4l3 8 4-16 3 8h4"/>'),
  };
  const themePicker = document.getElementById("themePickerModal");
  const themePickerClose = document.getElementById("themePickerClose");
  const themeOptions = Array.from(themePicker.querySelectorAll(".theme-picker-option"));
  const accentOptions = Array.from(themePicker.querySelectorAll(".theme-picker-accent"));
  let themePickerReturnFocus = null;

  function refreshThemePicker() {
    const preference = ThemeManager.getPreference();
    themeOptions.forEach(function (option) {
      const selected = option.dataset.value === preference;
      option.classList.toggle("selected", selected);
      option.setAttribute("aria-pressed", String(selected));
    });
    refreshAccentPicker();
  }

  function refreshAccentPicker() {
    const accent = ThemeManager.getAccentPreference();
    accentOptions.forEach(function (option) {
      const selected = option.dataset.value === accent;
      option.classList.toggle("selected", selected);
      option.setAttribute("aria-pressed", String(selected));
    });
  }

  function closeThemePicker() {
    if (themePicker.classList.contains("hidden")) return;
    themePicker.classList.add("hidden");
    themePicker.setAttribute("aria-hidden", "true");
    const target = themePickerReturnFocus;
    themePickerReturnFocus = null;
    if (target && target.isConnected) target.focus({ preventScroll: true });
  }

  function openThemePicker() {
    App.closeMenus();
    themePickerReturnFocus = document.activeElement;
    refreshThemePicker();
    themePicker.classList.remove("hidden");
    themePicker.setAttribute("aria-hidden", "false");
    const selected = themeOptions.find(function (option) { return option.classList.contains("selected"); });
    (selected || themeOptions[0]).focus({ preventScroll: true });
  }

  themeOptions.forEach(function (option) {
    option.addEventListener("click", function () {
      ThemeManager.setPreference(option.dataset.value);
      closeThemePicker();
    });
  });
  // 强调色点击后不关窗：即时预览，可连续切换对比
  accentOptions.forEach(function (option) {
    option.addEventListener("click", function () {
      ThemeManager.setAccentPreference(option.dataset.value);
      refreshAccentPicker();
    });
  });
  themePickerClose.addEventListener("click", closeThemePicker);
  document.getElementById("themePickerBackdrop").addEventListener("click", closeThemePicker);
  document.addEventListener("themechange", refreshThemePicker);
  document.addEventListener("accentchange", refreshAccentPicker);
  themePicker.addEventListener("keydown", function (event) {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      closeThemePicker();
    } else if (event.key === "Tab") {
      const first = themePickerClose;
      const focusables = themeOptions.concat(accentOptions);
      const last = focusables[focusables.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }
  });
  App.openThemePicker = openThemePicker;

  const sessionRequired = function () {
    return App.state.sessionId && App.state.hasConversation ? "" : "请先开始会话";
  };
  const commands = [
    { id: "file", title: "上传文件", description: "添加图片、音视频或文档附件", aliases: ["附件", "上传", "attach"], icon: "file",
      run: function () { App.fileInput.click(); } },
    { id: "tools", title: "选择工具", description: "管理本次会话可用的工具", aliases: ["工具", "mcp"], icon: "tools",
      run: function () { App.openToolModal(); } },
    { id: "skills", title: "Skills 提示词", description: "打开提示词库并加载到输入框", aliases: ["提示词", "技能", "prompt"], icon: "spark",
      run: function () { App.openSkills(); } },
    { id: "template", title: "任务模板", description: "复用任务正文、模型、工具、预算与工作路径", aliases: ["模板", "任务", "templates"], icon: "spark",
      run: function () { if (App.taskTemplates) App.taskTemplates.open(); } },
    { id: "model", title: "模型与参数", description: "选择模型和生成参数", aliases: ["模型", "参数"], icon: "settings",
      run: function () { App.openModelPanel(); } },
    { id: "settings", title: "聊天设置", description: "工具权限、子智能体与压缩策略", aliases: ["设置", "权限"], icon: "settings",
      run: function () { App.openChatSettings(); } },
    { id: "search", title: "搜索历史", description: "查找会话和聊天内容", aliases: ["搜索", "查找"], icon: "search",
      run: function () { App.historySearch.open(); } },
    { id: "compact", title: "压缩对话", description: "压缩当前会话上下文；执行前会再次确认", aliases: ["压缩", "总结"], icon: "compact",
      disabledReason: sessionRequired, run: function () { App.startManualCompaction(); } },
    { id: "context", title: "上下文管理", description: "查看历史覆盖、摘要、压缩原因与远端状态", aliases: ["上下文", "摘要", "ctx"], icon: "compact",
      run: function () { App.contextPanel.open(); } },
    { id: "share", title: "分享会话", description: "导出当前会话和附件", aliases: ["分享", "导出", "export"], icon: "share",
      disabledReason: sessionRequired, run: function () { App.downloadSession(App.state.sessionId); } },
    { id: "import", title: "导入会话", description: "从 JSONL 或 ZIP 文件加载历史", aliases: ["导入", "加载", "history"], icon: "import",
      run: function () { App.openHistoryPicker(); } },
    { id: "new", title: "新聊天", description: "切换到新会话；当前草稿会保留", aliases: ["新建", "新会话"], icon: "plus",
      run: function () { App.startNewChat(); } },
    { id: "workdir", title: "工作路径", description: "编辑当前会话或新会话的工作目录", aliases: ["目录", "路径", "cwd"], icon: "folder",
      run: function () { App.startWorkDirEdit(); } },
    { id: "archives", title: "归档会话", description: "浏览、搜索与管理已归档会话", aliases: ["归档", "archive"], icon: "folder",
      run: function () { App.archives.open(); } },
    { id: "theme", title: "主题", description: "打开主题选择弹窗", aliases: ["外观", "颜色"], icon: "theme",
      run: function () { App.openThemePicker(); } },
    { id: "status", title: "会话状态显示", description: "显示/隐藏输入框上方的会话状态", aliases: ["状态", "status", "taskstatus"], icon: "status",
      run: function () {
        if (App.taskStatus && App.taskStatus.toggleStatusVisible) {
          const visible = App.taskStatus.toggleStatusVisible();
          App.toast(visible ? "已显示会话状态" : "已隐藏会话状态");
        }
      } },
    { id: "terminal", title: "后台终端", description: "查看当前会话的终端任务和输出", aliases: ["命令", "终端", "shell"], icon: "terminal",
      disabledReason: function () {
        const button = document.getElementById("terminalJobsBtn");
        return button && !button.classList.contains("hidden") ? "" : "当前没有后台终端任务";
      }, run: function () { App.terminalJobs.open(); } },
    { id: "changes", title: "文件变更", description: "查看当前会话修改的文件", aliases: ["文件", "差异", "diff"], icon: "changes",
      disabledReason: function () {
        const button = document.getElementById("fileChangesBtn");
        return button && !button.classList.contains("hidden") ? "" : "当前没有文件变更";
      }, run: function () { App.fileHistory.togglePanel(); } },
    { id: "voice", title: "语音输入", description: "开始语音听写", aliases: ["麦克风", "听写"], icon: "voice",
      disabledReason: function () { return App.voiceBtn.classList.contains("hidden") ? "生成或压缩期间不可用" : ""; },
      run: function () { App.voiceBtn.click(); } },
    { id: "stop", title: "停止生成", description: "停止当前会话的生成或压缩", aliases: ["中止", "取消"], icon: "stop",
      disabledReason: function () { return App.stopGroup.classList.contains("hidden") ? "当前没有运行中的任务" : ""; },
      run: function () { App.stopBtn.click(); } },
  ];

  let rows = [];
  let activeIndex = -1;
  let dismissedValue = null;
  let composing = false;

  function close() {
    menu.classList.add("hidden");
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
    rows = [];
    activeIndex = -1;
  }

  function position() {
    if (menu.classList.contains("hidden")) return;
    const rect = App.composer.getBoundingClientRect();
    const width = Math.min(620, rect.width, window.innerWidth - 16);
    menu.style.width = Math.max(180, width) + "px";
    menu.style.left = Math.max(8, Math.min(rect.left, window.innerWidth - width - 8)) + "px";
    // 顶栏始终占据页面最上方，菜单留出空间，避免盖住移动端汉堡按钮。
    const above = rect.top - 72;
    if (above >= 160 || above >= window.innerHeight - rect.bottom - 16) {
      menu.style.top = "auto";
      menu.style.bottom = (window.innerHeight - rect.top + 8) + "px";
      menu.style.maxHeight = Math.max(80, Math.min(480, above)) + "px";
    } else {
      menu.style.bottom = "auto";
      menu.style.top = (rect.bottom + 8) + "px";
      menu.style.maxHeight = Math.max(80, Math.min(480, window.innerHeight - rect.bottom - 16)) + "px";
    }
  }

  function setActive(index) {
    if (index < 0 || index >= rows.length || rows[index].button.disabled) return;
    activeIndex = index;
    rows.forEach(function (row, i) {
      const selected = i === index;
      row.button.classList.toggle("is-active", selected);
      row.button.setAttribute("aria-selected", String(selected));
    });
    input.setAttribute("aria-activedescendant", rows[index].button.id);
    rows[index].button.scrollIntoView({ block: "nearest" });
  }

  function moveActive(delta) {
    const enabled = rows.map(function (row, i) { return row.button.disabled ? -1 : i; }).filter(function (i) { return i >= 0; });
    if (!enabled.length) return;
    const current = enabled.indexOf(activeIndex);
    setActive(enabled[(current + delta + enabled.length) % enabled.length]);
  }

  function execute(command) {
    const trigger = Utils.readTrigger(input.value, input.selectionStart, input.selectionEnd);
    if (!trigger || (command.disabledReason && command.disabledReason())) { close(); return; }
    input.value = Utils.removeTrigger(input.value, trigger);
    input.setSelectionRange(0, 0);
    close();
    dismissedValue = null;
    input.dispatchEvent(new Event("input", { bubbles: true }));
    try {
      const result = command.run();
      if (result && typeof result.catch === "function") result.catch(function (error) {
        App.toast(error.message || "快捷命令执行失败");
      });
    } catch (error) {
      App.toast(error.message || "快捷命令执行失败");
    }
  }

  function render() {
    if (dismissedValue === input.value) return;
    const trigger = Utils.readTrigger(input.value, input.selectionStart, input.selectionEnd);
    if (!trigger) { close(); return; }
    const matches = Utils.filterCommands(commands, trigger.query);
    list.replaceChildren();
    rows = [];
    activeIndex = -1;
    matches.forEach(function (command) {
      const reason = command.disabledReason ? command.disabledReason() : "";
      const button = document.createElement("button");
      button.type = "button";
      button.tabIndex = -1;
      button.className = "slash-command-item";
      button.id = "slashCommand-" + command.id;
      button.setAttribute("role", "option");
      button.setAttribute("aria-selected", "false");
      button.disabled = Boolean(reason);
      if (reason) button.title = reason;
      const icon = document.createElement("span");
      icon.className = "slash-command-icon";
      icon.innerHTML = icons[command.icon];
      const copy = document.createElement("span");
      copy.className = "slash-command-copy";
      const title = document.createElement("span");
      title.className = "slash-command-title";
      title.textContent = command.title;
      const description = document.createElement("span");
      description.className = "slash-command-description";
      description.textContent = reason || command.description;
      copy.append(title, description);
      const name = document.createElement("span");
      name.className = "slash-command-name";
      name.textContent = "/" + command.id;
      button.append(icon, copy, name);
      button.addEventListener("mousedown", function (event) { event.preventDefault(); });
      button.addEventListener("mouseenter", function () {
        const index = rows.findIndex(function (row) { return row.button === button; });
        if (!button.disabled) setActive(index);
      });
      button.addEventListener("click", function () { execute(command); });
      list.appendChild(button);
      rows.push({ button: button, command: command });
    });
    if (!matches.length) {
      const empty = document.createElement("div");
      empty.className = "slash-command-empty";
      empty.textContent = "没有匹配的命令；继续输入可作为普通消息发送";
      list.appendChild(empty);
    }
    if (menu.classList.contains("hidden")) App.closeMenus();
    menu.classList.remove("hidden");
    input.setAttribute("aria-expanded", "true");
    position();
    const first = rows.findIndex(function (row) { return !row.button.disabled; });
    if (first >= 0) setActive(first);
    else input.removeAttribute("aria-activedescendant");
  }

  input.setAttribute("aria-haspopup", "listbox");
  input.setAttribute("aria-controls", list.id);
  input.setAttribute("aria-expanded", "false");
  input.addEventListener("compositionstart", function () { composing = true; close(); });
  input.addEventListener("compositionend", function () { composing = false; dismissedValue = null; render(); });
  input.addEventListener("input", function (event) {
    if (composing || event.isComposing) return;
    dismissedValue = null;
    render();
  });
  input.addEventListener("focus", render);
  input.addEventListener("blur", function (event) {
    if (event.relatedTarget && menu.contains(event.relatedTarget)) return;
    if (event.relatedTarget) { close(); return; }
    setTimeout(function () {
      if (document.activeElement !== input && !menu.contains(document.activeElement)) close();
    }, 0);
  });
  input.addEventListener("click", render);
  input.addEventListener("keyup", function (event) {
    if (event.key === "ArrowLeft" || event.key === "ArrowRight" || event.key === "Home" || event.key === "End") render();
  });
  input.addEventListener("keydown", function (event) {
    if (menu.classList.contains("hidden") || composing || event.isComposing || event.keyCode === 229) return;
    if (event.key === "Escape") {
      event.preventDefault(); event.stopPropagation(); dismissedValue = input.value; close(); return;
    }
    if (event.altKey || event.ctrlKey || event.metaKey) return;
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault(); event.stopPropagation(); moveActive(event.key === "ArrowDown" ? 1 : -1); return;
    }
    if ((event.key === "Enter" && !event.shiftKey) || (event.key === "Tab" && !event.shiftKey)) {
      if (activeIndex < 0) {
        if (event.key === "Enter" && rows.length) {
          event.preventDefault(); event.stopPropagation();
          App.toast(rows[0].command.disabledReason() || "此命令当前不可用");
        }
        return;
      }
      event.preventDefault(); event.stopPropagation(); execute(rows[activeIndex].command);
    }
  }, true);
  document.addEventListener("click", function (event) {
    if (event.target === input || menu.contains(event.target)) return;
    close();
  });
  window.addEventListener("resize", position);
  if (window.visualViewport) window.visualViewport.addEventListener("resize", position);
  window.addEventListener("blur", close);
  App.closeSlashCommands = close;
})(window.App);
