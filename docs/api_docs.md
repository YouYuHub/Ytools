# API 接口说明

服务地址: `http://<host>:48621` ｜ 在线文档: `/docs`（Swagger）

## 1. 聊天 ChatLLM

### POST /chat_with_tool
聊天主接口，**SSE 流式**响应（`text/event-stream`）。

**请求体** (application/json):

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| messages | `Message[]` | 必填 | 对话消息。`Message` = `{role, content?, name?, tool_calls?, tool_call_id?, refusal?, reasoning_content?}`，role ∈ `user/assistant/system/tool` |
| tool_names | `string[]?` | null | 本轮选择的工具白名单（唯一入口）；不传则无工具模式，传未知工具名会被忽略并告警 |
| session_id | `string` | "default" | 会话ID，隔离聊天历史与文件记忆 |
| max_tokens | `int` | 8192 | 最大生成 token |
| temperature | `float` | 0.7 | 随机性；未显式传时按 `model_selection.chat_model.parameter` 填充 |
| top_p | `float` | 1.0 | 核采样；同上 |
| reasoning_effort | `string` | "medium" | 思考深度 `low/medium/high`；同上 |
| presence_penalty | `float` | 2.0 | 重复惩罚；同上 |
| timeout_connect/read/drain | `int` | 300/1800/120 | 连接/读取/冲刷超时（秒） |
| extra_body | `dict?` | null | 额外参数（如 enable_thinking） |
| parallel_tool_calls | `bool` | true | 允许并行工具调用 |
| tool_choice | `string\|dict` | "auto" | `none/auto/required` 或指定函数 |
| use_backend_history / backend_history_rounds | `bool/int?` | null | 是否启用后端历史摘要拼接；摘要-only 模式下 `backend_history_rounds` 为兼容参数，不限制已完成历史回传 |
| tools | - | - | 服务端内部字段，勿传 |

**响应** (SSE 事件，每行 `data: {json}`，以 `data: [DONE]` 结束):
- `warning`：提示（如未选工具）
- `reasoning_content` / `content`：思考与正文增量
- `tool_calls`：工具调用增量（按 index 合并）
- `tool_return`：工具执行结果 `{function_name, arguments, result}`
- `usage`：token 统计；`finish_reason`：结束原因（stop/length/tool_calls）
- `todo`：内置 `todo_write` 工具执行成功后的任务计划推送，`{"event":"todo","todos":[{content,status(pending|in_progress|done)}]}`；启用方式：「配置工具」模态框首位的「内置工具」分组勾选 `todo_write`（伪服务 `__builtin__`，与 MCP 工具共用工具选择持久化，见"工具选择持久化"；后端按名称识别、本地执行并落盘 `_meta.todo`，当前计划同时注入系统提示词供模型跨轮感知）
- `ask_user`：内置 `ask_user` 工具被调用时推送，`{"event":"ask_user","questions":[{question, options[], multiple}]...}`；启用方式同上（「内置工具」分组勾选 `ask_user`）；前端弹出交互卡片（逐题点选选项或自由输入，`multiple=true` 的题目可同时选择多个选项、答案以顿号拼接；**每题作答后才能提交**），**用户提交回答后回答文本作为下一条用户消息发送**，开启新一轮生成。模型调用 `ask_user` 的当轮任务在推送后立即暂停收尾（工具结果为 `waiting_user` 占位），等待用户回答；同一轮并行多次 `ask_user` 调用的问题会合并展示。前端仅允许回答“最新提问”：提问卡片之后一旦出现普通用户消息（新任务）或更新的提问，该卡片转为过期仅可查看（点击提示）。**覆盖式重答**：对最新提问再次回答时，后端截断该提问轮之后的旧回答轮（`truncate_rounds_for_reanswer`，一个问题只保留一个答案轮次；提问后已开启普通新任务时不截断、按追加处理），前端同步清除提问卡片之后的旧回答显示后继续新轮次；会话仍在流式输出时不允许提交回答
- `context_compaction`：单轮工具轨迹被压缩后的状态，含 `{scope:"round", before_tokens, after_tokens, fallback, compress_index, block_count, usage}`；`usage` 为压缩模型调用返回的 token 统计（部分服务端不返回时为 null）；新模式 `block_count` 通常为 1 个累计摘要块；工具原始输出仍已推送并保存，只会从后续模型请求上下文中替换为摘要
- `context_compaction`（**实时压缩事件**，开始/完成两个阶段，见下方"压缩事件格式"）：单轮与跨轮压缩均实时推送，**同一份 payload 同时落盘 JSONL 独立事件行**（`event="context_compaction"`），保证前端"加载历史"与"实时显示"字段完全一致

#### 压缩事件格式（SSE 实时推送 = JSONL 落盘，字段一致）

| 场景 | 阶段 | payload 字段 |
|---|---|---|
| 单轮压缩开始 | `start` | `{"event":"context_compaction","scope":"round","phase":"start","role":"assistant","compress_context":<将被压缩的本轮轨迹节选，≤800 tokens>}` |
| 单轮压缩完成 | `done` | `{"event":"context_compaction","scope":"round","phase":"done","role":"assistant","summary_text":<累计摘要全文>,"compress_usage":{usage..., before_tokens, after_tokens, fallback, block_count, cumulative:true}}` |
| 跨轮压缩开始 | `start` | `{"event":"context_compaction","scope":"session","phase":"start","role":"assistant","context_summary":<将压缩的旧轮次节选，≤800 tokens>}` |
| 跨轮压缩完成 | `done` | `{"event":"context_compaction","scope":"session","phase":"done","role":"assistant","summary_text":<累计摘要全文>,"summary_usage":{usage..., compressed_rounds, fallback, cumulative:true}}` |
| 累计摘要更新 | `done` | `{"event":"context_compaction","scope":"round/session","phase":"done","summary_text":<累计摘要全文>,"...usage":{usage..., cumulative:true}}` |
| 压缩中断标记 | `aborted` | `{"event":"context_compaction","scope":"round/session","phase":"aborted","role":"assistant","reason":"task_interrupted"}` |

