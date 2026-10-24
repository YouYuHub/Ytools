# Messages API 使用说明

发送前重复估算/校验已合并，重试仍重新校验最终请求；增量 SSE、连接复用及耗时日志见 [API 发送性能](api_send_performance.md)。

项目可按模型的有效 `apiType` 调用 Chat Completions、Anthropic 风格的 Messages API 或有状态 Responses API。本文只说明 Messages 的请求格式与能力；聊天、标题、压缩和子智能体四个角色均可选择 Messages，原有 `ChatLLM` 方法名和业务 SSE 格式继续使用。

2026-10-11 更新：已接通图片、PDF、纯文本和自定义内容文档。本文描述当前实现；最初的实施方案保留在 `messages_api_compatibility_plan.md`。

## 配置和参数

在 `setting/models.json` 的 provider 或模型条目设置 `"apiType": "messages"`。模型级 `apiType` 和 `url` 覆盖 provider 值。模型列表和参数桶依据当前目录解析，不依赖旧会话缓存的协议标记。

下例仅展示配置结构。请使用供应商实际提供的模型 ID 和地址：

```json
{
  "Messages Provider": {
    "vendor": "custom_endpoint",
    "apiKey": "YOUR_API_KEY",
    "apiType": "messages",
    "models": {
      "My Messages Model": {
        "id": "YOUR_MODEL_ID",
        "url": "https://api.anthropic.com/v1",
        "toolCalling": true,
        "vision": true,
        "supportDocTypes": [".pdf", ".txt", ".md", ".csv"],
        "maxInputTokens": 200000,
        "maxOutputTokens": 8192
      }
    }
  },
  "model_selection": {
    "chat_model": {
      "ownership_name": "Messages Provider",
      "model_name": "My Messages Model",
      "parameter": {
        "messages": { "max_tokens": 8192 }
      }
    }
  }
}
```

地址支持 `/v1`、完整 `/messages`、网关路径前缀和 query。官方根地址会补 `/v1/messages`；指向 `/chat/completions` 的 Messages 配置明确报错。默认使用 `x-api-key`（存在 `apiKey` 时）和 `anthropic-version: 2023-06-01`；默认不发送 Bearer。可用角色自定义请求头覆盖鉴权头或版本头；显式配置一种鉴权头会替换自动鉴权头。不要同时配置多种鉴权头。

参数面板在选择 Messages 模型时显示最大输出 token、Messages 专属参数控件以及通用「高级供应商参数 / extra_body」JSON 区域。可选字段为 `thinking`、`output_config`、`stop_sequences`、`temperature`、`top_p`、`extra_body`；留空不发送采样和思考配置，使用供应商默认值。`stop_sequences` 是自定义停止串数组：模型生成到其中任一项即停止，响应的 `stop_reason` 为 `stop_sequence`，`stop_sequence` 字段给出命中的那一个。`extra_body` 必须为对象，可承载模型支持的 `cache_control`、`metadata` 等扩展；调整输出预算会保留这些字段。

旧调用参数 `extra_body.stop`（字符串或字符串数组）转换为 `stop_sequences`，`extra_body.response_format.type=json_schema` 转为 `output_config.format`，保留 schema；显式原生字段优先。`json_object` 等无法保真转换的格式发送前报错。工具定义中的显式 `function.strict` 会传给上游；不自动修改 schema。完整对照与本次补齐见 [功能检查记录](messages_api_parity_audit.md)。

支持 adaptive thinking 的模型可填写：

```json
{
  "thinking": { "type": "adaptive" },
  "output_config": { "effort": "medium" }
}
```

支持 manual thinking 的模型可填写：

```json
{
  "thinking": { "type": "enabled", "budget_tokens": 2048 }
}
```

manual budget 至少 1024，必须小于实际 `max_tokens`，且不能强制选择工具。实际输出预算还会受模型上限、上下文剩余空间和已有全局硬限制约束。降额后不能容纳思考预算时，在发送前报错。具体模型支持哪些参数由供应商决定；应用不猜测模型能力。官方说明：Claude 4.7 及之后的模型（如 Haiku 5.5）对 `type: "enabled"` 返回 400，应改用 adaptive 并以 `output_config.effort` 控制深度；4.5 及更早模型只支持手动预算。面板中的 Enabled 仅在模型未声明 `thinking_budget: false` 时可选，滑块上限随最大输出 token 联动（不超过它的最大 1024 倍数），调小最大输出 token 时超出的预算会自动下调。开启 thinking（adaptive 或 manual）时，旧模型要求 `temperature` 为 1 或不设置、`top_p` 在 0.95–1 之间；发送前即报错，不静默改写。

