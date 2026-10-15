# Messages API 兼容实施方案

日期：2026-10-11。状态：首版及图片/PDF/文档输入扩展已实现；使用方法和实际边界见 [Messages API 使用说明](messages_api_usage.md)。以下保留实施前的分析，文件位置和待办描述是当时的设计依据，当前能力以使用说明为准。

本文中的 Messages API 指 Anthropic 风格的 `POST /v1/messages`，以及采用该请求/响应格式的兼容网关。依据是项目已有的 `apiType: "messages"` 配置。方案阶段只新增了本文；随后按用户确认实施首版，未修改用户的实际模型配置，未调用真实模型。

## 1. 建议方案与首版范围

建议在现有 ChatLLM 调用入口下增加协议适配层：根据有效模型配置的 apiType，选择 Chat Completions 或 Messages 的请求构造器、响应解析器。业务层继续使用当前的正文、工具调用、usage 和前端 SSE 契约。

Messages 的原生 assistant content blocks 另行保存，作为后续工具轮次回传的依据。不能只将 thinking 转成 reasoning_content：项目会裁剪 reasoning，且该字段无法保存签名、隐藏思考块和原始块顺序。

建议首版包含：

- 聊天文本、异步流式、同步非流式，以及现有同步 SSE 调用入口的兼容。
- 本地工具闭环，包括内置工具、MCP、多工具调用、工具失败结果和用户提问工具。
- 聊天、标题、压缩、子任务四角色，以及角色回退、父模型继承、会话覆盖。
- 完整流结束判定、取消、超时、错误分类、重试、usage 统计。
- 原生 thinking/signature/redacted_thinking 的解析与保存；显式思考配置按模型能力开放。
- 历史读取、检查点恢复、Todo 和上下文压缩在 Messages 下的合法性。

图片和 PDF 原生输入可以作为下一阶段。Responses API、Anthropic 服务器工具、Files API、云平台 IAM 接入不在本次范围。

