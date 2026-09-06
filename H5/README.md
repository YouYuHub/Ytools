# Ytools H5 前端说明文档

`H5/` 是智能体工具服务平台（FastAPI 后端）的 **Web 前端**：一个纯静态、无框架依赖的类 ChatGPT 风格聊天单页应用（原生 JavaScript + SCSS），直接请求后端 REST/SSE 接口。

> 后端接口出入参详见根目录 `docs/api_docs.md`；整体项目结构见根目录 `project_structure.md`。
> 前端样式应修改 H5/style/scss 源文件，由 CSS watch 自动生成，不直接编辑生成的 CSS。

---

## 一、快速开始

### 1. 运行方式

前端为纯静态页面，两种方式任选：

- **后端同端口托管**（推荐）：由 FastAPI 直接托管 `H5/` 静态目录，访问 `http://127.0.0.1:48621` 即可；
- **本地直开**：双击或浏览器打开 `index.html`（需注意跨域，后端已开 CORS）。

### 2. 指定后端地址

默认请求 `http://127.0.0.1:48621`（js/api.js:6）。如需指向其他后端，在浏览器控制台设置一次即可：

```js
localStorage.setItem('ytools-api-base', 'http://192.168.1.10:48621')
```

刷新生效；旧键名 `api-base` 会自动迁移。

### 3. URL 参数

| 参数 | 说明 |
|---|---|
| `?session=<id>` | 启动后直达指定会话，不存在则新建 |
| `?theme=light\|dark\|system` | 临时预览主题（不写入偏好） |

### 4. SCSS 构建与测试

```bash
npm install            # 安装 sass（devDependency）

npm run build:css      # 编译 style/scss/main.scss -> style/css/main.css
npm run watch:css      # 监听模式（也可双击 build-css.bat 一键编译）
npm test               # 运行单元测试（node --test "test_h5/*.test.js"）
```

> 页面引用的是编译产物 `style/css/main.css`，修改 SCSS 后必须重新编译才能生效。

---

## 二、目录结构

```
H5/
├── index.html                  # 单页应用入口：侧边栏 + 主区 + 各弹窗骨架
├── package.json                # 脚本：build:css / watch:css / test
├── build-css.bat               # 双击一键编译 SCSS（无本地 sass 时回退 npx）
├── watch_css.cmd               # 监听模式快捷方式
│
├── js/
│   ├── theme.js                # 主题管理（最先加载，避免首屏闪烁）
│   ├── api.js                  # 后端 API 封装（REST + SSE 流解析），挂 window.API
│   ├── markdown.js             # 轻量 Markdown 渲染器（先转义 HTML 再解析）
│   ├── session_utils.js        # 会话 ID 规整（与后端 normalize_session_id 对齐）
│   ├── session_list_utils.js   # 会话列表排序/标题兜底规则
│   ├── format_utils.js         # 数字/时间/JSON 格式化、token 用量文案
│   ├── history_parser.js       # 历史 JSONL -> 渲染记录列表
│   ├── app.js                  # 应用入口：一次性迁移旧配置并执行 init 启动序列（约 60 行）
│   ├── app/                    # 主逻辑模块（IIFE 挂 window.App 共享命名空间，见"四"）
│   │   ├── core.js             # 共享核心：state/常量/DOM 引用/通用工具（须最先加载）
│   │   ├── sessions.js         # 会话列表与会话切换（重命名/删除/新对话/恢复流）
│   │   ├── stats.js            # 侧边栏累计 token 与上下文统计条
│   │   ├── workdir.js          # 工作路径显示与内联编辑
│   │   ├── messages.js         # 历史渲染装配/消息渲染/问题导航 qnav
│   │   ├── history.js          # 分享导出/加载 JSONL/文件上传 chips
│   │   ├── compaction.js       # 手动压缩对话
│   │   ├── builtin.js          # 内置工具（todo 任务计划卡片/ask_user 提问弹窗）
│   │   ├── media.js            # 附件与图片/音视频/文档预览
│   │   ├── skills.js           # Skills 提示词库：可拖拽对话框（列表/预览/编辑/保存/加载到输入框）
│   │   ├── composer.js         # 输入区/聊天设置弹窗/功能菜单
│   │   ├── model_panel.js      # 模型参数面板（三角色选项卡/参数编辑）
│   │   ├── tools.js            # 工具选择弹窗（"配置工具"）
│   │   └── chat.js             # 发送/停止与 SSE 事件处理管线
│   └── vendor/prism/           # Prism 代码高亮（按语言拆分按需引入）
│
├── style/
│   ├── scss/                   # 样式源码（7 个分区 partial + 变量）
│   │   ├── main.scss           # 入口 @use 汇总
│   │   ├── _variables.scss     # CSS 自定义属性（light 默认 + [data-theme="dark"] 覆盖）
│   │   ├── _base.scss          # 重置与基础元素
│   │   ├── _layout.scss        # 整体布局（sidebar + main）
│   │   ├── _sidebar.scss       # 侧边栏（窄栏 rail / 展开面板）
│   │   ├── _chat.scss          # 聊天消息区
│   │   ├── _composer.scss      # 底部输入区
│   │   ├── _components.scss    # 弹窗/菜单/toast/chips 等通用组件
│   │   └── _prism.scss         # 代码高亮主题
│   └── css/main.css            # 编译产物（页面实际引用）
│
└── test_h5/                    # 单元测试（node:test + node:assert）
    ├── format_utils.test.js
    ├── history_parser.test.js
    ├── markdown.test.js
    ├── session_list_utils.test.js
    └── session_utils.test.js
```

