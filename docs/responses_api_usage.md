# Responses 使用说明

更新：2026-10-16。支持有状态续聊和无状态兼容两种模式；原有模型默认仍使用有状态续聊。

2026-10-24：异步传输支持完整响应后的连接复用；状态镜像改为小型原子侧车，历史读取和下载仍包含最新状态。恢复 journal 和有/无状态语义不变，见 [发送性能与验证](api_send_performance.md)。

## 选择上下文模式

打开底部“参数”→ 选择 Responses 模型 →“Responses · 上下文管理”→ 选择上下文模式 →“确定”。模式下方说明当前实际选择（包括“跟随模型配置”解析后的结果），保存后从下一次任务生效。四个模型角色可分别设置；当前会话保存为会话配置，无会话时保存为全局默认，沿用原有模型配置的保存规则。Responses 的面板背景、边框、标题/字段文字、滑条、选中页签与确认按钮使用独立粉色主题，并随深浅模式调整文字对比度；Messages 仍使用绿色主题。

| 模式 | 每次请求 | 压缩与恢复 |
| --- | --- | --- |
| 跟随模型配置 | 由模型/provider 的 `responsesMode` 决定，缺省为 `stateful` | 跟随实际模式 |
| 有状态 · 响应 ID 续聊 | `store: true`；首调用初始化，随后通过 `previous_response_id` 发送新增输入 | 本地摘要与官方原生压缩；断流优先查询已有响应 |
| 无状态兼容 · 每次发送上下文 | `store: false`；每次发送当前有效本地上下文，不发送 `previous_response_id` | 仅使用本地摘要；不查询未保存的响应，无需“准备重新同步” |

