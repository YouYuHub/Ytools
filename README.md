# Ytools

这是一个基于 FastAPI 的智能体工具测试项目，提供聊天、工具管理、文件上传和 MCP 协议集成。

## 主要功能

- FastAPI 服务端接口
- 通过 `chat_tool` 调用 OpenAI/OpenAI 兼容模型
- MCP 工具管理与调用
- 文件上传与解析
- 会话级文件记忆管理

## 运行方式

1. 安装依赖：
   ```bash
   pip install -r requirements.txt
   ```
2. 启动服务：
   ```bash
   python main.py
   ```

## 目录说明

- `main.py`：应用入口
- `routers/`：API 路由定义
- `chat/`：聊天工具客户端实现
- `mcp_server/`：MCP 服务相关代码
- `memory/`：记忆管理模块
- `util/`：辅助工具模块
- `config.py`：Pydantic 配置模型
