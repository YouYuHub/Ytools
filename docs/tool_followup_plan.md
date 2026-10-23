# 工具进一步优化与验证（2026-10-23）

按阶段实现并验证，模型说明只描述已提供的能力。

| 阶段 | 内容 | 状态 |
|---|---|---|
| 基线 | 文件发现、文件工具及工具合约回归 | 102 项通过 |
| 1 | 内容搜索按需扫描、准确的部分计数语义、可选 .gitignore | 114 项通过；100 个匹配文件的首批读取 1 个，显式完整计数读取 100 个；忽略规则在文件统计未变时仍校验内容 |
| 2 | 大文本压缩快照、配额、可靠回退与有界差异展示 | 126 项通过；含编码/混合换行精确回退、损坏/外部修改拒绝覆盖、空文件存在状态、配额与分页、事件循环可继续响应 |
| 3 | stdin 输入、可选 PTY、任务归属/超时/取消与模型工具接线 | 112 项组合回归通过；含 Windows cmd/PowerShell 真实输入与 PTY、工作进程退出自动回收、重试去重和审批契约、临时监督文件删除竞态 |
| 收尾 | 主/子任务、权限、协议、文档与前端回归 | Windows 定稿后全量 2016 项通过、11 项跳过；前端 654 项通过，资源哈希通过，90 个本地文档链接通过 |

每阶段记录实际测试结果。交互终端继承前台任务生命周期，独立后台服务保持原有语义。

## 已完成的接口

- `search_files` 默认按需扫描首批结果，`scan_pending` 与 `total_matches_exact` 区分待扫描和最终计数；需要全量计数时显式 `count_total=true`。搜索和枚举可选 `respect_gitignore=true`。续读仍有时间、文件数、输出和缓存预算。
- 历史快照每端 64 MiB（UTF-8），大文本 gzip，会话快照配额 256 MiB、自动记录每文件最多 512 版本，已有快照可复用。大文件保留整文件回退和历史内容分页，编辑页使用有界提示视图，拒绝保存截断正文。
- `run_command` 增加 `interactive/tty`，启用后提供 `write_stdin`；勾选命令工具会同时注入查询、输入和停止接口。参数、系统提示、前端标题及使用简表已同步。
- Windows 身份检查增加退出状态判定，防止把“进程已退出但句柄仍存在”当成活进程；PTY 读取采用非阻塞底层读取，使完全静默的程序也能及时检查生命周期。
- 文件历史 HTTP 读写移到工作线程；大文件解压、差异和磁盘操作不占用主事件循环。

## 复核命令

```powershell
python -m pytest test/test_search_incremental.py test/test_file_discovery.py test/test_builtin_file_tools.py test/test_tool_design_contract.py -q
python -m pytest test/test_file_snapshot_large.py test/test_file_history.py test/test_file_history_result.py test/test_file_history_router.py test/test_file_history_async.py test/test_context_patch.py test/test_builtin_file_tools.py -q
python -m pytest test/test_command_interactive.py test/test_command_leases.py test/test_command_incremental.py test/test_builtin_command_tool.py test/test_terminal_jobs.py test/test_tool_permissions.py -q
npm.cmd test --prefix H5
python -B H5/version_assets.py
python -m pytest test/test_frontend_asset_versions.py -q
```

Windows 最终全量命令：收集 `test/test_*.py` 并加 `test/ai_tool_simple_test.py`，执行 `python -m pytest <上述测试文件> -q --durations=10`。本轮日志保存在 `tmp/tools-release-backend-tests.log`；阶段日志为 `tmp/search-stage-tests.log`、`tmp/snapshot-stage-tests.log`、`tmp/interactive-combined-tests.log`、`tmp/tools-final-frontend-tests.log`。Linux 两组日志为 `tmp/linux-tool-tests.log`、`tmp/linux-tool-api-tests.log`。`tmp/` 为本机验证产物目录，不作为部署依赖。

这些回归有重叠，通过数量不能相加。Windows 与 Ubuntu WSL 已做真实文件和进程验证；macOS 尚无执行环境。PTY 页面按文本日志展示，尚无全屏终端模拟器。快照与磁盘文件分别原子替换，不承诺跨文件/崩溃事务。

Ubuntu WSL（Linux / Python 3.13.14）工具组合回归：161 项与 4 项子断言通过，覆盖搜索/忽略规则、文件工具、快照回退、补丁、管道 stdin、PTY、超时、停止和工作进程退出回收。测试在独立 Linux 虚拟环境运行，依赖按项目锁文件约束，不加载用户配置或调用模型。

Linux 接口/权限/工具合约/分发/提示词追加回归：121 项与 2 项子断言通过，1 项按平台跳过；含文件历史 REST 分页、异步处理、终端列表/日志/停止与新工具参数校验。两组 Linux 测试文件互不重复，合计 282 项通过、1 项跳过。

## 启用

部署环境安装 `requirements.lock.txt` 后重启后端、刷新页面；本次新增 `pathspec`，Windows 增加 `pywinpty`，其他依赖保持原锁定版本。当前执行环境已安装这两个依赖。工具仍按会话勾选，说明见 [简表](builtin_tools.md)、[终端](terminal_management.md)、[文件历史](file_diff.md)。
