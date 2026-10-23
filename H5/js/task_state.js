/** Task execution and stream connection are tracked separately. No DOM or timers. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.TaskState = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";
  const labels = {
    connecting: "正在连接", model: "等待模型", thinking: "正在思考", answering: "生成回答",
    tools: "执行工具", retrying: "正在重试", compacting: "压缩上下文",
    waiting_approval: "等待操作确认", waiting_budget: "等待调整预算", waiting_user: "等待你的回复",
    stopping: "正在停止", stopped: "已停止", completed: "已完成", failed: "任务出错",
    suspended: "任务已中断，等待恢复", disconnected: "连接已中断", ended: "任务已结束", background: "后台运行",
  };
  function initial() {
    return { phase: "model", connection: "connecting", terminal: false, tools: Object.create(null), approvals: Object.create(null),
      error: "", stopped: false, budget: false, question: false, compactionScope: "" };
  }
  function reduce(previous, action) {
    const s = Object.assign({}, previous || initial(), {
      tools: Object.assign(Object.create(null), previous && previous.tools),
      approvals: Object.assign(Object.create(null), previous && previous.approvals),
    });
    if (action.type === "connection") { s.connection = action.value; return s; }
    if (action.type === "stopping") { if (!s.terminal && s.phase !== "stopping") { s.resumePhase = s.phase; s.phase = "stopping"; } return s; }
    if (action.type === "stop_failed") { if (!s.terminal) { s.phase = s.resumePhase || "model"; s.error = action.error || "停止失败，请重试"; } return s; }
    if (action.type === "stopped") { s.stopped = true; s.phase = "stopped"; s.terminal = true; return s; }
    if (action.type === "end") {
      s.connection = action.completed || s.stopped ? "closed" : "disconnected";
      if (s.stopped) s.phase = "stopped";
      else if (["failed", "suspended", "ended"].includes(s.phase)) { /* Preserve an authoritative terminal state. */ }
      else if (s.question) s.phase = "waiting_user";
      else if (action.completed) s.phase = "completed";
      s.terminal = s.terminal || !!action.completed || s.stopped || s.phase === "failed";
      return s;
    }
    if (action.type === "suspended") { s.phase = "suspended"; s.terminal = true; return s; }
    if (action.type === "ended") { s.phase = "ended"; s.terminal = true; s.connection = "closed"; return s; }
    if (s.terminal) return s;
    const stopping = s.phase === "stopping";
    if (stopping) s.phase = s.resumePhase || "model";
    const data = action.data || {};
    if (data.event === "tool_approval" && data.approval && data.approval.agent_id !== "preview") {
      const a = data.approval;
      const key = a.id || [a.agent_id, a.tool_call_id].join(":");
      if (action.data.phase === "requested" || a.state === "pending") s.approvals[key] = true;
      else delete s.approvals[key];
    } else if (data.event === "task_usage" && data.task && data.task.kind === "chat") {
      const task = data.task;
      s.budget = task.state === "paused";
      if (task.state === "cancelled") { s.stopped = true; s.phase = "stopped"; s.terminal = true; }
      else if (task.state === "failed") { s.phase = "failed"; s.error = "任务异常结束"; s.terminal = true; }
      else if (task.state === "suspended") { s.phase = "suspended"; s.terminal = true; }
    } else if (data.event === "ask_user") {
      s.question = true; s.phase = "waiting_user";
    } else if (data.event === "context_compaction" || data.context_compaction || data.event === "responses_compaction") {
      const c = data.context_compaction || data;
      const terminalPhase = data.event === "responses_compaction"
        ? ["done", "error"].includes(c.phase)
        : ["done", "aborted"].includes(c.phase);
      if (terminalPhase) {
        s.phase = "model";
        s.compactionScope = "";
      } else {
        s.phase = "compacting";
        // 压缩明细：本地 session/round 与官方压缩窗口分开显示。
        if (data.event === "responses_compaction") s.compactionScope = "native";
        else if (c.scope === "session" || c.scope === "round") s.compactionScope = c.scope;
        else if (!s.compactionScope) s.compactionScope = "session";
        s.error = "";
      }
    } else if (data.error) {
      s.phase = data.retrying ? "retrying" : "failed";
      s.terminal = !data.retrying;
      s.error = String(data.error).slice(0, 300);
    } else if (data.tool_start) {
      const t = data.tool_start;
      s.tools[t.tool_call_id || t.function_name || "tool"] = t.function_name || "";
      s.phase = "tools";
      s.compactionScope = "";
    } else if (data.tool_return) {
      const t = data.tool_return;
      delete s.tools[t.tool_call_id || t.function_name || "tool"];
      s.phase = Object.keys(s.tools).length ? "tools" : "model";
      if (s.phase !== "compacting") s.compactionScope = "";
    } else if (typeof data.reasoning_content === "string" && data.reasoning_content) { s.phase = "thinking"; s.error = ""; s.compactionScope = ""; }
    else if (typeof data.content === "string" && data.content) { s.phase = "answering"; s.error = ""; s.compactionScope = ""; }
    else if (Array.isArray(data.tool_calls) && data.tool_calls.length) { s.phase = "model"; s.compactionScope = ""; }
    if (stopping && !s.terminal) { s.resumePhase = s.phase; s.phase = "stopping"; }
    return s;
  }
  function view(s) {
    if (!s) return { hidden: true };
    let phase = s.phase;
    if (!s.terminal && phase !== "stopping") {
      if (s.connection === "disconnected") phase = "disconnected";
      else if (s.connection === "connecting") phase = "connecting";
      else if (s.connection === "background") phase = "background";
      else if (s.budget) phase = "waiting_budget";
      else if (Object.keys(s.approvals).length) phase = "waiting_approval";
    }
    const pending = Object.keys(s.approvals).length, count = Object.keys(s.tools).length;
    let detail = "";
    if (phase === "disconnected") detail = "后端状态尚未确认，重新连接可继续查看";
    else if (phase === "waiting_approval") detail = pending + " 项操作待确认，请在工具标题栏处理";
    else if (phase === "waiting_budget") detail = "可在任务预算中调整额度并继续";
    else if (phase === "waiting_user") detail = "回复后开始下一轮";
    else if (phase === "suspended") detail = "请在原工具标题栏确认恢复，或在设置中放弃旧任务";
    else if (phase === "ended") detail = "请查看会话中的最后结果";
    else if (phase === "background") detail = "打开会话可继续查看";
    else if (phase === "compacting") {
      detail = s.compactionScope === "round" ? "本轮工具轨迹"
        : s.compactionScope === "native" ? "官方压缩窗口"
        : "跨轮历史";
    }
    else if (phase === "tools") detail = count > 1 ? count + " 个工具进行中" : Object.values(s.tools)[0] || "";
    return { phase, label: labels[phase] || "正在处理", detail, error: s.error,
      reconnect: phase === "disconnected", tone: ["failed", "disconnected"].includes(phase) ? "error"
        : ["waiting_approval", "waiting_budget", "waiting_user", "suspended"].includes(phase) ? "waiting"
          : s.terminal ? "done" : "active" };
  }
  return { initial, reduce, view };
});