无状态模式遵循 Responses 手动管理会话的方式：把历史输入和已确认输出交给下一次请求。它仍使用 Responses 协议与现有工具执行器。[官方会话状态说明](https://developers.openai.com/api/docs/guides/conversation-state)

模型级默认示例（也可在 provider 设置同名字段）：

```json
{
  "id": "YOUR_MODEL_ID",
  "apiType": "responses",
  "responsesMode": "stateless"
}
```

角色的 `parameter.responses.responses_mode` 可填 `stateful` 或 `stateless`；省略表示跟随模型。生效顺序为本次请求显式参数 → 角色生效参数（会话覆盖或全局）→ 模型/provider 配置 → 有状态默认。`responses_mode` 是应用控制字段，不发送给上游。完整配置结构见 `setting/models.example.json`。

切换模式保留本地聊天记录并清除旧模式的远端链状态；下一次任务按新模式构造输入。运行中的任务继续使用启动时参数。2026-10-16 Muse Spark 的诊断和真实测试见 [无状态兼容排查](archive/responses_stateless_2026-10-16.md)。

## 启用与参数

Responses API 本身是 HTTP 接口，SDK 只提供调用封装，不是协议要求。当前版本已移除 `openai==2.31.0` 依赖，由 Python 标准库直接发送 HTTP 请求、解析 JSON/SSE，不需要安装 OpenAI SDK，也不为 Responses 新增 HTTP 客户端依赖。重启后端并刷新前端即可加载改动。如果当前 `models.json` 已配置 `OpenCode Responses / GPT 6 Luna`，可在模型面板中选择；示例名称不代表所有安装都包含该供应商或模型。

模型或 provider 使用 `"apiType": "responses"`，地址可填写 API 基础地址或完整 `/responses`。保留第三方网关路径与 query，例如 `https://opencode.ai/zen/go/v1`，不会拼接 `/chat/completions`。

四个角色均可选择 Responses 模型：聊天、压缩、标题、子智能体。它们各自维护远端作用域，辅助任务和并发子任务不改变聊天主链。

在“模型与参数设置”选择 Responses 模型后，显示专属参数区域，滑条为亮浅粉色（#ff85b5）。可以调整最大输出 token、推理强度、推理摘要、文本详略；温度和 top_p 默认不发送，勾选自定义采样后才发送。模型声明不支持的参数会过滤。切换 Chat/Messages/Responses 时切换对应表单，按角色保存草稿与参数桶。

参数示例（填写到角色的 `parameter.responses`；密钥仍由 provider/model 配置提供）：

```json
{
  "responses_mode": "stateless",
  "max_output_tokens": 8192,
  "reasoning": {"effort": "low", "summary": "auto"},
  "text": {"verbosity": "medium"}
}
```

高级区域支持追加 instructions 和扩展请求参数。运行状态字段由后端管理；不能通过 extra_body 覆盖 store、input、previous_response_id、conversation、stream 或启用 background。自动 context_management 首版不开放，避免绕过本地双份压缩流程。

## 正常续聊与工具

有状态模式第一次发送包含当前有效本地上下文；之后只发送新增问题、引导消息和工具结果。两种模式均每次发送工具定义与当前系统 instructions。工具结果通过对应 `call_id` 配对，有状态模式已提交的问题和助手输出不会在正常续链中重复上传。

无状态模式每次发送本地当前有效历史，包括用户问题、助手输出、已完成工具及对应结果、多模态内容；发生本地压缩后发送累计摘要与仍保留的消息，不重新展开已压缩的原始全文。保留当前账户/端点/模型对应的完整原生输出（含可用的推理密文、assistant phase、函数调用 ID），不会把推理摘要当作可恢复的内部推理。原生私有记录缺失、导入分享或换账户时使用本地可见消息与工具事实。没有确认结果的旧工具只作为历史事实说明，不因此自动执行。[官方推理说明](https://developers.openai.com/api/docs/guides/reasoning)

因此，无状态模式每轮请求体、输入用量及本地请求审计通常更大；能否记住内容由本次发送的本地上下文决定，与旧响应 ID 的保存期限无关。`store: false` 也不代表第三方平台的日志、训练或隐私政策发生改变。

前端正文、思考摘要、工具卡、用量统计继续使用现有应用 SSE 格式。流式工具参数只表示活动，直到完整 completed 响应通过验证并持久提交，才进入执行器。failed/incomplete、残缺 JSON、缺少 ID 或异常输出类型不会执行工具；其中瞬时类 failed（`server_error` 等）与 completed 畸形工具调用会消耗网络重试预算自动重建请求，畸形项的精确原因与原始响应会记入请求审计，incomplete/cancelled 与确定性错误直接终止。

Responses 原生推理密文及 compaction 密文只保存在私有侧车；可见推理摘要映射为原来的 reasoning_content，不把 done 中的完整文本再追加一次。不同响应的用量分别计数。

支持文本、图片、原生文件输入，以及 read_media/read_document。读取工具产生的图片或文档归入对应 function_call_output 的内容数组。文件仍按模型 supportDocTypes 决定原生或文本读取。Responses 路径不接收原生音频/视频；GIF 使用静态图片处理，不沿用 Chat 的视频部件。Messages 的音视频限制也相同，但对应错误提示由 Messages 适配器生成。

## 本地上下文与双份压缩

下述官方远端压缩流程适用于有状态模式。无状态兼容模式的自动、任务内与手动压缩均只生成并保存本地摘要，下次调用发送摘要后的有效上下文；不调用 `/responses/compact` 或 `compaction_trigger`，也不把未发送工具结果误标记为已提交。官方支持对完整输入数组执行独立压缩，但不能假定不支持状态续接的兼容平台实现了该接口，因此本版无状态兼容使用现有本地压缩路径。[官方压缩说明](https://developers.openai.com/api/docs/guides/compaction)

界面上下文及 token 数字以本地摘要和有效历史为准。它们不是远端完整上下文的精确统计，也不代表每轮收费 token 数量；正常续链仍会计入远端历史的输入用量。

项目触发压缩时，从同一个冻结边界并行执行：

- 本地摘要压缩：继续保留可读摘要、原始轮次和本地上下文视图。
- 官方压缩：保留原生压缩结果，供远端续聊或一次初始化使用。

冻结窗口会包含刚执行完成、尚未发送的工具结果和媒体。如果官方压缩失败而本地摘要成功，这些未提交输入仍保留到下次成功发送，避免被本地摘要替换后丢失。

`compaction_trigger` 返回密文只证明压缩窗口已生成，不能证明兼容平台已裁剪该响应 ID 的祖先链。OpenCode 实测沿该 ID 续聊仍会带上压缩前的大上下文。因此 trigger 和独立 compact 统一在下一次生成时使用其完整原生返回窗口，加尚未覆盖的增量，建立一次新的 `store: true` 响应；这次不带旧 `previous_response_id`。成功后恢复普通 ID 增量续聊。已经纳入压缩的工具结果不重复发送，也不补造用户消息。

同一轮先做历史摘要、再做工具轨迹摘要时，两次本地压缩共享已完成的远端压缩边界；没有新增远端输入就复用结果，不重复发起官方压缩或重复累计用量。复用旧窗口只会确认该窗口实际覆盖的输入；其后到达的工具结果、媒体和用户引导仍保留并在后续请求发送。完整排查与实测见 [压缩后继续生成](archive/responses_compaction_2026-10-15.md)。

支持两条官方压缩路径：

| 平台能力 | 激活方式 |
| --- | --- |
| 独立 `/responses/compact` | 完整保存 compact.output；下次使用其完整输入窗口加新增输入创建新 root，成功后恢复增量链。compact.id 不作为 previous_response_id。 |
| 独立 compact 返回 404/405 | 尝试发送末尾 compaction_trigger；原生输出先标记为待启用，下一次生成用其完整窗口建立新有状态链。 |

两项压缩分别保存成功/失败状态，单项失败不会清除另一项已完成结果。本地摘要失败、官方压缩已完成时，重试只补本地部分。用户停止或进程中断会保留已确认结果；尚未确认的远端请求保留恢复标记。

远端压缩另按上一响应 usage 与待发送增量检查窗口阈值；即使本地摘要很小，也能触发原生压缩。压缩请求的 usage 是处理压缩前窗口的费用，不代表压缩后的上下文大小。启用新链后，保存该次真实 `input_tokens`；参数面板区分“窗口待启用”和“已从窗口建立新链”，并显示启用时输入用量。旧版未真正启用的 trigger 链会在下一次发送时自动恢复为原生窗口加后续已确认记录，不需要重放工具。

原始原生 output 及完整压缩窗口原样保存在私有侧车。重新发送时按官方输入 item schema 投影：保留全部输入条目、密文、工具配对及消息内容，去除 created_by 等输出专属元数据。实测 OpenCode 返回 created_by，但创建请求会拒绝该字段；不投影会导致重新同步时报 400。

OpenCode 当前实测要求 compaction_trigger 的 max_output_tokens 至少 20000。这是输出预算上限，实测压缩产生约几十个输出 token。适配器只对该控制调用使用此预算，不改变正常聊天输出预算；模型声明上限不足时给出明确错误。平台不支持两种原生压缩时，可保留本地摘要并使用“重新同步上下文”。

## 过期、异常与重新同步

有状态主会话在 Responses 参数区域可查看远端状态，并选择“重新同步上下文”的来源；无状态模式仅显示本地上下文说明，隐藏整个重新同步区域（来源选择、按钮与有状态帮助文字），不查询远端状态。辅助角色没有聊天主链的重新同步控件：

| 来源 | 数据 |
| --- | --- |
| 当前本地上下文 | 可读累计摘要与当前有效历史；适合默认恢复及接口切换。 |
| 原生恢复窗口 | 当前账户与模型的原生 items，包括私有密文和已保存工具输入输出；不能跨账户或模型直接移植。 |
| 完整原始历史 | 未经跨轮摘要缩减的本地轮次；先检查当前模型窗口，超出则提示先压缩。 |

按钮只准备待发送窗口，不额外调用模型。下一次正常发送初始化一次新 `store: true` 响应；成功后继续只发增量。历史中没有配对结果的工具请求转成未确认事实，避免悬空调用阻止恢复，也不会因此执行旧工具。

官方响应保留期限通常为 30 天，第三方平台期限可能不同；不能假定整个祖先链会随新调用无限延期。界面在链建立超过 20 天时提供年龄风险提示，这不是过期证明。收到明确的 previous_response_id 失效错误时，适配器最多自动用原生恢复窗口建立一次新链；认证错误和普通参数错误不误判为过期。

网络失败沿用 `NETWORK_RETRY_MAX_ATTEMPTS`；Responses 客户端对正数 N 使用“总尝试次数最多 N 次”的口径（0/负数不限制），与 Chat Completions / Messages 中“首次请求之外最多重试 N 次”的口径不同。收到 response.created ID 后发生断流，优先 retrieve 确认该响应，完整结果只补齐缺失文本。平台无法查询或结果仍不确定时，保留恢复状态并提示手动重新同步。已完成工具复用缓存；执行状态未知的工具禁止自动重跑。

以上查询恢复适用于有状态模式。无状态模式在尚未收到响应 ID 或内容时沿用连接失败/限流/服务错误重试；一旦收到响应 ID 或部分输出后断流，报告明确错误并结束此次调用，不查询旧 ID、不自动拼接另一条生成流、不执行未完成输出中的工具。下次正常发送从已确认的本地历史继续；重启遗留的未完成无状态请求同样不查询平台。停止请求仍使用现有取消机制。

兼容平台若明确报 `reasoning.encrypted_content` 不能与 `previous_response_id` 同用，仅移除该链上请求的密文 include 后重试一次，并为当前来源保存此兼容设置，不移除历史消息或已拿到的密文。根请求和无状态请求仍可请求推理密文。若随后明确报引用的响应不存在/过期，有状态模式最多自动从原生窗口重建一次；持续不提供续接能力的模型应改用无状态兼容模式。

这两种兼容处理均不自动改变 `responses_mode`：自动重建仅在当前调用中去掉父 ID，回传完整本地原生窗口及新增输入，仍然 `store: true`；成功后保存新 ID，下次仍尝试 ID 续接。每次调用最多重建一次，再失败则返回错误，不无限重建。正常过期和持续不支持 ID 续接不能仅凭一次错误区分，因此不会自动把 GPT 或其他模型改成无状态。前端恢复提示明确说明仍为有状态；状态 `ready` 只表示上次请求完成，不能证明平台 ID 续接可用。需要每次稳定重发上下文时，应显式选择无状态兼容模式并保存。

本地历史重新同步时，assistant 正文统一编码为 `output_text`，用户消息及工具输出内容仍使用 `input_text`。字符串正文与内容数组走同一转换流程；旧版本已经准备的同步窗口在发送前也会修正 assistant 的错误类型。已保存的原生 annotations、refusal 和 phase 保留。

私有请求记录中的 `failures` 最多保留最近 10 次异常，记录发生阶段、异常类型、时间、HTTP 状态和错误说明。读取超时有明确文字，查询接口返回 404/405 时会同时说明查询失败及此前断流原因，避免后一次错误覆盖最初的故障。2026-10-15 再次实测 OpenCode：创建和增量续聊正常，但对刚完成的响应执行 retrieve 仍返回 404 网页。该平台断流后需要手动选择本地上下文重新同步；不能据此断言最初断流一定由供应商造成。具体排查见 [断流恢复排查](archive/responses_recovery_2026-10-15.md)。

标准库传输分别处理连接/TLS、发送和读取超时；异步调用复用项目连接与取消机制，同步调用在读取、发送和连接等待期间检查停止标记。Content-Length、chunked（含扩展与 trailers）、gzip/deflate、UTF-8 跨块和多行 SSE 都独立解析，截断内容不当成完整成功响应。401/403/普通参数 400 不自动重试；429/5xx 沿用应用重试预算，并在有限范围内尊重 Retry-After。传输层不自动重试，也不自动跟随重定向；连接方式与项目现有 Chat/Messages 一样直接连接目标地址。

若任务在完成模型调用后、工具执行前被停止，下一次发送要求核对并重新同步。若工具已经完成但本地轮次没有保存结果，可以从私有账本补发缓存结果，避免再次执行。含未确认工具的原生窗口不直接建立新链，应核对实际效果后使用本地上下文来源。

编辑、插入、覆盖回答、删除轮次及接口/账户/模型变化都会使旧主链失效。其中编辑/插入/删除/截断只失效远端原生链，本地可读累计摘要保留并按位置平移锚点（与 compact.md“删除轮次不停摘要”同口径），下次请求仍用摘要替换已覆盖历史轮；按有效历史重新构建的是远端链，不是摘要。普通分享/导出保留本地事实并去除 Responses 指针与审计引用；导入后不复用来源账户的旧链。

## 存储与后端入口

- `_meta.responses_state`：主链当前指针、状态、代次、压缩状态。
- `chat_round.events` 的 `model_response`：主聊天及子智能体模型调用的响应 ID、父 ID、作用域和记录引用。
- `history_files/sidecars/responses/<session-hash>/`：原始请求、完整原生响应、工具执行账本、压缩窗口和作用域状态。

私有记录采用原子写入、跨进程锁及代次校验。请求和响应按调用增量保存，恢复窗口按引用组装，避免每轮复制整段历史。私有侧车不随普通会话分享导出；本机完整备份需要一并保留 history_files 目录，首版没有额外的私有备份界面。

Windows 文件暂时被索引器或其他进程占用时，私有状态原子写入执行有限退避重试。重试耗尽仍报告错误，不把没有保存的结果视为已确认提交。

状态包含 `responses_mode`。无状态模式仍保留私有响应及工具账本以回传原生输出和避免重复执行，但其 ID 仅用于本地审计/关联，不作为下一次请求的父 ID；脱敏状态的 `has_remote_head` 和 `age_risk` 均为 false。分享继续移除私有响应引用，导入后根据本地事实初始化，不要求携带来源账户密文。

后端增加：

```text
GET  /chat_context/responses_state?session_id=...
POST /chat_context/responses_rebuild?session_id=...&source=local
```

source 可为 local/native/raw。正在生成或压缩时拒绝重建。状态 API 不返回原生输入和密文。现有手动压缩接口保持不变，Responses 下会协调两项压缩。

## 实际验证

使用用户配置的 gpt-6-luna，对 OpenCode 进行真实测试（初版使用官方 SDK，标准库替换版重新验证）：

- 初次对话、跨请求记忆、函数调用/结果及同步和异步流式返回。
- 主聊天真实 read_file 闭环：读取随机代号，两次模型调用完成；重新打开链后仍能回忆。
- 可读摘要与原生压缩并行执行，压缩后仍能返回此前读取的随机代号。
- 原生压缩窗口一次重新同步建立新 root；下一请求恢复 previous_response_id，只有一个新增输入，代号仍能正确回忆。
- 2026-10-15 补充大小验证：沿 trigger 响应 ID 续聊仍约 22.5k 输入 token；使用其原生窗口建立新有状态链后降至 42 token。应用工具闭环验证输入从 22,467 降至 107 token，两次本地压缩只生成一份原生窗口，工具结果只提交一次，下一条消息仍为单条增量。
- 真实 read_media：读取生成的红色图片，工具结果发送 input_image 后模型正确回答“红色”。
- 真实 read_document：读取生成的 PDF，工具结果发送 input_file 后模型准确返回页面内随机代号。

标准库版本测试报告位于 tmp/responses_stdlib_live、tmp/responses_runtime_stdlib_live、tmp/responses_media_stdlib_live。测试显式禁止导入 OpenAI SDK；纯 Responses 调用还禁止导入 httpx。项目已有 MCP 依赖可能仍使用 httpx，与 Responses 传输无关。测试使用独立会话和生成的本地文件。

OpenCode 实测独立 compact 与 retrieve 均返回 404。已经验证 compaction_trigger 兼容路径；对于无法 retrieve 的断流，仍需使用上述重新同步入口。官方 OpenAI 自有账户的独立 compact/retrieve 路径通过模拟协议与故障测试验证，本次没有该账户的真实调用结果。

源码专项测试覆盖身份隔离、增量提交、流式去重、失败/取消、过期恢复、工具闭环、双份压缩部分失败、未知执行禁止重跑、worker 参数保真、JSONL 审计与删除，以及媒体工具结果。Chat/Messages 与前端现有测试同时保留。

新增 test/test_responses_transport.py 使用实际本地 TCP 服务模拟 HTTP/SSE，验证分块和压缩、截断、Content-Length 无须等 EOF、超时、停止、认证/限流/服务错误，以及断流后通过 GET 恢复同一响应。运行时不会因缺少 openai 或 httpx 无法调用 Responses。

## 官方资料

[会话状态](https://developers.openai.com/api/docs/guides/conversation-state)、[创建响应](https://developers.openai.com/api/reference/python/resources/responses/methods/create)、[压缩](https://developers.openai.com/api/docs/guides/compaction)、[函数调用](https://developers.openai.com/api/docs/guides/function-calling)、[文件输入](https://developers.openai.com/api/docs/guides/file-inputs)、[数据保留](https://developers.openai.com/api/docs/guides/your-data)。
