/** Current-session background terminals. Event-driven discovery and active-only polling. */
(function () {
  "use strict";
  const button = document.getElementById("terminalJobsBtn");
  const badge = document.getElementById("terminalJobsCount");
  if (!button || !window.API) return;
  const api = window.API;
  const labels = { running: "运行中", completed: "已结束", failed: "失败", stopped: "已停止", unknown: "进程已退出 / 状态未知" };
  let jobs = [], selected = null, overlay = null, panel, list, info, output, notice, stop, closeJob, download, follow;
  let listBusy = false, outputBusy = false, actionBusy = false, disposed = false, generation = 0, mutation = 0, previousFocus;
  let scopeSession, listRequest = null, outputRequest = null;
  let listTimer = null, outputTimer = null, refreshPending = false, needsRetry = false, retriesLeft = 0;

  function syncPolling() {
    const visible = !disposed && !document.hidden && Boolean(scopeSession);
    const pollList = visible && (jobs.length > 0 || (needsRetry && retriesLeft > 0));
    const pollOutput = visible && jobs.length > 0 && Boolean(overlay && selected);
    if (pollList && listTimer === null) {
      listTimer = setInterval(() => {
        if (listBusy || actionBusy) return;
        if (!jobs.length && needsRetry) retriesLeft--;
        return refreshList();
      }, 3000);
    } else if (!pollList && listTimer !== null) {
      clearInterval(listTimer); listTimer = null;
    }
    if (pollOutput && outputTimer === null) outputTimer = setInterval(refreshOutput, 1000);
    else if (!pollOutput && outputTimer !== null) { clearInterval(outputTimer); outputTimer = null; }
  }

  // Discovery is event-driven while idle. Coalesce triggers during an in-flight
  // request so a command created after an empty snapshot cannot be missed.
  function requestListRefresh() {
    syncScope();
    refreshPending = true;
    retriesLeft = 2; // Failed idle discovery gets two retries, then waits for an event.
    return refreshList();
  }

  function currentSession() { return window.App && window.App.state && window.App.state.sessionId || null; }
  function syncScope() {
    const id = currentSession();
    if (id === scopeSession) return false;
    scopeSession = id; mutation++; generation++;
    if (listRequest) listRequest.abort();
    if (outputRequest) outputRequest.abort();
    jobs = []; selected = null;
    refreshPending = false; needsRetry = false; retriesLeft = 0;
    syncEntry();
    return true;
  }
  function noteSessionChanged() { if (syncScope()) requestListRefresh(); }
  function noteToolResult(sessionId, name) {
    if (!sessionId || sessionId !== currentSession()) return;
    if (name === "run_command" || name === "poll_command" || name === "stop_command") return requestListRefresh();
  }

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }
  function makeButton(text, action, className) {
    const node = element("button", className, text);
    node.type = "button";
    node.addEventListener("click", action);
    return node;
  }
  async function timedRequest(fn, kind) {
    const controller = new AbortController();
    if (kind === "list") listRequest = controller;
    else outputRequest = controller;
    const timer = setTimeout(() => controller.abort(), 10000);
    try { return await fn({ signal: controller.signal }); }
    finally {
      clearTimeout(timer);
      if (listRequest === controller) listRequest = null;
      if (outputRequest === controller) outputRequest = null;
    }
  }
  function hidePanel() {
    if (!overlay) return;
    generation++;
    overlay.remove(); overlay = null;
    button.setAttribute("aria-expanded", "false");
    if (previousFocus === button && button.classList.contains("hidden")) {
      const plus = document.getElementById("plusBtn");
      if (plus) plus.focus();
    } else if (previousFocus && previousFocus.isConnected) previousFocus.focus();
    syncPolling();
  }
  function syncEntry() {
    button.classList.toggle("hidden", !jobs.length);
    badge.textContent = jobs.length > 99 ? "99+" : String(jobs.length);
    button.title = "本会话后台终端（" + jobs.length + " 个运行中）";
    if (!jobs.length) hidePanel();
    syncPolling();
  }
  function removeJob(id) {
    mutation++;
    if (listRequest) listRequest.abort();
    jobs = jobs.filter(job => job.job_id !== id);
    if (selected === id) selected = null;
    syncEntry();
    if (!overlay) return;
    if (!selected) selectJob(jobs[0].job_id);
    else renderList();
  }
  function renderList() {
    if (!overlay) return;
    const activeId = document.activeElement && document.activeElement.getAttribute ?
      document.activeElement.getAttribute("data-terminal-job") : null;
    list.replaceChildren();
    for (const job of jobs) {
      const item = makeButton("", () => selectJob(job.job_id));
      item.setAttribute("data-terminal-job", job.job_id);
      item.setAttribute("aria-pressed", String(job.job_id === selected));
      item.append(element("strong", "", labels[job.status] || job.status), element("span", "", job.command || job.job_id));
      list.append(item);
      if (activeId === job.job_id) item.focus();
    }
    if (!jobs.length) list.textContent = "没有后台终端";
  }
  function setInfo(job) {
    const elapsed = Math.max(0, Math.floor(Date.now() / 1000 - (job.created_at || Date.now() / 1000)));
    info.textContent = [job.command || job.job_id,
      (labels[job.status] || job.status) + " · " + job.shell + " · PID " + job.pid +
        (job.status === "running" ? " · 已运行 " + Math.floor(elapsed / 60) + " 分 " + elapsed % 60 + " 秒" : "") +
        (job.exit_code != null ? " · 退出码 " + job.exit_code : ""),
      "目录：" + (job.cwd || ""), "会话：" + (job.session_id || "未指定"),
      job.status === "running" && !job.can_stop ? "旧任务缺少进程身份记录，关闭只隐藏记录；停止请使用系统终端。" : ""
    ].filter(Boolean).join("\n");
    stop.disabled = actionBusy || !job.can_stop;
    closeJob.disabled = actionBusy;
    closeJob.textContent = job.status === "running" && job.can_stop ? "停止并关闭" : "关闭记录";
    download.href = api.terminalLogUrl(job.job_id, scopeSession);
  }
  function selectJob(id) {
    if (id === selected && output.textContent) return;
    selected = id; generation++;
    output.textContent = ""; notice.textContent = "加载输出…";
    follow.checked = true;
    renderList();
    const job = jobs.find(item => item.job_id === id);
    if (job) setInfo(job);
    syncPolling();
    refreshOutput();
  }
  async function refreshList() {
    syncScope();
    const sessionId = scopeSession;
    if (!sessionId) { refreshPending = false; syncPolling(); return; }
    if (disposed || listBusy || document.hidden || actionBusy) return;
    listBusy = true;
    refreshPending = false;
    const version = mutation;
    try {
      const data = await timedRequest(options => api.listTerminalJobs(sessionId, options), "list");
      if (disposed || version !== mutation || sessionId !== currentSession()) return;
      // Also filter old server responses during an upgrade; never count finished/foreign jobs.
      jobs = (data.jobs || []).filter(job => job.session_id === sessionId && job.status === "running");
      needsRetry = false;
      syncEntry();
      if (!overlay) return;
      if (!jobs.length) { hidePanel(); return; }
      if (!jobs.some(job => job.job_id === selected)) selectJob(jobs[0].job_id);
      else { renderList(); setInfo(jobs.find(job => job.job_id === selected)); }
    } catch (error) {
      if (!disposed && version === mutation && sessionId === currentSession()) needsRetry = true;
      if (overlay && version === mutation && sessionId === currentSession()) notice.textContent = "无法更新终端列表：" + error.message;
    } finally {
      listBusy = false;
      syncPolling();
      if (!disposed && (refreshPending || version !== mutation || sessionId !== currentSession())) refreshList();
    }
  }
  async function refreshOutput() {
    syncScope();
    if (disposed || outputBusy || document.hidden || !overlay || !selected || actionBusy) return;
    const id = selected, version = generation, sessionId = scopeSession;
    outputBusy = true;
    try {
      const data = await timedRequest(options => api.getTerminalJob(id, sessionId, options), "output");
      if (!overlay || id !== selected || version !== generation || disposed || sessionId !== currentSession()) return;
      if (data.status !== "running") { removeJob(id); return; }
      setInfo(data);
      // Pause snapshots while browsing old output so rotating tails cannot displace the viewport.
      if (follow.checked || !output.textContent) {
        if (output.textContent !== data.output_tail) output.textContent = data.output_tail || "（暂无输出）";
        if (follow.checked) output.scrollTop = output.scrollHeight;
      }
      notice.textContent = !follow.checked ? "已暂停输出显示，勾选“跟随输出”恢复。" :
        data.output_truncated ? "显示最近 2,000 行，最多约 13 万字符；完整输出可下载日志。" : "输出每秒自动更新；程序自身的输出缓冲可能延迟显示。";
    } catch (error) {
      if (overlay && id === selected && version === generation && sessionId === currentSession()) notice.textContent = "读取输出失败：" + error.message;
    } finally {
      outputBusy = false;
      if (!disposed && overlay && id !== selected) refreshOutput();
    }
  }
  async function operate(operation) {
    syncScope();
    if (!selected || actionBusy) return;
    const id = selected, sessionId = scopeSession;
    actionBusy = true;
    mutation++;
    generation++;
    stop.disabled = closeJob.disabled = true;
    try {
      const data = await api.operateTerminalJob(id, operation, sessionId);
      if (sessionId === currentSession() && (operation === "close" || data.status !== "running")) removeJob(id);
    } catch (error) {
      if (sessionId === currentSession()) {
        if (overlay) notice.textContent = error.message;
        if (window.App && window.App.toast) window.App.toast(error.message);
      }
    }
    finally {
      actionBusy = false;
      await requestListRefresh();
      await refreshOutput();
    }
  }
  function showPanel() {
    syncScope();
    if (overlay) { hidePanel(); return; }
    if (!jobs.length) return;
    previousFocus = document.activeElement;
    overlay = element("div", "terminal-overlay");
    panel = element("section", "terminal-panel"); panel.id = "terminalJobsPanel";
    panel.setAttribute("role", "dialog"); panel.setAttribute("aria-modal", "true"); panel.setAttribute("aria-label", "后台终端管理");
    const header = element("div", "terminal-header");
    const dismiss = makeButton("收起", hidePanel);
    header.append(element("h2", "", "后台终端 · 当前会话"), dismiss);
    list = element("nav", "terminal-list"); list.setAttribute("aria-label", "后台终端列表");
    info = element("div", "terminal-info");
    output = element("pre", "terminal-output"); output.tabIndex = 0; output.setAttribute("aria-label", "终端输出");
    notice = element("div", "terminal-notice"); notice.setAttribute("role", "status");
    const controls = element("div", "terminal-actions");
    stop = makeButton("停止", () => operate("stop"), "terminal-stop");
    closeJob = makeButton("关闭记录", () => operate("close"));
    download = element("a", "", "完整日志"); download.target = "_blank"; download.rel = "noopener";
    const label = element("label", "terminal-follow");
    follow = document.createElement("input"); follow.type = "checkbox"; follow.checked = true;
    label.append(follow, document.createTextNode("跟随输出"));
    follow.addEventListener("change", refreshOutput);
    output.addEventListener("wheel", event => { if (event.deltaY < 0) follow.checked = false; }, { passive: true });
    output.addEventListener("scroll", () => {
      if (output.scrollHeight - output.scrollTop - output.clientHeight > 24) follow.checked = false;
    }, { passive: true });
    controls.append(stop, closeJob, download, label);
    const detail = element("div", "terminal-detail"); detail.append(info, controls, output, notice);
    const body = element("div", "terminal-body"); body.append(list, detail);
    panel.append(header, body); overlay.append(panel);
    overlay.addEventListener("click", event => { if (event.target === overlay) hidePanel(); });
    overlay.addEventListener("keydown", event => {
      if (event.key === "Escape") { event.stopPropagation(); hidePanel(); }
      if (event.key === "Tab") {
        const nodes = Array.from(panel.querySelectorAll('button:not(:disabled),a[href],input,[tabindex="0"]'));
        const first = nodes[0], last = nodes[nodes.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
      }
    });
    document.body.append(overlay);
    button.setAttribute("aria-expanded", "true");
    selected = null;
    renderList();
    if (jobs.length) selectJob(jobs[0].job_id);
    requestListRefresh(); dismiss.focus();
  }
  button.addEventListener("click", showPanel);
  if (window.App) window.App.terminalJobs = { noteSessionChanged, noteToolResult, refresh: requestListRefresh, open: showPanel };
  document.addEventListener("visibilitychange", () => {
    syncPolling();
    if (!document.hidden) { requestListRefresh(); refreshOutput(); }
  });
  window.addEventListener("focus", () => { requestListRefresh(); refreshOutput(); });
  window.addEventListener("pagehide", () => {
    disposed = true; mutation++; generation++;
    if (listRequest) listRequest.abort();
    if (outputRequest) outputRequest.abort();
    syncPolling();
  });
  window.addEventListener("pageshow", event => {
    if (!event.persisted || !disposed) return;
    disposed = false;
    requestListRefresh(); refreshOutput();
  });
  requestListRefresh();
})();
