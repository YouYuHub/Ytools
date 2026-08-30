# 压缩逻辑说明与举例

本文描述 `agent_tool_sse` 的上下文压缩体系：**什么时候压缩、压缩什么、压成什么样、模型最终看到什么**。
压缩只替换"下一次发给模型的工作上下文"，原始对话永远完整保存在历史 JSONL 中（含工具原始输出、思考过程、媒体引用），前端回放不受压缩影响。

核心实现分布：

| 文件 | 职责 |
| --- | --- |
| `factory/agent_runtime/context_compaction.py` | 压缩主流程（跨轮 / 单轮 / 手动）、配置解析、超大结果拒绝 |
| `memory/chat_history_format.py` | 摘要渲染与解析、轮次窗口切分、轮次→上下文消息展开 |
| `memory/chat_memory.py` | 持久化、`context_summary` 存取、上下文视图与 token 统计 |
| `memory/chat_round_store.py` | `chat_round` 轮次与压缩事件结构（落盘格式见 `docs/chat_momory_template.json`） |
| `factory/agent_runtime/chat_runtime.py` | 轻量 token 估算 |
| `factory/chat_factory.py` | 各触发点的编排（任务开始 / 每次模型调用前 / 收尾 / 首调用预算检查） |
| `routers/chat_router.py` | 手动压缩接口 `/chat_context/compact_manual` |

---

## 1. 基本原则

- **原始数据不丢**：JSONL 中的轮次、工具结果、思考过程、`media://` 引用始终原样保留；压缩只影响发给模型的消息序列。
- **摘要必须是普通文本**：压缩模型被要求输出按标题分段的中文备忘（不依赖 JSON mode / 结构化输出），解析失败也能整体降级为纯文本摘要。
- **压缩模型可独立配置**：`models.json` 顶层 `model_selection.compaction_model`（仅支持 chat-completions 协议）；未配置或不可用时回退为当前聊天模型。
- **降级链**：压缩模型调用失败 → 换当前聊天模型重试一次（同一模型不重复尝试）→ 仍失败抛 `ContextCompactionError`，**当前任务终止**（不做"节选降级"，避免把残缺上下文喂给模型）。
- **摘要一次性写入**：压缩产物在完成后原子落盘，任务中断不会留下半成品摘要；中断遗留的未闭合 `start` 事件在下次任务开始时被标记为 `aborted`（前端对应条目失效，随后按需重新压缩）。

---

## 2. 配置项与派生阈值

### 2.1 环境变量（`.env`，均有前端配置入口）

| 变量 | 默认值 | 含义 |
| --- | --- | --- |
| `HISTORY_COMPACT_KEEP_ROUNDS` | 20 | 模型上下文保留的**总轮次窗口**；`0` 表示无限窗口（只按阈值压缩） |
| `HISTORY_COMPACT_TRIGGER_RATIO` | 0.8 | 压缩触发比例（上限 0.95） |
| `HISTORY_COMPACT_SUMMARY_BUDGET_RATIO` | 0.2 | 累计摘要总预算占聊天窗口的比例（上限 0.5） |
| `HISTORY_COMPACT_REJECT_OVERSIZED_FACTOR` | 1.5 | 超大工具结果拒绝系数（`<=0` 关闭，上限 10） |
| `HISTORY_COMPACT_MAX_OVERSIZED_REJECTIONS` | 3 | 连续超大拒绝达到该次数时终止任务 |
| `HISTORY_TOOL_RESULT_RETURN_MAX_LENGTH` | -1 | 历史轮次单个工具结果回传长度：`0` 不回传、负数完整回传、正数截断到前 N 字符 |

### 2.2 派生阈值（运行时计算）

设 `聊天窗口` = 当前聊天模型 `maxInputTokens`（默认 8192），`压缩窗口` = 压缩模型 `maxInputTokens`：

