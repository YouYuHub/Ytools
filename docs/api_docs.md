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
- `tool_start`：工具开始执行事件，`{"tool_start": {"function_name", "arguments", "tool_call_id"}}`。模型参数生成完毕、即将调用时推送（内置与 MCP 工具统一覆盖；被拦截的未授权工具不推送）。前端据此把对应工具块置为"执行中"状态，填补"参数生成完毕→结果返回"之间的静默期。`tool_call_id`（additive，旧前端忽略）为该次调用的父级 tool_call id；对 `sub_agent` 派发，前端据此在子任务事件到达前预建子任务块
- `tool_return`：工具执行结果 `{function_name, arguments, result}`；`sub_agent` 结果额外携带聚合字段 `sub_agent: {agent_id, status, rounds, usage_total}`（前端不渲染为普通工具气泡，而是在子任务块尾部显示"最终回复已返回父智能体"引用条）
- `round_started`：任务启动后立即推送本轮最终轮次号 `{"round_started": {"round": N}}`（**仅普通发送**；编辑重发/回答插入分别改推 `target_round`/`insert_round` 帧，不重复推送；覆盖式回答截断可能改变轮次，事件统一推迟到截断后发送；失败路径不推送——前端走失败重载兜底；仅实时推送不落盘）。前端据此给本轮提问气泡就地补挂编辑/复制/删除操作行：上一轮任务正常完成后**无需重载会话**即可编辑/删除"最后一轮"（旧实现收尾不重载导致本轮没有操作入口，需切走会话再切回）
- `round_started`：任务启动后立即推送本轮最终轮次号 `{"round_started": {"round": N}}`（**仅普通发送**；编辑重发/回答插入分别改推 `target_round`/`insert_round` 帧，不重复推送）。前端据此给本轮提问气泡就地补挂编辑/复制/删除操作行——上一轮正常完成后**无需重载会话**即可继续编辑/删除"最后一轮"（旧实现收尾不重载导致本轮没有操作入口）。覆盖式回答截断可能使轮次偏移，事件在截断后统一推送；失败路径不推送（前端走失败重载兜底）；仅实时推送不落盘
- `usage`：token 统计；`finish_reason`：结束原因（stop/length/tool_calls）
- `todo`：内置 `todo_write` 工具执行成功后的任务计划推送，`{"event":"todo","todos":[{id,content,status(pending|in_progress|done)}]}`（`id` 为步骤稳定标识；模型未携带时后端自动分配/继承自上一版计划，同一时间最多一个 `in_progress`，订阅会话元数据 `GET /chat_history/meta` 可读取 `todo` 同结构数据——**真源为侧车文件 `<session>_chat.jsonl.todo`**，`get_session_meta` 会以侧车值合并返回，旧会话无侧车时回退 `_meta.todo`）；**工具结果三态反馈**：message 按首次创建 / 部分更新 / 全部完成给出不同提示（全部完成时附「请汇总执行结果直接答复用户，无需再调用本工具」），并携带机器可读 `plan_complete` 布尔标记，模型无需解析文案即可判断计划收官；工具描述同时引导「合并状态变更」减少调用次数（完成某步与启动下一步在同一次提交中完成）；启用方式：「配置工具」模态框首位的「内置工具」分组勾选 `todo_write`（伪服务 `__builtin__`，与 MCP 工具共用工具选择持久化，见"工具选择持久化"；后端按名称识别、本地执行并落盘侧车 `<session>_chat.jsonl.todo`，当前计划同时注入系统提示词供模型跨轮感知——终态注入收官提醒防止重复调用）
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

#### 子智能体事件格式（`event="sub_agent"`，SSE 实时推送 = JSONL 落盘字段一致）

内置工具 `sub_agent`（「内置工具」分组勾选，V1 不支持嵌套派发）让父智能体一次并发派发多个独立上下文的子任务：子任务有专属系统提示词、复用父级本轮工具（剔除 `sub_agent`/`check_tool_exists`，`ask_user` 替换为占位定义），全轨迹按 `agent_id` 聚合写入当前 `chat_round.events`（无 `role` 字段，不参与父级压缩游标计数），父模型只能看到子任务最终回复（`role=tool` 文本）。完整设计见 `docs/sub_agent_v1.md`。事件阶段：

| 阶段 | payload 关键字段 |
|---|---|
| `start` | `{agent_id, parent_agent_id:"main", parent_tool_call_id, agent_index, task, todo?, tools[], rounds_limit, timeout_seconds}` |
| `delta` | `{agent_id, ..., reasoning_delta / content_delta}`（仅 SSE 实时推流，**不落盘**） |
| `model_call` | `{agent_id, seq, reasoning_content, content, tool_calls[]}`（子任务本轮完整轨迹落盘） |
| `tool_start` | `{agent_id, seq, tool_call_id, tool_name, arguments}` |
| `tool_result` | `{agent_id, seq, tool_call_id, tool_name, arguments, result, oversized?}` |
| `todo` | `{agent_id, todos[]}`（子任务私有计划，只存事件块内） |
| `done` | `{agent_id, status, final_reply, rounds, usage_total, error, ended_at}`；status ∈ `done|error|stopped|interrupted|timeout|max_rounds` |

- 身份模型：`agent_id`（`agent_<8hex>`，子智能体实例 ID，轮内唯一）与 `parent_tool_call_id`（父级 tool_call id，用于归属父轮与关联父级工具气泡）分离；同一父级 tool_call 一次重派不会复用旧 `agent_id`。
- 并发与限额：父循环用 `asyncio.gather` 并发执行全部子任务，`SUB_AGENT_MAX_CONCURRENT`（默认 3）信号量限流；每个子任务受 `SUB_AGENT_MAX_ROUNDS`（40）、`SUB_AGENT_TIMEOUT_SECONDS`（900，超时被取消并兜底补写 `done(timeout)`）、`SUB_AGENT_REPLY_MAX_CHARS`（30000，父侧最终回复截断，完整轨迹始终在 JSONL）保护。
- 层级取消：父级停止（`/stop_chat`、新消息打断、worker 退出、上游错误）会取消全部子任务并补写 `done(stopped)`；子任务自身的超时/轮次上限/流错误只终止自己，不影响父任务与兄弟任务。子任务事件仅在事件循环的同步 `_write_guard()` 块内写历史（跨进程文件锁不可重入，禁止工作线程直写）。
- 可见性：父级上下文只包含 `assistant(tool_calls=sub_agent)` 与 `role=tool(最终回复)`；子任务思考/正文/工具轨迹/todo 永不进入父模型上下文（历史重建与压缩文本渲染显式排除无 `role` 的 sub_agent 条目），父轮 `usage_total` 另行合并子任务 usage。
- 模型：子智能体使用独立角色 `sub_agent_model`（见"模型选择"，未配置时继承聊天模型）；子任务请求副本同样经过思考回传整形与参数填充。
- 前端渲染：按 `agent_id` 聚合为独立子任务块（标题条 #序号·任务摘要·状态徽标 + 可折叠的思考/正文/工具轨迹/计划区），done 后折叠为摘要态；历史回放按 JSONL 中事件块原样重建；旧前端遇未知事件走默认分支忽略、降级为普通 `tool_return` 文本。

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
>
> **会话配置快照**：会话**首次正式开始任务**（第一次 `/chat_with_tool` 生成，幂等）时，后端把当时的全局默认配置固化为会话独立覆盖——
> `_meta.work_dir`（← .env `DEFAULT_CHAT_WORK_DIR`）、`_meta.tool_selection`（← mcp_servers.json `inputs`）、
> `_meta.model_selection`（← models.json 顶层 `model_selection` 全角色），并写入 `_meta.config_snapshot_at` 标记。
> 此后该会话**不再跟随全局设置变化**（改动全局只影响未开始任务的新会话）；快照不会覆盖用户已手动设置的会话覆盖；
> 会话内手动改某项仍即时生效，清除单键覆盖的「恢复跟随全局」语义不变。

