# 子智能体（Sub Agent）设计文档 V1

> 版本：V1（2026-08 定稿）
> 定位：作为后续实现与修改的**唯一依据文档**；实现偏差必须回写本文档。
> V2 演进方向见第 14 节，V2 内容不在 V1 实现范围内。

---

## 0. V1 决策摘要（相对初版方案的四处修订）

| # | 议题 | 初版方案 | V1 决策 | 依据 |
|---|---|---|---|---|
| 1 | 子智能体身份 | `agent_id = 父级 tool_call_id` | **独立 `agent_id`** + `parent_agent_id` + `parent_tool_call_id` 三字段 | 一次工具调用 ≠ 一个 Agent 实例；重试歧义、嵌套扩展、归组清晰 |
| 2 | 运行态共享 | "in-process 嵌套，环境自动继承" | **SubAgentContext 显式数据类**：共享只读配置，隔离可变执行态 | 并发子任务不互踩；依赖显式化 |
| 3 | 停止机制 | "共享 run_task 停止信号" | **层级取消（CancellationToken）**：父停→全部子停；子超时/失败→只停自己 | 停止语义可预期，UI 可展示 |
| 4 | 代码组织 | "新建 sub_agent.py 精简循环（复制模式）" | **SubAgentRunner + 共享纯函数提取**，父/子共用同一批基建函数 | 防止两个 Loop 半年后漂移 |

**关于 `agent_id` vs `tool_call_id` 的结论**（回答最初疑问）：
V1 只有单层子智能体、一次派发对应一个 tool_call，此时 `tool_call_id` **功能上够用**；但独立 `agent_id` 是**更好的长期选择**，理由：
1. **重试/多派发歧义**：父模型对超时的子任务重新发起一次 `sub_agent` 调用会得到新 tool_call_id——用 tool_call_id 当身份时，"同一个子任务重试"与"两个独立子任务"在数据上无法区分；用独立 agent_id 则一次派发 = 一个新 Agent 实例，语义清晰。
2. **归组维度分离**：JSONL 中事件按 `agent_id` 聚合成块；`parent_tool_call_id` 专职"关联父级调用气泡/最终回复"。两个维度各自演进，互不污染。
3. **嵌套扩展**：V2 若允许子派孙，`parent_agent_id` 链必须有独立的实例 ID 才能表达"哪个 Agent 是父"。
4. 成本极低：仅多一次 uuid 生成与一个字段。

前端并发区分渲染统一以 `agent_id` 为块 key；`parent_tool_call_id` 仅用于把块与父级工具气泡关联。

---

## 1. 版本与范围

### 1.1 V1 目标
- 父智能体（主循环）可调用内置工具 `sub_agent`，一次可**并发派发多个**子任务；
- 子智能体拥有**独立**的消息上下文与系统提示词，可使用**与父级相同的工具**（MCP + 内置文件工具 + check_tool_exists）；
- 子任务全部轨迹（思考/正文/工具调用/结果/todo/usage）按 `agent_id` 聚合落盘为 `chat_round.events` 中的 `event="sub_agent"` 事件块；
- 父模型上下文中只出现子智能体的**最终回复**（以 role=tool 结果形态）；
- 前端按 `agent_id` 将子任务渲染为独立块（并发调用靠 id 区分），支持实时流与历史回放。

### 1.2 V1 明确不做（Non-goals）
| 项 | 说明 | V2 方向 |
|---|---|---|
| 嵌套派发 | 子智能体工具白名单**硬剔除** `sub_agent`；误调用返回 blocked 结果 | `SUB_AGENT_MAX_DEPTH` 配置 + parent_agent_id 链 |
| 子任务独立模型选择 | 沿用父级 chat_model（ambient 解析），不做 per-subagent 模型 | SubAgentContext 增加 model_config 字段 |
| 子任务内 ask_user | 子无用户通道；注入**占位定义**，调用时返回明确错误文案 | 阻塞问题写回最终回复让父决策 |
| 子任务内 read_media | 不注入（媒体部件依赖父轮上下文） | 透传 current_task_media_references |
| 子任务独立压缩 | 单任务生命周期短，靠轮次上限+超大拒绝兜底 | 独立压缩配置 |
| 子任务后台存活 | 子任务生命周期严格限制在父轮内，父轮结束/打断即终止 | 独立后台任务模型 |

---

## 2. 身份模型（三字段）

```json
{
  "agent_id": "agent_7f82c1a9",        // 子智能体实例 ID，生成时刻分配，全程不变
  "parent_agent_id": "main",           // V1 恒为 "main"（父即主循环）；V2 嵌套时为父实例的 agent_id
  "parent_tool_call_id": "call_abc123" // 父级消息链中本次 sub_agent 调用的 tool_call id
}
```

