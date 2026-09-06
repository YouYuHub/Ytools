# Ytools 智能体工具使用测试 - 项目结构

## 概述
基于 **FastAPI** 的智能体工具服务平台，核心业务：大模型对话（SSE 流式、后台任务 + 断线重连）、MCP 工具调用、**多模态文件上传**（图片/音频/视频，media:// 引用解析为 OpenAI 兼容格式）、文件解析、会话记忆与历史压缩、模型/工作目录动态配置；另附 **H5 前端**（聊天界面，含附件粘贴/预览）与交互测试脚本。

---

## 一、目录结构（仅业务代码）
```
agent_tool_sse/
├── main.py                      # FastAPI 入口：init_path → 注册4个路由 → CORS → 自定义 OpenAPI(binary format 补丁) → 配置热重载线程 → uvicorn(48621)
├── config.py                    # Pydantic 模型 + 工作目录管理 + mcp_servers.json 读取/规整/写入
├── env_manager.py               # .env/models.json 加载、模型三角色选择（model_selection）、DPAPI 加密、models.json 热重载
├── requirements.txt             # 依赖清单（fastapi/uvicorn/pydantic/mcp，可选 fitz/docx/pandas）
├── .env                         # 一些全局配置
├── project_structure.md         # 本文档
├── remove_pycache.py            # 工具：递归清理 __pycache__ 目录
├── tmp_repro.py / _chunk*.txt / _diag.txt   # 调试/问题复现用临时文件
│
├── setting/
│   ├── models.json              # 模型目录：provider→models（含 apiKey/url/能力），variables/runtime 全局变量，
│   │                            #   顶层 model_selection（chat_model/compaction_model/title_model 三角色选择）
│   └── model_config.json        # MCP 服务器注册表：server_id → command/args
│
├── chat/
│   └── chat_llm.py              # ChatLLM：纯标准库 HTTP/1.1 + SSE 流式客户端（同步/异步/非流式）
│
├── factory/                     # 核心业务逻辑层
│   ├── agent_runtime/           # Agent 编排所需的可复用运行时组件
│   │   ├── chat_runtime.py      # usage 聚合、token 估算、消息构建、参数解析、回传长度解析
│   │   ├── context_compaction.py # 跨轮/单轮上下文压缩、累计摘要与问题索引、超大结果拒绝阈值
│   │   ├── builtin_tools.py     # 本地内置工具（check_tool_exists、todo_write 任务计划、ask_user 向用户提问；工具选择中的伪服务 __builtin__）
│   │   ├── tool_registry.py     # MCP 工具发现：并发探测、JSON Schema 过滤、探测结果 TTL 缓存（/tools/list 与发送路径共用，?refresh=1 强制重探）
│   │   └── tool_executor.py     # 工具调用归一化、参数解析、线程池并发执行
│   ├── session_worker.py        # 每会话独立 worker 进程：任务开始 os.chdir(会话目录)、命令/事件 IPC、主进程代理
│   ├── chat_factory.py          # tool_chat_server 主循环、后台生成任务 + SSE 重连编排、首调用预算检查、超大结果拒绝
│   └── file_factory.py          # 文件解析器（pdf/docx/doc/csv/xls/xlsx/txt/md）
│
├── memory/                      # 记忆持久化层
│   ├── chat_memory.py           # ChatMemoryManager：会话 JSONL + 元数据 + 轮次聚合 + 导入/删除/压缩事件落盘
│   ├── chat_round_store.py      # ChatRoundStore：chat_round 轮次状态机（聚合消息/usage/压缩状态）
│   ├── chat_history_format.py   # 历史格式统一：摘要规整/渲染、chat_round→上下文消息（含工具结果回传模式）
│   └── file_memory.py           # FileMemoryManager：上传文件记录管理
│
├── routers/                     # API 路由层
│   ├── chat_router.py           # 聊天主接口 + 会话状态 + 历史文件/元数据/标题/导入/删除 + 上下文统计/手动压缩
│   ├── chat_config_router.py    # 模型选择/工作目录/工具选择/历史压缩/回传长度配置
│   ├── tools_manage_router.py   # 工具列表（默认返回探测缓存，refresh=1 强制重探）
│   ├── prompt_router.py         # Skills 提示词库接口：md_files 增删改查/重命名（名称白名单+路径逃逸校验）
│   └── file_router.py           # 文档上传解析（原始字节另存为预览）/媒体上传（视频500MB流式）/Range 读取
│
├── prompt/                      # Skills 提示词库模块
│   ├── prompt_manager.py        # md_files 目录初始化（空目录播种示例）、文件名规整与增删改查
│   └── md_files/                # 用户自管理的可复用聊天提示词（Markdown，可前端编辑也可直接改文件）
│
├── util/
│   ├── timestamp_utils.py       # 全仓统一时间戳格式
│   ├── file_lock.py             # 跨进程文件锁（msvcrt/flock）：会话 JSONL 多进程写入互斥
│   ├── config_watcher.py        # 配置文件热重载轮询线程（mcp_servers.json / models.json，hash 比对 + 重载回调）
│   └── mcp_client.py            # MCP 客户端：构建启动参数、连接会话、调用工具
│
├── mcp_server/
 │   ├── sys_tools_server.py            # 系统 MCP 服务器（文件读写/正则搜索/命令执行/网页抓取与搜索）
│   └── PipeIpcMCP.exe           # 命名管道终端 MCP 服务器（setup_pipe/run_pipe_command/read_pipe_history）
│
├── H5/                          # 前端聊天界面（纯静态，由后端同端口静态托管或本地打开）
│   ├── README.md                # 前端使用说明
│   ├── index.html               # 单页应用入口（Ytools）
│   ├── js/                      # theme.js / api.js / markdown.js / session_utils.js 等工具模块 +
│   │                            # app.js（入口）+ app/（14 个功能模块：core/sessions/stats/workdir/
│   │                            # messages/history/compaction/builtin/media/skills/composer/
│   │                            # model_panel/tools/chat；skills=提示词库可拖拽对话框）
│   │   └── vendor/prism/        # 代码高亮（按语言拆分）
│   ├── style/scss/ → style/css/main.css   # SCSS 源与编译产物
│   └── test_h5/                 # 前端工具函数单元测试（format_utils/history_parser/markdown/session_list_utils/session_utils）
│
├── docs/
│   ├── api_docs.md              # 全部 REST 接口出入参数说明
│   ├── chat_momory_template.json # 聊天记忆 JSONL 模板
│   └── compact.md               # 压缩逻辑说明文档
│
├── history_files/               # 运行时数据：<session>_chat.jsonl（会话历史）+ upload/<session>/（上传文件解析结果）
│
└── test/                        # 单元测试（test_chat_llm / test_tool_executor / test_chat_runtime /
                                 #   test_context_compaction / test_chat_router / test_upload_id_and_delete / ...）
```

