# 页面与工具模块拆分、验证记录（2026-10-25）

本轮先建立真实浏览器回归和差分基线，再分阶段移动代码。原消息与工具执行入口保留，模型侧参数定义保持一致。

## 拆分范围

| 模块 | 职责 | 原入口的处理 |
| --- | --- | --- |
| `H5/pages.json`、`util/ui_pages.py` | 四个入口页的登录要求、初始改密入口和脚本加载策略 | HTTP 页面白名单、账号初始化和资源版本生成共用同一清单 |
| `H5/js/app/message_diff.js` | 文件差异 DOM、语言高亮、工具路径标签 | `messages.js` 保留转发函数，继续负责消息编排 |
| `factory/agent_runtime/file_diff_result.py` | 纯差异统计、展示 diff 与精简模型回执 | `builtin_tools.py` 保留原函数签名并传入现有预算 |
| `factory/agent_runtime/tool_inputs.py` | 计划、提问和子任务参数规整 | 原函数名、参数和策略覆盖入口继续有效 |

`messages.js` 从 5386 行降至 5169 行；`builtin_tools.py` 从 4870 行降至 4507 行。新模块分别承接展示、纯计算和参数规则，文件写入、权限、锁和命令执行仍走原执行入口。

页面清单校验文件名、重复项、布尔字段、未知字段和必需入口策略；非法配置立即报错。新增根目录 HTML 页面必须登记并通过身份初始化及资源哈希测试。资源版本脚本仍使用 `python H5/version_assets.py`。

## 拆分前后的差分核对

- 166 组文件差异和模型回执逐项相同：覆盖混合换行、Unicode、空文件、无变化、大文件跳过、超长差异截断及变更行区间。
- 154 组参数规整结果逐项相同：覆盖非法输入、重复计划 ID、状态修正、问题选项及子任务长度边界。
- 177 组差异 DOM/路径输出逐项相同，使用实际 Prism 语言组件，包括多文件和流式未闭合路径参数。
- 工具参数定义与拆分前一致；其余 102 个执行器/辅助函数 AST 完全一致。

本地差分基线保存在忽略目录 `tmp/refactor-*-baseline.json`，原源码快照在 `tmp/refactor-before-20261025/`。永久回归同时覆盖模块独立导入、策略覆盖和输入不被修改，避免依赖这些临时文件。

## 真实浏览器测试

入口为 `test/test_web_page_flows.py`，由 pytest 启动独立 FastAPI/SQLite/临时文件服务，再驱动无头 Chromium 加载真实页面与脚本。模型输出为本地桩，其余账号、MFA、Cookie/CSRF、文件版本和流式任务链使用项目实现。测试不连接现有常驻服务，浏览器仅访问临时服务，MCP 配置与工作目录也被隔离。

四条流程：

1. 匿名跳转登录，完成密码与 MFA 登录，主页面打开独立编辑器，保存后核对内容，再回退至基线。
2. 已绑定账号在账号页生成另一验证器二维码并确认；旧标签页凭证过期时自动刷新 CSRF 并成功重试。
3. 同一浏览器切换账号，确认草稿隔离、旧页清理自己的缓存、跨账号会话和文件请求被拒绝。
4. 浏览器主动断开模型流并刷新，后台任务继续完成并落盘，重新打开会话显示最后一帧内容。

编辑器流程还通过真实消息入口挂载差异视图，检查拆出的渲染模块和 HTML 加载顺序。

可重复运行：

```powershell
python -m pytest test/test_web_page_flows.py -q
npm --prefix H5 test
python -m pytest test/test_ui_pages.py test/test_tool_helper_boundaries.py test/test_frontend_asset_versions.py -q
```

浏览器测试需要 Node、`browser` 的 npm 依赖和 Playwright Chromium。缺少时明确跳过；准备独立测试机时可执行 `npm --prefix browser ci` 和 `node browser/node_modules/playwright/cli.js install chromium`。普通单元测试不要求安装浏览器。

## 验证进度

- 拆分前：H5 784 项通过，真实浏览器 4 条流程通过。
- 页面清单阶段：页面策略、资产及账号资源 28 项通过。
- 前端拆分阶段：原 784 项通过；新增独立渲染测试后，786 项通过。
- 后端拆分阶段：文件工具、回执、参数、计划和提问等 117 项通过。
- Windows 全量后端首次运行：2210 passed、2 skipped、68 subtests passed，另有 1 个新增浏览器用例失败。原因是模拟工具标题和文件列表出现同名文件，测试点击了错误元素；已限定在文件列表行内点击，并通过实际会话 URL 等待历史加载。
- 修正测试定位后，真实浏览器、页面策略、资源版本与模块边界组合复验：19 passed，其中包含全部 4 条真实浏览器流程。
- Linux（WSL Ubuntu）页面、账号/HTTP、工具/输入与差异回归：173 passed、4 subtests passed。首次运行发现测试虚拟环境缺少锁文件中的 `regex`，按锁定版本补齐后重跑通过；没有为测试修改搜索实现。

各阶段有重叠，不应累计为总用例数。Windows 全量中的两项跳过是可选独立二维码解码依赖缺失；对应测试在开发解释器单独运行，3 项通过，浏览器流程也实际验证二维码加载和确认。macOS 未实测。

完整日志在本地忽略目录 `tmp/page-refactor-*.log`；最终跨页及边界复验见 `page-refactor-final-validation.log`，前端见 `page-refactor-h5-stage3.log`，Linux 见 `page-refactor-linux-final.log`。

## 加载与后续拆分

后端重启后读取统一页面清单；刷新网页加载新的展示模块。现有账号库无需迁移。实际浏览器验收使用独立测试服务，部署时仍需按正常流程重启常驻服务。

后续适合继续拆导航/引用定位和会话持久化。部分旧前端测试按源码标记提取函数，本轮保留标记并用真实浏览器补充入口、依赖顺序和跨页行为验证；后续可逐步迁移到模块公开接口。