- `agent_id` 格式：`agent_` + 8 位小写 hex（`uuid.uuid4().hex[:8]`）；冲突概率在单轮 ≤ 并发上限的规模下可忽略，仍做轮内唯一性兜底（重复则重生成）。
- `agent_index`：本轮第几个子任务（0 起，按父 tool_calls 顺序），用于展示"子智能体 #1/#2"。
- `parent_agent_id` 为常量 `"main"`：V1 不参与任何逻辑，纯为 V2 预留字段（前端/统计可先忽略）。

**id 关系链（V1）**：

```
父 chat_round.events
 ├─ assistant(tool_calls=[{id: "call_abc123", name: "sub_agent"}])   ← 父级既有消息
 ├─ event=sub_agent, agent_id=agent_x, parent_tool_call_id=call_abc123, phase=start→done  ← 子任务块
 └─ role=tool, tool_call_id="call_abc123", tool_name="sub_agent", result=最终回复        ← 父级既有消息
```

---

## 3. 架构总览

```
Chat Session（worker 进程，事件循环单线程）
 │
 ▼
Main Agent（chat_factory._run_chat_generation，V1 保持现状不重构）
 │  工具分发阶段识别 tool_calls 中 name=="sub_agent"
 │
 ├── SubAgentContext A ──► SubAgentRunner A ──► asyncio.Task
 ├── SubAgentContext B ──► SubAgentRunner B ──► asyncio.Task      （asyncio.gather 并发，
 └── MCP/内置工具（原路径不变）                                    Semaphore 限流）
          │
          │  事件经父循环提供的 emit 回调（补时间戳 → JSONL 追加 + SSE 推送，事件循环内串行）
          ▼
 final_reply（仅此一条数据返回父级，包装为 tool result 汇入统一结果流）
```

原则：**共享的是不可变配置与无状态纯函数，隔离的是可变执行态**。

### 3.1 SubAgentContext（不可变派发配置）

```python
@dataclass
class SubAgentContext:
    # 身份
    agent_id: str                     # agent_<8hex>
    parent_agent_id: str              # V1 恒 "main"
    parent_tool_call_id: str
    agent_index: int
    session_id: str

    # 任务
    task: str                         # 子任务目标（父级写入，要求自包含）
    initial_todo: list[dict] | None   # 父级派发时预置的初始计划（可空）

    # 工具（派发时刻固化，子任务内不随外部变化）
    tools: list[dict]                 # 工具定义列表（已剔除 sub_agent；含 ask_user 占位定义）
    tool_servers: dict[str, str]      # 工具名 → server_id 映射（含 __builtin__ 伪服务键）
    configured_tool_names: set[str]   # check_tool_exists 用的注册表全集
    configured_tool_servers: dict[str, str]

    # 限制
    max_rounds: int                   # 模型调用轮次上限
    timeout_seconds: float            # 单个子任务整体超时
    reply_max_chars: int              # 最终回复截断保护

    # 回调（父循环注入；子 Runner 不直接持有可变全局）
    emit_event: Callable[[dict], Awaitable[None]]   # JSONL 追加 + SSE 推送
    stop_checker: Callable[[], bool]                # 父级停止信号查询（session.run_task）

    # 取消令牌（层级取消的叶子）
    cancel_token: asyncio.Event       # set() = 本子任务被要求停止（父停/超时/轮次上限）
```

### 3.2 可变执行态边界（Runner 私有，不进 context，不进全局）

| 状态 | 归属 | 说明 |
|---|---|---|
| `messages`（子任务消息序列） | Runner 实例属性 | 与父级 messages 完全独立 |
| `todo`（子任务当前计划） | Runner 实例属性 | 只写进子事件块，**不写 `_meta.todo`** |
| `usage` 累计 | Runner 实例属性（UsageAccumulator） | 收尾并入父轮 usage_total |
| `rounds` / started_at | Runner 实例属性 | done 事件携带 |
| 父级 messages / round_tools / stream 缓冲 | 父循环局部变量 | 子任务只通过 `emit_event` 回调写 SSE，不直接持有 `stream` |
| `tool_registry.ALL_TOOLS / TOOL_MCP_SERVERS` | 全局只读缓存 | 派发时刻**快照进 context**（tools/tool_servers），子任务运行期间不回读全局 |