---

## 二、核心业务流程

### 1. 工具聊天主流程（`factory/chat_factory.py: tool_chat_server`）
这是整个系统的核心循环，生成任务在**后台独立任务**中运行，SSE 消费端可断线重连：

```
接收 ChatLLMRequest(session_id, tool_names, ...)
   │
   ├─ ① 校验模型配置：require_default_chat_config()
   │     （models.json 顶层 model_selection.chat_model → 解析出 url/model/apiKey；
   │       未配置时回退 .env 的 CHAT_OWNERSHIP_NANE/CHAT_MODEL_NAME）
   ├─ ② 刷新工具（缓存优先）：探测缓存 TTL 内直接复用，过期才 refresh_tools_from_mcp() → ALL_TOOLS / TOOL_MCP_SERVERS
   │     （并发探测 setting/mcp_servers.json 中所有 MCP 服务器；服务启动时已由后台线程预热缓存）
   ├─ ③ 工具白名单过滤：前端只允许传 tool_names，后端实时工具为唯一真相
   │     - 未注册工具名 → 忽略并发 warning 事件
   │     - 未选择工具 → 无工具模式继续
   │     - 有选择时注入内置工具 check_tool_exists；工具选择「内置工具」分组勾选的项也注入（伪服务 __builtin__，与 MCP 工具共用选择持久化）：todo_write（模型自我规划，状态存 _meta.todo 并经 SSE todo 事件实时推送）、ask_user（向用户提问，工具结果为 waiting_user 占位并推 SSE ask_user 事件，当轮任务暂停，用户回答作为下一条用户消息开启新一轮）
   ├─ ④ 上下文构建：
   │     - 可选后端历史拼接（USE_BACKEND_HISTORY / BACKEND_HISTORY_ROUNDS，默认跟随 HISTORY_COMPACT_KEEP_ROUNDS，前端提供完整历史则跳过）
   │     - 注入系统提示词（含当前工作路径、思考过程回传长度说明）+ 会话已上传文件解析内容
   │     - 首调用预算检查：历史+文件+当前请求+工具定义超模型窗口时，
   │       文件记忆先降级为 3000 字符摘要（warning: CONTEXT_BUDGET_FILE_DOWNGRADED），
   │       仍超窗则推送 error 终止
   ├─ ⑤ 流式请求循环（while session_chat_memory.run_task，按会话隔离停止）：
   │     ├─ ChatLLM 流式请求（stop_checker 支持手动停止）
   │     ├─ 解析 SSE：合并 tool_calls/function_call 增量（按 index 累加）、收集 usage、捕获 finish_reason
   │     ├─ 有工具调用 → normalize_tool_calls（修复流式拼接粘连）→ 拦截未授权工具
   │     │     → 内置工具本地执行 + 线程池并发执行 MCP 工具（execute_tool_round，ONE_TASK_MAX_WORKERS）
   │     │     → 超大结果拒绝（超阈值不进入模型上下文，改写反馈让模型重新规划，连续超长达上限终止）
   │     │     → 结果写入 messages + 历史 + 发 tool_return SSE → 单轮压缩检查 → 继续循环
   │     ├─ 无工具调用 → 结束任务（记录 [DONE]）
   │     └─ finish_reason=length → 追加"请继续"提示让模型续写
   ├─ ⑥ 上下文压缩：agent_runtime/context_compaction.py
   │     - 跨轮历史在统一流程中归并为一个累计摘要，所有历史问题独立保留最近 10k tokens
   │     - 单轮工具轨迹超过「min(聊天窗口,压缩窗口)×比例」阈值时，在下一次模型调用前压缩为累计摘要
   │     - 压缩开始/完成事件实时推 SSE 并落盘 JSONL（字段一致）
   └─ ⑦ 清理会话管理器 → 输出 data: [DONE]
```

