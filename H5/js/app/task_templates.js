/** Reusable task definitions. Applying creates an independent session and an unsent draft. */
(function (App) {
  "use strict";
  if (!App) return;
  const node = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
  const button = (text, fn) => { const n = node("button", "template-button", text); n.type = "button"; n.addEventListener("click", fn); return n; };
  const roles = [["chat_model", "聊天模型"], ["compaction_model", "压缩模型"], ["title_model", "标题模型"], ["sub_agent_model", "子智能体"]];
  const limits = [["tokens", "累计 tokens", 1], ["cost", "费用预算", .01], ["model_calls", "模型调用次数", 1], ["tool_calls", "工具派发次数", 1], ["seconds", "运行秒数", 1]];
  const pretty = value => JSON.stringify(value, null, 2);
  let opened = false, busy = false, loadingOnly = false, initialized = false, epoch = 0, controller = null, identity = null, revision = null;
  let cfg = null, baseline = "", background = [], returnFocus = null, previousInert = false, pending = null, preview = null, selectedId = "";
  const modal = node("div", "task-template-modal"); modal.hidden = true;
  const backdrop = node("div", "task-template-backdrop"), dialog = node("section", "task-template-dialog");
  dialog.setAttribute("role", "dialog"); dialog.setAttribute("aria-modal", "true"); dialog.setAttribute("aria-labelledby", "taskTemplateTitle");
  const header = node("header", "template-header"), title = node("h2", "", "任务模板"), closeButton = button("关闭", close); title.id = "taskTemplateTitle"; header.append(title, closeButton);
  const body = node("div", "template-body"), toolbar = node("div", "template-toolbar"), picker = node("select"); picker.id = "taskTemplateSelect"; picker.setAttribute("aria-label", "已保存的任务模板");
  const newButton = button("新建", () => discard(() => fill(empty()))), captureButton = button("从当前配置创建", () => discard(capture));
  const refreshButton = button("刷新列表", () => act(async () => { await readAll(); if (!initialized) fill(empty()); status.textContent = "列表与可用配置已刷新"; })); toolbar.append(picker, newButton, captureButton, refreshButton);
  const editor = node("div", "template-editor");
  function field(label, input) { const wrap = node("label", "template-field"); wrap.append(node("span", "", label), input); return wrap; }
  function input(id, rows) { const n = node(rows ? "textarea" : "input"); n.id = id; if (rows) { n.rows = rows; n.spellcheck = false; } else n.type = "text"; return n; }
  const name = input("taskTemplateName"), description = input("taskTemplateDescription"), prompt = input("taskTemplatePrompt", 5);
  name.maxLength = 80; description.maxLength = 500; prompt.maxLength = 32768;
  editor.append(field("模板名称", name), field("用途说明（可选）", description), field("任务正文", prompt));
  function section(label, hint) { const details = node("details", "template-section"); details.append(node("summary", "", label)); if (hint) details.append(node("p", "template-hint", hint)); editor.append(details); return details; }
  const modelSection = section("模型选择", "模板保存四种角色的模型引用；生成参数、认证和价格使用当前配置，不打包进模板。"), modelFields = {};
  roles.forEach(([role, label]) => { const select = node("select"); select.setAttribute("aria-label", label); modelFields[role] = select; modelSection.append(field(label, select)); });
  const toolSection = section("可用工具", "按服务和工具原名保存，支持跨服务重名。选择“自定义”后全部取消代表无工具模式。"), toolMode = node("select");
  toolMode.setAttribute("aria-label", "模板工具策略");
  [["inherit", "沿用当前会话 / 全局工具"], ["custom", "自定义工具选择"]].forEach(([value, text]) => { const o = node("option", "", text); o.value = value; toolMode.append(o); });
  const toolList = node("div", "template-tools"), toolSearch = input("taskTemplateToolSearch"), refreshTools = button("刷新工具清单", () => act(async () => { await API.listTools(true); await loadOptions(); }));
  toolSearch.placeholder = "搜索服务或工具"; toolSearch.setAttribute("aria-label", "筛选模板工具");
  toolSection.append(toolMode, toolSearch, refreshTools, toolList); let toolBoxes = [];
  const budgetSection = section("任务预算", "0 表示无限制。额度作用于新任务；费用币种沿用当前计价设置，模板不包含价格表。"), budgetMode = node("select"); budgetMode.setAttribute("aria-label", "模板预算策略");
  [["inherit", "沿用当前会话 / 全局预算"], ["custom", "指定预算（可设无限制）"]].forEach(([value, text]) => { const o = node("option", "", text); o.value = value; budgetMode.append(o); });
  const budgetFields = {}, budgetGrid = node("div", "template-budget-grid"), currency = node("p", "template-hint"), budgetCurrency = node("select");
  budgetCurrency.setAttribute("aria-label", "费用预算币种"); ["CNY", "USD", "EUR"].forEach(value => { const option = node("option", "", value); option.value = value; budgetCurrency.append(option); });
  limits.forEach(([key, label, step]) => { const n = input("taskTemplateBudget_" + key); n.type = "number"; n.min = "0"; n.max = "1000000000000000"; n.step = String(step); n.value = "0"; n.setAttribute("aria-label", label); budgetFields[key] = n; budgetGrid.append(field(label, n)); });
  budgetSection.append(budgetMode, currency, field("费用预算币种", budgetCurrency), budgetGrid);
  const pathSection = section("工作路径规则", "目录须已存在。相对子目录以应用时当前会话 / 全局工作目录为基准，不能越过该目录。"), pathMode = node("select"), workPath = input("taskTemplateWorkPath"); pathMode.setAttribute("aria-label", "模板工作路径规则");
  [["inherit", "沿用当前会话 / 全局工作目录"], ["fixed", "固定绝对路径"], ["relative", "当前工作目录下的相对子目录"]].forEach(([value, text]) => { const o = node("option", "", text); o.value = value; pathMode.append(o); });
  pathSection.append(pathMode, field("路径", workPath));
  const formatSection = section("输出格式与验收要求", "作为任务正文的一部分提供给模型；应用前会展示完整内容，不会自动运行验证命令。"), output = input("taskTemplateOutput", 3), acceptance = input("taskTemplateAcceptance", 3);
  output.maxLength = acceptance.maxLength = 16384; formatSection.append(field("输出格式", output), field("验收要求", acceptance));
  const management = node("div", "template-toolbar"), duplicate = button("另存为副本", () => { identity = revision = null; selectedId = ""; picker.value = ""; name.value = name.value ? name.value.slice(0, 76) + " 副本" : ""; noteChanged(); });
  const importButton = button("导入 JSON", () => upload.click()), exportButton = button("导出 JSON", exportDefinition), remove = button("删除模板", () => confirmAction("确认删除这个已保存的模板？", () => act(async () => { await API.deleteTaskTemplate(identity, revision); fill(empty()); await loadList(); status.textContent = "模板已删除"; })));
  const upload = node("input"); upload.type = "file"; upload.accept = ".json,application/json"; upload.hidden = true;
  management.append(duplicate, importButton, exportButton, remove, upload); editor.append(management, node("p", "template-hint", "导出包含任务正文和路径，分享前请检查其中的业务信息。关闭面板会保留本页未保存的草稿。"));
  const confirmation = node("div", "template-confirm"); confirmation.hidden = true; const confirmText = node("p"), confirmYes = button("确认", async () => { const fn = pending; pending = null; confirmation.hidden = true; if (fn) await fn(); }), confirmNo = button("取消", () => { pending = null; confirmation.hidden = true; }); confirmation.append(confirmText, confirmYes, confirmNo);
  const previewBox = node("section", "template-preview"); previewBox.hidden = true;
  const status = node("p", "template-status"); status.setAttribute("role", "status"); status.setAttribute("aria-live", "polite");
  const footer = node("footer", "template-footer"), saveButton = button("保存模板", save), previewButton = button("预览并新建会话", prepare), startButton = button("确认新建（不自动发送）", start), backButton = button("返回编辑", () => { preview = null; showPreview(false); });
  startButton.hidden = backButton.hidden = true; footer.append(backButton, saveButton, previewButton, startButton);
  body.append(toolbar, editor, confirmation, previewBox, status); dialog.append(header, body, footer); modal.append(backdrop, dialog); document.body.append(modal);
  backdrop.addEventListener("click", close);
  function empty() { return { name: "", description: "", prompt: "", models: {}, tools: null, budget: null, budget_currency: null, work_dir: { mode: "inherit", path: "" }, output_format: "", acceptance: "" }; }
  function data() {
    const models = {}, tools = {}, budget = {};
    roles.forEach(([role]) => { if (modelFields[role].value) models[role] = JSON.parse(modelFields[role].value); });
    toolBoxes.forEach(box => { if (box.input.checked) (tools[box.tool.server] ||= []).push(box.tool.name); });
    limits.forEach(([key]) => { budget[key] = Number(budgetFields[key].value); });
    return { name: name.value.trim(), description: description.value, prompt: prompt.value, models, tools: toolMode.value === "custom" ? tools : null,
      budget: budgetMode.value === "custom" ? budget : null, budget_currency: budgetMode.value === "custom" ? budgetCurrency.value : null,
      work_dir: { mode: pathMode.value, path: pathMode.value === "inherit" ? "" : workPath.value.trim() }, output_format: output.value, acceptance: acceptance.value };
  }
  function valid() {
    const value = data();
    if (!value.name) { name.focus(); throw new Error("请填写模板名称"); }
    if (value.budget) limits.forEach(([key, label, step]) => { const n = value.budget[key]; if (!Number.isFinite(n) || n < 0 || (step === 1 && !Number.isInteger(n))) throw new Error(label + "必须为非负数；0 表示无限制，次数和 tokens 须为整数"); });
    return value;
  }
  function setBusy(value) { busy = value; dialog.setAttribute("aria-busy", String(value)); body.inert = value; footer.inert = value; closeButton.disabled = value && !loadingOnly; }
  async function act(fn) {
    if (busy) return; const current = epoch; setBusy(true); status.textContent = "处理中…";
    try { return await fn(); } catch (error) { if (current === epoch) status.textContent = error.message + (error.status === 404 ? "；接口或模板不存在，请刷新列表；升级后需重启后端。" : ""); }
    finally { if (current === epoch) setBusy(false); }
  }
  function showPreview(value) { toolbar.hidden = editor.hidden = value; previewBox.hidden = !value; saveButton.hidden = previewButton.hidden = value; startButton.hidden = backButton.hidden = !value; }
  function noteChanged() { preview = null; status.textContent = JSON.stringify(data()) === baseline ? "已保存" : "尚有未保存的修改"; remove.disabled = !identity; }
  editor.addEventListener("input", noteChanged); editor.addEventListener("change", noteChanged);
  function confirmAction(message, fn) { pending = fn; confirmText.textContent = message; confirmation.hidden = false; confirmYes.focus(); }
  function discard(fn) { if (JSON.stringify(data()) !== baseline) confirmAction("当前有未保存的修改，确认放弃并继续？", fn); else fn(); }
  function fill(value, record) {
    initialized = true; identity = record ? record.id : null; revision = record ? record.version : null; selectedId = identity || ""; picker.value = selectedId;
    name.value = value.name || ""; description.value = value.description || ""; prompt.value = value.prompt || ""; output.value = value.output_format || ""; acceptance.value = value.acceptance || "";
    renderChoices(value); pathMode.value = value.work_dir && value.work_dir.mode || "inherit"; workPath.value = value.work_dir && value.work_dir.path || "";
    budgetMode.value = value.budget === null || value.budget === undefined ? "inherit" : "custom";
    limits.forEach(([key]) => { budgetFields[key].value = (value.budget || {})[key] || 0; });
    budgetCurrency.value = value.budget_currency || (cfg && cfg.currency) || "CNY"; currency.textContent = "当前计价币种：" + ((cfg && cfg.currency) || "尚未读取");
    applyModes(); baseline = JSON.stringify(data()); remove.disabled = !identity; preview = null; showPreview(false); confirmation.hidden = true; status.textContent = identity ? "已加载模板" : "填写后保存模板，或先预览并新建会话";
  }
  function renderChoices(value) {
    roles.forEach(([role]) => {
      const select = modelFields[role], current = value.models && value.models[role]; select.replaceChildren();
      const follow = node("option", "", "使用全局默认模型"); follow.value = ""; select.append(follow);
      (cfg && cfg.models || []).forEach(item => { const option = node("option", "", item.provider + " / " + item.model + " · " + item.api_type); option.value = JSON.stringify({ provider: item.provider, model: item.model }); select.append(option); });
      if (current) { const key = JSON.stringify({ provider: current.provider, model: current.model }); if (!Array.from(select.options).some(o => o.value === key)) { const o = node("option", "", current.provider + " / " + current.model + "（未找到）"); o.value = key; select.append(o); } select.value = key; }
    });
    const chosen = value.tools || {}, available = (cfg && cfg.tools || []).slice(); toolList.replaceChildren(); toolBoxes = [];
    Object.entries(chosen).forEach(([server, names]) => names.forEach(name => { if (!available.some(t => t.server === server && t.name === name)) available.push({ server, name, missing: true }); }));
    const groups = new Map();
    available.forEach(tool => {
      if (!groups.has(tool.server)) { const group = node("fieldset", "template-tool-group"); group.append(node("legend", "", tool.server === "__builtin__" ? "内置工具" : tool.server)); toolList.append(group); groups.set(tool.server, group); }
      const label = node("label", "template-tool"), check = node("input"); check.type = "checkbox"; check.checked = (chosen[tool.server] || []).includes(tool.name);
      check.setAttribute("aria-label", tool.server + " / " + tool.name); label.title = tool.description || "";
      label.append(check, node("span", "", tool.name + (tool.missing ? "（未发现，请刷新工具或取消选择）" : ""))); groups.get(tool.server).append(label); toolBoxes.push({ input: check, tool, label });
    });
    toolMode.value = value.tools === null || value.tools === undefined ? "inherit" : "custom"; filterTools();
  }
  function filterTools() { const query = toolSearch.value.toLowerCase().trim(); toolBoxes.forEach(box => { box.label.hidden = !(box.tool.server + " " + box.tool.name).toLowerCase().includes(query); }); }
  function applyModes() { toolList.inert = toolMode.value !== "custom"; toolList.classList.toggle("is-inherited", toolMode.value !== "custom"); toolSearch.disabled = toolMode.value !== "custom"; budgetGrid.hidden = budgetMode.value !== "custom"; budgetCurrency.disabled = budgetMode.value !== "custom"; workPath.disabled = pathMode.value === "inherit"; }
  toolMode.addEventListener("change", applyModes); budgetMode.addEventListener("change", applyModes); pathMode.addEventListener("change", applyModes); toolSearch.addEventListener("input", filterTools);
  async function loadList() {
    const current = epoch, rows = await API.listTaskTemplates(controller && controller.signal); if (!opened || epoch !== current) return;
    picker.replaceChildren(); const blank = node("option", "", "新模板 / 未保存草稿"); blank.value = ""; picker.append(blank);
    (rows.templates || []).forEach(item => { const option = node("option", "", item.name); option.value = item.id; picker.append(option); }); picker.value = selectedId;
    if (identity && !Array.from(picker.options).some(o => o.value === identity)) { selectedId = ""; picker.value = ""; status.textContent = "该模板已在其他页面删除；草稿保留，可另存为副本"; }
  }
  async function loadOptions() {
    const current = epoch, value = initialized ? data() : null, options = await API.taskTemplateOptions(App.state.sessionId, controller && controller.signal); if (!opened || epoch !== current) return null;
    cfg = options; currency.textContent = "当前计价币种：" + cfg.currency;
    if (value) { renderChoices(value); applyModes(); }
    return options;
  }
  async function readAll() {
    const results = await Promise.allSettled([loadOptions(), loadList()]);
    const failed = results.find(result => result.status === "rejected"); if (failed) throw failed.reason;
  }
  picker.addEventListener("change", () => {
    const id = picker.value; picker.value = selectedId;
    discard(() => act(async () => { if (!id) fill(empty()); else { const record = await API.getTaskTemplate(id); fill(record.template, record); } }));
  });
  async function capture(prefill) {
    return act(async () => { const options = await loadOptions(); fill(Object.assign(empty(), options.capture, { prompt: prefill === undefined ? App.input.value : prefill })); baseline = ""; status.textContent = "已读取当前模型、工具、预算和工作路径；请填写模板名称后保存"; });
  }
  async function save() { return act(async () => { const record = await API.saveTaskTemplate(valid(), identity, revision); fill(record.template, record); await loadList(); status.textContent = "模板已保存"; }); }
  function token() { return crypto.randomUUID ? crypto.randomUUID() : "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, c => { const n = crypto.getRandomValues(new Uint8Array(1))[0] & 15; return (c === "x" ? n : (n & 3) | 8).toString(16); }); }
  async function prepare() {
    return act(async () => {
      const template = valid(), source = App.state.sessionId || null, plan = await API.previewTaskTemplate(template, source, controller && controller.signal);
      if (!opened || (App.state.sessionId || null) !== source) throw new Error("当前会话已切换，请重新预览模板");
      preview = { template, source, plan, request: token() }; previewBox.replaceChildren(node("h3", "", "确认新建会话配置"));
      previewBox.append(node("p", "template-hint", "将创建独立会话并填入任务草稿。当前会话及草稿保留，点击发送后才开始运行。"));
      const summary = plan.summary;
      const lines = ["模板：" + summary.name, "工作目录：" + summary.work_dir, "模型：\n" + Object.entries(summary.models).map(([role, item]) => (roles.find(r => r[0] === role) || [role, role])[1] + "：" + item.provider + " / " + item.model).join("\n"),
        "工具：" + (summary.tools.length ? summary.tools.map(t => (t.server === "__builtin__" ? "内置工具" : t.server) + " / " + t.name).join("、") : "无工具"),
        "预算：" + limits.map(([key, label]) => label + " " + (summary.budget[key] > 0 ? summary.budget[key] : "无限制")).join("；") + "（" + summary.currency + "）"];
      previewBox.append(node("pre", "", lines.join("\n\n")), node("h3", "", "将填入的任务正文"), node("pre", "", summary.prompt));
      (summary.warnings || []).forEach(warning => previewBox.append(node("p", "template-hint", warning))); showPreview(true); status.textContent = "请核对配置与任务正文，再确认新建"; body.scrollTop = 0;
    });
  }
  async function start() {
    if (!preview) return;
    return act(async () => {
      const current = preview;
      if ((App.state.sessionId || null) !== current.source) throw new Error("当前会话已切换，请返回编辑后重新预览");
      const started = await API.startTaskTemplate(current.template, current.source, current.request, current.plan.fingerprint);
      if ((App.state.sessionId || null) !== current.source) { status.textContent = "会话已创建：" + started.session_id + "；当前页面已切换，可从历史列表打开"; return; }
      // Session switching saves the previous composer draft using the existing flow.
      setBusy(false); close(); await App.openSession(started.session_id);
      if (App.state.sessionId === started.session_id) {
        if (!App.state.hasConversation && !App.input.value) { App.input.value = started.summary.prompt; App.input.dispatchEvent(new Event("input", { bubbles: true })); }
        App.input.focus(); if (App.saveSessionDraft) App.saveSessionDraft();
      }
      if (App.loadSessions) App.loadSessions(); App.toast("模板已应用到新会话，确认后点击发送");
    });
  }
  function exportDefinition() {
    try { const value = valid(), url = URL.createObjectURL(new Blob([pretty({ format: "ytools-task-template", version: 1, template: value })], { type: "application/json" }));
      const a = node("a"); a.href = url; a.download = "ytools-task-template.json"; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) { status.textContent = error.message; }
  }
  upload.addEventListener("change", () => {
    const file = upload.files && upload.files[0]; upload.value = ""; if (!file) return;
    discard(() => act(async () => { if (file.size > 262144) throw new Error("模板文件不能超过 256KB"); const parsed = JSON.parse(await file.text());
      if (parsed.format !== "ytools-task-template" || parsed.version !== 1 || !parsed.template || typeof parsed.template !== "object") throw new Error("不是受支持的 Ytools 任务模板文件");
      const validated = await API.validateTaskTemplate(parsed.template);
      fill(validated.template); baseline = ""; status.textContent = "已导入为草稿，请核对后保存；尚未创建会话";
    }));
  });
  async function open(prefill) {
    if (opened) return; if (App.closeMenus) App.closeMenus();
    opened = true; const current = ++epoch; returnFocus = document.activeElement; previousInert = modal.inert; modal.inert = false;
    background = Array.from(document.body.children).filter(n => n !== modal && !["SCRIPT", "STYLE", "LINK"].includes(n.tagName)).map(n => { const old = n.inert; n.inert = true; return [n, old]; });
    modal.hidden = false; document.body.classList.add("task-template-open"); closeButton.focus({ preventScroll: true }); controller = new AbortController();
    loadingOnly = true; setBusy(true); status.textContent = "正在读取任务模板与当前配置…";
    try { await readAll(); if (!opened || current !== epoch) return; if (!initialized) fill(empty());
      if (prefill && prefill.capture) {
        const applyPrefill = () => { fill(Object.assign(empty(), cfg.capture, { prompt: prefill.prompt || "" })); baseline = ""; };
        if (JSON.stringify(data()) !== baseline) confirmAction("当前有未保存的模板修改，确认放弃并从提示词创建？", applyPrefill); else applyPrefill();
      }
      status.textContent = "选择已有模板，或新建并保存；应用前可预览全部配置";
    } catch (error) { if (opened && current === epoch && error.name !== "AbortError") status.textContent = "读取失败：" + error.message + "；升级后请重启后端，可刷新列表重试"; }
    finally { if (current === epoch) { loadingOnly = false; setBusy(false); } }
  }
  function close() {
    if (!opened || (busy && !loadingOnly)) return; opened = false; ++epoch; if (controller) controller.abort(); controller = null; loadingOnly = false; setBusy(false); preview = null; showPreview(false); confirmation.hidden = true;
    modal.hidden = true; modal.inert = previousInert; document.body.classList.remove("task-template-open"); background.forEach(([n, old]) => { n.inert = old; }); background = [];
    if (returnFocus && returnFocus.isConnected && !returnFocus.closest("[inert],.hidden,[hidden]")) returnFocus.focus({ preventScroll: true });
    else if (App.input) App.input.focus({ preventScroll: true });
  }
  modal.addEventListener("keydown", event => {
    if (event.isComposing) return;
    if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); close(); }
    if (event.key !== "Tab") return;
    const targets = Array.from(dialog.querySelectorAll("button,input,textarea,select,summary")).filter(n => !n.disabled && !n.closest("[hidden],[inert]") && n.getClientRects().length);
    const first = targets[0], last = targets[targets.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); if (last) last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); if (first) first.focus(); }
  });
  const entry = document.getElementById("taskTemplatesItem"); if (entry) entry.addEventListener("click", () => open());
  async function restoreDraft(meta, session) {
    const text = meta && meta.task_template && meta.task_template.initial_prompt;
    if (!text || meta.task_template.draft_consumed || meta.record_count || meta.completion_count || App.input.value) return;
    try {
      const running = await API.streamStatus(session);
      if (!running.running && App.state.sessionId === session && !App.input.value) {
        App.input.value = text; App.input.dispatchEvent(new Event("input", { bubbles: true }));
      }
    } catch (_) { /* Preserve the persisted draft for a later successful reload. */ }
  }
  App.taskTemplates = { open, close, restoreDraft };
})(window.App);