**继承语义明确化**（回应"共享 cwd/环境"质疑）：
- cwd：worker 进程已在任务开始 chdir 到会话目录，子任务**只读继承**，不写 cwd、不切换目录；
- ambient 模型选择（ContextVar）：`asyncio.create_task` 自动复制父任务上下文，子任务读到的模型配置与父一致，且**多会话互不影响**（既有机制保证）；
- MCP 客户端：无共享连接池，`execute_tool_round` 每次调用独立建连，天然无状态；
- JSONL 写入：子任务**不直接**调用 chat_memory，一律经父循环的 emit 回调（见 6.3），保证写入发生在事件循环内同步守卫块中（跨进程文件锁不可重入，禁止工作线程写历史）。

---

## 4. 工具定义与授权

### 4.1 工具定义（`factory/agent_runtime/builtin_tools.py`）

新增常量与定义：

```python
SUB_AGENT_TOOL_NAME = "sub_agent"
```

工具 schema（function 定义）要点：
- 参数：
  - `task`（string，必填）：子任务目标。**描述中强制要求自包含**——必须写入：目标与验收标准、涉及的文件/目录绝对路径、已有事实与结论、约束（如"只读不改"）、期望的回复内容（结论+证据+路径）。
  - `todo`（array，可选）：初始任务计划，元素 `{id, content, status}`，语义与 `todo_write` 一致（≤20 项），经 `normalize_todo_items` 校验（校验失败不阻断派发，剔除该参数并在 start 事件记录告警）。
- description（父级提示词，教模型正确使用）：
  - 何时使用：可并行的独立调研/检索/批量处理子任务；需要大量中间工具调用但父级只关心结论的场景；
  - 何时不用：单次工具调用能完成的事（直接调工具）；需要用户交互的决策（用 ask_user）；
  - 关键契约：**子智能体看不到对话历史与文件块，一切必要信息必须写进 task；你只能通过它的最终回复获取结果；它无法向用户提问**；
  - 并发提示：可在同一条回复中并发派发多个相互独立的子任务。

### 4.2 注入与选择
- 加入 `SELECTABLE_BUILTIN_TOOL_NAMES`（伪服务键 `__builtin__`）→ 工具选择模态框"内置工具"分组自动出现，会话级/全局默认持久化复用现有链路，**零新增接口**；
- `is_builtin_tool()` 加入该名称（父循环自动拦截，不进 MCP 执行器）；
- `execute_builtin_tool`（check_tool_exists）的全集并入该名称；
- 总开关 `SUB_AGENT_ENABLED=false` 时：不注入定义（模型看不到）；已开启会话的旧记录回放不受影响。

### 4.3 子智能体侧工具白名单
```
= 父级本轮工具（MCP 工具 + 内置文件工具 write/edit/read/search + check_tool_exists 自动注入）
  − sub_agent（硬剔除 → V1 禁嵌套）
  − read_media（剔除）
  − ask_user（替换为占位定义：同名同参 schema，执行时返回
    "子智能体无法向用户提问；请基于已知信息决策，或在最终回复中说明阻塞点与所需信息"）
  − todo_write（保留定义，拦截后写入子任务私有 todo，见 7.2）
```

### 4.4 上下文隔离矩阵

| 内容 | 子智能体 | 说明 |
|---|---|---|
| 系统提示词 | ✅ 独立构建 | `system_prompt.py` 新增 `build_sub_agent_system_text()`（见 7.1） |
| 后端历史 / 累计摘要 / 最近问题 | ❌ | 一次性任务，不调 get_context_messages |
| 会话上传文件块 | ❌（V1） | 父级把需要的文件路径/摘要写进 task |
| 父级 `_meta.todo` | ❌ | 只用派发参数 `todo` 与自己后续的 todo_write |
| 当前用户消息原文 | ❌（V1） | task 即全部输入（父级被要求转述必要上下文） |

---

## 5. 层级取消（CancellationToken）

### 5.1 取消关系
```
父级停止（/stop_chat · 新消息打断 · worker 停止 · 上游错误）
   └─► 取消【所有】子任务          （向下传播，无条件）

单个子任务超时（SUB_AGENT_TIMEOUT_SECONDS）
单个子任务轮次达上限（SUB_AGENT_MAX_ROUNDS）
单个子任务上游流错误
   └─► 仅终止该子任务，以对应 status 返回最终回复
       （不向父级传播，不影响兄弟子任务与父任务）
```

### 5.2 实现契约
- 每个子任务一个独立 `asyncio.Task`（事件循环内并发），Runner 持有 `cancel_token: asyncio.Event`；
- 子任务循环每轮开始与每个 await 检查点评估三个条件（任一命中即收尾）：
  1. `stop_checker()` 为真（父级停止）→ 收尾 status=stopped；
  2. `cancel_token.is_set()`（超时监控触发）→ 收尾 status=timeout；
  3. `rounds >= max_rounds` → 收尾 status=max_rounds。
