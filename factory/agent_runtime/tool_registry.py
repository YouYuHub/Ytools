"""MCP 工具发现、schema 过滤和运行时注册表。"""
import asyncio
import hashlib
import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

from env_manager import load_var
from util.mcp_client import get_mcp_tools

from util.logger import get_logger

logger = get_logger("factory.agent_runtime.tool_registry")



ALL_TOOLS: List[Dict[str, Any]] = []
TOOL_MCP_SERVERS: Dict[str, str] = {}
# Model-visible name -> original MCP name. TOOL_MCP_SERVERS and this map together
# identify a tool by (server_id, original_name), even when servers reuse names.
TOOL_ORIGINAL_NAMES: Dict[str, str] = {}
TOOL_NAME_INDEX: Dict[str, List[str]] = {}

# 工具注册表全局交换锁：工具发现既可能跑在事件循环线程（生成任务 / /tools/list），
# 也可能跑在配置热重载线程（mcp_servers.json 的 servers 变更时），
# 最终的全局赋值必须原子，避免读方看到半更新的注册表
_REGISTRY_SWAP_LOCK = threading.Lock()

# 工具探测结果缓存：MCP 工具发现要逐服务拉起子进程做完整握手（Python 服务的
# 子进程冷启动约 1s，磁盘缓存冷/杀软扫描时可达数十秒），而 /tools/list 与
# 每条消息的发送路径过去都在逐请求重探。缓存保存最近一次 refresh_tools_from_mcp
# 的完整响应体，TTL 内直接复用；MCP_TOOLS_CACHE_TTL_SECONDS <= 0 可禁用。
# 失效时机由两条强制重探路径保证：mcp_servers.json 的 servers 变更触发热重载
# 重探（main.py 配置监听），前端"配置工具"弹窗的刷新按钮走 /tools/list?refresh=1
_TOOLS_CACHE: Dict[str, Any] | None = None
_TOOLS_CACHE_MONO: float = 0.0
_TOOLS_CACHE_LOCK = threading.Lock()

_DEFAULT_TOOLS_CACHE_TTL_SECONDS = 60.0


def get_tool_registry_snapshot() -> tuple[list[dict], dict[str, str], dict[str, str], dict[str, list[str]]]:
    """Read tools and their identity maps from the same atomic registry version."""
    with _REGISTRY_SWAP_LOCK:
        return (
            list(ALL_TOOLS),
            dict(TOOL_MCP_SERVERS),
            dict(TOOL_ORIGINAL_NAMES),
            {name: list(aliases) for name, aliases in TOOL_NAME_INDEX.items()},
        )


def _qualified_tool_name(server_id: str, original_name: str, reserved: set[str], used: set[str]) -> str:
    """Build a stable, provider-safe model name for an ambiguous MCP tool."""
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", original_name).strip("_-") or "tool"
    identity = f"{server_id}\0{original_name}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    base = f"mcp_{digest}_{slug[:45]}"[:64]
    candidate = base
    suffix = 2
    while candidate in reserved or candidate in used:
        tail = f"_{suffix}"
        candidate = f"{base[:64 - len(tail)]}{tail}"
        suffix += 1
    return candidate


def resolve_tool_selection(
    selection: Any,
    tool_servers: dict[str, str] | None = None,
    original_names: dict[str, str] | None = None,
) -> list[str]:
    """Resolve persisted {server_id: [names]} selections to current model names.

    New snapshots store model-visible names. Legacy snapshots store raw MCP names;
    the server key lets us migrate duplicate names without guessing.
    """
    if not isinstance(selection, dict):
        return []
    if tool_servers is None or original_names is None:
        _tools, snapshot_servers, snapshot_originals, _index = get_tool_registry_snapshot()
        tool_servers = snapshot_servers
        original_names = snapshot_originals
    resolved: list[str] = []
    seen: set[str] = set()
    for server_id, names in selection.items():
        server_key = str(server_id)
        if not isinstance(names, list):
            continue
        for value in names:
            if not isinstance(value, str) or not value.strip():
                continue
            name = value.strip()
            if server_key == "__builtin__":
                canonical = name
            elif (
                name in tool_servers
                and tool_servers.get(name) == server_key
            ):
                canonical = name
            else:
                matches = [
                    visible_name
                    for visible_name, source_server in tool_servers.items()
                    if source_server == server_key
                    and original_names.get(visible_name) == name
                ]
                canonical = matches[0] if len(matches) == 1 else name
            if canonical not in seen:
                seen.add(canonical)
                resolved.append(canonical)
    return resolved


