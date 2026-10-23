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
| 多文件修改 | `apply_patch(patch)`，需在工具配置中启用；支持 Add/Update/Delete File，最多 20 个文件。先读原文，提供唯一上下文；结果含逐文件统计，失败检查 `files/rollback_complete`，`needs_attention` 文件须先重读核对 |
| 读取文本 | `read_file(path, start_line=1, limit=200)`；按 `next_start_line` 续读。长行续读保留原 `start_line`，传 `char_offset=next_char_offset` |
| 查文件路径 | `list_files(pattern="*", dir_path?)`；如 `*.py`、`src/**/*.py`，不读文件内容；随 `search_files` 一起启用 |
| 跨文件搜内容 | `search_files(pattern, dir_path?)`；默认普通文本，正则设 `is_regex=true`。`file_pattern` 筛选文件名或相对路径 glob |
| 读取文档 | `read_document(filename)` 默认自动选择。`text` 用 `offset/limit`；`table` 用 `next_row/next_sheet`；PDF 选页同时给 `page_start/page_count` |
| 查看媒体 | `read_media(references)`，每条回复一次、最多 5 个来源；可用 `media://`、本地路径、HTTP(S) 直链，检查 `loaded/skipped` |
| 执行命令 | `run_command(command, work_dir?)` 每次新 shell，默认短等 1000ms；未结束返回 `job_id`。语法须匹配 shell，cd 不影响下次调用 |
| 继续查看 | `poll_command(job_id)` 自动续取新增输出。`status=running` 或 `has_more=true` 时继续查询；前台总超时仍生效 |
| 连续输入 | `run_command(..., interactive=true)`；需 TTY 的 REPL 加 `tty=true`。用 `write_stdin(job_id, chars="内容\n")` 输入并续取输出；管道读取至 EOF 的程序再传 `eof=true` |
| 独立服务 | `run_command(..., background=true)`，不受前台总超时/任务结束回收；`poll_command(job_id)` 查看，`stop_command(job_id)` 停止 |

## 常见续读与限制

- 文件路径相对于会话工作目录；旧编码文本显式给 `encoding`。`read_file.content_hash` 是全文指纹，可传给编辑或覆盖工具。
- 修改结果的 `history_recorded` 表示是否已记录回退版本；`false` 时检查 `history_reason`，不代表文件写入失败。快照每端最多 64 MiB（UTF-8），超过 256 Ki 字符时压缩保存；会话快照配额 256 MiB，自动记录每文件最多 512 版本。配额满时保留旧版本并明确报告。
- 文件读取、编辑及写入结果上限为 64 MiB；单次写入/替换文本最多 2 Mi 字符，批量编辑文本合计最多 4 Mi 字符。批量替换按顺序执行，任一项失败不提交。
- 补丁单文件最多 8 MiB / 20 万行，修改前后内容合计最多 64 MiB。先校验全部文件，再逐文件提交；跨文件不是操作系统级事务。提交失败会尝试回滚，外部并发修改导致回滚不完整时会明确报告，不能直接重试旧补丁。不支持 Move 和模糊匹配。
- 补丁 `files[]` 含逐文件增删行数与版本记录结果；行数 `null` 表示大文件未统计，不是没有修改。完整差异只供页面展示；失败时先核对 `needs_attention=true` 的文件。
- 搜索默认深度 6、单文件 2 MiB、候选 3000 个、扫描 10 秒。正则单行还有执行预算。`scan_complete=false` 或 `truncated=true` 时先缩小范围或续查；不能判定目标不存在。
- 文件枚举与内容搜索用相同查询加 `cursor=next_cursor` 续读；游标保存 5 分钟，已扫描文件/目录改变或服务进程重启后重新查询。搜索默认按需扫描，`scan_pending=true` 表示尚待续扫，`total_matches` 仅为已扫描计数；末页可能无命中。需要完整计数时设 `count_total=true`，仍检查 `total_matches_exact/scan_complete`。
- 搜索和枚举可设 `respect_gitignore=true`，读取搜索根及下级 `.gitignore`，不读取祖先、全局 Git 配置；默认不读取。忽略规则不能重新包含已被默认排除的路径。相对路径 glob 从 `dir_path` 起算，`**/` 可以匹配零层或多层目录。
- 默认跳过隐藏、依赖、构建目录及 `history_files/tmp/vendor`。直接指定目标目录，或显式 `no_default_excludes=true`；`exclude_dirs=[]` 不关闭默认排除。
- 文本行号从 1 开始，文档字符偏移从 0 开始，PDF 页码从 1 开始。表格 `header_row=0` 表示无表头；PDF 选页失败会直接报错。
- 本地文档文本/表格解析最多 10 MiB；原生读取使用配置中的原生文档配额，硬上限 64 MiB，需当前模型支持该格式。
- 原生文档只发送到下一次模型请求。视频旧画面会回收，回看需重读。本地/会话视频按返回的 `video.read.end` 续读；网络 URL 不接受时间区间。
- 命令前台默认 120 秒、允许 1–1800 秒，预算包括预处理和启动；进程回收可能额外耗时。后台不使用此超时，停止聊天后仍保留后台任务。
- 终端默认自动续读，无新增输出时最多等 1 秒；`wait_seconds=0–30` 可调。`cursor=""` 重读，传 `next_cursor` 可指定位置，之后继续自动续读；`tail_lines=1–200` 只预览尾部，不推进位置，不能与 cursor/wait_seconds/encoding 混用。乱码可设 `encoding`，后续沿用，空字符串恢复自动识别。完整日志见 `output_path`。
- 交互模式默认短等 1000ms，与后台互斥。输入仅限当前任务/代理，ask 权限模式下发送输入须审批；空输入只读取。`write_stdin` 原样发送 UTF-8，最多 16384 字符/64KiB，换行需显式包含；只传 job_id/request_id 查回执，重试沿用 request_id 和原文，勿换 ID 自动重发。PTY 不支持 `eof=true`，程序退出或 `stop_command` 结束终端。

## 最小示例

```json
{"path":"src/main.py","old_string":"return 1","new_string":"return 2"}
```

多处编辑改用：

```json
{"path":"src/main.py","edits":[{"old_string":"first","new_string":"updated"},{"old_string":"second","new_string":""}]}
```

上下文补丁（JSON 的 `patch` 字段中保留这些换行）：

```text
*** Begin Patch
*** Update File: src/main.py
@@
-return 1
+return 2
*** Add File: notes.md
+Done
*** End Patch
```

后台任务：

```json
{"command":"python -u worker.py","background":true}
```

随后把实际返回的 `job_id` 传给 `poll_command`，不要把示例值当作真实任务 ID。
