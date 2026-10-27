# 工具改进计划与验证记录

按可独立验证的阶段实施；模型说明只描述已经实现的能力。

| 阶段 | 内容 | 验证 |
|---|---|---|
| 基线 | 当前文件、版本链和终端 | 127 项通过 |
| 1 | 主任务/子任务显式返回文件版本记录状态，页面同步展示 | Python 136 项通过；前端相关 6 项通过 |
| 2 | 文件枚举、相对路径 glob、可续读的搜索结果 | 相关 90 项通过 |
| 3 | 终端按游标获取增量输出、有界等待 | 相关 46 项通过 |
| 4 | 命令短暂等待后返回活动任务，保持总超时及取消语义 | 命令、权限等 73 项通过；含 Windows cmd/PowerShell 真实运行、超时和按子任务取消 |
| 5 | 多文件上下文补丁及大文件读取复用 | 已完成，专项覆盖编码/混合换行、回滚、并发冲突及索引复用；相关 49 项通过，后续边界回归 77 项通过 |
| 收尾 | 工具选择/权限/协议回归，简短使用说明与资源版本同步 | 后端工具链 468 项通过；追加终端回归 95 项通过；最新前端全量 644 项通过；14 个工具 Schema 与分发入口一致 |

本页保留首轮实现与验证记录。交互 PTY、快照配额、历史分页和增量扫描已进入 [进一步优化](tool_followup_plan.md)，当前接口以 [工具简表](../builtin_tools.md) 为准。

首轮全量回归：前端 640 项通过；后端 1963 项通过、11 项跳过，1 项旧提示词断言仍要求通过终端枚举文件。已将该断言改为验证 `list_files`、补丁和短等待规则，最新工具链回归已包含该用例并通过。各轮验证有重叠，不应将通过数量直接相加。

## 使用新能力

重启后端并刷新页面。启用文件搜索即可使用 `list_files`；需要多文件补丁时在工具配置勾选 `apply_patch`。终端不新增选择项，沿用 `run_command/poll_command/stop_command`。当前用法见 [工具简表](builtin_tools.md)。

## 复核命令

工具链合并回归（PowerShell）：

```powershell
$toolCheckFiles = @(
  'test/test_builtin_file_tools.py', 'test/test_builtin_command_tool.py',
  'test/test_file_history.py', 'test/test_file_history_result.py',
  'test/test_file_discovery.py', 'test/test_text_index.py', 'test/test_context_patch.py',
  'test/test_command_incremental.py', 'test/test_command_leases.py',
  'test/test_tool_permissions.py', 'test/test_tool_design_contract.py', 'test/test_tool_executor.py',
  'test/test_session_tool_selection.py', 'test/test_terminal_jobs.py',
  'test/test_sub_agent.py', 'test/test_sub_agent_reliability.py',
  'test/test_task_templates.py', 'test/test_system_prompt.py',
  'test/test_messages_api.py', 'test/test_responses_api.py'
)
python -m pytest @toolCheckFiles -q
```

追加终端回归：`python -m pytest test/test_command_leases.py test/test_command_incremental.py test/test_builtin_command_tool.py test/test_terminal_jobs.py test/test_tool_permissions.py -q`。

前端：在 `H5/` 执行 `npm test`。JS 修改后在根目录执行 `python -B H5/version_assets.py`，并以 `python -m pytest test/test_frontend_asset_versions.py -q` 核对资源版本。

## 首轮设计边界（第二轮变更见文首链接）

- 文件枚举随内容搜索启用；`apply_patch` 是单独可选工具。旧的读取、精确替换、写入模式和终端尾部采样保留。
- 搜索当前仍在扫描预算内计数，再分页捕获结果；5 分钟缓存有内存上限，文件变更或进程重启后重新查询，不承诺跨进程游标迁移。默认排除规则不读取 `.gitignore`。
- 大文本索引使用自动回收的临时文件，最多缓存 8 份、合计 64 MiB；超过配额可回退普通读取。缓存键包含文件版本和编码。
- 补丁限制为 20 个文本文件、单文件 8 MiB / 20 万行、修改前后合计 64 MiB，使用严格上下文匹配；不提供模糊替换或跨文件崩溃事务保证。
- 短等待命令仍受总超时及任务生命周期约束；独立后台任务按原有方式持久运行。Windows 进行了真实运行验证；Linux/macOS 尚未在真实主机验证。

## 首轮后续计划（已进入第二轮）

1. 按使用需求设计交互 stdin / 可选 PTY，独立验证 ConPTY、信号和进程回收。
2. 已结束命令入口、日志容量统计与可预览的保留策略。
3. 搜索按需计数、可选忽略规则，以及跨进程续查需求评估。