四角色分别使用 `parameter.messages`。Messages 桶缺失时仅可从 Chat 桶继承共同的 `max_tokens`；不会把 Chat 的温度默认值、`presence_penalty`、`reasoning_effort` 或 `extra_body.enable_thinking` 自动转成 Messages 参数。参数面板会根据协议显示对应控件；Messages 的 Temperature、Top P、thinking、effort、停止序列和供应商扩展参数分别编辑，未改动的可选参数继续使用供应商默认值。模型条目的 `parameterCapabilities` 可以显式标记不支持的参数；服务端最终组包也会过滤这些字段，因此旧会话里遗留的值不会发送。Anthropic 官方对 Claude 4.7 及后续模型标记了 `temperature`、`top_p`、`top_k` 的弃用，非默认值会返回 400；兼容端点若报告某模型不接受这些字段，应在该模型上将能力标为 `false`。压缩和子智能体仍保留现有模型回退链，标题失败仍采用原有标题机制。

## 图片与文档

| 输入 | 当前处理 |
| --- | --- |
| JPEG、PNG、GIF、WebP | 转为 `image`；上传引用、本地 `read_media`、网络 URL 均可用 |
| GIF 动图 | 只读取首帧；本地/上传转 PNG，网络 GIF 由供应商读取首帧，工具元信息标明首帧口径 |
| BMP、ICO、TIFF | 沿用图片解码并转为 PNG/JPEG；损坏图片明确提示未注入 |
| PDF | `read_document` 按声明类型走原生输入，最终转为 `document`；支持已有 PDF 分页读取 |
| TXT、Markdown、CSV 等文本 | 继续支持已有文本解析；原生文件部件也可转为 `text/plain` 文档 |
| Word、Excel 等二进制格式 | 使用已有文本/表格解析；不会将 DOCX/XLSX 原始字节作为 Messages 文档发送 |
| 音频、视频 | 官方 Messages 不接受这两类原生输入，明确提示未发送；`read_media` 拒绝读取这两类 |

`vision: true` 决定图片上传和 `read_media` 是否可用。`supportDocTypes` 决定 `read_document` 的原生/文本分流，与 `vision` 独立。Messages 模型的文档声明只保留 PDF 和可转换的文本扩展名；旧配置即使写了 `.docx`/`.xlsx`，也会走已有解析链路。上传仍保存原始文件供预览和下载，不改变用户文件。

应用已有的 `image_url`（data URL 或 http(s) URL）、`file.file_data` 部件会在发送时转成官方格式；上传的 `media://` 引用在后端解析，前端不用改成 Anthropic 字段。主聊天、子智能体的工具读取结果以及支持视觉的标题模型都走相应的图片路径。压缩使用文本与附件标签，避免把二进制数据塞进摘要。

直接调用项目接口时，也接受原生 `image` 和 `document` 内容块。图片/PDF 的 `source` 支持 `base64`、`url`、`file`（已有 Files API `file_id`）。文档还支持 `text` 和 `content` 来源，保留 `title`、`context`、`citations`、`cache_control`；图片保留 `cache_control` 和原生 `transformations`。工具结果的 content 数组可携带文本、图片和文档，不会被 JSON 字符串化。

下例为项目请求的消息字段示意，URL 请替换为实际可访问的附件地址：

```json
{
  "messages": [
    {
      "role": "user",
      "content": [
        {"type": "image", "source": {"type": "url", "url": "https://example.com/photo.png"}},
        {
          "type": "document",
          "source": {"type": "url", "url": "https://example.com/manual.pdf"},
          "title": "手册",
          "citations": {"enabled": true}
        },
        {"type": "text", "text": "比较图片与手册中的说明"}
      ]
    }
  ]
}
```

已启用的文档引用通过 `citations_delta` 收集，完整结果保留在后端原生记录，并在最终业务事件中提供可选 `citations` 数组。每项含 `content_block_index`、正文 Unicode 码点位置 `text_start`/`text_end` 和原生 `citation`。正文、工具、usage、finish_reason 继续使用现有业务字段；当前前端显示正文，尚无专门的文档引用面板。文档引用必须全部开启或全部关闭，并且不能与结构化输出 `output_config.format` 同时使用。

现有上传接口保存到本地后发送内联数据；这次没有增加供应商 Files API 的上传/删除管理界面。已有 `file_id` 可直接引用，URL 附件由供应商下载；云平台/兼容网关能否接受这些来源由其接口能力决定。

## 附件预算与校验