**会话控制**：每个会话独立后台任务（`_SESSION_STREAMS` 注册表 + `_SessionStream` 有界环形事件缓冲）。
- 首个请求启动 `asyncio.create_task` 生成循环；页面刷新/断线后重连只订阅缓冲，从当前轮起点回放（`replay` 标记 + 提问文本）；
- 携带新用户消息的请求会平滑打断旧任务后重启；
- `/stop_chat` 仅停止指定会话（`ChatMemoryManager.run_task` 按 session_id 隔离）；`GET /chat_stream/status` 供前端查询后台任务是否在跑。

### 2. 文件上传流程（`routers/file_router.py`）
```
文档：/file/upload_session_files —— 读取文件（异步）→ 校验大小(≤10MB)/数量(≤10)
  → 线程池并发解析文本 → FileMemoryManager 持久化（JSON，最多10个）
  → 原始字节另存 files/ 目录（stored_name，供点击预览/下载）→ 返回逐文件状态
媒体：/file/upload_session_media —— 图片/音频/视频原始字节存 media/ 目录
  （图片/音频 ≤20MB；视频 ≤500MB 流式落盘；ico/tif/tiff 自动转 PNG）
  → 返回 media:// 引用，聊天消息 content 部件引用，发送上游前解析为 base64
读取：/file/get_session_media、/file/get_session_document 均支持 HTTP Range（206），
  音频/视频进度条即时拖动跳转；/file/get_session_document 供 PDF/文本预览与下载
```
解析后的文本在下一轮聊天时注入系统提示词供模型使用；媒体以 `media://` 引用保存在历史中（JSONL 不膨胀）。上传目录名（按前端传入的 session_id 命名，可能与会话文件名不一致）会记录到会话 `_meta.upload_id`，删除会话时可连带清理。

