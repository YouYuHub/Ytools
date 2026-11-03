(function (App: any) {
  "use strict";
  const { $, el, state, toast } = App;
  const globalMode = $("#permissionGlobal"), sessionMode = $("#permissionSession");
  const status = $("#permissionStatus"), requests = $("#permissionRequests");
  const mounts = new Set<any>(), toolMounts = new WeakMap<object, any>(), agentMounts = new WeakMap<object, any>();
  const previews = new Map<string, any>(), taskBusy = new Set<string>();
  let mode = "ask", settings: any = null, settingsSequence = 0, suspendedTasks: any[] = [];
  const controller = ToolApprovalState.create(API, renderAll);
  function setText(node: any, value: string): void { if (node.textContent !== value) node.textContent = value; }
  function problem(error: any, notifyUser?: unknown): void {
    status.textContent = error.message || "权限请求失败，请重试";
    if (notifyUser && toast) toast(status.textContent);
  }
  function matches(mount: any, approval: any): boolean {
    const identity = mount.identity;
    if (approval.session_id !== identity.sessionId || approval.agent_id !== (identity.agentId || "main")) return false;
    const roundNode = mount.ui.wrap.closest("[data-round]");
    const round = identity.round || (roundNode && Number(roundNode.dataset.round));
    if (round && approval.round && Number(approval.round) !== Number(round)) return false;
    if (identity.taskId && identity.taskId !== approval.task_id) return false;
    return mount.kind === "agent" || approval.tool_call_id === identity.toolCallId;
  }
  function renderMount(mount: any): void {
    if (mount.ui.wrap.isConnected) mount.connected = true;
    else if (mount.connected) { mounts.delete(mount); return; }
    const items = controller.items(mount.identity.sessionId).filter((a: any) => matches(mount, a));
    const ids = new Set(items.map((a: any) => a.id));
    for (const [id, group] of mount.groups) if (!ids.has(id)) { group.wrap.remove(); mount.groups.delete(id); }
    items.forEach((a: any) => {
      let group = mount.groups.get(a.id);
      if (!group) {
        const wrap = el("span", "permission-inline-group"), label = el("span", "permission-inline-label");
        const error = el("span", "permission-inline-error"); error.hidden = true;
        const buttons = ["approve", "deny"].map(decision => {
          const button = el("button", decision === "approve" ? "permission-approve" : "permission-deny", decision === "approve" ? "允许" : "拒绝");
          button.type = "button";
          button.setAttribute("aria-label", (decision === "approve" ? "允许本次 " : "拒绝本次 ") + a.tool + (mount.kind === "agent" ? "（子任务顶部）" : ""));
          button.addEventListener("click", (event: MouseEvent) => { event.stopPropagation(); return submit(a, decision); });
          return button;
        });
        wrap.appendChild(label); buttons.forEach((button: any) => wrap.appendChild(button)); wrap.appendChild(error);
        mount.ui.approvalHost.appendChild(wrap);
        group = { wrap, label, buttons, error }; mount.groups.set(a.id, group);
      }
      const busy = controller.busy(a.id);
      setText(group.label, (mount.kind === "agent" ? a.tool + " · " : "") + (busy ? "提交中…" : a.task_state === "suspended" ? "确认后恢复" : a.state === "pending" ? "等待确认" : a.state === "denied" ? "已拒绝" : "已允许"));
      group.buttons.forEach((button: any) => { button.disabled = busy || (a.state !== "pending" && a.task_state !== "suspended"); });
      group.wrap.title = "工作目录：" + a.root + "\n" + JSON.stringify(a.args, null, 2) + (a.paths && a.paths.length ? "\n实际路径：\n" + a.paths.join("\n") : "");
    });
    mount.ui.approvalHost.hidden = !items.length;
    const outcome = controller.states(mount.identity.sessionId).filter((a: any) => matches(mount, a)).at(-1);
    const waiting = items.find((a: any) => a.state === "pending" || a.task_state === "suspended");
    if (mount.ui.setApprovalWaiting) mount.ui.setApprovalWaiting(waiting || items[0] || null, outcome);
  }
  function bind(ui: any, identity: any, kind: string, registry: WeakMap<object, any>): void {
    if (!ui || !ui.approvalHost || !identity.sessionId) return;
    let mount = registry.get(ui.wrap);
    if (!mount) { mount = { ui, identity: {}, kind, groups: new Map() }; registry.set(ui.wrap, mount); }
    Object.assign(mount.identity, identity); mounts.add(mount); renderMount(mount);
  }
  function errors(id: string, message: string): void {
    for (const mount of mounts) {
      const group = mount.groups.get(id);
      if (group) { group.error.hidden = !message; setText(group.error, message || ""); }
    }
  }
  async function submit(a: any, decision: string): Promise<void> {
    if (controller.busy(a.id)) return;
    errors(a.id, "");
    try {
      const result = await controller.decide(a.id, decision);
      status.textContent = decision === "approve" ? "已允许本次操作" : "已拒绝，本次不会执行";
      if (result.ticket) authorizePreview(a, result.ticket);
      else if (decision === "deny" && a.agent_id === "preview") {
        for (const entry of previews.values()) if (entry.approvalId === a.id) {
          entry.failed = true;
          if (entry.ui) { entry.ui.setOutput("已拒绝，本次媒体不会加载。"); entry.ui.finish(); }
        }
      }
      if (a.agent_id !== "preview" && state.sessionId === a.session_id && App.openSession &&
        (result.resumed || a.task_state === "suspended" || !(state.streaming && state.streamingSession === a.session_id))) {
        await App.openSession(a.session_id);
      }
    } catch (error) {
      errors(a.id, (error as Error).message || "提交失败，请重试");
      for (const mount of mounts) if (mount.kind === "tool" && matches(mount, a) && mount.ui.setApprovalError) {
        mount.ui.setApprovalError((error as Error).message || "提交失败，请重试");
      }
      if (state.sessionId === a.session_id) problem(error, true);
    } finally { await refreshPending(a.session_id); }
  }
  function renderSettingsTasks(): void {
    requests.replaceChildren();
    const items = controller.items(state.sessionId);
    if (items.length) requests.appendChild(el("div", "permission-meta", items.length + " 项操作等待确认，请在会话中的工具标题栏选择允许或拒绝。"));
    suspendedTasks.filter(task => task.session_id === state.sessionId).forEach(task => {
      const hasApproval = items.some((a: any) => a.task_id === task.id);
      const card = el("section", "permission-card"); card.appendChild(el("strong", "", "挂起的任务"));
      card.appendChild(el("div", "permission-meta", task.unknown_execution ? "工具执行结果尚未确定，不能自动重跑。请先核对实际结果，再放弃并重新发送任务。" : "服务已停止，可以恢复原任务或放弃。"));
      const actions = el("div", "permission-actions");
      (hasApproval ? ["cancel"] : ["resume", "cancel"]).forEach(actionName => {
        const action = el("button", actionName === "resume" ? "permission-approve" : "permission-deny", actionName === "resume" ? "恢复原任务" : "放弃此任务");
        action.type = "button"; action.disabled = taskBusy.has(task.id) || (actionName === "resume" && task.unknown_execution);
        action.addEventListener("click", async function () {
          if (taskBusy.has(task.id)) return;
          taskBusy.add(task.id); renderSettingsTasks();
          try {
            await API[actionName === "resume" ? "resumePermissionTask" : "cancelPermissionTask"](task.id, task.session_id);
            if (actionName === "resume" && state.sessionId === task.session_id && App.openSession) await App.openSession(task.session_id);
          } catch (error) { problem(error, true); }
          finally { taskBusy.delete(task.id); await refreshPending(task.session_id); }
        }); actions.appendChild(action);
      }); card.appendChild(actions); requests.appendChild(card);
    });
  }
  function renderAll(): void { mounts.forEach(renderMount); renderSettingsTasks(); }
  async function refreshPending(sid: string): Promise<void> {
    if (!sid) { suspendedTasks = []; renderAll(); return; }
    try {
      const data = await controller.refresh(sid);
      if (data && state.sessionId === sid) {
        suspendedTasks = data.tasks || []; renderAll();
        if (App.taskStatus) App.taskStatus.snapshot(sid, data);
      }
    } catch (error) { if (state.sessionId === sid) problem(error); }
  }
  async function refresh(): Promise<void> {
    const sid = state.sessionId;
    try {
      const config = await API.getPermissions(sid || undefined);
      if (state.sessionId !== sid) return;
      mode = config.mode; await refreshPending(sid); scanMedia();
    } catch (error) { if (state.sessionId === sid) problem(error); }
  }
  async function loadSettings(): Promise<void> {
    const sequence = ++settingsSequence, sid = state.sessionId;
    settings = null; globalMode.disabled = true; sessionMode.disabled = true; status.textContent = "";
    try {
      const config = await API.getPermissions(sid || undefined);
      if (sequence !== settingsSequence || state.sessionId !== sid) return;
      mode = config.mode; globalMode.value = config.global_mode; sessionMode.value = config.session_mode || "";
      settings = { sid, global: globalMode.value, session: sessionMode.value };
      const readonly = (window as unknown as { YtoolsAuth?: any }).YtoolsAuth && !(window as unknown as { YtoolsAuth: any }).YtoolsAuth.executionAllowed;
      settings.readonly = !!readonly;
      globalMode.disabled = !!readonly; sessionMode.disabled = !!readonly || !sid;
      if (readonly) status.textContent = "当前账号未获工具执行授权，权限模式只读。";
      await refreshPending(sid);
    } catch (error) { if (sequence === settingsSequence) problem(error); }
  }
  async function saveSettings(): Promise<void> {
    if (settings && settings.readonly) return;
    if (!settings || settings.sid !== state.sessionId) throw new Error("权限配置尚未加载，请重新打开聊天设置");
    const saved = settings, global = globalMode.value, session = sessionMode.value;
    try {
      if (global !== saved.global) { await API.setPermissions(global, null); saved.global = global; }
      if (saved.sid && session !== saved.session) { await API.setPermissions(session || null, saved.sid); saved.session = session; }
      status.textContent = "已保存，从下一次任务生效"; await refresh();
    } catch (error) { problem(error); throw error; }
  }
  function authorizePreview(approval: any, grant: any): void {
    const source = approval.args.references[0];
    for (const [key, entry] of previews) if (key.startsWith(approval.session_id + "\n") && (entry.approvalId === approval.id || entry.canonical === source)) {
      entry.grant = grant; entry.canonical = source; entry.failed = false;
      if (entry.ui) { entry.ui.setOutput("已允许本次媒体展示。"); entry.ui.finish(); }
    }
    scanMedia();
  }
  function showPreview(entry: any, node: any, sid: string): void {
    if (!entry.approval || !node.isConnected || state.sessionId !== sid || !App.buildToolBlock || (entry.ui && entry.ui.wrap.isConnected)) return;
    const a = entry.approval;
    entry.ui = App.buildToolBlock(a.tool || "read_media", { sessionId: sid, agentId: "preview", toolCallId: a.tool_call_id, taskId: a.task_id });
    entry.ui.setInput(FormatUtils.prettyJson(a.args));
    const target = node.closest("video,audio,picture") || node;
    target.parentNode.insertBefore(entry.ui.wrap, target);
  }
  function scanMedia(): void {
    document.querySelectorAll('img[src],audio[src],video[src],iframe[src],source[src],a[href]').forEach(node => {
      const attr = node.hasAttribute("src") ? "src" : "href";
      let url: URL; try { url = new URL(node.getAttribute(attr) as string, location.href); } catch (_) { return; }
      if (!url.pathname.endsWith("/permissions/media")) return;
      if (url.searchParams.has("grant")) {
        try {
          const ticket = JSON.parse(atob(url.searchParams.get("grant")!.split(".")[0].replace(/-/g, "+").replace(/_/g, "/")));
          if (ticket.expires * 1000 > Date.now()) return;
        } catch (_) {}
        url.searchParams.delete("grant"); node.setAttribute(attr, url.href);
        previews.delete(url.searchParams.get("session_id") + "\n" + url.searchParams.get("source"));
      }
      const sid = url.searchParams.get("session_id") as string, source = url.searchParams.get("source");
      if (sid !== state.sessionId || !source) return;
      const key = sid + "\n" + source;
      let entry = previews.get(key);
      if (entry) {
        if (entry.grant) { const next = API.permissionMediaUrl(sid, entry.canonical || source, entry.grant); if (node.getAttribute(attr) !== next) node.setAttribute(attr, next); }
        else if (!entry.failed) showPreview(entry, node, sid);
        return;
      }
      entry = { source }; previews.set(key, entry);
      API.previewPermission(sid, source).then((data: any) => {
        if (previews.get(key) !== entry) return;
        Object.assign(entry, { canonical: data.source, grant: data.ticket, approvalId: data.approval && data.approval.id, approval: data.approval });
        if (data.approval) { controller.apply({ phase: "requested", approval: data.approval }); showPreview(entry, node, sid); }
        if (data.ticket) scanMedia();
      }).catch((error: any) => { entry.failed = true; if (state.sessionId === sid) problem(error); });
    });
  }
  if (typeof MutationObserver !== "undefined") new MutationObserver(function () {
    // Tool blocks are bound before insertion; reconcile once after insertion, without observing our own button updates.
    mounts.forEach(mount => { if (!mount.connected && mount.ui.wrap.isConnected) renderMount(mount); else if (mount.connected && !mount.ui.wrap.isConnected) mounts.delete(mount); });
    scanMedia();
  }).observe(App.chatInner, { childList: true, subtree: true });
  App.permissions = { refresh, loadSettings, saveSettings, syncPending: refreshPending, receive: (event: any) => controller.apply(event),
    track: (approval: any) => controller.track(approval), mode: () => mode,
    bindTool: (ui: any, identity: any) => bind(ui, identity, "tool", toolMounts),
    bindAgent: (ui: any, identity: any) => bind(ui, identity, "agent", agentMounts) };
})((window as unknown as { App: any }).App);
