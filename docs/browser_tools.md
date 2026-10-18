# 浏览器点击与调试工具

本项目接入官方 Playwright MCP，让模型打开网页、读取页面快照、点击、输入、截图，并查看控制台与网络请求。默认运行无界面的 Chromium；**不需要开发或打开额外的 GUI 程序**。如果需要亲眼观察操作、手动登录或接管已有标签，可将服务切换为可见浏览器模式；此时需要运行在可显示桌面的会话中。

## 安装

在项目根目录执行：

```powershell
npm ci --prefix browser
node browser/node_modules/playwright/cli.js install chromium
```

Node.js 需要 20 或更新版本。`browser/package-lock.json` 固定了 Playwright MCP 版本；不要把 `browser/node_modules` 当作项目源码。Chromium 下载后保存在 Playwright 的用户缓存中。首次安装需要网络；日常运行不需要为浏览器工具额外启动服务。

安装后可运行 `python test/manual_browser_smoke.py`，用本地测试页面验证真实的导航、点击和截图链路。

工具服务已在 `setting/mcp_servers.json` 中注册为 `Browser`，选用了 `browser_navigate`、`browser_snapshot`、`browser_click`、`browser_type`、`browser_take_screenshot`、`browser_console_messages`、`browser_network_requests` 和 `browser_tabs`。新会话默认选中这些工具；旧会话若已固化工具选择，需要在前端「配置工具」中启用。没有安装依赖时，Browser 工具发现会失败，其他 MCP 工具仍可用。

## 使用

推荐让模型按以下顺序操作：

1. `browser_navigate` 打开网页，例如本地服务地址 `http://127.0.0.1:8000/`。
2. `browser_snapshot` 获取页面结构与元素引用。
3. `browser_click` / `browser_type` 操作快照中的目标；页面变化后重新取快照。
4. `browser_console_messages` 和 `browser_network_requests` 排查脚本错误及请求失败。
5. `browser_take_screenshot` 保存画面，需要视觉判断时使用 `read_media` 读取工具返回的本地绝对路径。

页面快照是文字，因此无视觉能力的模型也能完成常规点击和调试。截图会以 MCP 图片结果返回；本项目将图片写入系统临时目录下的 `ytools_mcp_images`，并把绝对路径放进工具结果。Playwright 自身的快照、日志及截图文件则写入系统临时目录下的 `ytools_playwright_artifacts`，不写到会话工作目录。`read_media` 仅在本轮选中且所用模型支持视觉时可用。图片单张上限 12 MiB；临时文件可以按需清理。

## 会话状态

浏览器服务在 `setting/mcp_servers.json` 中标记为 `persistent: true`。项目的普通 MCP 工具仍按每次调用启动子进程的原有方式运行；Browser 使用独立事件循环维持一个 stdio 连接，因此导航后的页面能在后续点击、截图等调用中继续使用。

每个聊天的 worker 进程有自己的浏览器连接。父智能体使用主连接；每个子智能体使用独立连接和 `--isolated` 浏览器环境，避免并行子任务互相点击同一页面。子智能体结束、聊天 worker 退出或浏览器工具超时后会关闭相应连接。连接关闭后，下一次调用会启动新浏览器，之前的未持久化页面状态会丢失。`--isolated` 也意味着登录状态不会跨浏览器重启保存。

配置中的 `--headless` 可去掉以显示浏览器窗口。可见模式需要桌面显示环境；Windows 服务或无桌面会话应继续使用无界面模式。官方 Playwright MCP 也支持通过扩展或 CDP 连接现有浏览器，但这需要另行配置，不属于默认隔离模式。

## 接口与限制

工具定义先进入项目统一的工具注册表，再由聊天循环调用。因此 Chat Completions、Messages 和 Responses 三种模型接口共享同一套浏览器工具与文本工具结果。截图不会自动作为图像注入每一种接口；视觉模型需调用 `read_media`。工具是否出现在某轮请求中，仍取决于该会话的工具选择。

浏览器可以访问模型所请求的网页。项目本身主要面向受信任的本机环境；若要开放给其他用户，应同时限制工具可访问的站点、浏览器下载与上传路径，并为服务增加身份认证。不要把带登录状态的可见浏览器配置为多个用户共用。当前默认使用隔离模式，避免读取日常浏览器配置文件。

## 故障排查

- **工具列表没有 Browser**：先完成上述 `npm ci`，再重启服务或等待 MCP 工具热重载；检查后端日志中的工具发现失败信息。
- **提示缺少浏览器可执行文件**：执行 `node browser/node_modules/playwright/cli.js install chromium`。
- **导航后再次调用出现空白页**：检查工具调用是否超时、聊天 worker 是否已因空闲退出。两者都会关闭隔离浏览器。
- **截图路径无法读取**：确认本轮选择了 `read_media`，模型启用了视觉能力，并且临时图片尚未清理。
- **页面操作超时**：先重新获取 `browser_snapshot`；动态页面可检查控制台和网络请求。全局 `MCP_TOOL_CALL_TIMEOUT_SECONDS` 覆盖连接、启动和工具调用全过程。

官方资料：[Playwright MCP](https://github.com/microsoft/playwright-mcp)、[Playwright 定位器](https://playwright.dev/docs/locators)、[浏览器安装](https://playwright.dev/docs/browsers)。
