# coding: utf-8
# 标准库
import os
import json
import traceback
import asyncio
from typing import List, Any, Optional
import sys
import shutil
import shlex
import time
# import platform
from pathlib import Path
# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

# Python 3.11+ ExceptionGroup 支持
try:
    BaseExceptionGroup  # type: ignore[used-before-def]
except NameError:
    BaseExceptionGroup = tuple()  # type: ignore[assignment,misc]

# 第三方库
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# 自定义模块
from config import DEFAULT_MCP_TOOL_CALL_TIMEOUT_SECONDS, FunctionDefinition
from env_manager import load_var


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_mcp_servers_config() -> dict:
    server_path = PROJECT_ROOT / "setting" / "mcp_servers.json"
    if not server_path.exists():
        return {}
    with server_path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        return {}
    servers = data.get("servers", {})
    return servers if isinstance(servers, dict) else {}


def _get_configured_server_command(server_ref: str) -> tuple[str, list[str]] | None:
    """返回配置中的 command 和 args，未命中服务器标识时返回 None。"""
    servers = _load_mcp_servers_config()
    server_info = servers.get(server_ref)
    if not isinstance(server_info, dict):
        return None
    command = server_info.get("command")
    if not isinstance(command, str) or not command.strip():
        return None
    args = server_info.get("args", [])
    if args is None:
        args = []
    if not isinstance(args, list):
        raise ValueError(f"MCP 服务器 [{server_ref}] 的 args 必须是数组")
    return command.strip(), [str(item) for item in args]


def _resolve_project_file(value: str) -> str:
    """将项目根目录下存在的文件解析为绝对路径，其他参数保持原样。"""
    value = str(value)
    if not value:
        return value
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        return str(candidate.resolve()) if candidate.is_file() else value
    project_file = (PROJECT_ROOT / candidate).resolve()
    if project_file.is_file():
        return str(project_file)
    return value


def _resolve_project_command(value: str) -> str:
    """解析命令中的项目相对路径；普通命令名保留给 PATH 查找。"""
    value = str(value)
    if not value:
        return value
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        return str(candidate.resolve()) if candidate.is_file() else value
    project_candidate = (PROJECT_ROOT / candidate).resolve()
    if project_candidate.is_file() or any(separator in value for separator in ("/", "\\")):
        return str(project_candidate)
    return value


# def _resolve_mcp_server_ref(mcp_service: str) -> str:
#     if not mcp_service:
#         return mcp_service
#     configured = _get_configured_server_command(mcp_service)
#     if configured is not None:
#         command, args = configured
#         resolved_parts = [_resolve_project_command(command)]
#         resolved_parts.extend(_resolve_project_file(item) for item in args)
#         resolved_parts = [item.replace("\\", "/") for item in resolved_parts]
#         # 用 shell quoting 保留带空格参数的边界；build_stdio_server_parameters
#         # 会再次拆分为 command/args 传给 stdio 客户端。
#         return shlex.join(resolved_parts)
#     return mcp_service


def _split_command_line(command_line: str) -> list[str]:
    """拆分直接传入的命令；单独的项目文件路径不经过 shell 解析。"""
    resolved_file = _resolve_project_file(command_line)
    if Path(resolved_file).is_file():
        return [resolved_file]
    parts = shlex.split(command_line, posix=os.name != "nt")
    if os.name == "nt":
        parts = [
            part[1:-1]
            if len(part) >= 2 and part[0] == part[-1] and part[0] in {"'", '"'}
            else part
            for part in parts
        ]
    if not parts:
        raise ValueError("空的服务器命令")
    return parts


def _resolve_command(command: str, original_ref: str) -> str:
    """解析可执行文件，优先使用项目根目录下的相对路径，其次查找 PATH。"""
    command = _resolve_project_command(command)
    command_path = Path(command)
    if command_path.is_absolute() and command_path.is_file():
        return str(command_path.resolve())
    expanded = os.path.expanduser(command)
    command_path = Path(expanded)
    if command_path.is_absolute() and command_path.is_file():
        return str(command_path.resolve())
    command_path_from_path = shutil.which(expanded)
    if command_path_from_path:
        return command_path_from_path
    raise FileNotFoundError(
        f"命令或文件 '{original_ref}' 未找到（请确保在 PATH 中或使用可执行文件路径）"
    )


