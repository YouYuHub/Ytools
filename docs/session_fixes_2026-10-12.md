# 2026-10-12 会话问题修复

## 核对记录

`yt-2026-10-11_23.13.10_108_chat.jsonl` 的两轮均已保存正文和一个思考事件，但因缺少 thinking 签名在响应收尾时报错。工具问题实际见于 `yt-2026-10-11_22.44.37_315_chat.jsonl`；该会话最后一轮同样只有一个思考事件。用户补充的 Deepseek 截图说明重复思考涉及共用前端管线。

## 流式思考与 Messages 兼容

前端原先将任何不含非空 reasoning_content 的帧视为思考结束。空 delta、usage、签名活动等控制帧穿插在思考之间，导致后续增量新建思考框。现在只有正文、有效工具调用、completion 完成/截断、错误等真正的阶段边界会结束思考；工具轮次间的真实分段仍保留。普通发送和重连共用实现。

完整 Messages 响应中的无签名 thinking 可显示，但标记 `replay_eligible=false`，不写入原生回放 sidecar，不将这类块发回供应商；工具参数完整且合法结束时可以提交，后续通过普通工具消息或事实重建继续。官方有签名响应继续保持原样回放。EOF、未闭合块、无效工具 JSON、错误停止原因和网络失败的拒绝/重试逻辑保持有效。

参数面板随所选协议切换控件和颜色：Messages 采用绿色滑块，常用参数独立编辑，extra_body 折叠为高级扩展。模型 `parameterCapabilities` 显式为 false 的字段在面板隐藏/禁用，并在后端最终组包时过滤旧会话的遗留值，解决已弃用 temperature 引发的 400。手动思考预算受 max_tokens 约束。

## 工具修复

- `search_files` 扫描上限、读取失败或跳过大文件时明确提示部分结果，零命中不再声称不存在。`scan_truncated` 表示扫描上限，`scan_complete` 表示候选范围内是否完整读取，`output_truncated` 单独表示展示预算/区块数上限。返回真实命中数与已展示命中数。恰好达到 max_results 且无遗漏时不误报截断。
- 默认排除 history_files/tmp/vendor 及已有隐藏、依赖、构建目录。需要搜索这些目录时直接指定 dir_path，或传 exclude_dirs 覆盖默认列表；空数组包含全部目录。扫描深度和 file_pattern 仍限定本次搜索范围。
- 普通文本模式不会自动变成正则；形似 `A|B` 且零命中时提示显式设置 is_regex=true。上下文行仅将实际命中行标记为 `>`，长行提示使用 read_file。
- `fetch_url` 有界分块下载，在中途断流时保留已收到内容并提示部分响应；新增提取文本 offset/next_offset 续读和动态页面空正文说明。下载/解压硬上限仍保留，工具不执行 JavaScript。
- 大 diff 的模型返回增加短小的实际修改预览，包含位置和删除/新增行；完整 diff 继续由前端独立接收。

## 验证边界

新增回归覆盖控制帧穿插、真正的阶段边界、无签名流式/非流式响应与工具续接、搜索假阴性/展示截断/目录排除、网页传输中断/字符续读和修改预览。历史验证快照（2026-10-12）：当时相关后端回归 202 项通过、9 项因缺少可选 PyMuPDF 跳过，前端 280 项通过；这些计数不代表当前工作区。随后对修改预览/网页分页收尾改动复核，并额外验证下载/解压上限。

使用项目现有 OpenCode Message / Qwen3.7 Max 配置完成了一次最小真实流式调用：收到 36 字符思考与 2 字符正文，返回 `finish_reason=stop`、`response_complete=true`、`replay_eligible=false`，约 4.9 秒完成，未再出现签名收尾错误。工具续接由固定响应回归验证，尚未用真实供应商执行工具闭环。当时的浏览器工具策略禁止访问本地 file:// 页面，因此在 2026-10-12 本次核验中未完成参数面板视觉验证；该记录不表示当前浏览器策略或现行界面状态。控件切换、保存、默认值与绿色样式由当时测试/编译核验。

协议依据：[Messages](https://platform.claude.com/docs/en/api/messages/create)、[Thinking](https://platform.claude.com/docs/en/build-with-claude/thinking)、[模型弃用](https://platform.claude.com/docs/en/about-claude/model-deprecations)。有签名 thinking 的回传须保持原样；兼容平台缺少签名时只保留可展示与可执行的通用数据。
