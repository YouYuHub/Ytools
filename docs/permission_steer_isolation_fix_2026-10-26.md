# 权限即时生效与引导/队列会话隔离修复

2026-10-26。修复两个运行时状态隔离问题。

## 1. 权限变更对进行中任务立即生效

根因：`PermissionRuntime.__init__` 把 `store.policy(session)["mode"]` 快照写入
`self.context["mode"]` 与任务 `data["mode"]`，运行期 `plan_tool` 只读该快照
（`needs_approval = context["mode"] == "ask" and bool(reason)`）；账号全局模式
缓存在 `principal.resource_cache["account_preferences"]`，worker 进程内不会自动失效。

修复（`factory/agent_runtime/permission_runtime.py`）：

- 新增 `current_policy_mode(session)`：先 `pop("account_preferences")` 绕过偏好缓存，
  再取 `store.policy(session)["mode"]`（会话覆盖走 SQLite，天然实时）。
- 新增 `refresh_mode()`：重读最新模式，变化时更新内存上下文并回写 `tasks.data["mode"]`。
- `authorize()`（每批工具调用）与 `begin_call()`（每次执行前）开头调用 `refresh_mode()`。
- `guard_path()` 与 `permission_urlopen()`（账号/非账号两条路径）每次调用前刷新，
  长任务内的后续文件/网络访问不再被旧快照拦截。
- `wait()` 审批等待循环：进入等待前与每次 1 秒唤醒后检查，若当前模式为
  `full_control` 则把该 pending 审批置为 `approved` 并立即放行；任务已停止时不放行，
  读库失败保持等待。切回 `ask` 不追溯已发出的审批。

语义：切到完全控制 = 无需审批（新调用直接通过，等待中的自动放行）；
切回询问 = 新调用恢复审批，已放行的不追回。

## 2. 引导/队列消息跨会话串扰

根因（均为前端渲染层，后端队列本身已按会话隔离）：

- `H5/js/app/stream_pipeline.js` 的 `message_injected` 分支无条件
  `App.appendUserMessage(...)`，把后台会话的引导气泡插入当前正在看的会话 DOM。
- `H5/js/app/sessions.js` 的 `startNewChat()` 未调用 `App.renderPendingOutbox()`，
  新建会话后输入框上方的暂存/待注入提示残留上一个会话条目。

修复：

- `message_injected` 先 `clearInjectedPending(sessionId, message_id)` 清提示并置
  `hasStage = true`，随后 `if (state.sessionId !== sessionId) return;`——后台会话
  的消费不插入当前 DOM，切回该会话时由历史回放显示气泡。
- `startNewChat()` 末尾增加 `App.renderPendingOutbox()`，新对话（`sessionId=null`）
  不显示任何旧会话暂存条目。

队列消息复核结论：`pendingQueue`/`steerMessage`/`injectedPending` 均带 `sessionId`；
`renderPendingOutbox()` 只显示当前会话条目；`flushPendingMessages(target)` 在
`state.sessionId !== target`、压缩中、有待答提问、目标会话仍在流式时均直接返回；
`chat.js` 的附接流（`attachStreamSession`）与停止/切换（`detachAttachedStream`）
均校验会话；后端 `inject_user_message`/`cancel_injected_message` 按
`peek_worker_proxy(session_id)` / `_get_session_stream(session_id)` 取会话独立队列。
队列不存在跨会话串扰，无需额外修复。

## 回归

- `python -m py_compile`（permission_runtime/policy/store、chat_factory、session_worker）：通过。
- `node --check`（stream_pipeline/sessions/composer）：通过。
- `pytest test_permission_decision_api test_permission_generation test_permission_recovery
  test_permission_worker test_inject_message`：38 passed。
- `pytest test_permission_round_edits test_permission_resume_native_reset
  test_per_session_stop test_chat_stream_attach`：10 passed。