| 阈值 | 公式 | 用途 |
| --- | --- | --- |
| 上下文压缩阈值 | `max(1024, min(聊天窗口, 压缩窗口) × trigger_ratio)` | 跨轮压缩与单轮压缩共用；取两窗口较小者，保证摘要能同时放进两边上下文 |
| 摘要总预算 | `max(1024, 聊天窗口 × summary_budget_ratio)` | 累计摘要的输出上限；也是手动压缩的门槛/预算 |
| 单段摘要输出上限 | `max(64, min(8192, 模型 maxOutputTokens, 输出预算))` | 防止单段摘要无限膨胀 |
| 超大结果拒绝阈值 | `max(1024, min(聊天窗口, 压缩窗口) × oversized_reject_factor)` | "先于压缩"拦截超大工具结果 |

**举例**（当前 `.env`：trigger_ratio=0.5，其余默认；聊天/压缩窗口均为 128k）：

- 压缩阈值 = 128k × 0.5 = **64k tokens**
- 摘要总预算 = 128k × 0.2 = **25.6k tokens**
- 超大拒绝阈值 = 128k × 1.5 = **192k tokens**

### 2.3 token 估算口径（轻量估算，非 tokenizer）

- 文本：`ASCII 字符数 ÷ 4 + 非 ASCII 字符数`（中文按 1 字 ≈ 1 token）；
- 每条消息额外 +4；`tool_calls` 结构整体按 JSON 序列化后估算；
- **多模态部件（图片/音频/视频，无论 data URL 还是 `media://` 引用）固定按 1024 tokens 计**，避免 base64 数据按字符估算虚高数万倍误触发预算检查；
- 请求上下文 = 消息 tokens + 运行时系统提示 tokens + 工具定义 tokens。

---

## 3. 模型看到的历史视图（压缩前后）

每次请求的消息序列由主系统提示 + 历史部分 + 当前轮消息组成。历史部分有两种形态：

### 3.1 未压缩模式（从未触发过压缩）

按总轮次窗口 `keep_rounds` 回传最近 N 轮**完整对话**（最旧的超出窗口舍弃），无摘要、无问题索引。
轮次展开规则（`round_entry_to_context_messages`）：

- 跳过 `done` / `error` 收尾标记和 `<think>` 包裹的重复内容；
- 用户侧只保留第一条非"停止任务"文本（多模态消息取文本部件 + 媒体占位符）；
- **被中断/停止/出错且没有任何助手输出的轮次，仍回传该轮的用户问题**（事件数据已落盘，含崩溃恢复的 `interrupted` 轮；丢弃会让"请继续上面的任务"这类后续提问在模型侧完全失忆）。仅含"停止任务"的轮次不产生消息；
- 工具结果回传长度由 `HISTORY_TOOL_RESULT_RETURN_MAX_LENGTH` 决定：
  - `0`（不回传）：工具调用渲染成 assistant 文本行 `[调用工具] 工具名({参数})`，结果不回传；
  - 负数（完整回传）：按 Chat Completions 规范展开为 `assistant.tool_calls` → `tool(tool_call_id, 完整结果)`；
  - 正数：同上，但每个结果截断到前 N 字符并以 `…（工具结果过长，按配置已截断）…` 结尾。

### 3.2 摘要模式（`context_summary` 存在后）

消息序列：

```
[system] 主系统提示（persona + 运行时文本 + 文件块 + 任务计划 todo）
[system] 【历史压缩摘要】（累计摘要，单块）
[system] 【用户最近问题】（保真问题索引，按轮次标注）
[user/assistant/tool ...] 窗口内未压缩轮次的完整对话
[user] 当前用户问题
```

**累计摘要**（`render_context_summary`）渲染为：

```
【历史压缩摘要】
（覆盖轮次 1-15）
<模型输出的 summary 正文>
【任务目标】
- …
【已完成工作】
- …
【关键设计决定】
- …
【未解决问题】
- …
【重要文件】
- …
```

**保真问题索引**（`render_recent_questions_message`）渲染为（受 10k token 预算，超出从最旧丢弃，至少保留最新一条）：

```
【用户最近问题】（按轮次，最旧在前）
- 第 1 轮：…
- 第 2 轮：…
```

