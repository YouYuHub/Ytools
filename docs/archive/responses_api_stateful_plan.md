# 有状态 Responses API 适配方案

2026-10-16 实现更新：现已增加可选无状态 Responses 兼容模式（`store: false`，每次发送有效本地上下文），用于不提供响应 ID 续接能力的模型。本文保留当初“仅有状态”的方案范围，以下“始终 store: true”等约束只描述有状态路径；当前行为以 [Responses 使用说明](../responses_api_usage.md) 和 [无状态排查](responses_stateless_2026-10-16.md) 为准。

日期：2026-10-13。状态：有状态 Responses 首版已实现，并已使用用户配置的 `gpt-6-luna` 完成真实调用验证。实际使用与兼容平台差异见 [Responses 使用说明](../responses_api_usage.md)。本文保留原方案的设计依据；“建议”段落不表示每个可选扩展都已实现。

实际实现（2026-10-14 更新）：三种调用入口共用协议工厂，Responses 传输已由 SDK 替换为 Python 标准库 HTTP/JSON/SSE，不再依赖 openai；远端请求始终 `store: true`。主聊天、子智能体、标题、可读摘要分别使用独立作用域。模型响应和请求以增量方式存入私有侧车，`_meta` 保存主链指针，轮次保存调用审计。前端继续接收应用 SSE；Responses 使用独立参数区域及亮浅粉色滑条。

真实验证的目标是 `OpenCode Responses / GPT 6 Luna`。其创建、续链、函数工具、图片与 PDF 工具结果、同步/异步流式均通过。该平台独立 `/responses/compact` 和查询 `/responses/{id}` 返回 404；压缩采用官方 `compaction_trigger` 控制输入继续有状态链，已验证压缩后仍能记住随机问题/工具代号。查询缺失时保留未确认记录，提供手动重新同步，不自动重复执行旧工具。

2026-10-15 任务内压缩修正：trigger 将未发送工具结果与控制项一并提交后，完整原生输出作为下一次生成的新有状态链输入，不再假定沿 trigger 响应 ID 续聊会裁剪祖先链。OpenCode 对照测试中，旧 ID 路径仍约 22.5k 输入 token，新 root 路径为 42 token。历史与本轮本地摘要共享原生压缩边界，旧版链自动保留压缩后新增记录并迁移；另按远端 usage 触发压缩。详见 [压缩后继续生成](responses_compaction_2026-10-15.md)。

本地上下文估算为界面显示依据；远端预算独立利用上一响应 usage 与新增输入估算，密文不按字符长度计 token，真实窗口限制仍由上游校验。本版未自动调用可选的 `/responses/input_tokens` 端点，也未提供官方 hosted tools 或独立 Conversations 对象。

范围：OpenAI 官方 Responses API 及实现其有状态能力的兼容平台。正常对话统一使用 `store: true` 和 `previous_response_id`；不增加 `store: false` 或每轮回传全量历史的运行模式。首次初始化、过期恢复、切换协议和重新建立响应链时，需要发送一次上下文，这是有状态模式的初始化步骤。

本文区分“官方已确认的接口行为”和“项目建议的实现策略”。接口核对日期为上面的日期；第三方平台的保存期限、压缩、查询恢复等能力仍须实际验证。

## 1. 建议采用的方案

在现有 ChatLLM 外观和协议分发下接入 Responses，增加独立的远端状态管理器：

1. 日常续聊使用最新有效的响应 ID，只发送尚未提交的新问题、新工具结果和新引导消息。
2. 本地 JSONL 继续完整保存用户、正文、可见推理摘要、工具调用和结果，作为历史展示、编辑、协议切换和恢复的依据。
3. 本地上下文面板、压缩摘要和其 token 估算以本地数据为准，遵循用户明确选择；远端状态另行保存，不要求两者逐条一致。
4. 项目触发压缩时，对同一逻辑历史边界同时执行本地摘要压缩和官方压缩，两份结果分别保存。官方结果按原生结构保留，不转换为本地摘要。
5. 官方独立压缩完成后，下一次生成用其完整 `output` 加新增输入创建新的 `store: true` 响应，此后继续用新响应 ID。
6. 提供“重新同步上下文”入口：准备一次上下文初始化，在下一次发送时建立新链；成功后恢复增量续聊。
7. 网络异常先确认远端响应的状态，再决定重试。工具执行与模型请求分别记录，避免重试重复修改文件或执行命令。

这是“本地历史与展示 + 远端生成上下文”两个视图。**本地显示变小，不等于远端请求上下文已经变小；本地压缩不能直接修改远端链。**

### 用户提出的问题对应的结论

| 问题 | 方案中的处理 |
| --- | --- |
| 用户问题不用每次回传吗？ | 已经包含在有效响应链里的问题不重复发送；当前新问题仍需发送一次。 |
| tools 每次要传吗？ | 工具定义按当前配置每次发送；新产生的工具结果发送一次；旧调用和旧结果不反复发送。 |
| 连续对话保存什么 ID？ | 保存每次模型请求的 `response.id`，会话维护最新可续接的 ID。一个 chat_round 可能对应多个响应 ID。 |
| 保存到 chat_round 还是 _meta？ | 两者都有用途：轮次事件保存每次调用的审计记录，`_meta` 保存当前活动指针；原生 output、恢复窗口和大数据放私有侧车。 |
| 会话超过 30 天怎么办？ | 从本地恢复数据重新建链一次，后续仍增量发送。不能假定每次生成都会延长所有祖先响应的保留期限。 |
| 官方压缩后保存什么？ | 保存整个压缩结果窗口和其中的加密 compaction item；不能只保存一段文本或只保存压缩资源 ID。 |
| 官方压缩和本地压缩同时做合理吗？ | 合理。共用逻辑边界，分开执行、保存和报告成功状态；它们服务于不同的上下文视图。 |
| 前端格式是否改变？ | 正文、工具卡、usage 等继续使用现有应用 SSE；增加状态/压缩进度字段，不把原生加密内容发到前端。 |

## 2. 官方规范核对

### 2.1 有状态的两种方式

