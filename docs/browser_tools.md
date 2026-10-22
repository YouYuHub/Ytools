# 浏览器工具：一个 MCP、两套预设、两个运行目标

本项目使用微软官方 `@playwright/mcp`。配置和工具发现只有 **一个 Browser 服务**，当前返回 25 个不重复的工具。浏览器工具按能力定义，点击、输入、快照等能力可用于不同连接，无需按连接模式把工具硬拆成两份。

## 三个概念

| 项目 | 控制什么 |
| --- | --- |
| Browser MCP 服务 | 一份标准工具清单、原始参数与 stdio 传输 |
| 前端工具预设 | 本轮给模型提供哪些工具；可手动修改或全选 |
| mcp_profile | 本次调用使用哪个浏览器运行实例 |

后端按需启动并复用两个微软 MCP 进程，一个独立隔离，一个扩展连接。**两者可以同时存活**，页面、标签索引、元素引用和调用队列分别维护。无需另写 GUI 程序；扩展连接使用用户电脑上的真实 Chrome / Edge（当前配置使用 msedge；--no-webmcp 是服务能力选项）。

`profiles` 是本项目通用连接配置；模型传 `{mcp_profile, arguments}`，项目消费路由字段，将 arguments 内的原始参数发送给微软 MCP。工具名不参与目标分类。详见 [通用 profiles 与完整调用流程](mcp_profiles.md)。

## 前端两套工具预设

打开「＋ → 配置工具」，展开 Browser 后显示工具预设和说明；收起 Browser 时预设、说明与工具列表一起隐藏：

- **网页操作**：当前 17 项，覆盖标签页、导航、快照、查找、点击、输入、表单、下拉选择、拖动、文件操作、键盘、等待等。
- **调试诊断**：当前 12 项，覆盖窗口、媒体模拟、页面 JavaScript、控制台、网络清单与单请求详情、截图，以及导航、快照、标签、查找、等待。
- **全部**：选择服务实际返回的全部工具，当前 25 项；新版服务增加的工具也能全选或手动选择。
- **清空**：只清除此 Browser 服务的勾选，保留其他 MCP 和内置工具选择。

点击预设会替换本服务的勾选，可以继续手动增删；点击确定才保存，取消不改变生效配置。两套预设有少量共用基础工具，不产生重复工具定义。`browser_run_code_unsafe` 保持可选，不自动加入这两套预设；可手动选择或通过「全部」启用，含义以微软原始工具说明为准。

**预设不切换浏览器连接**。可使用任一预设操作独立或扩展目标，连接由每次调用的 `mcp_profile` 决定。

## 如何让模型选择目标

| mcp_profile | 实际效果 |
| --- | --- |
| `isolated` | 独立、无界面的隔离浏览器，适合开发测试 |
| `extension` | 通过 Playwright Extension 连接用户已打开、已登录的页面 |

默认配置沿用 `isolated`。可以直接在聊天中说：

> 使用扩展连接（mcp_profile=extension），列出可访问的标签页，找到标题包含“订单管理”、URL 包含 example.com/orders 的页面，读取快照。多个匹配先让我选择。

也可以同时安排两边：

> 用 extension 查看我已打开的测试页面；另用 isolated 打开 http://127.0.0.1:8000/ 做独立验证。每次调用明确目标，各自保持页面状态。

对应工具参数示例：

```json
{"mcp_profile": "extension", "arguments": {"action": "list"}}
```

```json
{"mcp_profile": "isolated", "arguments": {"url": "http://127.0.0.1:8000/"}}
```

模型应先列出目标连接的标签页，核对标题与 URL，再获取快照、点击和输入。`browser_tabs` 切换标签使用同一连接最新列表中的索引，例如 `{"mcp_profile":"extension","arguments":{"action":"select","index":1}}`。页面变化或用户手动操作后重新取快照；不能把独立浏览器的索引、元素引用交给扩展连接。工具结果会标注 `[MCP 服务: Browser; profile: isolated/extension]`，便于确认归属。

用户可在真实浏览器中与模型交替操作；切换前台标签不等于切换模型目标，应通过 MCP 的标签列表确认。扩展版本与授权决定可访问的页面范围。从手机使用项目前端，操作的仍是运行后端电脑上的浏览器。

## 安装与配置

项目根目录执行：

```powershell
npm ci --prefix browser
node browser/node_modules/playwright/cli.js install chromium
```

Node.js 需要 20 或更新版本，MCP 当前锁定为 `0.0.83`。Chromium 用于独立目标；扩展目标使用本机已安装的 Chrome / Edge。

