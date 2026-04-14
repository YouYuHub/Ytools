# py 库导入
from typing import Optional
# import asyncio

# fastapi 库导入
from fastapi import APIRouter, HTTPException

# 自定义模块导入
from factory.chat_factory import load_all_tools
from util.mcp_client import (
    get_mcp_tools,
    add_mcp_tools,
    remove_mcp_tools,
    list_mcp_tools,
    call_mcp_tool
)

# 实例化APIRouter
api_tools_manage_router = APIRouter(prefix="/tools")


@api_tools_manage_router.get("/list")
async def api_list_tools(mcp_service_file: str = "all"):
    """
    列出 MCP 服务器上的工具
    Args:
        mcp_service_file: MCP 服务器文件路径，或 "all" 代表所有服务器上的工具
    Returns:
        JSON格式的字典，包含工具列表和统计信息
    """
    try:
        result = await list_mcp_tools(mcp_service_file)
        return result
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取工具列表失败: {str(e)}")


@api_tools_manage_router.post("/add")
async def api_add_tools(mcp_service_file: str = "mcp_server/sys_server.py"):
    """
    添加 MCP 服务器上的工具到 tools.json
    Args:
        mcp_service_file: MCP 服务器文件路径
    Returns:
        JSON格式的字典，包含成功和失败的工具信息
    """
    try:
        result = await add_mcp_tools(mcp_service_file)
        await load_all_tools()
        return result
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"添加工具失败: {str(e)}")


@api_tools_manage_router.delete("/remove")
async def api_remove_tools(mcp_service_file: str = "mcp_server/sys_server.py"):
    """
    从 tools.json 中移除指定 MCP 服务器的工具
    Args:
        mcp_service_file: MCP 服务器文件路径
    Returns:
        JSON格式的字典，包含移除的工具信息和统计
    """
    try:
        result = await remove_mcp_tools(mcp_service_file)
        await load_all_tools()
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"移除工具失败: {str(e)}")


@api_tools_manage_router.get("/get_real_tools")
async def api_get_tools(mcp_server_file: str = "mcp_server/sys_server.py"):
    """
    获取指定 MCP 服务器的所有可用工具（直接从服务器获取，不经过 tools.json）
    Args:
        mcp_server_file: MCP 服务器文件路径
    Returns:
        工具信息列表
    """
    try:
        result = await get_mcp_tools(mcp_server_file)
        if result is None:
            return {"tools": [], "total": 0}
        return {
            "tools": [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters
                }
                for tool in result
            ],
            "total": len(result)
        }
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取工具失败: {str(e)}")


@api_tools_manage_router.post("/call")
async def api_call_tool(
    function_name: str,
    arguments: Optional[dict] = None,
    mcp_service_file: str = "mcp_server/sys_server.py"
):
    """
    调用 MCP 服务器上的工具
    Args:
        function_name: 工具名称
        arguments: 工具参数
        mcp_service_file: MCP 服务器文件路径
    Returns:
        工具执行结果
    """
    try:
        result = await call_mcp_tool(function_name, arguments, mcp_service_file)
        return {
            "success": True,
            "result": result
        }
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"调用工具失败: {str(e)}")
