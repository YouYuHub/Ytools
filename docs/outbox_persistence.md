# 暂存消息持久化（Outbox）

输入框上方的暂存区（引导/队列/待消费提示行）原来只在前端内存，
刷新页面即丢失；切走期间被消费的引导还会因 SSE 中止而残留幽灵行。
本机制把“未发送”条目落盘到会话侧车，打开会话时恢复；“已提交待消费”
以后端实时注入队列为准。一期落地文本+引用，二期补齐媒体/文档。

## 数据模型

侧车文件：`history_files/.../sidecars/<session>_chat.jsonl.outbox`
（与 `.todo` 同目录、管理口径一致；删会话时一并删除，见
`ChatMemoryManager.delete_chat_session_file` 的后缀表）。

```json
[
  {"id": "o_lxyz...", "kind": "steer", "text": "...",
   "media": [{"media_ref": "media://a.png", "name": "a.png", "kind": "image"}],
   "quotes": [{"text": "..."}],
   "docs": [{"filename": "a.pdf", "uploaded": true}],
   "created_at": 1730000000.0}
]
```

- `id`：前端生成（`o_` + 时间36进制 + 随机），按 id 幂等 upsert。
- `kind`：仅 `steer`（引导槽，单条）/`queue`（队列槽，可多条）。
- `media`：只存元数据（`media_ref/name/kind`，无 File）；发送时
  `send()` 对有 `media_ref` 的条目跳过上传直接复用引用。
- `docs`：只存 `{filename, uploaded}`；文档内容在会话 file_memory
  （磁盘持久化），发送时跳过已上传项直接使用。
- `text` 上限 50000 字符；非法条目（无 id/坏 kind/超长）后端丢弃，
  路由层转 400（schema 先行 422）。

## 后端接口（`routers/outbox_router.py`，前缀 `/outbox`）

| 接口 | 说明 |
|---|---|
| `GET /outbox?session_id=` | 返回 `{entries:[...]（侧车，按 created_at 排序）, injected:[...]（实时注入队列快照）}` |
| `POST /outbox/submit` | 新增/更新一条（body 含 session_id/id/kind/text/media/quotes） |
| `POST /outbox/remove` | 按 ids 删除（body 含 session_id/ids），返回 `{removed:n}` |

存储实现：`ChatMemoryManager.get_session_outbox / upsert_session_outbox /
remove_session_outbox`（`memory/chat_memory.py`，原子写 + 空删文件，
与 todo 侧车同口径；读损坏返回空列表不抛错）。

`injected` 快照：`factory.chat_factory.peek_injected_messages(session_id)`
只读、不出队。inline 模式直接读 `_SESSION_STREAMS` 的 deque；
worker 模式（默认）经新增 `peek_inject` IPC 命令向 worker 查询
（`proxy.peek_injected_messages` + worker 侧 `peek_inject` 分支，
5 秒超时失败返回 None）。返回 `[{message_id, text, quotes, media}]`，
text 由 content 经 `content_part_to_text` 提取，media 从部件的
`media://` 引用提取（`[{media_ref, kind}]`，供恢复行缩略图）。
查询失败时接口返回 `injected: null`（未知），前端跳过对账——
不得把未知当作空，否则会把仍在后端队列的消息误判丢失并转存，
导致重复发送。

## 前端状态机（`H5/src/app/composer.ts`）

内存仍是三槽：`state.steerMessage`（单条）、`state.pendingQueue`（数组）、
`state.injectedPending`（数组），渲染入口 `renderPendingOutbox()`。

提交时先上传换引用再登记：队列提交对媒体调
`App.prepareSteerContent`、对文档调 `uploadStashDocs`（失败抛错→
仅内存暂存 + toast）；引导提交先传文档再走注入，注入失败回退暂存时
附件已带引用，可直接登记。文档随提交快照（`staged` + 本会话过滤，
与发送口径一致），上传后进入 file_memory（会话共享上下文）。

