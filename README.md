# Ytools 智能体工具服务

Ytools 是一个基于 FastAPI 的本地智能体工具项目，包含流式聊天、MCP 工具调用、会话历史与上下文压缩、文件和媒体处理、子智能体，以及原生 JavaScript 编写的 H5 界面。

## 本地运行

建议使用 Python 3.10 或更新版本，并在项目根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.lock.txt
python scripts/manage_accounts.py init-admin --username admin
python main.py
```

`requirements.lock.txt` 固定直接与传递依赖版本；升级流程见 [维护说明](docs/project_maintenance.md)。上传接口接收单文件 `application/octet-stream` 原始请求体，通过 `filename` 查询参数传文件名；多文件由前端逐个请求，无需 `python-multipart`。文档解析依赖在 `requirements.txt` 中列为可选项，按实际需要安装和验证；DOCX 解析所需的可选依赖名称是 `python-docx`。服务端口默认是 `48621`；启动后可访问 `http://127.0.0.1:48621/docs` 查看 API 文档。

`main.py` 的 `/` 返回 JSON，`/ui/` 提供登录与 H5 页面。打开 `http://127.0.0.1:48621/ui/`，首次管理员登录需设置 MFA；页面使用同源 Cookie 和 CSRF，不再支持直接从本地文件访问业务 API。账号管理位于侧栏“账号与安全”，接口文档仅管理员可访问。旧数据需明确迁移归属，详见 [安全设置与部署](docs/security_implementation.md)。

工具权限默认“询问”，支持全局默认与每会话独立覆盖。启动命令、调用 MCP、内置工具访问工作目录外资源前需用户批准；“完全控制”保留原来的自由调用行为。等待确认时关闭服务，重启并重新打开原会话后可继续或放弃；执行结果未知的工具不会自动重跑。详见 [权限使用与实现说明](docs/tool_permissions.md)。

首次聊天前需先配置模型与工具（见下文）。不要把真实密钥、`.env`、会话历史或上传文件直接提交到公开仓库。

## 配置模型（setting/models.json）

在 `setting/models.json` 中声明供应商与模型，并用顶层 `model_selection` 选择各角色使用的模型（不需要配置，前端页面手动选择即可）。示例：

```json
{
  "My Provider": {
    "vendor": "custom_endpoint",
    "apiKey": "sk-xxx",
    "apiType": "chat-completions",
    "url": "https://api.example.com/v1",
    "models": {
      "My Chat Model": {
        "id": "my-chat-model",
        "toolCalling": true,
        "vision": false,
        "parameterCapabilities": {
          "temperature": true,
          "top_p": true
        },
        "maxInputTokens": 128000,
        "maxOutputTokens": 8192
      }
    }
  },
  "model_selection": {
    "chat_model": {
      "ownership_name": "My Provider",
      "model_name": "My Chat Model"
    }
  }
}
```

