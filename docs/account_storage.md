# 账号数据与维护

## 数据边界

HTTP 请求和后台模型任务使用服务端身份；并行工具调用会继承该身份。生产请求必须携带身份才能进入存储层。启动恢复和离线维护使用带用途说明的 `system_access(store, reason=...)`。无身份的独立 Python 库调用回退根目录单用户布局，不应直接暴露给 HTTP。

会话 ID 全局唯一，资源归属不会随删除释放。账号身份与文件位置同时校验；目录分开不等于主机执行沙箱。管理员管理账号，不自动获得其他账号聊天内容的访问权。

## 会话归属与目录

会话归属唯一真源是账号库 `session_catalog(id,owner,state,updated)` 单表；注册与鉴权单事务完成，不再双写其他表。

| 状态 | 含义 |
| --- | --- |
| reserved | 已预登记，还没有历史文件 |
| active | 历史已写入 |
| missing | 曾有历史，现在文件缺失；保留归属等待对账 |
| deleting | 删除中 |
| cleanup_pending | 部分删除失败，需继续清理；阻止读取及重新创建 |
| deleted | 删除完成，保留 ID 与归属墓碑 |

所有会话统一使用 `history_files/users/<user_id>/sessions/<session_id>/`，其中包含原格式的 `*_chat.jsonl`、`files/` 或 `uploads/<upload_id>/`（附件和文件版本）、`sidecars/`（检查点、计划、协议状态）。分组注册表与归档标记同样按账户存放：`history_files/users/<user_id>/session_groups.json`、`history_files/users/<user_id>/session_archives.json`，锁按账户拆分。提示词与任务模板使用各自的账号子目录。

删除即删除整个账号会话目录；删除失败后可再次删除同一会话。对账仅调整明确的生命周期，不自动认领无主数据、不删除文件、不因为预登记没有历史就释放 ID。缺失数据应人工核实。

会话 ID 清洗唯一真源是 `memory.storage_layout.safe_session_id`（保留 Unicode 词字符/空格/点/横线，其余转下划线并拦截 `..`），各模块统一引用，不再各自实现。

## 离线备份、恢复与对账

先停止全部服务和写入工具。维护命令会检查后端监听端口、存活任务，并设置启动互斥标记。自定义脚本、另一实例或远程共享磁盘的写入者也必须先停止。

以下命令使用项目 Python，在项目根目录执行（Linux 将解释器改为 `python`）：

```powershell
C:\Python314\python.exe scripts/manage_storage.py backup --archive tmp/full-backup.zip
C:\Python314\python.exe scripts/manage_storage.py verify --archive tmp/full-backup.zip
C:\Python314\python.exe scripts/manage_storage.py restore --archive tmp/full-backup.zip --target tmp/restore-drill
C:\Python314\python.exe scripts/manage_storage.py reconcile
C:\Python314\python.exe scripts/manage_storage.py reconcile --apply
```

备份包含历史、附件、文件版本、协议记录、控制状态数据库、账号库、`mfa.key`、提示词、模板、配置和 `.env`。SQLite 使用在线备份 API 生成一致性快照；全套跨数据库一致性依靠停写保证。备份不包含程序代码、旧初始化密码文件、旧私有备份、临时锁和 SQLite 原始 WAL/SHM，也不自动包含工具任意访问的外部工作目录。程序代码应另行保存匹配或兼容的版本；外部项目文件应另行备份。

备份和恢复目录仅当前 OS 用户可读；归档本身没有密码加密，异机存放应使用受保护或加密存储。不要发送给其他人。校验会检查完整清单、路径、大小和 SHA-256；恢复同时检查所有 SQLite 完整性。恢复必须使用全新的目录，不能覆盖运行数据。先用兼容版本的程序代码搭建独立新项目，再将恢复的数据放入新项目。恢复自定义安全目录时，账号库和密钥统一落在恢复副本的 `history_files/state/security/`；启动新实例前必须修改 `.env` 中原实例的绝对路径、端口和 origin，避免连接真实数据。

账号指纹覆盖用户、会话归属与状态、MFA、恢复码、额度、偏好与改名历史，用于校验备份与实例的一致性。

## 容量与公平并发

管理员在“账号与安全→账号管理→管理”配置账号总存储（GiB）、模型并发、每日请求和 token 额度。旧额度更新接口省略新增字段时保留已有设置。普通账号默认 5 GiB、2 个并发模型请求；管理员容量和账号并发默认不限，但仍受实例总并发约束。

总存储包含该账号会话目录、附件、文件版本、检查点、协议回放、提示词和模板；共享账号库、运行日志、外部工作区不归入个人容量。页面展示的是文件逻辑大小，不是操作系统分配块大小。

容量检查使用一次元数据遍历，不读文件正文，不逐文件解析真实路径，也不跨请求缓存总量；外部工具及检查点改动在下次检查时重新计入。重叠目录只计一次，符号链接和目录联接不跟随，权限错误不会当作零容量。计量与排队计时、基准和验证见 [改进记录](archive/performance_paging_release_2026-10-25.md)。

大附件和文件快照在账号跨进程写锁内检查剩余容量，上传超限清理半成品。每次新模型请求也检查总存储。已开始的回复、历史与恢复检查点允许写完，避免丢失结果，因此此额度是应用准入额度，不能替代服务器磁盘硬配额。转码缓存、数据库页增长、并发历史落盘可使最终大小略高于额度；达到额度后新模型请求和大文件写入被拒绝，读取、导出、删除仍可用。多租户公开部署建议再配置 Linux 文件系统/容器硬配额。

`YTOOLS_MODEL_CONCURRENCY` 控制实例同时向供应商发起的模型请求数，默认 8。主模型、子任务、标题和压缩调用共用租约；按账号最近获准时间轮转，账号内按入队时间排列。等待会响应任务停止，不持有数据库锁，也不发送上游请求；取消、正常结束和异常会释放租约。进程消失或 PID 被复用后可回收租约，不能仅凭请求时间长回收存活任务。此机制治理模型请求，不承诺 CPU、终端子进程或磁盘 I/O 的操作系统级公平调度。
