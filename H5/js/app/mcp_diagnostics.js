/** MCP connection diagnosis and explicit manual tool calls. */
(function (App) {
  "use strict";
  if (!App) return;
  const node = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
  const button = (text, handler) => { const n = node("button", "mcp-test-button", text); n.type = "button"; n.addEventListener("click", handler); return n; };
  const pretty = value => JSON.stringify(value, null, 2);
  const stages = { configuration: "配置检查", connect: "启动与连接", initialize: "协议握手", list_tools: "工具清单", validate_arguments: "参数验证", call_tool: "工具执行", queued: "等待测试" };
  const codes = { busy: "测试繁忙", timeout: "测试超时", invalid_config: "配置错误", command_not_found: "启动命令不存在",
    unsupported_transport: "不支持的连接方式", permission_denied: "权限或认证失败", transport_error: "连接中断",
    protocol_error: "协议交互失败", schema_error: "参数说明异常", invalid_arguments: "参数不符合要求", tool_not_found: "工具不存在", tool_error: "工具执行失败" };
  let opened = false, busy = false, controller = null, version = 0, previousFocus = null, background = [], lastReport = null, tools = [], previousInert = false;
  const modal = node("div", "mcp-test-modal"); modal.hidden = true;
  const backdrop = node("div", "mcp-test-backdrop");
  const dialog = node("section", "mcp-test-dialog"); dialog.setAttribute("role", "dialog"); dialog.setAttribute("aria-modal", "true"); dialog.setAttribute("aria-labelledby", "mcpTestTitle");
  const header = node("header", "mcp-test-header"), title = node("h2", "", "MCP 测试"); title.id = "mcpTestTitle";
  const closeButton = button("关闭", close); header.append(title, closeButton);
  const body = node("div", "mcp-test-body");
  const intro = node("p", "mcp-test-hint", "选择已配置的 MCP 服务，测试连接或手动调用工具。每次测试新建连接；连接测试只握手并读取工具清单。");
  const connection = node("div", "mcp-test-connection");
  const connectionHint = node("p", "mcp-test-hint"); connectionHint.id = "mcpTestConnectionHint"; connectionHint.hidden = true;
  const serverInfo = new Map();
  const server = node("select"); server.id = "mcpTestServer";
  const profileSelect = node("select"); profileSelect.id = "mcpTestProfile";
  const timeout = node("input"); timeout.id = "mcpTestTimeout"; timeout.type = "number"; timeout.min = "1"; timeout.max = "300"; timeout.value = "30";
  const probe = button("测试连接", () => run("connect")), reload = button("刷新服务", loadServers);
  function field(text, input) { const label = node("label", "mcp-test-field"); label.append(node("span", "", text), input); return label; }
  connection.append(field("MCP 服务", server), field("测试超时（秒）", timeout), probe, reload);
  const profileSelectField = field("连接配置（profile）", profileSelect); profileSelectField.hidden = true;
  const form = node("section", "mcp-test-call"); form.append(node("h3", "", "手动调用工具"));
  const tool = node("input"); tool.id = "mcpTestTool"; tool.type = "text"; tool.placeholder = "输入工具原名，或从上方列表勾选"; tool.autocomplete = "off"; tool.spellcheck = false;
  const selections = new Map();
  let pickerScroll = null;
  const pickerWrap = node("div", "mcp-test-picker-wrap"), picker = node("div", "mcp-test-picker"); picker.id = "mcpTestToolPicker"; picker.hidden = true;
  const pickerButton = button("选择工具（可多选）", () => picker.hidden ? openPicker() : closePicker()); pickerButton.id = "mcpTestChooseTools";
  pickerButton.setAttribute("aria-expanded", "false"); pickerButton.setAttribute("aria-controls", picker.id);
  const toolSearch = node("input"); toolSearch.id = "mcpTestToolSearch"; toolSearch.type = "search"; toolSearch.placeholder = "搜索工具名称或说明"; toolSearch.setAttribute("aria-label", "搜索工具名称或说明");
  const pickerActions = node("div", "mcp-test-picker-actions");
  const selectAll = button("全选筛选结果", () => {
    if (busy) return;
    filteredTools().forEach(item => selectedTools().add(item.name));
    if (!tool.value.trim() && selectedTools().size) activateTool(selectedTools().values().next().value);
    syncSelectionUI();
  });
  const clearSelection = button("清空选择", () => { if (!busy) { selectedTools().clear(); activateTool(""); } });
  pickerActions.append(selectAll, clearSelection, button("收起列表", () => { closePicker(); pickerButton.focus(); }));
  const names = node("div", "mcp-test-tool-options"); names.id = "mcpTestTools"; names.setAttribute("role", "group"); names.setAttribute("aria-label", "可选测试工具");
  picker.append(toolSearch, pickerActions, names); pickerWrap.append(pickerButton, picker);
  const selectedTabs = node("div", "mcp-test-selected-tools"); selectedTabs.setAttribute("role", "group"); selectedTabs.setAttribute("aria-label", "已选工具，点击切换参数");
  const toolCurrent = node("div", "mcp-test-tool-current");
  const addTool = button("加入测试", () => {
    const name = tool.value.trim(); if (busy || !name) return;
    selectedTools().add(name); showSchema();
  }); addTool.id = "mcpTestAddTool";
  toolCurrent.append(field("当前编辑工具原名", tool), addTool);
  const description = node("p", "mcp-test-hint");
  const descriptionDetails = node("details", "mcp-test-description"); descriptionDetails.append(node("summary", "", "工具说明"), description);
  const schemaDetails = node("details", "mcp-test-schema"), schema = node("pre"); schemaDetails.append(node("summary", "", "查看参数 Schema"), schema);
  const args = node("textarea"); args.id = "mcpTestArguments"; args.value = "{}"; args.rows = 7; args.spellcheck = false; args.setAttribute("aria-describedby", "mcpTestCallHint");
  const argumentModes = node("div", "mcp-test-modes"); argumentModes.setAttribute("role", "group"); argumentModes.setAttribute("aria-label", "参数输入方式");
  const formMode = button("表单填写", () => switchMode("form")), jsonMode = button("JSON 编辑", () => switchMode("json"));
  formMode.id = "mcpTestFormMode"; jsonMode.id = "mcpTestJsonMode"; argumentModes.append(formMode, jsonMode);
  const modeHint = node("p", "mcp-test-hint");
  const formArguments = node("fieldset", "mcp-test-form-arguments"); formArguments.id = "mcpTestFormArguments";
  const jsonArguments = field("参数（JSON 对象）", args);
  // Drafts stay in memory and are isolated by server, profile and original tool name.
  const drafts = new Map(); let draft = null, argumentMode = "json";
  const example = button("填入示例参数", fillExample), execute = button("执行测试（实际调用）", () => run("call"));
  execute.id = "mcpTestExecute";
  const continueLabel = node("label", "mcp-param-option mcp-test-continue"), continueOnError = node("input"); continueOnError.type = "checkbox"; continueOnError.id = "mcpTestContinueOnError";
  continueLabel.append(continueOnError, node("span", "", "单项失败后继续测试其他工具")); continueLabel.hidden = true;
  const callHint = node("p", "mcp-test-hint"); callHint.id = "mcpTestCallHint";
  form.append(pickerWrap, selectedTabs, toolCurrent, descriptionDetails, schemaDetails, argumentModes, modeHint, formArguments, jsonArguments, example, continueLabel, callHint, execute);
  const status = node("p", "mcp-test-status"); status.setAttribute("role", "status"); status.setAttribute("aria-live", "polite");
  const result = node("div", "mcp-test-result");
  const footer = node("footer", "mcp-test-footer");
  const stop = button("停止等待", cancel), copy = button("复制脱敏报告", copyReport), download = button("下载脱敏报告", downloadReport);
  stop.hidden = true; copy.disabled = download.disabled = true;
  footer.append(stop, copy, download);
  body.append(intro, connection, profileSelectField, connectionHint, form, status, result); dialog.append(header, body, footer); modal.append(backdrop, dialog); document.body.append(modal);
  backdrop.addEventListener("click", close);
  toolSearch.addEventListener("input", renderPicker);
  // Unlike the native datalist, this popup belongs to the scrolling body. Close
  // it when the surrounding page moves, but allow scrolling its own tool list.
  body.addEventListener("scroll", onPageScroll);
  window.addEventListener("scroll", onPageScroll, true);
  window.addEventListener("resize", () => closePicker());
  modal.addEventListener("pointerdown", event => { if (!pickerWrap.contains(event.target)) closePicker(); });

  function setBusy(value) {
    busy = value; dialog.setAttribute("aria-busy", String(value));
    [server, profileSelect, timeout, probe, reload, tool, args, example, execute, pickerButton, toolSearch, selectAll, clearSelection, continueOnError].forEach(n => { n.disabled = value; });
    if (value) closePicker();
    formArguments.disabled = value; jsonMode.disabled = value; formMode.disabled = value || !(draft && draft.editor || canGenerateForm());
    stop.hidden = !value; copy.disabled = download.disabled = !lastReport;
    syncSelectionUI();
  }
  function resetResult() { lastReport = null; result.replaceChildren(); copy.disabled = download.disabled = true; }
  function resetTools() { tools = []; toolSearch.value = ""; closePicker(); renderPicker(); showSchema(); }
  server.addEventListener("change", () => { tool.value = ""; profileSelect.value = ""; showConnectionHint(); resetTools(); resetResult(); status.textContent = "已切换服务，请测试连接或填写工具参数。"; });
  profileSelect.addEventListener("change", () => { showConnectionHint(); tool.value = ""; resetTools(); resetResult(); status.textContent = "已切换连接；各连接的工具选择和参数草稿分别保留，请测试连接获取当前清单。"; });
  tool.addEventListener("input", showSchema);
  function selectedProfileInfo() {
    const info = serverInfo.get(server.value) || {};
    return (info.mcp_profiles || []).find(entry => entry.id === profileSelect.value) || info;
  }
  function showConnectionHint() {
    const info = serverInfo.get(server.value) || {};
    const previous = profileSelect.value, entries = info.mcp_profiles || [];
    profileSelect.replaceChildren();
    entries.forEach(entry => { const option = node("option", "", entry.label + " · " + entry.id); option.value = entry.id; option.disabled = entry.available === false || !!info.locked_profile && entry.id !== info.locked_profile; profileSelect.append(option); });
    profileSelect.value = info.locked_profile || (entries.some(entry => entry.id === previous) ? previous : info.default_profile || (entries[0] || {}).id || "");
    profileSelectField.hidden = !entries.length;
    const current = selectedProfileInfo();
    connectionHint.textContent = [info.connection_hint, current !== info && (current.description || current.connection_hint)].filter(Boolean).join(" ");
    if (current.browser_mode === "extension") connectionHint.textContent += " 测试连接只检查 MCP 握手与清单。实际调用需要浏览器授权，首次测试可将超时设为 120 秒；测试结束释放此连接，聊天中使用独立的常驻连接。";
    connectionHint.hidden = !connectionHint.textContent;
  }
  function selectionKey() { return JSON.stringify([server.value, profileSelect.value]); }
  function selectedTools() {
    const key = selectionKey();
    if (!selections.has(key)) selections.set(key, new Set());
    return selections.get(key);
  }
  function activateTool(name) { tool.value = name; showSchema(); }
  function filteredTools() {
    const query = toolSearch.value.trim().toLowerCase();
    return tools.filter(item => !query || (item.name + " " + (item.description || "")).toLowerCase().includes(query));
  }
  function openPicker() {
    if (busy) return;
    toolSearch.value = ""; renderPicker(); picker.hidden = false; pickerButton.setAttribute("aria-expanded", "true");
    const anchor = pickerButton.getBoundingClientRect(), bounds = body.getBoundingClientRect();
    const below = bounds.bottom - anchor.bottom - 8, above = anchor.top - bounds.top - 8;
    const up = below < 220 && above > below; picker.classList.toggle("opens-up", up);
    picker.style.maxHeight = Math.max(90, Math.min(360, up ? above : below)) + "px";
    toolSearch.focus({ preventScroll: true });
    pickerScroll = [body.scrollTop, body.scrollLeft, window.scrollX, window.scrollY];
  }
  function onPageScroll(event) {
    if (picker.hidden || event.target instanceof Node && picker.contains(event.target)) return;
    // A click can first scroll the trigger into view; that scroll event may be
    // delivered after the popup opens. Only a subsequent position change closes it.
    if ([body, document, window].includes(event.target) && pickerScroll &&
      [body.scrollTop, body.scrollLeft, window.scrollX, window.scrollY].every((value, index) => value === pickerScroll[index])) return;
    closePicker();
  }
  function closePicker() {
    const restoreFocus = !picker.hidden && picker.contains(document.activeElement);
    picker.hidden = true; pickerScroll = null; pickerButton.setAttribute("aria-expanded", "false");
    if (restoreFocus) pickerButton.focus({ preventScroll: true });
  }
  function renderPicker() {
    names.replaceChildren();
    const found = filteredTools();
    found.forEach(item => {
      const row = node("label", "mcp-test-tool-option"), check = node("input"); check.type = "checkbox"; check.dataset.toolName = item.name;
      check.checked = selectedTools().has(item.name); check.disabled = busy;
      const info = node("span", "mcp-test-tool-option-info"); info.append(node("span", "mcp-test-tool-name", item.name));
      if (item.description) info.append(node("span", "mcp-test-tool-summary", item.description));
      row.append(check, info); names.append(row);
      check.addEventListener("change", () => {
        if (busy) return;
        if (check.checked) { selectedTools().add(item.name); activateTool(item.name); }
        else { selectedTools().delete(item.name); if (tool.value.trim() === item.name) activateTool(selectedTools().values().next().value || ""); else syncSelectionUI(); }
      });
    });
    if (!found.length) names.append(node("p", "mcp-test-hint", tools.length ? "没有匹配的工具，可修改搜索条件。" : "请先测试连接获取工具清单；也可手动输入工具原名加入测试。"));
    selectAll.disabled = busy || !found.length;
  }
  function syncSelectionUI() {
    const selected = selectedTools(), count = selected.size;
    pickerButton.textContent = "选择工具（可多选） · 已选 " + count;
    names.querySelectorAll("input[data-tool-name]").forEach(input => { input.checked = selected.has(input.dataset.toolName); input.disabled = busy; });
    selectedTabs.replaceChildren();
    [...selected].forEach((name, index) => {
      const chip = node("span", "mcp-test-tool-chip"), edit = button((index + 1) + ". " + name, () => { if (!busy) { closePicker(); activateTool(name); } });
      edit.setAttribute("aria-pressed", String(tool.value.trim() === name)); edit.setAttribute("aria-label", "编辑参数 " + name); edit.title = name; edit.disabled = busy;
      const remove = button("×", () => {
        if (busy) return;
        selected.delete(name); activateTool(tool.value.trim() === name ? selected.values().next().value || "" : tool.value.trim());
        pickerButton.focus({ preventScroll: true });
      }); remove.setAttribute("aria-label", "移除测试工具 " + name); remove.disabled = busy;
      chip.append(edit, remove); selectedTabs.append(chip);
    });
    selectedTabs.hidden = !count;
    addTool.disabled = busy || !tool.value.trim() || selected.has(tool.value.trim());
    continueLabel.hidden = count < 2;
    execute.textContent = count > 1 ? "执行 " + count + " 项测试（实际调用）" : "执行测试（实际调用）";
    callHint.textContent = (count > 1 ? "按标签顺序执行 " + count + " 项测试，每项独立连接、分别返回结果。" : "会按输入参数实际执行工具。") +
      "可能修改文件或操作外部服务。无需模型参与，也不会发送聊天消息。";
  }
  function showSchema() {
    const selected = tools.find(t => t.name === tool.value.trim());
    description.textContent = selected ? selected.description || "该工具没有描述" : "可手动输入服务提供的工具原名，执行前会重新验证。";
    schema.textContent = selected ? pretty(selected.input_schema) : "尚未获取该工具的 Schema";
    const key = JSON.stringify([server.value, profileSelect.value, tool.value.trim()]);
    if (!draft || draft.key !== key) {
      if (draft) { draft.raw = args.value; draft.mode = argumentMode; }
      draft = drafts.get(key) || { key, raw: "{}", mode: null, editor: null, signature: null };
      drafts.delete(key); drafts.set(key, draft);
      // Selected tools must keep their drafts until the entire batch is tested.
      for (const key of drafts.keys()) {
        if (drafts.size <= 32) break;
        const [serverId, profileId, name] = JSON.parse(key);
        if (key !== draft.key && !(selections.get(JSON.stringify([serverId, profileId])) || new Set()).has(name)) drafts.delete(key);
      }
      args.value = draft.raw; argumentMode = draft.mode || "json";
    }
    const builder = App.mcpSchemaForm;
    const available = selected && selected.schema_valid !== false && !selected.schema_truncated && builder && builder.supported(selected.input_schema);
    if (available) {
      const signature = pretty(selected.input_schema);
      if (!draft.editor || draft.signature !== signature) {
        try {
          const value = draft.editor && argumentMode === "form" ? draft.editor.read({ partial: true }) : parseArguments();
          draft.editor = builder.create(selected.input_schema, value); draft.signature = signature; draft.schema = selected.input_schema;
          if (!draft.mode) argumentMode = "form";
        } catch (_) { /* Preserve invalid drafts; JSON remains editable, and the old form stays intact. */ }
      }
    }
    modeHint.textContent = draft.editor ? "按字段填写，或切换 JSON；未勾选的可选参数不会发送，输入内容后会自动勾选。复杂字段可填写 JSON。" :
      available ? "当前 JSON 草稿格式有误，请修正后切换表单。" :
      selected ? "此工具的 Schema 不完整或结构较复杂，请使用 JSON 编辑；执行前仍由后端校验。" : "先测试连接并选择工具，即可自动生成参数表单；也可直接填写 JSON 测试。";
    if (draft.editor && !available) modeHint.textContent = "保留了此工具上次生成的表单；可重新测试连接更新字段。完整参数以服务端验证为准。";
    displayMode();
    syncSelectionUI();
  }
  function displayMode() {
    if (!draft || !draft.editor) argumentMode = "json";
    formArguments.replaceChildren();
    if (draft && draft.editor) formArguments.append(draft.editor.element);
    formArguments.hidden = argumentMode !== "form"; jsonArguments.hidden = argumentMode !== "json";
    formMode.setAttribute("aria-pressed", String(argumentMode === "form")); jsonMode.setAttribute("aria-pressed", String(argumentMode === "json"));
    // A valid JSON draft can always be converted when the selected schema supports forms.
    formMode.disabled = busy || !(draft && draft.editor || canGenerateForm()); jsonMode.disabled = busy;
    formArguments.disabled = busy;
  }
  function canGenerateForm() {
    const selected = tools.find(t => t.name === tool.value.trim());
    return selected && selected.schema_valid !== false && !selected.schema_truncated && App.mcpSchemaForm && App.mcpSchemaForm.supported(selected.input_schema);
  }
  function parseArguments() {
    let value;
    try { value = JSON.parse(args.value); } catch (_) { throw Object.assign(new Error("参数 JSON 格式不正确，请检查引号、逗号和括号。"), { control: args }); }
    if (!value || Array.isArray(value) || typeof value !== "object") throw Object.assign(new Error("参数必须是 JSON 对象，例如 {}。"), { control: args });
    return value;
  }
  function argumentError(error) { status.textContent = error.message; if (error.control) error.control.focus(); }
  function switchMode(mode) {
    if (busy || mode === argumentMode) return;
    try {
      if (mode === "json") args.value = pretty(draft.editor.read({ partial: true }));
      else {
        const selected = tools.find(t => t.name === tool.value.trim());
        const source = canGenerateForm() ? selected.input_schema : draft.schema;
        if (!source) return;
        draft.editor = App.mcpSchemaForm.create(source, parseArguments()); draft.signature = pretty(source); draft.schema = source;
      }
      argumentMode = mode; draft.mode = mode; draft.raw = args.value; showSchema();
      status.textContent = mode === "form" ? "已切换为表单，已有参数已保留。" : "已切换为 JSON，表单参数已同步。";
    } catch (error) { argumentError(error); }
  }
  function fillExample() {
    const selected = tools.find(t => t.name === tool.value.trim());
    if (!selected || selected.schema_valid === false || !selected.input_schema || typeof selected.input_schema !== "object") { status.textContent = "请先测试连接并选择提供有效 Schema 的工具。"; return; }
    args.value = pretty(App.mcpSchemaForm.sample(selected.input_schema));
    if (App.mcpSchemaForm.supported(selected.input_schema) && !selected.schema_truncated) {
      draft.editor = App.mcpSchemaForm.create(selected.input_schema, parseArguments()); draft.signature = pretty(selected.input_schema); draft.schema = selected.input_schema;
    }
    draft.raw = args.value; showSchema(); status.textContent = "示例仅作结构参考，请填写实际参数后点击执行测试。";
  }
  function cancel() {
    ++version; if (controller) controller.abort(); controller = null; setBusy(false);
    if (lastReport && lastReport.action === "batch_call") { lastReport.cancelled = true; lastReport.ok = false; }
    status.textContent = "已停止等待。已开始的外部操作可能仍会完成。";
  }
  async function loadServers() {
    if (!opened || busy) return;
    if (controller) controller.abort(); controller = new AbortController();
    const current = ++version; setBusy(true); status.textContent = "正在读取已配置服务…";
    try {
      const data = await API.mcpDiagnosticServers(controller.signal);
      if (!opened || current !== version) return;
      const previous = server.value; server.replaceChildren();
      serverInfo.clear();
      (data.servers || []).forEach(item => { serverInfo.set(item.id, item); const option = node("option", "", item.id + (item.connection_label ? " · " + item.connection_label : "")); option.value = item.id; server.append(option); });
      if (Array.from(server.options).some(option => option.value === previous)) server.value = previous;
      if (server.value !== previous) tool.value = "";
      showConnectionHint(); resetTools(); resetResult(); status.textContent = server.options.length ? "请选择服务并测试连接，或输入工具名和参数直接测试。" : "尚未配置 MCP 服务，请先在 setting/mcp_servers.json 中添加服务。";
    } catch (error) {
      if (opened && current === version && error.name !== "AbortError") status.textContent = "服务读取失败：" + error.message + (error.status === 404 ? "；请重启更新后的后端。" : "");
    } finally { if (current === version) { controller = null; setBusy(false); } }
  }
  async function run(action) {
    if (busy || !opened) return;
    if (!server.value) { status.textContent = "请先选择 MCP 服务。"; return; }
    const seconds = Number(timeout.value);
    if (!Number.isFinite(seconds) || seconds < 1 || seconds > 300) { status.textContent = "测试超时需为 1–300 秒。"; timeout.focus(); return; }
    const jobs = [];
    if (action === "call") {
      const selected = selectedTools(), original = tool.value.trim();
      if (!selected.size) {
        if (!original) { status.textContent = "请选择测试工具，或输入工具原名。"; tool.focus(); return; }
        selected.add(original);
      } else if (original && !selected.has(original)) { status.textContent = "当前工具尚未加入测试，请点击「加入测试」，或切换到已选工具。"; addTool.focus(); return; }
      // Check all drafts before starting any external operation. Each tool still
      // receives the backend's complete schema validation in its own request.
      for (const name of selected) {
        activateTool(name);
        try {
          const parameters = argumentMode === "form" ? draft.editor.read() : parseArguments();
          if (argumentMode === "form") args.value = pretty(parameters);
          if (new TextEncoder().encode(args.value).length > 65536) throw new Error("参数不能超过 64KB。");
          jobs.push({ name, parameters });
        } catch (error) { argumentError(error); status.textContent = name + "：" + error.message; return; }
      }
      activateTool(selected.has(original) ? original : selected.values().next().value);
    } else jobs.push({ name: null, parameters: {} });
    const batch = jobs.length > 1, reports = [], requestController = new AbortController();
    controller = requestController; const current = ++version; setBusy(true); resetResult();
    let latestTools = null;
    try {
      for (const [index, job] of jobs.entries()) {
        if (!opened || current !== version || requestController.signal.aborted) return;
        status.textContent = action === "connect" ? "正在启动服务、握手并获取工具清单…" :
          "正在执行 " + (index + 1) + "/" + jobs.length + "：" + job.name + "…";
        if (action === "call" && selectedProfileInfo().browser_mode === "extension") status.textContent += " 首次连接请切换到浏览器完成 Playwright Extension 授权、选择标签页。";
        let report, requestError = null;
        try {
          report = await API.testMcpServer({ server_id: server.value, action, tool_name: job.name,
            arguments: job.parameters, timeout_seconds: seconds,
            ...(profileSelect.value ? { mcp_profile: profileSelect.value } : {}) }, requestController.signal);
        } catch (error) {
          requestError = error;
          // HTTP errors may contain raw request values. Do not put those messages
          // (or client-side argument drafts) into the downloadable batch report.
          report = { ok: false, redacted: true, error: { code: "request_error", message: "测试请求未完成，请查看界面提示。" } };
        }
        if (!opened || current !== version || requestController.signal.aborted) return;
        if (Array.isArray(report.tools) && (report.tools.length || report.tool_count === 0)) latestTools = report.tools;
        if (batch) {
          // The tool catalog can be large and is repeated in every response.
          // Keep it once in the bundle, and mount result bodies on demand.
          const itemReport = { ...report }; delete itemReport.tools;
          reports.push(itemReport);
          const card = node("details", "mcp-test-batch-result"); card.open = !report.ok;
          card.append(node("summary", "", (index + 1) + ". " + job.name + " · " + (report.ok ? "成功" : codes[(report.error || {}).code] || "失败")));
          const content = node("div"); card.append(content); result.append(card);
          let rendered = false;
          const render = () => { if (card.open && !rendered) { rendered = true; renderReport(itemReport, content); } };
          card.addEventListener("toggle", render); render();
          lastReport = { version: 1, action: "batch_call", redacted: true, ok: reports.length === jobs.length && reports.every(item => item.ok),
            requested_count: jobs.length, completed_count: reports.length, tools: latestTools || [], reports: reports.slice() };
        } else { reports.push(report); lastReport = report; renderReport(report); }
        if (requestError) (batch ? result.lastElementChild : result).append(node("p", "mcp-test-error", "测试请求失败：" + requestError.message));
        copy.disabled = download.disabled = false;
        if (!report.ok && !continueOnError.checked) {
          if (batch) lastReport.stopped_on_error = true;
          break;
        }
      }
      if (latestTools) {
        tools = latestTools; renderPicker();
        if (!tool.value.trim() && tools.length) tool.value = tools[0].name;
        if (!selectedTools().size && tool.value.trim()) selectedTools().add(tool.value.trim());
        showSchema();
      }
      if (batch) status.textContent = "测试完成 · 成功 " + reports.filter(item => item.ok).length + " · 失败 " + reports.filter(item => !item.ok).length +
        (reports.length < jobs.length ? " · 未执行 " + (jobs.length - reports.length) : "");
    } catch (error) {
      if (opened && current === version && error.name !== "AbortError") status.textContent = "测试请求失败：" + error.message;
    } finally { if (current === version) { controller = null; setBusy(false); } }
  }
  function renderReport(report, target = result) {
    if (target === result) result.replaceChildren();
    const error = report.error || {};
    if (target === result) status.textContent = (report.ok ? "测试成功" : codes[error.code] || "测试失败") + " · " + (report.elapsed_ms == null ? "" : report.elapsed_ms + " ms") +
      (report.tool_count == null ? "" : " · " + report.tool_count + " 个工具");
    if (error.message) target.append(node("p", "mcp-test-error", (stages[error.phase] || error.phase || "") + "：" + error.message));
    (report.steps || []).forEach(step => target.append(node("p", "mcp-test-hint", "✓ " + (stages[step.phase] || step.phase))));
    if (report.protocol_version) target.append(node("p", "mcp-test-hint", "协商协议版本：" + report.protocol_version));
    (report.warnings || []).forEach(text => target.append(node("p", "mcp-test-hint", text)));
    if (report.result) {
      target.append(node("h3", "", "工具返回"));
      target.append(node("pre", "", pretty(report.result)));
      if (report.ok && !(report.result.content || []).length && !report.result.structured_content) target.append(node("p", "mcp-test-hint", "工具已成功返回空结果。"));
    }
    const details = node("details"); details.append(node("summary", "", "完整脱敏报告"), node("pre", "", pretty(report))); target.append(details);
    target.append(node("p", "mcp-test-hint", "报告隐藏常见密钥、认证信息和本机路径；分享前请检查业务正文。"));
  }
  async function copyReport() { if (!lastReport) return; try { await navigator.clipboard.writeText(pretty(lastReport)); App.toast("已复制脱敏报告"); } catch (_) { App.toast("复制失败，可下载报告"); } }
  function downloadReport() {
    if (!lastReport) return;
    const url = URL.createObjectURL(new Blob([pretty(lastReport)], { type: "application/json" }));
    const link = node("a"); link.href = url; link.download = "mcp-diagnostic-" + Date.now() + ".json"; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  function open(serverId, toolName) {
    if (opened) return;
    opened = true; previousFocus = document.activeElement;
    // 父级工具弹窗会将其他 body 子节点设为 inert；嵌套打开时显式恢复交互。
    previousInert = modal.inert; modal.inert = false;
    if (App.hideToolTip) App.hideToolTip();
    document.body.classList.add("mcp-test-open");
    background = Array.from(document.body.children).filter(n => n !== modal && !["SCRIPT", "STYLE", "LINK"].includes(n.tagName)).map(n => { const old = n.inert; n.inert = true; return [n, old]; });
    modal.hidden = false; closeButton.focus({ preventScroll: true });
    const loading = loadServers(), openingVersion = version;
    loading.then(() => {
      if (!opened || version !== openingVersion) return;
      if (serverId && Array.from(server.options).some(o => o.value === serverId)) {
        if (server.value !== serverId) tool.value = "";
        server.value = serverId;
      }
      showConnectionHint();
      if (toolName) { tool.value = toolName; selections.set(selectionKey(), new Set([toolName])); }
      showSchema();
    });
  }
  function close() {
    if (!opened) return;
    opened = false; ++version; if (controller) controller.abort(); controller = null;
    closePicker();
    document.body.classList.remove("mcp-test-open");
    modal.hidden = true; setBusy(false); background.forEach(([n, old]) => { n.inert = old; }); background = [];
    modal.inert = previousInert;
    if (previousFocus && previousFocus.isConnected) previousFocus.focus({ preventScroll: true });
  }
  modal.addEventListener("keydown", event => {
    if (event.isComposing) return;
    if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); if (!picker.hidden) { closePicker(); pickerButton.focus(); } else close(); }
    if (event.key !== "Tab") return;
    const targets = Array.from(dialog.querySelectorAll("button,input,textarea,select,summary")).filter(n => !n.matches(":disabled") && !n.closest("[hidden]") && n.getClientRects().length);
    const first = targets[0], last = targets[targets.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  });
  const entry = document.getElementById("mcpDiagnosticsOpen"); if (entry) entry.addEventListener("click", () => open());
  App.mcpDiagnostics = { open, close };
})(window.App);
