/* Authoritative usage snapshots. SSE and refresh replace values, never add them. */
(function (App) {
  "use strict";
  const fields = [["tokens", "累计 tokens", 1], ["cost", "预估费用", 0.01],
    ["model_calls", "模型调用次数", 1], ["tool_calls", "工具派发次数", 1], ["seconds", "任务运行秒数", 1]];
  const roleNames = { main: "主模型", sub_agent: "子任务", compaction: "压缩", title: "标题" };
  const reasonNames = { tokens: "累计 tokens 已达额度", cost: "预估费用已达额度", model_calls: "模型调用次数已达额度",
    tool_calls: "工具派发次数已达额度", seconds: "任务运行时间已达额度", missing_price: "费用预算需要先配置模型价格" };
  const stateNames = { running: "运行中", paused: "等待追加预算", suspended: "挂起，先恢复原任务", completed: "已完成", failed: "失败", cancelled: "已停止" };
  let snapshot = null, session = "", revision = 0, controller = null, timer = null, opened = false;
  let configRevision = 0, pageActive = true;
  let returnFocus = null, background = [], returnSettings = false, cfg = null, saving = false;
  const liveTasks = new Map();
  const decoratedTexts = new WeakMap();
  const estimateHint = " · 含估算/待返回用量";
  const $ = id => document.getElementById(id);
  const node = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
  const button = (text, fn) => { const b = node("button", "task-cost-button", text); b.type = "button"; b.addEventListener("click", fn); return b; };
  const money = n => Number(n || 0).toLocaleString(undefined, { maximumFractionDigits: 6 });
  const modal = node("div", "task-cost-modal"); modal.hidden = true;
  const backdrop = node("div", "task-cost-backdrop");
  const dialog = node("section", "task-cost-dialog"); dialog.setAttribute("role", "dialog"); dialog.setAttribute("aria-modal", "true"); dialog.setAttribute("aria-labelledby", "taskCostsTitle");
  const header = node("header", "task-cost-header"), title = node("h2", "", "任务预算与用量"); title.id = "taskCostsTitle";
  const closeButton = button("关闭", close); header.append(title, closeButton);
  const content = node("div", "task-cost-content");
  const error = node("p", "task-cost-error"); error.setAttribute("role", "alert");
  dialog.append(header, error, content); modal.append(backdrop, dialog); document.body.append(modal);
  backdrop.addEventListener("click", close);

  function limitForm(limits) {
    const wrap = node("div", "task-cost-limits"), inputs = {};
    fields.forEach(([key, label, step]) => {
      const row = node("label", "task-cost-field"), name = node("span", "", label);
      const mode = node("select"), unlimited = node("option", "", "无限制"), limited = node("option", "", "指定额度");
      unlimited.value = "unlimited"; limited.value = "limited"; mode.append(unlimited, limited);
      mode.setAttribute("aria-label", label + "模式"); mode.value = Number(limits[key]) > 0 ? "limited" : "unlimited";
      const input = node("input"); input.type = "number"; input.min = String(step); input.step = String(step);
      input.setAttribute("aria-label", label + "额度"); input.value = Number(limits[key]) > 0 ? limits[key] : "";
      input.disabled = mode.value === "unlimited"; input.placeholder = "请输入额度";
      mode.addEventListener("change", () => { input.disabled = mode.value === "unlimited"; if (!input.disabled) input.focus(); });
      row.append(name, mode, input); wrap.append(row); inputs[key] = { mode, input };
    });
    return { wrap, read: () => {
      const result = {};
      fields.forEach(([key, label, step]) => {
        const { mode, input } = inputs[key], value = mode.value === "unlimited" ? 0 : Number(input.value);
        if (!Number.isFinite(value) || value < 0 || (mode.value === "limited" && value <= 0) || (step === 1 && !Number.isInteger(value))) {
          input.focus(); throw new Error(label + "额度无效");
        }
        result[key] = value;
      });
      return result;
    } };
  }

  function section(label, intro) {
    const box = node("section", "task-cost-section"); box.append(node("h3", "", label));
    if (intro) box.append(node("p", "task-cost-hint", intro));
    content.append(box); return box;
  }

  async function act(fn) {
    if (saving) return;
    saving = true; error.textContent = ""; content.inert = true; closeButton.disabled = true;
    try { await fn(); }
    catch (e) { error.textContent = e.message || "操作失败，请重试"; }
    finally { saving = false; content.inert = false; closeButton.disabled = false; }
  }

  function taskBlock(task) {
    const box = section((task.kind === "manual_compaction" ? "手动压缩" : "第 " + (task.round || "—") + " 轮") + " · " + (stateNames[task.state] || task.state),
      "模型 " + task.model_calls + " 次 · 工具派发 " + task.tool_calls + " 次 · 运行约 " + Math.round(task.elapsed) + " 秒");
    const lines = node("div", "task-cost-breakdown");
    Object.entries(task.roles || {}).forEach(([role, info]) => {
      lines.append(node("p", "", (roleNames[role] || role) + "：" + FormatUtils.fmtNum(info.usage.total_tokens) + " tokens · " + info.calls + " 次"));
    }); box.append(lines);
    box.append(node("p", "task-cost-hint", "合计 " + FormatUtils.fmtNum(task.usage.total_tokens) + " tokens；已配置价格部分约 " + money(task.cost) + " " + snapshot.currency
      + (task.unknown_price_calls ? "；" + task.unknown_price_calls + " 次调用缺少价格" : "")
      + (task.estimated_calls ? "；" + task.estimated_calls + " 次用量为估算或尚未返回" : "")));
    if (task.state !== "paused") return;
    box.append(node("p", "task-cost-hint", reasonNames[task.reason] || "任务已在调用边界等待。"));
    const suggested = Object.assign({}, task.limits);
    fields.forEach(([key]) => {
      const used = key === "tokens" ? task.usage.total_tokens : key === "cost" ? task.cost : key === "seconds" ? task.elapsed : task[key];
      if (suggested[key] > 0) suggested[key] = Math.ceil(Math.max(suggested[key] * 2, used * 2 + 1));
    });
    const form = limitForm(suggested); box.append(form.wrap);
    const actions = node("div", "task-cost-actions");
    actions.append(button("按新额度继续本任务", () => act(async () => {
      await API.continueTaskBudget(task, form.read()); await refresh(); await load();
    })), button("本任务改为无限并继续", () => act(async () => {
      await API.continueTaskBudget(task, Object.fromEntries(fields.map(([key]) => [key, 0]))); await refresh(); await load();
    })), button("停止任务", () => act(async () => {
      await API.stopTaskBudget(task); await refresh(); await load();
    })));
    box.append(actions);
  }

  function render() {
    content.replaceChildren();
    if (snapshot && snapshot.tracked) snapshot.tasks.filter(task => task.state === "paused").forEach(taskBlock);
    const limits = section("任务预算", "所有额度均为整个任务累计，主模型与子任务共享；全设为无限制即关闭预算限制。保存后用于后续任务，正在等待的任务请在下方单独追加额度。");
    limits.append(node("p", "task-cost-hint", "达到阈值后，在后续模型请求或工具批次派发前等待。已发出的请求与命令仍会完成，可能超出额度；同一工具批次整体放行。费用为本地估算，不能替代供应商账单。"));
    const scope = node("select"); scope.setAttribute("aria-label", "预算配置范围");
    [["global", cfg.scope === "account" ? "账号默认" : "全局默认"], ...(session ? [["session", "仅当前会话"], ["inherit", cfg.scope === "account" ? "当前会话跟随账号默认" : "当前会话跟随全局"]] : [])].forEach(([value, text]) => {
      const option = node("option", "", text); option.value = value; scope.append(option);
    }); scope.value = session ? (cfg.override ? "session" : "inherit") : "global";
    let form;
    const formHolder = node("div");
    function updateForm() {
      form = limitForm(scope.value === "global" ? cfg.global : cfg.limits);
      formHolder.replaceChildren(form.wrap); formHolder.inert = scope.value === "inherit";
    }
    scope.addEventListener("change", updateForm); updateForm();
    limits.append(scope, formHolder, button("保存预算设置", () => act(async () => {
      const sid = scope.value === "global" ? null : session;
      await API.saveTaskCostConfig(scope.value === "inherit" ? null : form.read(), sid);
      App.toast("预算已保存，后续任务生效"); await load();
    })));

    const pricing = section("模型价格（全局）", "单价单位为每百万 tokens。未填写表示价格未知；免费模型请明确填 0。历史调用使用发起时的价格，修改单价不重算历史费用。");
    const currency = node("select"); currency.setAttribute("aria-label", "计价币种");
    ["CNY", "USD", "EUR"].forEach(name => { const o = node("option", "", name); currency.append(o); }); currency.value = cfg.pricing.currency;
    const models = node("select"); models.setAttribute("aria-label", "选择要设置价格的模型");
    (cfg.models || []).forEach(model => { const o = node("option", "", model.label); o.value = model.key; models.append(o); });
    const prices = {};
    pricing.append(currency, models);
    [["input", "输入单价"], ["output", "输出单价"], ["cached_input", "缓存读取单价"]].forEach(([key, label]) => {
      const row = node("label", "task-cost-price"); const input = node("input"); input.type = "number"; input.min = "0"; input.step = "any"; input.placeholder = "未配置";
      row.append(node("span", "", label), input); prices[key] = input; pricing.append(row);
    });
    function fillPrice() { const price = cfg.pricing.models[models.value] || {}; Object.entries(prices).forEach(([key, input]) => { input.value = price[key] == null ? "" : price[key]; }); }
    models.addEventListener("change", fillPrice); fillPrice();
    pricing.append(button("保存该模型价格", () => act(async () => {
      if (!models.value) throw new Error("请先配置模型");
      const price = {};
      Object.entries(prices).forEach(([key, input]) => { const value = Number(input.value); if (!input.value.trim() || !Number.isFinite(value) || value < 0) throw new Error("请填写完整的非负单价，免费填 0"); price[key] = value; });
      const next = { currency: currency.value, models: Object.assign({}, cfg.pricing.models, { [models.value]: price }) };
      await API.saveTaskPricing(next); App.toast("模型价格已保存"); await load();
    })));
    if (window.YtoolsAuth && window.YtoolsAuth.user.role !== "admin") {
      pricing.querySelectorAll("input,select,button").forEach(input => { input.disabled = true; });
      pricing.append(node("p", "task-cost-hint", "模型价格由管理员统一维护；上方任务预算保存到当前账号。"));
    }
    if (snapshot && snapshot.tracked) {
      const total = section("当前会话累计", "历史用量作为初始值保留；旧记录中未保存的标题消耗与费用无法追溯。");
      total.append(node("p", "", FormatUtils.fmtNum(snapshot.usage.total_tokens) + " tokens；已计价部分约 " + money(snapshot.cost) + " " + snapshot.currency));
      const tasks = snapshot.tasks.slice().reverse();
      tasks.filter(task => task.state !== "paused").slice(0, 30).forEach(taskBlock);
      if (tasks.length > 30) content.append(node("p", "task-cost-hint", "显示最近 30 项任务，累计统计包含全部任务。"));
    } else section("当前会话用量", "后续任务开始后记录完整用量与分类明细；已有历史显示原统计。");
  }

  async function load() {
    const sid = session, seq = ++configRevision;
    const result = await API.taskCostConfig(sid);
    if (!opened || sid !== session || seq !== configRevision) return;
    cfg = result; render();
  }

  function close() {
    if (saving || !opened) return;
    opened = false; modal.hidden = true; configRevision++;
    background.forEach(([element, inert]) => { element.inert = inert; }); background = [];
    if (returnSettings && App.showChatSettingsModal) { returnSettings = false; App.showChatSettingsModal(); }
    else if (returnFocus && returnFocus.isConnected) returnFocus.focus({ preventScroll: true });
  }

  async function open() {
    if (opened) return;
    opened = true; returnFocus = document.activeElement; error.textContent = "";
    const settings = $("chatSettingsModal");
    returnSettings = settings && !settings.classList.contains("hidden");
    if (returnSettings) { App.hideChatSettingsModal(); settings.classList.add("hidden"); }
    if (App.plusMenu) App.plusMenu.classList.add("hidden");
    background = Array.from(document.body.children).filter(n => n !== modal && !["SCRIPT", "STYLE", "LINK"].includes(n.tagName)).map(n => { const old = n.inert; n.inert = true; return [n, old]; });
    modal.hidden = false; content.textContent = "正在加载…"; closeButton.focus({ preventScroll: true });
    try { await refresh(); await load(); } catch (e) { error.textContent = e.message || "加载失败，请关闭后重试"; }
  }

  function summaryModel(line) {
    if (line._roundModelSnapshot !== undefined) return line._roundModelSnapshot;
    // 兼容旧的流式摘要：只恢复模型标签，不把显示状态当成模型名称。
    // 已经重复堆积的提示也在本次刷新中清理，后续输出始终由快照重建。
    const text = line.textContent.replace(/(?: · 含估算\/待返回用量)+\s*$/, "").trim();
    const at = text.indexOf(" · 模型 ");
    const label = at >= 0 ? text.slice(at + " · 模型 ".length)
      : text.startsWith("本轮模型 ") ? text.slice("本轮模型 ".length) : "";
    return label ? { name: label } : null;
  }

  function decorate() {
    if (!snapshot || !snapshot.tracked || session !== App.state.sessionId) return;
    const byRound = new Map();
    snapshot.tasks.forEach(t => { if (t.kind === "chat" && t.round > 0) byRound.set(t.round, t); });
    document.querySelectorAll(".round-usage[data-round]").forEach(line => {
      const task = byRound.get(Number(line.dataset.round)); if (!task) return;
      const text = FormatUtils.roundSummaryText(task.usage, summaryModel(line)) + (task.estimated_calls ? estimateHint : "");
      decoratedTexts.set(line, text);
      if (line.textContent !== text) line.textContent = text;
      line.title = Object.entries(task.roles).map(([role, info]) => (roleNames[role] || role) + " " + FormatUtils.fmtNum(info.usage.total_tokens)).join(" · ");
    });
  }

  function showBanner() {
    const banner = $("taskBudgetBanner"); if (!banner) return;
    const paused = snapshot && snapshot.tasks.find(t => t.state === "paused");
    banner.replaceChildren(); banner.hidden = !paused;
    if (paused) banner.append(node("span", "", "任务已暂停：" + (reasonNames[paused.reason] || "等待预算")), button("调整额度并继续", open));
  }

  function syncTimer() {
    clearTimeout(timer); timer = null;
    if (pageActive && !document.hidden && session && snapshot && snapshot.tasks.some(t => ["running", "paused"].includes(t.state))) timer = setTimeout(refresh, 3000);
  }

  async function refresh() {
    if (!session || !pageActive || document.hidden) return;
    const sid = session, seq = ++revision;
    if (controller) controller.abort(); controller = new AbortController();
    try {
      const data = await API.taskCostUsage(sid, { signal: controller.signal });
      if (seq !== revision || sid !== session || sid !== App.state.sessionId) return;
      snapshot = data; showBanner(); decorate();
      if (App.setSessionUsage) App.setSessionUsage(App.state.sessionUsage);
    } catch (_) { /* Keep the last authoritative snapshot on transient failures. */ }
    finally { if (seq === revision && sid === session) syncTimer(); }
  }

  function noteSessionChanged() {
    if (session === (App.state.sessionId || "")) { refresh(); return; }
    session = App.state.sessionId || ""; revision++; snapshot = null;
    if (controller) controller.abort(); clearTimeout(timer); showBanner();
    if (opened) close(); refresh();
  }

  App.taskCosts = { open, refresh, noteSessionChanged,
    activeId: sid => liveTasks.get(sid),
    usage: () => snapshot && snapshot.tracked && session === App.state.sessionId ? snapshot.usage : null,
    event: data => { if (data && data.event === "task_usage") {
      if (data.task && data.task.kind === "chat") liveTasks.set(data.task.session_id, data.task.id);
      if (session !== (App.state.sessionId || "")) noteSessionChanged(); else refresh();
    } }, decorate };
  ["taskCostsItem", "taskBudgetSettingsItem", "tokenTotal", "sessionMenuUsage"].forEach(id => {
    const entry = $(id); if (!entry) return;
    entry.addEventListener("click", open); entry.setAttribute("role", "button"); entry.tabIndex = 0; entry.title = "任务预算与完整用量";
    entry.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } });
  });
  modal.addEventListener("keydown", e => {
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(); }
    if (e.key === "Tab") {
      const items = Array.from(dialog.querySelectorAll("button,input,select")).filter(n => !n.disabled && !n.closest("[inert]") && n.getClientRects().length);
      const first = items[0], last = items[items.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  });
  let decorated = false;
  function summaryChanged(line) {
    return line.matches(".round-usage[data-round]") && decoratedTexts.get(line) !== line.textContent;
  }
  const observer = new MutationObserver(mutations => {
    if (decorated || !snapshot || !snapshot.tracked || session !== App.state.sessionId) return;
    const changed = mutations.some(mutation => {
      const target = mutation.target.nodeType === 1 ? mutation.target : mutation.target.parentElement;
      const line = target && target.closest(".round-usage[data-round]");
      // 忽略 decorate 自己写回的相同结果，以及正文/工具输出的频繁变化。
      if (line) return summaryChanged(line);
      return Array.from(mutation.addedNodes).some(node => node.nodeType === 1 &&
        (summaryChanged(node) || Array.from(node.querySelectorAll(".round-usage[data-round]")).some(summaryChanged)));
    });
    if (!changed) return;
    decorated = true;
    requestAnimationFrame(() => { decorated = false; decorate(); });
  });
  if ($("chatInner")) observer.observe($("chatInner"), { childList: true, subtree: true });
  document.addEventListener("visibilitychange", () => { if (document.hidden) { clearTimeout(timer); if (controller) controller.abort(); } else refresh(); });
  window.addEventListener("pagehide", () => { pageActive = false; clearTimeout(timer); if (controller) controller.abort(); });
  window.addEventListener("pageshow", () => { pageActive = true; refresh(); });
  window.addEventListener("focus", refresh);
  noteSessionChanged();
})(window.App);