- **超时监控**：父循环对每个子任务包一层 `asyncio.wait_for(child_task, timeout=SUB_AGENT_TIMEOUT_SECONDS)`；`TimeoutError` → 先 `child_task.cancel()`，再由父循环统一为该子任务补写 `done(status=timeout)` 事件（兜底，防止子任务自身收尾路径也被卡死时块无收口）。
- **父级停止路径**：父循环在工具执行 gather 前登记 `sub_agent_tasks`；任务级 `finally`（`_run_chat_generation` 既有 finally）中 `cancel` 所有未完成子任务；子任务 CancelledError 处理器尽力补写 `done(status=stopped)`（写失败静默，父级兜底补写）。
- **崩溃恢复**：子事件随 `.pending` 侧车检查点逐事件落盘（emit 回调走 add_chat_history 同款检查点逻辑）；进程重启后该轮恢复为 `interrupted`，缺失 `done` 事件的子任务块由前端渲染为"已中断"（见 10.4）。
- **不传播**：子任务失败/超时**不**导致父任务终止；结果文本如实说明（"子任务未完成：原因 + 已完成进展"），由父模型决策重派或换路径。

---

## 6. SubAgentRunner（代码组织）

### 6.1 V1 结构决策
V1 **不重写父循环**（`_run_chat_generation` 与 stream/compaction/持久化深度耦合，整体重构风险不成比例）；但子循环**按 Runner 职责拆分**，且父/子共同依赖的纯函数**一次性提取到共享模块**，杜绝"两个 Loop 各改各的"：

### 6.2 共享代码提取清单（V1 必做的接口前置改动）

| 函数 | 现位置 | 目标位置 | 说明 |
|---|---|---|---|
| `_parse_sse_event` | chat_factory.py（私有） | `agent_runtime/chat_runtime.py` | 纯函数，原样搬移 |
| `_merge_tool_call_delta` / `_merge_function_call_delta` | 同上 | 同上 | 纯函数，原样搬移 |
| `_filter_tool_calls_fields` | 同上 | 同上 | 纯函数，原样搬移 |
| `_copy_for_request`（思考占位契约） | 同上 | 同上 | 纯函数；父/子请求副本共用 |
| `chat_factory.py 内 re-export 兼容 | — | 保留原 import 路径 | chat_factory 从 chat_runtime 导入并 re-export，既有测试不破 |

已在共享位置、直接复用（无需搬移）：
- `tool_executor.py`：`normalize_tool_calls` / `prepare_tool_execution` / `execute_tool_round`（线程池 MCP 执行）
- `chat_runtime.py`：`UsageAccumulator` / `estimate_*` token 估算 / `parse_return_length`
- `context_compaction.py`：`build_oversized_tool_feedback` / `make_oversized_result_preview` / 阈值解析
- `builtin_tools.py`：`execute_builtin_tool` / `try_execute_builtin_file_tool` / `normalize_todo_items` / `normalize_ask_questions`
- `system_prompt.py`：`build_runtime_system_text`（子提示词复用其内部件）

### 6.3 Runner 骨架（`factory/agent_runtime/sub_agent.py`）

```python
@dataclass
class SubAgentResult:
    agent_id: str
    parent_tool_call_id: str
    status: str            # done|error|stopped|interrupted|timeout|max_rounds
    final_reply: str       # 返回给父级的文本（超时/中断时为"进展+未完成原因"说明）
    rounds: int
    usage_total: dict
    error: str | None

class SubAgentRunner:
    def __init__(self, context: SubAgentContext): ...

    async def run(self) -> SubAgentResult:
        # 1) 预检查：task+工具定义 token 预算 vs 模型窗口（超窗 → error 收尾，不走父级降级链）
        # 2) emit(phase=start)
        # 3) 循环（≤max_rounds）：
        #      model_call() → 有 tool_calls → execute_tool_round() → 结果落 messages/事件块
        #                    → 无 tool_calls → final_reply = 正文 → done
        #      每轮顶部检查 5.2 的三个停止条件
        #    finish_reason=length → 内部"请继续"消息续写（同父循环语义）
        #    工具调用流超时 → 以失败结果反馈子模型重试（同父循环语义）
        # 4) emit(phase=done) → 返回 SubAgentResult

    async def _model_call(self) -> ...        # 流式请求/解析/增量 emit(delta)/usage 收集
    async def _execute_tool_round(self, tcs)  # 内置拦截(todo_write/ask_user占位/check_tool_exists)
                                              # + 文件工具 + execute_tool_round（复用）
    async def _emit(self, phase, **fields)    # 组装 sub_agent 事件 → context.emit_event
    def _finalize(...) -> SubAgentResult