- 推送顺序：**先推 SSE 帧、后落盘 JSONL**——实时显示优先，落盘失败（如 Windows 文件被搜索索引/杀软短暂占用）只打 WARN，不阻塞推送。
- JSONL 落盘行在 `timestamp` 之外与 SSE payload 逐字段一致；该行不参与 `usage`/`user_questions`/`chat_round` 统计（仅 `record_count` 计数）。
- 跨轮和单轮压缩都将新摘要合并为一个累计摘要块；合并结果随当前 `done` 事件返回，不再额外发送 `merge_block_count` 事件。
- 单轮压缩的 `before_tokens` 为全量上下文（历史+系统提示+当前轮+工具定义）；当前轮轨迹不足阈值一半时跳过压缩（防空转，超窗主要来自历史时交由跨轮压缩/首调用预算检查处理）。
- **压缩失败策略**：压缩模型未配置或配置不可用时自动使用当前聊天模型压缩；压缩模型调用失败时换聊天模型重试一次；聊天模型仍失败则抛出终止级错误，**任务终止**（不再节选降级）——请求前/单轮压缩失败会立即推送错误帧并停止生成。
- **任务中断恢复**：压缩结果采用"完成后一次性写入"，中断不会留下半成品数据。若上次任务在压缩进行中被终止（有 `start` 无 `done`），下次请求开始时会自动补一条 `aborted` 事件（前端把对应条目置为中断态），未覆盖的轮次由常规压缩按预算**重新压缩**。

### POST /stop_chat
停止指定会话的聊天任务（**按会话隔离**，仅停止该 `session_id` 的后台生成任务，不影响其他会话仍在进行的任务）。
- Query: `session_id`（默认 "default"）
- 返回: `{"stop_chat": "stopped"}`；失败 400

### GET /chat_stream/status
查询指定会话当前是否有后台生成任务（前端刷新/切换会话后据此决定是否续接 SSE 续看）。
- Query: `session_id`（默认 "default"）
- 返回: `{"running": bool, "session_id": "已规整的会话标识"}`

## 2. 聊天历史 ChatHistory

> **会话标识以文件名为准**：历史文件位于 `history_files/<session_id>_chat.jsonl`，`session_id` 即文件名去掉 `_chat.jsonl` 后缀的部分。
> 所有接口的 `session_id` 参数均可直接传文件名（后端自动去掉 `_chat.jsonl` / `.jsonl` 后缀并规整非法字符，空值回退 `"default"`）。
> `_meta` 元数据**不再包含 `session_id` 字段**，避免文件名与元数据不一致导致错乱。

| 接口 | 方法 | 参数 | 返回 |
|---|---|---|---|
| /chat_history/sessions | GET | - | 会话文件名列表 `["default_chat.jsonl", ...]` |
| /chat_history/file | GET | session_id | 下载会话 JSONL 文件（首行 `_meta` 元数据） |
| /chat_history/meta | GET | session_id | `{title, user_questions, usage, created_at, updated_at, record_count, completion_count, context_summary, upload_id?}`；**只读接口**：优先仅解析首行 `_meta`（正常文件与全量重算结果一致），不再把全量内容重写回盘；首行不可靠（缺失/损坏/标题为默认值/缺 updated_at 或 user_questions）时回退全量重算兜底，两种路径均不落盘 |
| /chat_history/title | PUT | title(必填), session_id | `{state, describe, title, meta}`；更新首行 `_meta` 的 `title` 字段，title 为空报 400 |
| /chat_history/delete_file | DELETE | session_id | `{state, describe}`；**连带删除**该会话上传文件目录 `history_files/upload/`（目录名取 `_meta.upload_id`，旧记录回退按 session_id 推导）；任一侧删除失败返回 500 `{detail}` |
| /chat_history/delete_lines | DELETE | startline(必填,≥1), endline(必填,≥1,包含), session_id | `{state, describe, meta_after, usage_after}`；行号**不含** `_meta` 首行；startline>endline 报 400 |
| /chat_history/upload_chat_file | POST | 见下方说明 | 见下方说明 |

> **`_meta.upload_id`（可选字段）**：本会话上传文件所在目录名（`history_files/upload/<upload_id>/`）。上传目录按**上传时前端传入的 `session_id`** 命名，可能与聊天会话文件名不一致，故记录于此，供 `/chat_history/delete_file` 连带清理。仅从新记录开始维护，已有旧会话无此字段（删除时按 session_id 推导兜底）。

### POST /chat_history/upload_chat_file
接收前端上传的 jsonl 聊天历史文件，按现有格式过滤后保存到 `history_files` 目录。

- **Form**: `file`(必填, jsonl 文件)；**Query**: `session_id`(默认 "default"，可传文件名)、`overwrite`(默认 false)
- 过滤规则：
  - 首行若为 `{"_meta": {...}}` 则作为基础元数据（其中 `session_id` 字段会被移除）；
  - 其余行通过 `parse_round_entry` 验证，只保留能正确解析为 `chat_round` 的条目（含非空 `question`、至少一条 `user` 事件、合法 `status`），无法解析的行丢弃并计入 `skipped_lines`；
  - 写入后重新计算 `user_questions` / `usage` / `record_count` / `completion_count`。
- **同名冲突**：目标文件 `<session_id>_chat.jsonl` 已存在且 `overwrite=false`（默认）时，自动追加时间戳另存为 `<session_id>_<YYYYMMDD_HHMMSS>_chat.jsonl`，避免覆盖旧会话；`overwrite=true` 时强制覆盖。
- **返回**: `{state, session_id(实际写入), filename(实际保存文件名), total_lines, imported_rounds, skipped_lines, record_count, completion_count, title, overwrite, collision(bool, 是否同名另存), meta}`

## 3. 上下文 Context

| 接口 | 方法 | 参数 | 返回 |
|---|---|---|---|
| /chat_context/token_stats | GET | session_id(默认"default"), max_rounds(可选，≤0全部；省略时跟随 `HISTORY_COMPACT_KEEP_ROUNDS`，默认 20), include_tools(默认false), tool_names(可重复，按已选工具过滤) | 见下方说明 |
| /chat_context/compact_manual | POST | session_id(默认"default"), force(默认false；为 true 时跳过下限守卫), stream(默认false) | stream=false: `{state, session_id, compressed_rounds, stats}`；stream=true: SSE 流（见下方说明）；上下文估算低于摘要预算且未 force 时拒绝（stream=false 返回 400 `{detail}`，stream=true 由结果帧 error 带回提示） |