| 接口 | 方法 | 参数 | 返回 |
|---|---|---|---|
| /chat_history/sessions | GET | - | 会话文件名列表 `["default_chat.jsonl", ...]` |
| /chat_history/file | GET | session_id | 下载会话 JSONL 文件（首行 `_meta` 元数据） |
| /chat_history/meta | GET | session_id | `{title, user_questions, usage, created_at, updated_at, record_count, completion_count, context_summary, upload_id?}`；**只读接口**：优先仅解析首行 `_meta`（正常文件与全量重算结果一致），不再把全量内容重写回盘；首行不可靠（缺失/损坏/标题为默认值/缺 updated_at 或 user_questions）时回退全量重算兜底，两种路径均不落盘 |
| /chat_history/title | PUT | title(必填), session_id | `{state, describe, title, meta}`；更新首行 `_meta` 的 `title` 字段，title 为空报 400 |
| /chat_history/delete_file | DELETE | session_id | `{state, describe}`；**连带删除**该会话上传文件目录 `history_files/session_files/`（目录名取 `_meta.upload_id`，旧记录回退按 session_id 推导）；任一侧删除失败返回 500 `{detail}` |
| /chat_history/delete_lines | DELETE | startline(必填,≥1), endline(必填,≥1,包含), session_id | `{state, describe, meta_after, usage_after}`；行号**不含** `_meta` 首行；startline>endline 报 400 |
| /chat_history/delete_rounds | POST | `{session_id, start_round(必填,1-based 轮次号), mode, delete_files, dry_run, keep_media_refs}` | 见下方「按轮次号删除（用户消息编辑重发）」；生成任务运行中报 409 |
| /chat_history/upload_chat_file | POST | 见下方说明 | 见下方说明 |
| /chat_history/export_zip | GET | session_ids(逗号分隔，≤100 个) | zip 二进制下载；`Content-Disposition` 带 UTF-8 文件名（单会话 `<id>_chat.zip` / 多会话 `ytools_sessions_<时间戳>.zip`），`X-Skipped-Sessions` 列出不存在的会话；详见下方「会话分享 / 导入」 |
| /chat_history/import_preview | POST | 见下方说明 | 见下方说明 |
| /chat_history/import_package | POST | 见下方说明 | 见下方说明 |

> **`_meta.upload_id`（可选字段）**：本会话上传文件所在目录名（`history_files/session_files/<upload_id>/`，字段名沿用 upload_id）。上传目录按**上传时前端传入的 `session_id`** 命名，可能与聊天会话文件名不一致，故记录于此，供 `/chat_history/delete_file` 连带清理。仅从新记录开始维护，已有旧会话无此字段（删除时按 session_id 推导兜底）。目录原名 `history_files/upload/`（upload 改名为 session_files）。

### POST /chat_history/delete_rounds（按轮次号删除，用户消息编辑重发）

**轮次号口径**：1-based 的第 N 个 `chat_round` 条目（与前端历史渲染 `data-round` 同口径）；游离压缩事件行不占轮次号。生成任务运行中拒绝（409）——pending 轮次在 worker 内存，此刻截断会交错写入。**前端编排**：任务运行中允许进入编辑态（仅提示"确认发送后会先停止生成再重发"）；用户确认后前端先调 `POST /stop_chat` 停止生成（任务按"手动停止"收尾落盘，停止接口同步等待任务完全退出），随后删除接口不再遇到 409。前端另有「删除该轮」hover 入口走 `single` 模式（确认框 → 删除 → 重载会话，运行中直接提示暂不删除）。

- **请求体**：
  - `start_round`(必填)：从第 N 轮开始删；
  - `mode`：`truncate`=删除该轮及其后所有轮次（编辑重发 GPT 语义）｜`single`=仅删除该轮整轮（用户消息与回复一并删除，后续轮次保留并前移）；
  - `delete_files`(默认 true)：连带清理该删除范围内引用、且保留内容不再引用的**用户上传附件**（媒体按引用差集精确判定；文档按上传时间窗近似判定）。**不动模型文件版本链与 diff**；
  - `dry_run`(默认 false)：预演——只返回明细不写盘不删文件；
  - `keep_media_refs`：清理时排除的媒体 stored_name（编辑重发时被编辑消息的复用附件）。
- **返回**：dry_run 时 `{state: "planned", planned_rounds: [{round, question, status}], planned_files: {media_files, doc_files}}`；正式执行 `{state: "succeed", removed_rounds, planned_*, files_cleanup: {removed, failed}, meta_after, usage_after}`。
- **一致性保障**：写盘前生成 `<历史文件>.bak` 侧车备份（覆盖式）；`context_summary` 失效（下次聊天前从剩余原始轮次重建）；meta（usage/user_questions/标题）按剩余条目重算。

**编辑重发两种模式的前端语义**：

| 模式 | 删除范围 | 重发方式 | 上下文语义 |
|---|---|---|---|
| `single` + `target_round=N` | 删第 N 轮整轮 | `/chat_with_tool` 带 `target_round: N` 原地重跑 | 上下文截到第 N-1 轮；新回复替换历史第 N 轮位置；后续轮次保留但其历史依据不含旧第 N 轮 |
| `truncate` + 普通发送 | 删第 N 轮及之后 | 普通发送（追加） | GPT 同款：后续轮次一并删除，新回复追加末尾 |

**`target_round` 原地重跑（ChatLLMRequest 新字段）**：轮次收尾替换历史第 N 轮条目而非追加（用户消息相同则保留原 `started_at`，不同则视为已编辑同样整轮替换）；`get_current_round_number` 覆盖为 N（file_history 版本链 round 标注仍为 N）；`get_context_messages` 截到第 N-1 轮；越界（历史被并发删除）自动降级为追加；任务异常中断时同样按替换式收尾（stopped/interrupted 占据原轮次位置）。旧累计摘要失效后本次任务按原始轮次+截断窗口构建，且任务内**跳过前置压缩**（防旧第 N 轮内容被压进摘要重新进入上下文）。

### POST /chat_history/upload_chat_file
接收前端上传的 jsonl 聊天历史文件，按现有格式过滤后保存到 `history_files` 目录。

- **Form**: `file`(必填, jsonl 文件)；**Query**: `session_id`(默认 "default"，可传文件名)、`overwrite`(默认 false)
- 过滤规则：
  - 首行若为 `{"_meta": {...}}` 则作为基础元数据（其中 `session_id` 字段会被移除）；
  - 其余行通过 `parse_round_entry` 验证，只保留能正确解析为 `chat_round` 的条目（含非空 `question`、至少一条 `user` 事件、合法 `status`），无法解析的行丢弃并计入 `skipped_lines`；
  - 写入后重新计算 `user_questions` / `usage` / `record_count` / `completion_count`。