def _build_stdio_parameters(command: str, args: list[str], original_ref: str) -> StdioServerParameters:
    """按可执行文件类型构建最终的 stdio 启动参数。"""
    command = _resolve_command(command, original_ref)
    command_path = Path(command)
    ext = command_path.suffix.lower()
    if ext == ".py":
        return StdioServerParameters(command=sys.executable, args=[str(command_path), *args])
    if ext == ".js":
        node = shutil.which("node") or shutil.which("nodejs")
        if not node:
            raise FileNotFoundError("Node.js 未在 PATH 中找到，请先安装或将 node 加入 PATH")
        return StdioServerParameters(command=node, args=[str(command_path), *args])
    if ext == ".jar":
        java = shutil.which("java")
        if not java:
            raise FileNotFoundError("Java 未在 PATH 中找到，请先安装或将 java 加入 PATH")
        return StdioServerParameters(command=java, args=["-jar", str(command_path), *args])
    if ext == ".exe" or os.access(str(command_path), os.X_OK):
        return StdioServerParameters(command=str(command_path), args=args)
    # 没有常见扩展名时，尝试按 shebang 选择解释器。
    try:
        with command_path.open("r", encoding="utf-8", errors="ignore") as f:
            first = f.readline().strip()
        if not first.startswith("#!"):
            raise ValueError(f"无法识别的服务器文件类型: {command}")
        shebang = shlex.split(first[2:].strip())
        if not shebang:
            raise ValueError(f"服务器文件 '{command}' 的 shebang 为空")
        interpreter = shutil.which(shebang[0])
        if not interpreter:
            raise FileNotFoundError(f"shebang 指定的解释器 '{shebang[0]}' 未找到")
        return StdioServerParameters(
            command=interpreter,
            args=[*shebang[1:], str(command_path), *args],
        )
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"无法构建服务器启动参数: {exc}") from exc


def build_stdio_server_parameters(mcp_service: str) -> StdioServerParameters:
    """
    根据传入的服务器标识（文件路径或命令）构建 StdioServerParameters。
    如果传入的是 mcp_servers.json 中的服务器标识，则按配置中的 command/args
    顺序构造启动参数；项目内文件的相对路径按 PROJECT_ROOT 解析。
    支持：
      - Python 脚本 (.py)：使用当前 Python 解释器运行
      - Node.js 脚本 (.js)：使用 `node` 运行
      - Java JAR (.jar)：使用 `java -jar` 运行
      - 可执行文件 (.exe 或具有可执行权限的二进制)
      - 命令字符串（如 'node server.js' 或 'my-server'，将通过 PATH 查找可执行文件）
    """
    if not mcp_service or not isinstance(mcp_service, str):
        raise ValueError("mcp_service 必须是非空字符串")
    svc = mcp_service.strip()
    configured = _get_configured_server_command(svc)
    if configured is not None:
        # 配置中的 command/args 已经是结构化数据，不能先拼成字符串再解析，
        # 否则带空格的文件参数会丢失边界，也会把整条命令误判为文件路径。
        command, args = configured
    else:
        parts = _split_command_line(svc)
        command, args = parts[0], parts[1:]
    args = [_resolve_project_file(item) for item in args]
    return _build_stdio_parameters(command, args, svc)


def _format_mcp_error(exc: BaseException) -> str:
    """提取 ExceptionGroup 中的子异常消息，便于诊断。"""
    if isinstance(exc, BaseExceptionGroup):
        messages = []
        for sub in exc.exceptions:
            msg = _format_mcp_error(sub)
            if msg:
                messages.append(msg)
        if messages:
            return "; ".join(messages)
    return str(exc)


def _sdk_attr(obj: Any, new_name: str, old_name: str, default: Any = None) -> Any:
    """按 2.0 协议优先读取 SDK 属性，auto 回退 1.x 旧命名。

    mcp SDK 2.0 起 pydantic 字段由 camelCase 改为 snake_case（如 isError -> is_error、
    inputSchema -> input_schema）；优先取 2.0 命名，缺失时回退旧命名，均无则给 default。
    """
    value = getattr(obj, new_name, None)
    if value is None:
        value = getattr(obj, old_name, default)
    return default if value is None else value


async def _upgrade_modern_protocol(session: Any) -> None:
    """mcp 2.0+ 的 discover 流程：把会话升级到 modern 协议（2026-07-28）。

    旧版 SDK 没有 discover 方法（直接跳过，保持握手协商版本）；
    服务端不支持 modern 版本时 discover 会报 Method not found，同样保持原版本即可。
    """
    discover = getattr(session, "discover", None)
    if discover is None:
        return
    try:
        await discover()
    except Exception:
        pass


