from fastapi import APIRouter, HTTPException

from config import get_current_dir
from factory import tool_registry

# 实例化APIRouter
api_tools_manage_router = APIRouter(prefix="/tools")


@api_tools_manage_router.get("/list")
async def api_list_tools():
    """
    实时列出全部可用工具（基于 mcp_servers.json 探测 MCP 服务）
    Returns:
        JSON格式的字典，包含工具列表和统计信息
    """
    try:
        result = await tool_registry.refresh_tools_from_mcp(get_current_dir())
        result["mode"] = "runtime"
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取工具列表失败: {str(e)}")