“第一版不主动开启思考”只能减少参数面板工作，不能免除原生块处理：部分模型默认开启思考，且并不接受关闭配置。具体行为应按模型能力确定。[Thinking 官方说明](https://platform.claude.com/docs/en/build-with-claude/thinking)

## 2. 当前代码已经具备什么、缺少什么

以下表格是 2026-10-11 首次实施前的历史代码快照，行号与“当前实现”描述均不代表现行状态；实现已完成并扩展，现状以 [Messages API 使用说明](messages_api_usage.md) 和源码为准。

| 位置 | 当前实现 | 对接入的影响 |
| --- | --- | --- |
| `setting/models.example.json` | 已有 Messages provider 和 parameter.messages 示例 | 配置预留不等于运行时支持；现有示例参数也需要核对 |
| `env_manager.py:291` | get_model_config 合并 provider/model，模型字段优先 | 可作为最终模型配置的统一来源 |
| `env_manager.py:423,651,665` | 四角色、协议参数分桶、thinking 白名单、跨桶回退 | 可以复用存储，但发送参数要按协议重新筛选 |
| `chat/chat_llm.py:300` | _resolve_chat_config 明确拒绝非 chat-completions | 需要统一支持协议校验和分发 |
| `chat/chat_llm.py:333,361` | 固定补 /chat/completions，固定 OpenAI payload | 需要独立 endpoint/header/payload 构造器 |
| `chat/chat_llm.py:622,920,1228` | 同步 SSE、异步 SSE、非流式均自行完成 HTTP 与 Chat 响应解析 | 三条路径均需覆盖，不能只改异步聊天 |
| `config.py:72,99` | Message 没有原生协议数据；ChatLLMRequest 没有 thinking | 仅在字典里加字段会在请求模型构造时丢失 |
| `factory/agent_runtime/chat_runtime.py:181,204,494` | 剥离下划线字段、同 ID usage 只收首份、reasoning 裁剪 | 原生元数据、分段 usage 都不能直接套用 |
| `factory/chat_factory.py:1697,1942` | 累积正文/reasoning/tool_calls，重新组装 assistant | 需要保存原生消息，并确认工具调用已经完整提交 |
| `factory/agent_runtime/sub_agent.py:684,1258` | 子任务有自己的流累积和工具循环 | 必须同步接入，避免父模型可用而子任务失败 |
| `env_manager.py:801`、`routers/chat_config_router.py:1146` | 压缩/子任务限制为 Chat Completions | 完成能力接入后统一解除，不宜逐处硬编码 |
| `factory/agent_runtime/title_generator.py:205` | 标题模型运行时拒绝 Messages | 当前选择界面与实际调用不一致 |
| `factory/agent_runtime/context_compaction.py:432` | 压缩模型不可用时回退聊天模型 | 即使保留独立压缩模型限制，也会走 Messages 回退 |
| `factory/agent_runtime/sub_agent.py:554` | 未选独立子模型时继承父模型 | 父模型切 Messages 后，默认子任务也必须能调用 |
| `memory/chat_history_format.py:443` | 历史重建按正文/tool_calls 投影 | 新增原生字段后仍需改恢复路径 |
| `factory/agent_runtime/todo_runtime.py:29,88` | 拆出 Todo 调用、删除混合工具消息中的旧 Todo | 可能破坏原生消息和签名前缀 |
| `H5/js/app/model_panel.js:305` | Messages 仍提交 presence_penalty/reasoning_effort/enable_thinking | 要按协议显示控件和生成参数 |
| `util/media_limits.py:15,38` | 按 image_url/video_url/input_audio/file 计数与降级 | 媒体配额过滤应放在协议转换之前 |

目前模型调用使用标准库 socket/asyncio，没有依赖 OpenAI SDK。首版继续复用现有传输能力，避免同时更换网络客户端、重试策略和上游协议。将来可以独立评估 HTTP 客户端或官方 SDK。

## 3. 分层与调用入口

建议目录结构如下；这些是拟新增/调整的位置，不代表本次已经创建代码：

~~~text
chat/
  chat_llm.py                  兼容现有入口，分发协议，组织超时/取消/重试
  protocols/
    base.py                    协议接口、统一响应、后端元数据约定
    chat_completions.py        搬入现有协议构造和解析逻辑
    messages.py                Messages 转换与流状态机
  http_transport.py            按需提取现有 HTTP/分块传输/SSE frame 读取
~~~

~~~mermaid
flowchart TD
    A[聊天 / 标题 / 压缩 / 子任务] --> B[ChatLLM 兼容入口]
    B --> C[解析有效模型与协议]
    C --> D[Chat Completions 适配器]
    C --> E[Messages 适配器]
    D --> F[现有 HTTP 传输能力]
    E --> F
    F --> G[协议响应解析]
    G --> H[统一正文 / 工具 / usage / 完成事件]
    G --> I[后端原生消息与回放状态]
    H --> A
    A --> J[现有前端 SSE]
    I --> K[历史与检查点]
~~~

适配器负责 endpoint、协议头、参数白名单、messages/tools 转换、非流式响应、流状态与完成条件。传输层负责连接、字节、HTTP body 和完整 SSE frame；不要让它理解 tool_use 或 choices。

保留 `ChatLLM.chat_completions()` 等现有方法作为兼容外观，内部按 apiType 分发。可以增加名称更中立的统一入口，再逐步迁移调用点；不要求首版同步重命名全项目。

对外仍使用：

~~~text
id / content / reasoning_content / tool_calls / finish_reason / usage
error / retrying / discard_tool_calls / stream_truncated
data: [DONE]
~~~

对内新增原生消息与完成状态。主/子循环先消费这些状态，再向前端投影；signature、redacted data、请求快照不能通过现有“其他字段全部转发”路径发送到浏览器。

## 4. 配置、地址和参数

### 4.1 协议由有效模型决定

实施前方案记录：统一归一化 `chat-completions / chat_completions`，新增 `messages` 并保持未配置时的 Chat Completions 默认；当时 Responses 尚未实现，计划对未知协议与 Responses 报配置错误。Responses 后续已落地，当前角色支持三种协议；现状以 [Messages API 使用说明](messages_api_usage.md)、[Responses 使用说明](responses_api_usage.md) 与源码为准。

每次解析模型都重新推导协议，不能依赖保存过的 selection.api_type。当前 _backfill_selection_api_type 只补缺失值；会话覆盖也可能保留陈旧协议。models.json 改协议后，需要使参数桶和运行时一起更新。

list_available_models 当前用 provider 的协议和 URL，而实际调用允许 model 覆盖 provider。列表、参数面板和实际请求都应从同一份 resolved config 派生。

### 4.2 Endpoint 与鉴权

建议 Messages 地址规则：

| 配置 url | 实际地址 |
| --- | --- |
| `https://api.anthropic.com` | `https://api.anthropic.com/v1/messages` |
| `https://api.anthropic.com/v1` | `https://api.anthropic.com/v1/messages` |
| `https://gateway.example/zen/go/v1` | `https://gateway.example/zen/go/v1/messages` |
| 已以 `/messages` 结尾 | 原样使用 |

仅为官方域名的无路径根地址补 /v1；自定义前缀完整保留。地址带 query 时用 URL 组件处理并保留 query，不直接拼字符串。误填 /chat/completions 到 Messages 配置时明确报错。

基础头包括 JSON content-type 和 anthropic-version（初始基线用 2023-06-01）。当前官方支持 Bearer 鉴权，也保留 x-api-key；不能把“Messages 只接受 x-api-key”写进实现。[API overview](https://platform.claude.com/docs/en/api/overview)

建议默认沿用项目现有 Bearer，用现有角色 headers 支持网关和 x-api-key 覆盖。HTTP 头按大小写不敏感合并；显式指定一种鉴权头时移除自动生成的另一种，避免重复/冲突。Host、Content-Length、Connection 等传输头由传输层控制。密钥为空的本地网关按明确配置处理，不向官方地址发送 not-needed 占位值。

### 4.3 参数按协议构造

复用 parameter.messages，保留“显式请求参数 > 角色参数 > 协议默认”的优先级，以及“会话覆盖 > 全局默认”。

| 当前参数 | Messages 处理建议 |
| --- | --- |
| max_tokens | 输出预算；项目生成调用要求正数，并受模型声明上限约束 |
| temperature / top_p | 按模型能力与显式设置发送；不把 ChatLLMRequest 默认 0.7/1.0 自动带入 |
| presence_penalty | 非标准 Messages 参数，默认不发送 |
| reasoning_effort | 不自动透传；支持该能力时使用原生 output_config.effort |
| extra_body.enable_thinking | 不自动等价转换为 thinking |
| thinking | 显式增加请求字段/内部协议参数容器，防止 Pydantic 丢弃 |
| stop / stop_sequences | 有支持需求时显式映射到 stop_sequences |
| stream_options | Chat 专属，不能带入 Messages |
| extra_body | 保留网关扩展入口，但保护 model/messages/system/tools/stream 等结构字段 |

“没有主动设置采样参数”应该表示不发送，不能通过 Pydantic 默认值变成主动设置。可利用显式字段集合或内部协议参数对象保留此区别。output_config 等新字段还需通过 env_manager 白名单和角色参数合并链。

当前跨桶回退是“当前桶 → chat_completions → 任意非空桶”。建议 Messages 专属字段禁止跨桶继承；缺失 Messages 桶使用协议默认，公共字段如输出预算可单独继承。Chat 旧配置的行为保留；显式空 Messages 桶应能表达“使用协议默认”。

采样、manual/adaptive thinking、强制 tool_choice 都有模型差异。能力未知时默认省略 temperature/top_p/top_k；当前部分模型会拒绝项目的 temperature=0.7 或 top_p=1.0 设置。用户显式配置了不支持的参数时，应明确报错，不静默忽略。前端可显示协议基础控件和经声明支持的可选控件，不宜依据模型名包含 Claude 或 vendor 名猜能力。[Thinking 参数限制](https://platform.claude.com/docs/en/build-with-claude/thinking)、[Define tools](https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools)、[Extended thinking](https://platform.claude.com/docs/en/build-with-claude/extended-thinking)

## 5. 消息与工具转换

### 5.1 基础消息

按通用协议基线，将开头连续 system 消息按原序转换成顶层 system；保留用户和助手正文顺序。空 content 的工具助手消息只输出 tool_use blocks，不制造空 text。

中途 system 不能无声提升为全局 system。部分模型有 mid-conversation system 扩展，但应按能力单独接入；通用基线发现这类消息时明确解释不支持。现有运行时提示、摘要和引导消息需要分别核对其位置和来源。[Create a Message](https://platform.claude.com/docs/en/api/messages/create)、[Mid-conversation system messages](https://platform.claude.com/docs/en/build-with-claude/mid-conversation-system-messages)

序列化上游请求时使用字段白名单，不发送项目 quotes、session 元数据、工具内部标记；引用仍由现有 quote_format 转成模型可读文本。

### 5.2 工具定义与选择

工具定义从 `{type:"function", function:{name, description, parameters}}` 转成 `{name, description, input_schema}`。

| 内部 tool_choice | Messages 值 |
| --- | --- |
| auto | `{"type":"auto"}` |
| none | `{"type":"none"}` |
| required | `{"type":"any"}`，仅模型支持时 |
| 指定 function | `{"type":"tool","name":"..."}`，仅模型支持时 |

parallel_tool_calls=false 对应 tool_choice.disable_parallel_tool_use=true；true 时通常省略禁止字段。没有工具时不发送 tools/tool_choice。请求模型并行输出与后端实际工具并发执行是两个独立控制项。

tool_registry 已有工具名合法化和 MCP 原始名映射（tool_registry.py:56,420），优先复用。Messages 原生调用不使用“拼接工具名拆成多个工具、重写 ID”的启发式修复（tool_executor.py:77）；未知工具保留原 ID，返回错误结果。

### 5.3 工具消息闭环

例如内部历史：

~~~json
[
  {"role":"assistant","content":null,"tool_calls":[
    {"id":"toolu_a","type":"function","function":{"name":"read_file","arguments":"{\"path\":\"README.md\"}"}}
  ]},
  {"role":"tool","tool_call_id":"toolu_a","content":"文件内容"}
]
~~~

转换成：

~~~json
[
  {"role":"assistant","content":[
    {"type":"tool_use","id":"toolu_a","name":"read_file","input":{"path":"README.md"}}
  ]},
  {"role":"user","content":[
    {"type":"tool_result","tool_use_id":"toolu_a","content":"文件内容"}
  ]}
]
~~~

同一 assistant 的多个 tool_use，其全部结果汇总在紧随其后的同一 user 消息里；tool_result 在额外用户文本/媒体之前。不能在调用与结果之间插入引导消息。已有代码通常在工具批次后消费注入消息，这个顺序应继续保证。[Handle tool calls](https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls)

需要严格校验：原 ID、参数对象、唯一结果、所属调用、相邻位置与批次完整性。sanitize_tool_call_pairing 当前主要按 ID 判断存在性，不能代替 Messages 的序列校验。

工具执行失败、未知工具、权限拒绝和超时都应有匹配结果；is_error 从工具执行状态产生，不能仅凭正文含“error”判断。ask_user 的暂停/占位/恢复也要保持一一配对。

如果 assistant 原本来自 Messages，优先使用原生完整块，不通过正文字符串和 tool_calls 重建，以保留 text/thinking/tool_use 的原始交错顺序。

## 6. 流式解析、完成状态与统计

### 6.1 独立流状态机

传输层先按空行组装完整 SSE frame，支持 event 字段、多行 data、注释、CRLF 和跨 HTTP chunk 的 UTF-8 字节；当前逐 data 行 json.loads 的循环需要在这里调整。

Messages 适配器为每次 HTTP 尝试单独维护 message ID、content blocks、block index 到工具 index 的映射、参数缓冲、签名缓冲、usage、stop_reason、完成状态。

| 上游事件 | 处理 |
| --- | --- |
| message_start | 记录 ID/model/初始 usage |
| content_block_start | 按 index 建立 text/thinking/redacted/tool_use 状态 |
| text_delta | 输出现有 content 增量，并保存原生 text |
| thinking_delta | 输出可展示的 reasoning_content，另保存原生 thinking |
| signature_delta | 合并签名，仅保留在后端 |
| input_json_delta | 累积 partial_json；不能逐片段执行工具 |
| content_block_stop | 固化该块，工具参数做完整 JSON/object 校验 |
| message_delta | 记录 stop_reason；usage 按累计值覆盖合并 |
| message_stop | 确认响应完成、保存原生结果、提交完整 usage 和 finish_reason |
| ping / 注释 | 作为连接心跳处理，不产生正文或 usage |
| error | 进入错误分类，废弃本次尚未提交的工具调用 |
| 未知事件 | 容忍新增事件；若影响必需内容/完整性，则报不支持，不能执行不理解的工具块 |

block index 不能直接当工具数组下标：index=0 可能是 thinking，index=1 才是第一个工具。工具 ID/name 初始只发送一次，后续仅追加 arguments，避免现有 delta 合并器把工具名重复拼接。

官方流以 message_stop 收尾，不以 [DONE] 收尾；适配器在完成后生成项目内部的 [DONE]。message_stop 表示此次响应完整，不表示 Agent 已经完成任务。[Streaming](https://platform.claude.com/docs/en/build-with-claude/streaming)

### 6.2 工具调用提交必须与展示分开

当前主/子循环中，EOF 后如果已有 tool_calls，可能继续进入执行路径（chat_factory.py:1857；sub_agent.py:1278）。因此不能直接把 Messages 的参数片段送入现有执行累积器后认为适配完成。

建议保留实时工具预览，但增加后端 response_complete/tools_ready 状态：只有 message_stop 到达、所有工具块闭合、参数是完整对象、stop_reason=tool_use 时，才允许执行。截断、取消、超时、error 时清除 provisional calls。主/子循环共享这一判断。

如果先用更小的接入步骤，也可以在适配器里缓冲工具调用，完成后一次输出完整列表；但要同时补充预览/活动事件，让现有工具流无输出超时仍能监测生成阶段，不能因此丢掉超时保护。

message_delta 已有 stop_reason 但缺少 message_stop，仍按未确认完整处理，不能提前执行工具。max_tokens 截断到一半的 JSON 不能补成 {} 后执行。

response_complete 和 replay_eligible 分别判断：响应虽已完整收尾，但工具调用未达到 tools_ready 时，不能把含这些 tool_use 的 assistant 直接追加到后续回放链。即使单个工具块闭合，未提交的调用也只进入诊断/展示；续写从最后确认状态与可见正文提示构造，避免出现没有结果的悬空调用。

### 6.3 stop_reason 映射

| Messages stop_reason | 内部 finish_reason / 行为 |
| --- | --- |
| end_turn | stop；完成当前回答 |
| stop_sequence | stop；另保留原 stop_sequence |
| tool_use | tool_calls；通过完整性校验后执行 |
| max_tokens | length；续写前清除无效工具块 |
| model_context_window_exceeded | length + 上下文超限状态；压缩/缩减后再请求 |
| refusal | 终止当前回答并保留拒绝状态；不按网络错误重试 |
| pause_turn | 属于服务器工具续轮，首版不启用；遇到时明确报告未支持 |
| 未知值 | 保留原值并报告协议能力不足，不擅自当 stop |

不能将所有非 tool_use 都当正常完成。非流式响应也复用相同 block 解析、stop_reason 和工具完整性规则，不能只读取 content[0].text。[Stop reasons](https://platform.claude.com/docs/en/build-with-claude/handling-stop-reasons)

### 6.4 usage 只提交最终完整统计

当前 UsageAccumulator 对同一 ID 只收第一份；Messages 开始的 input usage 和尾部 output usage 必须先在适配器合并，再发布一份完整 usage。

建议归一化：

~~~text
prompt_tokens = input_tokens + cache_creation_input_tokens + cache_read_input_tokens
completion_tokens = output_tokens
total_tokens = prompt_tokens + completion_tokens
~~~

字段不存在时才按 0 处理，另保存原生 breakdown，嵌套缓存明细不能再加一遍。统计累计帧采用覆盖/合并，不累加各帧。[Prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)

发布顺序为原生完成状态 → 最终 usage → finish_reason → [DONE]。失败尝试如已有供应商 usage，可按该尝试 ID 提交一次，标明 partial；没有供应商统计时保持未知，不伪造为已计费 0。

## 7. 原生块、历史和上下文前缀

### 7.1 展示投影与原生回放分别保存

建议 Message 增加显式的后端 provider_state 字段，包含协议、来源配置标识、模型、响应 ID、完成状态和完整有序原生 content。字段名是设计建议，可在实现时调整。

该字段需要贯通 Message、build_request_messages、运行时列表、检查点和历史恢复。推荐完整原生数据保存在后端侧车，普通 assistant/子任务事件至多保存可解析的后端引用。不能仅使用 _messages_blocks：下划线字段在当前构造链被剥离。

Chat 请求序列化应排除 provider_state；Messages adapter 从中读取原生内容，但也不将该字段名原样发上游。客户端提交的同名数据不应直接成为可信回放状态，可信数据来自供应商响应和后端已保存的原生记录。

当前原始历史接口直接返回 JSONL（chat_router.py:296），导入也可能保留额外字段。因此完整签名/请求快照优先放后端侧车，普通历史、分享和导入使用展示投影；导入同名 provider_state 不能自动恢复可信回放。

普通 reasoning_content 仍供显示和现有 Chat 策略使用，绝不拿它配上 signature 伪造 thinking。旧历史没有原生数据时，继续使用普通文本/工具投影；不要从旧 reasoning 补造签名。

### 7.2 原样回传还不够，部分模型要求前缀稳定

工具续轮必须保留该响应中的完整 thinking、signature、redacted_thinking 及顺序。部分新模型还校验生成这些块时的 system、tools 和先前消息；重写历史或换工具定义可能使块失效。[Thinking](https://platform.claude.com/docs/en/build-with-claude/thinking)、[Preserved thinking](https://platform.claude.com/docs/en/build-with-claude/preserved-thinking)

这是本项目需要额外设计的地方：Todo 会删除/拆分工具消息，压缩会重建上下文，工具空转保护会改变 tools，媒体回收会替换先前内容。因此只新增 raw blocks 字段不足以保证所有支持签名绑定的模型可用。

建议 Messages 维护一份“原生回放上下文”：

1. 首次请求保存最终实际发送的 system/tools/messages，而非尚未整形的运行时列表。
2. 当前工具续轮只在尾部追加供应商原生 assistant、完整 tool_result user 消息和后续用户文本；之前发送的内容不再由 copy_for_request/Todo/媒体回收重写。
3. 在这段原生续轮中冻结 system 和工具 schema。Todo 空转保护改成工具执行策略或经模型支持的 tool_choice 约束；不要删除已发送的 schema。本地工具授权仍由当前执行规则检查。
4. 对已发工具结果长度和已发媒体使用稳定快照；后续历史返回长度设置不能再裁剪这份已发送内容。
5. 需要压缩、更换模型/账号、改变工具集合、编辑历史或恢复缺失快照时，先确认没有未结算的 tool_use，再根据已保存的任务事实、计划和工具结果创建新的上下文；不把旧签名块重新拼到改写过的前缀上。
6. 新上下文之后生成的原生块采用新的回放记录。旧块保留用于历史展示/审计，但不会在之后自动恢复插入。当前工具链无法安全重建时给出恢复限制，不能悄悄伪造 continuation。

重建时原子切换回放版本，同时重建/清空 TodoContext.exchanges 和 previous_ids。当前 TodoContext.sync 会自动插入缓存的旧 exchange（todo_runtime.py:60），不能让它把旧签名上下文重新带回。压缩产物在新版本中成为普通事实投影，之后的请求和预算使用新版本；旧 provider_state 不再经历史重建自动复活。

这是建议的客户端实现策略，并非要求首版引入 Anthropic 服务器压缩。对支持前缀绑定的模型，工具链中触发压缩时必须走上述安全重建路径；不能沿用原有“改写前缀、保留近期 thinking”的路径。

持久化可采用会话侧车或专门的原生回放记录，以避免在每个 assistant 事件重复保存整个前缀。文本与协议块保留原值；媒体通过不可变 blob/reference 和内容校验恢复相同字节，不在历史 JSONL 和日志反复存整包 base64。

TodoContext 在普通显示/压缩投影中仍可简化计划；原生回放中完整 exchange 保留全部工具及结果。创建新上下文后可以转为普通计划事实，但不能恢复一个拆出的 todo tool_use 并假定其原生 thinking 仍有效。

## 8. 重试、取消、预算和媒体

### 8.1 重试策略

复用现有用户停止、连接/读取/发送超时，以及可配置重试次数。首版针对 Messages 增加错误分类，避免把确定性请求错误纳入无限重试：

- 请求格式、无效参数、签名/配对错误：终止并说明原因。
- 鉴权/权限错误：终止，提示检查配置。
- 429、临时 5xx、连接错误：按现有次数与可用 Retry-After 重试。
- 媒体限额错误：只有确实改变请求后才重试。
- 上下文过大：进入压缩/预算处理，不重复发送同一超限请求。

没有可见输出时，可以重发同一有效请求。有正文后中断，使用最后已确认的回放状态和可见正文提示续写；不能把半个 thinking/signature 或半个 tool_use 当作有效原生消息。

部分正文可保留在展示历史；请求恢复状态与展示记录分开。已经记录的工具结果可在网络恢复时复用。取消或超时不提交未完成的模型工具调用。

工具自身已开始执行但结果尚未落盘，是另一种中断：当前并发执行在批次返回后再记录结果，进程退出时可能有动作已经生效、结果却未保存。建议按“回放版本 + response ID + tool_use ID”记录 planned/running/completed/unknown；完成结果复用，尚未开始的调用生成取消结果，执行状态未知时不自动重跑。若首版不增加此记录，应明确只能恢复已落盘结果，不能承诺执行中断后的工具恰好执行一次。

有效的 refusal、隐藏思考块或其他合法终止状态，不应仅因没有正文被当前“空输出重试”逻辑无限重试。

### 8.2 预算

现有输入估算针对 Chat 字段，Messages 需要覆盖顶层 system、工具 schema、原生 thinking、tool_result 内容和图片/PDF。不能将 base64/signature 按一般文本字符数直接估算；预算说明应区分模型可读文本、媒体估算和不透明签名数据。

max_tokens 包括该轮思考输出；发生预检降额时还要检查 manual thinking 的预算约束。模型声明 maxInputTokens/maxOutputTokens、供应商请求硬限制和应用安全余量分别处理，不把现有网关的 2^20 限额当作 Messages 协议标准。

现有 _precheck_hard_limit 估算没有顶层 system，转换前后都应明确估算口径，避免转换后漏掉系统提示。压缩阈值也需考虑原生回放实际保留的输入，而非仅统计已裁剪的展示投影。

### 8.3 多模态能力边界

| 内部部件 | 通用 Messages 基线 |
| --- | --- |
| text | text block |
| image_url 的 data URL | image.source.base64，分离 media_type 和纯 base64 |
| image_url 的 HTTPS URL | 按能力使用 image.source.url |
| file 的 PDF data URL | document.source.base64，media_type=application/pdf |
| DOCX/XLSX/PPTX 等 | 默认使用已有文本解析；不能仅换 type 就视为原生支持 |
| input_audio / video_url | 当前标准输入块未列出对应原生类型，据此首版不直接透传；转写/抽帧另做能力 |

上述图片/PDF 支持需要保留现有本地 media:// 解析和文件读取权限；协议适配层只转换准备好的输入。compatibility gateway 的 Office/音视频扩展必须显式配置并另测。[Vision](https://platform.claude.com/docs/en/build-with-claude/vision)、[PDF support](https://platform.claude.com/docs/en/build-with-claude/pdf-support)

vision=true 不代表支持所有媒体。支持文档类型应取“模型声明 ∩ 协议能力”；例如当前 Messages 示例声明 .docx，不足以证明官方 Messages 支持它。

媒体数量过滤和错误学习优先在内部部件转换前完成；缓存键增加规范化协议。未发送的附件给出明确占位/提示，不让模型把占位当作已经读取。原生回放中已发送的媒体不受之后投影回收影响。

## 9. 实施顺序与文件清单

推荐按依赖顺序开发；对用户开放 Messages 前，应完成前四步，不能把“聊天能输出一句话”当完整兼容。

| 步骤 | 内容 | 主要涉及文件 |
| --- | --- | --- |
| 1 | 协议接口、有效配置归一化、endpoint/headers、纯请求与响应转换 | chat/chat_llm.py、拟新增 chat/protocols/*、env_manager.py、config.py |
| 2 | 流 frame 与 Messages 状态机、usage、完成门槛、错误分类 | 调用/传输层、factory/agent_runtime/chat_runtime.py |
| 3 | 主/子循环、原生消息保存、回放上下文、Todo/压缩安全重建 | factory/chat_factory.py、sub_agent.py、todo_runtime.py、context_compaction.py、memory/* |
| 4 | 四角色校验与回退、参数面板/默认值、历史恢复与回归 | title_generator.py、routers/chat_config_router.py、H5/js/app/model_panel.js、H5/js/app/core.js、setting/models.example.json |
| 5 | 图片/PDF、按类型能力、媒体配额和预算 | util/media_limits.py、memory/file_memory.py、builtin_tools.py、请求适配器 |
| 6 | 文档和目标网关联调 | docs/api_docs.md、README.md、本方案、专门 mock/fixture 与后续实测 |

不要在首版同时实现 Responses、改写全部 Agent 框架或切换 SDK。Chat 适配器先按现有行为提取，再验证回归；业务状态中新增的原生能力明确限定到对应协议。

## 10. 验收与测试方案

先使用本地 mock HTTP/SSE 和固定 fixture 测试，不需要真实 API key。正式接入时再对用户目标网关做少量冒烟调用，网关的 API 兼容程度不由 apiType 字段保证。

| 验收项 | 必须覆盖的场景 |
| --- | --- |
| 配置 | model 覆盖 provider、协议别名、会话覆盖、目录改协议、空桶、不跨协议泄漏参数 |
| Header/URL | 根 URL、/v1、完整 endpoint、网关前缀/query、Bearer/x-api-key 覆盖、大小写去重 |
| 请求 | 初始 system、引用文本、原生块优先、空正文工具调用、无工具时省略控制字段 |
| 工具 | 多工具全部配对、失败/未知工具、原 ID、不拆拼接名字、ask_user 暂停恢复 |
| SSE | UTF-8/chunk 任意切分、多行 frame、交错 text/thinking/tools、signature delta、ping、未知事件 |
| 完成性 | EOF 在参数中途/stop_reason 后、取消、工具无输出超时、max_tokens 半截 JSON；均不执行未完成调用，不回放悬空 tool_use |
| usage | 开始输入+尾部输出合并、同 ID 一次统计、缓存计数、失败尝试统计 |
| 思考 | 原值与顺序、redacted/隐藏思考、签名不进入前端、请求不裁尾/不伪造 |
| 前缀 | 工具续轮只追加；Todo/tools/媒体/历史变化走安全重建；回放版本切换不插回旧 Todo exchange |
| 历史 | 新旧数据、检查点、重启、压缩、编辑重发、模型切换；展示与实际回放分别正确 |
| 四角色 | Messages 标题/压缩/子任务独立选择，压缩回退父模型，子任务继承父模型，父子不同协议 |
| 执行恢复 | 已记录工具结果复用；执行中断的 unknown 状态不自动重跑；导入记录不能伪造可信原生状态 |
| 原有功能 | 原 Chat payload/SSE/headers/工具执行/上下文预算保持既有行为 |
| 媒体阶段 | 图/PDF 转换、文档解析降级、音视频明确提示、配额在转换前生效 |

可复用已有测试：test_chat_llm、test_model_custom_headers、test_chat_config_router、test_session_model_selection、test_tool_call_pairing、test_stream_truncation_recovery、test_upstream_error_retry、test_tool_call_stream_timeout、test_reasoning_passback、test_context_compaction、test_sub_agent、test_title_generator、test_media_limits 和 test_native_document_support。

另建议新增 Messages 专用的 payload、stream 和原生回放测试，使协议差异可独立定位。断流测试应断言“执行工具次数为 0”，签名测试应断言“下一次实际发送的数据完整一致”，不能只检查解析器是否产出一个字典。

本文是实施前的设计记录。首版的验证范围、配置示例及未支持能力以 [使用说明](messages_api_usage.md) 为准。
