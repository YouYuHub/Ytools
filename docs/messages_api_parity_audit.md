# Messages 与 Chat Completions 功能对照

检查日期：2026-10-11。对照项目现有实现，核对 Anthropic 官方接口后补齐；下表描述应用行为，并不表示所有兼容网关具有相同模型能力。

## 本次补齐

| 问题 | 补齐后的行为 |
| --- | --- |
| Messages 没有接入供应商媒体数量错误恢复 | 同步流式、异步流式、非流式均识别现有 `At most N image(s)/document(s)/media` 等错误，调整未绑定原生回放的请求后重试；缓存按协议、端点、供应商和模型隔离 |
| 工具结果内的附件没有参与数量降级 | 递归处理 `tool_result.content` 和自定义文档内的图片；保留工具 ID、错误标志、正文和未发送提示 |
| 请求直接携带附件时配置配额只会报错 | 新请求按配置/已学习的 `mediaLimits` 保留最近附件；签名绑定的原生回放仍只校验、不修改；官方大小和页数等硬上限仍明确报错 |
| 完整结束的空响应被当作成功 | 空 `end_turn` 在现有重试预算内追加新的 user 继续提示；空响应不写原生记录，已完成工具不重复执行；`stop_sequence`、拒绝及长度/窗口结束保留各自语义 |
| 旧调用方的停止序列和结构化输出扩展会原样发给 Messages | `extra_body.stop`（字符串/数组）转换为 `stop_sequences`；`response_format.type=json_schema` 转换为 `output_config.format`；显式原生字段优先 |
| 参数面板修改 token 预算会遗漏扩展参数 | Messages JSON 面板支持并保留 `extra_body` 对象，四角色均沿用已有扩展参数通路；可配置自动缓存、metadata、服务等级等模型支持的原生字段 |
| 禁用工具且禁用并行时生成无关参数 | `tool_choice=none` 只发送 `{type: "none"}`；强制选择未声明工具在发送前报错；显式 `function.strict` 保留到两种协议 |
| 压缩从 Messages 回退到 Chat 时继承了错误协议的参数 | 按实际调用模型的角色参数重建请求；双向回退隔离思考、扩展参数和采样设置，并保持输出预算上限 |

媒体降级只处理明确的数量错误，且本次模型响应尚未开始输出。如果原生回放中有历史附件超出新配额，会停止并提示压缩/重建上下文；不会删除签名绑定的图片或文档。降级警告沿用已有 `MEDIA_LIMIT_DOWNGRADE` 业务事件。一般连接/超时/429/5xx 继续使用现有网络重试机制。

## 现有功能对照

| 项目能力 | Messages 状态 |
| --- | --- |
| 主聊天、标题、历史/任务压缩、子智能体 | 已支持；各自解析生效模型、参数和自定义头 |
| MCP 与内置工具、并行批次、授权失败、执行异常 | 已支持；转换为 `tool_use` / `tool_result`，失败通过 `is_error` 告知模型 |
| 图片上传与 `read_media` | 已支持 JPEG/PNG/GIF/WebP 和其他图片的格式转换；GIF 首帧；本地、上传引用和网络 URL |
| `read_document` 与 PDF 分页、Word/Excel | 已支持；PDF/文本可原生发送，Office 使用已有文本/表格解析 |
| SSE 正文、思考、工具、usage、finish_reason | 使用现有业务结构；原生缓存 usage 合并后保留于 `messages_usage`，签名和 provider_state 留在后端 |
| 连接失败、读写超时、空 body、提前 EOF、流内错误 | 已支持；恢复/终止遵循现有配置，并丢弃未完成工具调用 |
| 用户停止、工具参数阶段超时 | 已支持；关闭上游，不执行尚未完整提交的工具 |
| max_tokens 截断续写 | 已支持；丢弃不完整工具；实际窗口已用尽时明确提示压缩 |
| 历史加载、压缩、编辑、导入/导出、协议或模型切换 | 沿用原有路径；原生回放通过私有记录保护，失效或跨模型时使用事实文本重建 |
| Todo、ask_user、文件版本链、子任务事件 | 沿用共享运行时，不要求前端改用 Anthropic 内容块 |
| 输入预算、输出上限、附件配额 | 已支持预检；图片/PDF tokens 仍是近似估算，与项目原有统计口径一致 |
| 停止序列、JSON Schema 输出、严格工具参数 | 已补齐兼容转换；实际支持情况及 schema 约束由目标模型/网关决定 |

## 有意保留的协议边界

- 原生音频、视频无法按当前 Messages 内容块发送；图片工具明确拒绝，已有输入会告知模型未发送。PDF、图片和解析后的文字可正常使用。
- Chat 的 `presence_penalty`、`reasoning_effort`、`enable_thinking` 不会自动换成 Messages 参数；使用原生 `thinking` / `output_config`。采样和强制工具选择是否可用随具体模型而异。
- `response_format=json_object` 无法保真转换为任意字段的原生严格 JSON Schema，发送前明确提示改用 schema。应用不会自动修改 schema 或声称保证供应商未支持的约束。
- 中途 system 消息、新模型的服务端工具及 `pause_turn` 尚未接入通用适配器。项目现有 system 前缀、客户端 MCP/内置工具使用已接通的路径。
- Files API 上传/删除管理、官方精确 token counting、专用文档引用面板没有新增；已有 file_id 输入、引用元数据和本地文件管理可用。Chat Completions 原有路径也没有这些 Anthropic 专属管理能力。
- 参数/鉴权/附件格式的确定性错误不会反复重发；媒体配额有可行的修改方案时单独恢复。真实模型参数与网关差异仍需用实际配置验证。

## 配置示例

Messages 参数面板的 JSON 可写：

```json
{
  "thinking": {"type": "adaptive"},
  "output_config": {"effort": "medium"},
  "extra_body": {
    "cache_control": {"type": "ephemeral"},
    "stop": ["END"]
  }
}
```

这里的 `stop` 会转为 `stop_sequences`。原生 `stop_sequences` 更适合新配置；示例用于展示旧调用参数迁移。缓存并非默认开启，需按目标供应商支持情况显式设置。

## 验证与依据

`test/test_messages_parity.py` 覆盖三种传输的配额恢复、嵌套附件、缓存配额、安全回放、空响应恢复及上限、主/子工具不重复执行、旧参数转换、四角色扩展参数与双向压缩回退。现有 Messages 多模态/异常测试、Chat Completions、工具、历史、Todo、模型选择和前端测试继续回归。

历史验证快照（2026-10-11）：当时记录相关后端回归 699 项与 22 个 subtests 通过、H5 272 项通过；后端有 20 条未 await 清理协程警告。此数字只描述当日的测试集合与环境，不代表当前工作区。当前请运行 Messages 专项 pytest 与 `cd H5 && npm test` 获取最新结果。测试使用内存 HTTP 和模型替身，未访问真实供应商。

官方依据：[Messages](https://platform.claude.com/docs/en/api/messages/create)、[空 end_turn 的处理](https://platform.claude.com/docs/en/build-with-claude/handling-stop-reasons)、[结构化输出](https://platform.claude.com/docs/en/build-with-claude/structured-outputs)、[工具结果](https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls)、[并行工具控制](https://platform.claude.com/docs/en/agents-and-tools/tool-use/parallel-tool-use)、[自动缓存](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)。