在目标浏览器安装微软 [Playwright Extension](https://chromewebstore.google.com/detail/playwright-extension/mmlmfjhmonkocbjadbfplnigmagldckm)，打开目标网页并登录。Edge 可允许其他商店的扩展后安装。该扩展与 Codex / ChatGPT 的浏览器扩展不同。

当前只保留以下 Browser 配置（位于 `setting/mcp_servers.json` 的 servers 内）：

```json
"Browser": {
  "type": "stdio",
  "command": "node",
  "args": [
    "browser/node_modules/@playwright/mcp/cli.js",
    "--no-webmcp",
    "--browser",
    "msedge"
  ],
  "env": {
    "PLAYWRIGHT_MCP_OUTPUT_DIR": "{temp}/ytools_playwright_artifacts"
  },
  "persistent": true,
  "version": "0.0.83",
  "default_profile": "isolated",
  "profiles": {
    "isolated": {
      "label": "独立隔离浏览器",
      "description": "用于独立测试，页面与登录状态独立。",
      "args": [
        "browser/node_modules/@playwright/mcp/cli.js",
        "--no-webmcp",
        "--browser",
        "msedge",
        "--isolated",
        "--headless"
      ],
      "env": {
        "PLAYWRIGHT_MCP_OUTPUT_DIR": "{temp}/ytools_playwright_artifacts/isolated",
        "PLAYWRIGHT_MCP_EXTENSION": "false",
        "PLAYWRIGHT_MCP_ISOLATED": "true",
        "PLAYWRIGHT_MCP_HEADLESS": "true",
        "PLAYWRIGHT_MCP_CDP_ENDPOINT": "",
        "PLAYWRIGHT_MCP_ENDPOINT": "",
        "PLAYWRIGHT_MCP_EXTENSION_TOKEN": "",
        "PLAYWRIGHT_MCP_PROFILE_DIR_NAME": "",
        "PLAYWRIGHT_MCP_USER_DATA_DIR": ""
      }
    },
    "extension": {
      "label": "已打开网页 · 扩展连接",
      "description": "用于用户已打开的浏览器页面；首次实际调用需要用户授权并选择标签页。",
      "args": [
        "browser/node_modules/@playwright/mcp/cli.js",
        "--no-webmcp",
        "--browser",
        "msedge",
        "--extension"
      ],
      "env": {
        "PLAYWRIGHT_MCP_OUTPUT_DIR": "{temp}/ytools_playwright_artifacts/extension",
        "PLAYWRIGHT_MCP_EXTENSION": "true",
        "PLAYWRIGHT_MCP_ISOLATED": "false",
        "PLAYWRIGHT_MCP_HEADLESS": "false",
        "PLAYWRIGHT_MCP_CDP_ENDPOINT": "",
        "PLAYWRIGHT_MCP_ENDPOINT": ""
      }
    }
  }
}
```

- 公共 command/env 可继承，profile.args 显式设置两种完整启动参数；不再由代码根据工具名或模式改写启动参数。args 是替换，env 是合并。
- 当前两个连接使用 Edge；修改通道时需同时调整对应 profile 的完整 args。扩展专有的资料目录或 token 只配置在 extension 中。
- 输出目录在 profile.env 中分别指定。保留 persistent=true 才能跨工具调用保留页面状态。
- 不会立即常驻两个进程；首次业务调用按需建立对应连接。发现阶段会短暂启动各配置读取工具清单，随后释放。
- 扩展目标首次实际调用需要用户在浏览器授权、选择标签页；握手和工具发现不要求授权业务网页。

## 连接生命周期

### env、persistent、profiles 的区别

| 字段 | 用途 | 能否用于其他 MCP |
| --- | --- | --- |
| env | 子进程环境；服务地址、认证或输出目录，键和值均为字符串 | 可用于所有已配置的 stdio MCP；变量名由服务决定 |
| persistent | 聊天中复用进程和 MCP 连接，保留连接内状态 | 可用于其他 stdio MCP；profile 可覆盖公共值 |
| profiles | 定义多个连接的 command/args/env/persistent 和可选工具白名单 | 通用，不依赖工具名称、服务名或浏览器品牌 |
| default_profile / locked_profile | 默认连接 / 固定连接 | 通用；固定值在后端强制校验 |

这些是本项目客户端配置，其他客户端不一定识别。env 继承后端环境再逐层覆盖，支持 `{temp}` 和系统环境变量展开。args 在 profile 内整体替换，输出目录不再由代码自动追加。详情和其他服务示例见 [通用 profiles](mcp_profiles.md)。

### 连接释放与复用

连接按「服务、聊天 worker、父/子智能体范围、运行目标」分别复用。isolated → extension → isolated 不会重新启动前一个正常连接。某目标超时或取消仅清理该目标连接；子智能体结束清理自己的两个目标，worker 退出清理全部。扩展连接重建后可能需要再次授权，连接释放不会撤回已发生的网页操作。

扩展目标沿用真实浏览器授权页面；不同子智能体的连接不会自动复制日常登录环境。并发客户端还需遵循扩展对标签页归属的限制，同一真实页面应避免被多个智能体同时控制。

Chat Completions、Messages、Responses 三种模型接口共用同一份工具注册和运行路由。工具选择以当前服务 ID 与工具清单为准；服务已移除或工具名失效时，需在配置工具面板或任务模板中重新选择，不做旧浏览器服务名、旧调用别名迁移。用户自行配置的其他 MCP 服务保留各自身份。

## MCP 测试与验证

「配置工具 → MCP 测试」只列一个 Browser，额外选择 **连接配置（profile）** 即可测试两个运行实例。路由目标放在HTTP 请求顶层 mcp_profile；JSON/表单工具参数仍使用微软返回的原始 Schema。

连接测试只握手、读取清单；实际调用才执行网页动作。extension 首次实际调用要在浏览器授权，可将测试超时调为 120 秒。诊断使用独立临时连接，不复用聊天连接；每项结束后释放，不能通过逐项诊断组成持续页面工作流。

可执行 `python test/manual_browser_smoke.py`，验证独立目标的真实导航、点击、控制台/网络、截图和父子智能体隔离。扩展真实网页动作需用户完成授权后验证。

本次更新包括 Python 路由和配置变更，请在正在运行的任务结束或停止后重启后端，再刷新前端。

官方参考：[Playwright MCP](https://github.com/microsoft/playwright-mcp)、[Playwright Extension](https://github.com/microsoft/playwright/blob/main/packages/extension/README.md)、[浏览器安装](https://playwright.dev/docs/browsers)。