### 3. 模型/配置动态切换
- **切换模型**：`POST /chat_config/models/select`（body: `provider/model/role/parameter`）→ 校验 models.json 存在该组合 → 写入 models.json 顶层 `model_selection.<role>`（三角色：chat/compaction/title）并同步内存，实时生效；不再写 .env。**会话级覆盖**：携带 `session_id` 时写入该会话 `_meta.model_selection.<role>`（仅覆盖该角色，`clear=true` 清除恢复跟随全局），生成/压缩/参数默认值填充按「会话覆盖 → 全局默认」逐角色解析——ambient 任务级上下文（ContextVar）实现，`create_task` 上下文隔离保证多会话互不影响；覆盖模型失效时发 `MODEL_SELECTION_FALLBACK` warning 并回退全局
- **切换工作目录**：两层目录——`POST /change_chat_dir` 设置全局默认（DEFAULT_CHAT_WORK_DIR，新会话初始目录；前端在**新对话**中双击路径修改的就是该默认值）；`POST /chat_config/work_dir` 设置会话独立目录（写会话 `_meta.work_dir`）。生成任务默认在每会话独立 worker 进程中执行，任务开始时按「会话覆盖 → 全局默认 → 进程 cwd」解析并 `os.chdir`，相对路径与 MCP 子进程随会话隔离（`CHAT_WORKER_MODE=inline` 回退主进程旧语义）
- **工具选择**：两层选择——全局默认存于 mcp_servers.json 顶层 `inputs` 键（不携带 session_id 的 `POST /chat_config/tool_selection`，新对话中保存即为默认）；会话级覆盖存于会话 `_meta.tool_selection`（携带 session_id 时写入，空 inputs 清除恢复跟随全局）。请求未携带 `tool_names`（null）时生成流程按「会话覆盖 → 全局默认」解析为本轮工具；显式 `[]` 仍为无工具模式。**内置工具（todo_write/ask_user）并入同一链路**：前端在工具模态框首位「内置工具」分组勾选，保存为伪服务键 `__builtin__`，生成时按名称识别注入，会话级/全局默认语义与 MCP 工具一致
- **配置热重载**：`util/config_watcher.py` 全局守护线程按 `CONFIG_HOT_RELOAD_INTERVAL_SECONDS`（默认 5s，<=0 禁用）轮询 setting/mcp_servers.json 与 setting/models.json，与内存 sha256 hash 比对；变化且解析成功才重载（解析失败保留内存现状并打印 `[config-watch]` 日志）。mcp_servers 仅 `servers` 键变化才重新探测 MCP 工具；models.json 变更重载 models_config/model_selection/setting_vars（不触碰 .env）
- **历史压缩参数**：`POST /chat_config/history_compaction` → 跨轮保留数/触发比例/每次压缩轮数/单轮压缩比例（ratio）与摘要预算比例/超大拒绝系数与上限；单轮阈值由聊天与压缩模型窗口及比例共同决定；压缩模型由 models/select（role=compaction_model）统一管理
- **回传长度**：`POST /chat_config/context_return` → 思考过程（reasoning_content）与历史工具结果的最大回传长度（0=不回传、负数=全部、正数=截断），写回 .env 即时生效
- **MCP 工具超时**：`GET/POST /chat_config/mcp_tools` → 配置单次 MCP 工具调用（连接/初始化/执行全过程）的超时秒数，写入 `MCP_TOOL_CALL_TIMEOUT_SECONDS`；正数超时后返回工具错误，0 表示不限制。工具执行在工作线程中，停止接口会取消后台生成任务并等待收尾

---


## 三、模块业务逻辑详解

### 1. 聊天客户端（`chat/chat_llm.py`）
- 纯标准库实现（socket + ssl），不依赖 openai SDK，支持 HTTP/1.1 chunked 传输解析（处理 UTF-8 被切断问题）
- `chat_completions()` 统一入口：`stream=True` 返回异步生成器，`stream=False` 返回完整字典（供历史压缩用）
- 模型配置热解析：每次请求从 models.json 解析当前选中模型，**连接失败自动无限重试**直到用户停止
- `reasoning_effort` 无条件随请求下发（由 `apply_role_parameter_defaults` 按模型参数桶填充）
- 统一输出 SSE 事件：content / reasoning_content / tool_calls / usage / finish_reason