```

### 6.4 子智能体系统提示词（`build_sub_agent_system_text()`）
- persona：你是被父智能体（Ytools 主 Agent）调度的子智能体，独立完成分派的子任务；你的最终回复是父智能体唯一能看到的产出，要写成**父智能体可直接引用**的结论报告（结论先行 + 关键证据/数值 + 涉及文件绝对路径 + 未解决事项）；
- 复用 `build_runtime_system_text()` 的工作路径、工具规则、回传长度、超时说明（参数化工作路径）；
- **整段去掉** `build_media_tag_prompt()`（媒体伪标签 / SVG / KaTeX / Mermaid / Canvas）：子回复受众是父模型，不面向用户渲染；
- 追加约束：不要向用户说话、不要等待交互、无法继续时明确说明阻塞点后收尾。

### 6.5 子任务内防护（全部复用/新增）
| 防护 | 机制 |
|---|---|
| 轮次上限 | max_rounds（默认 40） |
| 整体超时 | wait_for + cancel_token（默认 900s） |
| 单结果超大 | 复用父级同款阈值与头尾节选拒绝逻辑（独立连续计数） |
| 最终回复长度 | 超过 `reply_max_chars` 截断 + "（最终回复过长已截断，完整轨迹见子任务块）"尾注 |
| 请求预算 | 派发时 task+工具定义超窗 → error 收尾 |

---

## 7. 执行流程（时序）

```
父循环（工具分发阶段）
 ├─ 解析 tool_calls 中全部 sub_agent 调用 → SubAgentContext 列表（agent_id/agent_index 依序分配）
 ├─ tool_start 帧（补 tool_call_id 字段，见 9.2）
 ├─ asyncio.gather(
 │     [wait_for(runner_A.run(), timeout), wait_for(runner_B.run(), timeout), ...],
 │     [原 MCP/内置工具执行路径]            # 与既有 asyncio.to_thread(execute_tool_round) 并行
 │   )
 │    ├─ 每个子任务内部：见 6.3 run() 伪码
 │    └─ 事件流向：runner._emit → 父循环 emit_sub_agent_event(payload)：
 │          payload 补 timestamp → session_chat_memory.add_sub_agent_event(payload)   # JSONL
 │                                  → stream 同步推 SSE 帧                            # 实时
 ├─ 各 SubAgentResult（含失败的错误结果）包装为 {index, tool_call, tool_name, tool_args,
 │  result, error} dict，汇入既有 tool_results 列表
 └─ 后续统一路径零改动：按 index 排序 → 落盘 role=tool → SSE tool_return →
    超大拒绝 → todo 归并 → 任务内/单轮压缩检查
```

要点：
- 子任务 result 中 `result` 字段 = `final_reply`（已按 reply_max_chars 截断）；超时/中断时为进展说明文本；
- JSONL 中父级 `role=tool` 条目照常落盘（tool_call_id=call_abc123, tool_name=sub_agent），**与子任务块通过 parent_tool_call_id 关联**；
- 子任务抛出的异常不外溢：Runner 内部兜底转 error 结果；wait_for 的 TimeoutError 由父循环兜底补 done。

### 7.1 JSONL 事件块格式（最终版）

写入位置：当前 `chat_round.events` 内，独立事件条目，`event="sub_agent"`，按 `agent_id` 归组，追加式（适配 `.pending` 检查点逐事件快照）。

```jsonc
// —— start（派发）——
{"event":"sub_agent", "agent_id":"agent_7f82c1a9", "parent_agent_id":"main",
 "parent_tool_call_id":"call_abc123", "agent_index":0,
 "phase":"start",
 "task":"<子任务目标全文>",
 "todo":[{"id":"1","content":"...","status":"pending"}],      // 预置计划，可缺省
 "tools":["read_file","search_files"],                        // 实际注入的工具名列表
 "rounds_limit":40, "timeout_seconds":900,
 "timestamp":"YYYY-MM-DD HH:MM:SS"}

// —— model_call（每轮模型调用收尾时整条落盘；delta 帧不落盘）——
{"event":"sub_agent", "agent_id":"agent_7f82c1a9", "parent_tool_call_id":"call_abc123",
 "agent_index":0, "phase":"model_call", "seq":1,
 "reasoning_content":"<子智能体本轮思考全文>",
 "content":"<子智能体本轮正文>",
 "tool_calls":[{"id":"...","type":"function","function":{"name":"...","arguments":"{}"}}],
 "timestamp":"..."}

