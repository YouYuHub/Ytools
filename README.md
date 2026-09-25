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

上传接口接收单文件 `application/octet-stream` 原始请求体，通过 `filename` 查询参数传文件名；多文件由前端逐个请求，无需 `python-multipart`。文档解析依赖在 `requirements.txt` 中列为可选项，按实际需要安装和验证。服务端口默认是 `48621`；启动后可访问 `http://127.0.0.1:48621/docs` 查看 API 文档。

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

- `inputs` 列出要启用的工具（`服务名: [工具名]`），只有启用的工具才会提供给模型；也可以在前端「配置工具」弹窗中勾选，保存后写回此键。
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

专题文档：[上下文压缩](docs/compact.md) · [文件 Diff](docs/file_diff.md) · [子智能体](docs/sub_agent_v1.md)。
