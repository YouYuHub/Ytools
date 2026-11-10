# 停止（暂停）即时生效设计

> 目标：用户点「停止」后 **≤1 秒**内出流、可继续、可立即发新消息；同时绝不
> 丢数据、绝不错乱轮次。本文档对齐当前实现（P0+P1+P3）。

## 一、旧实现为什么慢

旧的安全模型是「等旧任务退出，所以它不会写」：

| 位置 | 阻塞上限 | 说明 |
|---|---|---|
| `tool_control.py` `run_sync_tool` | 20s | 内置工具取消后"陪等"线程退出 |
| `config.py` `MCP_TOOL_CALL_TIMEOUT_SECONDS` | 300s | MCP 工具超时，且同步跑、无取消通道 |
| `chat_factory.py` `_wait_worker_idle` | 15s | 停止接口在请求线程里等 worker 空闲 |
| 历史落盘 | 视争用 | 跨进程文件锁 |

三者串在一条链上，最慢的决定用户感知。`stop_chat_task` 的响应时间因此
从几百毫秒到几十秒不等。

## 二、新模型：写令牌（write lease）

核心思路从「等它退出」换成「它没有写权限」：

```
factory/agent_runtime/round_lease.py
    会话侧车：<session>_chat.jsonl.lease
    { epoch, revoked, reason, stamped_by, created, updated, pid }
```

### 生命周期

```
主进程派发  ──stamp_dispatch_lease()──▶  epoch=E, stamped_by=dispatch, revoked=0
                    （覆盖 worker 启动/工具发现/前置压缩 的 pre-run 窗口）
worker 开跑 ──adopt_dispatch_lease()──▶  收养 E（epoch 不变），改写 stamped_by=round
                    （若此时已 revoked → 说明停在 pre-run 窗口，直接结束生成）
用户停止    ──revoke_lease_file()────▶  epoch=E, revoked=1, reason=user_stop
旧任务写入  ──_assert_lease_writable()─▶  epoch 不匹配 或 revoked → 抛 RoundAbandoned
新轮开始    ──stamp_round_lease()────▶  epoch=E2, revoked=0（旧 epoch 自动失效）
```

`stamped_by` 是必需的区分：否则上一轮遗留的 `round` 戳（可能已撤销）会被下一轮
误当成"派发戳"收养，导致停止后下一轮直接判死。

### 写入口

只有两条**生成写入**路径做校验（`_assert_lease_writable`）：

- `ChatMemoryManager.add_chat_history()` —— 主写入口
- `ChatMemoryManager._finish_current_round()` —— 轮次收尾（可 `check_lease=False` / `tolerate_revoked=True`）

`_write_guard()` **不做**校验：删除/重命名/归档等管理操作也用它，不能被停止流程误伤。

### 被拒绝的写入怎么办

`RoundAbandoned` 是控制流信号，不是错误：

- 生成循环外层 `except RoundAbandoned` → 打 info 日志、安静退出；
- worker 侧收尾 `tolerate_revoked=True` → 返回 `"写令牌已撤销"`，不抛错；
- 绝不向前端推 error 帧（用户已经看到"已停止"了）。

## 三、软 / 硬停止分离

`POST /stop_chat?session_id=x&phase=soft`（默认 `soft`）

### 软停止（请求线程内，立即返回）

1. `lease.revoke("user_stop")`（inline）+ `revoke_lease_file()`（跨进程）
2. `stream.user_stop_requested = True`、`stop_requested = True`
3. `proxy.request_stop(reason="user")` —— 只发 IPC，**不等回执**
4. `_finalize_round_locally(timeout=2s)` —— 主进程自己定稿本轮为 `stopped`
5. `stream.finish()` —— SSE 生成器立刻退出，前端不用等 worker

第 4 步要写整份 JSONL（大历史几十 MB），所以给 2 秒上限；超时交给硬停止重试。
第 3、4 步并行不悖：worker 即使还在收尾，也写不进历史（令牌已撤销）。

