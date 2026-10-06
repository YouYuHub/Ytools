# Ytools 智能体工具服务

Ytools 是一个基于 FastAPI 的本地智能体工具项目，包含流式聊天、MCP 工具调用、会话历史与上下文压缩、文件和媒体处理、子智能体，以及原生 JavaScript 编写的 H5 界面。

## 本地运行

建议使用 Python 3.10 或更新版本，并在项目根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python main.py
```

上传接口接收单文件 `application/octet-stream` 原始请求体，通过 `filename` 查询参数传文件名；多文件由前端逐个请求，无需 `python-multipart`。文档解析依赖在 `requirements.txt` 中列为可选项，按实际需要安装和验证。已知问题：`requirements.txt` 可选段中的 `docx` 包名有误（`docx` 是另一个无关包），实际应安装 `python-docx`。服务端口默认是 `48621`；启动后可访问 `http://127.0.0.1:48621/docs` 查看 API 文档。

当前 `main.py` 的 `/` 返回 JSON，不提供 H5 页面。可以直接在 Edge 或 Chrome 中打开 `H5/index.html`；其他浏览器不保证全部功能正常；前端默认请求 `http://127.0.0.1:48621`。如后端使用其他地址，参见 [H5 前端说明](H5/README.md#2-指定后端地址)。

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
- `model_selection` 支持四个角色：`chat_model`（聊天）、`compaction_model`（上下文压缩）、`title_model`（会话标题）、`sub_agent_model`（子智能体）；其中 `compaction_model` 与 `sub_agent_model` 仅支持 chat-completions 协议。
- 角色的 `parameter`（生成参数）按协议分桶存放（`chat_completions` / `messages` / `responses`），`headers` 为自定义请求头；两者均可省略，也可以直接在前端「参数」面板中修改（写入同一份配置）。
- 可选 `supportDocTypes`（例如 `[".pdf", ".docx"]`）声明模型原生接受的文档扩展名。它独立于 `vision` 图片能力；在「配置工具」→「内置工具」中启用 `read_document` 后，工具会按目标文件扩展名选择原生或解析文本。文本支持 `offset` / `limit`，原生 PDF 支持页区间；原生文件只在工具调用后的下一次模型请求中发送一次。
- 内置文件工具按需勾选：`read_file` 有 64 MiB 读取上限，默认连续读取 200 行，并用 `next_start_line` 指示续读；`edit_file` 支持单处替换或 `edits` 列表一次完成多处替换，`expected_hash` 可校验读后变更；`write_file` 的新调用须明确指定 `mode=create|overwrite|append`，旧调用参数仍兼容；`search_files` 默认按普通文本搜索，正则需指定 `is_regex=true`。勾选 `run_command` 后，模型还可使用 `poll_command`、`stop_command` 管理后台任务；已有会话可在「配置工具」中选择默认 shell。`read_media` 对仍在上下文的图片避免重复注入，已回收的视频区间可以重新读取。
- 修改保存后自动热重载生效，无需重启服务。

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
- `servers` 修改后会自动重新探测工具（配置热重载，默认 5 秒轮询）；每次工具调用都会重新启动服务子进程，改完脚本后下次调用即生效。

## 工作路径与模型参数（会话独立机制）

工作路径与模型参数均为「全局默认 + 会话独立」两级配置，会话内的修改只对本会话生效：

- **工作路径**（聊天状态栏右侧双击修改）
  - 新对话中修改 → 保存为全局默认（`.env` 的 `DEFAULT_CHAT_WORK_DIR`），作为之后新会话的初始工作目录；
  - 已有会话中修改 → 只保存为本会话的独立目录，清空输入恢复跟随全局默认。
- **模型参数**（「参数」面板，含聊天 / 压缩 / 标题 / 子智能体四个角色）
  - 新对话中修改 → 保存为全局默认（`models.json` 的 `model_selection`）；
  - 已有会话中修改 → 只保存为本会话的独立选择，面板中「清除会话覆盖」恢复跟随全局默认。

独立机制：会话首次正式开始任务时，会把当时的全局默认（工作路径 / 工具选择 / 模型选择）固化为会话独立配置；此后该会话不再跟随全局设置变化，全局默认的修改只影响之后的新会话。

## 使用边界

本项目当前适合在受信任的本机环境中运行。代码默认监听 `0.0.0.0`，允许所有 CORS 来源，HTTP 路由没有统一身份认证；`GET /file/get_local_file` 还允许按绝对路径读取本机文件。仅本机使用时，请将 `.env` 中的 `DEFAULT_SERVICE_HOST` 设为 `127.0.0.1`。在加入认证、限制本地文件读取范围之前，不应把端口开放到不受信任的网络。

上传接口在读取原始请求体时执行大小限制；文档和小型媒体在限制内读入内存，视频及 GIF 写入临时文件后保存。不要把当前实现当作面向公网的上传服务。重启维护工具所依赖的服务快照写入在 `main.py` 中被注释，项目根目录也没有 `restart_helper.py`，因此不要依赖该工具完成自动重启。

## 测试与开发文档

后端测试位于 `test/`。安装 `pytest` 后，可在项目根目录运行 `python -m pytest test -q`；部分 `manual_*.py` 是需要外部服务或人工操作的脚本，不属于普通单元测试。前端测试位于 `H5/test_h5/`，在 `H5/` 目录执行 `npm test`。样式修改应编辑 `H5/style/scss/`，再按 [H5 文档](H5/README.md#4-scss-构建与测试)编译。

专题文档：[API 接口说明](docs/api_docs.md) · [上下文压缩](docs/compact.md) · [文件 Diff 与历史版本链](docs/file_diff.md) · [子智能体](docs/sub_agent_v1.md) · [引用选中文本](docs/quote_selection.md)。

## 本地凭据配置

克隆后将 `.env.example` 复制为 `.env`，并将 `setting/models.example.json` 复制为 `setting/models.json`，再在本机填写自己的凭据。真实配置文件已加入 Git 忽略规则，不应提交到仓库。

注意：`env_manager.py` 支持 `enc:dpapi:` 前缀的 DPAPI 加密凭据，但解密仅在 Windows 上可用；非 Windows 环境加载时会按原样返回密文（无法解密），该格式不要在跨平台部署中使用。


### 前端语音输入

点击麦克风可在光标或选区处输入中文语音，识别预览实时显示。有文字时可直接点击发送或按回车，发送前冻结当前可见文本并停止识别，迟到结果不会写回输入框。再次点击麦克风会正常结束识别并接收最后一段结果；修改输入框正文、切换会话或离开页面会取消旧识别。连续识别在断开后自动重启，连续五个周期未识别到内容则停止并提示。权限、设备和网络错误有独立提示。

语音功能使用浏览器 Web Speech API，不调用项目后端转写接口；是否可用及转写准确性取决于浏览器的语音服务。


网页工具使用与限制见 [网页搜索与抓取说明](docs/web_tools.md)。

### 工具文本编码

终端、文本/CSV 文档和网页读取的编码策略及旧编码用法见 [工具文本编码说明](docs/text_encoding.md)。PowerShell 子会话默认 UTF-8 读写项目文本；无 BOM 的旧编码文件需显式指定编码。
