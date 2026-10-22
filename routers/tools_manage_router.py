import asyncio
import json
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from config import get_current_dir
from factory.agent_runtime import tool_registry
from util import mcp_diagnostics

# 实例化APIRouter
api_tools_manage_router = APIRouter(prefix="/tools")


class MCPDiagnosticRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    server_id: str = Field(min_length=1, max_length=512)
    action: Literal["connect", "call"] = "connect"
    tool_name: str | None = Field(default=None, max_length=256)
    arguments: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: float = Field(default=30, ge=1, le=300, allow_inf_nan=False)
    mcp_profile: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")

    @field_validator("arguments")
    @classmethod
    def bounded_arguments(cls, value):
        if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")) > 65536:
            raise ValueError("测试参数不能超过 64KB")
        return value


@api_tools_manage_router.get("/diagnostics")
async def diagnostic_servers():
    try:
        return mcp_diagnostics.configured_servers()
    except mcp_diagnostics.DiagnosticFailure as exc:
        raise HTTPException(422, str(exc)) from exc


@api_tools_manage_router.post("/diagnostics")
async def test_mcp_server(payload: MCPDiagnosticRequest, request: Request):
    try:
        servers = mcp_diagnostics.load_servers()
    except mcp_diagnostics.DiagnosticFailure as exc:
        raise HTTPException(422, str(exc)) from exc
    if payload.server_id not in servers:
        raise HTTPException(404, "MCP 服务不存在或已从配置中移除")
    if payload.action == "call" and not (payload.tool_name or "").strip():
        raise HTTPException(422, "请填写 MCP 工具原名")
    task = asyncio.create_task(mcp_diagnostics.diagnose(**payload.model_dump()))
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=.2)
            if await request.is_disconnected():
                task.cancel()
                raise HTTPException(499, "测试等待已取消；已经执行的外部操作不会撤回")
        return task.result()
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


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
