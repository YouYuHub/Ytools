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
| temperature | `float` | 0.7 | 随机性 |
| top_p | `float` | 1.0 | 核采样 |
| reasoning_effort | `string` | "medium" | 思考深度 `low/medium/high` |
| presence_penalty | `float` | 2.0 | 重复惩罚 |
| timeout_connect/read/drain | `int` | 300/1800/120 | 连接/读取/冲刷超时（秒） |
| extra_body | `dict?` | null | 额外参数（如 enable_thinking） |
| parallel_tool_calls | `bool` | true | 允许并行工具调用 |
| tool_choice | `string\|dict` | "auto" | `none/auto/required` 或指定函数 |
| use_backend_history / backend_history_rounds | `bool/int?` | null | 是否/最近几轮启用后端历史拼接（null=服务端默认） |
| tools | - | - | 服务端内部字段，勿传 |

**响应** (SSE 事件，每行 `data: {json}`，以 `data: [DONE]` 结束):
- `warning`：提示（如未选工具）
- `reasoning_content` / `content`：思考与正文增量
- `tool_calls`：工具调用增量（按 index 合并）
- `tool_return`：工具执行结果 `{function_name, arguments, result}`
- `usage`：token 统计；`finish_reason`：结束原因（stop/length/tool_calls）

### POST /stop_chat
停止指定会话的聊天任务。
- Query: `session_id`（默认 "default"）
- 返回: `{"stop_chat": "stopped"}`；失败 400

## 2. 聊天历史 ChatHistory（query 均含 `session_id`，默认 "default"）

| 接口 | 方法 | 参数 | 返回 |
|---|---|---|---|
| /chat_history/sessions | GET | - | 会话文件名列表 `["default_chat.jsonl", ...]` |
| /chat_history/file | GET | session_id | 下载会话 JSONL 文件（首行 `_meta` 元数据） |
| /chat_history/meta | GET | session_id | `{session_id, title, user_questions, usage, created_at, updated_at, record_count, completion_count, context_summary}` |
| /chat_history/delete_file | DELETE | session_id | `{state, describe}` |
| /chat_history/delete_lines | DELETE | startline(必填,≥1), endline(必填,≥1,包含), session_id | `{state, describe, meta_after, usage_after}`；行号**不含** `_meta` 首行；startline>endline 报 400 |

## 3. 配置 ChatConfig

| 接口 | 方法 | 参数 | 返回 |
|---|---|---|---|
| /change_chat_dir | POST | Query `new_dir`(必填) | `{state, message, current_dir, persisted_env}`；空路径 400 |
| /chat_config/work_dir | GET | - | `{state, current_dir, persisted_dir, is_consistent, env_name, env_value, env_file, source, read_only}` |
| /chat_config/history_compaction | GET | - | `{keep_rounds, trigger_ratio, env_names, memory_state}` |
| /chat_config/history_compaction | POST | Body `{keep_rounds:int≥1, trigger_ratio:float 0<r≤1}` | `{state, updated, config, memory_state}` |
| /chat_config/models | GET | - | `{state, count, current, models[]}`；`models[]` 为扁平 `{provider_name, model_name, api_type, url, vision, tool_calling, max_input_tokens, max_output_tokens, api_key_present}` |
| /chat_config/models/select | POST | Body `{provider, model}` | `{state, message, current, memory_state}`；组合不存在报 400 |

## 4. 工具 ToolsManage

| 接口 | 方法 | 参数 | 返回 |
|---|---|---|---|
| /tools/list | GET | - | `{tools[], total, servers[], failed_servers[], discovery, server_metrics[], mode}`；实时探测 `mcp_servers.json`，单服务超时/失败不阻塞（记录于 failed_servers） |

## 5. 文件 FileUpload

### POST /file/upload_session_files
上传文件并解析文本（≤10 个，单个 ≤10MB），结果写入文件记忆。
- Form: `files`(必填, 多文件)；Query: `session_id`
- 返回: `{total, success, failed, results[]}`；`results[]` 每项 `{filename, status(success/failed), message?, type?, content_length?}`

| 接口 | 方法 | 参数 | 返回 |
|---|---|---|---|
| /file/get_session_file_memory | GET | number(1-10,默认10), session_id | `{total, files[]}`，`files[]` = `{timestamp, filename, type, content, size}` |
| /file/get_session_file_text | GET | number(默认10), max_total_chars(默认3000), session_id | `{summary, length}`（纯文本摘要，供 LLM 上下文） |
| /file/delete_session_file_memory | DELETE | filename(必填), session_id | `{message, deleted_count}` |
| /file/clear_session_file_memorys | DELETE | session_id | `{message}` |

## 6. 根路由
- `GET /` → `{"message": "欢迎使用大模型智能体工具接口"}`