**POST /chat_context/compact_manual?stream=true（SSE 手动压缩）**

手动确认后不再以当前 token 是否超过门槛决定是否执行，而是与自动跨轮流程统一：把全部已完成轮次纳入累计摘要，并重建全历史最近用户问题索引（最多 10k tokens）。累计摘要目标预算为 `聊天窗口 × summary_budget_ratio`；原始历史仍保存在 JSONL，但不再回传给模型。事件与聊天内自动压缩完全同构：

- **下限守卫**：模型上下文估算（`token_stats.messages_tokens` 口径，仅消息部分）低于摘要总预算（`聊天窗口 × summary_budget_ratio`）时拒绝执行——摘要可能不比原文小，压缩无收益；提示形如"当前上下文约 X tokens，低于摘要预算 Y tokens（模型窗口 × 摘要预算比例 Z），压缩无收益，已取消本次手动压缩"。`force=true` 跳过该守卫
- `{"event":"context_compaction","scope":"session","phase":"start","context_summary":...}`：待压缩内容预览
- `{"event":"context_compaction","phase":"delta","reasoning_content"?:...,"content"?:...}`：压缩模型流式增量（仅实时推送，不落盘）
- `{"event":"context_compaction","phase":"done","summary_text":<摘要全文>,"summary_usage":{...}}`：完成（summary_text 随同一 payload 落盘）
- `{"event":"compaction_manual_result","compressed_rounds":N,"stats":{...},"error":null}`：结果帧
- `data: [DONE]` 结束
### GET /chat_context/token_stats
查看指定会话**模型上下文 token 构成**统计。进入摘要模式后，口径为“累计摘要 + 全历史最近问题 + 当前 pending 任务”；未生成摘要的旧会话暂以原始轮次估算，供首次压缩前展示：

- `context_token_limit`：当前聊天模型最大输入窗口（model.json `maxInputTokens`）
- `messages_tokens`：历史消息（含跨轮摘要 system 消息）估算 token
- `tool_definition_tokens`：MCP 工具定义估算 token（仅 `include_tools=true` 时计入）
- `tool_names`：前端当前选择的工具名称；与 `include_tools=true` 一起传入时只统计这些工具定义，并包含运行时注入的 `check_tool_exists` schema
- `request_context_tokens`：`messages_tokens + system_prompt_tokens + tool_definition_tokens`
- `estimated_budget_ratio`：`request_context_tokens / context_token_limit`（四舍五入到 4 位小数）
- `rounds`：`{total(总轮数), summarized(已被跨轮摘要覆盖), retained(当前 pending 任务轮数), max_rounds}`
- `has_context_summary` / `summary_text_length`：是否已有跨轮摘要及正文长度
- `context_compress_count` / `history_compress_count`：单轮压缩累计调用次数 / 跨轮压缩累计调用次数
- `history_context_mode`：`summary_only` 或 `legacy_raw`
- `raw_history_rounds_sent`：实际回传给模型的已完成原始轮次数，新模式为 `0`
- `recent_questions_count` / `recent_questions_length`：全历史最近用户问题索引的条数/字符数
- `round_tokens[]`：新模式只统计当前 pending 任务轮；未生成摘要的旧会话才统计原始历史轮次

### POST /chat_context/compact_manual
手动触发指定会话的**跨轮历史压缩**，与自动压缩共用同一流程（`compact_session_history_if_needed`）：把全部未覆盖的已完成轮次合并进一个累计 `context_summary`，并从全部 `chat_round.question` 重建最近 10k tokens 的原始问题索引。压缩模型 usage 累计落盘（`_meta._history_compress_usage`）。压缩过程事件按与 SSE 相同的结构落盘为 JSONL 独立行（`event="context_compaction"`，scope="session"），历史加载时可见（手动压缩无 SSE 流可推，仅落盘）。

- `compressed_rounds`：本次新纳入累计摘要的轮次数；`0` 表示历史已经全部进入摘要模式且没有新增轮次
- `stats`：压缩后重新计算的 token 统计（字段同 `GET /chat_context/token_stats`）
- 压缩模型未配置或无效时返回 400 `{detail}`

## 4. 配置 ChatConfig

