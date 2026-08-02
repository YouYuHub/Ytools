# 智能体工具使用测试 - 项目结构

## 概述
基于 **FastAPI** 的智能体工具服务平台，核心业务：大模型对话（SSE 流式）、MCP 工具调用、文件解析、会话记忆与历史压缩、模型/工作目录动态配置。

---

## 一、核心业务流程

### 1. 工具聊天主流程（`factory/chat_factory.py: tool_chat_server`）
这是整个系统的核心循环：

```
接收 ChatLLMRequest(session_id, tool_names, ...)
   │
   ├─ ① 校验模型配置：require_default_chat_config()
   │     （.env 的 CHAT_PROVIDER_SELECTION/CHAT_MODEL_SELECTION → models.json 解析出 url/model/apiKey）
   ├─ ② 实时刷新工具：refresh_tools_from_mcp() → ALL_TOOLS / TOOL_MCP_SERVERS
   │     （并发探测 setting/mcp_servers.json 中所有 MCP 服务器）
   ├─ ③ 工具白名单过滤：前端只允许传 tool_names，后端实时工具为唯一真相
   │     - 未注册工具名 → 忽略并发 warning 事件
   │     - 未选择工具 → 无工具模式继续
   │     - 有选择时注入内置工具 check_tool_exists
   ├─ ④ 上下文构建：
   │     - 可选后端历史拼接（USE_BACKEND_HISTORY / BACKEND_HISTORY_ROUNDS，前端提供完整历史则跳过）
   │     - 注入系统提示词 + 当前工作路径 + 会话已上传文件解析内容
   ├─ ⑤ 流式请求循环（while run_task）：
   │     ├─ ChatLLM 流式请求（stop_checker 支持手动停止）
   │     ├─ 解析 SSE：合并 tool_calls/function_call 增量（按 index 累加）、收集 usage、捕获 finish_reason
   │     ├─ 有工具调用 → normalize_tool_calls（修复流式拼接粘连）→ 拦截未授权工具
   │     │     → 线程池并发执行 MCP 工具（execute_tool_round，max 3 线程）
   │     │     → 结果写入 messages + 历史 + 发 tool_return SSE → 继续循环
   │     ├─ 无工具调用 → 结束任务（记录 [DONE]）
   │     └─ finish_reason=length → 追加"请继续"提示让模型续写
   ├─ ⑥ 历史压缩：_compact_session_history_if_needed()（超过上下文预算时用 LLM 压缩旧轮次为摘要）
   └─ ⑦ 清理会话管理器 → 输出 data: [DONE]
```

**会话控制**：`ChatMemoryManager.run_task` 属性作为全局停止开关，`/stop_chat` 置 False 即优雅停止。

### 2. 文件上传流程（`routers/file_router.py: /file/upload_session_files`）
```
读取文件（异步）→ 校验大小(≤10MB)/数量(≤10) → 线程池并发解析文本
→ FileMemoryManager 持久化（每文件一个 JSON，最多10个）→ 返回逐文件状态
```
解析后的文本在下一轮聊天时注入系统提示词供模型使用。

### 3. 模型/配置动态切换
- **切换模型**：`POST /chat_config/models/select` → 校验 models.json 存在该组合 → 写回 .env 并同步内存 env_vars
- **切换工作目录**：`POST /change_chat_dir` → `os.chdir()` + 持久化 CHAT_WORK_DIR 到 .env
- **历史压缩参数**：`POST /chat_config/history_compaction` → 写回 HISTORY_COMPACT_KEEP_ROUNDS / HISTORY_COMPACT_TRIGGER_RATIO

---

## 二、目录结构（仅业务代码）

