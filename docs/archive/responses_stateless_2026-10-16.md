# Responses 无状态兼容与 Muse Spark 会话排查

日期：2026-10-16。

## 结论与证据

会话 `yt-2026-10-16_11.14.29_882_chat.jsonl` 使用 `OpenCode Responses / Muse Spark 1.3 Contributor`（模型 ID `muse-spark-1.3-contributor`）。当时项目仅按有状态方式调用所有 Responses 模型，遇到了两层兼容问题：

1. 历史中明确记录 HTTP 400：`reasoning.encrypted_content` 不能与 `previous_response_id` 同时使用。原适配器每次请求都会要求返回推理密文，续聊又附带父响应 ID，该平台拒绝这一组合。“准备重新同步”清除父 ID，所以能暂时回答一次。
2. 用同一模型进行独立测试，移除冲突的 include 后，平台又立即返回 `referenced response not found or expired`。因此仅修复参数冲突仍不够：本次实测中该平台/模型无法通过刚生成的响应 ID 继续对话。这不能解释为正常的 30 天保留期限，也没有证据表明是 API key 失效。

历史还出现过“该 Go 模型使用请求数据训练，需要在工作区隐私设置中允许”的平台策略错误。它与当前续接失败是不同问题；后续独立调用已成功，本次没有修改工作区隐私设置。

项目应当允许每个模型/角色明确选择上下文管理方式，避免依赖供应商的远端存储实现。现已增加无状态兼容选项，同时保留已有有状态默认。

## 已实现的行为

| 项目 | 无状态兼容模式 |
| --- | --- |
| 请求 | `store: false`；始终不携带 `previous_response_id` |
| 历史 | 每次发送当前有效本地上下文，包括历史问题、助手输出、工具调用和结果；压缩后使用本地摘要 |
| 系统提示 | 当前系统 instructions、条件更新及工具定义仍随每次请求生成和发送 |
| 原生输出 | 对相同端点/账户/模型回传已确认的完整原生输出，保留可用的推理密文、assistant phase、call_id；不依赖远端通过 item ID 恢复内部推理 |
| 工具执行 | 继续使用现有权限确认与执行账本；只有完整响应通过验证并提交后才执行；旧工具不会因为重发历史而执行 |
| 图片、文档 | 继续走原有 Responses 内容转换及工具结果配对 |
| 压缩 | 自动、任务内、手动压缩只做现有本地摘要；不请求平台远端压缩 |
| 断流 | 未开始返回时沿用可重试网络错误策略；收到 ID/部分内容后中断则明确结束，不查询未保存响应、不自动重跑未确认工具 |
| 重启 | 遗留未完成无状态模型请求不再尝试 GET 查询，下一次从已确认本地上下文继续；权限等待仍走已有权限任务恢复机制 |
| 切换模式 | 保留本地记录，清除旧模式的远端链状态，下次任务生效 |
| 前端格式 | 正文、思考、工具、用量继续采用应用原有 SSE 格式；Responses 浅粉色保留 |

有状态模式还新增一项受限兼容处理：仅在平台明确拒绝上述两个参数组合时，去掉链上请求的 `reasoning.encrypted_content` include 并重试相同输入/父 ID。后续同来源链请求复用这个设置；普通参数错误不触发此处理。明确的父响应不存在错误仍最多重建一次，不把所有模型自动切成无状态。

## 如何启用

在当前会话底部点击“参数”，选中聊天模型，在“Responses · 上下文管理”选择“无状态兼容 · 每次发送上下文”，点击“确定”。此后直接正常发送，无需先点“准备重新同步”。压缩、标题和子智能体角色使用这类模型时，也应在各自页签设置相应模式。

可在 `models.json` 的模型或 provider 中设置 `"responsesMode": "stateless"` 作为默认；角色参数 `parameter.responses.responses_mode` 可单独覆盖。下拉框“跟随模型配置”使用该默认。旧配置不填时仍是有状态。此次没有自动更改用户的真实模型配置或会话选择。

