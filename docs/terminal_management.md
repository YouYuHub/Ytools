# 终端输入、输出与任务管理

模型使用简表见 [内置工具](builtin_tools.md)。前台 `timeout_seconds` 默认 120、范围 1–1800 秒，包括预处理和启动；后台任务不使用该超时。停止聊天或子任务会向正在执行的前台命令发送取消信号并回收进程树，已分离的后台任务继续通过 `job_id` 管理。回收失败会明确反馈，不把“已请求终止”当作“确认终止”。

Windows 上短暂占用命令记录／输入回执文件时，原子替换会有界重试；监督器退出前允许回执线程完成该提交。重试不重新发送 stdin，原有 `request_id` 防重复语义保持不变。

## 短等待和增量输出

- 模型通常只需 `run_command(command=...)` → `poll_command(job_id)` → 按需 `stop_command(job_id)`。默认短等 1000ms；尚在运行时返回 `job_id`，短命令尽量在首次调用中返回最终状态。命令仍属前台任务，受总超时约束；任务结束、停止、所属子任务退出或工作进程消失时回收。独立监督进程负责超时和所属进程检查。
- `yield_time_ms` 为 0–30000 毫秒，默认 1000，0 立即返回；显式指定时与 `background=true` 互斥。启动和预处理可能额外占用时间，故它不代表整个工具调用严格在该时长内返回。Windows cmd 的本地 head/tail 兜底需要完整输出，此少见情况默认仍等结束；可直接运行上游命令以短等返回。
- `poll_command(job_id)` 自动续取新增 `output`，默认无输出时最多等 1 秒，`wait_seconds=0–30` 可调。`status=running` 或 `has_more=true` 时继续查询；命令退出不表示输出已读完。以 `status/exit_code/timed_out` 判断执行结果，不以是否有输出判断成功。
- 位置按会话、任务和代理隔离，并持久化到命令记录；首次 run 返回的输出已经计入位置。`poll_command` 与 `write_stdin` 共用位置，同一消费者并发读取串行处理；页面查看日志不会推进模型位置。每个命令最多保存 64 个消费者，超出后淘汰最久未使用者，该消费者下次从头读。网络丢包等情况仍可显式重读，不承诺消息端到端只交付一次。
- `cursor=""` 从头重读，传 `next_cursor` 从指定位置读；显式读取也更新后续自动位置。自动位置遇到日志替换/截断时从头读取并返回 `output_reset=true`；显式失效游标报错并提示从头重读。位置保存失败时仍返回输出/输入回执及 `cursor_saved=false`，后续显式传 `next_cursor` 继续。未完成的多字节字符延后解码。
- 乱码时可指定 `encoding`，后续自动沿用；`encoding=""` 恢复自动识别，不固定自动探测出的编码，以支持 Windows 混合编码输出。
- `tail_lines=1–200` 显式选择尾部预览，不推进自动位置，不能与 cursor/wait_seconds/encoding 混用。HTTP 页面和直接调用共享底层 `command_jobs.action` 继续默认尾部预览；直接调用内部 `execute_run_command` 保持旧的阻塞语义，模型入口统一使用新默认值。
- `background=true` 保持独立服务语义，任务结束或停止聊天不会回收它。两类命令共用现有任务 ID、会话归属和日志管理入口。

前端“+”后的 SVG 入口只在当前会话存在仍在运行的命令任务时显示；全部停止、结束或关闭后隐藏。它管理本会话通过内置工具创建的后台或短等待命令，不枚举系统里的其他终端，也不接管外部 MCP 启动的进程。

## 交互输入与 PTY