```
agent_tool_sse/
├── main.py                      # FastAPI 入口：init_path → 注册4个路由 → CORS → uvicorn(48621)
├── config.py                    # Pydantic 模型 + 工作目录管理
├── env_manager.py                  # .env/models.json 加载、模型选择解析、DPAPI 加密
├── setting/
│   ├── models.json              # 模型目录：provider→models（含 apiKey/url/能力），variables/runtime 全局变量
│   └── mcp_servers.json         # MCP 服务器注册表：server_id → command/args
│
├── chat/
│   └── chat_llm.py              # ChatLLM：纯标准库 HTTP/1.1 + SSE 流式客户端（同步/异步/非流式）
│
├── factory/                     # 核心业务逻辑层
│   ├── chat_factory.py          # tool_chat_server 主循环、历史压缩、SSE 事件处理
│   ├── chat_runtime.py          # 运行时辅助：usage 聚合、token 估算、消息构建、参数解析
│   ├── tool_registry.py         # MCP 工具发现：并发探测服务器、JSON Schema 过滤
│   ├── tool_executor.py         # 工具调用归一化、参数解析、线程池并发执行
│   └── file_factory.py          # 文件解析器（pdf/docx/doc/csv/xls/xlsx/txt/md）
│
├── memory/                      # 记忆持久化层
│   ├── chat_memory.py           # ChatMemoryManager：会话 JSONL + 元数据 + 轮次聚合
│   ├── chat_round_store.py      # ChatRoundStore：chat_round 轮次状态机（聚合消息/usage）
│   ├── chat_history_format.py   # 历史格式统一：摘要规整/渲染、chat_round→上下文消息
│   ├── file_memory.py           # FileMemoryManager：上传文件记录管理
│   └── timestamp_utils.py       # 全仓统一时间戳格式
│
├── routers/                     # API 路由层
│   ├── chat_router.py           # 聊天主接口 + 历史文件管理
│   ├── chat_config_router.py    # 模型选择/工作目录/历史压缩配置
│   ├── tools_manage_router.py   # 工具列表
│   └── file_router.py           # 文件上传/查询/删除
│
├── util/
│   └── mcp_client.py            # MCP 客户端：构建启动参数、连接会话、调用工具
│
├── mcp_server/
│   ├── sys_server.py            # 系统 MCP 服务器（时间/目录/文件操作）
│   └── PipeCmdMCP.exe           # 命名管道终端 MCP 服务器（setup_pipe/run_pipe_command/read_pipe_history）
│
├── docs/
│   ├── api_docs.md                # 全部 REST 接口出入参数说明
│   ├── chat_round_schema.md       # chat_round JSONL 结构说明
│   └── chat_state_flow.md         # Chat 状态流 Mermaid 流程图
│
└── test/                        # 各模块单元测试（test_chat_llm/test_tool_executor/...）
```

---

## 三、模块业务逻辑详解

### 1. 聊天客户端（`chat/chat_llm.py`）
- 纯标准库实现（socket + ssl），不依赖 openai SDK，支持 HTTP/1.1 chunked 传输解析（处理 UTF-8 被切断问题）
- `chat_completions()` 统一入口：`stream=True` 返回异步生成器，`stream=False` 返回完整字典（供历史压缩用）
- 模型配置热解析：每次请求从 models.json 解析当前选中模型，**连接失败自动无限重试**直到用户停止
- `reasoning_effort` 参数仅对 gpt-5 / o系列 / deepseek-v4 系列下发
- 统一输出 SSE 事件：content / reasoning_content / tool_calls / usage / finish_reason

### 2. 工具聊天工厂（`factory/chat_factory.py`）
- **工具授权模型**：`tool_request.tools` 是服务端内部字段（exclude），由前端 `tool_names` + 后端实时工具生成，未授权工具调用会被拦截并返回 `blocked: true` 事件
- **SSE 事件归一化**：流式 `tool_calls` 按 index 增量合并，兼容老模型 `function_call`；透传前过滤 id/type 字段
- **usage 汇总**：`UsageAccumulator` 按 completion_id + 指纹去重，合并后挂到当前 chat_round
- **内置工具** `check_tool_exists`：本地执行（不经过 MCP），查后端实时工具列表
- **历史压缩**：估算 token 超预算时，用低温度 LLM 调用把最旧轮次压缩成 `{summary, key_facts, open_items, tool_state}` 结构存到 meta，后续上下文优先用摘要
- **错误处理**：上游流错误/用户停止/异常分别记录不同状态，避免误记

### 3. 工具注册与执行（`factory/tool_registry.py` + `tool_executor.py`）
- 注册表：读 `setting/mcp_servers.json`，信号量并发探测（默认4并发、12s超时），失败的服务器记录 metrics 不阻塞；schema 做白名单字段过滤防模型误用
- 执行器：
  - `normalize_tool_calls`：修复流式拼接粘连（多个工具名/多个 JSON 参数被连成一个字符串时自动拆分）
  - `prepare_tool_execution`：解析参数、提取 `over_task` 信号、标记解析错误
  - `execute_tool_round`：线程池并发执行，pipe 类工具（setup_pipe/run_pipe_command/read_pipe_history）连接失败自动重试3次

