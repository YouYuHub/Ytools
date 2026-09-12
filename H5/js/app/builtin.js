/**
 * 内置工具交互（服务端本地执行）
 * - todo_write：任务计划徽标与面板渲染（SSE todo 事件驱动）
 * - ask_user：提问卡片（聊天流内）+ 提问模态框，回答作为下一条消息发送
 * 依赖：app/core.js；App.*：messages（问题导航）、chat（发送）模块
 */
(function (App) {
  "use strict";
  const {
    state, el, toast, $,
    input, askModal, askQuestions, askSubmit,
    contextTokenTodoSlot, todoPanelHost
  } = App;

  // ---------- 内置工具（服务端本地执行：todo_write / ask_user，后续可扩展 subAgent 等） ----------
  // 内置工具并入“配置工具”模态框：作为伪服务 __builtin__ 下的第一组参与勾选，
  // 保存/加载与 MCP 工具走同一链路（会话独立 _meta.tool_selection / 全局默认 inputs），
  // 后端按名称识别并注入（与 factory/agent_runtime/builtin_tools.py 对应）
  const TODO_TOOL_NAME = "todo_write";
  const ASK_USER_TOOL_NAME = "ask_user";
  const WRITE_FILE_TOOL_NAME = "write_file";
  const EDIT_FILE_TOOL_NAME = "edit_file";
  const READ_FILE_TOOL_NAME = "read_file";
  const SEARCH_FILES_TOOL_NAME = "search_files";
  const READ_MEDIA_TOOL_NAME = "read_media";
  const BUILTIN_SERVER_KEY = "__builtin__";
  const BUILTIN_TOOLS = [
    {
      name: TODO_TOOL_NAME,
      description: "任务计划：模型自我规划多步骤任务并实时更新计划清单，状态栏可查看进度",
    },
    {
      name: ASK_USER_TOOL_NAME,
      description: "向用户提问：模型遇到关键分歧时弹出提问卡片，你点选选项或输入回答后任务继续",
    },
    {
      name: WRITE_FILE_TOOL_NAME,
      description: "写入文件（内置）：整文件覆盖或追加，自动创建目录；相对路径基于会话工作目录",
    },
    {
      name: EDIT_FILE_TOOL_NAME,
      description: "编辑文件（内置）：按精确字符串替换，比按行号改写更安全；为后续文件 diff 预留",
    },
    {
      name: READ_FILE_TOOL_NAME,
      description: "读取文件（内置）：带行号与编码探测的分段读取，二进制拒绝；为后续文件 diff 预留",
    },
    {
      name: SEARCH_FILES_TOOL_NAME,
      description: "跨文件搜索（内置）：类 grep 的正则逐行匹配，跳过依赖目录与大文件",
    },
    {
      name: READ_MEDIA_TOOL_NAME,
      description: "读取媒体（内置）：模型读取当前任务消息中的图片/视频（≤5 个）并以视觉形式观察",
    },
  ];

  function isBuiltinToolName(name) {
    return BUILTIN_TOOLS.some(function (tool) { return tool.name === name; });
  }

  function renderTodoWidget() {
    if (!contextTokenTodoSlot || !todoPanelHost) return;
    const todos = Array.isArray(state.todoTodos) ? state.todoTodos : [];
    // 按钮：放在状态栏左侧预留槽
    contextTokenTodoSlot.innerHTML = "";
    // 面板：独立容器参与 composer-wrap 垂直布局（占位、不遮挡输入框），
    // 位于状态栏与输入框之间；最多完整显示 6 条，超出部分面板内滚动
    if (!todos.length) {
      todoPanelHost.innerHTML = "";
      todoPanelHost.classList.add("hidden");
      return;
    }
    const done = todos.filter(function (item) { return item.status === "done"; }).length;
    const toggle = el("button", "todo-toggle" + (state.todoPanelOpen ? " open" : ""),
      "📋 计划 " + done + "/" + todos.length);
    // el() 走 textContent（安全文本），SVG 需按 HTML 插入才会渲染
    toggle.insertAdjacentHTML("beforeend",
      '<svg class="icon todo-chevron" viewBox="0 0 24 24"><path d="m6 9 6 6 6-6"/></svg>');
    toggle.title = "任务计划（模型自我规划），点击展开/收起";
    toggle.addEventListener("click", function () {
      state.todoPanelOpen = !state.todoPanelOpen;
      renderTodoWidget();
    });
    contextTokenTodoSlot.appendChild(toggle);
    todoPanelHost.innerHTML = "";
    if (state.todoPanelOpen) {
      todos.forEach(function (item) {
        const row = el("div", "todo-row todo-" + (item.status || "pending"));
        const icon = item.status === "done" ? "✓" : (item.status === "in_progress" ? "▶" : "○");
        row.appendChild(el("span", "todo-row-icon", icon));
        row.appendChild(el("span", "todo-row-text", item.content || ""));
        todoPanelHost.appendChild(row);
      });
      todoPanelHost.classList.remove("hidden");
    } else {
      todoPanelHost.classList.add("hidden");
    }
  }

  function applySessionTodo(todos) {
    state.todoTodos = Array.isArray(todos) ? todos : [];
    renderTodoWidget();
  }

  // ---------- 模型提问交互（ask_user：问题卡片 + 回答作为下一条消息） ----------

  // 判断提问卡片是否仍可回答：其后只能出现“针对提问的回答”（前缀匹配）与模型回复，
  // 一旦出现普通用户消息（开启新任务）或更新的提问卡片，即视为过期不可再答
  function isAskBlockAnswerable(block) {
    const chat = block.closest("#chatInner");
    if (!chat) return true;
    // 卡片可能位于 .msg 内（实时流）或直接挂在 #chatInner 下（历史回放）：
    // 统一向上找到 #chatInner 的直接子节点作为定位锚点
    let anchor = block;
    while (anchor.parentElement && anchor.parentElement !== chat) {
      anchor = anchor.parentElement;
    }
    const msgs = Array.from(chat.children);
    const idx = msgs.indexOf(anchor);
    if (idx < 0) return true;
    for (let i = idx + 1; i < msgs.length; i++) {
      const node = msgs[i];
      if (node.querySelector(".ask-block")) return false;
      const bubble = node.querySelector(".msg-user .msg-bubble");
      if (bubble && bubble.textContent.indexOf("【回答模型提问】") !== 0) return false;
    }
    return true;
  }

  // 按最新对话状态刷新所有提问卡片：过期卡片保留展示但不可再答
  function refreshAskBlockStates() {
    document.querySelectorAll("#chatInner .ask-block").forEach(function (block) {
      const answerable = isAskBlockAnswerable(block);
      block.classList.toggle("ask-block-stale", !answerable);
      const hint = block.querySelector(".ask-block-hint");
      if (hint) {
        hint.textContent = answerable
          ? "点击本卡片可重新打开回答窗口"
          : "已有后续对话，该提问不可再次回答";
      }
    });
  }

  // 聊天流内静态卡片：仅展示问题与选项，交互在弹窗中完成；
  // 点击卡片可重新打开回答窗口（仅最新提问可答，过期卡片点击给出提示）
  function buildAskBlock(questions) {
    const wrap = el("div", "ask-block ask-block-clickable");
    wrap.appendChild(el("div", "ask-block-title", "🙋 模型向你提问（回答后任务继续）"));
    (Array.isArray(questions) ? questions : []).forEach(function (q, index) {
      let text = (index + 1) + ". " + (q.question || "");
      if (Array.isArray(q.options) && q.options.length) {
        text += "（选项：" + q.options.join(" / ") + "）";
      }
      wrap.appendChild(el("div", "ask-block-question", text));
    });
    wrap.appendChild(el("div", "ask-block-hint", "点击本卡片可重新打开回答窗口"));
    if (Array.isArray(questions) && questions.length) {
      wrap.addEventListener("click", function () {
        if (!isAskBlockAnswerable(wrap)) {
          toast("该提问已有后续对话，不能再次回答");
          return;
        }
        openAskModal(questions, wrap);
      });
    }
    return wrap;
  }

  // 从 ask_user 工具入参中解析问题列表（历史回放用）；非法返回 null
  function parseAskQuestionsFromArgs(args) {
    try {
      const data = typeof args === "string" ? JSON.parse(args) : args;
      const questions = data && Array.isArray(data.questions) ? data.questions : null;
      if (!questions || !questions.length) return null;
      const valid = questions.every(function (q) {
        return q && typeof q.question === "string" && q.question.trim();
      });
      return valid ? questions : null;
    } catch (_) {
      return null;
    }
  }

  function renderAskModal(questions) {
    askQuestions.innerHTML = "";
    (Array.isArray(questions) ? questions : []).forEach(function (q, index) {
      const item = el("div", "ask-question");
      const multi = !!q.multiple;
      item.appendChild((function () {
        const row = el("div", "ask-question-text", (index + 1) + ". " + (q.question || ""));
        if (multi) row.appendChild(el("span", "ask-multi-tag", "可多选"));
        return row;
      })());
      const optionsWrap = el("div", "ask-options");
      const custom = document.createElement("input");
      custom.type = "text";
      custom.className = "ask-custom";
      custom.placeholder = "或输入自定义回答…";
      custom.maxLength = 500;
      const clearSelected = function () {
        optionsWrap.querySelectorAll(".ask-option.selected").forEach(function (other) {
          other.classList.remove("selected");
        });
      };
      (Array.isArray(q.options) ? q.options : []).forEach(function (option) {
        const chip = el("button", "ask-option", option);
        chip.type = "button";
        chip.addEventListener("click", function () {
          if (multi) {
            chip.classList.toggle("selected");
          } else {
            const wasSelected = chip.classList.contains("selected");
            clearSelected();
            if (!wasSelected) chip.classList.add("selected");
          }
          // 有点选时清空自定义输入，保证每题答案来源唯一
          if (optionsWrap.querySelector(".ask-option.selected")) custom.value = "";
          updateAskSubmitState();
        });
        optionsWrap.appendChild(chip);
      });
      // 自定义输入优先：输入内容时取消点选，保证每题答案来源唯一
      custom.addEventListener("input", function () {
        if (custom.value.trim()) clearSelected();
        updateAskSubmitState();
      });
      if (optionsWrap.childElementCount) item.appendChild(optionsWrap);
      item.appendChild(custom);
      askQuestions.appendChild(item);
    });
    updateAskSubmitState();
  }

  // 每题都有答案（点选或自定义输入）才允许提交
  function updateAskSubmitState() {
    if (!askSubmit) return;
    const items = askQuestions.querySelectorAll(".ask-question");
    let complete = items.length > 0;
    items.forEach(function (item) {
      const hasSelection = !!item.querySelector(".ask-option.selected");
      const custom = item.querySelector(".ask-custom");
      const hasCustom = !!(custom && custom.value.trim());
      if (!hasSelection && !hasCustom) complete = false;
    });
    askSubmit.disabled = !complete;
  }

  function openAskModal(questions, sourceBlock) {
    state.pendingAskQuestions = Array.isArray(questions) ? questions : [];
    state.pendingAskBlock = sourceBlock || null;
    renderAskModal(state.pendingAskQuestions);
    askModal.classList.remove("hidden");
    askModal.setAttribute("aria-hidden", "false");
  }

  function closeAskModal() {
    askModal.classList.add("hidden");
    askModal.setAttribute("aria-hidden", "true");
    state.pendingAskQuestions = null;
    state.pendingAskBlock = null;
  }

  // 覆盖式重答：移除提问卡片之后的所有轮次节点（旧回答气泡与模型回复），
  // 与后端 truncate_rounds_for_reanswer 的 JSONL 截断保持一一对应
  function clearNodesAfterAskBlock(block) {
    if (!block || !block.isConnected) return;
    const chat = block.closest("#chatInner");
    if (!chat) return;
    let anchor = block;
    while (anchor.parentElement && anchor.parentElement !== chat) {
      anchor = anchor.parentElement;
    }
    if (anchor.parentElement !== chat) return;
    let node = anchor.nextElementSibling;
    while (node) {
      const next = node.nextElementSibling;
      node.remove();
      node = next;
    }
    App.rebuildQnav();
  }

  function handleAskSubmit() {
    const questions = state.pendingAskQuestions;
    if (!Array.isArray(questions) || !questions.length) {
      closeAskModal();
      return;
    }
    // 有未作答的题目时禁止提交（按钮同时处于禁用态，此处为兜底）
    if (askSubmit && askSubmit.disabled) return;
    // 会话仍在输出时不允许提交：覆盖式回答需要确定的历史边界，
    // 等当前流结束后重新点击提问卡片提交即可
    if (state.streaming && state.streamingSession === state.sessionId) {
      toast("当前会话仍在输出，请稍后再提交回答");
      return;
    }
    const items = askQuestions.querySelectorAll(".ask-question");
    const lines = ["【回答模型提问】"];
    questions.forEach(function (q, index) {
      let answer = "";
      const item = items[index];
      if (item) {
        const custom = item.querySelector(".ask-custom");
        if (custom && custom.value.trim()) {
          answer = custom.value.trim();
        } else {
          // 多选题用顿号拼接所有选中项
          answer = Array.from(item.querySelectorAll(".ask-option.selected"))
            .map(function (chip) { return chip.textContent; })
            .join("、");
        }
      }
      lines.push((index + 1) + ". " + (q.question || ""));
      lines.push("   答：" + (answer || "（未回答）"));
    });
    const text = lines.join("\n");
    const sourceBlock = state.pendingAskBlock;
    closeAskModal();
    // 先清除提问卡片之后的旧回答轮（覆盖而非追加），再发送新回答
    clearNodesAfterAskBlock(sourceBlock);
    input.value = text;
    App.send();
  }
  askSubmit.addEventListener("click", handleAskSubmit);
  $("#askModalClose").addEventListener("click", closeAskModal);
  $("#askModalBackdrop").addEventListener("click", closeAskModal);


  // ---------- 导出（供其它模块经 App.* 调用） ----------
  App.TODO_TOOL_NAME = TODO_TOOL_NAME;
  App.ASK_USER_TOOL_NAME = ASK_USER_TOOL_NAME;
  App.BUILTIN_SERVER_KEY = BUILTIN_SERVER_KEY;
  App.BUILTIN_TOOLS = BUILTIN_TOOLS;
  App.isBuiltinToolName = isBuiltinToolName;
  App.renderTodoWidget = renderTodoWidget;
  App.applySessionTodo = applySessionTodo;
  App.buildAskBlock = buildAskBlock;
  App.parseAskQuestionsFromArgs = parseAskQuestionsFromArgs;
  App.refreshAskBlockStates = refreshAskBlockStates;
  App.openAskModal = openAskModal;
  App.closeAskModal = closeAskModal;
})(window.App);