- `ownership_name` 为供应商顶层键名，`model_name` 为 `models` 内的模型键名；`id` 为请求 API 时实际发送的模型 id（缺省时使用键名）。
- `model_selection` 支持四个角色：`chat_model`（聊天）、`compaction_model`（上下文压缩）、`title_model`（会话标题）、`sub_agent_model`（子智能体）；角色选择校验支持 chat-completions、messages 和 responses，子智能体已可为该角色单独选模，协议行为以各角色的运行时适配为准。
- 可选 `parameterCapabilities` 用于声明 Messages/Responses 模型不支持的原生参数（例如 `{"temperature": false, "top_p": false, "thinking_budget": false}`）；默认未声明的参数由供应商自行决定。服务端会再次过滤标记为 `false` 的字段，避免旧会话参数发到上游。
- 角色的 `parameter`（生成参数）按协议分桶存放（`chat_completions` / `messages` / `responses`），`headers` 为自定义请求头；两者均可省略，也可以直接在前端「参数」面板中修改（写入同一份配置）。
- 可选 `supportDocTypes`（例如 `[".pdf", ".docx"]`）声明模型原生接受的文档扩展名。它独立于 `vision` 图片能力；启用 `read_document` 后按目标扩展名选择读取方式。不支持原生表格的模型会获得保留表头和原始行号的 Markdown 表格，支持工作表选择和按行续读；文本支持 `offset` / `limit`，结果按当前模型预算分页。原生 PDF 支持页区间；原生文件只发送一次。XLSX/XLS 解析分别需要 `openpyxl`/`xlrd`，详见 [文档读取参数](docs/api_docs.md#内置-read_document-工具factoryagent_runtimebuiltin_toolspy)。
- 内置工具按需勾选，见 [使用简表](docs/builtin_tools.md)。文件工具支持按行读取、精确编辑、多文件补丁及大文本压缩快照回退；`write_file` 必须明确 `mode=create|overwrite|append`。`search_files` 默认按需扫描，并同时启用 `list_files`；可选 `.gitignore`，结果按游标续读。勾选 `run_command` 同时启用 `poll_command/write_stdin/stop_command`，默认短等待，查询只传 job_id 自动续读，支持交互输入及可选 PTY。`ask_user` 暂停同轮其他调用，前台命令随任务停止而取消；后台任务独立管理。
- 修改保存后自动热重载生效，无需重启服务。

Responses 使用 Python 标准库直接调用 HTTP/JSON/SSE，无需 OpenAI SDK；默认以 `store: true` 和 `previous_response_id` 增量续聊，也可在模型参数面板选择“无状态兼容”，使用 `store: false` 每次发送有效本地上下文。支持图片/文档工具及异常处理；有状态并行保留本地摘要与官方压缩，无状态仅使用本地摘要。前端显示本地上下文；详见 [Responses 使用说明](docs/responses_api_usage.md)、[无状态兼容排查](docs/responses_stateless_2026-10-16.md) 与 [原有状态方案](docs/responses_api_stateful_plan.md)。

## 开发 MCP 工具（setting/mcp_servers.json）

在 `setting/mcp_servers.json` 的 `servers` 中注册 MCP 服务（stdio 启动），例如：

```json
{
  "servers": {
    "MyTools": {
      "type": "stdio",
      "command": "mcp_server/my_server.py",
      "args": []
    }
  },
  "inputs": {
    "MyTools": []
  }
}
```

`command` 支持相对路径(以项目启动文件所在文件为起点的路径) 和绝对路径（和vscode一模一样），支持所有mcp标准协议的服务（stdio），如 `.py` / `.js` / `.jar` / `.exe` 文件或 PATH 中的命令（项目内相对路径按项目根解析，`args` 同理）；`.py` 以当前 Python 解释器运行，`.js` 使用 `node`，`.jar` 使用 `java -jar`。对应的 Python 服务示例：

```python
# coding: utf-8
"""自定义 MCP 工具服务示例。"""
try:
    from mcp.server.mcpserver import MCPServer as FastMCP  # mcp 2.x
except ImportError:
    from mcp.server.fastmcp import FastMCP  # mcp 1.x

server = FastMCP("my-tools")


@server.tool()
async def echo(text: str) -> str:
    """回显输入文本。"""
    return f"echo: {text}"


if __name__ == "__main__":
    server.run(transport="stdio")
```

- `inputs` 列出要启用的工具（`服务名: [工具名]`），只有启用的工具才会提供给模型；也可以在前端「配置工具」弹窗中勾选，保存后写回此键。跨服务重名或与内置工具/`over_task` 冲突的 MCP 工具会获得稳定的模型调用名；配置按服务身份读取旧的原始名称，并在保存时逐步迁移为新名称。
- `servers` 修改后会自动重新探测工具（配置热重载，默认 5 秒轮询）；普通 MCP 工具每次调用重新启动服务子进程。标记 `persistent: true` 的服务在会话 worker 中保持连接，适合需要跨调用保留状态的浏览器工具。

浏览器只配置一个 `Browser` MCP，完整清单只返回一份；前端提供「网页操作」「调试诊断」两套工具勾选预设，也可全选或手动调整。每次调用通过 `mcp_profile=isolated/extension` 选择独立浏览器或 Playwright Extension，后端按需保持两个独立进程并复用连接。工具参数采用 `{mcp_profile, arguments}` 封装；项目消费路由字段后仍按标准 MCP 调用微软原始工具。其他 stdio MCP 也可配置多个连接，详见 [通用 profiles](docs/mcp_profiles.md)。安装、授权与目标指定见 [浏览器工具说明](docs/browser_tools.md)。

在「配置工具 → MCP 测试」可主动测试服务连接，根据工具 Schema 自动生成参数表单，或手动编辑 JSON 执行测试，查看错误分类并导出脱敏报告，详见 [MCP 诊断与手动测试](docs/mcp_diagnostics.md)。

在「＋ → 任务模板」或 `/template` 中，可将任务正文、四种角色的模型引用、工具、预算、工作路径规则、输出格式和验收要求保存为模板；预览后创建独立会话，确认发送才运行。支持从当前配置、Skills 提示词创建，以及 JSON 导入导出，详见 [任务模板说明](docs/task_templates.md)。

## 工作路径与模型参数（会话独立机制）

工作路径与模型参数均为「全局默认 + 会话独立」两级配置，会话内的修改只对本会话生效：

- **工作路径**（聊天状态栏右侧双击修改）
  - 新对话中修改 → 保存为全局默认（`.env` 的 `DEFAULT_CHAT_WORK_DIR`），作为之后新会话的初始工作目录；
  - 已有会话中修改 → 只保存为本会话的独立目录，清空输入恢复跟随全局默认。
- **模型参数**（「参数」面板，含聊天 / 压缩 / 标题 / 子智能体四个角色）
  - 新对话中修改 → 保存为全局默认（`models.json` 的 `model_selection`）；
  - 已有会话中修改 → 只保存为本会话的独立选择，面板中「清除会话覆盖」恢复跟随全局默认。

独立机制：会话首次正式开始任务时，会把当时的全局默认（工作路径 / 工具选择 / 模型选择）固化为会话独立配置；此后该会话不再跟随全局设置变化，全局默认的修改只影响之后的新会话。

## 上下文管理

点击输入框上方的上下文用量、会话菜单“上下文管理”，或输入 `/context`，可查看本地上下文构成、历史与摘要覆盖、待重建状态、最近压缩原因及 Responses 远端链。面板按需读取，兼容手机，详见 [上下文管理说明](docs/context_panel.md)。

## 任务预算

累计 tokens、预估费用、模型调用、工具派发和运行时间均可设为无限制或指定额度。在「＋ → 任务预算与用量」配置全局默认或会话覆盖；主模型、子任务、压缩和标题统一计量。达到额度后可追加额度继续或停止，默认全部无限制。详见 [任务预算与完整用量](docs/task_budgets.md)。

输入框上方显示任务阶段，区分思考、工具、等待确认/预算/回复与结束状态。断线时可重新连接查看后台进度；不会重新发送原提问。实现及维护见 [任务状态与模块拆分](docs/page_maintenance_followup.md)。

侧边栏标题右侧的运行图标可识别前台和后台任务，等待与状态未知使用静态标记，结束后清除。详见 [会话运行标记](docs/session_activity.md)。

## 使用边界

服务默认监听 `127.0.0.1`，业务 API 强制登录、CSRF 和资源归属校验；公开注册关闭。普通成员只可进行无工具聊天；管理员及明确授权的可信操作员可以使用主机工具。账号隔离不等于操作系统执行隔离，当前不对相互不信任的账号开放主机工具。Linux 远程部署使用同机 HTTPS 代理和专用低权限账号，入口及验证步骤见 [安全实施说明](docs/security_implementation.md) 和 [部署模板](deploy/linux/)。

上传接口在读取原始请求体时执行大小限制；文档和小型媒体在限制内读入内存，视频及 GIF 写入临时文件后保存。不要把当前实现当作面向公网的上传服务。重启维护工具所依赖的服务快照写入在 `main.py` 中被注释，项目根目录也没有 `restart_helper.py`，因此不要依赖该工具完成自动重启。

## 测试与开发文档

后端测试位于 `test/`。安装 `pytest` 后，可在项目根目录运行 `python -m pytest test -q`；部分 `manual_*.py` 是需要外部服务或人工操作的脚本，不属于普通单元测试。前端测试位于 `H5/test_h5/`，在 `H5/` 目录执行 `npm test`。样式修改应编辑 `H5/style/scss/`，再按 [H5 文档](H5/README.md#4-scss-构建与测试)编译。

专题文档：[API 接口说明](docs/api_docs.md) · [上下文压缩](docs/compact.md) · [文件 Diff 与历史版本链](docs/file_diff.md) · [子智能体](docs/sub_agent_v1.md) · [引用选中文本](docs/quote_selection.md) · [后台终端管理](docs/terminal_management.md) · [2026-10-12 会话问题修复记录](docs/session_fixes_2026-10-12.md)。

检查快照：[2026-10-23 项目检查与工具能力对比](docs/project_review_2026-10-23.md)。报告保留改进前的判断；已落地能力以 [内置工具简表](docs/builtin_tools.md) 和下方升级记录为准。

工具升级：[首轮记录](docs/tool_improvement_plan.md)、[进一步优化与验证](docs/tool_followup_plan.md)。

维护入口：[项目结构](project_structure.md) · [构建与依赖维护](docs/project_maintenance.md) · [长会话显示修复](docs/frontend_live_tail.md)。

## 本地凭据配置

复制 `.env.example` 为 `.env`，并将 `setting/models.example.json` 复制为 `setting/models.json`，再按本机环境填写配置。项目根目录的 `.gitignore` 排除了真实密钥、`.env`、`setting/models.json`、会话历史和运行日志；提交前仍应检查暂存文件。示例文件仅供结构参考，不应被当作生产凭据。

注意：`env_manager.py` 支持 `enc:dpapi:` 前缀的 DPAPI 加密凭据，但解密仅在 Windows 上可用；非 Windows 环境加载时会按原样返回密文（无法解密），该格式不要在跨平台部署中使用。

## 后端日志（`.env` 的 `LOG_LEVEL`）

后端启动时读取 `.env` 的 `LOG_LEVEL`（`debug` / `info` / `warning` / `error` / `critical`，大小写不敏感，默认 `info`），统一控制后端日志输出，改动后经配置热重载（默认 5 秒轮询）即时生效、无需重启：

- 所有日志带时间戳、级别与原始消息（如 `[2026-01-01 12:00:00] [INFO] model: deepseek-v4.1-flash`）；
- `debug` 时终端额外实时打印 SSE 流式增量数据（模型输出原样透传，便于排查协议问题）；
- `error` 及以上级别追加写入项目根 `log/` 目录（不存在自动创建）的错误日志，记录完整文件路径、行号与函数名，单文件上限 100MiB，滚动保留 5 份；
- `warning` 及以下只在终端输出，不写文件。SSE 推送给前端的告警文案不受该配置影响，保持原样。


### 前端语音输入

点击麦克风可在光标或选区处输入中文语音，识别文本实时写入输入框。有文字时可直接发送；发送、切换会话、新建对话或离开页面会取消当前识别，避免迟到结果写回。再次点击麦克风会正常结束识别并接收最后一段结果。用户编辑输入框不会中断听写，会以编辑后的文本和光标重锚定后续插入位置。浏览器语音服务自然断开后自动续听；连续两次空白续听仍无结果时暂停，点击麦克风可再次启动。权限、设备和网络错误会停止并显示提示。

语音功能使用浏览器 Web Speech API，不调用项目后端转写接口；是否可用及转写准确性取决于浏览器的语音服务。


网页工具使用与限制见 [网页搜索与抓取说明](docs/web_tools.md)；`run_command(background=true)` 创建的后台任务可在 H5 中查看、停止、关闭及下载日志，见[后台终端管理](docs/terminal_management.md)。

### 工具文本编码

终端、文本/CSV 文档和网页读取的编码策略及旧编码用法见 [工具文本编码说明](docs/text_encoding.md)。PowerShell 子会话默认 UTF-8 读写项目文本；无 BOM 的旧编码文件需显式指定编码。

### 供应商媒体限制

支持在供应商或模型条目配置 `mediaLimits`，并针对明确的媒体数量报错自动降级。GIF 转换的视频也按视频配额处理，工具参数、分段读取及回传说明见 [read_media 使用说明](docs/read_media.md)，供应商示例配置见 [媒体限制说明](docs/media_limits.md)。

### 会话归档

会话菜单支持归档；侧栏底部「设置 → 归档会话」可查找、打开、取消归档和删除，归档后仍可继续聊天。侧栏和常规搜索默认只处理未归档会话，可切换为已归档或全部范围；会话文件无需搬迁。使用及备份说明见 [会话归档文档](docs/session_archives.md)。

### 验证与发布

开发检查：`python scripts/release_check.py --profile quick`；完整发布验收：`python scripts/release_check.py --profile release`。门禁包含前后端、必需浏览器流程、备份恢复用例及容量统计基准，结果写入 `tmp/validation/`。安装依赖、Windows/Linux CI 与上线检查见 [统一验收说明](docs/release_validation.md)。

长会话默认先取最近完整轮次，保留完整问题导航，早期记录与大工具结果按需读取；原始历史、模型上下文及导出保持完整，见 [历史分页](docs/history_paging.md)。