脚本加载顺序（index.html 底部）：`theme.js` → `api.js` → prism 系列 → `markdown.js` → `session_list_utils.js` → `session_utils.js` → `format_utils.js` → `history_parser.js` → `app/core.js`（共享核心，须最先于其余 app/ 模块）→ `app/sessions.js` → `app/stats.js` → `app/workdir.js` → `app/messages.js` → `app/history.js` → `app/compaction.js` → `app/builtin.js` → `app/media.js` → `app/skills.js` → `app/composer.js` → `app/model_panel.js` → `app/tools.js` → `app/chat.js` → `app.js`（入口，最后执行 init）。工具函数模块均为「浏览器挂 window 全局 / Node 下 module.exports」的双端写法，因此可直接被 node:test 测试；app/ 各模块间的跨模块调用统一走 `App.xxx(...)`（运行期解析，仅要求 core 先加载、入口最后加载）。

---

## 三、JS 模块详解

### api.js — 后端接口封装（window.API）

统一入口 `request()` 处理非 2xx 错误（提取 detail/message）。主要分组：

| 分组 | 方法 | 对应接口 |
|---|---|---|
| 会话历史 | `listSessions` / `streamStatus` / `getSessionMeta` / `getContextTokenStats` / `updateSessionTitle` / `fetchSessionFile` / `deleteSession` / `uploadChatHistory` | `/chat_history/*`、`/chat_stream/status`、`/chat_context/token_stats` |
| 工具 | `listTools(refresh?)` | `/tools/list`；后端默认返回 TTL 内的探测缓存（毫秒级，首屏友好），`refresh=true` 追加 `?refresh=1` 强制重探 |
| 模型配置 | `getModels(role)` / `selectModel(provider, model, role, parameter)` | `/chat_config/models[/select]` |
| 聊天设置 | `getHistoryCompactionConfig` / `updateHistoryCompactionConfig` / `getContextReturnConfig` / `updateContextReturnConfig` | `/chat_config/history_compaction`、`/chat_config/context_return` |
| 工作目录 | `getWorkDirConfig` / `changeChatDir` | `/chat_config/work_dir`、`/change_chat_dir` |
| MCP 工具执行 | `getMcpToolConfig` / `updateMcpToolConfig` | `/chat_config/mcp_tools` |
| 文件 | `uploadSessionFiles` / `getSessionFiles` / `deleteSessionFile` | `/file/*` |
| Skills 提示词 | `listPrompts` / `readPrompt(name)` / `createPrompt(name, content?)` / `savePrompt(name, content)` / `renamePrompt(old, new)` / `deletePrompt(name)` | `/prompts/list`、`/prompts/read`、`/prompts/create`、`/prompts/save`、`/prompts/rename`、`/prompts/delete` |
| 控制 | `stopChat(sessionId)` | `/stop_chat` |
| 聊天流 | `chatStream(payload, onEvent, signal)` | `POST /chat_with_tool`（SSE） |

`chatStream()` 用 fetch + ReadableStream 手工解析 SSE：逐行读取 `data:` 前缀行，`[DONE]` 结束，其余按 JSON 解析后经 `onEvent({type, data})` 回调（type 为 `data` / `done`）。支持 AbortSignal 中断。