- **同名冲突**：目标文件 `<session_id>_chat.jsonl` 已存在且 `overwrite=false`（默认）时，自动追加时间戳另存为 `<session_id>_<YYYYMMDD_HHMMSS>_chat.jsonl`，避免覆盖旧会话；`overwrite=true` 时强制覆盖。
- **返回**: `{state, session_id(实际写入), filename(实际保存文件名), total_lines, imported_rounds, skipped_lines, record_count, completion_count, title, overwrite, collision(bool, 是否同名另存), meta}`

### 会话分享 / 多会话导入（zip + jsonl，两阶段冲突确认）

**分享打包 `GET /chat_history/export_zip?session_ids=a,b,c`**

把一个或多个会话打包为 zip 下载，zip 内布局：

```
<session>_chat.jsonl              # 会话历史（与 history_files 根一致）
session_files/<upload_id>/...     # 该会话在 session_files/ 下的全部上传数据（解析记录/media/files/file_diffs）
manifest.json                     # {version, exported_at, sessions: [{session_id, filename, title, upload_id}]}
```

- 单会话下载名 `<session_id>_chat.zip`，多会话 `ytools_sessions_<时间戳>.zip`；
- 不存在的会话列入响应头 `X-Skipped-Sessions`（URL 编码逗号分隔），全部不存在时报 400。

**导入两阶段**（前端弹窗逐会话选择「覆盖 / 重命名另存 / 跳过」）：

1. `POST /chat_history/import_preview`（Form: `file`，支持 .zip / .jsonl）——**预检不落盘**：
   - 返回 `{type(zip|jsonl), filename, total, sessions[], conflicts[]}`；
   - `sessions[]` 每项 `{session_id, title, imported_rounds, skipped_lines, exists, media_count}`；`exists=true` 表示本地已有同名会话（即冲突，列入 `conflicts`）；
   - jsonl 按文件名解析会话 ID，zip 按 manifest + 包内 `*_chat.jsonl` 解析（含路径穿越防护）。
2. `POST /chat_history/import_package?conflict_strategy=ask&decisions={"<sid>":"overwrite|rename|skip"}`（Form: `file`）——**提交导入**：
   - 冲突决策优先级：逐会话 `decisions` > 全局 `conflict_strategy`（ask 且无逐项决策时该会话跳过，绝不静默改名/覆盖）；
   - 无冲突会话直接导入；zip 包同时落盘各会话的 `session_files/<upload_id>/` 数据；
   - 冲突「重命名」：会话 ID 追加时间戳另存，上传数据目录归属同步改名为新会话 ID，并回写首行 `_meta.upload_id`；「覆盖」：清空旧 jsonl 与旧上传目录后写入；
   - 返回 `{state, imported[], skipped[], failed[]}`，单会话失败不阻断其他会话。

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
| /chat_config/history_compaction | GET | `session_id`（可选） | `{keep_rounds, trigger_ratio, summary_budget_ratio, summary_total_budget, effective_threshold{value, chat_window, compaction_window, window, trigger_ratio, formula}, compaction_model, available_compaction_models[], defaults, env_names, memory_state}`；`effective_threshold` 的模型窗口按会话生效模型口径解析（与 token_stats 一致），不传 `session_id` 时为纯全局口径 |
| /chat_config/history_compaction | POST | Body 见下方完整策略；Query `session_id`（可选） | `{state, updated, config, memory_state}`；返回的 `config.effective_threshold` 同样支持会话口径 |
| /chat_config/models | GET | `role`（可选，默认 chat_model）、`session_id`（可选） | `{state, count, current, selection, models[], role, role_info}`；`models[]` 为扁平 `{provider_name, model_name, model_id, api_type, url, vision, tool_calling, max_input_tokens, max_output_tokens, api_key_present}`；`model_name` 为 models.json 中 models 字段的键名（写入 model_selection 的值），`model_id` 为请求 API 使用的模型 id；`selection` 为全局三模型选择；`role_info` 为指定角色的详情：`selection`（provider/model/api_type/parameter 分桶）、`effective_parameter`（按回退链解析的生效参数）、`available_models`/`available_count`（可选模型，compaction_model 只列 chat-completions）、compaction 额外带 `compaction_status`；携带 `session_id` 时 `role_info` 为该会话生效结果并附加 `is_overridden`，响应额外返回 `{session_id, session_selection, effective_selection, warning}`（`session_selection` 为会话 `_meta.model_selection` 覆盖值，未设置为 null；失效覆盖回退全局时 `warning` 给出提示） |
| /chat_config/models/select | POST | Body `{provider, model, role?, parameter?, session_id?, clear?}` | 不携带 `session_id`（全局默认）：`{state, message, current, effective_parameter, selection}`；组合不存在报 400（compaction_model 必须为 chat-completions 协议）。携带 `session_id`（会话级）：`{state, message, session_id, session_selection, effective_selection, warning, role, role_info}`；写入该会话 `_meta.model_selection.<role>`，不修改全局 models.json；`clear=true` 清除该角色会话覆盖恢复跟随全局（忽略 provider/model） |
| /chat_config/context_return | GET | - | `{reasoning_max_length, tool_result_max_length, defaults, semantics, env_names, memory_state}` |
| /chat_config/context_return | POST | Body `{reasoning_max_length, tool_result_max_length}`（整数，缺省时使用默认值） | `{state, updated, config, memory_state}`；写回 .env 并同步内存 env_vars，下次聊天立即生效 |
| /chat_config/mcp_tools | GET | - | `{call_timeout_seconds, defaults, semantics, env_names, memory_state}`；读取 MCP 工具单次执行超时 |
| /chat_config/mcp_tools | POST | Body `{call_timeout_seconds}`（非负数，0 表示不限制） | `{state, updated, config}`；写回 .env 并同步内存，下一次工具调用立即生效 |
| /chat_config/tool_concurrency | GET | - | `{mcp_tool_workers, sub_agent_max_concurrent, defaults, semantics, env_names, memory_state}`；MCP 工具并发线程数与子智能体并发上限 |
| /chat_config/tool_concurrency | POST | Body `{mcp_tool_workers, sub_agent_max_concurrent}`（均 ≥1，缺省用默认值） | `{state, updated, config, memory_state}`；写回 .env 并同步内存，下一次工具执行立即生效 |
| /chat_config/video_read_limit | GET | - | `{max_seconds, effective_seconds, limits{min,max}, defaults, semantics, env_names, memory_state}`；read_media 工具视频区间读取最大秒数（.env `VIDEO_MAX_READ_SECONDS`），`effective_seconds` 为读取端钳制后的现读生效值（5–3600，非法回退 60），随 .env 热重载同步 |
| /chat_config/video_read_limit | POST | Body `{max_seconds}`（整数，缺省用默认 60） | `{state, updated, config}`；写回 .env 并同步内存，下一次 read_media 调用即按新值读取（工具描述动态构建现读）；越界值保存原样、生效值钳制 5–3600 |
| /chat_config/tool_selection | GET | `session_id`（可选） | `{state, inputs, servers[], config_path, memory_state}`；每次以磁盘 mcp_servers.json 的 `inputs` 为准并同步内存（手工编辑文件后刷新页面即生效），未提及的已配置服务补 `[]`；`inputs` 可含内置工具伪服务键 `__builtin__`（值如 `["todo_write","ask_user"]`）；携带 `session_id` 时附加 `{session_id, session_selection, effective_selection, is_overridden, warning}`：`session_selection` 为会话 `_meta.tool_selection` 覆盖值（未设置为 null），`effective_selection` 为会话实际生效选择（会话覆盖 → 全局 `inputs`） |
| /chat_config/tool_selection | POST | Body `{inputs: {服务名: [工具名...]}, session_id?}` | 不携带 `session_id`（全局默认）：`{state, message, updated, inputs, memory_state}`；服务名必须已在 `servers` 中配置或为内置工具伪服务 `__builtin__`（其余未知服务报 400）；全量替换语义，未提及的已配置服务保存为 `[]`，未提及的 `__builtin__` 不写入（= 未勾选内置工具）；实时更新内存并写回 mcp_servers.j |
| /chat_config/retitle_setting | GET | `session_id`（必填） | `{state, session_id, enabled, exists, title_state: {title_generated, attempted}}`；读取会话「每条消息重新标题」开关（`_meta.retitle_each_message`，会话独立配置，未设置视为关闭）与标题生成状态概览 |
| /chat_config/retitle_setting | POST | Body `{session_id, enabled}` | `{state, session_id, enabled, message}`；写入会话「每条消息重新标题」开关：开启时每轮任务收尾都重新生成标题（开启瞬间清除 `_title_state.attempted`），关闭时仅首轮生成一次；会话不存在返回 404 |son（仅替换 `inputs` 键）。携带 `session_id`（会话级）：`{state, message, session_id, session_selection, effective_selection, is_overridden, updated_at}`；写入该会话 `_meta.tool_selection`，不修改全局 `inputs`；空 `inputs` 清除会话覆盖恢复跟随全局默认；会话级不做未知服务校验（失效服务名在生成时自动忽略并告警） |

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
- **内置工具**（`todo_write` / `ask_user` / `write_file` / `edit_file` / `read_file` / `search_files` / `read_media`，后续可扩展 subAgent 等）并入同一链路：前端在「配置工具」模态框首位的「内置工具」分组勾选，保存为伪服务键 `__builtin__`（如 `{"__builtin__": ["ask_user"]}`）；生成时后端把该键下的名称与 MCP 工具名一并作为 `requested_names`，按名称识别注入（`inject_builtin_tools`），会话级/全局默认语义与 MCP 工具完全一致。

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

