const assert = require("node:assert/strict");
const test = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

class Node {
  constructor(tag, className, text) {
    this.tagName = tag;
    this.children = [];
    this.textContent = text || "";
    this.listeners = {};
    this.attributes = {};
    const classes = new Set((className || "").split(" ").filter(Boolean));
    this.classList = {
      contains: value => classes.has(value),
      add: value => classes.add(value),
      remove: (...values) => values.forEach(value => classes.delete(value)),
      toggle: value => classes.has(value) ? classes.delete(value) : classes.add(value),
    };
  }
  appendChild(node) { if (node.parentNode) node.parentNode.removeChild(node); this.children.push(node); node.parentNode = this; }
  removeChild(node) { this.children.splice(this.children.indexOf(node), 1); node.parentNode = null; return node; }
  get firstChild() { return this.children[0]; }
  addEventListener(type, fn) { this.listeners[type] = fn; }
  setAttribute(name, value) { this.attributes[name] = value; }
  querySelector() { return null; }
}

function buildControls(requests) {
  const source = fs.readFileSync(path.join(__dirname, "../js/app/messages.js"), "utf8");
  const start = source.indexOf("  function buildSubAgentBlock(");
  const end = source.indexOf("  // 代码块复制（事件委托）", start);
  const context = {
    el: (tag, cls, text) => new Node(tag, cls, text),
    state: { sessionId: "different_session" }, toast() {},
    API: { async stopSubAgent(sid, aid) { requests.push([sid, aid]); return { ok: true }; } },
    App: { highlightCodeBlocks() {}, updateCodeblockCopyButtons() {}, toolIconSvg() { return '<svg class="icon"><path/></svg>'; } },
    Markdown: { render: value => value },
    subAgentTaskSummary: value => value,
    SUB_AGENT_ERROR_STATUSES: ["stopped", "timeout", "error"],
    SUB_AGENT_STATUS_TEXT: { stopped: "已停止" },
    buildThinkBlock() {
      return { wrap: new Node("div", "think-block"), done() {}, add() {}, streaming() {}, textContent() { return ""; } };
    },
  };
  vm.createContext(context);
  vm.runInContext(source.slice(start, end), context);
  return context.buildSubAgentBlock("original_session");
}

test("排队子任务可单独停止，按钮使用任务会话而不是当前切换后的会话", async () => {
  const requests = [];
  const ui = buildControls(requests);
  ui.applyEvent({ phase: "start", agent_id: "agent_one", task: "任务", queued: true, rounds_limit: 0, timeout_seconds: 0 });
  const [head, stop] = ui.wrap.children[0].children;
  assert.equal(head.tagName, "button");
  assert.equal(stop.tagName, "button");
  assert.equal(stop.hidden, false);
  await stop.listeners.click();
  assert.deepEqual(requests, [["original_session", "agent_one"]]);
  assert.equal(stop.disabled, true);
  ui.applyEvent({ phase: "done", status: "stopped", final_reply: "部分进展" });
  assert.equal(stop.hidden, true);
  assert.equal(ui.isDone(), true);
});

test("用户折叠子任务后，后续增量和排队结束通知均保留折叠状态", () => {
  const ui = buildControls([]);
  ui.applyEvent({ phase: "start", agent_id: "agent_one", task: "任务" });
  const head = ui.wrap.children[0].children[0];
  assert.equal(ui.wrap.classList.contains("open"), true);
  head.listeners.click();
  ui.applyEvent({ phase: "delta", seq: 1, reasoning_delta: "持续思考" });
  ui.applyEvent({ phase: "notice", message: "开始执行", queued: false });
  assert.equal(ui.wrap.classList.contains("open"), false);
  head.listeners.click();
  assert.equal(ui.wrap.classList.contains("open"), true);
});