### theme.js — 主题管理（window.ThemeManager）

- 偏好持久化于 `localStorage("ytools-theme-preference")`，有效主题写到 `<html data-theme>`；
- 支持 `system`（跟随系统并监听变化）/ `light` / `dark`；
- 设置偏好时派发 `document` 级 `themechange` 事件供 UI 同步；
- 在 `<head>` 中最先加载，避免首屏主题闪烁。

### markdown.js — 轻量 Markdown 渲染器（window.Markdown）

先整体 HTML 转义再解析，天然防注入。支持：标题、代码块（配合 Prism 高亮）、行内代码、粗体/斜体、链接、有序/无序列表、引用、表格（`\|` 转义与行内代码保护）、分割线。

### 纯逻辑工具模块（均可单测）

| 模块 | 导出 | 职责 |
|---|---|---|
| session_utils.js | `sanitizeSessionId` / `fileNameToSessionId` | 会话标识以文件名为准（`<id>_chat.jsonl`），保留 Unicode/空格/下划线/点/横线，替换危险字符并拦截 `..` 路径穿越，兜底 `"default"` |
| session_list_utils.js | `sortRows` / `firstQuestionTitle` / `timeValue` | 列表按 `max(后端 updated_at, 本地 recency)` 倒序、created_at 兜底；标题取首条提问前 40 字符（与后端 `_meta.title` 规则一致） |
| format_utils.js | `fmtNum` / `fmtTime` / `prettyJson` / `normalizeUsage` / `usageText` / `compactionUsageText` | 千分位数字、时间去日期前缀、JSON 美化、usage 三字段归一化、「本轮消耗 N tokens（输入 x · 输出 y）」及压缩消耗文案 |
| history_parser.js | `parseHistory` / `recordsBeforeActiveRound` | 解析后端 JSONL：首行 `_meta` 取累计 usage；`chat_round.events` 展开为 user/think/assistant/tool/toolResult/usage/notice 记录；`context_compaction` 事件行转为 compaction 记录（`phase=aborted` 回溯置中断态）；打开会话时剔除正在流式的当前轮避免重复渲染 |

---

## 四、主逻辑（js/app/ 模块 + app.js 入口）

主逻辑按功能拆分为 `js/app/` 下 14 个 IIFE 模块 + 瘦入口 `app.js`（一次性迁移旧配置并执行 init 启动序列）。共享机制：

- `app/core.js` 创建 `window.App` 并以 `Object.assign(App, {...})` 导出共享 state、常量、DOM 引用与通用工具；其余模块在头部解构所需核心成员（`const { state, el, toast } = App;`），并以 `App.foo = foo;` 注册自己的导出；
- **跨模块调用一律写 `App.xxx(...)`**（运行期解析）：加载顺序只要求 core 最先、入口最后，模块之间无顺序耦合；
- **`App.xxx` 只可运行期解引用，禁止加载期当值引用**：`addEventListener(type, App.xxx)`、`const f = App.xxx` 这类写法拿到的是 `undefined`（被引用的方法由更晚加载的模块赋值；`addEventListener(type, undefined)` 还会被静默忽略，表现为按钮点击无响应）。跨模块监听器必须包一层 `function () { App.xxx(); }` 在触发时再解引用；
- 原单文件中唯一的跨区块可变变量 `manualCompactRunning` 收编为 `state.manualCompactRunning`。

### 1. 核心状态（state，js/app/core.js）

| 字段 | 说明 |
|---|---|
| `sessionId` | 当前会话 id；**`null` 表示"待开始的新对话"**——ID 仅在真正产生内容的动作（发送消息 / 上传文件）时由 `ensureSessionId()` 惰性生成（`yt-<YYYY-MM-DD_HH.MM.SS>_<三位随机数>`），选工具、调参数等操作不分配 ID、也不发起按会话的后端请求，避免后端凭空创建只有 `_meta` 的空会话文件 |
| `streaming` / `streamingSession` / `abort` | 当前监听的流状态与 AbortController（同一时刻只监听一个会话的流，后端任务可多会话并行） |
| `tools/servers/failedServers` | MCP 工具列表、按服务器分组、连接失败的服务器 |
| `selectedTools` / `draftTools` | 已生效的工具集合 / 工具弹窗内的草稿集合（确定才生效） |
| `sessionTotalTokens` / `sessionUsage` | 侧边栏展示的会话累计 token |
| `contextTokenStats` | 最近一次上下文 token 统计缓存 |
| `workDir` | 当前工作路径（composer 下方常显，可编辑切换） |
| `chatSettingsDefaults` | 聊天设置后端 defaults 合并结果 |
| `activeStream` | 进行中流的上下文（用户气泡/回复容器引用、usage 等），用于切会话后续看 |
| `hasConversation` | 控制顶栏按钮「加载 JSONL ↔ 分享导出」双态 |
| `importedHistoryText` | 本地导入 JSONL 原文（后端不可用时的导出兜底） |
| `sessionRecency` | 本地「最近触碰」记录，后端落盘延迟时保持列表置顶顺序 |
| `modelConfigs` 等 | 参数面板的三角色模型配置、当前角色选项卡、草稿选中模型、参数是否手动改过等 |