- `reasoning_max_length`（对应 env `REASONING_RETURN_MAX_LENGTH`，默认 -1）：思考过程（`reasoning_content`）的回传裁剪长度。**回传规则**：每次请求最多只回传一条真实思考——最近一次 API 调用输出的那条（本轮没有输出时向前回溯最近一次真实思考），负数全量、正数保留末尾 N 字符；配置为 `0` 或回溯不到真实思考时回传 `"..."` 占位符（带 `tool_calls` 的最新 assistant 消息字段必须存在，GLM / DeepSeek 等严格上游缺失即 400）。**落盘与回传解耦**：运行时上下文与 JSONL 历史永远保存每轮思考的原始全文，历史 assistant（含旧工具轮）的思考在请求副本中直接剥离、不回传上游，也不落 `"..."` 假数据。
- `tool_result_max_length`（对应 env `HISTORY_TOOL_RESULT_RETURN_MAX_LENGTH`，默认 0）：后端历史轮次重建上下文时，单个工具结果回传前 N 字符；回传格式符合 Chat Completions 规范（`assistant.tool_calls` → `tool.tool_call_id`）。
- 两个值共用语义：`0` = 不回传，负数 = 全部回传，正数 = 按 N 截断（思考过程保留末尾，工具结果保留开头）。（思考过程因上游强校验，`0` 实际回传占位符，见上）
- 该配置同时作用于父循环与子智能体（`sub_agent`）：子任务请求副本经过同一份思考回传整形（共享 `copy_for_request`，可显式传 limit），子任务工具超长结果同样走 `tool_result` 超长反馈路径。

### 子智能体（sub_agent）配置

配置项写入项目 `.env`（默认值由 `config.py` 的 `DEFAULT_SUB_AGENT_*` 提供），每次派发时现读：

```env
SUB_AGENT_ENABLED=true          # 总开关；关闭时本轮工具不注入 sub_agent 定义
SUB_AGENT_MAX_ROUNDS=40         # 子任务工具调用轮次上限，达到后收尾并返回进展说明
SUB_AGENT_MAX_CONCURRENT=3      # 一批子任务的并发信号量
SUB_AGENT_TIMEOUT_SECONDS=900   # 子任务整体超时（0=不限制），超时取消并兜底补写 done(timeout)
SUB_AGENT_REPLY_MAX_CHARS=30000 # 返回父级的最终回复最大字符数（完整轨迹始终在 JSONL 事件块）
```

启用方式：「配置工具」模态框「内置工具」分组勾选 `sub_agent`（伪服务 `__builtin__`）。父智能体调用参数：`task`（必填，自包含任务描述：目标/路径/已有结论/验收标准）+ `todo`（可选，预置计划数组，最多 20 项，随 start 事件显示在子任务块内）。子任务自身的 todo 独立存储（只显示在子任务块内，不写入父会话 `_meta.todo`）。

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

后端仍保留**工具调用流式阶段超时**（env `TOOL_CALL_STREAM_TIMEOUT_SECONDS`，默认 300；该项已从配置接口移除，只能手工改 `.env`）控制**模型 SSE 输出 `tool_calls` 阶段**的无响应超时：部分服务商 API 在该阶段会无限卡住（HTTP 连接不断、永无后续事件），普通 HTTP 读超时既兜不住也不该终止任务。首个 `tool_calls` 增量到达后开始按"事件间隔"计时（期间任意后续事件续期，`finish_reason` 到达后停止计时）；超时后**不终止任务**：本次工具调用按失败处理（assistant 工具调用消息 + 失败工具结果成对落盘，SSE 推送 `tool_return`（`timeout: true`）与 `TOOL_CALL_STREAM_TIMEOUT` warning 帧），模型基于失败结果继续运行（重试或直接回答）。调用结构不可用（工具名未注册等）时退化为内部提示消息，让模型重新发起调用。

超时值还会实时写入**系统提示词**（`factory/system_prompt.py`，每次构造提示词时从 `load_var` 现读）：正数时告知模型「单次执行超时 N 秒，长耗时操作需拆分/缩小范围」；`0` 时告知「不限制超时，需主动拆分并阶段性反馈」——用户在聊天设置中修改超时后，下一轮对话模型即可感知新配置。

工具执行已放入工作线程，因此工具阻塞期间事件循环仍可处理 `GET /chat_context/token_stats` 和 `POST /stop_chat`。手动停止会同时设置会话停止标记、取消后台生成任务并等待其完成收尾；正在执行的同步工具线程无法被 Python 强制杀死，会由上述超时配置最终释放，推荐将超时设置为合理的正数。

### 文件变更 diff（内置 write_file / edit_file）

内置 `write_file` / `edit_file` 执行成功后，结果 dict 会附加 `_file_diff` 顶级键（`factory/agent_runtime/builtin_tools.py` 生成，标准 unified diff 文本）：

```json
{
  "diff": "--- a/app.py\n+++ b/app.py\n@@ -1,1 +1,2 @@\n-value = 1\n+value = 2\n+print(value)",
  "lines_added": 2,
  "lines_removed": 1,
  "diff_truncated": false,
  "diff_skipped": ""
}
```

