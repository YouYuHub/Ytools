# coding: utf-8
# 标准库
import os
import json
import traceback
from typing import List, Any
import sys
from pathlib import Path
# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

# 第三方库
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# 自定义模块
from config import FunctionDefinition


async def get_mcp_tools(mcp_server_file: str = "mcp_server/sys_server.py") -> List[FunctionDefinition] | None:
    """
    获取指定 MCP 服务器的所有可用工具
    Args:
        mcp_server_file: MCP 服务器文件路径
    Returns:
        工具信息列表
    """
    # 检查服务器文件是否存在
    if not os.path.exists(mcp_server_file):
        raise FileNotFoundError(f"MCP 服务器文件 '{mcp_server_file}' 不存在")
    elif not mcp_server_file.endswith(".py"):
        raise ValueError(f"MCP 服务器文件 '{mcp_server_file}' 必须是 Python 文件")
    # print(f"🔧 正在连接 MCP 服务器: {mcp_server_file}")
    server_params = StdioServerParameters(
        command=sys.executable,  # 使用当前 Python 解释器
        args=[mcp_server_file],
        # cwd=os.path.dirname(os.path.abspath(mcp_server_file)),
        # env=os.environ.copy()  # 传递当前环境变量
    )
    try:
        async with stdio_client(server_params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                # 初始化会话
                await session.initialize()
                # 列出可用工具
                tools_result = await session.list_tools()
                print(f"✅ 成功获取 {len(tools_result.tools)} 个工具")
                # 转换为 FunctionDefinition 对象
                tools = []
                for tool in tools_result.tools:
                    # MCP 协议中工具的参数 schema 属性是 inputSchema
                    parameters = {}
                    if hasattr(tool, 'inputSchema'):
                        parameters = tool.inputSchema
                    elif hasattr(tool, 'input_schema'):
                        parameters = tool.input_schema
                    elif hasattr(tool, 'parameters'):
                        parameters = tool.parameters
                    tools.append(FunctionDefinition(
                        name=tool.name,
                        description=tool.description or "",
                        parameters=parameters
                    ))
                return tools
    except Exception as e:
        # print(f"❌ 连接 MCP 服务器失败: {str(e)}")
        traceback.print_exc()
        raise f"error: {e}"


async def add_mcp_tools(mcp_service_file: str = "mcp_server/sys_server.py") -> dict:
    """ 添加 MCP 服务器上的工具，添加到 tools.json 文件中
    Args:
        mcp_service_file: MCP 服务器文件路径
    Returns:
        JSON格式的字典，包含成功和失败的工具信息：
        {
            "success": [
                {"name": "tool_name", "description": "...", "server_file": "..."}
            ],
            "failed": [
                {"name": "tool_name", "reason": "工具已存在", "server_file": "..."}
            ],
            "total_success": 0,
            "total_failed": 0
        }
    说明：tools.json 格式如下：
    [
        {
            "type": "function",
            "function": {
                "server_file": "mcp_server/sys_server.py",
                "name": "search_web",
                "description": "搜索指定关键词的网页",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "搜索关键词"},
                    },
                    "required": ["query"],  # 可选
                },
            },
        },
        ...
    ]
    """
    # 检查服务器文件是否存在
    if not os.path.exists(mcp_service_file):
        raise FileNotFoundError(f"MCP 服务器文件 '{mcp_service_file}' 不存在")
    elif not mcp_service_file.endswith(".py"):
        raise ValueError(f"MCP 服务器文件 '{mcp_service_file}' 必须是 Python 文件")
    # 获取所有可用工具
    # print(f"📦 正在从 {mcp_service_file} 获取工具...")
    tools = await get_mcp_tools(mcp_service_file)
    if not tools:
        # print("⚠️ 未获取到任何工具")
        return {
            "success": [],
            "failed": [],
            "total_success": 0,
            "total_failed": 0
        }
    # print(f"✅ 成功获取 {len(tools)} 个工具")
    # 定义工具JSON文件路径
    tools_json_path = "tools.json"
    # 读取现有工具列表（如果文件存在）
    existing_tools = []
    if os.path.exists(tools_json_path):
        try:
            with open(tools_json_path, 'r', encoding='utf-8') as f:
                existing_tools = json.load(f)
            # print(f"📖 已加载现有的 {len(existing_tools)} 个工具")
        except Exception as e:
            # print(f"⚠️ 读取现有工具文件失败: {e}，将创建新文件")
            existing_tools = []
    # 结果统计
    success_tools = []
    failed_tools = []
    for tool in tools:
        # 构建工具定义
        tool_definition = {
            "type": "function",
            "function": {
                "server_file": mcp_service_file,
                "name": tool.name,
                "description": tool.description or "",
                "parameters": tool.parameters or {}
            }
        }
        # 检查是否已存在同名工具（相同名称且相同服务器文件）
        is_duplicate = False
        for existing_tool in existing_tools:
            if (existing_tool.get("function", {}).get("name") == tool.name):
                # and existing_tool.get("function", {}).get("server_file") == mcp_service_file  # 忽略服务器文件
                is_duplicate = True
                break
        if is_duplicate:
            # 工具已存在，记录失败
            failed_tools.append({
                "name": tool.name,
                "reason": "工具已存在",
                "server_file": mcp_service_file
            })
            # print(f"  ❌ 工具载入失败: {tool.name} (原因: 工具已存在)")
        else:
            # 添加工具到列表
            existing_tools.append(tool_definition)
            success_tools.append({
                "name": tool.name,
                "description": tool.description or "",
                "server_file": mcp_service_file
            })
            # print(f"  ➕ 新增工具: {tool.name}")
    # 保存工具列表到JSON文件（仅当有成功添加的工具时）
    if success_tools:
        try:
            with open(tools_json_path, 'w', encoding='utf-8') as f:
                json.dump(existing_tools, f, ensure_ascii=False, indent=4)
            # print(f"\n💾 工具列表已保存到 {tools_json_path}")
        except Exception as e:
            # print(f"❌ 保存工具文件失败: {e}")
            traceback.print_exc()
            raise f"error: {e}"
    # 构建返回结果
    result = {
        "success": success_tools,
        "failed": failed_tools,
        "total_success": len(success_tools),
        "total_failed": len(failed_tools)
    }
    # print(f"\n📊 载入结果:")
    # print(f"   - 成功: {result['total_success']} 个")
    # print(f"   - 失败: {result['total_failed']} 个")
    # print(f"   - 总计: {len(existing_tools)} 个")
    return result


async def remove_mcp_tools(mcp_service_file: str = "mcp_server/sys_server.py") -> dict:
    """ 移除 MCP 服务器上的工具，从 tools.json 文件中移除
    Args:
        mcp_service_file: MCP 服务器文件路径
    Returns:
        JSON格式的字典，包含移除的工具信息和统计：
        {
            "removed": [
                {"name": "tool_name", "server_file": "..."}
            ],
            "total_removed": 0
        }
    """
    # # 检查服务器文件是否存在（可选，如果只是想清理无效引用可以注释掉）
    # if not os.path.exists(mcp_service_file):
    #     print(f"⚠️ 警告: MCP 服务器文件 '{mcp_service_file}' 不存在，但仍将尝试从工具列表中移除相关引用")
    # 定义工具JSON文件路径
    tools_json_path = "tools.json"
    # 检查工具文件是否存在
    if not os.path.exists(tools_json_path):
        # print(f"⚠️ 工具文件 '{tools_json_path}' 不存在，无需移除")
        return {
            "removed": [],
            "total_removed": 0
        }
    # 读取现有工具列表
    try:
        with open(tools_json_path, 'r', encoding='utf-8') as f:
            existing_tools = json.load(f)
        # print(f"📖 已加载 {len(existing_tools)} 个工具")
    except Exception as e:
        # print(f"❌ 读取工具文件失败: {e}")
        raise f"error: {e}"
    # 查找需要移除的工具
    removed_tools = []
    remaining_tools = []
    for tool in existing_tools:
        server_file = tool.get("function", {}).get("server_file", "")
        tool_name = tool.get("function", {}).get("name", "")
        if server_file == mcp_service_file:
            # 记录要移除的工具
            removed_tools.append({
                "name": tool_name,
                "server_file": server_file
            })
            # print(f"  🗑️  移除工具: {tool_name}")
        else:
            # 保留其他工具
            remaining_tools.append(tool)
    # 如果没有找到要移除的工具
    if not removed_tools:
        # print(f"ℹ️  未找到来自 '{mcp_service_file}' 的工具")
        return {
            "removed": [],
            "total_removed": 0
        }
    # 保存更新后的工具列表
    try:
        with open(tools_json_path, 'w', encoding='utf-8') as f:
            json.dump(remaining_tools, f, ensure_ascii=False, indent=4)
        # print(f"\n💾 工具列表已更新并保存到 {tools_json_path}")
    except Exception as e:
        # print(f"❌ 保存工具文件失败: {e}")
        traceback.print_exc()
        raise f"error: {e}"
    # 构建返回结果
    result = {
        "removed": removed_tools,
        "total_removed": len(removed_tools)
    }
    # print(f"\n📊 移除结果:")
    # print(f"   - 已移除: {result['total_removed']} 个")
    return result


async def list_mcp_tools(mcp_service_file: str = "all") -> dict:
    """ 列出 MCP 服务器上的工具
    Args:
        mcp_service_file: MCP 服务器文件路径，或 "all" 代表所有服务器上的工具
    Returns:
        JSON格式的字典，包含工具列表和统计信息：
        {
            "tools": [
                {
                    "name": "tool_name",
                    "description": "...",
                    "server_file": "...",
                    "parameters": {...}
                }
            ],
            "total": 0,
            "servers": ["server1.py", "server2.py"]
        }
    """
    # 定义工具JSON文件路径
    tools_json_path = "tools.json"
    # 检查工具文件是否存在
    if not os.path.exists(tools_json_path):
        # print(f"⚠️ 工具文件 '{tools_json_path}' 不存在")
        return {
            "tools": [],
            "total": 0,
            "servers": []
        }
    # 读取现有工具列表
    try:
        with open(tools_json_path, 'r', encoding='utf-8') as f:
            existing_tools = json.load(f)
    except Exception as e:
        print(f"❌ 读取工具文件失败: {e}")
        raise f"error: {e}"
    # 过滤工具
    filtered_tools = []
    servers_set = set()
    if mcp_service_file == "all":
        # 返回所有工具
        filtered_tools = existing_tools
        # print(f"📋 列出所有工具")
    else:
        # # 检查服务器文件是否存在
        # if not os.path.exists(mcp_service_file):
        #     print(f"⚠️ 警告: MCP 服务器文件 '{mcp_service_file}' 不存在")
        # 只返回指定服务器的工具
        for tool in existing_tools:
            server_file = tool.get("function", {}).get("server_file", "")
            if server_file == mcp_service_file:
                filtered_tools.append(tool)
        # print(f"📋 列出 {mcp_service_file} 的工具")
    # 提取工具信息和服务器列表
    tools_info = []
    for tool in filtered_tools:
        func_info = tool.get("function", {})
        tools_info.append({
            "name": func_info.get("name", ""),
            "description": func_info.get("description", ""),
            "server_file": func_info.get("server_file", ""),
            "parameters": func_info.get("parameters", {})
        })
        servers_set.add(func_info.get("server_file", ""))
    # 构建返回结果
    result = {
        "tools": tools_info,
        "total": len(tools_info),
        "servers": sorted(list(servers_set))
    }
    # print(f"✅ 共找到 {result['total']} 个工具")
    # if result['servers']:
    #     print(f"📁 涉及 {len(result['servers'])} 个服务器: {', '.join(result['servers'])}")
    return result


async def call_mcp_tool(function_name: str, arguments: dict = None, mcp_service_file: str = "mcp_server/sys_server.py") -> Any:
    """ 通过 MCP 客户端调用 MCP 服务器上的工具
    Args:
        function_name: 工具名称
        arguments: 工具参数
        mcp_service_file: MCP 服务器文件路径
    Returns:
        工具执行结果
    """
    # 检查服务器文件是否存在
    if not os.path.exists(mcp_service_file):
        raise FileNotFoundError(f"MCP 服务器文件 '{mcp_service_file}' 不存在")
    elif not mcp_service_file.endswith(".py"):
        raise ValueError(f"MCP 服务器文件 '{mcp_service_file}' 必须是 Python 文件")
    
    server_params = StdioServerParameters(
        command=sys.executable,
        args=[mcp_service_file],
        # env=os.environ.copy()
    )
    
    try:
        async with stdio_client(server_params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                # 初始化会话
                await session.initialize()
                # 验证工具是否存在
                tools = await session.list_tools()
                available_tools = [tool.name for tool in tools.tools]
                if function_name not in available_tools:
                    raise ValueError(
                        f"工具 '{function_name}' 不存在。可用工具: {', '.join(available_tools)}"
                    )
                
                try:
                    # 调用工具
                    result = await session.call_tool(function_name, arguments)
                    # 解析结果
                    # 注意：即使 result.content 为空，也可能是合法的返回值（如空列表 []）
                    if result.isError:
                        # 如果工具执行出错，抛出异常
                        error_text = result.content[0].text if result.content else "未知错误"
                        raise ValueError(f"MCP 工具执行失败: {error_text}")
                    
                    if result.content and len(result.content) > 0:
                        # 如果只有一个 content 元素，直接返回其文本
                        if len(result.content) == 1:
                            return result.content[0].text
                        else:
                            # 如果有多个 content 元素，将它们合并（适用于返回列表的情况）
                            texts = []
                            for content_item in result.content:
                                if hasattr(content_item, 'text') and content_item.text:
                                    texts.append(content_item.text)
                            # 如果所有元素都是文本，尝试判断是否是列表形式
                            # 对于 list_dir_item 这样的函数，返回的是多个独立的文本项
                            return texts
                    else:
                        # 没有 content 但也没有错误，可能是空列表等合法返回值
                        # 返回空列表表示成功但无内容
                        return []
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


if __name__ == '__main__':
    import asyncio
    try:
        result = asyncio.run(get_mcp_tools())
        # print("获取到的工具列表:")
        # print("="*50)
        # print(result)
        # if result:
        #     for i, tool in enumerate(result, 1):
        #         print(f"{i}. {tool.name}: {tool.description[:50]}...")
        # else:
        #     print("未获取到任何工具")
        # print("="*50)
        # result = asyncio.run(add_mcp_tools())
        # print("工具添加结果:")
        # print(result)
        # print("="*50)
        # result = asyncio.run(list_mcp_tools())
        # print("工具列表结果:")
        # print(result)
        # print("="*50)
        # result = asyncio.run(remove_mcp_tools())
        # print("工具删除结果:")
        # print(result)
        print("="*50)
        # result = asyncio.run(call_mcp_tool("format_current_time"))
        # print(f"工具执行结果: {result}")
        # result = asyncio.run(call_mcp_tool("list_dir_item"))
        # print(f"工具执行结果: {result}")
        # result = asyncio.run(call_mcp_tool("create_dir", {"dir_path": "垃圾测试"}))
        # print(f"工具执行结果: {result}")
        result = asyncio.run(call_mcp_tool("create_file", {"full_file_name": "垃圾测试/test.txt", "content": "测试内容"}))
        print(f"工具执行结果: {result}")
    except Exception as e:
        print(f"\n❌ 程序执行失败: {e}")
        sys.exit(1)