| 接口 | 方法 | 参数 | 返回 |
|---|---|---|---|
| /change_chat_dir | POST | Query `new_dir`(必填) | `{state, message, current_dir, persisted_env}`；设置**全局默认**工作目录（.env `DEFAULT_CHAT_WORK_DIR`），仅作为新会话初始目录与未覆盖会话的默认值；空路径 400 |
| /chat_config/work_dir | GET | `session_id`（可选） | 基础字段 `{state, current_dir, persisted_dir, default_dir, is_consistent, env_name, env_value, env_file, source, read_only}`；携带 `session_id` 时附加 `{session_id, session_dir, session_dir_valid, effective_dir, is_overridden, warning}`：`session_dir` 为会话 `_meta.work_dir` 覆盖值（未设置为 null），`effective_dir` 为会话实际生效目录（覆盖 → 全局默认 → 进程 cwd），目录失效回退时 `warning` 给出提示 |
| /chat_config/work_dir | POST | Body `{session_id, work_dir}` | `{state, message, session_id, session_dir, effective_dir, warning, updated_at}`；设置/清除会话独立工作目录（写会话 `_meta.work_dir`）。`work_dir` 非空时校验目录存在（不存在 400）；空串/None 清除覆盖恢复跟随默认。对正在运行的任务不生效，下一轮生成任务开始时生效 |
| /chat_config/history_compaction | GET | - | `{keep_rounds, trigger_ratio, summary_budget_ratio, summary_total_budget, compaction_model, available_compaction_models[], defaults, env_names, memory_state}` |
| /chat_config/history_compaction | POST | Body 见下方完整策略 | `{state, updated, config, memory_state}` |
| /chat_config/models | GET | `role`（可选，默认 chat_model）、`session_id`（可选） | `{state, count, current, selection, models[], role, role_info}`；`models[]` 为扁平 `{provider_name, model_name, model_id, api_type, url, vision, tool_calling, max_input_tokens, max_output_tokens, api_key_present}`；`model_name` 为 models.json 中 models 字段的键名（写入 model_selection 的值），`model_id` 为请求 API 使用的模型 id；`selection` 为全局三模型选择；`role_info` 为指定角色的详情：`selection`（provider/model/api_type/parameter 分桶）、`effective_parameter`（按回退链解析的生效参数）、`available_models`/`available_count`（可选模型，compaction_model 只列 chat-completions）、compaction 额外带 `compaction_status`；携带 `session_id` 时 `role_info` 为该会话生效结果并附加 `is_overridden`，响应额外返回 `{session_id, session_selection, effective_selection, warning}`（`session_selection` 为会话 `_meta.model_selection` 覆盖值，未设置为 null；失效覆盖回退全局时 `warning` 给出提示） |
| /chat_config/models/select | POST | Body `{provider, model, role?, parameter?, session_id?, clear?}` | 不携带 `session_id`（全局默认）：`{state, message, current, effective_parameter, selection}`；组合不存在报 400（compaction_model 必须为 chat-completions 协议）。携带 `session_id`（会话级）：`{state, message, session_id, session_selection, effective_selection, warning, role, role_info}`；写入该会话 `_meta.model_selection.<role>`，不修改全局 models.json；`clear=true` 清除该角色会话覆盖恢复跟随全局（忽略 provider/model） |
| /chat_config/context_return | GET | - | `{reasoning_max_length, tool_result_max_length, defaults, semantics, env_names, memory_state}` |
| /chat_config/context_return | POST | Body `{reasoning_max_length, tool_result_max_length}`（整数，缺省时使用默认值） | `{state, updated, config, memory_state}`；写回 .env 并同步内存 env_vars，下次聊天立即生效 |
| /chat_config/mcp_tools | GET | - | `{call_timeout_seconds, defaults, semantics, env_names, memory_state}`；读取 MCP 工具单次执行超时 |
| /chat_config/mcp_tools | POST | Body `{call_timeout_seconds}`（非负数，0 表示不限制） | `{state, updated, config}`；写回 .env 并同步内存，下一次工具调用立即生效 |
| /chat_config/tool_selection | GET | `session_id`（可选） | `{state, inputs, servers[], config_path, memory_state}`；每次以磁盘 mcp_servers.json 的 `inputs` 为准并同步内存（手工编辑文件后刷新页面即生效），未提及的已配置服务补 `[]`；`inputs` 可含内置工具伪服务键 `__builtin__`（值如 `["todo_write","ask_user"]`）；携带 `session_id` 时附加 `{session_id, session_selection, effective_selection, is_overridden, warning}`：`session_selection` 为会话 `_meta.tool_selection` 覆盖值（未设置为 null），`effective_selection` 为会话实际生效选择（会话覆盖 → 全局 `inputs`） |
| /chat_config/tool_selection | POST | Body `{inputs: {服务名: [工具名...]}, session_id?}` | 不携带 `session_id`（全局默认）：`{state, message, updated, inputs, memory_state}`；服务名必须已在 `servers` 中配置或为内置工具伪服务 `__builtin__`（其余未知服务报 400）；全量替换语义，未提及的已配置服务保存为 `[]`，未提及的 `__builtin__` 不写入（= 未勾选内置工具）；实时更新内存并写回 mcp_servers.json（仅替换 `inputs` 键）。携带 `session_id`（会话级）：`{state, message, session_id, session_selection, effective_selection, is_overridden, updated_at}`；写入该会话 `_meta.tool_selection`，不修改全局 `inputs`；空 `inputs` 清除会话覆盖恢复跟随全局默认；会话级不做未知服务校验（失效服务名在生成时自动忽略并告警） |

### 工作目录与会话独立 worker 进程

工作目录分两层，解析顺序为「会话覆盖 → 全局默认 → 进程 cwd」：

- **全局默认**：`.env` 的 `DEFAULT_CHAT_WORK_DIR`（`POST /change_chat_dir` 设置），作为新会话的初始目录与未覆盖会话的默认值；
- **会话级覆盖**：会话历史 JSONL 首行 `_meta.work_dir`（`POST /chat_config/work_dir` 设置，空值清除覆盖），仅在用户显式设置时落盘（惰性）。

生成任务默认运行在**每会话独立的 worker 进程**（`factory/session_worker.py`）：worker 在每次生成任务开始时解析会话生效目录并 `os.chdir`，因此系统提示词中的工作路径、MCP 工具子进程的默认目录、相对路径解析都随会话隔离；覆盖目录失效时向前端推送 `warning` 事件（`code=WORK_DIR_FALLBACK`）并回退默认目录，不终止任务。目录切换对正在运行的任务不生效，下一轮任务开始时生效。

- `.env` 的 `CHAT_WORKER_MODE=inline` 可回退为旧版主进程内执行（目录语义退化为全局 cwd，仅供测试/故障兜底）；`CHAT_WORKER_IDLE_TIMEOUT_SECONDS` 控制 worker 空闲自退出时间（默认 900 秒，进程随用随建）；
- 会话 JSONL 的写入方（worker 进程写轮次/压缩/usage，主进程写标题/删除/导入/上传记录）通过 `<session>_chat.jsonl.lock` 跨进程文件锁互斥，进程崩溃时由操作系统自动释放。

### 工具选择与会话隔离

工具选择同样分两层，解析顺序为「会话覆盖 → 全局默认」：

- **全局默认**：`setting/mcp_servers.json` 顶层 `inputs` 键（不携带 `session_id` 的 `POST /chat_config/tool_selection` 修改），作为**新建会话前**选择工具时的默认值（前端在新对话中保存工具选择即写入此处）；
- **会话级覆盖**：会话历史 JSONL 首行 `_meta.tool_selection`（携带 `session_id` 的 `POST /chat_config/tool_selection` 修改，空 `inputs` 清除覆盖），结构与 `inputs` 一致（`{服务名: [工具名]}`），仅在用户显式设置时落盘（惰性）。
- **内置工具**（`todo_write` / `ask_user`，后续可扩展 subAgent 等）并入同一链路：前端在「配置工具」模态框首位的「内置工具」分组勾选，保存为伪服务键 `__builtin__`（如 `{"__builtin__": ["ask_user"]}`）；生成时后端把该键下的名称与 MCP 工具名一并作为 `requested_names`，按名称识别注入（`inject_builtin_tools`），会话级/全局默认语义与 MCP 工具完全一致。