**窗口切分**（`split_context_window`）：未压缩轮次优先占满 `keep_rounds` 窗口（其问题不再重复进入索引）；剩余窗口从最新往回分配给已压缩轮次的原始问题（带全局轮次编号，与摘要 `round_start/round_end` 同一编号体系）。`keep_rounds=0` 时全部未压缩轮次完整回传、全部已压缩问题进入索引。

---

## 4. 压缩的五个触发点

### 4.1 任务开始 / 任务收尾：自动跨轮压缩

每次聊天任务开始（合并后端历史之前，直接作用于持久层）与收尾（`done` 落盘后）各调用一次
`compact_session_history_if_needed`（自动模式，预算 = 压缩阈值）。同时满足两个条件才压缩：

1. **未压缩轮次数 > `keep_rounds`**（`keep_rounds=0` 时不设下限）；
2. **"累计摘要 + 全部未压缩轮次" 的估算 tokens > 压缩阈值**。

> 换句话说：轮次没超过窗口、上下文也没超阈值时，任务开始/收尾不会做压缩；超阈值但轮次很少的场景交给 4.3 的任务内重建处理。

### 4.2 每次模型调用前：任务内预算管理 + 单轮轨迹压缩

任务的多轮工具循环中，每次调用模型前依次执行：

**（a）任务内上下文预算管理**：全量上下文（消息 + 工具定义）> 压缩阈值时，重建整个消息序列：

1. 跨轮压缩持久层历史（`force_all`：不受 keep_rounds 保护、把全部未覆盖轮次纳入累计摘要；预算 = 阈值 × 0.4）；
2. 从持久层重建历史部分：主系统提示保持在首位，其后是累计摘要 system、最近问题 system、窗口内未压缩轮次；
3. 当前任务轮的消息（最后一条用户问题起）原样保留，交由单轮压缩继续管理。

**（b）单轮工具轨迹压缩**（`compact_active_round_context_if_needed`）：详见第 5 节。

### 4.3 首次模型调用前的窗口硬检查（P1）

系统提示/文件块/任务计划组装完成后，估算整个请求上下文与**聊天窗口**比较（这是硬限制，单轮阈值由 4.2 负责）。超窗时按序降级：

1. **上传文件降级**：文件记忆是超窗最常见来源 → 整体替换为 3000 字符以内的文本摘要（推送 `CONTEXT_BUDGET_FILE_DOWNGRADED` 警告帧）；
2. **强制跨轮压缩**：预算按实际可用空间计算
   `history_allowance = 窗口 − 系统提示 − 工具定义 − 当前请求 − max(1024, 窗口/16)`，
   `force_all` 把全部历史纳入累计摘要；压缩失败（`ContextCompactionError`）直接终止任务；
3. **问题预算降级链**：重建"摘要-only"候选后仍超窗时，把最近问题索引预算按
   `10000 → 5000 → 2000 → 1000 → 256` 逐级收紧重试；
4. 全部降级仍超窗 → 输出组成明细（系统提示+文件 / 后端历史 / 当前请求 / 工具定义）的错误并终止。

> 另外切换到更小窗口模型后的首次调用也走同一条强制压缩路径。

### 4.4 手动压缩

`POST /chat_context/compact_manual?session_id=…&force=false&stream=false`：

- 预算与门槛统一使用**摘要总预算**（窗口 × summary_budget_ratio）；
- **下限守卫**：当前上下文估算（仅消息部分 `messages_tokens`）低于摘要总预算时压缩无收益（摘要可能不比原文小），直接拒绝并提示，例如：
  > 当前上下文约 8,420 tokens，低于摘要预算 25,600 tokens（模型窗口 × 摘要预算比例 0.2），压缩无收益，已取消本次手动压缩。
- `force=true` 跳过守卫；确认后 `force_all` 把**全部已完成轮次**归并为一个累计摘要；
- `stream=true` 时以 SSE 返回与自动压缩同构的 `start/delta/done` 事件，结尾附加
  `{"event": "compaction_manual_result", compressed_rounds, stats, error}` 与 `[DONE]`；
- 前端确认对话框中还有一层"低于限制无反应/提示"的预检查（与该守卫同一阈值）。

### 4.5 中断恢复

