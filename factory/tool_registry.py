import json
import asyncio
import time
from pathlib import Path
from typing import Any, Dict, List
from util.mcp_client import get_mcp_tools
from env_manager import load_var


ALL_TOOLS: List[Dict[str, Any]] = []
TOOL_MCP_SERVERS: Dict[str, str] = {}


def _load_mcp_servers(_current_dir: str = "") -> dict:
    _ = _current_dir  # 参数保留兼容，实际使用项目根目录
    server_path = Path(__file__).parent.parent / "setting" / "mcp_servers.json"
    if not server_path.exists():
        return {}
    with server_path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    servers = data.get("servers", {}) if isinstance(data, dict) else {}
    return servers if isinstance(servers, dict) else {}


def _resolve_server_ref(current_dir: str, server_ref: str) -> str:
    if not server_ref:
        return ""
    server_path = Path(server_ref)
    if server_path.exists() or server_path.suffix:
        return server_ref
    servers = _load_mcp_servers(current_dir)
    server_info = servers.get(server_ref)
    if isinstance(server_info, dict):
        command = server_info.get("command")
        args = server_info.get("args", [])
        if command:
            if isinstance(args, list) and args:
                return " ".join([str(command), *[str(item) for item in args]])
            return str(command)
    return server_ref


def _read_server_ids(current_dir: str) -> list[str]:
    servers = _load_mcp_servers(current_dir)
    return [str(server_id) for server_id in servers.keys()]


def _parse_positive_int(value: Any, default_value: int) -> int:
    try:
        parsed = int(value)
        return parsed if parsed > 0 else default_value
    except (TypeError, ValueError):
        return default_value


def _parse_non_negative_float(value: Any, default_value: float) -> float:
    try:
        parsed = float(value)
        return parsed if parsed >= 0 else default_value
    except (TypeError, ValueError):
        return default_value


def _filter_parameter_property(prop_def: dict) -> dict:
    if not isinstance(prop_def, dict):
        return prop_def
    filtered = {}
    standard_fields = {
        "type", "description", "enum", "const",
        "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
        "minLength", "maxLength", "pattern",
        "format", "default", "items",
        "anyOf", "allOf", "oneOf"
    }
    for key, value in prop_def.items():
        if key in standard_fields:
            if key == "items" and isinstance(value, dict):
                filtered[key] = _filter_parameters(value)
            else:
                filtered[key] = value
    return filtered


def _filter_parameters(params: dict) -> dict:
    if not isinstance(params, dict):
        return params
    filtered = {}
    standard_fields = {
        "type", "properties", "required", "items",
        "additionalProperties", "enum", "const",
        "anyOf", "allOf", "oneOf", "not",
        "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
        "minLength", "maxLength", "pattern",
        "minItems", "maxItems", "uniqueItems",
        "format", "default"
    }
    for key, value in params.items():
        if key in standard_fields:
            if key == "properties" and isinstance(value, dict):
                filtered[key] = {
                    prop_name: _filter_parameter_property(prop_def)
                    for prop_name, prop_def in value.items()
                }
            elif key == "items" and isinstance(value, dict):
                filtered[key] = _filter_parameters(value)
            else:
                filtered[key] = value
    return filtered


def _filter_tool_for_api(tool_def: dict) -> dict:
    if not isinstance(tool_def, dict):
        return tool_def
    filtered = {}
    if "type" in tool_def:
        filtered["type"] = tool_def["type"]
    if "function" in tool_def and isinstance(tool_def["function"], dict):
        func_info = tool_def["function"]
        filtered_func = {}
        if "name" in func_info:
            filtered_func["name"] = func_info["name"]
        if "description" in func_info:
            filtered_func["description"] = func_info["description"]
        if "parameters" in func_info and isinstance(func_info["parameters"], dict):
            filtered_func["parameters"] = _filter_parameters(func_info["parameters"])
        filtered["function"] = filtered_func
    return filtered


