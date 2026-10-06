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