### 2. 工具聊天工厂（`factory/chat_factory.py`）
- **后台任务 + SSE 重连**：生成循环运行在独立 `asyncio.Task`，事件写入有界环形缓冲（4000 条）；重连请求从当前轮起点回放（`replay` + `question_text`），已完成任务不回放（内容在 JSONL）；带新用户消息的请求平滑打断旧任务重启；`/chat_stream/status` 暴露运行状态
- **工具授权模型**：`tool_request.tools` 是服务端内部字段（exclude），由前端 `tool_names` + 后端实时工具生成，未授权工具调用会被拦截并返回 `blocked: true` 事件
- **SSE 事件归一化**：流式 `tool_calls` 按 index 增量合并，兼容老模型 `function_call`；透传前过滤 id/type 字段
- **usage 汇总**：`UsageAccumulator` 按 completion_id + 指纹去重，合并后挂到当前 chat_round
- **首调用预算检查**：请求发起前估算「消息 + 工具定义」token，超当前模型窗口时优先把文件记忆降级为 3000 字符摘要（发 warning 事件），仍超窗则发 error 并终止，避免上游 400/静默截断
- **内置工具**：由 `agent_runtime/builtin_tools.py` 本地执行（不经过 MCP），并入工具选择模态框首位的「内置工具」分组（伪服务 `__builtin__`，会话级/全局默认持久化）控制注入——`check_tool_exists` 查后端实时工具列表（选了外部工具时自动注入）；`todo_write` 模型自我规划（落盘 `_meta.todo`）；`ask_user` 向用户提问（前端弹卡片，回答作为下一条用户消息，当轮任务暂停）
   - **上下文压缩**：由 `agent_runtime/context_compaction.py` 统一负责。压缩模型调用支持**流式接口**：传入 `event_emitter` 时以 `stream=True` 请求，模型思考/正文增量经 `phase="delta"` 事件（`reasoning_content`/`content` 与聊天 SSE 同名字段）实时推 SSE，delta 帧不落盘；done 事件携带 `summary_text` 最终累计摘要全文并随同一 payload 落盘 JSONL，前端刷新后可在压缩块回放摘要。跨轮历史与单轮工具轨迹达到「`min(聊天窗口,压缩窗口)×ratio`」时，使用压缩模型输出普通文本摘要，并归并为一个累计摘要块；已完成原始轮次只存储在 JSONL，不再回传给模型，全部历史原始用户问题保留最近 ≤10k tokens（`context_summary.recent_questions`），渲染为独立 system 消息；单次超大工具结果也会在下一次模型调用前触发；原始结果仍写入 JSONL 与 SSE。
- **超大结果拒绝**：单次工具结果估算 token 超过 `min(聊天窗口,压缩窗口)×系数`（默认 1.5）时，结果不进入模型上下文，改写"输出过长，请重新考虑工具"反馈并让模型重新规划；JSONL 只落头尾节选预览（`result_preview`）。连续超长拒绝达到上限（默认 3）时终止任务并写入说明。
- **错误处理**：上游流错误/用户停止/异常分别记录不同状态，避免误记；服务端取消（CancelledError）尽力落盘后重抛

### 3. Agent 运行时工具组件（`factory/agent_runtime/`）
- 注册表：读 `setting/mcp_servers.json`，信号量并发探测（默认4并发、12s超时，可用 MCP_DISCOVERY_MAX_CONCURRENCY / MCP_DISCOVERY_TIMEOUT_SECONDS 配置），失败的服务器记录 metrics 不阻塞；schema 做白名单字段过滤防模型误用；返回的每个工具附带 `server_id`
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
  - 首行 `_meta` 元数据（title/user_questions/usage/context_summary/record_count/completion_count/upload_id 等），追加时实时重算；`_meta` 不再含 session_id（会话标识以文件名为准）
  - `ChatRoundStore` 将散消息按 `chat_round` 事件聚合：`{event, question, events[], status, usage_total, completion_count}`；status 含 done/error/stopped/interrupted
  - 单轮工具上下文压缩状态随轮次保存为可选 `compress_blocks`（累计摘要块带覆盖游标）/`compress_content`/`compress_index`；活动期间写入 `_meta._active_round_compaction` 检查点，历史重建时跳过游标之前已摘要的工具轨迹
  - 压缩 usage 分桶累计：单轮 `compress_usage`、跨轮 `_history_compress_usage`；压缩过程事件（start/done）以 `event="context_compaction"` 独立行落盘，与 SSE 推送同结构
  - 会话管理器注册表缓存 + 读写加锁（写用临时文件原子替换），保证线程安全；`run_task` 属性按会话控制对话启停
  - 支持按行删除/清空/下载、列表查询、标题更新（PUT /chat_history/title）、jsonl 导入（同名冲突自动追加时间戳另存）