async def get_mcp_tools(mcp_server: str = "sysServer") -> List[FunctionDefinition] | None:
    """
    获取指定 MCP 服务器的所有可用工具
    Args:
        mcp_server: MCP 服务器配置项
    Returns:
        工具信息列表
    """
    # 之前的单py文件支持实现
    # # 检查服务器文件是否存在
    # if not os.path.exists(mcp_server):
    #     raise FileNotFoundError(f"MCP 服务器文件 '{mcp_server}' 不存在")
    # elif not mcp_server.endswith(".py"):
    #     raise ValueError(f"MCP 服务器文件 '{mcp_server}' 必须是 Python 文件")
    # # print(f"🔧 正在连接 MCP 服务器: {mcp_server}")
    # server_params = StdioServerParameters(
    #     command=sys.executable,  # 使用当前 Python 解释器
    #     args=[mcp_server],
    #     # cwd=os.path.dirname(os.path.abspath(mcp_server)),
    #     # env=os.environ.copy()  # 传递当前环境变量
    # )
    # 构建启动参数，支持多语言/可执行文件/命令（.py/.js/.jar/.exe/可执行文件/PATH 命令）
    try:
        server_params = build_stdio_server_parameters(mcp_server)
    except Exception as e:
        traceback.print_exc()
        raise
    try:
        async with stdio_client(server_params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                # 初始化会话
                await session.initialize()
                # mcp 2.0+：尝试 discover 升级到 modern 协议；旧 SDK/旧服务端自动保持原版本
                await _upgrade_modern_protocol(session)
                # 列出可用工具
                tools_result = await session.list_tools()
                print(f"✅ 成功获取 {len(tools_result.tools)} 个工具")
                # 转换为 FunctionDefinition 对象
                tools = []
                for tool in tools_result.tools:
                    # MCP 协议中工具的参数 schema 属性：2.0 为 input_schema，1.x 为 inputSchema
                    parameters = _sdk_attr(tool, "input_schema", "inputSchema", {})
                    tools.append(FunctionDefinition(
                        name=tool.name,
                        description=tool.description or "",
                        parameters=parameters
                    ))
                return tools
    except Exception as e:
        detail = _format_mcp_error(e)
        print(f"⚠️ MCP 服务器 [{mcp_server}] 连接失败：{detail}")
        # Python 不能 raise 字符串；保留真实异常链，避免工具发现失败时
        # 二次变成“exceptions must derive from BaseException”而丢失根因。
        raise RuntimeError(f"MCP 服务器 [{mcp_server}] 连接失败: {detail}") from e


def _mcp_call_timeout_seconds() -> float:
    """读取 MCP 调用总超时；0 或负数表示不限制。

    tool_executor 也会在同步工作线程外层施加超时。这里再次保护直接调用
    mcp_client 的路径（调试脚本、其他业务代码），避免绕过统一配置后永久
    等待 MCP 服务端响应。
    """
    try:
        return float(load_var(
            "MCP_TOOL_CALL_TIMEOUT_SECONDS",
            DEFAULT_MCP_TOOL_CALL_TIMEOUT_SECONDS,
        ))
    except (TypeError, ValueError):
        return float(DEFAULT_MCP_TOOL_CALL_TIMEOUT_SECONDS)


async def _call_mcp_tool_impl(function_name: str, arguments: dict = None, mcp_service: str = "sysServer") -> Any:
    """ 通过 MCP 客户端调用 MCP 服务器上的工具
    Args:
        function_name: 工具名称
        arguments: 工具参数
        mcp_service: MCP 服务器配置项
    Returns:
        工具执行结果
    """
    # 之前尝试使用标准IO方式调用MCP服务器版本
    # # 检查服务器文件是否存在
    # if not os.path.exists(mcp_service):
    #     raise FileNotFoundError(f"MCP 服务器文件 '{mcp_service}' 不存在")
    # elif not mcp_service.endswith(".py"):
    #     raise ValueError(f"MCP 服务器文件 '{mcp_service}' 必须是 Python 文件")
    # server_params = StdioServerParameters(
    #     command=sys.executable,
    #     args=[mcp_service],
    #     # env=os.environ.copy()
    # )
    # 构建启动参数（支持多语言/可执行文件/命令）
    try:
        server_params = build_stdio_server_parameters(mcp_service)
    except Exception as build_error:
        traceback.print_exc()
        raise build_error
    try:
        async with stdio_client(server_params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                # 初始化会话
                await session.initialize()
                # mcp 2.0+：尝试 discover 升级到 modern 协议；旧 SDK/旧服务端自动保持原版本
                await _upgrade_modern_protocol(session)
                # 验证工具是否存在
                tools = await session.list_tools()
                available_tools = [tool.name for tool in tools.tools]
                if function_name not in available_tools:
                    raise ValueError(
                        f"工具 '{function_name}' 不存在。可用工具: {', '.join(available_tools)}"
                    )
                try:
                    pipe_tools = {"setup_pipe", "run_pipe_command", "read_pipe_history"}
                    max_pipe_retries = 3 if function_name in pipe_tools else 1
                    last_error: Optional[Exception] = None
                    for attempt in range(1, max_pipe_retries + 1):
                        # 调用工具
                        result = await session.call_tool(function_name, arguments or {})
                        # 解析结果
                        # 注意：即使 result.content 为空，也可能是合法的返回值（如空列表 []）
                        # 2.0 协议优先，auto 兼容 1.x（isError）
                        if _sdk_attr(result, "is_error", "isError", False):
                            error_text = result.content[0].text if result.content else "未知错误"
                            is_pipe_connect_error = "Failed to connect to named-pipe server" in error_text
                            if is_pipe_connect_error and attempt < max_pipe_retries:
                                wait_seconds = 0.3 * attempt
                                print(f"[WARN] {function_name} 第{attempt}次调用命名管道连接失败，{wait_seconds:.1f}s 后重试")
                                await asyncio.sleep(wait_seconds)
                                continue
                            last_error = ValueError(f"MCP 工具执行失败: {error_text}")
                            break
                        if result.content and len(result.content) > 0:
                            texts = []
                            for content_item in result.content:
                                if hasattr(content_item, 'text') and content_item.text:
                                    texts.append(content_item.text)
                            return "\n".join(texts) if texts else ""
                        else:
                            # 没有 content 但也没有错误，可能是空列表等合法返回值
                            # 返回空列表表示成功但无内容
                            return []
                    if last_error is not None:
                        raise last_error
                except Exception as call_error:
                    # 重新抛出工具调用错误
                    raise call_error
    except ExceptionGroup as eg:
        # 解包 ExceptionGroup，提取第一个有意义的异常
        # ExceptionGroup 可能嵌套多层，需要递归查找

        def extract_real_exception(exc_group):
            """递归提取 ExceptionGroup 中的真实异常"""
            if hasattr(exc_group, 'exceptions') and exc_group.exceptions:
                # 取第一个异常
                first_exc = exc_group.exceptions[0]
                # 如果还是 ExceptionGroup，继续递归
                if isinstance(first_exc, ExceptionGroup):
                    return extract_real_exception(first_exc)
                return first_exc
            return exc_group
        
        real_exception = extract_real_exception(eg)
        # 如果是 ValueError（工具验证错误），直接抛出
        if isinstance(real_exception, ValueError):
            raise real_exception
        # 其他异常，包装后抛出
        raise RuntimeError(f"MCP 调用失败: {real_exception}") from real_exception
    except Exception as e:
        # 其他异常直接抛出
        raise e


async def call_mcp_tool(function_name: str, arguments: dict = None, mcp_service: str = "sysServer") -> Any:
    """通过 MCP 客户端调用工具，并对完整生命周期施加配置的总超时。"""
    timeout_seconds = _mcp_call_timeout_seconds()
    call = _call_mcp_tool_impl(function_name, arguments, mcp_service)
    if timeout_seconds > 0:
        return await asyncio.wait_for(call, timeout=timeout_seconds)
    return await call


if __name__ == '__main__':
    import asyncio
    try:
        start_time = time.time()
        # res = asyncio.run(call_mcp_tool('setup_pipe', {
        #     # 这里是函数的参数字典，比如 'a': 10, b: 20
        #     "pipe_name": r"\\.\pipe\default_server",
        #     "terminal_mode": r"cmd.exe /k chcp 65001",
        #     # "terminal_mode": r"powershell.exe",
        #     # "first_command": "ssh root@120.48.43.229",
        #     # "first_command": "powershell",
        #     "first_command": "powershell -Command \"Get-Process | Where-Object {$_.MainWindowTitle -ne ''} | Select-Object MainWindowTitle, ProcessName, Id | Format-Table -AutoSize\"",
        #     "wait_milliseconds": 5000,
        #     "prompt": "C:\\\\Users\\\\Administrator\\\\Desktop\\\\python学习录\\\\main_study\\\\large_model\\\\agent_tool_sse>",
        # }, mcp_service=r"mcp_server\PipeCmdMCP.exe"))
        res = asyncio.run(call_mcp_tool('run_pipe_command', {
            # 这里是函数的参数字典，比如 'a': 10, b: 20
            "command": "sleep 5 && echo hi",
            "pipe_name": r"\\.\pipe\default_server",
            "wait_milliseconds": 6000,
            # "prompt": "",
            "prompt": "agent_tool_sse>",
            # "terminal_mode": r"powershell.exe",
            # "first_command": "ssh root@120.48.43.229",
            "next_command": "123",
        }, mcp_service=r"C:\Users\Administrator\Desktop\C++学习录\MCP\MCPshell\x64\Release\PipeIpcMCP.exe"))
        print('RESULT:', res)
    except Exception as e:
        print(f"\n❌ 程序执行失败: {e}")
        sys.exit(1)
    finally:
        print(f"elapsed time: {time.time() - start_time}")
    print(f"{os.path.basename(__file__)} 运行结束")