def canonicalize_requested_names(
    names: Any,
    tool_servers: dict[str, str] | None = None,
    name_index: dict[str, list[str]] | None = None,
) -> tuple[list[str], list[str]]:
    """Canonicalize current and legacy flat tool_names; ambiguous raw names fail closed."""
    if not isinstance(names, list):
        return [], []
    if tool_servers is None or name_index is None:
        _tools, snapshot_servers, _originals, snapshot_index = get_tool_registry_snapshot()
        tool_servers = snapshot_servers
        name_index = snapshot_index
    canonical_names: list[str] = []
    unresolved: list[str] = []
    seen: set[str] = set()
    try:
        from factory.agent_runtime.builtin_tools import is_builtin_tool
    except Exception:
        is_builtin_tool = lambda _name: False
    for value in names:
        if not isinstance(value, str) or not value.strip():
            continue
        name = value.strip()
        if is_builtin_tool(name) or name in tool_servers or name not in name_index:
            canonical = name
        else:
            matches = [visible_name for visible_name in name_index[name] if visible_name in tool_servers]
            if len(matches) == 1:
                canonical = matches[0]
            else:
                unresolved.append(name)
                continue
        if canonical not in seen:
            seen.add(canonical)
            canonical_names.append(canonical)
    return canonical_names, unresolved


def tools_cache_ttl_seconds() -> float:
    """工具探测缓存 TTL（秒）；<=0 表示禁用缓存（每次请求都重新探测）。"""
    try:
        ttl = float(load_var("MCP_TOOLS_CACHE_TTL_SECONDS", _DEFAULT_TOOLS_CACHE_TTL_SECONDS))
    except (TypeError, ValueError):
        return _DEFAULT_TOOLS_CACHE_TTL_SECONDS
    return ttl


def get_cached_tools_payload(max_age_seconds: float) -> Dict[str, Any] | None:
    """返回仍在 TTL 内的工具探测缓存；无缓存/已过期/禁用缓存时返回 None。"""
    if max_age_seconds <= 0:
        return None
    with _TOOLS_CACHE_LOCK:
        if _TOOLS_CACHE is None:
            return None
        if time.monotonic() - _TOOLS_CACHE_MONO > max_age_seconds:
            return None
        return dict(_TOOLS_CACHE)


def _store_tools_cache(payload: Dict[str, Any]) -> None:
    global _TOOLS_CACHE, _TOOLS_CACHE_MONO
    # 浅拷贝后再存：调用方（路由层）可能在返回体上追加 mode 等展示字段，
    # 不能让这些字段渗进缓存
    with _TOOLS_CACHE_LOCK:
        _TOOLS_CACHE = dict(payload)
        _TOOLS_CACHE_MONO = time.monotonic()


def _load_mcp_servers(_current_dir: str = "") -> dict:
    _ = _current_dir  # 参数保留兼容，实际使用项目根目录
    server_path = Path(__file__).parent.parent.parent / "setting" / "mcp_servers.json"
    if not server_path.exists():
        return {}
    with server_path.open("r", encoding="utf-8") as file:
        data = json.load(file)
    servers = data.get("servers", {}) if isinstance(data, dict) else {}
    return servers if isinstance(servers, dict) else {}


