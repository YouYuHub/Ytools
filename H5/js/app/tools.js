/**
 * 工具选择模态框（“配置工具”）
 * - 工具分组渲染（内置工具伪服务固定首位）、草稿勾选、搜索过滤
 * - 会话独立选择 vs 全局默认的加载/保存、工具行描述悬浮提示
 * 依赖：app/core.js、API；App.*：builtin（内置工具注册表）、stats 模块
 */
(function (App) {
  "use strict";
  const {
    state, el, toast, $,
    toolModal, toolGroups, toolSearchInput, toolSelected,
    toolCollapseAll, toolRefresh, toolTip, hideToolTip,
    toolFollowGlobal
  } = App;
  const confirmButton = $("#toolConfirm");
  const cancelButton = $("#toolCancel");
  const closeButton = $("#toolModalClose");
  const scopeHint = $("#toolSelectionScope");
  const content = $("#toolConfigContent");
  const toolbar = toolModal.querySelector(".tool-modal-toolbar");
  const collapsedServers = new Set();
  let pendingSelectionInputs = null;
  let openVersion = 0;
  let modalOpened = false;
  let returnFocus = null;
  let background = [];
  let hadScrollLock = false;
  let tooltipTimer = null;
  let serverMetadata = Object.create(null);
  const BROWSER_PRESETS = [
    { id: "web", label: "网页操作", names: ["browser_close", "browser_handle_dialog", "browser_file_upload", "browser_drop", "browser_find", "browser_fill_form", "browser_press_key", "browser_type", "browser_navigate", "browser_navigate_back", "browser_snapshot", "browser_click", "browser_drag", "browser_hover", "browser_select_option", "browser_tabs", "browser_wait_for"] },
    { id: "debug", label: "调试诊断", names: ["browser_resize", "browser_console_messages", "browser_emulate_media", "browser_evaluate", "browser_network_requests", "browser_network_request", "browser_take_screenshot", "browser_navigate", "browser_snapshot", "browser_tabs", "browser_find", "browser_wait_for"] }
  ];

  function hideDescription() {
    if (tooltipTimer !== null) { clearTimeout(tooltipTimer); tooltipTimer = null; }
    hideToolTip();
  }

  function fitToolModal() {
    if (!modalOpened) return;
    const viewport = window.visualViewport;
    toolModal.style.setProperty("--tool-viewport-height", (viewport ? viewport.height : window.innerHeight) + "px");
    toolModal.style.setProperty("--tool-viewport-top", (viewport ? viewport.offsetTop : 0) + "px");
  }

  function setSaving(saving) {
    App.toolModalSaving = saving;
    toolModal.setAttribute("aria-busy", String(saving));
    [confirmButton, cancelButton, closeButton, toolFollowGlobal, toolRefresh].filter(Boolean).forEach(function (button) { button.disabled = saving; });
    content.inert = saving;
    toolbar.inert = saving;
  }

  App.onToolModalClosed = function () {
    openVersion++;
    hideDescription();
    if (!modalOpened) return;
    modalOpened = false;
    background.forEach(function (entry) { entry.node.inert = entry.before; });
    background = [];
    if (!hadScrollLock) document.body.classList.remove("tool-settings-open");
    const target = returnFocus && returnFocus.isConnected && returnFocus.getClientRects().length ? returnFocus : App.plusBtn;
    returnFocus = null;
    if (target && !target.inert) target.focus({ preventScroll: true });
  };
  window.addEventListener("resize", fitToolModal);
  content.addEventListener("scroll", hideDescription, { passive: true });
  if (window.visualViewport) {
    window.visualViewport.addEventListener("resize", fitToolModal);
    window.visualViewport.addEventListener("scroll", fitToolModal);
  }
  toolModal.addEventListener("keydown", function (event) {
    if (!modalOpened || event.isComposing) return;
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      App.closeToolModal();
      return;
    }
    if (event.key !== "Tab") return;
    const focusable = Array.from(toolModal.querySelectorAll("button, input, select, [tabindex]")).filter(function (node) {
      return !node.disabled && node.tabIndex >= 0 && !node.closest("[inert]") && node.getClientRects().length;
    });
    if (!focusable.length) { event.preventDefault(); return; }
    const first = focusable[0], last = focusable[focusable.length - 1];
    if (event.shiftKey && (document.activeElement === first || !toolModal.contains(document.activeElement))) {
      event.preventDefault(); last.focus();
    } else if (!event.shiftKey && (document.activeElement === last || !toolModal.contains(document.activeElement))) {
      event.preventDefault(); first.focus();
    }
  });

  // ---------- 工具选择 ----------
  function normalizeTool(rawTool) {
    if (typeof rawTool === "string") {
      return { name: rawTool, description: "", serverId: "" };
    }
    const functionInfo = rawTool && typeof rawTool.function === "object" ? rawTool.function : {};
    const name = functionInfo.name || rawTool.name || "";
    const originalName = String(rawTool.original_name || rawTool.originalName || name);
    const description = String(functionInfo.description || rawTool.description || "暂无工具描述");
    return {
      name: name,
      originalName: originalName,
      description: description + (name !== originalName ? "\n模型调用名：" + name : ""),
      serverId: String(rawTool.server_id || rawTool.serverId || rawTool.server || ""),
    };
  }

  function serverLabel(serverId) {
    if (serverId === App.BUILTIN_SERVER_KEY) return "内置工具";
    if (!serverId) return "未分组工具";
    const parts = serverId.replace(/\\/g, "/").split("/").filter(Boolean);
    return parts[parts.length - 1] || serverId;
  }

  function unique(values) {
    return Array.from(new Set(values.filter(Boolean)));
  }

  function remapToolSet(names, previousTools, nextTools) {
    const remapped = new Set();
    names.forEach(function (name) {
      if (App.isBuiltinToolName(name)) {
        remapped.add(name);
        return;
      }
      const exact = nextTools.find(function (tool) { return tool.name === name; });
      if (exact) {
        remapped.add(exact.name);
        return;
      }
      const previous = previousTools.find(function (tool) { return tool.name === name; });
      if (!previous) return;
      const matches = nextTools.filter(function (tool) {
        return tool.serverId === previous.serverId
          && tool.originalName === previous.originalName;
      });
      if (matches.length === 1) remapped.add(matches[0].name);
    });
    return remapped;
  }

  // forceRefresh 为 true 时要求后端绕过缓存强制重探（"刷新"按钮）；
  // 页面初始化等常规调用默认走后端缓存
  async function refreshChatModelConfig() {
    if (typeof App.refreshChatModelLabel !== "function") return false;
    // 传入当前会话 ID 会强制重新请求 role_info；只刷新 MCP 工具列表
    // 不会同步 models.json 中的 vision 等模型能力。
    try {
      return await App.refreshChatModelLabel(state.sessionId || undefined, true);
    } catch (_) {
      return false;
    }
  }

  async function loadTools(forceRefresh) {
    let success = true;
    const previousTools = state.tools.slice();
    // 和工具探测并行刷新当前会话的模型能力；无论工具接口成功与否，
    // 都等能力结果返回后再渲染弹窗，避免使用旧 vision 状态。
    const modelConfigRefresh = forceRefresh
      ? refreshChatModelConfig()
      : Promise.resolve(null);
    try {
      const data = await API.listTools(forceRefresh);
      state.tools = (data.tools || []).map(normalizeTool).filter(function (tool) { return tool.name; });
      state.servers = Array.isArray(data.servers) ? data.servers.map(String) : [];
      state.failedServers = Array.isArray(data.failed_servers) ? data.failed_servers.map(String) : [];
      serverMetadata = Object.assign(Object.create(null), data.server_metadata || {});
      if (pendingSelectionInputs) {
        const pending = pendingSelectionInputs;
        pendingSelectionInputs = null;
        applyToolSelectionInputs(pending);
      } else {
        state.selectedTools = remapToolSet(state.selectedTools, previousTools, state.tools);
        state.draftTools = remapToolSet(state.draftTools, previousTools, state.tools);
      }
    } catch (_) {
      success = false;
      state.tools = [];
      state.servers = [];
      state.failedServers = [];
      serverMetadata = Object.create(null);
    }
    const modelConfigRefreshed = await modelConfigRefresh;
    if (success) {
      const availableNames = new Set(state.tools.map(function (tool) { return tool.name; }));
      state.selectedTools = new Set(Array.from(state.selectedTools).filter(function (name) {
        return App.isBuiltinToolName(name) || availableNames.has(name);
      }));
      state.draftTools = new Set(Array.from(state.draftTools).filter(function (name) {
        return App.isBuiltinToolName(name) || availableNames.has(name);
      }));
    }
    if (!toolModal.classList.contains("hidden")) renderToolGroups();
    return { success: success, modelConfigRefreshed: modelConfigRefreshed };
  }

  function groupTools() {
    const groups = new Map();
    // 内置工具作为伪服务分组始终参与渲染（不依赖 MCP 服务列表）
    groups.set(App.BUILTIN_SERVER_KEY, App.BUILTIN_TOOLS);
    state.tools.forEach(function (tool) {
      // 兼容旧版后端仍返回原名的 MCP 工具；新版后端已为内置同名工具生成别名。
      if (App.isBuiltinToolName(tool.name)) return;
      const key = tool.serverId || "__unassigned__";
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(tool);
    });
    return groups;
  }

  // 按所属 MCP 服务分组当前选择（服务名 -> 工具名数组），交给后端持久化到 mcp_servers.json；
  // 内置工具归入伪服务 __builtin__（后端据此识别，见 factory/agent_runtime/builtin_tools.py）
  function buildToolSelectionInputs(selected) {
    const serverOf = new Map();
    state.tools.forEach(function (tool) {
      if (tool.serverId) serverOf.set(tool.name, tool.serverId);
    });
    const inputs = {};
    const builtinNames = [];
    Array.from(selected).sort().forEach(function (name) {
      if (App.isBuiltinToolName(name)) {
        builtinNames.push(name);
        return;
      }
      const server = serverOf.get(name);
      if (!server) return;
      if (!inputs[server]) inputs[server] = [];
      inputs[server].push(name);
    });
    if (builtinNames.length) inputs[App.BUILTIN_SERVER_KEY] = builtinNames;
    return inputs;
  }

  async function saveToolSelection(selected, sessionId) {
    // 会话内写覆盖快照（全取消 = 显式无工具模式）；新对话写全局默认。
    await API.updateToolSelection(buildToolSelectionInputs(selected), sessionId || undefined);
  }

  // 会话级「恢复跟随全局默认」：清除 _meta.tool_selection 覆盖（clear 语义），
  // 之后该会话重新跟随 mcp_servers.json 的全局 inputs；新对话无覆盖，不显示按钮
  async function followGlobalToolSelection() {
    if (!state.sessionId || App.toolModalSaving || toolRefresh.disabled) return;
    const sessionId = state.sessionId;
    setSaving(true);
    try {
      await API.updateToolSelection({}, sessionId, true);
      if (state.sessionId !== sessionId) return;
      if (!await loadToolSelection()) throw new Error("设置已恢复，但读取默认工具失败，请稍后重试");
      state.draftTools = new Set(state.selectedTools);
      renderToolGroups();
      App.refreshContextTokenStats(sessionId);
      toast("本会话已恢复跟随全局默认工具选择");
    } catch (err) {
      toast("恢复跟随全局失败：" + err.message);
    } finally {
      setSaving(false);
      if (modalOpened) closeButton.focus({ preventScroll: true });
    }
  }

  // 把保存的选择（服务名 -> 工具名数组）应用为当前选中集合（替换语义，不再与本地合并）
  function applyToolSelectionInputs(inputs) {
    const names = [];
    if (inputs && typeof inputs === "object") {
      Object.keys(inputs).forEach(function (server) {
        const list = inputs[server];
        if (Array.isArray(list)) {
          list.forEach(function (name) {
            if (typeof name !== "string" || !name) return;
            if (server === App.BUILTIN_SERVER_KEY || !state.tools.length) {
              names.push(name);
              return;
            }
            const exact = state.tools.find(function (tool) {
              return tool.serverId === server && tool.name === name;
            });
            if (exact) {
              names.push(exact.name);
              return;
            }
            // 旧选择按服务保存原始 MCP 名称；迁移到当前唯一模型名。
            const legacyMatches = state.tools.filter(function (tool) {
              return tool.serverId === server && tool.originalName === name;
            });
            if (legacyMatches.length === 1) names.push(legacyMatches[0].name);
          });
        }
      });
    }
    if (!state.tools.length) pendingSelectionInputs = inputs;
    state.selectedTools = new Set(names);
    // 工具列表已加载时先过滤掉当前不可用的工具（内置工具始终可用，不参与过滤）
    if (state.tools.length) {
      const availableNames = new Set(state.tools.map(function (tool) { return tool.name; }));
      state.selectedTools = new Set(Array.from(state.selectedTools).filter(function (name) {
        return App.isBuiltinToolName(name) || availableNames.has(name);
      }));
    }
  }

  // 会话级工具选择：随会话切换刷新；新对话（无 sessionId）加载全局默认
  function loadToolSelection() {
    const sid = state.sessionId || "";
    return API.getToolSelection(sid || undefined).then(function (data) {
      if ((state.sessionId || "") !== sid) return false; // 等待期间已切换会话
      const effective = (data && (data.effective_selection || data.inputs)) || {};
      applyToolSelectionInputs(effective);
      state.toolSelectionOverridden = Boolean(data && data.is_overridden);
      updateToolSelectionHint();
      return true;
    }).catch(function () { return false; /* 加载失败保持本地状态 */ });
  }

  // 模态框"已选 N 项"上悬浮提示当前是会话独立选择还是全局默认；
  // 「跟随全局」按钮仅在会话覆盖态显示（清除覆盖用，见 followGlobalToolSelection）
  function updateToolSelectionHint() {
    if (scopeHint) scopeHint.textContent = !state.sessionId
      ? "新会话默认 · 保存后用于后续新会话"
      : state.toolSelectionOverridden ? "当前会话 · 独立工具选择" : "当前会话 · 正在跟随全局默认";
    if (toolFollowGlobal) {
      toolFollowGlobal.hidden = !state.toolSelectionOverridden;
    }
    if (!toolSelected) return;
    toolSelected.title = state.toolSelectionOverridden
      ? "当前为该会话的独立工具选择（含显式无工具模式）；点「跟随全局」可恢复跟随默认"
      : "当前跟随全局默认工具选择；新对话中修改会更新全局默认";
  }

  function updateToolSelected() {
    toolSelected.textContent = "已选 " + state.draftTools.size + " 项";
  }

  function updateGroupCheckbox(checkbox, tools) {
    const selectedCount = tools.filter(function (tool) { return state.draftTools.has(tool.name); }).length;
    checkbox.checked = tools.length > 0 && selectedCount === tools.length;
    checkbox.indeterminate = selectedCount > 0 && selectedCount < tools.length;
    const group = checkbox.closest(".tool-group");
    if (group) {
      group.classList.toggle("has-selection", selectedCount > 0);
      const count = group.querySelector(".tool-group-count");
      if (count) { count.textContent = selectedCount + " / " + tools.length; count.title = "已选 " + selectedCount + " 项，共 " + tools.length + " 项"; }
    }
  }

  function toggleDraftTool(name, checked) {
    // 当前模型不支持视觉（vision=false）时 read_media 不可用：勾选守卫，
    // 整组全选/单独勾选/行点击三条路径都经过这里统一拦截
    if (checked && name === "read_media" && !App.getChatModelVision()) return;
    if (checked) state.draftTools.add(name);
    else state.draftTools.delete(name);
    updateToolSelected();
  }

  function renderToolGroups() {
    hideDescription();
    toolGroups.innerHTML = "";
    const query = toolSearchInput.value.trim().toLowerCase();
    const groups = groupTools();
    // 当前模型不支持视觉时 read_media 不可用：先从草稿勾选集剔除（渲染后
    // 复选框置灰，保存后不会带上），后端请求侧同样兜底过滤
    const visionBlocked = !App.getChatModelVision();
    if (visionBlocked && state.draftTools.has("read_media")) {
      state.draftTools.delete("read_media");
      updateToolSelected();
    }
    // 内置工具分组固定排首位，其后按 MCP 服务顺序
    const order = unique([App.BUILTIN_SERVER_KEY].concat(state.servers, Array.from(groups.keys())));

    if (!order.length) {
      toolGroups.appendChild(el("div", "tool-modal-empty", "暂无可用工具"));
      updateToolSelected();
      return;
    }

    order.forEach(function (serverId) {
      const sourceServerId = serverId;
      const isBrowser = Boolean((serverMetadata[serverId] || {}).browser_mode);
      const groupLabel = serverLabel(serverId);
      const allTools = groups.get(sourceServerId) || [];
      const connection = serverMetadata[sourceServerId] || {};
      const searchLabel = (isBrowser ? "浏览器 " : "") + serverId + " " + groupLabel;
      const serverMatches = query && (searchLabel + " " + (connection.connection_label || "")).toLowerCase().includes(query);
      const visibleTools = allTools.filter(function (tool) {
        return !query || serverMatches || tool.name.toLowerCase().includes(query)
          || String(tool.originalName || tool.name).toLowerCase().includes(query)
          || tool.description.toLowerCase().includes(query);
      });
      if (query && !visibleTools.length && !serverMatches) return;

      const group = el("section", "tool-group");
      group.dataset.serverId = serverId;
      group.classList.toggle("open", Boolean(query) || !collapsedServers.has(serverId));
      group.classList.toggle("has-error", state.failedServers.includes(sourceServerId));
      const head = el("div", "tool-group-head");
      const groupCheck = document.createElement("input");
      groupCheck.type = "checkbox";
      groupCheck.className = "tool-group-check";
      groupCheck.title = "选择此服务下的全部工具";
      groupCheck.setAttribute("aria-label", "选择“" + groupLabel + "”的全部工具");
      groupCheck.disabled = !allTools.length;

      const toggle = el("button", "tool-group-toggle");
      toggle.type = "button";
      toggle.innerHTML =
        '<svg class="icon tool-group-arrow" viewBox="0 0 24 24"><path d="m9 18 6-6-6-6"/></svg>' +
        '<svg class="icon tool-group-icon" viewBox="0 0 24 24"><path d="M12 3v18M5 7h14M5 17h14M7 3v4m10-4v4M7 17v4m10-4v4"/></svg>' +
        '<span class="tool-group-title"><span class="tool-group-name"></span><span class="tool-group-id"></span></span>';
      toggle.querySelector(".tool-group-name").textContent = groupLabel;
      toggle.querySelector(".tool-group-id").textContent =
        serverId === App.BUILTIN_SERVER_KEY ? "内置 · 本机执行"
        : state.failedServers.includes(sourceServerId) ? "MCP · 连接失败"
        : connection.connection_label || (serverId === serverLabel(serverId) || serverId === "__unassigned__" ? "MCP 服务" : serverId);
      if (connection.connection_hint) toggle.title = connection.connection_hint;
      toggle.setAttribute("aria-expanded", String(group.classList.contains("open")));
      toggle.addEventListener("click", function () {
        const opened = group.classList.toggle("open");
        if (opened) collapsedServers.delete(serverId); else collapsedServers.add(serverId);
        toggle.setAttribute("aria-expanded", String(opened));
        updateCollapseAll();
      });

      const count = el("span", "tool-group-count", allTools.length + " 项");
      head.appendChild(groupCheck);
      head.appendChild(toggle);
      head.appendChild(count);
      const isMcp = serverId !== App.BUILTIN_SERVER_KEY && serverId !== "__unassigned__";
      function testButton(toolName) {
        const test = el("button", "mcp-test-entry", "测试");
        test.type = "button";
        test.setAttribute("aria-label", "测试 " + serverLabel(sourceServerId) + (toolName ? " / " + toolName : " 连接"));
        test.addEventListener("click", function (event) {
          event.stopPropagation();
          if (App.mcpDiagnostics) App.mcpDiagnostics.open(sourceServerId, toolName);
        });
        return test;
      }
      if (isMcp) head.appendChild(testButton());
      group.appendChild(head);
      updateGroupCheckbox(groupCheck, allTools);

      const body = el("div", "tool-group-body");
      if (connection.profile_status) {
        const failedProfiles = connection.profile_status.filter(function (profile) { return !profile.ok; });
        if (failedProfiles.length) body.appendChild(el("p", "tool-group-connection-hint", "部分连接发现失败：" + failedProfiles.map(function (profile) { return profile.id; }).join("、") + "。当前清单仅包含发现成功的连接，请在 MCP 测试中检查。"));
      }
      if (!isBrowser && connection.mcp_profiles) body.appendChild(el("p", "tool-group-connection-hint", connection.connection_hint));
      if (isBrowser) {
        const presetBar = el("div", "tool-browser-presets");
        presetBar.setAttribute("role", "group"); presetBar.setAttribute("aria-label", groupLabel + "工具预设");
        const presets = BROWSER_PRESETS.concat([{ id: "all", label: "全部", names: null }, { id: "none", label: "清空", names: [] }]);
        presets.forEach(function (preset) {
          const desired = new Set(allTools.filter(function (tool) { return !preset.names || preset.names.includes(tool.originalName || tool.name); }).map(function (tool) { return tool.name; }));
          const selected = allTools.filter(function (tool) { return state.draftTools.has(tool.name); });
          const button = el("button", "tool-browser-preset", preset.label + (preset.id === "none" ? "" : " · " + desired.size));
          button.type = "button"; button.dataset.preset = preset.id;
          button.disabled = !allTools.length;
          button.setAttribute("aria-pressed", String(selected.length === desired.size && selected.every(function (tool) { return desired.has(tool.name); })));
          button.addEventListener("click", function () {
            if (App.toolModalSaving || toolRefresh.disabled) return;
            allTools.forEach(function (tool) { state.draftTools.delete(tool.name); });
            desired.forEach(function (name) { state.draftTools.add(name); });
            renderToolGroups();
            const currentGroup = Array.from(toolGroups.querySelectorAll(".tool-group")).find(function (node) { return node.dataset.serverId === serverId; });
            const nextButton = currentGroup && currentGroup.querySelector('[data-preset="' + preset.id + '"]');
            if (nextButton) nextButton.focus({ preventScroll: true });
          });
          presetBar.appendChild(button);
        });
        body.appendChild(presetBar);
        body.appendChild(el("p", "tool-browser-note", "预设替换本服务勾选，可继续手动调整。" + (connection.mcp_profiles ? "多个连接可同时保留；提问时指定“独立浏览器”或“已打开网页”，模型通过 mcp_profile 选择。默认连接：" + connection.default_profile + "。" : "预设不改变浏览器连接模式。")));
      }

      if (connection.browser_mode === "extension") body.appendChild(el("p", "tool-group-connection-hint",
        (connection.browser_channel || "浏览器") + " 扩展连接：首次实际调用时，请在 Playwright Extension 中授权并选择已有标签页。"));
      if (!allTools.length) {
        body.appendChild(el("div", "tool-group-empty", state.failedServers.includes(sourceServerId) ? "服务连接失败，暂无可用工具" : "暂无可用工具"));
      } else {
        visibleTools.forEach(function (tool) {
          const row = el("div", "tool-row");
          row.dataset.toolName = tool.name;
          const checkbox = document.createElement("input");
          checkbox.type = "checkbox";
          checkbox.className = "tool-row-check";
          checkbox.checked = state.draftTools.has(tool.name);
          checkbox.setAttribute("aria-label", "启用 " + (tool.originalName || tool.name));
          row.classList.toggle("is-selected", checkbox.checked);
          checkbox.addEventListener("click", function (event) { event.stopPropagation(); });
          checkbox.addEventListener("change", function () {
            toggleDraftTool(tool.name, checkbox.checked);
            checkbox.checked = state.draftTools.has(tool.name);
            row.classList.toggle("is-selected", checkbox.checked);
            updateGroupCheckbox(groupCheck, allTools);
          });

          const iconClass = "icon tool-row-icon" +
            (App.isBuiltinToolName(tool.name) ? " is-builtin-tool-icon" : "");
          row.innerHTML = App.toolIconSvg(tool.name, iconClass) + '<span class="tool-row-info"><span class="tool-row-name"></span><span class="tool-row-description"></span></span>';
          row.querySelector(".tool-row-name").textContent = tool.originalName || tool.name;
          row.querySelector(".tool-row-name").title = tool.name !== tool.originalName
            ? "模型调用名：" + tool.name
            : tool.name;
          // read_media 在当前模型不支持视觉时置灰禁用（勾选路径已统一守卫）
          const rowVisionBlocked = visionBlocked && tool.name === "read_media";
          if (rowVisionBlocked) {
            checkbox.disabled = true;
            row.classList.add("is-disabled");
            row.title = "当前模型不支持视觉（vision=false），read_media 不可用";
            row.querySelector(".tool-row-description").textContent =
              tool.description + "（当前模型不支持视觉，不可用）";
          } else {
            row.querySelector(".tool-row-description").textContent = tool.description;
          }
          row.insertBefore(checkbox, row.firstChild);
          if (isMcp) row.appendChild(testButton(tool.originalName || tool.name));
          row.addEventListener("click", function () {
            if (checkbox.disabled || App.toolModalSaving) return;
            checkbox.checked = !checkbox.checked;
            checkbox.dispatchEvent(new Event("change", { bubbles: true }));
          });
          body.appendChild(row);
        });
      }
      groupCheck.addEventListener("click", function (event) { event.stopPropagation(); });
      groupCheck.addEventListener("change", function () {
        allTools.forEach(function (tool) { toggleDraftTool(tool.name, groupCheck.checked); });
        updateGroupCheckbox(groupCheck, allTools);
        body.querySelectorAll(".tool-row-check").forEach(function (checkbox) {
          const row = checkbox.closest(".tool-row");
          const rowName = row.dataset.toolName;
          checkbox.checked = state.draftTools.has(rowName);
          row.classList.toggle("is-selected", checkbox.checked);
        });
      });
      group.appendChild(body);
      toolGroups.appendChild(group);
    });

    if (!toolGroups.children.length) {
      toolGroups.appendChild(el("div", "tool-modal-empty", "没有匹配的工具"));
    }
    updateToolSelected();
    updateCollapseAll();
  }

  function updateCollapseAll() {
    const groups = Array.from(toolGroups.querySelectorAll(".tool-group"));
    const allOpen = groups.length > 0 && groups.every(function (group) { return group.classList.contains("open"); });
    toolCollapseAll.textContent = allOpen ? "−" : "+";
    toolCollapseAll.title = allOpen ? "折叠全部" : "展开全部";
    toolCollapseAll.setAttribute("aria-label", toolCollapseAll.title);
    toolCollapseAll.disabled = !groups.length;
  }

  async function openToolModal() {
    if (App.toolModalSaving) return;
    const version = ++openVersion;
    const sessionId = state.sessionId;
    const trigger = document.activeElement;
    // 打开弹窗时重新读取当前会话模型能力，避免一直沿用页面启动时缓存的
    // vision 值；等能力刷新完成后再渲染，防止旧 false 把 read_media 从草稿移除。
    await refreshChatModelConfig();
    if (version !== openVersion || state.sessionId !== sessionId) return;
    state.draftTools = new Set(state.selectedTools);
    toolSearchInput.value = "";
    toolModal.classList.remove("hidden");
    toolModal.setAttribute("aria-hidden", "false");
    updateToolSelectionHint();
    renderToolGroups();
    content.scrollTop = 0;
    if (!modalOpened) {
      modalOpened = true;
      returnFocus = trigger;
      background = Array.from(document.body.children).filter(function (node) {
        return node !== toolModal && node !== toolTip && !["SCRIPT", "STYLE", "LINK"].includes(node.tagName);
      }).map(function (node) { const before = node.inert; node.inert = true; return { node: node, before: before }; });
      hadScrollLock = document.body.classList.contains("tool-settings-open");
      document.body.classList.add("tool-settings-open");
    }
    fitToolModal();
    // 手机不自动唤起键盘，让工具列表先占满可用空间。
    (window.innerWidth <= 600 ? closeButton : toolSearchInput).focus({ preventScroll: true });
  }

  $("#toolSearchInput").addEventListener("input", renderToolGroups);

  // 描述浮层优先放在工具行下方，避免遮住正在点击的复选框和工具行。
  function positionToolTip(row) {
    const rect = row.getBoundingClientRect();
    const w = toolTip.offsetWidth;
    const h = toolTip.offsetHeight;
    let left = (window.innerWidth - w) / 2;
    let top = rect.bottom + 8;
    if (top + h > window.innerHeight - 8) top = rect.top - h - 8;
    if (left < 8) left = 8;
    if (top < 8) top = 8;
    toolTip.style.left = left + "px";
    toolTip.style.top = top + "px";
  }

  // 事件委托：renderToolGroups 会重建行节点，绑定在容器上避免重复监听
  toolGroups.addEventListener("mouseover", function (e) {
    if (window.innerWidth <= 600 || !window.matchMedia("(hover: hover) and (pointer: fine)").matches) return;
    const row = e.target.closest(".tool-row");
    if (!row) return;
    if (e.relatedTarget && row.contains(e.relatedTarget)) return;
    hideDescription();
    const desc = row.querySelector(".tool-row-description");
    if (!desc || !desc.textContent.trim()
      || (desc.scrollWidth <= desc.clientWidth + 1 && desc.scrollHeight <= desc.clientHeight + 1)) return;
    tooltipTimer = setTimeout(function () {
      tooltipTimer = null;
      if (!modalOpened || !row.isConnected) return;
      toolTip.textContent = desc.textContent;
      toolTip.style.display = "block";
      positionToolTip(row);
    }, 350);
  });
  toolGroups.addEventListener("mouseout", function (e) {
    const to = e.relatedTarget;
    // 移入提示框时保持显示；切换工具行由 mouseover 接管计时。
    if (to && ((to.closest && to.closest(".tool-row")) || toolTip.contains(to))) return;
    hideDescription();
  });
  // 从提示框移回工具行保持显示，移出页面则隐藏
  toolTip.addEventListener("mouseout", function (e) {
    const to = e.relatedTarget;
    if (to && to.closest && to.closest(".tool-row")) return;
    hideDescription();
  });

  toolRefresh.addEventListener("click", async function () {
    if (toolRefresh.disabled) return;
    toolRefresh.disabled = true;
    toolRefresh.classList.add("is-loading");
    confirmButton.disabled = true;
    if (toolFollowGlobal) toolFollowGlobal.disabled = true;
    const result = await loadTools(true);
    toolRefresh.disabled = false;
    toolRefresh.classList.remove("is-loading");
    confirmButton.disabled = false;
    if (toolFollowGlobal) toolFollowGlobal.disabled = false;
    toast(!result.success
      ? "工具列表更新失败"
      : result.modelConfigRefreshed
        ? "工具列表和模型能力已更新"
        : "工具列表已更新，但模型能力刷新失败");
  });
  $("#toolCollapseAll").addEventListener("click", function () {
    const groups = Array.from(toolGroups.querySelectorAll(".tool-group"));
    const shouldOpen = groups.some(function (group) { return !group.classList.contains("open"); });
    groups.forEach(function (group) {
      group.classList.toggle("open", shouldOpen);
      const serverId = group.dataset.serverId;
      if (shouldOpen) collapsedServers.delete(serverId); else collapsedServers.add(serverId);
      group.querySelector(".tool-group-toggle").setAttribute("aria-expanded", String(shouldOpen));
    });
    updateCollapseAll();
  });
  confirmButton.addEventListener("click", async function () {
    if (App.toolModalSaving || toolRefresh.disabled) return;
    const sessionId = state.sessionId;
    const selected = new Set(state.draftTools);
    setSaving(true);
    confirmButton.textContent = "保存中…";
    let saved = false;
    try {
      await saveToolSelection(selected, sessionId);
      saved = true;
      if (state.sessionId === sessionId) {
        state.selectedTools = selected;
        state.toolSelectionOverridden = Boolean(sessionId);
        updateToolSelectionHint();
        App.refreshContextTokenStats(sessionId);
      }
      toast(sessionId
        ? (selected.size ? "已保存本会话工具选择（" + selected.size + " 项）" : "已保存：本会话显式无工具模式（不跟随全局默认）")
        : "已保存为新会话默认工具选择（" + selected.size + " 项）");
    } catch (err) {
      toast("工具选择保存失败：" + err.message);
    } finally {
      setSaving(false);
      confirmButton.textContent = "确定";
    }
    if (saved) App.closeToolModal();
    else if (modalOpened) confirmButton.focus({ preventScroll: true });
  });
  cancelButton.addEventListener("click", function () { App.closeToolModal(); });

  // 「跟随全局」：仅会话覆盖态显示；清除本会话独立选择（含显式无工具模式），
  // 恢复跟随 mcp_servers.json 的全局默认
  if (toolFollowGlobal) {
    toolFollowGlobal.addEventListener("click", followGlobalToolSelection);
  }


  // ---------- 导出（供其它模块经 App.* 调用） ----------
  App.loadTools = loadTools;
  App.loadToolSelection = loadToolSelection;
  App.openToolModal = openToolModal;
  App.positionToolTip = positionToolTip;
})(window.App);