- **FileMemoryManager**（`history_files/upload/<session>/<file>.json`）：每文件一 JSON，超限删最旧，支持文本摘要供 LLM 使用；上传目录名写入会话 `_meta.upload_id`，删除会话时连带清理
  - **chat_history_format.py**：摘要规范化/渲染（累计摘要与最近问题索引）、chat_round → 上下文消息；工具结果回传可配置（0=不回传、负数=全部、正数=截断前 N 字符）

### 6. 配置模型（`config.py`）
- `Message`：对话消息（role/content/tool_calls/tool_call_id/reasoning_content/refusal）
- `ChatLLMRequest`：聊天请求全参（超时/采样参数/工具选择/会话隔离字段）；`tools` 为服务端内部字段；支持 use_backend_history/backend_history_rounds
- `ChatModelSelection`：模型切换请求（provider/model/role/parameter，role ∈ chat_model/compaction_model/title_model）
- `HistoryCompactionConfig`：跨轮保留数、统一压缩触发比例、摘要预算比例（`summary_budget_ratio`）、超大结果拒绝系数与连续拒绝上限
- `ContextReturnConfig`：思考过程与历史工具结果的最大回传长度（0 不回传 / 负数全部 / 正数截断）
- 工作目录：`set_current_dir` / `apply_persisted_work_dir`（启动时从 DEFAULT_CHAT_WORK_DIR 恢复上次目录）

### 7. 环境与模型配置（`env_manager.py`）
- `.env` 支持 `${VAR}` 变量引用、`enc:dpapi:` Windows DPAPI 加密密钥解密
- `models.json`：支持顶层 provider + 嵌套子 provider 扫描，`variables`/`runtime` 作为全局配置变量
- **模型三角色选择**：顶层 `model_selection` 键（chat_model/compaction_model/title_model），每项 `{ownership_name, model_name, parameter, api_type}`；`parameter` 按 api_type 分桶存储（chat_completions/messages/responses），取参回退链：当前 api_type 桶 → chat_completions 桶 → 任意非空桶 → 空；旧版扁平格式加载时自动迁移
- 模型选择三件套：`list_available_models`（扁平列表给前端）/ `get_current_model_selection` / `select_chat_model`（写 models.json + 同步内存）；`model_selection` 未配置 chat_model 时回退 .env 的 CHAT_OWNERSHIP_NANE/CHAT_MODEL_NAME
- `apply_role_parameter_defaults`：请求体未显式提供的生成参数按选中模型的 parameter 自动填充
- `set_env_vars`：写回 .env 并保持内存 env_vars 一致（防 hot-reload 覆盖）

### 8. 系统 MCP 服务器（`mcp_server/sys_tools_server.py`）
| 工具 | 业务 |
|---|---|
| list_dir | 目录浏览（通配符过滤、递归深度、文件/目录过滤，JSON 输出） |
| read_file | 读取文件（行号显示、行范围、utf-8/gbk 自适应、2000 行截断保护） |
| write_file | 写入文件（整文件覆盖/追加，自动创建多级目录） |
| edit_file | 精确字符串替换编辑（0 处/多处歧义校验，自动适配 CRLF/LF） |
| search_files | 跨文件正则搜索（类似 grep，文件名通配符、忽略大小写、上下文行） |
| run_command | 终端命令执行（超时杀进程树、输出截断、gbk/utf-8 编码自适应） |
| fetch_url | 抓取网页/HTTP 接口（正文纯文本、按标签/正则抽取、超长截断） |
| web_search | 网页搜索（解析 Bing 网页版，返回标题/链接/摘要） |

另注册命名管道终端服务器 `PipeIpcMCP.exe`（工具：setup_pipe / run_pipe_command / read_pipe_history）。

可用 `python test/manual_sys_tools_check.py` 通过真实 MCP 客户端逐工具自检（临时沙箱，自动清理）。

### 9. H5 前端（`H5/`）
- 纯静态单页应用（无构建依赖，SCSS 可选编译），直接请求后端接口
- 功能：会话列表/搜索/新建、SSE 流式渲染（含 reasoning/工具调用/压缩事件）、历史回放、多模态附件（粘贴/上传图片音频视频与文档，气泡缩略图/首帧，点击模态框预览图片/播放音视频/PDF/文本）、主题切换、代码高亮（prism）、markdown 渲染
- 工具函数均有对应单元测试（`H5/test/`）

### 10. API 接口总览
> 完整出入参数说明见 `docs/api_docs.md`，在线文档见 `/docs`