### 4. MCP 客户端（`util/mcp_client.py`）
- `build_stdio_server_parameters`：按扩展名自动选启动方式（.py→python / .js→node / .jar→java -jar / .exe→直启 / 命令字符串→PATH 查找 / shebang）
- `call_mcp_tool`：会话初始化 → 校验工具存在 → 调用 → 递归解包 `ExceptionGroup` 提取真实异常
- `mcp_servers.json` 中相对路径的 args 自动解析为项目内绝对路径

### 5. 记忆管理（`memory/`）
- **ChatMemoryManager**（`history_files/<session>_chat.jsonl`）：
  - 首行 `_meta` 元数据（title/user_questions/usage/context_summary/record_count），追加时实时重算
  - `ChatRoundStore` 将散消息按 `chat_round` 事件聚合：`{event, question, events[], status, usage_total, completion_count}`
  - 会话管理器注册表缓存 + 读写加锁，保证线程安全；`run_task` 属性控制对话启停
  - 支持按行删除/清空/下载，列表查询
- **FileMemoryManager**（`history_files/upload/<session>/<file>.json`）：每文件一 JSON，超限删最旧，支持文本摘要供 LLM 使用
- **chat_history_format.py**：摘要规范化/渲染、chat_round → 上下文消息的唯一定义（去重、过滤 think 标签、工具调用渲染为 `[调用工具] name(args)`）

### 6. 配置模型（`config.py`）
- `Message`：对话消息（role/content/tool_calls/tool_call_id/reasoning_content）
- `ChatLLMRequest`：聊天请求全参（超时/采样参数/工具选择/会话隔离字段）；`tools` 为服务端内部字段
- 工作目录：`set_current_dir` / `apply_persisted_work_dir`（启动时从 CHAT_WORK_DIR 恢复上次目录）

### 7. 环境与模型配置（`env_manager.py`）
- `.env` 支持 `${VAR}` 变量引用、`enc:dpapi:` Windows DPAPI 加密密钥解密
- `models.json`：支持顶层 provider + 嵌套子 provider 扫描，`variables`/`runtime` 作为全局配置变量
- 模型选择三件套：`list_available_models`（扁平列表给前端）/ `get_current_model_selection` / `select_chat_model`（写盘+同步内存）
- `set_env_vars`：写回 .env 并保持内存 env_vars 一致（防 hot-reload 覆盖）

### 8. 系统 MCP 服务器（`mcp_server/sys_server.py`）
| 工具 | 业务 |
|---|---|
| format_current_time | 当前时间 |
| list_dir_item | 列目录（递归/按类型过滤/深度控制） |
| create_dir / create_file / write_file_lines | 目录与文件操作 |
| get_file_content | 读取文件指定行范围 |

### 9. API 接口总览
> 完整出入参数说明见 `docs/api_docs.md`，在线文档见 `/docs`

| 路由 | 方法 | 业务 |
|---|---|---|
| `/chat_with_tool` | POST | 聊天主接口（SSE 流式） |
| `/stop_chat` | POST | 停止指定会话任务 |
| `/chat_history/sessions` / `file` / `meta` | GET | 会话列表 / 下载历史文件 / 元数据 |
| `/chat_history/delete_file` / `delete_lines` | DELETE | 删除会话 / 删除指定行范围 |
| `/tools/list` | GET | 实时探测并列出全部 MCP 工具 |
| `/file/upload_session_files` | POST | 上传并解析文件（≤10个/≤10MB） |
| `/file/get_session_file_memory` / `get_session_file_text` | GET | 文件历史 / LLM 文本摘要 |
| `/file/delete_session_file_memory` / `clear_session_file_memorys` | DELETE | 删单文件 / 清空会话文件 |
| `/change_chat_dir` | POST | 切换并持久化工作目录 |
| `/chat_config/work_dir` | GET | 当前工作目录一致性检查 |
| `/chat_config/history_compaction` | GET/POST | 历史压缩参数读写 |
| `/chat_config/models` | GET | 可用模型列表 + 当前选择 |
| `/chat_config/models/select` | POST | 切换当前模型 |