- `run_command(..., interactive=true)` 保持 UTF-8 管道 stdin 可写，默认短等 1000ms 返回；`tty=true` 自动启用交互，提供 120×30 伪终端，适用于 REPL。Windows 需要项目依赖 `pywinpty`，POSIX 使用系统 PTY。两者均与 `background=true` 互斥，继承前台任务生命周期和总超时。
- `write_stdin(job_id, chars, cursor?, wait_seconds?, eof?, request_id?)` 原样发送并自动续取输出，默认无输出时最多等 1 秒，通常不用传 cursor。提交一行须显式包含换行（如管道 `\n`，终端回车 `\r`）。单次最多 16384 字符/64KiB；`eof=true` 关闭管道 stdin，PTY 拒绝此选项。无输入且不带 request_id 时等同 `poll_command(job_id)`，也可读取非交互命令。
- 输入或查询输入回执限制在同一会话、任务和代理，发送前检查进程身份；只读日志按会话校验。ask 权限模式下，非空输入或 EOF 须审批，并绑定审批时的终端身份；空输入不另审批。
- 结果含 `input_status=accepted|queued|writing|failed|not_sent`、`input_bytes`、`request_id` 和增量输出字段。`accepted` 只表示已写入 stdin，不代表程序处理成功。只传 `job_id/request_id` 可查回执，不重发输入；需重试时沿用同一 `request_id` 与原文，相同 ID 不重发、内容不同会拒绝。每个终端最多 1024 个输入请求；输入队列正文被处理后删除，回执仅保留摘要与状态。进程异常退出时 `writing` 可能表示部分写入，不能换 ID 自动重发。
- PTY 输出可含 ANSI 控制码、输入回显和屏幕重绘；当前页面按文本日志展示，不实现终端屏幕模拟。界面保留查看、下载与停止入口；模型通过工具发送输入。控制字符由程序及所在终端决定。

## 页面行为

- 打开/切换到会话时先检查一次列表；只有当前会话存在运行中的任务才每 3 秒刷新列表，最后一个任务结束/停止/关闭后撤销定时器。只有面板展开且选中运行任务时才每秒刷新输出，收起面板立即撤销输出定时器；页面隐藏、离开时两种定时器均暂停。
- 空闲状态通过主智能体和子智能体的 `run_command/poll_command/write_stdin/stop_command` 结果重新检查列表，发现新任务后恢复轮询。页面重新可见/聚焦或从浏览器后退缓存恢复时也检查一次，以便发现其他页面启动的任务。无关工具与其他会话的命令结果不触发请求；不会靠历史工具卡片的渲染反复唤醒。
- 发现请求期间的新触发合并成一次后续检查，避免空列表旧快照遗漏刚启动的任务。没有已知任务时，首次发现请求失败后最多再重试两次（合计最多 3 次请求），之后等待新的事件；已知运行任务遇到短暂网络故障仍保留入口并继续重试，不把请求失败视为任务结束。
- 切换会话立即清空列表并收起面板，新对话未分配会话 ID 时不请求列表。聊天结束、刷新页面或切换会话不会停止仍在运行的命令；回到所属会话可继续管理。
- 展示完整命令、shell、工作目录、会话 ID、PID、状态、运行时长和退出码。
- **停止**终止任务进程树并移出运行列表；**停止并关闭**还会标记记录已关闭。两者均保留日志供模型稍后读取。面板右上角的**收起**只关闭面板。
- 日志显示最近 2,000 行，最多 131,072 字符，每次磁盘读取约 256 KiB；避免长期服务大日志全部进入内存。**完整日志**以文件下载方式获取。日志仍按现有编码检测显示，不执行日志里的 HTML。
- 向上浏览会暂停输出显示；勾选**跟随输出**恢复。终端输出缓冲取决于命令自身，例如 Python 服务可使用 `python -u` 或 `PYTHONUNBUFFERED=1`。
- 模型调用 `stop_command`、用户点击停止、命令正常结束/失败或被外部命令终止后，均不再计入按钮数量；不要求用户另行点击“关闭记录”。运行列表只包含 `running` 状态；`completed/failed/stopped/unknown` 的日志仍可通过 `poll_command` 或原日志路径读取。

## 平台实现

