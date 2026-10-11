# 后台终端管理

前端“+”后的 SVG 入口只在当前会话存在仍在运行的后台命令时显示；全部停止、结束或关闭后隐藏。它管理本会话通过内置 `run_command(background=true)` 创建的后台命令，不枚举系统里的其他终端，也不接管外部 MCP 启动的进程。

## 页面行为

- 列表每 3 秒刷新，面板打开时所选命令输出每秒刷新；页面隐藏时暂停轮询。切换会话立即清空列表并收起面板，新对话未分配会话 ID 时不请求列表。聊天结束、刷新页面或切换会话不会停止仍在运行的命令；回到所属会话可继续管理。
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

这是后台命令的日志查看与生命周期管理，未增加 PTY、交互式 stdin 或终端模拟器。当前服务端的认证和网络访问控制同样适用于这些接口。

## HTTP 接口

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/terminal/jobs` | 本会话未关闭且仍运行的记录，返回 `{jobs: [...]}`；不含日志正文 |
| GET | `/terminal/jobs/{job_id}` | 状态及有界 `output_tail`、`output_truncated` |
| POST | `/terminal/jobs/{job_id}/stop` | 停止进程树，移出运行列表，保留日志 |
| POST | `/terminal/jobs/{job_id}/close` | 运行中先停止，再隐藏记录；旧版无法验证身份时仅隐藏 |
| GET | `/terminal/jobs/{job_id}/log/download` | 下载当前日志文件 |

上述所有 HTTP 接口均要求非空查询参数 `session_id`，例如 `/terminal/jobs?session_id=yt-example`。列表按会话筛选；状态、停止、关闭和日志下载也校验记录归属。缺少会话归属的旧记录不会被分配给任意会话。注册表仍由服务端工作进程共同读取，这只是存储机制，不代表页面需要跨会话共享。

记录字段包括 `job_id`、`command`、`pid`、`shell`、`cwd`、`session_id`、`created_at`、`status`、`exit_code`、`can_stop`。状态为 `running/completed/failed/stopped/unknown`；进程已退出且无结束标记时为未知状态，并从运行列表隐藏。请求仅使用工具生成的 job_id，不接受任意 PID 或日志路径。模型工具的 poll/stop 同样校验会话归属。