最终请求（包含实际原生回放、工具结果和嵌套附件）在发送前检查：默认请求体不超过 32MiB，单图 base64 不超过 10MiB，单图边长不超过 8000px；超过 20 张图时边长限制为 2000px。200k 及以下窗口按 100 张图，其余按 600 张；PDF 总页数在不足 1M 窗口时最多 100 页，否则最多 600 页。未配置窗口时采用较保守的 100 张/100 页。大小、数量和页数限制依据核对时的官方规范，网关可能有更低上限。

模型或 provider 可增加 `messagesLimits` 降低这些上限（正整数，不能放宽官方上限）：

```json
{
  "messagesLimits": {
    "max_request_bytes": 20971520,
    "max_image_bytes": 5242880,
    "max_images": 20,
    "max_pdf_pages": 50,
    "max_image_dimension": 2000
  }
}
```

已有 `mediaLimits` 数量配额和本地上传/原生读取大小预算仍有效。新请求按配置/已学习的配额保留最近附件；明确的供应商数量错误可在尚未输出时缩减附件后重试，包括工具结果内的图片/文档。签名绑定的回放前缀不会为了配额而删改；实际回放超限时明确停止，提示减少附件或重建上下文。网络重试添加部分正文后也重新检查最终请求体。配额缓存按端点、模型和协议隔离；配额提示不会重复追加，也不会改变纯文本续写请求。

本地图片会解码校验、转换格式，较大图片沿用缩小尺寸策略。base64 PDF 在安装 PyMuPDF 时检查有效性、加密状态及总页数；缺少该可选依赖时仍可发送原生 PDF，但详细页数/有效性由供应商检查。URL 和 `file_id` 的真实图片尺寸、PDF 页数不能在本地得知，由供应商校验，其错误进入统一异常处理。

图片/PDF 的二进制编码不按字符计入 token 预算；文本文档和工具结果中的文本仍正常计数。图片/PDF token 值继续采用项目的近似占位估算，不能代替官方 Token Counting API 的精确计数。

## 工具、流式输出与恢复

- 开头的 system 消息转为顶层 `system`；中途 system 明确报错。
- 工具 schema 转为 `input_schema`；每批 `tool_use` 的结果按声明顺序放入紧随其后的一个 user 消息。执行失败、未授权或超大结果拒绝使用 `is_error`。
- 文本和可显示的思考实时输出。工具 JSON 仅缓存；必须收到 `message_stop`、合法结束原因、完整内容块和有效对象参数，才提交给工具执行器。EOF、异常或超时都不会执行未提交的工具调用。
- `thinking.signature`、`redacted_thinking.data` 和完整块顺序保存在后端原生记录，浏览器 SSE 不接收这些数据。普通 `reasoning_content` 不用于伪造签名块。
- 兼容平台返回无签名 `thinking` 时，完整响应的正文/思考仍显示，完整工具调用仍可提交；该响应标记 `replay_eligible=false`，不写原生回放记录，下一次请求通过普通历史转换或事实重建继续，绝不回传无签名思考块。有签名的官方响应保持原生回放规则。提前 EOF、未闭合内容块或无效工具 JSON 仍拒绝提交。
- 前端只在非空正文、有效工具调用、completion 完成/截断和错误等真实边界关闭思考块；空增量、签名活动、usage 或其他元数据不会拆分思考。刷新重连与普通发送共用这套处理。
- 同一待完成工具链回放实际已发送的消息前缀，并冻结 system、tools 和 thinking/output 配置；只追加完整工具结果和后续文本。Todo 整理不会拆改这段原生链。
- 工具开始前写入 running 状态，结果完成后原子写入 completed。恢复优先复用完成结果；缺少结果且执行状态未知时明确报错，不自动重新执行。批次执行途中进程崩溃仍不能保证每个副作用恰好发生一次。
- 上下文压缩后退役旧回放引用，并重建 Todo 缓存。模型/端点/鉴权来源切换、已完成对话或缺失原生记录使用事实文本重建，不伪造 thinking。导入历史移除原生引用。
- 原生记录在 `history_files/sidecars/messages/<会话哈希>/`。删除会话会一并删除该会话原生目录。导出、分享和导入不迁移该私有目录；跨会话恢复使用文本事实。

## 网络和异常处理

网络重试沿用 `NETWORK_RETRY_MAX_ATTEMPTS`：正数表示首次请求之外允许的重试次数，0/负数不限制。Messages 流式和非流式均使用这一配置。