无状态每次重发上下文，上传量、输入费用及本地请求审计的体积可能增加；本地压缩仍然生效。`store: false` 表示本次请求不要求平台保存可续接响应，不能替代供应商自己的日志/隐私政策。

## 同日最新会话的回退核对

再次检查同一会话：`_meta.model_selection.chat_model.parameter.responses.responses_mode` 明确为 `stateful`，`_meta.responses_state.responses_mode` 也为 `stateful`。最新请求审计全部为 `store: true`，没有自动切换为无状态。

实际出现的请求顺序为：

1. 带父 ID 和密文 include，发送 1 项新增输入，平台拒绝参数组合。
2. 移除密文 include，保留同一父 ID 和 1 项输入，平台返回父响应不存在/过期。
3. 去掉父 ID，发送本地完整原生窗口加新增输入，共 10 项，仍为 `store: true`；成功保存新 ID。
4. 后一轮再次用新 ID 发送 1 项输入，平台仍返回父响应不存在/过期；本次调用重建为 13 项完整输入后成功。

因此回答成功依赖于有状态模式的“单次恢复重建”，不能据此认定该平台该模型的远端 ID 链工作正常。它在当次恢复请求中回传了完整上下文，但配置仍是有状态，下次还会先尝试 ID 续接。该端点下的 Muse Spark 当前更适合显式选择无状态兼容；这是平台/模型组合的实测结论，不是对所有供应商同名模型的能力断言。

UI 已将状态说明改为“上次请求已完成；下次仍尝试响应 ID 续接”，恢复提示也明确说明未切为无状态。选择无状态后，重新同步来源和按钮完全隐藏；粉色主题覆盖背景与文字，不再继承 Messages 的绿色底色/标题。

## 验证

使用用户已配置的同一模型、独立临时会话与合成数据真实调用，不上传该历史会话全文，不执行文件修改或终端命令。

| 调用 | 发送 input items 数 | 结果 |
| --- | --- | --- |
| 1 | 1 | 保存随机测试口令，回复成功 |
| 2 | 4 | 回传历史与原生输出，准确复述口令 |
| 3 | 7 | 返回一个 `diagnostic_echo` 函数调用 |
| 4 | 11 | 回传完整历史、原生调用及本地模拟的工具结果，准确返回最终口令 |

四次均为 `store: false`，均无父响应 ID，未调用查询接口。测试发现此模型只接受 `tool_choice: auto`；改用项目默认的 auto 后工具闭环成功，不能据此保证其支持强制指定函数。

自动测试覆盖模式优先级、角色参数、连续历史/原生推理/phase、图片工具结果配对、账户隔离、模式切换、本地压缩、手动压缩参数、异步和同步断流、连接重试、重启遗留请求清理、受限 include 兼容与父 ID 失效重建。前端覆盖模式草稿、角色切换、模型默认、保存/恢复默认，以及原有全套测试。

## 实现位置

- `config.py`、`env_manager.py`：请求/角色/模型模式配置与回显。
- `chat/protocols/responses.py`、`responses_content.py`：模式解析、未保存响应验证、完整原生输出回传。
- `factory/agent_runtime/responses_context.py`：无状态全量上下文构造。
- `chat/responses_client.py`、`chat/protocols/responses_store.py`：网络恢复、受限兼容处理、私有账本与模式状态。
- `factory/agent_runtime/responses_compaction.py`、`routers/chat_router.py`：自动/手动压缩按生效模式执行。
- `H5/index.html`、`H5/js/app/model_panel.js`：四角色上下文模式下拉框及说明。
- `test/test_responses_stateless.py`：新增专项测试。

## 官方依据

- [Conversation state](https://developers.openai.com/api/docs/guides/conversation-state)：手动管理输入输出数组、`store: false` 与 ID 续接是不同选择。
- [Reasoning](https://developers.openai.com/api/docs/guides/reasoning)：无状态工具循环应保存并回传原生推理项；有密文时保留密文。
- [Compaction](https://developers.openai.com/api/docs/guides/compaction)：独立压缩返回的窗口需要完整保留；兼容平台的实现能力仍需单独验证。本次无状态兼容采用现有本地摘要。
