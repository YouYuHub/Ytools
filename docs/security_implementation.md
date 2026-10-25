# 账号与 Linux 远程访问安全

实施日期：2026-10-24。设计依据见 [安全方案](security_account_remote_access_plan_2026-10-24.md)。本文件记录当前行为；原方案及 CSV 的“无认证”等描述保留为实施前快照。

## 适用范围与边界

支持本地使用、Linux 专用实例和可信小团队。普通成员使用无工具聊天；管理员管理实例并使用主机工具。操作员需服务器设置 `YTOOLS_TRUSTED_OPERATORS=true` 才可执行工具，创建/提升此角色还需管理员明确确认信任。

**尚未提供相互不信任用户的 OS 执行沙箱**。终端、文件工具和 MCP 仍使用服务账号的 OS 权限，工作目录与审批不能替代容器/虚拟机。陌生用户不可提升为管理员/操作员；需要此类执行时使用独立虚拟机及独立服务配置。普通成员的所有主机工具调用由后端拒绝。

初次代码实施未改动运行服务。后续已按用户授权完成本地管理员初始化、备份、旧数据迁移与重启，详见 [启用记录](account_activation_2026-10-24.md) 和 [账号操作指南](account_quickstart.md)。目标 Linux 服务器的域名、证书、防火墙及 Docker 没有在本机操作中配置。

## 已实现

| 层级 | 防护 |
| --- | --- |
| 账号 | 关闭注册；CLI 创建首管理员；Argon2id；管理员/操作员强制 TOTP，成员可自选 MFA（服务器恢复后所有角色必须重绑定）；加密 MFA 密钥、一次性恢复码、防重放、登录限速、设备撤销、初始密码强制修改、近期验证和审计 |
| 登录 | 不透明随机 Cookie，数据库仅保存凭证哈希；HttpOnly、SameSite=Lax；生产使用 Secure 的 `__Host-` Cookie；空闲 30 分钟、绝对 12 小时；MFA/近期验证轮换凭证 |
| HTTP | 默认拒绝未登录和未登记路由；校验 Host、生产 HTTPS、Origin/Referer、CSRF；拒绝跨站及 WebSocket；限制请求体；私有响应禁止缓存 |
| 数据 | 会话、搜索索引、分组、归档、提示词、模板、附件和版本记录按账号授权；检查请求体、重复查询参数及批量 ID；旧数据必须显式迁移；管理身份不会自动获得他人会话的 HTTP 读取权限 |
| 任务 | 身份由服务器 IPC 传入；退出登录不取消后台任务；禁用/变更角色撤销登录、阻止后续操作并尝试停止可识别任务；SSE 每 2 秒核对登录有效性 |
| 额度 | 跨会话、worker、辅助模型及重试原子累计；成员/操作员默认每日 1000 次、500 万 tokens，管理员默认不限；账号页面可调整 |
| 预览 | 拒绝私网/回环/链路本地/元数据地址；验证全部 DNS 结果并固定实际连接 IP；HTTPS 验证原域名；禁止自动跳转；内网例外须精确 origin 白名单 |
| 浏览器 | 同源 API、自动 CSRF、账号缓存分区；跨标签页可刷新 CSRF，账号切换停止旧页面处理；主动内容强制附件，PDF 保留受限同源预览；Canvas 全部消息核对 iframe 来源及画布 ID |

Google / Microsoft 验证器扫码绑定已加入登录及账号页面，二维码在浏览器本机生成，保留手动备用和自动清理；见 [扫码绑定说明](mfa_qr_enrollment.md)。

账号数据库/MFA 密钥、模型密钥配置及权限控制资源加入工具保护范围，但可信操作者通过终端仍具有服务账号权限。普通成员无主机读取能力。

每账号每分钟限 1800 个 API 请求、180 个写请求，每小时 120 个新会话；普通成员最多登记 10000 个会话。JSON 上限 8 MiB，原始上传入口上限 600 MiB，具体文件类型沿用更小限制。上传按 8 MiB 向上取整计量，每账号滚动 24 小时 2 GiB，中断也计入。SSE 不逐帧读写数据库。

token 额度使用服务估算及上游回执，不是供应商账单。发送前预留输入估算和输出上限；未发送则释放；结果未知/进程崩溃保守保留；已知用量结算实际值。每日额度按 UTC 重置，单会话预算不能突破账号额度。原任务费用账本继续保留。

禁用不会撤销已发生的外部效果。脱离监督器的独立进程及无法核对身份的旧终端需人工检查；停止失败记入审计的 `needs_attention`，不会盲目按 PID 杀进程。

## 本地启用与旧数据