生成请求 `tool_names` 字段的语义保持「前端传入优先」；仅当请求**完全未携带** `tool_names`（`null`，区别于显式空列表 `[]`）时，后端回退为该会话的生效工具选择（`_meta.tool_selection` → 全局 `inputs`），并向前端推送 `warning` 事件（`code=TOOL_SELECTION_FALLBACK`，全局配置读取失败时触发）。显式传 `[]` 仍表示无工具模式。清空会话历史时 `_meta.tool_selection` 与 `_meta.work_dir` 一样予以保留。

### 模型选择与会话隔离

模型选择（聊天/压缩/标题模型，角色集合随版本扩展）同样分两层，按角色独立解析「会话覆盖 → 全局默认」：

- **全局默认**：models.json 顶层 `model_selection` 键（不携带 `session_id` 的 `POST /chat_config/models/select` 修改），作为**新建会话前**选择模型时的默认值；
- **会话级覆盖**：会话历史 JSONL 首行 `_meta.model_selection`（携带 `session_id` 的 select 修改，`clear=true` 清除角色覆盖），结构为 `{角色: {ownership_name, model_name, parameter, api_type}}`，仅存已覆盖的角色；未覆盖角色、清除后的角色跟随全局默认。

生成/压缩时的生效链路：

- 聊天模型：`/chat_with_tool` 的参数默认值填充、生成任务内的模型解析（`require_default_chat_config`）、上下文窗口估算均按会话生效模型计算；
- 压缩模型：跨轮/单轮压缩的模型与参数按会话生效选择解析（手动压缩接口 `/chat_context/compact_manual` 同样按会话生效）；
- 标题模型：当前仅保存配置（标题生成暂未启用）；
- 会话覆盖的模型已从 models.json 删除（手工编辑/热重载后）时，生成任务推送 `warning` 事件（`code=MODEL_SELECTION_FALLBACK`）并按角色回退全局默认，不终止任务；
- 会话首次覆盖某角色且未传 parameter 时，沿用全局该角色的参数桶（"仅切换模型、参数保持不变"语义与全局一致）；清空会话历史时 `_meta.model_selection` 予以保留。

实现说明：会话覆盖在生成任务/压缩任务开始时合并为"生效选择"并注入任务级上下文（ambient 覆盖），任务内所有模型配置读取自动按会话生效；`asyncio.create_task` 的上下文隔离保证多会话互不影响，未设置覆盖的路由层读取保持全局语义。

### 配置文件热重载（不依赖 uvicorn --reload）

主进程内运行一个全局守护线程 `config-hot-reload`（`util/config_watcher.py`），按固定间隔轮询两个配置文件，与内存中的 sha256 hash 比对：

- `setting/mcp_servers.json`：hash 变化且 JSON 解析成功后，同步工具选择内存快照；仅当 `servers` 键内容变化时才重新探测 MCP 工具（更新 `tool_registry.ALL_TOOLS` 并刷新 `/tools/list` 探测缓存，见"工具列表缓存与预热"），只改 `inputs` 不触发昂贵的工具发现；
- `setting/models.json`：hash 变化且解析成功后重载 `models_config` / `model_selection` / `setting_vars`（`env_manager.reload_models_config()`，不触碰 .env）。

行为约定：

- **解析失败不更新**：文件写了一半 / JSON 非法时保留内存现状，hash 也不更新（下一轮自动重试），终端打印 `[config-watch] ... 解析失败`；
- **原子赋值**：models.json 重载与模型选择写接口均为"先构建完整配置、再整体替换全局变量"，请求协程读到的要么是旧配置要么是新配置；工具注册表的全局交换由 `_REGISTRY_SWAP_LOCK` 保护；
- **间隔配置**：`.env` 的 `CONFIG_HOT_RELOAD_INTERVAL_SECONDS`（默认 5 秒，`<=0` 禁用轮询）；worker 子进程不跑该线程（每个生成任务开始时自行 init_path 刷新配置并按磁盘 mcp_servers.json 重新发现工具）；
- 更新/失败信息均打印 `[config-watch]` 前缀的调试日志；接口写入（如保存模型选择）也会触发 hash 变化并重载，属幂等操作。

### 回传长度配置

`POST /chat_config/context_return` 控制两类“回传给大模型的最大长度”（整数）：

```json
{
  "reasoning_max_length": 2048,
  "tool_result_max_length": 0
}
```

- `reasoning_max_length`（对应 env `REASONING_RETURN_MAX_LENGTH`，默认 2048）：本轮思考过程（`reasoning_content`）随带工具调用的 assistant 消息回传时保留末尾 N 字符。
- `tool_result_max_length`（对应 env `HISTORY_TOOL_RESULT_RETURN_MAX_LENGTH`，默认 0）：后端历史轮次重建上下文时，单个工具结果回传前 N 字符；回传格式符合 Chat Completions 规范（`assistant.tool_calls` → `tool.tool_call_id`）。
- 两个值共用语义：`0` = 不回传，负数 = 全部回传，正数 = 按 N 截断（思考过程保留末尾，工具结果保留开头）。

### MCP 工具执行超时配置

`GET/POST /chat_config/mcp_tools` 控制单次 MCP 工具调用的总超时，覆盖连接服务器、初始化会话和实际调用过程。配置写入项目 `.env` 的 `MCP_TOOL_CALL_TIMEOUT_SECONDS`，并同步运行时内存，下一次工具调用立即使用新值：

```json
{
  "call_timeout_seconds": 300
}
```

- 正数：超过指定秒数后中止本次调用，并将超时错误作为工具结果反馈给模型；
- `0`：不设置超时，工具若自身永久阻塞仍可能无法返回，不建议用于不可控的本地终端工具；
- 负数：请求接口返回 400，不会覆盖已有配置。

工具执行已放入工作线程，因此工具阻塞期间事件循环仍可处理 `GET /chat_context/token_stats` 和 `POST /stop_chat`。手动停止会同时设置会话停止标记、取消后台生成任务并等待其完成收尾；正在执行的同步工具线程无法被 Python 强制杀死，会由上述超时配置最终释放，推荐将超时设置为合理的正数。