官方提供响应链 `previous_response_id` 和独立 Conversations 对象两种持久状态方式；后者有独立 `conversation.id`。两种引用不能同时用于同一次创建请求。[Conversation state](https://developers.openai.com/api/docs/guides/conversation-state)、[Create response](https://developers.openai.com/api/reference/python/resources/responses/methods/create)

建议首版只实现响应链：

- 能自然对应本项目的每次模型调用、重跑和分支。
- 兼容平台往往先实现 `/responses`，不一定实现 `/conversations`。
- 远端状态失效后，有本地历史和原生快照可重新建立链。

Conversations 也是有状态方案，但本次不把它和响应链混成两个默认运行方式。若未来希望长期保留官方远端会话，可单独扩展；届时需要处理会话 item 的编辑、删除、并发写入及分支。

### 2.2 保留期限的准确含义

普通 Response 默认保存 30 天；数据控制说明对 `store=true` 的表述是至少 30 天，受账户的数据保留策略例外影响。Conversations 及其 items 没有这个 30 天 TTL，数据控制表列为直到删除。ZDR 会把 `store` 视为 false，因此不适合本次要求的持久响应链。[Data controls](https://developers.openai.com/api/docs/guides/your-data)

实现上：

- 30 天用作恢复风险提示依据，不写成“第 30 天必定删除”的精确倒计时。
- 记录链起点、最近响应和恢复检查点的创建时间。
- 不假定新响应会为整条祖先链续期；官方没有在上述文档中保证这种行为。
- 年龄提示是估算，实际可用性由请求或有需要时的查询结果决定。
- 本地保存是长期恢复依据；不能只留下一个远端 ID。

### 2.3 增量输入与每次请求的配置

`previous_response_id` 关联前文，但此前顶层 `instructions` 不自动沿用。新的用户输入、工具结果和当前工具定义按本次请求发送。响应链减少上传内容，并不表示历史输入 token 不计费。[Migration guide](https://developers.openai.com/api/docs/guides/migrate-to-responses)

将项目系统提示、可信运行时说明和显式配置的 instructions 组装为本次顶层 instructions。文件内容、工具返回值仍保留数据身份。后续不要把同一份系统提示再当成新增 input 消息持续追加。

### 2.4 官方压缩不是可读摘要

官方提供两类压缩：

- 创建请求的 `context_management` 配置可启用服务端自动压缩，仍按原响应链增量续接。
- `POST /responses/compact` 返回新的上下文窗口。该窗口可能同时包含保留的原始 items 和加密 compaction item，后续使用完整返回窗口。[Compaction guide](https://developers.openai.com/api/docs/guides/compaction)

独立压缩结果的 `object` 为 `response.compaction`，其 ID 是压缩资源标识；**不能把它直接当作下一次 previous_response_id**。它的 `output` 才是后续初始化输入。当前 compact 参数参考也列出了可选 `previous_response_id`，但本方案优先使用本地保存的有效原生窗口调用，以便失效恢复时也能工作。[Compact reference](https://developers.openai.com/api/reference/python/resources/responses/methods/compact)、[Official SDK continuation notes](https://developers.openai.com/api/reference/go)

另一个当前接口能力是 input 中的 `compaction_trigger`，规范要求它位于输入最后。可作为后续直接在有效链上手动压缩的优化入口，但必须验证目标模型、平台和 SDK 版本的行为；首版手动压缩采用上面返回窗口语义明确的独立 compact 接口。[Create response schema](https://developers.openai.com/api/reference/resources/responses/methods/create)

### 2.5 兼容平台不能仅凭接口名称判定能力

建议模型/平台能力中单独声明持久存储、previous_response_id、retrieve、compact、input_tokens、原生媒体和可见推理摘要。前两项是此模式的必需能力；其余分别决定自动恢复、双份压缩和预算校验的可用程度。

实施时用受控多轮测试验证上下文真的被保存，而不是只看请求成功：有些网关可能接受 store/previous_response_id 字段但未正确续接。未满足有状态能力时明确报不支持，不偷偷改成逐轮全量回传。官方 compact 不可用时，双份压缩入口必须显示相应限制。

## 3. 首次实施前的项目代码快照（历史）

> 以下对照记录属于 2026-10-13 开始实施前的历史状态；后续 Responses 实现已落地，表中的“现状/缺少什么”不可作为当前代码说明。现行行为以 [Responses 使用说明](../responses_api_usage.md) 与源码为准。

| 现有位置 | 当前行为 | Responses 需要处理的差异 |
| --- | --- | --- |
| `chat/protocols/__init__.py` | 只支持 chat_completions/messages | 增加 Responses 能力注册与统一分发。 |
| `chat/chat_llm.py` | 三种调用入口分别分发 Messages，其余走 Chat | 同步 SSE、异步 SSE、非流式都要覆盖，不能只接聊天流。 |
| `config.py` | 有 messages/provider_state，缺少会话链调用上下文 | 增加服务端内部的调用范围、输入游标和响应状态；防止 Pydantic 丢字段。 |
| `env_manager.py` | responses 参数桶、max_output_tokens/instructions 已预留；角色校验仍拒绝 Responses | 预留配置不等于可调用；还需完善参数白名单、隔离和能力声明。 |
| `factory/chat_factory.py` | 每次任务拼接本地历史，工具循环发送完整 messages | Responses 正常续接走新增输入队列，不能整包再次发送。 |
| `factory/agent_runtime/chat_runtime.py` | 请求副本会裁剪 reasoning，清理工具配对 | 原生 items 和远端链不能被这套文本整形规则修改。 |
| 同文件 `UsageAccumulator` | 使用响应 ID 和全局 usage 数值指纹去重 | 不同响应碰巧有相同 usage 时也应计费计数；改为作用域/响应 ID 内去重。 |
| `chat/protocols/native_store.py` | Messages 私有回放记录，用于原生签名工具续轮 | 可借鉴原子写与引用结构；不能拿 Messages 记录类型直接存 Responses。 |
| `memory/chat_round_store.py` | 一个用户轮次保存多事件，最后一次 done 收尾 | 每次模型完成都要记响应 ID，包括中间工具响应；不能只在轮次结束记一个 ID。 |
| `memory/chat_memory.py` | _meta、跨进程锁、侧车 pending checkpoint；本地上下文视图 | 保存活动链、同步边界和独立恢复记录，继续复用锁/检查点机制。 |
| `memory/chat_history_format.py` | 从正文/tool_calls 投影历史 | 该投影无法还原完整 output items，需独立原生恢复视图。 |
| `factory/agent_runtime/context_compaction.py` | 输出可读摘要并替换本地发送视图 | 本地摘要保留；新增双份压缩协调器，官方结果不进入文本摘要归一化。 |
| `factory/agent_runtime/todo_runtime.py` | 合并/删除旧计划工具交互 | Responses 原生账本不能做此改写；计划展示和新提醒另作投影。 |
| `factory/agent_runtime/sub_agent.py` | 子任务使用同一会话存储范围和独立循环 | 父任务与每个 agent_id 必须拥有独立远端链。 |
| `memory/chat_memory.py` 导入/导出 | ZIP 收集 JSONL 和 session_files，不自动包含协议侧车 | 分享与本机备份需区分，不能假定现有导出能备份原生恢复状态。 |
| `H5/js/app/model_panel.js` | Responses 只有部分 max_output_tokens 预留 | 增加独立控件组和状态操作；原生参数不可借用 Chat 默认值。 |

当前用户提出的“本地为准”适合继续沿用 `get_context_messages()` 和本地压缩摘要作为面板视图。Responses 的实际发送由独立状态管理器准备，不拿该面板视图逐轮覆盖远端历史。

## 4. 分层与工厂分发

建议新增/调整以下层次，名称是设计建议，不表示本次已创建这些代码文件：

~~~text
chat/
  chat_llm.py                       兼容现有三个入口
  responses_client.py               有状态请求、恢复、重试、查询与 compact 协调
  responses_transport.py            标准库 HTTP、JSON、SSE 与连接资源管理
  protocols/
    responses.py                   请求/工具转换、输出与事件状态机
    responses_content.py           图片、文件和工具多模态结果转换
    responses_store.py             原生 items、响应记录、恢复窗口
factory/agent_runtime/
  responses_context.py             输入队列、活动链、提交、恢复、分支
  responses_compaction.py          本地摘要 + 官方压缩协调
memory/
  chat_memory.py                   _meta 指针、轮次关联、历史变更通知
~~~

协议工厂按有效模型 `apiType` 选择适配器，至少统一 `create_stream/create_nonstream`、参数与能力、规范化输出。只有 Responses 暴露远端查询/压缩能力，业务层通过能力接口调用，避免继续散落 `is_messages` 二分判断。

~~~mermaid
flowchart TD
    A[聊天 / 标题 / 本地摘要 / 子任务] --> B[ChatLLM 协议工厂]
    B --> C[Chat Completions]
    B --> D[Messages]
    B --> E[Responses]
    E --> F[Responses 状态管理器]
    F --> G[新增输入或一次恢复窗口]
    G --> H[Responses 客户端]
    H --> I[统一正文 / 工具 / usage / 状态事件]
    H --> J[私有原生 output 与恢复记录]
    I --> K[本地历史和前端]
    J --> F
    L[同一历史边界的压缩任务] --> M[现有本地摘要压缩]
    L --> N[官方 compact]
    M --> K
    N --> J
~~~

### 客户端建议

最初方案采用官方 Python SDK；2026-10-14 按用户要求改为标准库 HTTP 客户端，移除 requirements 的 openai 依赖。同步/异步创建、retrieve、compact 使用相同 HTTP 规范，协议 items 和持久状态管理不依赖 SDK 类型。

恢复和重试统一由应用层管理，传输层不自动重试；显式处理连接/TLS、读取、发送超时，关闭流与连接资源。有 response.id 时先查询恢复，不能因为切换传输库就重复创建请求或执行工具。

这仍是工厂分发；前端和工具执行器使用统一应用事件，Responses 的标准库 HTTP 实现承担原先 SDK 的网络职责，状态管理、恢复和事件解析要求保持一致。

## 5. 正常调用生命周期

### 5.1 首次调用

新会话只发送当前问题；从其他协议切来的会话，先准备一次恢复上下文。模型名称用实际配置，不写死在适配器。

~~~json
{
  "model": "YOUR_MODEL_ID",
  "store": true,
  "stream": true,
  "instructions": "本次生效的系统提示和运行时说明",
  "input": [
    {"role": "user", "content": "请检查这个项目"}
  ],
  "tools": [
    {
      "type": "function",
      "name": "search_files",
      "description": "搜索文件",
      "parameters": {
        "type": "object",
        "properties": {"pattern": {"type": "string"}},
        "required": ["pattern"]
      },
      "strict": false
    }
  ]
}
~~~

示例为简化协议结构，不替换当前工具的真实完整 schema。已有大量可选参数工具，首版显式 `strict:false` 保持调用语义；需要 strict 的工具逐项调整 required、nullable 和 additionalProperties。Responses 的默认严格模式行为与 Chat 不同，不能无条件原样搬入。[Function calling](https://developers.openai.com/api/docs/guides/function-calling)

### 5.2 工具续轮

假设响应 A 返回 function_call，响应 ID 为 `resp_A`，工具的调用 ID 为 `call_search_1`。

~~~json
{
  "model": "YOUR_MODEL_ID",
  "store": true,
  "previous_response_id": "resp_A",
  "instructions": "本次生效的系统提示和运行时说明",
  "input": [
    {
      "type": "function_call_output",
      "call_id": "call_search_1",
      "output": "搜索结果..."
    }
  ],
  "tools": [
    {
      "type": "function",
      "name": "search_files",
      "parameters": {
        "type": "object",
        "properties": {"pattern": {"type": "string"}},
        "required": ["pattern"]
      },
      "strict": false
    }
  ]
}
~~~

这里不重复发送最初的用户问题和 A 的 assistant/function_call；它们已经在远端。B 完成后 head 更新为 `resp_B`；如果 B 又调用工具，下一次必须引用 B，而不是仍引用 A。

三个 ID 分开保存：

| ID | 用途 |
| --- | --- |
| `response.id` | 引用前一次响应、查询恢复。 |
| output item 的 `id` | 流事件定位和原生 item 保存。 |
| function_call 的 `call_id` | 回传工具结果，映射到内部 tool_call.id。 |

多工具调用保留全部 call_id。首版等同批工具结果齐备后一次提交；失败、超时、未知或被禁用工具也生成对应的结果说明。不能通过删除声明或漏传结果来“修复”远端已存在的调用。

工具结果有“原始结果”和“实际发送结果”两个口径。现有超长工具反馈、媒体降级等发送前处理继续保留，原始结果用于本地展示，原生输入账本保存实际发送的版本，恢复时不把原始大结果偷偷换回去。正文中的 tool_call 标记继续按现有规则反馈模型改用原生调用，不执行、不制造不存在的 call_id。

### 5.3 新问题与运行中引导

普通新问题使用最后一个有效 head，仅追加新用户消息。注入的引导消息也是新输入，有独立本地事件 ID。工具尚未完成时，先把当前工具结果配对完成，再追加引导。

取消尚未发送的引导可以从队列移除；已经提交到远端的内容不能只在本地取消，需要分支或重建。HTTP 首版不接入 WebSocket 的实时 steering 协议。

### 5.4 每次是否发送

| 内容 | 正常响应链续接 |
| --- | --- |
| 旧用户问题、旧正文、旧调用、已提交结果 | 不重复发送。 |
| 新问题、新工具结果、新引导 | 作为 input 新 items 发送一次。 |
| instructions | 按当前生效内容每次发送。 |
| tools、tool_choice、parallel_tool_calls | 按当前可用工具与策略每次发送。 |
| model、输出预算、reasoning/text 等参数 | 按当前配置和模型能力每次构造。 |
| 本地累计历史摘要、最近问题索引 | 正常续链不再次追加；恢复/切换时才按明确视图使用。 |
| 当前文件清单/计划提醒的变更 | 只发送新版本数据或明确的新提醒，不回写旧前缀。 |

不能简单比较 messages 数量来找增量：现有 Todo、媒体降级和本地压缩都会改变它。新增输入应有稳定事件 ID、类型、顺序、提交批次和状态。

同理，空收尾重试、Todo 未完成提醒和文本工具调用纠正都作为新的、可审计的输入，只排队一次；不能因为本地重建 messages 后再次发现相同条件，就重复追加已经提交的提醒。

## 6. 状态落盘：轮次、_meta 和侧车各存什么

### 6.1 _meta 只保存活动索引

以下是拟新增的应用内部结构，**不是 OpenAI 请求字段**：

~~~json
{
  "_meta": {
    "responses_state": {
      "version": 1,
      "mode": "previous_response_id",
      "scope": "chat_model",
      "chain_epoch": "local_chain_07",
      "backend_identity": "sha256:provider-account-project-fingerprint",
      "model_id": "YOUR_MODEL_ID",
      "context_revision": 4,
      "head_response_id": "resp_B",
      "head_record_id": "record_B",
      "head_created_at": 1791810000,
      "chain_started_at": 1791800000,
      "covered_input_seq": 18,
      "status": "ready",
      "pending_request_ref": null,
      "pending_compaction_ref": null,
      "recovery_checkpoint_ref": "checkpoint_03"
    }
  }
}
~~~

- `head_response_id` 是下一次续接的父响应，来源必须是成功提交的普通 Response。
- `chain_epoch` 是本地链代际；重新建立根响应时更换。
- `context_revision` 记录需要改变已有模型上下文的历史编辑/删除/切换，不因改标题、分组或普通追加而失效。
- `covered_input_seq` 是此调用范围内已确认提交的输入序列，不使用易变的 events 数组下标。
- `backend_identity` 绑定端点、账户/项目和鉴权范围。凭据只取不可逆指纹，原始密钥/签名请求头不进入历史。
- 模型绑定单独保存；首版模型变化保守重建，以免不同能力或更小窗口误用旧链。

恢复、压缩中、待建立新链和失效有明确状态，不能用 head 是否为空代替状态机。前端状态接口返回裁剪后的状态，不直接透传 _meta。

### 6.2 chat_round 保存每次模型调用

在 events 中新增内部 `model_response` 记录，关联该请求前后的 head、调用范围、请求序号和原生侧车引用。例如：

~~~json
{
  "event": "model_response",
  "protocol": "responses",
  "scope": "chat_model",
  "round_uid": "round_12",
  "request_uid": "request_12_02",
  "chain_epoch": "local_chain_07",
  "previous_response_id": "resp_A",
  "response_id": "resp_B",
  "status": "completed",
  "record_ref": "record_B",
  "input_seq_range": [17, 18],
  "committed": true
}
~~~

一个用户问题经历 A→工具→B→工具→C，三个响应都记录。标题请求、子任务或压缩辅助生成不能写到父聊天 head。

保留现有正文/工具事件，不把原生 output 展平后替换它们。历史解析器识别内部记录但不生成聊天气泡；JSONL 仍兼容读取没有这些字段的旧会话。

### 6.3 私有侧车保存原生数据

建议 `history_files/sidecars/responses/<session-hash>/` 下保存：

- 请求账本：request_uid、父响应、作用域、冻结输入、配置摘要、响应 ID、接收进度和状态。
- 响应记录：完整 output items、顺序、原生 status/error/incomplete_details、usage、created_at、request ID。
- 输入账本：实际已提交的 input 与新增输入队列；大型媒体用本地内容寻址引用去重，发送时还原。
- 工具执行账本：response_id + call_id、planned/running/completed/unknown、结果引用和提交状态。
- 恢复检查点：初始化窗口、独立 compact 完整 output，以及之后尚未纳入检查点的有序输入/输出。
- 压缩操作记录：冻结边界、两项子任务状态、分别的 usage、窗口/本地摘要引用和重试信息。

原生字段包括 reasoning、encrypted_content、message.phase、annotations 和未知但合法的返回字段；原样保存。只投影部分字段用于显示，不能将显示数据当作原生恢复数据。

Messages 的 source_key、protocol="messages"、工具签名回放规则不直接复用。可抽公共原子写工具，协议记录仍分开。

### 6.4 提交与崩溃恢复

建议提交顺序：

1. 发请求前保存请求意图、父 head、冻结输入和预期 context_revision。
2. 收到 response.created 时及时保存 response_id，作为查询恢复入口。
3. 接收完整响应并写入原生侧车；此时工具尚未执行。
4. 校验完成状态、调用结构和输入配对后，写轮次调用记录，原子推进 head 与 covered_input_seq。
5. 工具执行前记 running，执行后先保存结果，再排入下一次 input。

通过现有跨进程锁、活动任务租约及 revision/head 的比较更新阻止旧请求覆盖新分支。侧车、JSONL、_meta 跨文件不是天然事务，需要可重放的提交意图和提交标记；启动时对账，而不是假定几个 os.replace 一起成功。

普通 `response.completed` 只表示一次模型调用完成，不等于整个工具任务完成。带工具调用的 completed 响应可成为 head；任务仍等待执行和提交结果。

## 7. 双份压缩：本地为展示依据，官方为远端续接依据

### 7.1 是否合理

合理，也符合本项目需要支持多协议切换的实际情况：

- 本地摘要保留可读内容、原始轮次、工具事实和压缩覆盖范围，供前端、搜索历史、跨协议和人工恢复使用。
- 官方窗口保留模型原生状态，供 Responses 后续生成使用。
- 两者内容和 token 规模可以不同。面板用本地数据，usage 用上游真实消耗，不能混为同一个统计。

代价是两次压缩计算、可能不同的完成时间和两套恢复状态。只在手动请求或实际达到压缩条件时执行，不为了保持两边相同而每轮调用两个模型。

**“本地保留数据”建议指继续保留完整原始 JSONL，以及新的本地摘要；不是把原始历史从磁盘删除。官方压缩也不改写本地原文。**

### 7.2 默认执行流程

建议把目前项目触发的历史压缩、手动压缩和单轮工具轨迹压缩接入同一个协调器：

1. 在模型调用之间进入安全边界。待执行/待配对工具处理完，长时间等待中的 ask_user 不强行压缩。
2. 冻结 `context_revision + head_response_id + covered_input_seq + pending_input_ids`，生成 compaction_op_id。
3. 本地分支使用现有本地摘要及相应原始事件；官方分支使用同一逻辑边界上的有效 Responses 原生窗口。
4. 并行生成本地摘要、调用官方 compact。两份输入格式不同，但不能一份包含后来注入的问题而另一份没有。
5. 分别原子保存结果，确认历史边界未变后更新各自状态。
6. 本地更新 context_summary/单轮摘要和压缩事件；官方保存完整 compact.output，设置 `ready_to_rebase`。
7. 下一次 Responses 生成携带该窗口和新增问题/工具结果，不带旧 previous_response_id；成功后启用新 head、新 epoch。

长网络操作期间不一直持有文件写锁。通过任务状态/租约阻止同一链继续生成，提交时短暂加锁并核对冻结边界；历史若已被编辑，旧结果作为审计保留但不激活。

生成期间到达的引导消息排在冻结边界之后。不能为了压缩“消化”它，也不能同时写入两次。

冻结窗口中尚未提交到远端的完整工具结果要纳入本次 native compact 输入，并记录它们已经由待使用窗口覆盖。下一次 root 只追加窗口边界之后的输入，不能再将这些工具结果发送第二遍；待 root 成功后才整体确认输入游标。

### 7.3 原生窗口从哪里来

独立 compact 需要模型可理解的完整有效窗口，不能从前端泡泡拼装：

- 初始窗口 + 此后实际提交的 inputs + 完整模型 output，保持顺序和调用配对。
- 有独立压缩检查点后，使用它的完整 output + 此后尾部，不重新追加已经被它覆盖的旧 items。
- 若使用了服务端自动压缩，保存原生 compaction item 和覆盖边界，依据该模式的官方规则准备恢复窗口；不能套用独立 compact 的输出裁剪规则。
- 网络恢复存在歧义、原生记录缺失或工具状态 unknown 时，先对账，再做压缩。

当前 compact 参考支持引用 previous_response_id；可在目标平台验证后用 head 优化上传量。无论采用哪种输入方式，都保存此次返回的完整 output。首版基础流程不依赖旧远端 ID 才能压缩。

不能把 `context_summary` 文本当作“官方完整历史”交给 compact，同时声称保留了原有原生推理状态。如果原生窗口丢失，只能做明确标注的本地事实恢复。

### 7.4 下一次生成的准确结构

下面是伪代码，用于说明顺序，不是本次新增的实现：

~~~python
compacted = await responses_client.compact(
    model=effective_responses_model,
    input=frozen_native_window,
    instructions=current_instructions,
)
save_complete_compaction_output(compacted.output)

# 下一次真实生成。这个 input 的历史窗口只在新链初始化时发送一次。
response = await responses_client.create(
    model=effective_responses_model,
    store=True,
    instructions=current_instructions,
    input=[*compacted.output, *new_input_items],
    tools=current_tools,
)
commit_new_chain_head(response.id)

# 后续恢复为 previous_response_id + 新 input items。
~~~

注意：

- 不传 `previous_response_id=compacted.id`。
- 不带旧链 head，再把完整 compact.output 追加到同一次请求。
- 不只取 encrypted_content；不解析或裁剪它；不把它写入 summary_text。
- compact 返回窗口中保留了哪些用户/助手 items，就完整保存哪些；不用本地轮数策略再次修剪。
- 不用一个“请确认已接收历史”的额外生成调用只换一个新 ID。正常下一次问题/续写即可完成初始化。
- 没有新的用户问题时，手动准备的压缩窗口保持待使用；若任务进行中被压缩打断，窗口本身可以作为下一次生成的输入。trigger 也使用完整原生输出建立新有状态链，避免兼容平台继续携带旧祖先链。

### 7.5 两边成功/失败的处理

| 本地摘要 | 官方压缩 | 保存与后续行为 |
| --- | --- | --- |
| 成功 | 成功 | 保存两份结果；本地面板显示新摘要；下一次用官方窗口建立新链。 |
| 成功 | 失败 | 保留本地摘要；标记官方未完成，可只重试官方分支；旧远端链仍按旧上下文运行，不能宣称远端已缩小。 |
| 失败 | 成功 | 保留官方窗口；本地继续显示最后有效视图并提示部分完成，可只补本地摘要。官方窗口可在下一次生成使用。 |
| 失败 | 失败 | 保留原来的两套有效状态，记录失败，不把半成品当作压缩结果。 |
| 历史边界改变 | 任意 | 已有结果不激活，按新边界重新准备；不让旧摘要或旧窗口覆盖新历史。 |

压缩子任务的成功结果不因另一边失败而丢弃。操作整体显示“完成/部分完成/失败”，每项有独立重试预算与结果引用。

若官方压缩失败但原远端窗口仍能容纳下一次请求，允许按明确旧状态继续；若已经无法容纳，停止生成并要求完成官方压缩或选择本地上下文重建。不能用本地面板 token 较小绕过远端超限。

### 7.6 自动压缩与独立压缩不要争抢

按用户最新选择，**首版默认以本地压缩触发点协调本地摘要 + 独立 compact**，让前端可读结果和官方结果有同一操作记录。

服务端 `context_management` 自动压缩作为可选能力：

- 若启用，收到原生 compaction 完成事件后在安全边界补本地摘要，避免同时再调用独立 compact。
- 原链继续使用普通 response.id，无需每次自动压缩都人为重建。
- 它是远端额外保护，不允许因为本地显示未超限就关闭真实超限检查。

同一调用范围同一时间只有一项压缩协调操作。对暂不支持 compact 的兼容平台，可以“本地摘要 + 一次本地上下文重建”继续有状态运行，但要说明远端采用的是本地事实上下文，并未完成官方压缩；默认不静默这样降级。

### 7.7 压缩模型和参数的边界

本地摘要继续使用 compaction_model；该角色如果选择 Responses，也是独立作用域的 `store:true` 生成，不使用父聊天 head。

官方 compact 建议使用当前 Responses 聊天模型及其端点。不要让独立选择的 Anthropic 压缩模型接收 OpenAI 的加密窗口，也不能假定任意更便宜模型都兼容原生压缩状态。

本地 target_tokens、summary_budget_ratio 只控制本地摘要。官方 compact 没有本项目这些相同语义的字段，不承诺两边压成相同长度。

要提前压缩；官方 compact 的输入也必须符合其模型窗口。已经超大时，先使用最近有效原生检查点和尾部；实在无法装入则走明确的本地事实重建，不随意切碎、截断加密内容。

## 8. 过期、失效及手动重新同步

### 8.1 手动入口建议

提供“重新同步上下文”，适用于长时间未使用、远端 ID 失效、换平台或希望以本地显示视图重新开始：

| 选项 | 来源与含义 |
| --- | --- |
| 当前本地上下文（推荐默认） | 使用面板对应的本地摘要、问题索引及保留事件建立新链，符合用户选择的“本地为准”；不包含显示视图之外的所有原文。 |
| 当前原生恢复窗口 | 同平台且原生记录有效时，优先保留官方 compaction/推理状态；内容可能与本地面板不同。 |
| 完整原始历史 | 按 JSONL 的当前有效历史边界重建；发送前核对预算，太大则提示先压缩；不承诺超大历史能够一次装入。 |

按钮在空闲时准备同步快照，状态为“待下次发送同步”。下一次用户消息/明确续写与快照一起创建 root Response；完成后提示同步成功。一次重建后又回到增量发送，不增加持续全量回传模式。

从本地文本恢复时，不把可见 reasoning_content 伪造成 OpenAI 原生 reasoning item；工具历史保持完整配对，或在明确的事实视图中转为普通文本叙述。不要制造没有真实调用的 function_call_output。

### 8.2 自动恢复策略

普通续接被明确拒绝为“指定父响应不存在/不可访问”时，可以：

1. 保留失败请求和原 head 信息，区分响应 ID 失效与端点 404、鉴权错误。
2. 同一平台且恢复记录完整，优先使用有效原生窗口 + 尚未提交输入重建一次。
3. 原生窗口不可用时，提示可从当前本地上下文恢复；按该会话明确选择的恢复策略处理。
4. 重建成功才更换 epoch/head；仍失败则停止，不无限循环“过期→重建”。

用户明确点击本地同步、切换协议时，用选定的本地视图。自动失效恢复默认优先原生保真；前端展示依据仍是本地。这两者不用强行等同。

不能把所有 HTTP 400/404 都当成过期。400 可能是参数、工具配对或窗口问题；404 也可能是网关地址错误。

### 8.3 长会话的维护策略

建议只在发送或用户打开状态详情时检查年龄/风险，不建立全局持续轮询。

可将链龄达到例如 20 天设置为重建提示或可选维护策略，作为项目的保守值，不是官方强制期限；时间判断应看链的依赖起点，不能只看最新 head。

官方压缩不会被当成祖先链永久续期保证。独立 compact 窗口初始化的新 root 才明确建立新代际，仍需继续保留本地恢复材料。

密钥轮换后属于同一账户/项目时，官方资源可能仍可访问；首版凭据指纹变化先暂停复用并核对，不假定“换 key 必过期”或“任意 key 都能访问”。

## 9. 本地/远端上下文不同：前端显示与预算

### 9.1 以本地为准的范围

按用户明确要求：

- 上下文明细、可读摘要、问题索引和 token 估算继续展示本地视图。
- 不拉取远端完整历史覆盖本地，也不为了显示一致重复上传本地摘要。
- 主界面可继续显示本地 token 比例；详情标注“本地估算”，不能改叫“本次远端实际输入”。
- 独立显示远端状态：正常续接、等待恢复、已压缩待建链、部分完成、失效。
- 双份压缩的本地摘要可以查看，官方结果只显示状态、时间和可获得的统计，不显示密文。

### 9.2 实际请求仍需远端预算管理

本地面板为准是 UI 约定，不是远端窗口限制的替代。

有状态请求的 HTTP input 很小，不代表模型实际 context 很小。当前 `_precheck_hard_limit` 和本地字符估算不能只数新增 input 就通过预检，也不能拿加密文本长度当作其模型 token 数。

历史工具结果长度、reasoning 回传长度等本地设置也不会改写已经存入远端的内容；在 Responses 下分别作用于本地投影/恢复视图或第一次提交前的处理。希望远端按照新的保留策略重新开始，应使用重新同步，而不是宣称本地裁剪立即作用于远端。

建议：

1. 常规使用最近成功响应的 usage、原生窗口和新增输入做保守预算；压缩后缺少精确值时标记不确定。
2. 接近预算边界、准备恢复/切换到小窗口模型时，可按能力调用官方 `/responses/input_tokens`，带相同 previous_response_id、instructions、input 和 tools。
3. 该计数接口不是每次刷新前端都调用；不支持的网关降级为明确估算，不能显示为精确。
4. 默认 truncation=disabled，遇到超限执行压缩/重建，不静默抛弃最早事实。

官方计数接口支持输入与状态引用；output_tokens 包括不可见输出和推理，不仅是正文字符。[Counting tokens](https://developers.openai.com/api/docs/guides/token-counting)

双份压缩分别统计 usage。本地摘要调用、独立 compact 调用各记一次；不能把压缩 usage 又从返回窗口计入普通模型调用，或者把下一次新 root 的 input_tokens 再当成压缩费用。

## 10. 流事件、完成判定和前端兼容

Responses 是有类型的 items/events，不是 choices[0].delta。建议状态机至少处理以下映射；具体事件字段按官方 schema 实现。[Streaming guide](https://developers.openai.com/api/docs/guides/streaming-responses)

| 上游事件/数据 | 应用处理 |
| --- | --- |
| response.created / in_progress | 保存响应 ID 和内部状态，不创建额外正文/思考框。 |
| output_item.added / done | 按 output_index + item_id 管理原生 item，done 保存完整最终结构。 |
| output_text.delta | 映射 content 增量。 |
| refusal.delta / 拒绝内容 | 保留并展示合法拒绝答复，不误判为空输出后无限重试。 |
| reasoning_summary_text.delta | 映射可见推理摘要增量，保留同一 item/summary 的边界。 |
| function_call_arguments.delta | 显示工具预览，不能执行。 |
| function_call_arguments.done | 用完整 arguments 校验；不重复追加 done 中的全量内容。 |
| response.completed | 校验与持久化后提交工具和 head，最终发送规范化 usage。 |
| response.incomplete | 保存 incomplete_details；按原因展示截断/未完成，不能无条件当作成功。 |
| response.failed / error | 映射已有错误/重试字段，进入状态恢复流程。 |
| compaction item / compaction.compacting | 保存原生窗口、投影压缩状态，不解释为推理内容。 |

加密 reasoning 和 compaction 必须使用完成后的 item；added 中的内容可能尚不完整。官方 compaction 进度事件不含摘要正文，不能据此生成本地 summary_text。[Streaming events reference](https://developers.openai.com/api/reference/resources/responses/streaming-events)

前端继续消费：

~~~text
id / content / reasoning_content / tool_calls / finish_reason / usage
error / retrying / discard_tool_calls / stream_truncated / response_complete
tool_start / tool_return / sub_agent / context_compaction
data: [DONE]
~~~

finish_reason 是适配层的应用投影：完整工具响应→tool_calls，完整非工具响应→stop，输出预算未完成→length，并额外保留明确的 response_status/incomplete_reason。应用不能因上游没有原生 finish_reason 就认为流被截断。

首版只自动提交经过校验的 completed 响应；failed/incomplete/cancelled 保存在请求记录，不自动覆盖有效 head，也不执行其中的预览工具。需要续写时对账后明确创建后续请求；无法恢复的 partial 内容以新分支的说明/可读片段保留，不能把它冒充完整原生 item。持久模式收到缺少有效 response.id 的“成功”结果也按协议错误处理。

要防止已经遇到过的重复思考问题：

- 空 usage、created、item 生命周期、密文、压缩进度都不关闭再创建思考气泡。
- delta 已显示的正文/摘要，不从 item.done 或 completed 再完整追加。
- 查询恢复按 item/内容偏移对账，并以替换/补尾方式恢复，不能全量追加到已有泡泡。
- output 中可能有多个 assistant messages 及 phase；原生存储保留边界。只有明确结束输出/工具转换才收束对应展示块。

推理模型通常只提供可见摘要，不提供原始隐含思考；面板称“推理摘要”更准确。不请求摘要时没有此框是合法结果。[Reasoning guide](https://developers.openai.com/api/docs/guides/reasoning)

## 11. 网络失败、重试、取消和工具副作用

这是首版必须完成的能力，不能以后补。

| 异常发生位置 | 建议行为 |
| --- | --- |
| 本地构造失败、请求尚未发出 | 修正/报错；允许按原意图重试，不推进游标。 |
| 连接/发送失败，不能确认远端接受 | 保存 unknown；只从已确认父 head 进行有界重试，可能产生孤立响应，不假装幂等。 |
| 已有 response_id，但流断开 | 优先 retrieve 该响应，确认 completed/failed/incomplete/运行中状态，再继续。 |
| HTTP 429/5xx、可重试错误 | 保留当前网络重试预算与退避；已知 ID 先对账，避免传输层与应用双重重试。 |
| 401/403 或确定的参数错误 | 直接报告，不套用过期历史重建。 |
| 工具参数未完整输出 | 丢弃预览调用，不执行，不推进成已提交工具。 |
| 工具已经完成，提交结果时断网 | 从执行账本复用结果；查询对应生成请求，确认结果是否已经被接受。 |
| 执行命令/写文件中崩溃 | 标记执行结果 unknown；核对实际文件/任务状态后恢复，不能自动再次执行。 |
| 用户停止 | 停止本地流和待执行工具，保留最后有效 head 与 pending 状态，禁止迟到结果推进 head。 |

retrieve 获取到 completed 响应后，从完整 output 恢复并只执行尚未执行的合法工具。运行中只做有限等待，不承诺 foreground 流断开一定能恢复完整事件流。[Retrieve response](https://developers.openai.com/api/reference/python/resources/responses/methods/retrieve)

现有“把部分输出拼进 messages，然后让模型继续”的重试方式不能直接用于 Responses：部分内容可能已经存在于远端。应优先查询；确实不能恢复时，从最后确认边界建立明确的新分支，保留 partial 标记，不偷偷重复累加。

工具去重键建议是 `scope + chain_epoch + response_id + call_id`，不能只凭参数相同就去重：模型可能在后续合法调用中要求再次执行相同命令。

崩溃下跨远端模型、文件系统和任意工具实现严格 exactly-once 不现实。目标是有账本、避免已知重复、未知副作用不自动重做。客户端请求标识和 metadata 用于关联，不当作官方创建请求的幂等保证。

Background 是独立能力，不等于有状态。首版默认 HTTP 前台 SSE；只在实际启用并受支持时才调用 cancel 或使用 starting_after/sequence_number 恢复。官方可取消/恢复后台响应，不能把这个保证直接套到全部前台流。[Background mode](https://developers.openai.com/api/docs/guides/background)

## 12. 历史编辑、切换接口和调用范围隔离

### 12.1 编辑、插入与删除

- target_round 重跑只使用目标轮之前的有效前缀；不能引用包含旧回答/后续轮的当前 head。
- insert_round 只使用插入点前的状态；插入完成后后续远端链失效。
- 删除旧轮改变历史事实，不能保留依赖该事实的加密摘要/远端 head。
- 可利用记录过的前缀响应做分支；前提是其内容边界、原生状态和远端可用性可验证。否则从该前缀的本地历史重建。
- 本地累计摘要若包含被删除/改写的事实，也必须失效或从有效原文重算；不能只调整轮号而把旧事实带入新链。
- 被修改边界之后的恢复检查点、compaction 窗口和待提交请求一同标为失效。

不能通过给当前链追加“忽略以前第 N 轮”达到真正的历史删除语义。

### 12.2 切换协议/模型/平台

| 切换 | 处理 |
| --- | --- |
| Chat/Messages → Responses | 用当前本地上下文一次初始化；可选完整原文，不带其他协议原生思考块。 |
| Responses → Chat/Messages | 用本地摘要和有效历史构建原协议请求；不发送 OpenAI 密文/远端 ID。 |
| Responses → 另一个平台/账户项目 | 新链；不能携带上一平台的 IDs 或假定密文可移植。 |
| Responses 同平台换模型 | 首版保守重建并验证新窗口/能力；将来可对明确兼容组合优化。 |
| 切走 Responses 后又切回 | 默认从当前本地事实重建；若中间产生了新内容，旧 head 不能继续代表完整会话。 |
| 只调整采样/输出预算/工具选择 | 更新下次配置，不自动重建所有旧历史；仍校验参数和窗口。 |
| 更改历史可见事实、移除已提交媒体 | 按上下文变化重建，不能只改变本地显示并声称远端忘记了数据。 |

官方加密内容的跨模型、账户和第三方平台可移植性不作未经验证的保证。

### 12.3 四个角色与子任务

- chat_model：每个本地会话的主链。
- sub_agent_model：每个 agent_id 独立链；并发子任务不会抢写父 head，也不共用同一个子链。
- title_model：每个标题任务独立 store=true 调用，不把标题问题带进聊天链。
- compaction_model：本地可读摘要生成的辅助链，与官方 compact 结果分开。

独立辅助生成即便只有一次调用也设置 store=true；不开放另一个无状态客户端模式。辅助作用域的 head/记录不覆盖主聊天 _meta 指针，运行完可按本地保留策略清理本地索引。

主进程、会话 worker、标题后台任务及手动压缩路由都带显式 scope；不能仅用 session_id 从共享全局变量推导 head。

## 13. 图片、文件、read_media/read_document 与工具能力

Responses 原生内容单独转换：

| 当前内部数据 | Responses 输入 |
| --- | --- |
| 文本 | input_text。 |
| 图片 image_url/data URL/本地 media:// | 解析后 input_image，可按能力使用 image_url 或 file_id。 |
| 支持的文件 | input_file，按实际输入方式使用 file_data、file_id 或 file_url。 |
| 工具文本结果 | function_call_output.output 字符串。 |
| 工具图片/文件结果 | 支持时使用 function_call_output.output 的内容数组；图片/文档读取工具也走这条路径。 |
| 不支持的音频/视频等类型 | 明确按能力转换/降级，不假定 Chat 的部件名可以原样使用。 |

官方当前 Responses 文件输入已覆盖 PDF、文本代码、部分 Office 文档及表格；不同类型的解析方式不同，非 PDF 文档通常提取文本，不保证包含嵌入图片。仍按项目 supportDocTypes、模型和平台能力控制。[File inputs](https://developers.openai.com/api/docs/guides/file-inputs)、[Images and vision](https://developers.openai.com/api/docs/guides/images-vision)

用户附带的图片/文件在其第一次相关输入时发送。正常工具续轮不再重新注入相同附件；read_media/read_document 明确读取同一文件时可以返回新结果，按新 call_id 发送。新上传、修改版本用新的内容引用。

恢复时需要可用的媒体原件或上传映射。file_id 属于对应账户/项目，文件删除/到期、临时 file_url 失效都可能导致恢复失败；保留本地原件供重新上传或编码，不能只保存远端 file_id。

不把 Responses 的历史媒体“本地已转占位”误当成远端已经删除图片。正常链可能继续携带原来的媒体语义；用户希望更换这种上下文策略时需要重建。

继续使用项目本地/MCP 工具执行器。官方 hosted web_search/file_search/code_interpreter、remote MCP、custom tools、programmatic/async tool calling 是独立功能，首版默认不注入。遇到未启用的执行类 item 明确记录/报不支持，不丢弃后伪装正常收尾。

## 14. 参数、配置与前端设置

模型级 `apiType/url` 覆盖 provider；配置、列表、实际调用从相同 resolved config 推导。Responses URL 使用独立规则，保留网关路径前缀和 query，正确区分 /responses 与 /responses/compact；不能继续补 /chat/completions。

建议保留 `parameter.responses`，明确参数与运行状态分离：

| 配置 | Responses 策略 |
| --- | --- |
| max_output_tokens | 原生输出预算，包括推理和可见输出；不要发 max_tokens。 |
| reasoning_effort | 显式映射 reasoning.effort，按模型支持档位显示。 |
| reasoning.summary | 可选择支持的可见推理摘要。 |
| temperature/top_p | 只有显式设置且能力支持时发送；不继承 Chat 的 0.7/1.0 默认。 |
| presence_penalty、enable_thinking、thinking、output_config | 不当作通用 Responses 参数透传。 |
| text.format/verbosity | 按声明能力和用户设置处理；JSON Schema 结构需校验。 |
| store | 此适配模式由后端固定 true，不提供关闭控件。 |
| previous_response_id/conversation/input | 由状态管理器控制，不允许 extra_body 或前端填写任意 ID 改写链。 |
| truncation | 默认 disabled。 |
| context_management | 由压缩策略和模型能力控制；与双份压缩协调，非随意 JSON 扩展。 |
| extra_body | 允许明确扩展，保护模型、输入、工具、状态和存储结构字段。 |

空 Responses 桶表示使用 Responses 默认，禁止回退继承任意非空 Messages/Chat 桶；公共输出预算可以显式迁移，不跨协议复制所有参数。

前端增加 Responses 参数控件组，模型切换时更换相应控件/标签；输出预算用滑块，推理档位/摘要/格式用下拉，按能力显示采样参数。未知能力保守省略，扩展 JSON 收起。store、head、内部游标不进入普通参数面板。

状态详情提供：当前模式、状态、最后同步时间、压缩两项状态、重新同步入口。主界面仍按本地上下文展示。

对于当前规范中的 configuration_update：首版不自动注入。它有模型及与自动压缩/独立 compact 的组合限制；普通参数变化先用请求级配置处理。如果未来使用该 item，应独立增加能力与策略测试，而不是随意添加到 extra_body。[Reasoning configuration updates](https://developers.openai.com/api/docs/guides/reasoning)

## 15. 导入、导出、分享与删除

- 旧历史无 Responses 字段时，正常导入，下次按本地上下文初始化。
- 分享导出保留可读历史及本地摘要，默认移除活动远端指针、私有密文和请求账本。
- 本机完整备份可另提供包括 Responses 私有侧车与媒体的选项。当前 JSONL/ZIP 分享格式不等于这种备份。
- 导入普通分享不能直接启用来源机器的 head；同账户完整备份恢复，也先核对作用域、ID 可用性和本地边界。
- 新建副本、重命名造成的本地 session 路径变化，需要重新绑定侧车会话作用域；不能因 session hash 变了就静默丢失状态。
- 本地删除会话清理其侧车与媒体引用。远端删除是另一个明确动作，不能声称删本地文件就删了 OpenAI 存储。
- Responses 删除单个资源、远端分支祖先和未来 Conversations 删除语义分别处理；涉及备份共用窗口的本地去重资源按引用计数回收。

## 16. 实施顺序与改动位置

以下均为后续实现工作，本次尚未修改：

| 顺序 | 工作 | 主要位置 |
| --- | --- | --- |
| 1 | 统一协议注册/工厂、能力、内部状态契约 | chat/protocols/__init__.py、chat/chat_llm.py、config.py。 |
| 2 | HTTP 客户端、非流式和三种入口、完整事件解析 | responses_transport.py、responses_client.py、protocols/responses.py；不新增 SDK 依赖。 |
| 3 | 请求/原生/工具账本、提交与启动对账 | 新 responses_store.py、memory/chat_memory.py、chat_round_store.py。 |
| 4 | 主聊天新增输入队列、工具闭环和异常恢复 | 新 responses_context.py、factory/chat_factory.py、chat_runtime.py。 |
| 5 | 双份压缩、窗口待使用状态、一次同步重建 | 新 responses_compaction.py、context_compaction.py、routers/chat_router.py。 |
| 6 | 子任务/标题/本地摘要作用域隔离、Todo/注入适配 | sub_agent.py、title_generator.py、todo_runtime.py、session_worker.py。 |
| 7 | 图片/文件/读取工具与恢复资源 | 新 responses_content.py、memory/file_memory.py、builtin_tools.py、媒体配额模块。 |
| 8 | 参数桶、模型能力、前端控件及状态/进度 | env_manager.py、chat_config_router.py、H5 模型/压缩/上下文组件。 |
| 9 | 编辑删除/切换、导入导出备份与回归 | chat_memory.py、chat_history_format.py、相关路由与测试。 |

推荐把“有状态工具循环 + 重试/崩溃恢复 + 双份压缩 + 重新同步”作为可发布首版的整体，不能只做到接口能返回文字就对用户宣布完整支持。

暂不实现：store=false 的无状态 Responses 对话、Conversations 第二种运行模式、WebSocket、官方托管工具全套、跨账户密文迁移。独立 compact 虽然文档称该压缩端点本身 stateless，使用它不意味着本项目增加无状态对话运行模式。

## 17. 验收清单

### 基础与工具

- 新会话、第二轮、连续工具多次调用：旧用户/旧输出不会重复发送，head 每次正确更新。
- 工具定义在当前请求可用；工具禁用/未知/失败/超时反馈保持 call_id 配对。
- item.id 与 call_id 不混用，多工具参数交错增量最终正确。
- 同步 SSE、异步 SSE、非流式均适配；空文本但合法工具/压缩输出不被误判失败。
- 图片、文件、read_media、read_document 正常续接不重复注入，恢复时可还原资源。

### 状态、异常与分支

- created 后断流、completed 前后崩溃、侧车写入失败、_meta 更新失败可对账。
- 已完成工具的结果提交失败后，不重复运行该 call；副作用 unknown 不自动重做。
- 401/403/参数 400 与父响应失效区分，失效自动重建最多一次。
- stop、超时、迟到结果、两项压缩中断不污染有效 head。
- 编辑/插入/删除不带入后续事实；本地及官方压缩检查点一起校验覆盖范围。
- 切换接口/模型/平台、切回 Responses 和导入分享正确重建。
- 父任务、并发子任务、标题与摘要请求完全隔离。

### 双份压缩与恢复

- 同一冻结边界产生两项结果，不消耗重复新增问题。
- 本地成功/官方失败及反向情况保留有效结果，显示部分完成，可单独重试。
- 完整 compact.output 原样保存并作为下一次初始化窗口，不裁剪、不拼回旧链、不使用 compact.id 续接。
- 已压缩待下次发送与已建立新链状态区分。
- 当前本地上下文/原生窗口/完整历史三种手动来源明确，超预算不静默裁剪。
- 过期后恢复一次，此后所有请求重新只发送增量。
- 本地界面为展示依据，真实远端 budget 不被本地估算误导。

### 前端及回归

- 思考摘要不会被 created/usage/compaction 帧拆成多个重复框。
- done/completed 全量字段不会再次重复追加；retrieve 恢复只补齐缺失内容。
- 原生密文、上传 base64、内部状态不通过普通 SSE 透传。
- usage 按响应/压缩操作一次计数；不同响应即使 usage 数字相同也不能去重丢失。
- 原 Chat/Messages 的工具、错误处理、媒体和压缩测试继续通过。

实现已先进行模拟事件和故障注入，再调用目标兼容平台真实验证。初版使用官方 SDK；2026-10-14 的标准库传输替换版在禁止导入 OpenAI SDK 时重新验证了续聊、三种调用入口、工具、图片/PDF、双份压缩和一次重新同步。官方 OpenAI 自有账户的独立 compact/retrieve 路径以协议测试覆盖；实际配置对应的 OpenCode 平台不提供这些查询端点。真实测试只使用隔离的测试会话和生成的本地文件，未修改用户的 models.json、默认模型选择或既有会话。