备份 `.env`、`setting/`、`history_files/`、`prompt/md_files/`；在没有历史写入任务时迁移。使用独立虚拟环境，避免改动运行中的解释器：

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.lock.txt
.\.venv\Scripts\python scripts/manage_accounts.py init-admin --username admin
.\.venv\Scripts\python scripts/manage_accounts.py adopt-legacy --username admin
# 核对预览后执行：
.\.venv\Scripts\python scripts/manage_accounts.py adopt-legacy --username admin --apply
.\.venv\Scripts\python scripts/serve.py
```

密码由终端隐藏输入并二次核对，无默认口令。打开 `/ui/`，首次管理员登录绑定验证器、保存恢复码。侧栏左下角“设置 → 账号与安全”可创建/启停账号、调整角色与额度、重设其他账号初始密码、撤销设备和查看审计。新建/重设初始密码首次登录后必须更改。

迁移默认预览，只登记指定账号的未归属会话/分组，并复制旧模板、提示词到私有目录；不重写 JSONL、附件、版本、协议侧车或压缩数据。已有归属不转移。提示词/模板冲突中止；中途失败时，依据 `adoption-<用户ID>.json` 清单核对，不要覆盖已有私有数据。

离线维护：

```text
python scripts/manage_accounts.py list
python scripts/manage_accounts.py create-user --username member
python scripts/manage_accounts.py create-user --username operator --role operator --trusted
python scripts/manage_accounts.py reset-password --username admin
python scripts/manage_accounts.py reset-mfa --username admin
```

这些命令需要服务器本机权限。MFA 重设撤销登录，下一次所有角色都需重新绑定。登录页提供设备丢失指引，账号页可查看剩余恢复码数量并验证身份重新生成、下载；详见 [设备丢失恢复指南](account_recovery.md)。账号库版本 4 增加恢复后重绑定标记，正常升级不改变现有绑定。不要删除数据库或 `mfa.key` 来“重置”；账号、归属与密钥必须一致备份。SQLite 在线备份使用备份 API，或停服务后完整备份含 WAL 的目录。

## Linux HTTPS 部署

模板见 [deploy/linux](../deploy/linux/)：环境示例、systemd 单元和 Nginx。采用单 API 进程、同机代理，多主机/多进程调度需另行设计。

1. 建立低权限用户 `ytools`，项目放 `/opt/ytools/app`，虚拟环境 `/opt/ytools/venv`，工作区 `/var/lib/ytools/workspaces`。服务用户不需要 sudo、宿主 SSH 密钥或 Docker socket。
2. 按锁文件安装依赖，复制环境示例到 `/etc/ytools/ytools.env`，替换真实 HTTPS origin，设为 `root:ytools 0640`。账号目录用 `/var/lib/ytools/security`。
3. CLI 初始化/迁移必须载入与 systemd 相同的环境并使用 `ytools` 身份，避免创建另一份账号数据库。可用 `sudo -u ytools sh -c 'set -a; . /etc/ytools/ytools.env; set +a; cd /opt/ytools/app; /opt/ytools/venv/bin/python scripts/manage_accounts.py init-admin --username admin'`。该配置文件仅由管理员维护，密码不放参数/环境中。
4. 配置证书与 Nginx 域名，先执行 `nginx -t`，再启用代理和 systemd。模板关闭代理缓冲、查询参数访问日志和缓存，保留 SSE，限制登录速率及连接数；安排证书续期。
5. 后端只监听回环。启动脚本拒绝生产模式非回环监听、任意代理信任 `*`、非 HTTPS origin；只信任同机代理。防火墙不开放 48621，公网仅放行 HTTPS 与受控维护入口。
6. 开放入口前用两个临时账号验收：匿名/跨账号被拒绝；登录/MFA/聊天/SSE/上传 Range 正常；退出、禁用、重连及备份恢复正常。

systemd 收窄服务账号权限；为兼容配置编辑和工具行为，应用及工作区仍可写。这不是账号之间的执行沙箱。**目标云服务器的 Nginx、证书及公网连通性尚未验收。**

OS 环境优先提供 `DEFAULT_SERVICE_HOST`、`DEFAULT_SERVICE_PORT`、`DEFAULT_CHAT_WORK_DIR` 及 `YTOOLS_*` 安全配置。被环境固定的工作目录不能用项目 `.env` 覆盖。`YTOOLS_ALLOWED_MEDIA_ORIGINS` 默认空，只在确需内网媒体时列出精确 scheme/host/port。

## 验证记录

测试只使用临时账号、资源与模拟模型，不读取真实聊天正文或请求真实模型。

- 账号、HTTP、数据隔离分步验证：11、21、31 项通过；相关旧功能回归 115 项通过。
- worker/任务用量组合 47 项通过；网络和额度专项 15 项通过。
- Windows 全量：2131 passed、11 skipped、1 个 CMD PTY 用例失败；最新安全改动、模型选择和交互终端模块合并复测 **82 passed**。
- Linux WSL：新版 FastAPI 专项 **91 passed**；锁定框架 FastAPI 0.143.3 / Starlette 1.8.0 专项 **102 passed**。验证发现并修复延迟路由兼容问题。
- 最终 Windows 安全、迁移、部署入口及资源哈希汇总 **70 passed**；最终 Linux 安全、工作目录、模型选择、任务用量与工具权限汇总 **124 passed**。
- 前端身份专项 **6 passed**，覆盖 CSRF、缓存、会话登记、跨标签页轮换及账号切换；最终 H5 全量 **765 passed**。Python 路由/安全模块语法检查及 CLI 帮助入口通过。

测试入口 `test/test_security_*.py`、`H5/test_h5/security_client.test.js`。新增业务 API 必须登记授权策略，并通过真实应用的路由覆盖测试。Linux 本地验证不能代替云服务器或 OS 沙箱验收。