def load_all_tools(current_dir: str) -> None:
    """兼容入口：同步触发一次刷新（仅在无运行事件循环时可用）"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(refresh_tools_from_mcp(current_dir))
        return
    if loop.is_running():
        return
    loop.run_until_complete(refresh_tools_from_mcp(current_dir))


async def refresh_tools_from_mcp(current_dir: str) -> dict[str, Any]:
    global ALL_TOOLS, TOOL_MCP_SERVERS
    server_ids = _read_server_ids(current_dir)
    if not server_ids:
        ALL_TOOLS = []
        TOOL_MCP_SERVERS = {}
        return {
            "tools": [],
            "total": 0,
            "servers": [],
            "failed_servers": [],
            "discovery": {
                "max_concurrency": 0,
                "timeout_seconds": 0,
                "elapsed_ms": 0,
            },
        }

    default_concurrency = min(4, len(server_ids))
    configured_concurrency = _parse_positive_int(
        load_var("MCP_DISCOVERY_MAX_CONCURRENCY", default_concurrency),
        default_concurrency,
    )
    max_concurrency = min(configured_concurrency, len(server_ids))
    timeout_seconds = _parse_non_negative_float(
        load_var("MCP_DISCOVERY_TIMEOUT_SECONDS", 12),
        12.0,
    )

    semaphore = asyncio.Semaphore(max_concurrency)

    async def _fetch_server_tools(server_id: str):
        fetch_started = time.perf_counter()
        async with semaphore:
            try:
                if timeout_seconds > 0:
                    server_tools = await asyncio.wait_for(get_mcp_tools(server_id), timeout=timeout_seconds)
                else:
                    server_tools = await get_mcp_tools(server_id)
                elapsed = int((time.perf_counter() - fetch_started) * 1000)
                return server_id, server_tools, None, elapsed
            except Exception as fetch_error:
                elapsed = int((time.perf_counter() - fetch_started) * 1000)
                return server_id, None, fetch_error, elapsed

    started_at = time.perf_counter()
    tasks = [_fetch_server_tools(server_id) for server_id in server_ids]
    results = await asyncio.gather(*tasks)
    elapsed_ms = int((time.perf_counter() - started_at) * 1000)

    filtered_tools: list[dict[str, Any]] = []
    tool_mcp_servers: dict[str, str] = {}
    failed_servers: list[str] = []
    server_metrics: list[dict[str, Any]] = []

    for server_id, server_tools, server_error, server_elapsed in results:
        if server_error is not None or server_tools is None:
            failed_servers.append(server_id)
            server_metrics.append(
                {
                    "server_id": server_id,
                    "ok": False,
                    "tool_count": 0,
                    "elapsed_ms": server_elapsed,
                    "error": str(server_error) if server_error is not None else "unknown",
                }
            )
            continue
        server_metrics.append(
            {
                "server_id": server_id,
                "ok": True,
                "tool_count": len(server_tools),
                "elapsed_ms": server_elapsed,
            }
        )
        for tool in server_tools:
            tool_name = getattr(tool, "name", None)
            if not isinstance(tool_name, str) or not tool_name.strip():
                continue
            tool_definition = {
                "type": "function",
                "function": {
                    "name": tool_name,
                    "description": getattr(tool, "description", "") or "",
                    "parameters": getattr(tool, "parameters", {}) or {},
                },
            }
            filtered_tool = _filter_tool_for_api(tool_definition)
            filtered_tools.append(filtered_tool)
            # 保留服务器标识，让 mcp_client 读取结构化 command/args，避免
            # 先用空格拼接后丢失带空格参数的边界。
            tool_mcp_servers[tool_name] = server_id

    ALL_TOOLS = filtered_tools
    TOOL_MCP_SERVERS = tool_mcp_servers
    print(
        f"✅ 已加载 {len(filtered_tools)} 个工具（{len(server_metrics)} 个 MCP 服务器）"
    )
    for metric in server_metrics:
        if not metric["ok"]:
            print(
                f"   ⚠️ 服务器 [{metric['server_id']}] 加载失败"
                f"（{metric['elapsed_ms']}ms）：{metric['error']}"
            )

    return {
        "tools": filtered_tools,
        "total": len(filtered_tools),
        "servers": server_ids,
        "failed_servers": failed_servers,
        "discovery": {
            "max_concurrency": max_concurrency,
            "timeout_seconds": timeout_seconds,
            "elapsed_ms": elapsed_ms,
        },
        "server_metrics": server_metrics,
    }