# def _resolve_server_ref(current_dir: str, server_ref: str) -> str:
#     if not server_ref:
#         return ""
#     server_path = Path(server_ref)
#     if server_path.exists() or server_path.suffix:
#         return server_ref
#     servers = _load_mcp_servers(current_dir)
#     server_info = servers.get(server_ref)
#     if isinstance(server_info, dict):
#         command = server_info.get("command")
#         args = server_info.get("args", [])
#         if command:
#             if isinstance(args, list) and args:
#                 return " ".join([str(command), *[str(item) for item in args]])
#             return str(command)
#     return server_ref


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
        "anyOf", "allOf", "oneOf",
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
        "format", "default",
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
    """同步兼容入口：仅在没有运行中的事件循环时刷新工具。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(refresh_tools_from_mcp(current_dir))
        return
    if loop.is_running():
        return
    loop.run_until_complete(refresh_tools_from_mcp(current_dir))


async def refresh_tools_from_mcp(current_dir: str) -> dict[str, Any]:
    global ALL_TOOLS, TOOL_MCP_SERVERS, TOOL_ORIGINAL_NAMES, TOOL_NAME_INDEX
    server_ids = _read_server_ids(current_dir)
    if not server_ids:
        with _REGISTRY_SWAP_LOCK:
            ALL_TOOLS = []
            TOOL_MCP_SERVERS = {}
            TOOL_ORIGINAL_NAMES = {}
            TOOL_NAME_INDEX = {}
        payload = {
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
        _store_tools_cache(payload)
        return payload
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
    results = await asyncio.gather(*[_fetch_server_tools(server_id) for server_id in server_ids])
    elapsed_ms = int((time.perf_counter() - started_at) * 1000)
    filtered_tools: list[dict[str, Any]] = []
    tool_mcp_servers: dict[str, str] = {}
    tool_original_names: dict[str, str] = {}
    tool_name_index: dict[str, list[str]] = {}
    discovered: list[tuple[str, str, dict]] = []
    seen_identities: set[tuple[str, str]] = set()
    failed_servers: list[str] = []
    server_metrics: list[dict[str, Any]] = []
    for server_id, server_tools, server_error, server_elapsed in results:
        if server_error is not None or server_tools is None:
            failed_servers.append(server_id)
            server_metrics.append({
                "server_id": server_id,
                "ok": False,
                "tool_count": 0,
                "elapsed_ms": server_elapsed,
                "error": str(server_error) if server_error is not None else "unknown",
            })
            continue
        server_metrics.append({
            "server_id": server_id,
            "ok": True,
            "tool_count": len(server_tools),
            "elapsed_ms": server_elapsed,
        })
        for tool in server_tools:
            tool_name = getattr(tool, "name", None)
            if not isinstance(tool_name, str) or not tool_name.strip():
                continue
            tool_name = tool_name.strip()
            identity = (server_id, tool_name)
            if identity in seen_identities:
                logger.warning(f"⚠️ MCP 服务 [{server_id}] 重复注册工具 [{tool_name}]，忽略重复项")
                continue
            seen_identities.add(identity)
            tool_definition = {
                "type": "function",
                "function": {
                    "name": tool_name,
                    "description": getattr(tool, "description", "") or "",
                    "parameters": getattr(tool, "parameters", {}) or {},
                },
            }
            discovered.append((server_id, tool_name, tool_definition))

    # Raw names remain unchanged when they are globally unique and do not collide
    # with a builtin/control tool. Otherwise expose stable qualified names.
    raw_name_counts: dict[str, int] = {}
    for _server_id, original_name, _definition in discovered:
        raw_name_counts[original_name] = raw_name_counts.get(original_name, 0) + 1
    try:
        from factory.agent_runtime.builtin_tools import SELECTABLE_BUILTIN_TOOL_NAMES, COMMAND_TOOL_NAMES
        reserved_names = set(SELECTABLE_BUILTIN_TOOL_NAMES) | COMMAND_TOOL_NAMES | {"over_task"}
    except Exception:
        reserved_names = {"over_task"}
    all_raw_names = set(raw_name_counts)
    used_names: set[str] = set()
    for server_id, original_name, tool_definition in discovered:
        needs_qualified_name = (
            raw_name_counts[original_name] > 1
            or original_name in reserved_names
            or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", original_name) is None
        )
        visible_name = (
            _qualified_tool_name(server_id, original_name, reserved_names | all_raw_names, used_names)
            if needs_qualified_name else original_name
        )
        used_names.add(visible_name)
        filtered_tool = _filter_tool_for_api(tool_definition)
        filtered_tool["function"]["name"] = visible_name
        filtered_tools.append(filtered_tool)
        tool_mcp_servers[visible_name] = server_id
        tool_original_names[visible_name] = original_name
        tool_name_index.setdefault(original_name, []).append(visible_name)
    # 原子交换注册表全局变量：读方（生成任务/工具列表接口）要么看到旧、要么看到新
    with _REGISTRY_SWAP_LOCK:
        ALL_TOOLS = filtered_tools
        TOOL_MCP_SERVERS = tool_mcp_servers
        TOOL_ORIGINAL_NAMES = tool_original_names
        TOOL_NAME_INDEX = tool_name_index
    api_tools = []
    for tool in filtered_tools:
        tool_with_server = dict(tool)
        func_name = (
            tool.get("function", {}).get("name", "")
            if isinstance(tool.get("function"), dict)
            else ""
        )
        tool_with_server["server_id"] = tool_mcp_servers.get(func_name, "")
        tool_with_server["original_name"] = tool_original_names.get(func_name, func_name)
        api_tools.append(tool_with_server)
    logger.info(f"✅ 已加载 {len(filtered_tools)} 个工具（{len(server_metrics)} 个 MCP 服务器）")
    for metric in server_metrics:
        if not metric["ok"]:
            logger.warning(f"   ⚠️ 服务器 [{metric['server_id']}] 加载失败"
                f"（{metric['elapsed_ms']}ms）：{metric['error']}")
    payload = {
        "tools": api_tools,
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
    _store_tools_cache(payload)
    return payload