### 模型选择（三种模型 + 参数）

模型选择存于 `setting/models.json` 顶层 `model_selection` 键（与 provider 目录同文件，读写即时生效，不再依赖 `.env` 的 `CHAT_OWNERSHIP_NANE/CHAT_MODEL_NAME`，仅作未配置时的回退）：

```json
{
  "model_selection": {
    "chat_model": {
      "ownership_name": "Qwen Completions",
      "model_name": "Qwen3.7 Max",
      "parameter": {
        "chat_completions": {"temperature": 0.5, "max_tokens": 65536, "presence_penalty": 2.0, "reasoning_effort": "medium", "extra_body": {}, "top_p": 1.0}
      },
      "api_type": "chat_completions"
    },
    "compaction_model": {"ownership_name": null, "model_name": null, "parameter": {}, "api_type": null},
    "title_model": {"ownership_name": null, "model_name": null, "parameter": {}, "api_type": null}
  }
}
```

`POST /chat_config/models/select` 新增参数：

- `role`（可选，默认 `chat_model`）：`chat_model`（聊天）/ `compaction_model`（压缩）/ `title_model`（标题）。`compaction_model` 必须为 `chat-completions` 协议。
- `parameter`（可选）：该模型的默认生成参数，**按 api_type 分桶存储**——只写入被选模型 `api_type` 对应的桶（如 messages 模型写入 `parameter["messages"]`），其它桶保留；`parameter` 传 `null` 时仅切换模型、参数桶不变（同协议切换沿用参数）。支持字段（三协议并集）：`temperature / max_tokens / top_p / presence_penalty / reasoning_effort / extra_body`，以及协议专属 `thinking`（messages）、`max_output_tokens / instructions`（responses）。
- `api_type`（只读回显，前端不传）：后端根据 models.json 中该模型的 `apiType` 自动确定，可选值 `chat_completions` / `messages` / `responses`（连字符归一为下划线、小写；`apiType` 缺失时默认 `chat_completions`）。
- 向后兼容：只传 `{provider, model}` 等价于 `role=chat_model` 且参数不变。

**参数取用回退链**：当前 `api_type` 的桶 > `chat_completions` 桶 > 任意第一个非空桶 > 空（内置默认）。旧版扁平 `parameter`（键为字段名）加载时自动迁移为 `chat_completions` 桶。

**聊天参数优先级**：请求体显式传参 > `model_selection.chat_model.parameter`（按当前聊天模型的 api_type 取桶）> 默认值（`/chat_with_tool` 对未显式提供的生成参数自动按配置填充）。

**压缩模型参数**：`model_selection.compaction_model.parameter`（按压缩模型 api_type 取桶）> 压缩内置默认值（temperature 0.2 / top_p 1.0 / presence_penalty 0.0 / reasoning_effort low / max_tokens 摘要长度预算）。未配置 `compaction_model` 时跟随当前聊天模型（参数仍用压缩配置或内置默认）。

**压缩模型优先级**：`model_selection.compaction_model`（未配置时跟随当前聊天模型）。压缩模型完全由 models.json 的 `model_selection` 管理，**不再从 `.env` 读取或写入**（`HISTORY_COMPACT_MODEL_PROVIDER/NAME` 已移除）。

`title_model` 当前仅保存配置（标题生成暂未启用），参数与 `api_type` 一并持久化，供后续消费。

### 工具选择持久化（mcp_servers.json inputs + 会话 _meta.tool_selection）

前端工具弹窗点击"确定"后调用 `POST /chat_config/tool_selection`：**新对话**（尚未产生会话 ID）中保存到全局默认，**已有会话**中保存到该会话独立字段；打开/切换会话与新建对话时调用 `GET /chat_config/tool_selection`（携带当前会话 ID）恢复该会话的生效勾选（替换语义）。全局选择保存在 `setting/mcp_servers.json` 顶层 `inputs` 键（与模型选择的 `model_selection` 同类设计，读写即时生效）：

```json
{
  "inputs": {
    "__builtin__": ["ask_user"],
    "PipeIpcMcp": ["setup_pipe", "run_pipe_command"],
    "SysServer": []
  }
}
```

- 键为 `servers` 中配置的服务名（或内置工具伪服务 `__builtin__`），值为该服务下选中的工具名数组；全局 POST 为全量替换语义，未提及的已配置服务保存为 `[]`，未提及的 `__builtin__` 不写入（= 未勾选内置工具），其余未知服务名报 400。
- 后端同时维护内存快照（`_MCP_TOOL_INPUTS_MEMORY`）并写回磁盘；GET 每次以磁盘为准并同步内存，手工编辑文件后刷新页面即可生效（配置热重载线程也会自动同步，见上文）。
- 会话级覆盖保存在会话历史 JSONL 首行 `_meta.tool_selection`，结构与 `inputs` 一致；空选择/清除后恢复跟随全局默认，清空会话历史时保留。

### 上下文压缩策略

`POST /chat_config/history_compaction` 使用完整替换语义，请发送以下全部字段：

```json
{
  "keep_rounds": 20,
  "trigger_ratio": 0.8,
  "summary_budget_ratio": 0.2,
  "oversized_reject_factor": 1.5,
  "max_oversized_rejections": 3
}
```

- `keep_rounds`：兼容配置；摘要-only 模式下已完成历史不再按该字段回传原始轮次。
- `trigger_ratio`：历史压缩触发比例；未使用强制摘要模式的调用按该比例判断，范围为 $0 < r \le 0.95$。
- `trigger_ratio`：单轮和跨轮共用的上下文压缩触发比例，范围为 $0 < r \le 0.95$。阈值 = `min(聊天模型窗口, 压缩模型窗口) × 该比例`；单轮触发时会先压缩此前可压缩的多轮历史，再压缩当前轮工具轨迹。
- `summary_budget_ratio`（对应 env `HISTORY_COMPACT_SUMMARY_BUDGET_RATIO`，默认 0.2，上限 0.5）：累计摘要预算比例，**以聊天窗口为基数**。摘要总预算 = `聊天窗口 × 该比例`（100k × 20% = 20k）；新增摘要单次输出仍受压缩模型上限约束，最终归并为一个累计摘要块。
- `oversized_reject_factor`（可选，对应 env `HISTORY_COMPACT_REJECT_OVERSIZED_FACTOR`，默认 1.5）：单次工具结果的**拒绝阈值** = `min(聊天模型窗口, 压缩模型窗口) × 该系数`。当单个工具返回结果的估算 token 超过阈值时，该结果**不进入模型上下文**，后端改为给模型写入一条"输出过长，请重新考虑工具使用"的反馈（并提示缩小范围/加过滤参数），让模型重新规划；原始结果仅保留头尾节选到 JSONL（`result_preview` 字段），不落完整结果。填 `0` 关闭该保护。
- `max_oversized_rejections`（可选，对应 env `HISTORY_COMPACT_MAX_OVERSIZED_REJECTIONS`，默认 3）：**连续**超长拒绝次数达到该值时终止当前任务（写入一条 assistant 说明消息并正常收尾）。