- 轮次收尾前的事件逐条写入侧车检查点 `<session_id>_chat.jsonl.pending`；进程重启后恢复为 `status=interrupted` 的历史轮次（工具调用/思考/结果不丢）；
- 有 `start` 无 `done/aborted` 的孤儿压缩事件，在下次任务开始时标记 `aborted` 并重新压缩。

---

## 5. 单轮工具轨迹压缩（active round）

**目的**：一个任务里连续几十次工具调用后，工具原始输出会挤爆窗口；在下一轮模型调用前，把"本轮已执行的工具轨迹"替换成一段累计摘要，只保留用户问题与摘要。

**触发条件**（全部满足才压缩）：

1. 全量上下文 > 压缩阈值；
2. 能定位到当前轮（最后一条非内部 user 消息）；
3. 当前轮至少有一次工具调用/结果；
4. **防空转**：本轮轨迹自身 tokens > `max(256, 阈值/2)`。若超窗主要来自历史（本轮轨迹不足阈值一半），单轮压缩压不掉历史，只会空转消耗压缩模型调用——此类场景留给任务内重建与首调用检查。

**压缩输入**（`_round_messages_to_compaction_text`，含工具**完整输出**，因为压缩时必须看到路径/数值/错误才能保留关键事实）：

```
【此前本轮摘要】          ← 已有累计块（第二次及以后压缩时）
【用户】…
【助手】…
[调用工具] tool_name({参数})
【工具结果：tool_name】
…
```

**产物**：一段累计摘要文本 + 游标 `compress_index`（按 `role=tool` 的事件**数量**计数，不是 events 下标；取本轮工具结果数与旧游标的较大者）。

**消息重建**：把当前轮的 assistant/tool 消息整体替换为一条插在前导 system 消息之后的摘要消息（兼容只接受 system 开头的 Chat Completions 实现）：

```
[system] 主系统提示
[system] 【本轮已执行工具摘要】
<累计摘要正文>
[user] 当前用户问题
```

摘要消息带内部标记 `_context_compaction_scope=active_round` 与 `_context_compaction_index=游标`，任务收尾时随 `done` 事件写入当前 `chat_round.events`（`context_compaction/round/done`，含 `summary_text`、`compress_index`、`compress_usage`）。之后重建历史时，游标之前的工具轨迹（含对应的 `tool_calls`）会被跳过，不再重复回传。

**增量事件**：压缩模型的思考/正文增量以 `phase=delta` 实时推送到前端（与聊天 SSE 字段同名 `reasoning_content`/`content`），**delta 只推流、不落盘**；`start`（含 ≤800 tokens 的压缩源节选）与 `done`（含前后 token 数与摘要全文）落盘。

---

## 6. 跨轮压缩细节（compact_session_history_if_needed）

按批把最旧的未压缩轮次并入**单块累计摘要**：

1. **确定压缩量**：从最新往回保留能在预算内放下（自动模式：预算 = 压缩阈值；force_all 模式：调用方传入预算）的尾部轮次，其余进入本批；每批最多取
   `_select_compaction_count` = 在压缩模型输入预算（约 `min(0.75 × 压缩窗口, 压缩窗口 − 输出上限 − 4096)`）内能容纳的轮次数，至少推进 1 轮；
2. **批压缩**：每轮渲染为 `【待压缩历史轮次 N】` + 完整轨迹文本（含工具原始输出、已有单轮摘要【本轮已有压缩摘要】），拼接后交给压缩模型；
3. **累计合并**：已有累计摘要时再调用一次压缩模型，输入为
   `【已有累计摘要】<旧文本> + 【新增历史摘要】<新文本>`，要求**继承旧摘要中仍有效的事实/决定/路径/未解决事项**，输出上限 = 摘要总预算；
4. **写状态**：解析分段标题生成单块累计摘要状态（`blocks[0]` 带 `round_start=1, round_end=新游标`），连同 `source_round_count` 游标、按新游标重算的保真问题索引一起原子写入 `_meta.context_summary`；压缩 usage 累计入 `compress_usage` / `_history_compress_usage`；
5. 循环直到未压缩轮次满足窗口/预算条件。

