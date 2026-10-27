# 账号存储收敛记录（2026-10-26）

本次只保留一套最优逻辑，不再兼容旧版双布局。生产请求恒走账号目录；无身份离线库调用回退根目录单用户布局。

## 收敛清单

| # | 冗余 | 收敛后 |
|---|---|---|
| 1 | `_safe_session_id` 四份拷贝（chat_memory/file_memory/file_history/storage_layout._safe） | 唯一真源 `memory.storage_layout.safe_session_id`，各模块统一引用 |
| 2 | `resources` + `session_catalog` 双写会话归属 | 会话只用 `session_catalog(id,owner,state,updated)` 单表；`resources` 表删除（旧库一次性抢救后 DROP）；`owner/owned/register/resource_key` 删除 `kind` 参数 |
| 3 | `session_groups.json` / `session_archives.json` 全局共享 + 全量过滤 + 全局锁 | 按账户分文件 `history_files/users/<uid>/session_groups.json|session_archives.json`，锁按账户拆分；损坏抛错不再静默；删除冗余 `order` 字段，列表按创建时间稳定排序 |
| 4 | 会话列表逐会话查库（N+1） | 单次 catalog 查询 + 单次归档快照，内存过滤后再读首行 |
| 5 | `_check_legacy_upload_owner` 冷启动全量扫首行 | 删除；各账户附件目录物理隔离，不存在跨账号目录共享 |
| 6 | `file_history_sync.js` 隐式依赖 Storage 包装链 | 显式经 `YtoolsAuth.rawStorage + storageKey()` 读写 |
| 7 | `security_client.js` + `account_preferences.js` 双 prototype 补丁语义纠缠 | 职责分离：前者是唯一的隔离补丁点并暴露 `rawStorage`，后者显式读写 + 链式捕获变更 |
| 8 | `task_costs` / `permission_runtime` 无归属校验，直查 `task_id` 可越权 | `task/approval/create/pending/claim/status/summary/continue/finish/reserve/settle` 等统一经 `authorize_session` 校验归属会话 |
| 9 | `history_pages` 进程缓存键只有路径 | 缓存键改为 `(owner, path)` 实现账户隔离 |
| 10 | 死代码：`storage_layout.install/_STORE`、`api.js` 的 `ytools-api-base` 覆盖、`theme.js` 旧键、`main.py` 注释快照 | 全部删除；同源部署直接用 `location.origin` |
| 11 | 旧版兼容税：`_existing_alias` 大小写扫描、`remap_legacy_attachment`、`adopt-legacy`、`storage_migration`、`validate_migration_archive`、import 时迁移副作用 | 全部删除；`security_fingerprint` 改为覆盖用户、会话归属与状态、MFA、额度、偏好 |

## 行为变化

- 新会话注册单事务完成（`reserved`），不再分两步写两表。
- 删除会话即删除整个 `users/<uid>/sessions/<sid>/` 目录（含 jsonl、附件、侧车、协议记录）。
- 导入：他人 ID 永不覆盖，自动时间戳另存；全新 ID 注册后写入。
- 根目录散落的 `*_chat.jsonl` 不再被生产读取（对账 `unowned` 只扫描 `users/` 下无登记文件）。
- 分组接口不再返回 `order` 字段；前端按创建时间稳定排序。
- 离线脚本命令删除 `migrate` / `rollback-journal` / `adopt-legacy`，保留 `backup/verify/restore/reconcile`。

## 验证

- 后端：账号存储 19、会话列表/归档/搜索 39、隔离与分享 32、分页 6、MFA/改名/预算 49、分享导入 41、分组 6、路由/偏好 27、浏览器全流程 19（含 9 个页面 flow）通过。
- 前端：全量 853 通过。