- Windows 沿用 WMI → wscript → 隐藏 shell 的启动链，合并 stdout/stderr 到日志文件，结束标记记录退出码；停止使用 `taskkill /T /F`。
- Linux/macOS 在独立进程组中启动轻量 Python 监督进程，等待 shell 退出并写结束标记；停止向进程组发送 SIGTERM，2 秒后仍未退出则 SIGKILL。默认 shell 为 sh，修正空参数前缀时缺少 `-c` 的后台执行问题。
- 命令注册信息保存在服务端用户目录 `~/.mcp_bg_logs/bg_*.meta.json`，主进程、工作进程共同读取。修改操作使用跨进程锁和原子替换。
- 新任务记录进程身份：Windows 创建时间、Linux 启动 ID 和进程起始时钟、macOS `ps` 起始时间。停止前再次核对，避免误杀复用 PID 的进程。旧版没有身份字段的运行记录不提供停止，关闭仅隐藏记录，并在页面说明。
- 未关闭记录和运行中任务不受 24 小时清理影响。关闭后日志至少保留 24 小时，再在后续命令触发清理时回收；共享锁文件不删除。

交互输入通过任务内模型工具处理，未新增任意输入的 HTTP 入口。当前服务端的认证和网络访问控制同样适用于现有接口。

## HTTP 接口

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/terminal/jobs` | 本会话未关闭且仍运行的记录，返回 `{jobs: [...]}`；不含日志正文 |
| GET | `/terminal/jobs/{job_id}` | 状态及有界 `output_tail`、`output_truncated` |
| POST | `/terminal/jobs/{job_id}/{operation}`（`operation=stop`） | 停止进程树，移出运行列表，保留日志 |
| POST | `/terminal/jobs/{job_id}/{operation}`（`operation=close`） | 运行中先停止，再隐藏记录；旧版无法验证身份时仅隐藏 |
| GET | `/terminal/jobs/{job_id}/log/download` | 下载当前日志文件 |

上述所有 HTTP 接口均要求非空查询参数 `session_id`，例如 `/terminal/jobs?session_id=yt-example`。列表按会话筛选；状态、停止、关闭和日志下载也校验记录归属。缺少会话归属的旧记录不会被分配给任意会话。注册表仍由服务端工作进程共同读取，这只是存储机制，不代表页面需要跨会话共享。

记录字段包括 `job_id`、`command`、`pid`、`shell`、`cwd`、`session_id`、`created_at`、`status`、`exit_code`、`can_stop`。状态为 `running/completed/failed/stopped/unknown`；进程已退出且无结束标记时为未知状态，并从运行列表隐藏。请求仅使用工具生成的 job_id，不接受任意 PID 或日志路径。模型工具的 poll/stop 同样校验会话归属。

## 常用调用简化的验证（2026-10-24）

1. 接入默认短等和自动位置后，首轮相关回归 126 项通过。
2. 补齐输入回执只读查询、兼容路径及异常恢复后，Windows 相关回归 170 项通过；Ubuntu WSL 86 项、2 项子断言通过。覆盖真实 cmd/PowerShell/sh、管道与 PTY、输入去重与回执、超时/停止/所属进程回收、输出分页、跨任务隔离、跨工作进程续读、并发与日志重置。
3. 前端全量 755 项通过，资源内容哈希校验通过。隔离页面验证工具标题仅保留原生 hover 时间提示，输入回执和失败状态可见，控制台无警告/错误；不调用模型或真实用户终端。
4. 后端全量首轮 2047 项通过、11 项跳过，3 项审批用例仍读取旧 stdout 字段。同步为 output，并显式设置单次等待完成的预算后，审批/工作进程组合 21 项全部通过。最终补充的 5 项异常及兼容测试包含在上述 170 项相关回归中；未在这些测试断言更新后重跑整套后端。

复核命令：

```powershell
python -B -m pytest test/test_command_reads.py test/test_command_incremental.py test/test_command_leases.py test/test_command_interactive.py test/test_builtin_command_tool.py test/test_tool_design_contract.py test/test_system_prompt.py test/test_terminal_jobs.py -q
python -B -m pytest test/test_permission_decision_api.py test/test_permission_worker.py -q
npm.cmd test --prefix H5
python -B -m pytest test/test_frontend_asset_versions.py -q
```

阶段日志见本机 `tmp/terminal-usability-focused.log`、`tmp/terminal-usability-linux.log`、`tmp/terminal-usability-h5.log`、`tmp/terminal-usability-full-python.log`、`tmp/terminal-usability-permissions.log`。macOS 无执行环境，尚未实测。无新增依赖；重启后端启用终端新默认，刷新页面加载前端更新。