### 硬停止（后台线程，不占 HTTP）

`_schedule_hard_stop()` → daemon 线程 → `_hard_stop()`：

1. 重试 `_finalize_round_locally`（带 `expected_epoch`）
2. inline 模式取消 `stream.task`；worker 模式 `_wait_worker_idle(15s)`
3. `_stop_saved_permission_tasks()`

全部包在 `try/except` 里：失败只打 warning，不影响已经完成的软停止。

### `expected_epoch` 守卫（防错杀新轮）

硬停止是异步的，期间用户可能已经发了新消息，`.pending` 检查点已属于新轮。
`_finalize_round_locally(expected_epoch=E)` 在写盘前比对侧车 epoch：

```python
if current and current != expected_epoch:
    logger.info("定稿跳过：新轮已接管")
    return
```

## 四、worker 侧：新轮不等旧任务

`session_worker.py` 的 `generate` 分支原逻辑无条件
`await _interrupt_worker_task(...)` —— 等旧任务完全退出才开新轮，
遇到卡住的工具线程就是十几秒。

现在按写令牌分叉：

```python
old_task.cancel()
if _lease_already_revoked(session_id):      # 已被软停止
    _reap_task_in_background(old_task, ...) # 不阻塞，交付后台监控
else:                                        # 普通打断（未停止）
    await _interrupt_worker_task(...)        # 仍等收尾，工具结果写完更安全
```

未停止的普通打断保持原语义：旧轮可能正在落盘工具结果，让它写完更安全。

## 五、代次隔离（旧轮迟到事件）

不等旧任务退出后，旧轮的 `sse` / `task_done` 会迟到。若不过滤：

- 旧 `task_done` 会把**新轮**的 `_generation_running` 误清为 `False`
- 前端看到"已结束"，实际新轮正在跑

实现：

- `_WorkerStreamAdapter.generation`，所有事件统一走 `_put()` 自动补齐 `generation`
- `task_done` 在 `finish()` 里显式携带
- `SessionWorkerProxy._dispatch()` 维护 `_latest_generation`：
  `evt.generation < _latest_generation` → 丢弃（不更新状态、不喂 handler）

## 六、状态查询口径

`GET /stream_status` 增加 `winding_down`：

```python
busy      = _session_generation_busy(sid)   # 物理：proxy 生成态 / stream 在跑
abandoned = _round_lease_revoked(sid)       # 语义：写令牌已撤销
running   = busy and not abandoned          # 面向用户
winding_down = busy and abandoned           # 已停止但资源仍在回收
```

不扣 `abandoned` 的话，`▶ 继续` 按钮要等 worker 空闲（最多 15s）才出现。

注意：破坏性操作（删除/重命名）仍用 `_session_generation_busy()` 物理口径保守
拒绝——旧工具线程可能还在改文件。

## 七、前端三态

```
streaming ──点停止──▶ stopping(软) ──soft 返回──▶ stopped（可继续/可发送）
                          │
                          └─(3s 无响应)─▶ stop_failed（降级：本地已停止，不再允许发送）
```

- **`stopping` 阶段允许发送**（`chat.ts` `send()`）：令牌已撤销、本轮已定稿，
  新消息是安全的。以前直接 `return` 会默默吞掉用户输入，是"15s 内重发被吃"的根源。
- 3 秒降级：`setTimeout(3000)` → `App.taskStatus.stopFailed(...)`，用 `typeof
  setTimeout === "function"` 保护（vm 单测沙箱没有定时器）。

## 八、数据安全论证

### 不会丢

| 数据 | 保护 |
|---|---|
| 已生成正文/工具结果 | 增量写 JSONL（临时文件 + 原子替换） |
| 当前轮未落盘部分 | `.pending` 检查点，软停止主动 `finalize_round("stopped")` |
| 用户消息 | round 起始即落盘 |
| 附件 | 独立目录，与轮次解耦 |