**游标自愈**：`source_round_count` 大于现存轮次数（历史被删/导入不匹配）时，旧摘要整体作废并从原始轮次重建；手动删除历史行同样会重置 `context_summary`。

---

## 7. 超大工具结果拒绝（先于压缩）

阈值 = `max(1024, min(聊天窗口, 压缩窗口) × oversized_reject_factor)`（`<=0` 关闭）。

工具返回后按轻量估算超阈值时：

- **结果不进入模型上下文**，替换为反馈文案：告知估算值与阈值，引导缩小查询范围/加分页/换工具，并附 ≤600 tokens 的**头尾节选**（头部 45% + 尾部 35%，中段以 `…【中间内容因上下文预算已省略】…` 标记）帮助模型重新规划；
- JSONL 落盘 `result_preview`（≤2048 tokens 头尾节选）+ `oversized: true` + 估算值，前端仍按普通工具结果渲染；
- **连续**拒绝达到 `max_oversized_rejections`（默认 3）次 → 终止当前任务（反馈文案作为最后回复）；一次正常结果会重置连续计数。

> 头尾节选保留预算只按 80% 分配并迭代收紧，吸收中文/代码比例变化导致的估算误差。

---

## 8. 压缩模型调用细节（summarize_context_text）

- **提示词**（按 scope 变化：`单轮工具执行上下文` / `跨轮会话历史` / `累计历史摘要`）：

  > 你是{scope}压缩器。请把用户任务、已确认事实、关键数值、路径、错误信息、工具执行结论以及仍需继续的事项压缩为后续模型可直接使用的中文备忘。
  > 输出时尽量按以下标题分段组织，没有对应内容的段落整体省略，标题必须原样使用：
  > 【任务目标】/【已完成工作】/【关键设计决定】/【未解决问题】/【重要文件】
  > 段落内使用短项目符号。只输出普通文本，不要输出 JSON、XML、代码围栏或任何固定数据结构。
  > 工具原始输出很长时，只保留会影响后续决策的结果、证据、错误和下一步。
  >
  > （累计历史摘要时追加）这是已有累计摘要与新增历史的合并任务：必须继承已有摘要中仍有效的事实、设计决定、文件路径和未解决事项，同时吸收新增内容；不要只输出新增部分。

  解析时兼容相近标题（如【目标】/【待办】/【下一步】等别名，见 `chat_history_format._SECTION_ALIASES`）；无任何标题时整体归入 `summary`。

- **输入预算**：`压缩窗口 − 提示词 − 输出上限 − 256`，超出的源文本做头尾节选截断（同第 7 节算法），`source_was_truncated` 记录是否截断；
- **输出限制**：`min(8192, 模型 maxOutputTokens, 输出预算)`；为防少数服务端忽略 `max_tokens`，产物再截断到 `2 × 输出预算`；
- **调用参数**：`temperature=0.2`、`reasoning_effort=low`、无工具；`model_selection.compaction_model.parameter` 中的同名字段可覆盖（`max_tokens`/`temperature`/`top_p`/`presence_penalty`/`reasoning_effort`/`extra_body`）；
- **流式模式**：提供了事件回调时走流式接口，逐帧解析 SSE，把 `reasoning_content`/`content` 增量实时回调（前端渲染压缩思考过程），最终摘要只取正文；两种模式最终文本与 usage 口径一致。

---

## 9. 完整举例

### 例 1：跨轮压缩（任务开始时自动触发）

配置：窗口 128k、trigger_ratio=0.5（阈值 64k）、keep_rounds=20。某会话已有 25 轮，累计上下文约 70k tokens。

新任务开始 → 自动压缩（25 > 20 且 70k > 64k）：

- 从最新往回保留能在 64k 内放下的尾部轮次（设第 21–25 轮共 20k）→ 压缩第 1–20 轮；
- 第 1–20 轮渲染为带完整工具输出的文本，批压缩 → 与（可能存在的）旧累计摘要合并为单块累计摘要，游标推进到 20；
- 问题索引：未压缩的第 21–25 轮占去窗口 5 个位置，剩余 15 个窗口从最新往回分配给已压缩轮次 → 第 6–20 轮的原始问题带编号进入索引（受 10k token 预算，超出从最旧丢弃）。