> 说明：压缩模型**不再**由本接口配置。压缩模型选择统一由 `POST /chat_config/models/select`（`role=compaction_model`）管理；本接口请求体若带多余 `compaction_model` 字段会被忽略。压缩模型当前状态见 `GET /chat_config/models?role=compaction_model` 的 `role_info.compaction_status`（`available_compaction_models` 列出可选项，`effective` 为最终生效模型）。

压缩模型仅被要求输出普通文本备忘，不要求 JSON mode。工具原始输出仍完整写入 JSONL 和 SSE；压缩只影响后续模型调用的临时工作上下文。进入摘要模式后，模型上下文固定为累计摘要 + 全历史最近 10k tokens 用户问题 + 当前任务。
单轮压缩摘要会随对应 `chat_round` 保存到 `events` 中的 round `context_compaction` done 事件，事件同时携带 `summary_text`、`compress_usage`、`before_tokens`、`after_tokens` 和 `compress_index`。`index` 按该轮 `role=tool` 事件计数，表示前 N 个工具结果已经被摘要覆盖，后续历史格式化会跳过这部分原始工具轨迹，避免摘要与原文重复。活动任务期间最新状态暂存于 `_meta._active_round_compaction`，轮次结束时转入 `chat_round.events`。
全部历史轮次的原始用户问题保留在 `context_summary.recent_questions`（保留**最近**的 ≤10000 token，超出从最旧截断），渲染为累计摘要之后的独立 system 消息"【用户最近问题】"；原始轮次已从上下文移除，模型据此仍能感知用户近期关注点。

### 压缩过程实时事件（SSE 推送 + JSONL 落盘）

跨轮压缩的开始与完成事件以 `event="context_compaction"` 独立事件行写入 JSONL；单轮压缩事件嵌入所属 `chat_round.events`；两者均随 `/chat_with_tool` SSE 实时推送同一份 payload。要点：

- **推送与落盘字段完全一致**（`timestamp` 由落盘方补充），前端加载历史与实时显示共用同一解析逻辑；
- **先推 SSE 后落盘**：实时显示优先，落盘失败不阻塞推送；
- 事件行不参与 `usage`/`user_questions`/`chat_round` 统计；
- 触发门槛：单轮压缩需"全量上下文超 `min(聊天窗口, 压缩模型窗口) × trigger_ratio` **且** 本轮轨迹 > 阈值一半"（防空转，避免历史主导时空转消耗压缩调用）；触发后会先压缩此前可压缩的多轮历史，再处理当前轮轨迹。
- **任务内预算管理**：长任务进行中，每批工具结果写入后若全量上下文超过统一压缩阈值，会**立即执行跨轮压缩并重建内存历史**——老轮次压成累计摘要（预算 = 阈值 × 0.4），随后内存消息重建为"累计摘要 system（含运行时提示） + 全历史最近问题 + 当前任务轮"；当前任务轮的轨迹仍由单轮压缩管理。
- 首调用预算检查：请求开始时若"历史+文件记忆+当前请求+工具定义"估算超过当前聊天模型窗口，文件记忆先降级为 3000 字符摘要并推送 `warning`（`CONTEXT_BUDGET_FILE_DOWNGRADED`）；仍超窗（典型场景：切换到更小窗口的模型）进入**历史降级链**：
  1. **强制跨轮压缩**——预算按实际可用空间计算（窗口−系统−文件−当前请求−工具定义−安全余量），把全部未覆盖历史纳入累计摘要；
  2. **问题索引收紧**——在极小模型窗口下按剩余空间收紧最近问题索引，但不恢复原始历史轮次；
  4. 全部降级后仍超窗才报错终止。
 摘要始终以一个累计摘要块回传，摘要预算由 `summary_budget_ratio` 统一控制。

## 5. 工具 ToolsManage

| 接口 | 方法 | 参数 | 返回 |
|---|---|---|---|
| /tools/list | GET | refresh(可选, 默认 false) | `{tools[], total, servers[], failed_servers[], discovery, server_metrics[], mode}`；`tools[]` 每项附加 `server_id`（所属 MCP 服务器键名），便于前端按归属管理；探测按 `mcp_servers.json` 并发进行，单服务超时/失败不阻塞（记录于 failed_servers） |

### 工具列表缓存与预热

工具发现需要逐服务拉起 MCP 子进程完成完整握手（Python 服务的子进程冷启动约 1s，磁盘缓存冷/杀软扫描时可达数十秒），因此探测结果**默认带 TTL 缓存**，不再逐请求重探：

- **TTL 缓存**：最近一次探测结果整体缓存；TTL 内 `GET /tools/list` 与发送消息路径（生成任务开始时的工具加载）直接复用缓存（响应 `mode="cache"`），过期才重新探测（`mode="runtime"`）。TTL 由 `.env` 的 `MCP_TOOLS_CACHE_TTL_SECONDS` 控制（默认 60 秒，`<=0` 禁用缓存）；
- **强制刷新**：`refresh=1` 绕过缓存立即重探，对应前端「配置工具」弹窗的「刷新」按钮；
- **启动预热**：服务启动时后台线程先探测一次（`tool-registry-prewarm` 线程，失败不阻塞启动），重启后的首个页面加载/首条消息直接命中缓存；
- **失效同步**：`mcp_servers.json` 的 `servers` 键变更时，配置热重载线程会强制重探并刷新缓存（仅改 `inputs` 不触发），缓存不会滞后于配置变更。

