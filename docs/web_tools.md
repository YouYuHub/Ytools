# 网页搜索与抓取工具

## 工具位置

五个工具在 `mcp_server/web_tools_server.py` 注册，由 MCP 执行。推荐流程：模型使用 `web_search` 找候选链接，再用 `web_open` 打开页面看正文与编号链接，用 `web_click` 跟进编号、用 `web_find` 定位正文关键词；`fetch_url` 保留给 HTML/tag/regex 精细抽取与 API 接口调用。

`fetch_url.timeout_seconds` 是请求预算（默认 30 秒，1–120 秒），连接和后续读取共用截止时间；`web_search` 的跨引擎总预算为 45 秒。正则使用支持执行超时的 `regex` 依赖（见 [项目说明](https://pypi.org/project/regex/#timeout)），单次匹配超时会明确标记结果不完整。

## fetch_url

- `text`：抽取正文纯文本，优先正文容器，移除脚本、样式与部分导航噪音。
- `html`：原始响应文本，适合核对页面实际标签及接口内容。
- `tag`：简单元素选择器，如 `div`、`div#main`、`div.content.primary`；可用逗号多选，如 `h2,p,div#main`，最多 20 个。结果按页面顺序返回，同一元素只选一次；父子元素同时选中时保留父元素，避免正文重复。按真实 HTML 元素配对，支持同名嵌套、大小写、引号/无引号属性；返回内部纯文本，排除脚本/样式。不是完整 CSS 或 XPath，不执行 JavaScript。
- `regex`：对原始响应文本匹配，保留脚本内容的可检索性；没有匹配时明确提示。每条匹配展示长度按 `max_chars / 4` 计算并限制在 200–2000 字符（默认 2000），最多收集 200 条；单条或总量被截断时明确说明，不能把显示内容当成完整原文。
- HTTP 4xx/5xx 也返回状态码、错误正文、Content-Type 及 Retry-After/Location（若存在），并注明是错误响应。没有内容的 DNS、连接、超时等传输错误仍报错；中途 `IncompleteRead`、读取超时/断连或响应短于 Content-Length 时保留已收到的内容，并明确标记“部分响应”，零匹配不能说明网页不存在目标。成功和错误响应都受下载/解压上限保护。
- 头部区分声明/推断编码与实际使用编码。max_chars 仍限制输出正文，不是下载大小。
- 输出显示提取后文本的字符区间 `[offset, end)`。GET/HEAD 内容超长时返回 `next_offset`；保持 URL、模式、pattern/tag 等参数不变并传 `offset=next_offset` 续读。每次仍会重新请求；网页变化会影响位置。POST 等其他方法指定非零 `offset` 会在发送前拒绝，避免重复执行有副作用的请求。regex 的单条匹配/总匹配上限独立生效，offset 不能恢复此前被正则上限舍弃的部分。
- `text` 提取为空时提示页面可能依赖 JavaScript、登录或受访问限制，并建议用 html/tag 核对或查找正文/下载链接。

空标签、页面不存在的元素、仅由 JavaScript 插入的内容都可能返回空结果；先查看 html 核对，不应自动认定解析故障。

## web_search

- 支持 `auto`、`bing`、`ddg`。auto 在候选不足或弱相关时尝试 DDG 合并；显式指定引擎时不会切换到另一引擎。
- 使用固定候选池及固定的补充条件，`max_results` 只影响最终展示数量。搜索引擎本身实时变化时，不保证不同调用的内容完全相同。
- DDG 解析基于 HTML 锚点属性与 class 集合，不依赖属性顺序、单双引号或单一 class；重定向 URL 按查询参数正确解包，不重复解码目标链接。
- DDG HTML 页面访问失败或没有可识别结果时，最多补充一次 Lite 页面尝试，支持其 `result-link` 标题和 `result-snippet` 摘要。明确的无结果页面不触发重复尝试；两个入口都失败时保留各自原因，不能保证外部服务一直可用。
- 排序优先考虑查询中完整版本号/年份的覆盖，再比较标题、摘要及 URL 的关键词命中。`3.13` 不会误匹配 `3.130`；完整 Bing 候选池也不会在重排之前挤掉 DDG 候选。最终结果没有完整匹配版本号/年份时给出泛化结果警告，建议更换关键词或指定 `site:`，不把首页包装成精确版本答案。
- `site:` 的域名及路径在结果层二次过滤，拒绝 `github.com.evil.test` 这类伪匹配；多个 site 条件按任选其一处理。
- 整个候选池均没有查询关键词命中时，不把明显无关页面作为有效结果返回。相关性是启发式校验，不是语义保证；同义词和不同语言可能导致漏召回，可换关键词重试。
- DDG 没有结果锚点且没有明确无结果标记时，提示验证码/访问限制/页面结构变化，不再静默返回成功的零结果。有效候选可保留，同时标注补充引擎的失败原因。
- 当前不存在搜索结果缓存；测试中的无关结果不能据此归因于缓存键。结果需用原始链接核验，零结果不代表目标网页不存在。

## web_open / web_click / web_find

- `web_open(url)`：返回标题、静态正文（字符分页，`offset` 续读）、最多 50 个编号链接。链接只保留 http(s)、去重、跳过带账号口令与指向本页自身的纯片段链接；编号只对本次返回的页面内容有效。
- `web_click(url, link_id)`：按 `web_open` 的编号打开目标页。无状态设计：每次重新读取来源页解析编号（MCP 服务按调用拉起子进程，内存状态不可跨调用），页面变动时请重新 `web_open`；目标页默认同样返回编号链接，可连续跟进。
- `web_find(url, query)`：正文内字面查找（忽略大小写），只返回匹配行、每处对应的正文字符 `offset` 与上下文；`offset` 可直接喂给 `web_open` 跳转续读。
- 三者与 `fetch_url` 共用同一套请求/解压/编码/截断与超时预算实现，不执行 JavaScript。

## PDF 与 JS 页面

- 抓取到 `application/pdf`（或 `%PDF` 魔数）时，`fetch_url`（仅 `text` 模式）与 `web_open`/`web_find`/`web_click` 自动切换为逐页文本提取：按物理页输出并标注页码（最多前 200 页），PDF 内嵌的 http(s) 链接同样编号可跟进。扫描版图片型 PDF 无可提取文本时会明确说明（暂不支持 OCR）。
- 静态正文为空且命中 JS 应用特征（`react-root`、`__NEXT_DATA__`、`ng-app`、`/_next/static/`、`vite/client`、“请启用 JavaScript”等）时，不再只说“可能依赖 JavaScript”，而是明确指引模型改用 `browser_navigate` 打开该 URL 后用 `browser_snapshot` 读取渲染后内容。

## 搜索改版兜底

- Bing/DDG 的结构化解析（结果块 class）一个都命中不了时，不再直接判空/报错，而是回退为“非结构化链接提取”：取页面全部站外链接，按查询关键词过滤重排。输出会明确标注 `非结构化兜底` 并提示用 `fetch_url` 核验；无关键词命中时仍返回空，不伪造结果。
- 明确的无结果页面（`no results` 等标记）不受影响，仍直接返回空。

## 2026-10-05 测试记录核实与改进

依据 `history_files/yt-2026-10-05_15.47.54_293_chat.jsonl` 核对后，修复了正则标签抽取的嵌套/伪标签问题、DDG 属性顺序依赖、HTTP 错误正文缺失及搜索诊断不足。没有接入完整 CSS/XPath、浏览器渲染或额外搜索服务，以保持参数与部署简单。

原报告中部分推测不成立：实现没有搜索结果缓存，也没有 XPath；当前 example.com 原始 HTML 没有 h1，选择 h1 返回空是正常结果。回归测试覆盖嵌套元素、脚本伪标签、空 head、多 class、DDG 重定向与验证码、site 过滤、固定候选池、错误响应上限、真实本地 HTTP 429 等。外网冒烟验证了 example.com 的 p 抽取、DDG Python 文档查询及 Bing site 查询；外网站点仍可能不稳定或访问受限。

执行：`python -m pytest test/test_web_tools_web.py test/test_web_tools_http_limits.py test/test_web_tools_navigation.py -q`。

## 2026-10-06 测试记录核实与改进

依据当天 DeepSeek Flash 测试记录增加了版本号/年份排序、缺少精确匹配的警告、DDG Lite 备用尝试、完整候选池合并修正及 tag 多选择器。没有接入第三家引擎、完整 CSS 或新的标签探查模式；页面结构仍可通过现有 `mode=html` 核对。详见 [测试报告核实与改进](archive/tool_test_review_2026-10-06.md)。