- **不进入模型上下文**：`_format_tool_result` / `_format_result_text` 组装模型可见文本时统一剥离该键，模型只在 message 里看到「diff +N -M 行」统计摘要，token 零负担；
- **SSE 事件**：`tool_return` 消息附带 `file_diff` 字段（与 result 并列），前端工具块输出区渲染彩色 diff 视图；
- **JSONL 落盘**：tool 历史记录附带 `file_diff` 字段，刷新/重开会话历史回放同样渲染；
- **边界降级**：新旧文本任一超过 256KB 时跳过逐行 diff（`diff_skipped: "file_too_large"`）；diff 文本超过 20000 字符时截断（`diff_truncated: true`）；内容无变化时 `diff_skipped: "unchanged"`；
- **content_hash**：`write_file`/`edit_file`/`read_file` 结果均含 `content_hash`（解码后全文 sha256 前 16 位），为后续「文件版本校验」（edit 时校验 expected_hash，检测读后文件被外部修改）预留；
- **子任务块**：sub_agent 内部调用的文件工具同样在 tool_result 子事件中携带 `file_diff`，渲染行为一致。

MCP sys_tools_server 版同名工具（走外部 MCP 协议）返回纯文本结果，不带 `file_diff`，前端自动回退普通文本渲染，无需处理。

### 文件历史版本链（V2，`/file_diff/*`）

内置文件工具每次成功写入后，除 V1 的 `file_diff` 外还会把「写前/写后全文」经 `chat_factory` / `sub_agent` 消费点（`_file_history` 顶级键，同样不进模型上下文）写入版本链：
`history_files/session_files/<session>/file_diffs/<key8>/meta.json + g<gen>/v*.txt`（全文快照链，key = sha1(绝对路径 normcase) 前 8 位；磁盘为唯一真源，会话重开仍可回放/回退）。
设计详见 `docs/file_diff.md` §9。核心语义：

- **Total Diff** = diff(当前代基线, 当前内容)：多次修改合并展示单文件总变更；
- **Change Diff** = diff(同代上一版本, 本版本)：单次修改；
- **round** = 用户会话轮次号：「回退到第 N 轮发起时」= 链上 round < N 的最新快照写回磁盘；
- **keep**：保留封版（历史代锁定不可撤回），以当前内容开新代基线；
- **keep_all / revert_all**：面板级批量操作——全部保留=逐文件封版（Total 归零、当前代锁定），全部撤回=逐文件回退到各自当前代基线并写回磁盘（历史可在编辑器找回）；单文件失败隔离记入 skipped；
- **hide_clean**：`/list` 默认隐藏已保留/已全部撤回（Total 无行数变化）的文件；留档清理用 DELETE /delete。