## 6. 文件 FileUpload

### POST /file/upload_session_files
上传文件并解析文本（≤10 个，单个 ≤10MB），结果写入文件记忆。
- Form: `files`(必填, 多文件)；Query: `session_id`
- 返回: `{total, success, failed, results[], upload_id}`；`results[]` 每项 `{filename, status(success/failed), message?, type?, content_length?}`
- 上传文件保存为 `history_files/upload/<session_id>/<文件名>.json`（目录按前端传入的 `session_id` 命名，仅存解析出的文本内容）；
  同时把**原始文件字节**另存到 `history_files/upload/<session_id>/files/<名>_<随机>.<ext>`（供 `GET /file/get_session_document` 点击预览/下载），
  `results[]` 与 file_memory 记录均携带 `stored_name`（保存失败时为 null，不影响解析结果）；
  上传成功后会把该目录名写入对应聊天会话 jsonl 的 `_meta.upload_id`（会话文件不存在时自动创建），
  供 `/chat_history/delete_file` 连带清理；`upload_id` 为实际记录的目录名（记录失败时为 null，不影响上传结果）。

| 接口 | 方法 | 参数 | 返回 |
|---|---|---|---|
| /file/get_session_file_memory | GET | number(1-10,默认10), session_id | `{total, files[]}`，`files[]` = `{timestamp, filename, type, content, size}` |
| /file/get_session_file_text | GET | number(默认10), max_total_chars(默认3000), session_id | `{summary, length}`（纯文本摘要，供 LLM 上下文） |
| /file/delete_session_file_memory | DELETE | filename(必填), session_id | `{message, deleted_count}` |
| /file/clear_session_file_memorys | DELETE | session_id | `{message}` |

### POST /file/upload_session_media
上传聊天多媒体附件（图片/音频/视频），**原始字节**保存到 `history_files/upload/<session>/media/`。
- Form: `files`(必填, 多文件)；Query: `session_id`
- 限制：≤10 个/次；单文件上限按类别——图片/音频 20MB、**视频 500MB（流式落盘，不整体读入内存）**；扩展名白名单——图片 png/jpg/jpeg/gif/webp/bmp/ico/tif/tiff（ico/tif/tiff 视觉模型不原生接受，上传时由 Pillow 自动转为 PNG 再落盘，需安装 pillow）、音频 wav/mp3/m4a/ogg/flac、视频 mp4/webm/mov/mkv
- 返回: `{total, success, failed, results[], upload_id}`；`results[]` 每项 `{filename, status, stored_name, media_ref, kind(image/audio/video), mime, size}`
- 聊天消息引用方式：`content` 部件列表中 `{"type":"image_url","image_url":{"url":"media://<stored_name>"}}`（音频为 `{"type":"input_audio","input_audio":{"data":"media://<stored_name>","format":"wav"}}`，视频 `video_url`）。
  后端发送上游前把 `media://` 引用解析为 OpenAI 兼容格式：URL 类部件 → `data:<mime>;base64,<b64>`，`input_audio.data` → 纯 base64；`https://` 与已内联 `data:`/base64 原样透传（url 与 base64 双格式兼容）。
- 历史落盘与回放只保留 `media://` 引用（JSONL 不膨胀），历史轮次不重复注入媒体数据；文本口径提取 text 部件，媒体部件以 `[图片]/[音频]/[视频]` 占位。

### GET /file/get_session_media
读取会话内已上传的媒体文件原始字节（消息气泡缩略图、音视频播放等用途）。
- Query: `name`(必填, 即 stored_name，禁止路径分隔符), `session_id`
- **支持 HTTP Range 请求**（`bytes=start-end` / `bytes=-N`，命中时返回 206 Partial Content，带 `Accept-Ranges`/`Content-Range` 头）：音频/视频进度条可**即时拖动跳转**，无需等待整体缓冲完成
- 返回: 文件字节流（Content-Type 按扩展名推断；带 Range 时为 206 切片）；不存在 404

### GET /file/get_session_document
读取会话内已上传文档的**原始文件字节**（前端点击预览 PDF/文本/下载原文件）。
原始字节在上传解析时另存于 `history_files/upload/<session>/files/`（`upload_session_files` 返回的 `stored_name`，并写入 file_memory 记录）；旧版本上传的文档无原始字节，无法预览。
- Query: `name`(必填, 即 stored_name，禁止路径分隔符), `session_id`
- 支持 HTTP Range 请求（206），大 PDF 按需加载
- 返回: 文件字节流（Content-Type 按扩展名推断）；不存在 404

## 7. Skills 提示词库 Prompts

管理 `prompt/md_files/` 目录下用户自管理的 Markdown 提示词文件（可复用的聊天提示词）。
文件也可在文件系统中直接增删改，与接口操作等效（读盘即最新内容）。
文件名校验：仅允许中英文、数字、下划线、连字符、空格和点，自动补 `.md` 后缀，
拒绝路径分隔符与 Windows 保留设备名（CON/PRN/AUX/NUL/COM1-9/LPT1-9）；
`prompt/md_files/` 为空时首次访问自动播种 `示例提示词.md`。

### GET /prompts/list
- 返回: `{"prompts": [{"name", "size_bytes", "updated_at"}, ...], "total": 数量}`，按更新时间倒序

### GET /prompts/read?name=
- Query: `name`（必填，可省略 `.md` 后缀）
- 返回: `{"name", "content"(Markdown 原文), "size_bytes", "updated_at"}`；不存在 404

### POST /prompts/create
- Body: `{"name", "content"(可选, 默认空)}`
- 行为: 新建文件；同名已存在返回 409；名称非法返回 400

### POST /prompts/save
- Body: `{"name", "content"}`
- 行为: 保存内容（存在则覆盖，不存在则创建）；内容上限 512KB

### POST /prompts/rename
- Body: `{"old_name", "new_name"}`
- 行为: 重命名；原文件不存在 404、目标同名 409

### POST /prompts/delete
- Body: `{"name"}`
- 行为: 删除文件；返回 `{"state": "succeed", "name"}`；不存在 404

## 8. 根路由
- `GET /` → `{"message": "欢迎使用大模型智能体工具接口"}`
