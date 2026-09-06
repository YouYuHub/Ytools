from fastapi import APIRouter, HTTPException

from config import get_current_dir
from factory.agent_runtime import tool_registry

# 实例化APIRouter
api_tools_manage_router = APIRouter(prefix="/tools")


@api_tools_manage_router.get("/list")
async def api_list_tools(refresh: bool = False):
    """
    列出全部可用工具（基于 mcp_servers.json 探测 MCP 服务）

    默认复用 TTL 内的探测缓存：工具发现要逐服务拉起 MCP 子进程握手，
    子进程冷启动可能耗时数秒，逐请求重探会拖慢页面首屏与每条消息。
    refresh=1 强制重探（对应前端"配置工具"弹窗的刷新按钮）；
    mcp_servers.json 的 servers 变更时配置热重载线程也会自动重探并刷新缓存。
    Returns:
        JSON格式的字典，包含工具列表和统计信息；mode 字段标记 cache/runtime
    """
    try:
        if not refresh:
            cached = tool_registry.get_cached_tools_payload(tool_registry.tools_cache_ttl_seconds())
            if cached is not None:
                cached["mode"] = "cache"
                return cached
        result = await tool_registry.refresh_tools_from_mcp(get_current_dir())
        result["mode"] = "runtime"
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取工具列表失败: {str(e)}")