// —— tool_start / tool_result（子任务自己的工具轨迹）——
{"event":"sub_agent", "agent_id":"agent_7f82c1a9", "parent_tool_call_id":"call_abc123",
 "agent_index":0, "phase":"tool_result", "seq":1,
 "tool_call_id":"call_def456", "tool_name":"read_file",
 "arguments":"{}", "result":"<完整结果>",
 "oversized":false, "oversized_estimated_tokens":0,
 "timestamp":"..."}

// —— todo（子任务私有计划更新）——
{"event":"sub_agent", "agent_id":"agent_7f82c1a9", "parent_tool_call_id":"call_abc123",
 "agent_index":0, "phase":"todo",
 "todos":[{"id":"1","content":"...","status":"in_progress"}],
 "timestamp":"..."}

// —— done（收尾，唯一收口）——
{"event":"sub_agent", "agent_id":"agent_7f82c1a9", "parent_tool_call_id":"call_abc123",
 "agent_index":0, "phase":"done",
 "status":"done | error | stopped | interrupted | timeout | max_rounds",
 "final_reply":"<返回给父智能体的最终回复全文>",
 "rounds":3,
 "usage_total":{"prompt_tokens":0,"completion_tokens":0,"total_tokens":0},
 "error":null, "ended_at":"YYYY-MM-DD HH:MM:SS",
 "timestamp":"..."}