localStorage 键：`ytools-session-title-overrides`（会话标题本地覆盖）、`ytools-api-base`、`ytools-theme-preference`。

### 2. 主要功能模块（按功能区块 × 所在文件）

| 区块（所在模块） | 要点 |
|---|---|
| 基础工具（app/core.js） | DOM 快捷创建、toast（2.6s 自动消失）、移动端判断（768px 断点）、瞬跳滚底（临时覆盖 smooth 滚动）、回到底部按钮偏移计算 |
| 主题菜单（app/core.js） | profileCard 弹出 system/light/dark 菜单，选中态与 `themechange` 事件联动 |
| 侧边栏（app/core.js） | 折叠/展开（窄栏 rail ↔ 完整面板），移动端联动 scrim 遮罩，Esc 关闭所有浮层 |
| 会话搜索（app/core.js） | 展开搜索框按会话名过滤列表 |
| 会话列表（app/sessions.js） | 加载列表 + 逐个拉 meta（title/updated_at）；三点菜单支持重命名（原地 input，Enter/blur 保存、Esc 取消）与删除（确认弹窗；正在生成的会话禁止删除；删当前会话则自动新建）；首条消息即时以提问前 40 字符置顶显示；标题溢出不用「…」，ChatGPT 式右缘 ~28px 渐隐遮罩（refreshSessionFades 仅给真截断的标题加 .truncated，mask 作用于文字本身、悬浮/选中背景均成立） |
| token 统计（app/stats.js） | 侧边栏累计 token；流结束后以会话文件 meta 校准 |
| 上下文统计条（app/stats.js） | composer 下方常显「30.1k/100k (30.1%)」（小屏只显百分比）；ratio≥0.8 黄色警告、≥1 红色危险；详细构成放 hover 提示。不做轮询，仅在 usage 到达/压缩完成/切会话/发消息等事件后 120ms 防抖刷新，in-flight 期间新请求排队合并 |
| 工作路径（app/workdir.js） | 常显工作目录，双击内联编辑调用 `changeChatDir` 实时切换，hover tooltip 显示完整路径 |
| 会话切换（app/sessions.js） | 新建会话仅置空 sessionId（待开始态），首次发送/上传才生成 ID 并即时以提问前 40 字符置顶侧边栏；打开会话用序号防竞态，拉历史 → 渲染 → 恢复进行中流 UI → 重建问题导航 → 瞬跳底部 → 探测后台任务是否仍在生成并续接 |
| 历史渲染（app/messages.js） | 按 records 类型装配：用户气泡、思考折叠块、回答正文、工具调用折叠块（输入 JSON+输出文本配对）、轮次 usage 行、提示/出错横幅、压缩块（含中断态） |
| 消息渲染（app/messages.js） | Prism 高亮、Markdown 渲染、代码复制按钮事件委托；复制按钮 sticky 钉住时水平位移至代码块中线避开分享按钮 |
| 问题导航 qnav（app/messages.js） | 用户问题 ≥2 条出现：右侧虚线轨（上限 40 根）hover 展开编号面板，点击瞬跳，滚动 rAF 节流高亮当前位置 |
| 导出/加载（app/history.js） | 顶栏为图标按钮：空态点击直接进入"加载 JSONL"文件选择；有会话内容时点击展开三选项菜单——**分享对话**（下载 `<id>_chat.jsonl`）、**加载 JSONL**（校验后上传导入，重名自动另存，导入失败本地预览兜底）、**压缩对话**（见下） |
| 手动压缩（app/compaction.js） | 点击"压缩对话"后：① 并行读取 token_stats 与历史压缩配置，仅用于确认弹窗展示当前上下文和摘要预算；② 二次确认（仅防误点击）；③ 确认后 POST `compact_manual?stream=true` 的 SSE 流，事件结构与自动压缩一致（start/delta/done + summary_text），复用 `buildCompactionBlock` 实时渲染压缩模型思考与累计摘要正文，结尾 result 帧提示纳入累计摘要的轮数并刷新用量/上下文统计。后端手动压缩与自动压缩共用多轮流程，覆盖全部已完成轮次，原始历史只存储不回传；模型上下文为累计摘要 + 全历史最近 10k tokens 用户问题 + 当前任务 |
| 输入区（app/composer.js） | textarea 自增高；Enter 发送 / Shift 换行（排除中文输入法组合态）；发送/语音/停止三态按钮只反映当前会话状态；建议 chip 点击即发送 |
| 模型参数面板 enhancePanel（app/model_panel.js / composer.js） | boost 按钮开关；面板以 fixed 定位垂直锚定参数按钮（空态向下展开到视口底、聊天态向上展开到视口顶），水平与输入框同宽对齐，高度不随输入框行数漂移；三角色选项卡（聊天模型/压缩模型/标题模型）；自绘模型 picker 按 provider 分组，附能力标签（视觉/工具）与 api_type/url；选新模型未手调过 max tokens 时随其 max_output_tokens 重设范围；任一参数改动置 dirty，「恢复默认」按 api_type 协议预设 + 角色默认；确定时仅 dirty 才组装 parameter 提交 `selectModel`（responses 协议字段为 max_output_tokens） |
| “+”功能菜单（app/composer.js） | 上传文件 / 选择工具 / 聊天设置 / Skills 提示词四个入口，分发至 history/tools/composer/skills 各自的处理逻辑；菜单以 fixed 定位锚定「+」按钮上方（打开时按按钮视口坐标计算，多行输入撑高输入框也不会漂移） |
| 聊天设置弹窗（app/composer.js） | 并行读写三组配置：回传长度（思考过程/工具结果，0=不回传、负数=全部回传、正数=N 字符）、MCP 工具执行超时（正数为秒数、0=不限制）与历史压缩策略（历史轮数/统一触发比例、累计摘要预算比例、超大拒绝系数等）；逐项数值校验，全成功才关闭 |
| Skills 提示词库（app/skills.js） | “+”菜单打开可拖拽对话框（标题栏按住拖动、位置会话内记忆、Esc/背景/×关闭）；左侧文件列表（新建行内输入、名称自动补 .md）+ 右侧「预览 / 编辑」单栏切换（Typora 风格合并视图）：预览为博客式 Markdown 渲染（多级标题/表格/```lang 代码块高亮/引用，不支持图片），编辑为「格式工具栏 + 编辑器」单栏——选中文字即可加粗/斜体/行内代码、设 H1-H3、切换有序/无序列表与引用（再点取消），一键插入表格/代码块/分割线/链接（URL 占位自动选中），表格弹出 6×8 网格划选行列（含表头行，实时显示「N 行 × M 列」），支持 Ctrl+B/I，替换走 execCommand 保留原生 Ctrl+Z 撤销；保存（Ctrl+S）写回 `prompt/md_files/`；删除为两段式确认（3 秒内再点一次）；未保存修改暂存内存草稿，切文件/关弹窗不丢（列表与标题栏显示 ● 未保存）；「加载到输入框」把当前内容填进消息框并聚焦，直接发送 |
| 文件 chips（app/history.js） | 上传限制 ≤10 个、单个 ≤10MB（超出 toast 截断/跳过）；chip 可单独删除；解析文本下一轮注入系统提示词 |
| 发送与停止（app/chat.js） | 见下节 SSE 管线；停止 = 先调后端 `/stop_chat` 再本地 abort |
| 工具选择弹窗（app/tools.js） | 打开时快照草稿集合；按 MCP server 分组三态全选 checkbox、折叠、搜索（名称+描述）、悬浮描述 tooltip；「刷新」走 `listTools(true)` 即 `GET /tools/list?refresh=1` 强制后端重探（页面初始化与普通加载读取后端探测缓存，毫秒级返回，见 api_docs.md"工具列表缓存与预热"）；确定才把草稿落为生效集合并刷新上下文统计 |

### 3. SSE 事件处理管线

`createStreamPipeline(onEvent)`（app/chat.js）为发送与续看共用。各事件渲染行为：

| 事件 | 处理 |
|---|---|
| `reasoning_content` | 独立思考折叠块增量追加，带流式动画；事件不再携带该字段即认为思考结束 |
| `content` | 回答正文累积后整块 Markdown 重渲 + Prism 高亮 + 光标动画 |
| `tool_calls` | 先封存文本块；按 `index` 合并增量（函数名更新、arguments 流式拼接进输入 JSON 编辑区） |
| `tool_return` | 按函数名匹配未完成工具块填充输出；找不到则兜底新建块 |
| `usage` | 同一 completion id 的 usage 是逐步增长的累计快照，按 id **覆盖而非相加**（无 id 用指纹去重），轮末写 round-usage 行并触发上下文统计刷新 |
| `context_compaction` | 兼容两种 payload 形态；区分 scope（round=本轮轨迹 / session=跨轮历史）与 phase（start/delta/done）：start 建运行态块并展示待压缩源预览；**delta 为压缩模型流式增量（`reasoning_content`/`content` 与聊天 SSE 同名字段），逐帧追加到块内"压缩模型思考"/"摘要正文（生成中）"区实时渲染**；done 就地更新完成态并展示 `summary_text` 累计摘要全文；完成后防抖刷新统计。delta 帧仅实时推送不落盘；done 的 summary_text 落盘 JSONL，**刷新页面后由历史记录重建摘要展示** |
| `warning` / `error` | 消息容器顶部/底部插入 notice-bar 横幅 |
| `replay` | 续看专用：携带 question_text 时补建用户提问气泡（查重防止与历史重复） |
| `done` | 收尾：去除光标、清理流式态与未完成块 |

接近底部时自动跟随滚动。

**后台流续看机制**：切换走仅中止本地连接（后端任务不停）；回到会话时经 `/chat_stream/status` 探测，仍在生成则以空 messages 发起 `chatStream`，靠 `replay` 事件从当前轮起点回放。

---

## 五、样式架构

- 入口 `main.scss` 以 `@use` 汇总 8 个 partial；变量集中在 `_variables.scss` 的 CSS 自定义属性，light 为默认，dark 通过 `html[data-theme="dark"]` 覆盖同名变量实现换肤；
- 布局常量：`--rail-width: 50px`（窄栏）、`--sidebar-width: 240px`、`--content-max: 960px`；
- 移动端 ≤768px：侧栏默认折叠为 rail，抽屉模式 + scrim 遮罩，上下文统计只显百分比。

### SCSS 嵌套约定

各 partial 采用「组件根块 + 内部按 DOM 层级嵌套」的写法（深度 ≤3），便于按组件定位样式：

- **状态/伪类用 `&`**：如 `.tool-block { &.open .tool-block-body {…} }`、`.session-item { &:hover {…} }`；主题前缀变体写作 `html[data-theme="light"] &.active {…}`；
- **@media 就近内嵌**在对应组件块内，且必须保持在基础规则之后（同优先级时依赖源序覆盖，重构时已逐对验证）；
- **以下情形保持顶层扁平**，避免选择器被过度限定或改变匹配：
  - 全局复用类：`.icon-btn`（顶栏/弹窗共用）、`.icon`、`.menu-item-label`（被参数按钮复用）、`.hidden`/`.token-total`；
  - JS 挂到 body 或 `#chatInner` 直下的动态元素：`.tool-tip`、`.stage-time`、`.round-usage`、`.notice-bar`、`.empty-tip`；
  - 跨容器状态前缀：`.app.empty .*`、`html[data-theme] .*`、`.msg-cursor .copy-btn`；
  - ID 规则视情况保留原限定（`#voiceBtn` 等）。
- 显示/隐藏切换依赖的 `.hidden { display:none !important }` 与 qnav 的 `!important` 保证不会被嵌套提高的优先级破坏；
- 类名与 HTML/JS 完全零改动，仅重组源码结构；编译产物经脚本逐规则比对验证（451 条规则中除合并重复的 `.composer-bar-spacer` 外均语义等价）。

---

## 六、测试

纯逻辑模块均配套 `node:test` 单元测试（无第三方依赖）：

```bash
npm test          # 全部
node --test test_h5/markdown.test.js   # 单个
```

覆盖：格式化工具、历史 JSONL 解析（含压缩事件/中断态/活动轮剔除）、Markdown 渲染、会话 ID 规整、会话列表排序与标题规则。