之后模型看到的请求：

```
[system] 主系统提示
[system] 【历史压缩摘要】
         （覆盖轮次 1-20）
         本轮任务目标是对命名管道工具做全面测试……
         【任务目标】
         - …
         【已完成工作】
         - …
[system] 【用户最近问题】（按轮次，最旧在前）
         - 第 6 轮：…
         - …
         - 第 20 轮：…
[user]   第 21 轮问题
[assistant] 第 21 轮回答（含 tool_calls / tool 结果，按回传长度）
…（第 22–25 轮完整对话）
[user]   当前问题
```

### 例 2：单轮工具轨迹压缩

同一任务中模型已连续执行 30 次工具调用（每次结果约 3k tokens），某次工具结果落盘后、下一次调用模型前：

- 全量上下文 ≈ 70k > 阈值 64k，且本轮轨迹 ≈ 45k > 阈值/2 = 32k → 触发单轮压缩；
- 30 条工具轨迹（含原始输出）压成一段约 2k tokens 的摘要，游标 `compress_index=30`；
- 重建后的消息：

```
压缩前：
[system] 主系统提示
[user] 帮忙统计目录下所有文件行数
[assistant tool_calls=call_1…call_30]
[tool] …（30 条完整结果）
[assistant] 中间结论…
[tool] …

压缩后：
[system] 主系统提示
[system] 【本轮已执行工具摘要】
         已统计 src/ 目录 12 个文件：共 3,842 行；data/ 下两个文件读取
         超时已改为分块读取……关键数值与错误均已保留。
[user] 帮忙统计目录下所有文件行数
```

- 任务收尾时该摘要写入本轮 `chat_round.events`（`context_compaction/round/done`），下次重建历史时第 1–30 条工具轨迹被跳过。

### 例 3：手动压缩被下限守卫拒绝

新会话只有 1 轮、上下文约 8k tokens < 摘要预算 25.6k：前端确认后请求被拒（SSE 模式经 `compaction_manual_result.error` 返回，JSON 模式 400），提示"压缩无收益"；`force=true` 可跳过。

### 例 4：超大工具结果

模型调用 `read_file` 读了 300k tokens 的日志（阈值 192k）：结果不进上下文，模型收到：

```
工具 read_file 的返回结果过大：估算约 300.0k token，超过 192.0k 的上下文预算，
结果无法纳入模型上下文。请重新考虑工具使用方式：例如缩小查询范围、添加过滤/限制参数、
分批获取，或改用能返回精炼结果的工具。请不要原样重试同样的调用。

为帮助你重新规划，以下是该结果的头尾节选（中间部分已省略）：
2026-08-23 12:50:01 INFO server started …
…【中间内容因上下文预算已省略】…
2026-08-23 12:59:58 ERROR offset 100000 out of range …
```

模型改用分页读取后正常结果重置连续计数；若连续 3 次仍超长则任务终止。

---

## 10. 统计与前端展示

`GET /chat_context/token_stats`（`get_context_token_stats`）返回请求上下文构成，前端上下文 Token 状态条使用：

- `context_token_limit`（窗口）、`messages_tokens`、`system_prompt_tokens`、`tool_definition_tokens`、`request_context_tokens`、`estimated_budget_ratio`；
- `rounds`: `{total, summarized, retained, max_rounds}`；
- `history_context_mode`: `summary_only` / `legacy_raw`；
- `has_context_summary`、`recent_questions_length/count`；
- `context_compress_count`（会话累计压缩次数）、`history_compress_count`（其中跨轮次数）；
- `round_tokens[]`: 每轮的问题/状态/消息数/tokens/压缩次数（含进行中的 pending 轮）。

压缩过程事件（SSE `context_compaction`，`scope=round|session`，`phase=start|delta|done|aborted`）驱动前端压缩块展示：`start` 显示压缩源节选，`delta` 实时渲染压缩模型思考/正文，`done` 携带摘要全文与 usage 并落盘，刷新后仍可回放。