```

注意：
- 所有条目**不带 role 字段**（区别于消息类事件）；
- `agent_index`、`parent_agent_id` 属冗余便利字段：前端不必逐条解析父调用链即可排序/命名；
- `phase=delta` / `phase=heartbeat` 仅实时 SSE 推送、**不落盘**（与 `context_compaction` 的 delta 策略一致）；
- todo 校验失败（父给的 `todo` 参数不合法）时 start 事件缺省 todo 字段并在 task 前追加一行告警说明。

### 7.2 持久层改动清单（精确到函数）

| 文件 | 改动 | 原因 |
|---|---|---|
| `memory/chat_round_store.py` `_is_valid_event()` | 新增 `event=="sub_agent"` 分支：校验 `agent_id`/`parent_tool_call_id` 非空、`phase` ∈ {start, model_call, tool_start, tool_result, todo, done}；start 需 task 非空；done 需 status ∈ 白名单 | **不改则全部子事件被静默丢弃（最大坑）** |
| `chat_round_store.ChatRoundStore` | 新增 `record_sub_agent_event(payload)`：pending_round 存在性校验（子任务只存在于父轮进行中）→ `_is_valid_event` → 补 timestamp → append；不触发收尾 | 落盘入口 |
| `memory/chat_memory.py` | 新增 `add_sub_agent_event(payload)`：`_write_guard` 内调 record_sub_agent_event + 检查点快照（同 add_chat_history 模式） | 崩溃恢复不丢子轨迹 |
| `memory/chat_history_format.py` | `round_entry_to_context_messages`（两模式）与 `round_entry_to_compaction_text` 循环开头统一：`if event.get("event") == "sub_agent": continue` | 历史重建/压缩输入**显式排除**子轨迹（text/tool_result 模式虽因无 role 天然跳过，compaction 渲染必须显式排除） |
| `chat_round_store.current_tool_result_count()` | 无需改动（只数 role=tool） | 压缩游标不受子事件干扰 |
| `_recompute_meta_from_entries` / user_questions | 无需改动 | 按轮次聚合，子事件无 role 不影响 |
| `get_context_token_stats` | 实现时核对 round_tokens 统计口径：子轨迹不计入父上下文消息估算（不回传给模型），或单独分桶展示 | 避免统计虚高误导 |
| `docs/chat_momory_template.json` | 追加 sub_agent 条目模板与本节内容同步 | 文档即格式 |

---

## 8. SSE 协议

子任务事件经父循环 stream 发送，与 JSONL **字段同构**（前端刷新后可从 JSONL 回放），另有两类仅推流帧：

| SSE 帧 | 落盘 | 前端行为 |
|---|---|---|
| `{event:"sub_agent", phase:"start", agent_id, parent_agent_id, parent_tool_call_id, agent_index, task, todo?, tools, rounds_limit}` | ✅ | 创建子任务块（标题=task 前 60 字，状态徽标"执行中"） |
| `{event:"sub_agent", phase:"delta", agent_id, reasoning_delta?, content_delta?}` | ❌ | 块内思考/正文流式渲染 |
| `{event:"sub_agent", phase:"model_call", agent_id, seq, reasoning_content, content, tool_calls}` | ✅ | 块内追加一条模型调用记录 |
| `{event:"sub_agent", phase:"tool_start", agent_id, tool_call_id, function_name, arguments}` | ✅ | 块内工具块置"执行中" |
| `{event:"sub_agent", phase:"tool_result", agent_id, ...}` | ✅ | 块内工具块填充结果（含 blocked/timeout/oversized 标记） |
| `{event:"sub_agent", phase:"todo", agent_id, todos}` | ✅ | 块内 todo 卡片（复用父级 todo 渲染组件） |
| `{event:"sub_agent", phase:"heartbeat", agent_id, rounds, elapsed_seconds}` | ❌ | 块状态条更新"仍在执行"（长工具调用期间 15s 间隔） |
| `{event:"sub_agent", phase:"done", agent_id, status, final_reply, rounds, usage_total, error}` | ✅ | 块收口折叠为摘要条，可展开回看全量轨迹 |

- **父级 tool_start 帧补字段**：`chat_factory.py` 工具开始事件循环处，为所有帧（不只 sub_agent）追加 `tool_call_id`（additive，旧前端无害）；前端可在 `sub_agent/start` 到达前用 parent_tool_call_id 预建块占位。
- **父级 tool_return（function_name=sub_agent）**：前端不渲染为普通工具气泡，改为块尾部"最终回复已返回父智能体"引用条（点击关联父气泡）。
- **断线重连**：子帧走既有 `_SessionStream` 环形缓冲回放（replay 从轮起点），无需额外机制。
- **旧前端兼容**：未知 `event` 字段按现有默认分支忽略；`tool_return` 降级为普通工具文本展示——不影响可用性。

---

## 9. 前端渲染（H5/js/app/chat.js + history_parser.js）

- `chat.js` 事件处理链前部新增分支：`data.event === "sub_agent"` → 按 `agent_id` 路由到块实例 map（`Map<agent_id, block>`）；块按 `agent_index` 排序展示。
- 块结构：
  ```
  ┌ 子智能体 #1 · <task 前 60 字> · [状态徽标] ────────────┐
  │ ▸ 思考（弱化色、独立滚动、可折叠，复用压缩块样式）      │
  │ ▸ 工具轨迹（复用现有工具块组件，含执行中/失败/超长标记）│
  │ ▸ 任务计划（todo 卡片，复用 todo 组件）               │
  │ ▸ 各轮正文（普通文本渲染，不套富媒体）                 │
  └ ↳ 最终回复已返回父智能体（引用条）────────────────────┘
  ```
- done 后折叠为摘要条（状态 + 轮次 + tokens + 耗时），点击展开全量轨迹；子正文**不渲染媒体标签/图表代码块**（子提示词已禁止，前端仍按普通文本兜底）。
- 视觉与父级消息区分（左侧缩进 + 边框 + 图标），明确"这是子智能体产出"。
- **历史回放**：`history_parser.js` 将 `chat_round.events` 中同 `agent_id` 的条目聚合为同构块；孤儿块（有 start 无 done，status 落 done 缺失 = 崩溃/打断）渲染为"已中断"态。
- 测试归入 `H5/test_h5/`（node --test）：子事件解析聚合、并发 id 区分、孤儿块渲染。

---

## 10. 配置项（`.env`，前端聊天设置同步提供）

| 变量 | 默认 | 含义 |
|---|---|---|
| `SUB_AGENT_ENABLED` | true | 总开关（false 时不注入定义） |
| `SUB_AGENT_MAX_ROUNDS` | 40 | 单个子任务模型调用轮次上限 |
| `SUB_AGENT_MAX_CONCURRENT` | 3 | 同一父轮并发子任务上限（Semaphore；超出的排队执行） |
| `SUB_AGENT_TIMEOUT_SECONDS` | 900 | 单个子任务整体超时 |
| `SUB_AGENT_REPLY_MAX_CHARS` | 30000 | 最终回复截断保护（完整轨迹始终在 JSONL 块） |

读取口径与项目一致（`load_var` 实时求值，可变说明进子/父系统提示）。

---

## 11. 错误与边界情况矩阵

| 场景 | 行为 |
|---|---|
| 父模型在无工具轮调用 sub_agent | 工具未注入 → normalize 阶段被剔除，文本回复自然继续 |
| 父派发 task 为空/超长 | 校验失败 → 即刻返回工具错误结果（不创建块、不 emit start） |
| 派发时预算超窗 | error 收尾（final_reply 说明"任务描述过长无法在窗口内执行"），父模型可自行精简重派 |
| 子任务上游流错误（含重试上限） | error 收尾，final_reply=错误摘要；父任务继续 |
| 子任务超时 | timeout 收尾（wait_for + cancel 兜底补写），final_reply=已完成进展说明 |
| 子任务轮次达上限 | max_rounds 收尾，final_reply=进展说明 |
| 用户停止（/stop_chat / 新消息打断） | 全部子任务 stopped；父轮收尾照常 |
| 服务崩溃/worker 被杀 | 子事件已在检查点；恢复轮 interrupted；孤儿块前端渲染"已中断" |
| 子任务内模型误调 sub_agent | 工具不在白名单 → blocked 结果："子智能体不支持再派发子任务，请自行完成" |
| 子任务内模型误调 ask_user | 占位定义执行 → 明确错误文案（见 4.3） |
| 子任务内 todo 参数非法 | normalize 失败不阻断：剔除参数 + start 事件带告警说明 |
| 并发 > MAX_CONCURRENT | 排队（Semaphore），块创建时间延后属预期 |
| 旧前端 | 未知事件忽略 + 降级普通工具气泡 |
| `SUB_AGENT_ENABLED` 中途关闭 | 只影响注入；本轮已派发任务照常完成 |

---

## 12. 测试清单

后端（pytest，参照 `test/` 既有风格）：
1. `_is_valid_event`：sub_agent 各 phase 放行 / 非法 phase·缺 agent_id·缺 task(start)·非法 status(done) 拒绝；
2. `ChatRoundStore.record_sub_agent_event`：正常追加、无 pending round 忽略、时间戳回填、不触发收尾、检查点快照含子事件；
3. 历史重建：`round_entry_to_context_messages` 两模式与 `round_entry_to_compaction_text` 均跳过 sub_agent 条目；
4. 压缩游标：子事件不改变 `current_tool_result_count`；
5. SubAgentRunner（mock ChatLLM）：多轮工具循环 → done；first-call 超窗 → error；length 续写；工具流超时反馈；max_rounds 收尾；stop_checker 命中 → stopped；流错误 → error；
6. 工具白名单：子任务工具集无 sub_agent / read_media / 真实 ask_user，含 ask_user 占位与 check_tool_exists；
7. 并发：两个子任务事件按 agent_id 区分不串块；Semaphore 排队生效；
8. 超时：wait_for 触发后块有 done(status=timeout) 收口（含 Runner 卡死时父级兜底补写）；
9. 父级 tool result 契约：assistant(tool_calls) 与 role=tool 成对、id 一致；
10. 停止传播：父停止 → 子任务 cancelled → done(stopped) 落盘。

前端（node --test）：见第 9 节末。

回归：既有全部测试（共享函数提取后 chat_factory re-export 不破坏导入路径）。

---

## 13. 实施顺序

- **P0 后端最小闭环**：共享函数提取（6.2）→ builtin_tools 定义/注入 → SubAgentContext + SubAgentRunner（不含 heartbeat）→ 父循环分发/gather/结果汇入 → round_store 校验放行 + record 方法 → done 块落盘 → pytest；
- **P1 SSE + 前端**：事件帧 → chat.js 块聚合 → history_parser 回放 → 样式 → node --test；
- **P2 增强**：heartbeat、tool_start 补 id 细化、`get_context_token_stats` 口径核对、文档模板同步（chat_momory_template.json / api_docs.md / project_structure.md）。

---

## 14. V2 展望（不在 V1 范围）

1. **嵌套派发**：`SUB_AGENT_MAX_DEPTH`；`parent_agent_id` 链启用（子任务的 agent_id 成为孙任务的 parent_agent_id）；SubAgentContext 增加自身深度与"本 Agent 已派发的子任务注册表"；前端块树形缩进。
2. **per-subagent 模型**：SubAgentContext.model_config（派发参数可选指定，默认继承父）；models.json 角色扩展或派发参数直传。
3. **父循环迁移到 AgentRunner**：`_run_chat_generation` 整体迁移为 `AgentRunner(MainContext)`——V1 提取的共享函数与 Runner 职责划分即为此铺路；迁移后 main/sub 仅差 context，彻底消除双 Loop。
4. **子任务 ask_user 转发**：子阻塞问题经父转达用户（父决定何时问），回答经 task 增量注入。
5. **子任务结构化结果**：final_reply 支持 JSON（status/conclusions/files/blocks），父级与前端引用更精准。
6. **read_media 透传**与媒体降采样策略复用。
7. **独立 usage 分桶**：`_meta.sub_agent_usage` 单独统计（V1 仅并入父轮 usage_total 并在块内展示）。