| 方法 | 路径 | 参数 / body | 返回 | 错误 |
|---|---|---|---|---|
| GET | `/file_diff/list` | session_id | `{files:[{key,path,display_path,kept,versions,added,removed,updated_at,last_role}], stats:{total,added,removed}}` | — |
| GET | `/file_diff/versions` | session_id, key | `{key, versions:[{v,gen,role,tool,round,at,hash,added,removed}]}` | 404 |
| GET | `/file_diff/content` | session_id, key, v?(缺省最新) | `{...,v,gen,hash,role,tool,round,at,encoding,eol,kept,content}` | 404 |
| GET | `/file_diff/total_diff` | session_id, key | `{key,display_path,baseline_v,current_v,kept,diff,lines_added,lines_removed,diff_truncated,diff_skipped}` | 404 |
| GET | `/file_diff/change_diff` | session_id, key, v | `{...,v,prev_v,gen,role,tool,round,at,diff,lines_added,lines_removed,diff_truncated,diff_skipped}` | 404 |
| POST | `/file_diff/hunk_undo` | `{session_id,key,hunk_index,until_hunk?}` | `{ok,hunk:{old_start,old_count,new_start,new_count},undone_count,version,total}`（撤回差异块并写回磁盘，不可恢复；until_hunk=true 撤回此处及之后） | 400/404 |
| POST | `/file_diff/hunk_keep` | `{session_id,key,hunk_index,until_hunk?}` | `{ok,version,new_gen,kept_hunk,kept_count,total}`（保留该块、其余还原为基线，固化为新代基线并写回磁盘；until_hunk=true 保留此处及之后） | 400/404 |
| POST | `/file_diff/sync` | `{session_id,key}` | `{synced,version?,total?,message?}`（从磁盘刷新：外部修改并入版本链并重算 diff） | 400/404 |
| GET | `/file_diff/full_view` | session_id, key, max_rows?(默认 20000) | `{key,path,display_path,baseline_v,current_v,current_hash,current_ends_with_nl,kept,rows:[{t,o,n,s,h}],hunks:[{index,old_start,...}],truncated,max_rows,diff,...}`（V2.2 全文视图：ctx/del/add 行序列 + hunk 坐标，超限截断前端回退紧凑模式） | 404 |
| POST | `/file_diff/rollback` | `{session_id,key, to_version?/to_round?, target:"baseline"}` | `{ok,version,restored_to:{v,hash},total}`（写回磁盘并追加 rollback 版本） | 400/404/409 |
| POST | `/file_diff/save` | `{session_id,key,content,expected_hash}` | `{ok,version,total}`（编辑器保存，乐观锁；写回磁盘 + 追加 user_edit 版本） | 400/404/**409**(hash 冲突) |
| POST | `/file_diff/keep` | `{session_id,key}` | `{ok,kept:true,new_gen,baseline_v,total}`（保留封版：历史代拒绝再撤回） | 400/404 |
| DELETE | `/file_diff/delete` | session_id, key | `{deleted,key}`（删除该文件版本链目录） | 400 |
| POST | `/file_diff/cleanup` | `{session_id,clean_only?=true}` | `{removed_count,removed:[{key,path}]}`（批量清理留档：只清已全部保留/撤回的文件链） | — |
| POST | `/file_diff/keep_all` | `{session_id, cleanup?=true}` | `{kept_count,kept:[{key,path,display_path,new_gen}],skipped:[{...,reason}],cleaned_count?}`（V2.4 全部保留封版：逐文件以当前内容开新代基线；默认顺带清理留档目录；单文件失败记入 skipped 不影响其余） | — |
| POST | `/file_diff/revert_all` | `{session_id}` | `{reverted_count,reverted:[{...,restored_to,disk_removed}],skipped:[...]}`（V2.4 全部撤回：逐文件回退到各自当前代基线并写回磁盘；新建文件回退到空基线时删除磁盘文件并标记 disk_removed=true） | — |

409 的两种来源：目标版本所在代已 `locked`（keep 后）；`save` 的 expected_hash 与链上最新版本不符（文件读后被外部修改）。
前端：顶栏「文件变更」按钮（徽标=文件数）→ 统计面板（只显示文件名，hover 见完整路径；footer 带「全部保留 / 全部撤回 / 清理留档」入口，前两者二次确认防误触）→ 点击文件**新窗口打开独立编辑器页 `H5/editor.html?session_id&key`**（V2.3：全文视图 ctx/add 行可编辑 overlay + Prism 行内高亮，del 红块只读；每 hunk 保留/撤回 +「此处及之后」区间操作；保存 Ctrl+S / 回退基线或任意轮次 / 保留 / 从磁盘刷新；超 2 万行回退紧凑 diff 模式）。

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
    "title_model": {"ownership_name": null, "model_name": null, "parameter": {}, "api_type": null},
    "sub_agent_model": {"ownership_name": null, "model_name": null, "parameter": {}, "api_type": null}
  }
}
```

`POST /chat_config/models/select` 新增参数：

- `role`（可选，默认 `chat_model`）：`chat_model`（聊天）/ `compaction_model`（压缩）/ `title_model`（标题）/ `sub_agent_model`（子智能体）。`compaction_model` / `sub_agent_model` 必须为 `chat-completions` 协议。
- `parameter`（可选）：该模型的默认生成参数，**按 api_type 分桶存储**——只写入被选模型 `api_type` 对应的桶（如 messages 模型写入 `parameter["messages"]`），其它桶保留；`parameter` 传 `null` 时仅切换模型、参数桶不变（同协议切换沿用参数）。支持字段（三协议并集）：`temperature / max_tokens / top_p / presence_penalty / reasoning_effort / extra_body`，以及协议专属 `thinking`（messages）、`max_output_tokens / instructions`（responses）。
- `api_type`（只读回显，前端不传）：后端根据 models.json 中该模型的 `apiType` 自动确定，可选值 `chat_completions` / `messages` / `responses`（连字符归一为下划线、小写；`apiType` 缺失时默认 `chat_completions`）。
- `headers`（可选）：该角色的**自定义请求头**，`[{"name": "x-foo", "value": "bar"}]` 列表形态（后端也接受 `{"x-foo": "bar"}` 键值字典的手工编辑形态）。持久化位置与 parameter 一致——全局默认存 models.json 顶层 `model_selection.<role>.headers`，会话级存 `_meta.model_selection.<role>.headers`（新会话首次任务开始时随 model_selection 整体快照一次）。继承语义与 parameter 相同：`null` 保持现有（"仅切换模型、自定义头不变"），提供时全量替换，`[]` 清空。**注入链**：请求上游时 ChatLLM 从模型配置的 `_custom_headers` 读取并拼入原始 HTTP 头（放在 Authorization 之后、Content-Length 之前；非法条目——空名/含冒号或空白/含 CRLF——在规范化阶段丢弃，防头部注入）；同名键后者覆盖前者。四个角色各自独立（聊天/压缩/标题/子智能体），聊天角色随 `require_default_chat_config` 注入，其余角色随各自模型解析处注入；原先硬编码的 `x-opencode-session` 已删除，opencode 等供应商需要的会话头改由该配置提供。
- 向后兼容：只传 `{provider, model}` 等价于 `role=chat_model` 且参数与自定义头均不变。

**参数取用回退链**：当前 `api_type` 的桶 > `chat_completions` 桶 > 任意第一个非空桶 > 空（内置默认）。旧版扁平 `parameter`（键为字段名）加载时自动迁移为 `chat_completions` 桶。

**聊天参数优先级**：请求体显式传参 > `model_selection.chat_model.parameter`（按当前聊天模型的 api_type 取桶）> 默认值（`/chat_with_tool` 对未显式提供的生成参数自动按配置填充）。

**压缩模型参数**：`model_selection.compaction_model.parameter`（按压缩模型 api_type 取桶）> 压缩内置默认值（temperature 0.2 / top_p 1.0 / presence_penalty 0.0 / reasoning_effort low / max_tokens 摘要长度预算）。未配置 `compaction_model` 时跟随当前聊天模型（参数仍用压缩配置或内置默认）。

**子智能体模型**：`model_selection.sub_agent_model`（未配置时子智能体继承当前聊天模型——含 ambient 会话覆盖，经 asyncio 上下文自动传递；配置后使用该角色端点与 `parameter` 填充，协议必须为 `chat-completions`）。前端聊天设置面板新增「子智能体模型」选项卡（与聊天/压缩/标题模型同级），未选择时提示"子智能体继承聊天模型"。

**压缩模型优先级**：`model_selection.compaction_model`（未配置时跟随当前聊天模型）。压缩模型完全由 models.json 的 `model_selection` 管理，**不再从 `.env` 读取或写入**（`HISTORY_COMPACT_MODEL_PROVIDER/NAME` 已移除）。

**标题模型**：`model_selection.title_model`（会话覆盖 → 全局默认，仅 chat-completions 协议）消费于**会话标题自动生成（前端驱动，与聊天主链路解耦）**：前端在 SSE 流内收集模型输出（思考/正文 delta），累计达 100 字符（思考/正文取较长者）即调 `POST /chat_config/generate_title`**一次**（流结束不足 100 字符时以已有全文触发）；请求携带【用户问题】（截 300 字）+【输出预览】+ 首问原始 content 部件（多模态）。后端调标题模型生成 ≤30 字标题、写回会话首行 `_meta.title` 与 `_title_state` 标记（source/model/applied_at）并把标题**同步返回**——前端收到即替换侧栏标题，**零轮询**（不再轮询 `/chat_history/meta`）。**生成时机（会话独立配置 `retitle_each_message`，前端聊天设置弹窗开关）**：关闭（默认）时仅首个任务尝试一次；开启时每轮都重新生成（覆盖式，手动重命名后的标题也会被覆盖，属该开关显式语义）。**跳过条件**：已有 `title_generated`（首轮已生成/手动重命名过）或 `attempted`（失败标记）时接口直接回显当前标题、不调标题模型。**失败防重试**：一次调用失败（上游 4xx/5xx、空输出等）后写 `_title_state.attempted` 标记（含 `title_attempted_at`），之后轮次不再自动重试；需要重试时开启「每条消息重新标题」（开启瞬间清除 attempted）或手动重命名会话。接口内同会话 2s 防抖窗口防重复请求。未配置标题模型或调用失败时**保持旧机制标题**（首个用户问题前 40 字兜底），零行为变化。**多模态标题**：首问含图片/视频/音频时，标题模型支持视觉（该模型 vision=true）则媒体解析为 base64 一并送入（大图自动降采样，与聊天主链路同口径）；不支持视觉时媒体部件按 vision=false 同口径转为「[图片 media://x.png]」文本占位（不发 base64）。请求参数：标题模型 `parameter`（按 api_type 取桶）> 内置默认（temperature 0.3 / top_p 1.0 / presence_penalty 0.0 / max_tokens 64 / reasoning_effort low，无工具、非流式、不拼后端历史）。注意：所选标题模型需在其网关侧实际可用——部分网关按角色/工作区对模型做区域门控（如同 provider 下聊天模型可用而标题角色调用返回 403 RegionError），失败会打印可行动的 WARN 并保持旧机制标题，更换标题模型即可。**自定义请求头**：标题角色的 `headers` 配置（模型面板标题模型 tab）随配置注入上游请求（opencode 等需会话头的供应商在此配置）。

### 工具选择持久化（mcp_servers.json inputs + 会话 _meta.tool_selection）

前端工具弹窗点击"确定"后调用 `POST /chat_config/tool_selection`：**新对话**（尚未产生会话 ID）中保存到全局默认，**已有会话**中保存到该会话独立字段；打开/切换会话与新建对话时调用 `GET /chat_config/tool_selection`（携带当前会话 ID）恢复该会话的生效勾选（替换语义）。全局选择保存在 `setting/mcp_servers.json` 顶层 `inputs` 键（与模型选择的 `model_selection` 同类设计，读写即时生效）：

```json
{
  "inputs": {
    "__builtin__": ["ask_user", "read_media"],
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
- 上传文件保存为 `history_files/session_files/<session_id>/<文件名>.json`（目录按前端传入的 `session_id` 命名，仅存解析出的文本内容）；
  同时把**原始文件字节**另存到 `history_files/session_files/<session_id>/files/<名>_<随机>.<ext>`（供 `GET /file/get_session_document` 点击预览/下载），
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
上传聊天多媒体附件（图片/音频/视频），**原始字节**保存到 `history_files/session_files/<session>/media/`。
- Form: `files`(必填, 多文件)；Query: `session_id`
- 限制：≤10 个/次；单文件上限按类别——图片/音频 20MB、**视频 600MB（流式落盘，不整体读入内存）**；扩展名白名单——图片 png/jpg/jpeg/gif/webp/bmp/ico/tif/tiff（ico/tif/tiff 视觉模型不原生接受，上传时由 Pillow 自动转为 PNG 再落盘，需安装 pillow）、音频 wav/mp3/m4a/ogg/flac、视频 mp4/webm/mov/mkv
- 返回: `{total, success, failed, results[], upload_id}`；`results[]` 每项 `{filename, status, stored_name, media_ref, kind(image/audio/video), mime, size}`
- 聊天消息引用方式：`content` 部件列表中 `{"type":"image_url","image_url":{"url":"media://<stored_name>"}}`（音频为 `{"type":"input_audio","input_audio":{"data":"media://<stored_name>","format":"wav"}}`，视频 `video_url`）。
  后端发送上游前把 `media://` 引用解析为 OpenAI 兼容格式：URL 类部件 → `data:<mime>;base64,<b64>`，`input_audio.data` → 纯 base64；`https://` 与已内联 `data:`/base64 原样透传（url 与 base64 双格式兼容）。
  **大图自动降采样**：超过 2MB 的图片（GIF 除外，无论动静）在解析时改发 JPEG 缩略图——长边 ≤1568px、质量 85，缓存到 `<session>/thumbs/<原名>.thumb.jpg`（按原文件 mtime+size 指纹失效重建）；生成失败回退原图。原图仍完整落盘，预览/下载不受影响。前端在**发送前**也做同等压缩（canvas 重采样，压缩无收益保留原文件），双保险进一步降低上传体积与视觉 token 计费。
  **视觉 API 拒绝格式的自动转换（发送上游前，不动落盘原图）**：静图 gif 与 bmp/ico/tif/tiff 统一转 PNG；**动图 gif 转 H.264 MP4**（Pillow `is_animated/n_frames` 判动静动，动图经 ffmpeg fps=10 + minterpolate 补帧 30fps + libx264 编码为 mp4，部件类型随之变为 `video_url`；ffmpeg 定位三级回退：系统 PATH → `imageio-ffmpeg` 包内置静态二进制 → 均缺失时按静图回退链路兜底）；转换缓存到 `<session>/media_transcode/<原名>.conv.<png|mp4>`（同款 mtime+size 指纹失效）。原生格式（png/jpg/jpeg/webp）不变。
- 历史落盘与回放只保留 `media://` 引用（JSONL 不膨胀），历史轮次不重复注入媒体数据；文本口径提取 text 部件，媒体部件以 `[图片]/[音频]/[视频]` 占位。

### GET /file/get_session_media
读取会话内已上传的媒体文件原始字节（消息气泡缩略图、音视频播放等用途）。
- Query: `name`(必填, 即 stored_name，禁止路径分隔符), `session_id`
- **支持 HTTP Range 请求**（`bytes=start-end` / `bytes=-N`，命中时返回 206 Partial Content，带 `Accept-Ranges`/`Content-Range` 头）：音频/视频进度条可**即时拖动跳转**，无需等待整体缓冲完成
- 返回: 文件字节流（Content-Type 按扩展名推断；带 Range 时为 206 切片）；不存在 404

### GET /file/get_session_document
读取会话内已上传文档的**原始文件字节**（前端点击预览 PDF/文本/下载原文件）。
原始字节在上传解析时另存于 `history_files/session_files/<session>/files/`（`upload_session_files` 返回的 `stored_name`，并写入 file_memory 记录）；旧版本上传的文档无原始字节，无法预览。
- Query: `name`(必填, 即 stored_name，禁止路径分隔符), `session_id`
- 支持 HTTP Range 请求（206），大 PDF 按需加载
- 返回: 文件字节流（Content-Type 按扩展名推断）；不存在 404

### 内置 read_media 工具（factory/agent_runtime/builtin_tools.py）
模型可调用的媒体读取内置工具（「配置工具」→「内置工具」分组勾选 `read_media`），
把媒体来源解析为 base64 回传给多模态模型，**本地/网络/用户上传统一入口**：

- `references`：媒体来源数组（可混用）：
  1) `media://` 会话引用（用户上传或 read_media 自行注册的来源）；
  2) 本地文件路径（绝对路径或相对会话工作目录的路径，worker 进程已 os.chdir）；
  3) http(s) 网络直链（下载后按魔数嗅探校准扩展名，30 秒超时、跟随重定向）；
  本地/网络来源首次读取会自动注册进会话媒体库（`register_media_source`，
  与用户上传同规则：类型校验、大小上限、ICO/TIFF 自动转 PNG），重复读取走
  会话链路（无需重复下载/读盘）；重复读取自动去重；
- `quality`：可选，50-100（默认 85）——仅对超过 2MB 的大图降采样生效
  （长边 1568、JPEG 质量按该参数；小图/GIF 按转换后口径回传）；
- `start_time` / `end_time`：可选，视频与动图 gif 的**区间读取**秒数
  （精确到帧，毫秒精度；内部按源帧率吸附到帧边界）。语义约定：
  - **未指定区间**的视频/动图默认读 `[0, min(时长, 60s)]`（单次注入上限
    `VIDEO_MAX_READ_SECONDS = 60`），返回 `video` 元信息块含实际读取区间，
    `truncated=true` 表示还有剩余内容，模型可按 `[上一段 end, end+60]` 续读；
  - **显式区间**（仅 `start_time` 或两者）：单次超过 60 秒自动截断为
    `[start, start+60]` 并标记 `truncated`；
  - **元数据探测约定**：`start_time == end_time`（如 `0`/`0`）只返回元数据
    文本（时长/帧率/分辨率/大小，不回传视频数据）；`end_time < start_time`
    参数错误；`start_time` 超出视频时长参数错误；
  - 返回值 `loaded[]` 每项携带 `video` 块 `{duration, fps, width, height,
    read:{start,end,seconds,truncated}}`（探测约定时为 `probe:true` 且无
    `read`）；`duration` 不可得时按帧计数估算（`estimated_duration:true`）；
  - 动图 gif 先转码 mp4 再按区间切片（同一转码缓存复用）；切片缓存
    `<原名>.clip_<start>_<end>.mp4` 按源文件指纹失效；**网络直链视频不支持
    区间读取**（URL 直传由供应商拉取）；同引用不同区间视为新的读取
    （去重键 `reference#start-end`），相同区间重复读取仍去重；
- **格式自动转换**（与聊天附件注入同口径）：视觉 API 拒绝的图片格式在
  构建部件前自动转换——静图 gif 与 bmp/ico/tif/tiff → PNG（Pillow）、
  **动图 gif → H.264 MP4**（流式字节扫描判动静动 + ffmpeg 转码「先缩放后
  插值」，部件类型变为 `video_url`；转码失败回退首帧 PNG/JPEG 缩略图）；
  `loaded[]` 每项额外携带 `converted` 元信息（`{original_format,
  converted_format, animated, converted_kind}`，未转换为 null）；网络直链
  遇到被拒格式时临时下载（上限按内容判定：gif 600MB / 其他 30MB）走同一
  转换内核，转换失败回退 URL 直传原行为；
- **ffmpeg 定位三级回退**：系统 PATH → `imageio-ffmpeg` 包内置的静态
  二进制（requirements.txt 已含，`pip install imageio-ffmpeg` 即得，无需
  系统级安装/管理员权限，自带 libx264 与 minterpolate）→ 转换不可用
  （静图 gif 回退首帧 PNG）；fresh 环境装完 requirements 即完整可用；
- 规模边界沿用媒体上传规则（图片/音频 20MB、视频 600MB）；读取侧按内容判定：静图类 30MB、动图 gif 与视频 600MB、音频 20MB，超限直接拒绝（错误占位注明原因，绝不注入原始大文件）；
- **无内容安全边界**（当前策略：工具由用户提供；本地/网络来源不做白名单
  拦截，本地路径相对会话工作目录解析）；用户授权确认逻辑后续接入：
  来源审计字段（`source_type` = session/network/local、`source` = 原始引用）
  已在加载链路沿途携带，届时在 `register_media_source` 入口挂确认钩子；
- 返回为纯文本元信息（读取了哪些、来源类型、实际生效 quality、是否降采样/
  跳过原因）；**数据本体（base64）不进入工具结果文本**——工具结果按原样落盘
  JSONL，媒体数据由后端按 loaded 项的 `reference+quality+区间` 坐标经
  `load_any_media_model_part` 现场加载为 user 消息多模态部件
  （`image_url`/`video_url`/`input_audio`），在工具结果处理完成后追加进
  后续模型请求上下文（仅内存、不落盘历史），token 统计按多模态部件固定
  占位计费；加载失败时以占位文本告知模型；
- **单次调用最多 5 个**（超出的引用标记 `limit_5_per_round` 跳过）；任务内多次
  读取累计时按**滚动窗口只保留最近 5 个部件**，被挤出的引用解除「已注入」标记，
  模型再次读取同一引用可重新注入。

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

## 9. 重启维护工具（RestartMcp，mcp_server/restart_tools_server.py）

模型可调用的开发运维 MCP 工具（工具选择中勾选 `RestartMcp` 分组即授权），用于
「修改代码后自驱动重启服务，并在重启完成后续接同一会话任务」。三个工具均为
MCP 工具链路调用，返回 JSON 字符串：

### restart_service
- 参数: `session_id`（必填）、`follow_up`（必填，≤500 字符续任务指令）、
  `delay_seconds`（10-180 默认 25：派发到强制终止的延迟秒数）
- 行为: 校验（follow_up 非空 / service_state.json 快照存在且 pid 存活 / 60s 冷却 /
  无残留调度）→ 写 `history_files/lock/restart_pending.json` 与
  `restart_pipeline.cmd` → 以分离进程派发流水线后**立即返回 scheduled**；
  流水线延迟 delay 后 `taskkill /F /T` 树杀主进程（含会话 worker 防孤儿），
  拉起项目根 `restart_helper.py`：等端口释放 → 分离启动 main.py → 健康轮询 →
  向 `POST /chat_with_tool` 注入 follow_up 作为该会话最新用户消息
  （`use_backend_history` 默认 true，服务端自动拼接全部历史；注入成功后
  删除 pending 文件）。
- 返回: 成功 `{"ok": true, "status": "scheduled", main_pid, new_port,
  delay_seconds, message, pending, log}`（此刻进程尚未被杀）；失败
  `{"ok": false, "reason"}`（含冷却期 / already_scheduled / 快照缺失 /
  pid 已不存活 / 参数非法等原因，请先按 reason 处理勿盲重试）。
- 使用约定: 调用前把当前进展与下一步计划写入 follow_up；调用后**立即结束本轮**
  （不再调用其它工具），等流水线在后台完成杀/启/注入；结果查证
  读 `history_files/lock/restart_done.json`（ok=true 且 injected=true 为成功）。

### restart_cancel
- 参数: 无
- 行为: 撤销尚未执行的重启调度（删除 restart_pending.json 与 restart_pipeline.cmd）；
  已进入 helper 恢复阶段（pending 被消费）时无法撤销。

### restart_status
- 参数: 无
- 行为: 只读返回 `{"cooldown_remaining", "state"(service_state.json),
  "pending", "done"}`，供模型或人工排障。

落盘文件（均位于 `history_files/lock/`）：`service_state.json`（main.py 启动时写的
pid/port/project_root/python_exe 快照）、`restart_pending.json`、
`restart_pipeline.cmd`、`restart_done.json`、`restart_helper.log`、
`restart_pipeline.log`、`restart_cooldown.json`。测试: `test/test_restart_tool.py`
（18 用例，调度全部模拟派发不真杀进程）、`test/restart_stdio_smoke.py`（stdio
协议冒烟）、`test/manual_restart_e2e.py`(真机人工验证清单)。

## 10. 表格导出 Export

聊天里 md 表格「更多」菜单（复制 Markdown / 下载 Excel）的后端支撑。
xlsx 生成器为纯标准库实现（`factory/xlsx_export.py`，zipfile + XML，零第三方依赖），
md 表格解析在 `factory/md_table_export.py`（与前端同语义：行内代码/转义竖线不切断分列、
行内标记清理、链接取 URL）。

### POST /export/table/xlsx
- Body: `{"markdown": md表格原文(必填, ≤200KB), "filename": 下载文件名(可选, ≤60字符)}`。
  `markdown` 为渲染时保存在 `data-table-raw` 里的原始表格文本（含表头行 + 分隔行）。
- 返回: xlsx 文件字节流（Content-Type
  `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`），
  响应头 `Content-Disposition`（filename* UTF-8 中文文件名）、
  `X-Export-Rows` / `X-Export-Cols`（实际行列数）；解析失败 422 `{detail}` 中文原因
  （非表格文本 / 缺分隔行 / 规模超限：行 ≤5000 列 ≤64 总格 ≤200000）。
- xlsx 特性：Sheet 名 `Sheet`；首行表头加粗样式；数字单元格原生数值类型；
  单元格内换行转义为 `_x000A_`；清洗异常单元格写入灰色底红字错误占位样式
  （显示「原值 + 导出失败说明」，不拖垮整表）。
- 前端交互：`H5/js/app/messages.js`（表格按钮事件与下载）、`H5/js/markdown.js`
  （`data-table-raw` 保存原文与「复制 / 更多」按钮组：更多菜单含
  复制 Markdown / 复制图片 / 下载 Excel）、`H5/js/table_canvas.js`
  （复制图片 canvas：列宽两轮收敛 + 行高自适应，长文本逐字换行不再截断）。
  测试: `test/test_table_export.py`（17 用例）、`H5/test_h5/table_export.test.js`。