test("子任务审批已允许或拒绝后不再错误显示等待确认", () => {
  for (const state of ['approved', 'denied']) {
    const ui = buildControls([]);
    ui.applyEvent({ phase: 'start', agent_id: 'child', task: '任务' });
    const badge = ui.wrap.children[0].children[0].children[3];
    ui.setApprovalWaiting({ state: 'pending' });
    assert.equal(badge.textContent, '等待确认');
    ui.setApprovalWaiting({ state, task_state: 'running' });
    assert.equal(badge.textContent, '运行中');
  }
});

test("子任务心跳更新活动和耗时，保留手动折叠且不追加正文", () => {
  const ui = buildControls([]);
  ui.applyEvent({ phase: "start", agent_id: "child", task: "任务" });
  const head = ui.wrap.children[0].children[0], body = ui.wrap.children[1], badge = head.children[3];
  head.listeners.click();
  const size = body.children.length;
  ui.applyEvent({ phase: "progress", activity: "model", elapsed_seconds: 1 });
  assert.equal(badge.textContent, "请求模型 · 1秒");
  ui.applyEvent({ phase: "heartbeat", activity: "tools", detail: "read_file", elapsed_seconds: 12.8, idle_seconds: 7, rounds: 3 });
  assert.equal(badge.textContent, "执行工具 · 12秒");
  assert.match(badge.title, /read_file.*3 轮.*7 秒/);
  assert.equal(body.children.length, size);
  assert.equal(ui.wrap.classList.contains("open"), false);
});

test("审批状态优先展示，解除后恢复最新活动，结束后迟到心跳不能改写状态", () => {
  const ui = buildControls([]);
  ui.applyEvent({ phase: "start", agent_id: "child", task: "任务" });
  const badge = ui.wrap.children[0].children[0].children[3];
  ui.setApprovalWaiting({ state: "pending" });
  ui.applyEvent({ phase: "progress", activity: "tools", detail: "edit_file" });
  assert.equal(badge.textContent, "等待确认");
  ui.setApprovalWaiting({ state: "approved" });
  assert.equal(badge.textContent, "执行工具");
  ui.applyEvent({ phase: "done", status: "stopped", final_reply: "保留进展", rounds: 2 });
  ui.applyEvent({ phase: "heartbeat", activity: "model", elapsed_seconds: 40 });
  ui.setApprovalWaiting({ state: "pending" });
  assert.equal(badge.textContent, "已停止 · 2 轮");
  assert.equal(ui.wrap.classList.contains("is-waiting"), false);
});


test("委派参数默认折叠并保留在子任务卡片内，不被启动及计划事件清除", () => {
  const ui = buildControls([]);
  ui.setDispatchInput('{"task":');
  const body = ui.wrap.children[1];
  const details = body.children[0];
  assert.equal(details.tagName, "details");
  assert.notEqual(details.open, true);
  ui.setDispatchInput('{"task":"核对接口","tools":["read_file"]}');
  assert.match(details.children[1].textContent, /read_file/);
  ui.applyEvent({ phase: "start", agent_id: "agent_one", task: "核对接口" });
  ui.applyEvent({ phase: "todo", todos: [] });
  assert.equal(body.children.filter(node => node === details).length, 1);
  assert.notEqual(details.open, true);
});


test("子任务标题提供工具SVG，详情显示完整多行任务且计划更新不重复", () => {
  const ui = buildControls([]);
  const fullTask = "任务首行" + "详细要求".repeat(50) + "\n第二行：保留末尾标记";
  ui.applyEvent({ phase: "start", task: fullTask, agent_id: "full_task" });
  const head = ui.wrap.children[0].children[0];
  assert.match(head.children[0].innerHTML, /<svg/);
  const body = ui.wrap.children[1];
  const taskSection = body.children[0];
  assert.equal(taskSection.children[1].textContent, fullTask);
  ui.applyEvent({ phase: "todo", todos: [] });
  ui.applyEvent({ phase: "todo", todos: [] });
  assert.equal(body.children.filter(node => node === taskSection).length, 1);
  assert.equal(taskSection.children[1].textContent, fullTask);
});