### 不会乱

| 风险 | 防护 |
|---|---|
| 两个写者写同一轮 | 令牌撤销后旧写者物理上无法写 |
| 轮号错位 | epoch 匹配才允许 finalize；`_round_already_recorded` 幂等 |
| 新消息与旧任务交错 | 令牌隔离，可并行，不靠时间窗口 |
| 硬停止错杀新轮 | `expected_epoch` 比对 |
| 旧轮迟到事件污染新轮 | `generation` 代次过滤 |
| worker 卡死 | 硬停止 15s 后 terminate 兜底 |

**核心差异**：旧模型"**等旧任务退出，所以它不会写**"（时间保证）；
新模型"**旧任务无权写，所以不用等**"（权限保证）。只有后者能在 5s 内完成
且不牺牲数据一致性。

## 九、改动清单

### 新增

- `factory/agent_runtime/round_lease.py`：`RoundLease`、`RoundAbandoned`、
  `stamp_dispatch_lease`、`stamp_round_lease`、`adopt_dispatch_lease`、
  `revoke_lease_file`、`check_lease_file`、侧车读写
- `test/test_round_lease_stop.py`（26）：令牌语义、跨进程、派发戳/收养、软硬分离
- `test/test_stop_resend_concurrency.py`（11）：停止后立即重发的隔离、epoch 守卫
- `test/test_worker_generation_isolation.py`（7）：代次隔离、不等旧任务

### 修改

- `memory/chat_memory.py`
  - `_assert_lease_writable()` 新增
  - `add_chat_history()` / `_finish_current_round()` 接入校验
  - `_finish_current_round(tolerate_revoked=)` 新增
  - `stop_current_round()` 传 `tolerate_revoked=True`
  - 管理器新增 `_round_lease` / `_lease_epoch` 字段
- `factory/chat_factory.py`
  - `stop_chat_task(session_id, soft=True)`：软/硬分离
  - `_finalize_round_locally(expected_epoch=)` 新增
  - `_schedule_hard_stop()` / `_hard_stop()` / `_capture_event_loop()` 新增
  - 派发前 `stamp_dispatch_lease()`；生成入口 `adopt_dispatch_lease()`
  - `run_inline()` 捕获 `RoundAbandoned` 静默退出
  - `_SessionStream.round_lease` 字段
- `factory/session_worker.py`
  - `_WorkerStreamAdapter(generation=)`、`_put()` 统一带代次
  - `_lease_already_revoked()` / `_reap_task_in_background()` 新增
  - `_dispatch()` 代次过滤、`_latest_generation`
  - `generate` 分支按令牌分叉
- `routers/chat_router.py`
  - `stop_chat(phase=)` 参数
  - `_round_lease_revoked()` 新增
  - `chat_stream_status` 增加 `winding_down`，`running` 扣除已撤销令牌
- `H5/src/app/chat.ts`
  - `send()` 允许 `stopping` 状态发送
  - 停止流程加 3s 降级守门

## 十、未做（P2，单独一轮）

- MCP 调用的取消通道（`tool_executor.py` 的 `loop.run_until_complete` 无法中断）
- `MCP_TOOL_CALL_TIMEOUT_SECONDS` 从 300 降到 60~120

现状：软停止已即时生效；硬停止在后台最长约 15s（`_wait_worker_idle`）+
工具线程自然收尾，用户不感知。P2 只影响"资源多久回收干净"，不影响体验。

## 十一、验证

```powershell
python -m pytest test/test_round_lease_stop.py test/test_stop_resend_concurrency.py `
  test/test_worker_generation_isolation.py test/test_resume_chat.py `
  test/test_outbox.py test/test_stop_wait_generation_seq.py -q
# 61 passed

cd H5
npm run typecheck; npm run build:js; npm run build:css
node --test test_h5/task_stop.test.js test_h5/stream_interruption_recovery.test.js
# 17 passed
npm test          # 992 passed
python version_assets.py
```
