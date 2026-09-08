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
    toolCollapseAll, toolRefresh, toolTip, hideToolTip
  } = App;

  // ---------- 工具选择 ----------
  function normalizeTool(rawTool) {
    if (typeof rawTool === "string") {
      return { name: rawTool, description: "", serverId: "" };
    }
    const functionInfo = rawTool && typeof rawTool.function === "object" ? rawTool.function : {};
    const name = functionInfo.name || rawTool.name || "";
    return {
      name: name,
      description: String(functionInfo.description || rawTool.description || "暂无工具描述"),
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

  // forceRefresh 为 true 时要求后端绕过缓存强制重探（"刷新"按钮）；
  // 页面初始化等常规调用默认走后端缓存
  async function loadTools(forceRefresh) {
    let success = true;
    try {
      const data = await API.listTools(forceRefresh);
      state.tools = (data.tools || []).map(normalizeTool).filter(function (tool) { return tool.name; });
      state.servers = Array.isArray(data.servers) ? data.servers.map(String) : [];
      state.failedServers = Array.isArray(data.failed_servers) ? data.failed_servers.map(String) : [];
    } catch (_) {
      success = false;
      state.tools = [];
      state.servers = [];
      state.failedServers = [];
    }
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
    return success;
  }

  function groupTools() {
    const groups = new Map();
    // 内置工具作为伪服务分组始终参与渲染（不依赖 MCP 服务列表）
    groups.set(App.BUILTIN_SERVER_KEY, App.BUILTIN_TOOLS);
    state.tools.forEach(function (tool) {
      // 与内置工具同名的 MCP 工具不再重复展示（如 SysServer 的
      // read_file/write_file/edit_file/search_files 已转为内置可选）：
      // 后端按名称解析时内置版优先，勾选任一条目效果一致，这里只去重视图
      if (App.isBuiltinToolName(tool.name)) return;
      const key = tool.serverId || "__unassigned__";
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(tool);
    });
    return groups;
  }

  // 按所属 MCP 服务分组当前选择（服务名 -> 工具名数组），交给后端持久化到 mcp_servers.json；
  // 内置工具归入伪服务 __builtin__（后端据此识别，见 factory/agent_runtime/builtin_tools.py）
  function buildToolSelectionInputs() {
    const serverOf = new Map();
    state.tools.forEach(function (tool) {
      if (tool.serverId) serverOf.set(tool.name, tool.serverId);
    });
    const inputs = {};
    const builtinNames = [];
    Array.from(state.selectedTools).sort().forEach(function (name) {
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

  async function saveToolSelection() {
    try {
      // 会话内选择写入该会话（_meta.tool_selection，仅本会话生效）；
      // 新对话（尚未产生 sessionId）的选择写入全局默认（mcp_servers.json 的 inputs 键），
      // 作为之后新建会话的默认工具——与工作路径"新会话前改默认"的语义一致
      if (state.sessionId) {
        await API.updateToolSelection(buildToolSelectionInputs(), state.sessionId);
        state.toolSelectionOverridden = true;
      } else {
        await API.updateToolSelection(buildToolSelectionInputs());
        state.toolSelectionOverridden = false;
      }
      updateToolSelectionHint();
    } catch (err) {
      toast("工具选择保存失败：" + err.message);
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
            if (typeof name === "string" && name) names.push(name);
          });
        }
      });
    }
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
    API.getToolSelection(sid || undefined).then(function (data) {
      if (sid && state.sessionId !== sid) return; // 等待期间已切换会话
      const effective = (data && (data.effective_selection || data.inputs)) || {};
      applyToolSelectionInputs(effective);
      state.toolSelectionOverridden = Boolean(data && data.is_overridden);
      updateToolSelectionHint();
    }).catch(function () { /* 加载失败保持本地状态 */ });
  }

  // 模态框"已选 N 项"上悬浮提示当前是会话独立选择还是全局默认
  function updateToolSelectionHint() {
    if (!toolSelected) return;
    toolSelected.title = state.toolSelectionOverridden
      ? "当前为该会话的独立工具选择（清除全部并保存可恢复跟随全局默认）"
      : "当前跟随全局默认工具选择；新对话中修改会更新全局默认";
  }

  function updateToolSelected() {
    toolSelected.textContent = "已选 " + state.draftTools.size + " 项";
  }

  function updateGroupCheckbox(checkbox, tools) {
    const selectedCount = tools.filter(function (tool) { return state.draftTools.has(tool.name); }).length;
    checkbox.checked = tools.length > 0 && selectedCount === tools.length;
    checkbox.indeterminate = selectedCount > 0 && selectedCount < tools.length;
  }

  function toggleDraftTool(name, checked) {
    if (checked) state.draftTools.add(name);
    else state.draftTools.delete(name);
    updateToolSelected();
  }

  function renderToolGroups() {
    toolGroups.innerHTML = "";
    const query = toolSearchInput.value.trim().toLowerCase();
    const groups = groupTools();
    // 内置工具分组固定排首位，其后按 MCP 服务顺序
    const order = unique([App.BUILTIN_SERVER_KEY].concat(state.servers, Array.from(groups.keys())));

    if (!order.length) {
      toolGroups.appendChild(el("div", "tool-modal-empty", "暂无可用工具"));
      updateToolSelected();
      return;
    }

    order.forEach(function (serverId) {
      const allTools = groups.get(serverId) || [];
      const visibleTools = allTools.filter(function (tool) {
        return !query || tool.name.toLowerCase().includes(query) || tool.description.toLowerCase().includes(query);
      });
      if (query && !visibleTools.length) return;

      const group = el("section", "tool-group open");
      const head = el("div", "tool-group-head");
      const groupCheck = document.createElement("input");
      groupCheck.type = "checkbox";
      groupCheck.className = "tool-group-check";
      groupCheck.title = "选择此服务下的全部工具";
      updateGroupCheckbox(groupCheck, allTools);

      const toggle = el("button", "tool-group-toggle");
      toggle.type = "button";
      toggle.innerHTML =
        '<svg class="icon tool-group-arrow" viewBox="0 0 24 24"><path d="m9 18 6-6-6-6"/></svg>' +
        '<svg class="icon tool-group-icon" viewBox="0 0 24 24"><path d="M12 3v18M5 7h14M5 17h14M7 3v4m10-4v4M7 17v4m10-4v4"/></svg>' +
        '<span class="tool-group-title"><span class="tool-group-name"></span><span class="tool-group-id"></span></span>';
      toggle.querySelector(".tool-group-name").textContent = serverLabel(serverId);
      toggle.querySelector(".tool-group-id").textContent =
        serverId === App.BUILTIN_SERVER_KEY ? "服务端本地执行"
        : serverId === "__unassigned__" ? "" : serverId;
      toggle.addEventListener("click", function () { group.classList.toggle("open"); });

      const count = el("span", "tool-group-count", allTools.length + " 项");
      head.appendChild(groupCheck);
      head.appendChild(toggle);
      head.appendChild(count);
      group.appendChild(head);

      const body = el("div", "tool-group-body");
      if (!allTools.length) {
        body.appendChild(el("div", "tool-group-empty", state.failedServers.includes(serverId) ? "服务连接失败，暂无可用工具" : "暂无可用工具"));
      } else {
        visibleTools.forEach(function (tool) {
          const row = el("div", "tool-row");
          const checkbox = document.createElement("input");
          checkbox.type = "checkbox";
          checkbox.className = "tool-row-check";
          checkbox.checked = state.draftTools.has(tool.name);
          checkbox.addEventListener("click", function (event) { event.stopPropagation(); });
          checkbox.addEventListener("change", function () {
            toggleDraftTool(tool.name, checkbox.checked);
            updateGroupCheckbox(groupCheck, allTools);
          });

          row.innerHTML = '<svg class="icon tool-row-icon" viewBox="0 0 24 24"><path d="M14.7 6.3a4.5 4.5 0 0 0-6 6L3 18l3 3 5.7-5.7a4.5 4.5 0 0 0 6-6L14 13l-3-3 3.7-3.7Z"/></svg><span class="tool-row-info"><span class="tool-row-name"></span><span class="tool-row-description"></span></span>';
          row.querySelector(".tool-row-name").textContent = tool.name;
          row.querySelector(".tool-row-description").textContent = tool.description;
          row.insertBefore(checkbox, row.firstChild);
          row.addEventListener("click", function () {
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
          const rowName = checkbox.closest(".tool-row").querySelector(".tool-row-name").textContent;
          checkbox.checked = state.draftTools.has(rowName);
        });
      });
      group.appendChild(body);
      toolGroups.appendChild(group);
    });

    if (!toolGroups.children.length) {
      toolGroups.appendChild(el("div", "tool-modal-empty", "没有匹配的工具"));
    }
    updateToolSelected();
  }

  function openToolModal() {
    state.draftTools = new Set(state.selectedTools);
    toolSearchInput.value = "";
    toolModal.classList.remove("hidden");
    toolModal.setAttribute("aria-hidden", "false");
    renderToolGroups();
    setTimeout(function () { toolSearchInput.focus(); }, 0);
  }

  $("#toolSearchInput").addEventListener("input", renderToolGroups);

  // 水平居中显示，垂直与工具行对齐；不跟随鼠标，内容超高时可在框内滚动
  function positionToolTip(row) {
    const rect = row.getBoundingClientRect();
    const w = toolTip.offsetWidth;
    const h = toolTip.offsetHeight;
    let left = (window.innerWidth - w) / 2;
    let top = rect.top - 6;
    if (top + h > window.innerHeight - 8) top = rect.bottom - h - 8;
    if (left < 8) left = 8;
    if (top < 8) top = 8;
    toolTip.style.left = left + "px";
    toolTip.style.top = top + "px";
  }

  // 事件委托：renderToolGroups 会重建行节点，绑定在容器上避免重复监听
  toolGroups.addEventListener("mouseover", function (e) {
    const row = e.target.closest(".tool-row");
    if (!row) return;
    const desc = row.querySelector(".tool-row-description");
    if (!desc || !desc.textContent.trim()) { hideToolTip(); return; }
    toolTip.textContent = desc.textContent;
    toolTip.style.display = "block";
    positionToolTip(row);
  });
  toolGroups.addEventListener("mouseout", function (e) {
    const to = e.relatedTarget;
    // 鼠标移入提示框或另一工具行时保持显示
    if (to && ((to.closest && to.closest(".tool-row")) || toolTip.contains(to))) return;
    hideToolTip();
  });
  // 从提示框移回工具行保持显示，移出页面则隐藏
  toolTip.addEventListener("mouseout", function (e) {
    const to = e.relatedTarget;
    if (to && to.closest && to.closest(".tool-row")) return;
    hideToolTip();
  });

  toolRefresh.addEventListener("click", async function () {
    if (toolRefresh.disabled) return;
    toolRefresh.disabled = true;
    toolRefresh.classList.add("is-loading");
    const success = await loadTools(true);
    toolRefresh.disabled = false;
    toolRefresh.classList.remove("is-loading");
    toast(success ? "工具列表已更新，共 " + state.tools.length + " 项" : "工具列表更新失败");
  });
  $("#toolCollapseAll").addEventListener("click", function () {
    const groups = Array.from(toolGroups.querySelectorAll(".tool-group"));
    const shouldOpen = groups.some(function (group) { return !group.classList.contains("open"); });
    groups.forEach(function (group) { group.classList.toggle("open", shouldOpen); });
    toolCollapseAll.textContent = shouldOpen ? "−" : "+";
  });
  $("#toolConfirm").addEventListener("click", async function () {
    const savingForSession = Boolean(state.sessionId);
    state.selectedTools = new Set(state.draftTools);
    App.closeToolModal();
    App.refreshContextTokenStats(state.sessionId);
    toast(savingForSession
      ? "已保存本会话工具选择（" + state.selectedTools.size + " 项）"
      : "已保存为新会话默认工具选择（" + state.selectedTools.size + " 项）");
    await saveToolSelection();
  });


  // ---------- 导出（供其它模块经 App.* 调用） ----------
  App.loadTools = loadTools;
  App.loadToolSelection = loadToolSelection;
  App.openToolModal = openToolModal;
  App.positionToolTip = positionToolTip;
})(window.App);
