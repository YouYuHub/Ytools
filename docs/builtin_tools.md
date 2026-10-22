# 内置工具使用简表

只调用当前请求 `tools` 中的工具。参数须为完整 JSON 对象；可选参数通常省略即可。布尔值用 `true/false`，行号用整数，时间用有限数字。参数错误会指出字段，修正后重试。

| 任务 | 工具与用法 |
|---|---|
| 维护计划 | `todo_write(todos)` 提交完整列表，最多 20 项；步骤含 `content/status`，`id` 可省略，返回后保持稳定。`[]` 清空 |
| 缺少信息或需要确认 | `ask_user(questions)` 提出 1–5 题；可给 `options`、`multiple`。调用后等待用户回答，同轮其他工具不执行 |
| 独立子任务 | `sub_agent(task, todo?)` 写全目标、绝对路径、事实、约束和所需证据；同轮子任务并发，父模型收到最终报告 |
| 新建文本 | `write_file(path, content, mode="create")`；目标已存在时报错，自动创建父目录 |
| 覆盖或追加 | `write_file(..., mode="overwrite"/"append")`，必须明确模式；已有文件可带 `expected_hash` |
| 修改局部 | `edit_file(path, old_string, new_string)`；多处用 `edits`，不要混用。出现多处匹配时加长原文，或明确 `replace_all=true` |
| 读取文本 | `read_file(path, start_line=1, limit=200)`；按 `next_start_line` 续读。长行续读保留原 `start_line`，传 `char_offset=next_char_offset` |
| 跨文件搜内容 | `search_files(pattern, dir_path?)`；默认普通文本，正则设 `is_regex=true`。`file_pattern` 筛选文件名，不枚举文件；枚举使用终端 |
| 读取文档 | `read_document(filename)` 默认自动选择。`text` 用 `offset/limit`；`table` 用 `next_row/next_sheet`；PDF 选页同时给 `page_start/page_count` |
| 查看媒体 | `read_media(references)`，每条回复一次、最多 5 个来源；可用 `media://`、本地路径、HTTP(S) 直链，检查 `loaded/skipped` |
| 执行命令 | `run_command(command, work_dir?)`；每次新 shell，语法须匹配 `shell`，`cd` 不会影响下一次调用 |
| 长任务 | `run_command(..., background=true)`，保留返回的 `job_id`；`poll_command(job_id, tail_lines=30)` 看进度，`stop_command(job_id)` 停止 |

## 常见续读与限制

- 文件路径相对于会话工作目录；旧编码文本显式给 `encoding`。`read_file.content_hash` 是全文指纹，可传给编辑或覆盖工具。
- 文件读取、编辑及写入结果上限为 64 MiB；单次写入/替换文本最多 2 Mi 字符，批量编辑文本合计最多 4 Mi 字符。批量替换按顺序执行，任一项失败不提交。
- 搜索默认深度 6、单文件 2 MiB、候选 3000 个、扫描 10 秒。正则单行还有执行预算。`scan_complete=false` 或 `truncated=true` 时先缩小范围或续查；不能判定目标不存在。
- 默认跳过隐藏、依赖、构建目录及 `history_files/tmp/vendor`。直接指定目标目录，或显式 `no_default_excludes=true`；`exclude_dirs=[]` 不关闭默认排除。
- 文本行号从 1 开始，文档字符偏移从 0 开始，PDF 页码从 1 开始。表格 `header_row=0` 表示无表头；PDF 选页失败会直接报错。
- 本地文档文本/表格解析最多 10 MiB；原生读取使用配置中的原生文档配额，硬上限 64 MiB，需当前模型支持该格式。
- 原生文档只发送到下一次模型请求。视频旧画面会回收，回看需重读。本地/会话视频按返回的 `video.read.end` 续读；网络 URL 不接受时间区间。
- 命令前台默认 120 秒、允许 1–1800 秒，预算包括预处理和启动；进程回收可能额外耗时。后台不使用此超时，停止聊天后仍保留后台任务。
- 后台轮询是最新输出采样，不保证覆盖两次查询之间的所有日志。完整日志见 `output_path`；超出 `read_file` 上限时通过终端分块筛选。

## 最小示例

```json
{"path":"src/main.py","old_string":"return 1","new_string":"return 2"}
```

多处编辑改用：

```json
{"path":"src/main.py","edits":[{"old_string":"first","new_string":"updated"},{"old_string":"second","new_string":""}]}
```

后台任务：

```json
{"command":"python -u worker.py","background":true}
```

随后把实际返回的 `job_id` 传给 `poll_command`，不要把示例值当作真实任务 ID。