| 事件 | 本地 | 后端 |
|---|---|---|
| 提交引导（注入失败/不可用→暂存） | 写入 steerMessage（合并旧条目时沿用首个 id） | upsert（steer） |
| 提交队列 | push（创建时带 id） | upsert（queue） |
| 自动派发/手动发送 | 先取走（防重入），再 `App.send` | 取走时 remove；发送失败重试时重新 upsert（见下） |
| 撤回（×） | 有源文件：移除 + 放回输入框；无源文件（恢复条目）：只删除 + toast | remove |
| 后端消费（SSE `message_injected`） | 清行 + 插气泡 | 后端出队（本来就如此） |
| 打开会话 | 拉取合并（见下） | — |
| 删会话 | — | 侧车随会话删除 |

失败重试不丢标签：`chat.ts queueDirectRetry` 按 `_originKind` 放回原槽
（引导回引导槽，槽被占则按时间顺序合并；带编辑轮次/文档的不进引导槽，
走队列）。重试放回后重新 upsert（发送前已 remove）。

带本地文件（未上传 `File`）的条目不登记——File 无法序列化；
等上传完成/发送成功后自然消失（与一期前行为一致）。纯引用条目可登记。

## 打开恢复合并规则（`App.loadSessionOutbox`，`openSession` 内调用）

1. 侧车条目按 id 合并：本地已有（更新、可能含 File）优先保留本地；
   本地没有则采用后端（引导槽被占时保留本地）。
2. `injected` 行按 `message_id` 对账（任务运行中只保留/恢复，不丢弃不转存——
   消费事件可能还在路上，且历史尚未落盘本轮，此时核销必误判；等任务结束
   后再对账；`running` 由接口返回）：
   - 后端队列仍在 → 保留/恢复显示；
   - 后端无 + 历史已有该文本 → 已消费，丢弃残留行（含 detach 中止
     SSE 后丢失消费事件的情况）；
   - 后端无 + 历史无 → 后端丢失（重启/任务结束时未消费），转回**内存**
     引导槽 + toast，不落盘；标记 `_converted`，自动派发跳过，需手动发送
     （避免误判导致重复发送）。
   消费事件（SSE `message_injected`，含重放）是权威信号：到达时按 id 清除
   同 id 的暂存行（误转存残留），只按 id 不按文本。
3. 无 id 的历史行保留（无法核对，手动处理）。

恢复后若会话空闲，既有补发链路（`maybeAttachRunningStream` →
`flushPendingMessages`）会立即自动发送——与“队列在任务完成后自动发送”
语义一致：刷新/切回只是恢复了本来就该发的条目。如需先改后发，
请在发送前点 × 撤回（内容放回输入框）。

暂存行显示附件/文档计数徽标；图片（最多 4 张）显示 24px 缩略图，
内存条目用本地预览（dataUrl/objUrl），恢复条目用远端 URL
（`App.resolveMediaSrc` → `/file/get_session_media`），点击进媒体预览框。
音频/视频只显示计数。

## 限制与后续

- 多标签页并发：后写覆盖（单 id upsert），可接受。
- 暂无 TTL（刻意不做）：条目纯文本 KB 级，会话本身也不设 TTL；
  静默删除用户排队的消息比保留更差。陈旧条目打开即自动发送
  （符合队列语义），如需先改后发请点 × 撤回。
- 带本地文件（未上传）的条目不登记——File 无法序列化；提交时上传
  失败会回退内存暂存并 toast，刷新丢失（与持久化前行为一致）。

## 测试

- 后端 `test/test_outbox.py`：侧车增删改查/非法拒绝/损坏读空/
  路由直调/注入快照（inline + worker fake proxy + 空 + 超时 None，
  含媒体引用提取）/docs 字段 roundtrip/删会话清理侧车。
- 前端 `H5/test_h5/steer_media.test.js`：提交/注入/撤回既有用例 +
  恢复合并、丢失转暂存、派发来源标记、队列上传登记、文档上传登记、
  无源文件删除-only、缩略图源选择。