| 情况 | 当前行为 |
| --- | --- |
| 连接失败、读写超时、408/409/425/429、5xx、overloaded 流内错误 | 按现有次数重试，通常等待 1 秒；数值 Retry-After 最长等待 60 秒 |
| 400/401/403、无效参数、签名或不支持的内容块/停止原因 | 网络层不重试；返回带阶段、错误明细和重试统计的错误 |
| HTTP 200 空 body、缺少完成状态、无输出的提前 EOF | 作为空响应重试，达到上限后报错 |
| 正常 `end_turn` 但无正文、思考或工具 | 在现有重试预算内追加新的 user 继续提示；保留工具结果，不写入空响应的原生记录；显式 `stop_sequence` 触发的空输出保持原义 |
| HTTP 400 / 流内错误明确声明媒体数量配额 | 未输出且未绑定原生回放时，缩减附件并警告后重试；原生回放不可删改时停止并提示压缩/重建 |
| 已输出正文后出现可重试流内错误或网络异常 | 保留已输出正文，附带续写提示重连；丢弃本次未完成调用和思考签名 |
| 已输出内容后提前 EOF | 发出 `stream_truncated`，交给主/子循环现有截断续写和重试上限处理 |
| `max_tokens` 截断 | 丢弃工具块，沿用长度截断续写逻辑 |
| `model_context_window_exceeded` | 明确停止并提示压缩，避免无空间时无限续写 |
| 用户停止或工具参数流停顿超时 | 沿用已有取消与工具超时机制，关闭上游；不执行部分工具调用 |
| 原生记录无法写盘 | 报本地保存错误，不重复调用模型，不提交工具 |
| 附件格式错误、图片损坏、加密 PDF、数量/页数/请求体超限 | 发送前返回明确的不可重试协议错误；本地媒体加载失败可转未注入占位，保留其余文本 |

上游 ping 和未知 SSE 事件不会计作工具进展。真正的输入 JSON/signature 增量会维护工具阶段等待期限。usage 将 message_start 与后续累计 usage 合并后只提交一次，同时保留缓存输入等原生统计字段。

## 实现边界和验证

Messages 当前覆盖文本、图片、PDF/文本文档、工具、原生思考和四角色。Messages 服务器工具及原生音频/视频输入不在支持范围；Word/Excel 使用已有解析工具。项目另有独立 Responses 实现，其能力与限制见 [Responses API 使用说明](responses_api_usage.md)。

Messages 的 `read_media` 工具定义只向模型声明图片读取和可选质量参数，移除视频时间区间参数，并明确 GIF 首帧语义；Chat Completions 保持原有媒体工具定义。

压缩发生跨协议回退时，按实际调用模型重新取角色参数，隔离 Messages 思考/扩展参数与 Chat 参数。`tool_choice=none` 不发送并行控制；强制选择未声明工具在发送前报错。

新增 `test/test_messages_api.py` 覆盖请求转换、协议头、SSE/chunk/UTF-8、完整完成判定、错误分类、重试、取消、超时、原生签名回放、无签名兼容响应、工具执行状态、主/子循环和历史恢复。参数面板测试覆盖协议切换、独立控件、默认值及非法扩展字段；`H5/test_h5/chat_reasoning_stream.test.js` 覆盖控制帧不拆分思考及真正的阶段边界。还运行了现有模型选择、标题、压缩、历史、工具配对、todo、截断恢复、停止及网络错误回归。

`test/test_messages_multimodal.py` 使用 Pillow/PyMuPDF 创建真实附件，覆盖混合图片/PDF、文本和自定义文档、URL/file_id、嵌套工具结果、签名前缀保留、GIF 首帧、损坏/加密/超限拒绝、引用增量、非流式结果、网络重试，以及主/子智能体实际工具读取后注入附件的闭环。首版的网络、取消、截断和四角色回归继续保留。

历史验证快照（2026-10-11）：当时记录 Python 699 项与 22 个 subtests 通过、H5 272 项通过；这些是该日期的测试结果，不代表当前工作区或当前依赖环境。当前请分别运行 `python -m pytest test/test_messages_api.py test/test_messages_multimodal.py test/test_messages_parity.py -q` 与 `cd H5 && npm test` 获取最新结果；测试使用模型替身，不访问真实供应商。

发送前预算检查覆盖实际原生回放前缀和顶层 system，思考签名及隐藏块按不透明数据处理。现有界面统计与自动压缩触发仍是文本估算值；实际回放若超过模型窗口，会在发送前停止并提示压缩。

测试使用内存 HTTP 和模型替身，没有调用真实供应商。真实供应商的模型参数和平台差异需用目标配置验收。协议依据：[Create a Message](https://platform.claude.com/docs/en/api/messages/create)、[Vision](https://platform.claude.com/docs/en/build-with-claude/vision)、[PDF](https://platform.claude.com/docs/en/build-with-claude/pdf-support)、[Files](https://platform.claude.com/docs/en/build-with-claude/files)、[Citations](https://platform.claude.com/docs/en/build-with-claude/citations)、[Streaming](https://platform.claude.com/docs/en/build-with-claude/streaming)。
