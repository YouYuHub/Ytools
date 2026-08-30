# coding: utf-8
from typing import Any
from pathlib import Path
import json
import sys
from dataclasses import replace
# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from config import (
    DEFAULT_HISTORY_TRIGGER_RATIO,
    DEFAULT_MAX_OVERSIZED_REJECTIONS,
    DEFAULT_OVERSIZED_REJECT_FACTOR,
    DEFAULT_REASONING_RETURN_MAX_LENGTH,
    DEFAULT_SUMMARY_BUDGET_RATIO,
    DEFAULT_TOOL_RESULT_RETURN_MAX_LENGTH,
    set_current_dir,
    get_current_dir,
    get_persisted_work_dir,
    PROJECT_ROOT,
    HistoryCompactionConfig,
    ChatModelSelection,
    ContextReturnConfig,
    McpToolSelection,
)
import env_manager
from env_manager import (
    ChatModelConfigurationError,
    get_current_model_selection,
    get_env_file_path,
    init_path,
    list_available_models,
    load_var,
    select_chat_model,
    set_env_vars,
)
from factory.agent_runtime.chat_runtime import parse_return_length
from factory.agent_runtime.context_compaction import (
    get_context_compaction_defaults,
    get_context_compaction_model_status,
    load_context_compaction_settings,
    resolve_context_compaction_model_config,
    resolve_summary_total_budget,
)

# 创建 API 路由器实例
api_chat_config_router = APIRouter()


def _normalized_dir_for_compare(directory: str | None) -> str | None:
    if not isinstance(directory, str) or not directory.strip():
        return None
    try:
        return str(Path(directory).expanduser().resolve()).replace("\\", "/").casefold()
    except (OSError, RuntimeError, TypeError, ValueError):
        return directory.replace("\\", "/").casefold()


def _history_compaction_memory_state() -> dict[str, str | None]:
    """动态读取 env_manager.env_vars，避免 init_path 重绑定字典后的陈旧引用。"""
    env_names = (
        "HISTORY_COMPACT_KEEP_ROUNDS",
        "HISTORY_COMPACT_TRIGGER_RATIO",
        "HISTORY_COMPACT_SUMMARY_BUDGET_RATIO",
        "HISTORY_COMPACT_REJECT_OVERSIZED_FACTOR",
        "HISTORY_COMPACT_MAX_OVERSIZED_REJECTIONS",
    )
    return {name: env_manager.env_vars.get(name) for name in env_names}


_CONTEXT_RETURN_ENV_NAMES = {
    "reasoning_max_length": "REASONING_RETURN_MAX_LENGTH",
    "tool_result_max_length": "HISTORY_TOOL_RESULT_RETURN_MAX_LENGTH",
}


def _context_return_memory_state() -> dict[str, str | None]:
    """动态读取 env_manager.env_vars，确保实时修改后内存值与 .env 保持一致。"""
    return {name: env_manager.env_vars.get(name) for name in _CONTEXT_RETURN_ENV_NAMES.values()}


def _context_return_config_payload() -> dict:
    return {
        "reasoning_max_length": parse_return_length(
            load_var(_CONTEXT_RETURN_ENV_NAMES["reasoning_max_length"], DEFAULT_REASONING_RETURN_MAX_LENGTH),
            DEFAULT_REASONING_RETURN_MAX_LENGTH,
        ),
        "tool_result_max_length": parse_return_length(
            load_var(_CONTEXT_RETURN_ENV_NAMES["tool_result_max_length"], DEFAULT_TOOL_RESULT_RETURN_MAX_LENGTH),
            DEFAULT_TOOL_RESULT_RETURN_MAX_LENGTH,
        ),
        "defaults": {
            "reasoning_max_length": DEFAULT_REASONING_RETURN_MAX_LENGTH,
            "tool_result_max_length": DEFAULT_TOOL_RESULT_RETURN_MAX_LENGTH,
        },
        "semantics": {
            "0": "不回传",
            "negative": "全部回传",
            "positive": "回传前 N 字符（思考过程保留末尾 N 字符）",
        },
        "env_names": _CONTEXT_RETURN_ENV_NAMES,
        "memory_state": _context_return_memory_state(),
    }


def _available_compaction_models() -> list[dict]:
    """仅暴露当前 ChatLLM 可调用的 Chat Completions 模型。"""
    return [
        model for model in list_available_models()
        if str(model.get("api_type") or "").casefold() == "chat-completions"
    ]


def _history_compaction_config_payload() -> dict:
    settings = load_context_compaction_settings()
    return {
        "keep_rounds": settings.keep_rounds,
        "trigger_ratio": settings.trigger_ratio,
        "summary_budget_ratio": settings.summary_budget_ratio,
        "summary_total_budget": resolve_summary_total_budget(settings),
        "oversized_reject_factor": settings.oversized_reject_factor,
        "max_oversized_rejections": settings.max_oversized_rejections,
        "defaults": get_context_compaction_defaults(),
        "compaction_model": get_context_compaction_model_status(settings),
        "available_compaction_models": _available_compaction_models(),
        "env_names": {
            "keep_rounds": "HISTORY_COMPACT_KEEP_ROUNDS",
            "trigger_ratio": "HISTORY_COMPACT_TRIGGER_RATIO",
            "summary_budget_ratio": "HISTORY_COMPACT_SUMMARY_BUDGET_RATIO",
            "oversized_reject_factor": "HISTORY_COMPACT_REJECT_OVERSIZED_FACTOR",
            "max_oversized_rejections": "HISTORY_COMPACT_MAX_OVERSIZED_REJECTIONS",
        },
        "memory_state": _history_compaction_memory_state(),
    }


@api_chat_config_router.post("/change_chat_dir")
async def change_chat_dir(new_dir: str):
    """
    更改聊天工作目录
    参数:
        new_dir: 新的目录路径
    返回:
        操作结果
    """
    try:
        if not isinstance(new_dir, str) or not new_dir.strip():
            raise HTTPException(status_code=400, detail="new_dir 不能为空")
        if not set_current_dir(new_dir):
            return {
                "state": "failed",
                "message": "更改聊天工作目录失败，请确认路径存在且可访问",
                "current_dir": get_current_dir(),
            }
        current_dir = get_current_dir()
        set_env_vars({"CHAT_WORK_DIR": current_dir})
        return {
            "state": "succeed",
            "message": f"聊天工作目录已更改为: {current_dir}",
            "current_dir": current_dir,
            "persisted_env": load_var("CHAT_WORK_DIR"),
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@api_chat_config_router.get("/chat_config/work_dir")
async def get_chat_work_dir_config():
    """只读返回当前工作目录及项目 .env 中保存的工作目录。"""
    current_dir = get_current_dir()
    persisted_dir = get_persisted_work_dir()
    normalized_current = _normalized_dir_for_compare(current_dir)
    normalized_persisted = _normalized_dir_for_compare(persisted_dir)
    return JSONResponse(content={
        "state": "succeed",
        "current_dir": current_dir,
        "persisted_dir": persisted_dir,
        "is_consistent": bool(
            normalized_current
            and normalized_persisted
            and normalized_current == normalized_persisted
        ),
        "env_name": "CHAT_WORK_DIR",
        "env_value": load_var("CHAT_WORK_DIR"),
        "env_file": get_env_file_path(),
        "source": "project .env",
        "read_only": True,
    })


@api_chat_config_router.get("/chat_config/history_compaction")
async def get_history_compaction_config():
    """获取跨轮历史与单轮工具上下文的完整压缩策略。"""
    return JSONResponse(content=_history_compaction_config_payload())


@api_chat_config_router.post("/chat_config/history_compaction")
async def update_history_compaction_config(payload: HistoryCompactionConfig):
    """更新完整压缩策略。

    压缩模型选择统一由 POST /chat_config/models/select（role=compaction_model）管理，
    本接口不再接收 compaction_model 字段；多余字段会被忽略。
    单轮和跨轮压缩共用聊天/压缩模型窗口与 trigger_ratio 计算阈值。
    """
    candidate_settings = replace(
        load_context_compaction_settings(),
        keep_rounds=int(payload.keep_rounds),
        trigger_ratio=float(payload.trigger_ratio),
        summary_budget_ratio=float(payload.summary_budget_ratio),
        oversized_reject_factor=float(
            payload.oversized_reject_factor
            if payload.oversized_reject_factor is not None
            else load_context_compaction_settings().oversized_reject_factor
        ),
        max_oversized_rejections=int(
            payload.max_oversized_rejections
            if payload.max_oversized_rejections is not None
            else load_context_compaction_settings().max_oversized_rejections
        ),
    )
    try:
        resolve_context_compaction_model_config(candidate_settings)
    except ChatModelConfigurationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    update_env: dict[str, Any] = {
        "HISTORY_COMPACT_KEEP_ROUNDS": int(payload.keep_rounds),
        "HISTORY_COMPACT_TRIGGER_RATIO": float(payload.trigger_ratio),
        "HISTORY_COMPACT_SUMMARY_BUDGET_RATIO": float(payload.summary_budget_ratio),
    }
    if payload.oversized_reject_factor is not None:
        update_env["HISTORY_COMPACT_REJECT_OVERSIZED_FACTOR"] = float(payload.oversized_reject_factor)
    if payload.max_oversized_rejections is not None:
        update_env["HISTORY_COMPACT_MAX_OVERSIZED_REJECTIONS"] = int(payload.max_oversized_rejections)
    try:
        updated = set_env_vars(update_env)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse(content={
        "state": "succeed",
        "updated": updated,
        "config": _history_compaction_config_payload(),
        "memory_state": _history_compaction_memory_state(),
    })


@api_chat_config_router.get("/chat_config/context_return")
async def get_context_return_config():
    """获取思考过程与历史工具结果的最大回传长度配置。"""
    return JSONResponse(content=_context_return_config_payload())


@api_chat_config_router.post("/chat_config/context_return")
async def update_context_return_config(payload: ContextReturnConfig):
    """实时更新两个回传长度并持久化。

    - 写回项目 .env（set_env_vars 同时同步内存 env_vars），下次聊天请求立即生效
    - 0 表示不回传，负数表示全部回传
    """
    try:
        updated = set_env_vars({
            _CONTEXT_RETURN_ENV_NAMES["reasoning_max_length"]: int(payload.reasoning_max_length),
            _CONTEXT_RETURN_ENV_NAMES["tool_result_max_length"]: int(payload.tool_result_max_length),
        })
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse(content={
        "state": "succeed",
        "updated": updated,
        "config": _context_return_config_payload(),
        "memory_state": _context_return_memory_state(),
    })


_MODEL_ROLES = ("chat_model", "compaction_model", "title_model")


# ---------- 工具选择（setting/mcp_servers.json 的 inputs 键） ----------

# 内存中的工具选择快照（服务名 -> 工具名数组），与磁盘 mcp_servers.json 实时同步
_MCP_TOOL_INPUTS_MEMORY: dict[str, list[str]] | None = None


def _mcp_servers_config_path() -> Path:
    return PROJECT_ROOT / "setting" / "mcp_servers.json"


def _read_mcp_servers_file() -> dict:
    path = _mcp_servers_config_path()
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_mcp_servers_file(data: dict) -> None:
    """全量写回 mcp_servers.json（仅替换 inputs 键，其余内容原样保留）。"""
    path = _mcp_servers_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _configured_server_names(data: dict) -> dict:
    servers = data.get("servers")
    return servers if isinstance(servers, dict) else {}


def _normalize_tool_inputs(raw: Any) -> dict[str, list[str]]:
    """规整为 {服务名: [去重工具名]}；非法条目直接丢弃。"""
    if not isinstance(raw, dict):
        return {}
    normalized: dict[str, list[str]] = {}
    for server, tool_names in raw.items():
        if not isinstance(server, str) or not server.strip() or not isinstance(tool_names, list):
            continue
        names: list[str] = []
        for name in tool_names:
            if isinstance(name, str) and name.strip() and name not in names:
                names.append(name)
        normalized[server] = names
    return normalized


@api_chat_config_router.get("/chat_config/tool_selection")
async def get_tool_selection():
    """读取已保存的工具选择（前端页面刷新时调用以恢复勾选）。

    每次都以磁盘 mcp_servers.json 的 `inputs` 键为准并同步内存，
    手工编辑文件后刷新页面即可生效；未出现在 inputs 中的已配置服务补 []。
    """
    global _MCP_TOOL_INPUTS_MEMORY
    data = _read_mcp_servers_file()
    servers = _configured_server_names(data)
    inputs = _normalize_tool_inputs(data.get("inputs"))
    for server in servers:
        inputs.setdefault(str(server), [])
    _MCP_TOOL_INPUTS_MEMORY = inputs
    return JSONResponse(content={
        "state": "succeed",
        "inputs": inputs,
        "servers": [str(server) for server in servers],
        "config_path": str(_mcp_servers_config_path()),
        "memory_state": _MCP_TOOL_INPUTS_MEMORY,
    })


@api_chat_config_router.post("/chat_config/tool_selection")
async def update_tool_selection(payload: McpToolSelection):
    """保存前端选择的工具：实时更新内存并写回 setting/mcp_servers.json。

    Body: {"inputs": {"pipeIpcMcp": ["setup_pipe"], "sysServer": []}}
    - 键为 `servers` 中已配置的服务名，未知服务报 400
    - 全量替换语义：未提及的已配置服务保存为 []
    - 仅替换文件中的 inputs 键，servers 等其余内容保持不变
    """
    global _MCP_TOOL_INPUTS_MEMORY
    data = _read_mcp_servers_file()
    servers = _configured_server_names(data)
    if not servers:
        raise HTTPException(status_code=400, detail="setting/mcp_servers.json 未配置任何 MCP 服务器，无法保存工具选择")
    incoming = _normalize_tool_inputs(payload.inputs)
    unknown = [server for server in incoming if server not in servers]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"未知的服务名: {'、'.join(unknown)}；已配置: {'、'.join(str(s) for s in servers)}",
        )
    inputs = {str(server): incoming.get(str(server), []) for server in servers}
    data["inputs"] = inputs
    try:
        _write_mcp_servers_file(data)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"写入 setting/mcp_servers.json 失败: {exc}")
    _MCP_TOOL_INPUTS_MEMORY = inputs
    return JSONResponse(content={
        "state": "succeed",
        "message": f"已保存 {sum(len(names) for names in inputs.values())} 项工具选择",
        "updated": {"inputs": inputs},
        "inputs": inputs,
        "memory_state": _MCP_TOOL_INPUTS_MEMORY,
    })


def _role_info_payload(role: str) -> dict:
    """单个角色（chat/compaction/title）的完整配置信息，供 GET /chat_config/models 展示。"""
    selection = env_manager.get_role_selection(role)
    models = list_available_models()
    if role == "compaction_model":
        available = [model for model in models if str(model.get("api_type") or "").casefold() == "chat-completions"]
    else:
        available = models
    if role == "chat_model":
        current = get_current_model_selection()
        provider, model = current["provider"], current["model"]
    else:
        provider, model = selection.get("ownership_name"), selection.get("model_name")
    api_type = selection.get("api_type")
    if api_type is None and provider and model:
        chat_config = env_manager.get_model_config(provider, model)
        api_type = env_manager._derive_api_type(chat_config) if chat_config is not None else None
    payload: dict[str, Any] = {
        "role": role,
        "selection": {
            "provider": provider,
            "model": model,
            "api_type": api_type,
            "parameter": selection.get("parameter") or {},
        },
        "effective_parameter": env_manager.get_role_parameter(role),
        "available_models": available,
        "available_count": len(available),
    }
    if role == "compaction_model":
        payload["compaction_status"] = get_context_compaction_model_status()
    return payload


@api_chat_config_router.get("/chat_config/models")
async def list_chat_models(role: str = "chat_model"):
    """列出 models.json 中所有可用的 provider / model 组合，并返回指定角色的完整配置。

    查询参数 `role`（chat_model / compaction_model / title_model，默认 chat_model）：
    `role_info` 返回该角色的当前选择（provider/model/api_type/parameter 分桶）、
    生效参数（effective_parameter，按回退链解析）与可选模型列表
    （compaction_model 只列 chat-completions 协议）。

    每次调用都会基于项目根目录重新执行 `init_path()`，
    让前端在编辑 models.json 后能看到最新内容。
    """
    if role not in _MODEL_ROLES:
        raise HTTPException(status_code=400, detail=f"role 必须是 {'/'.join(_MODEL_ROLES)} 之一，当前为 {role!r}")
    try:
        init_path(PROJECT_ROOT)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"重新加载 models.json 失败：{exc}")
    models = list_available_models()
    return JSONResponse(content={
        "state": "succeed",
        "count": len(models),
        "current": get_current_model_selection(),
        "selection": env_manager.get_model_selection(),
        "models": models,
        "role": role,
        "role_info": _role_info_payload(role),
    })


@api_chat_config_router.post("/chat_config/models/select")
async def select_active_chat_model(payload: ChatModelSelection):
    """选择三种模型（chat/compaction/title）并配置参数，写入 models.json 顶层 `model_selection` 键。

    - 先校验 models.json 目录中存在该组合（compaction_model 必须为 chat-completions 协议）
    - 写回 setting/models.json 的 model_selection.<role>（含 parameter），并同步内存，实时生效
    - 不再写 .env 的 CHAT_OWNERSHIP_NANE / CHAT_MODEL_NAME
    - parameter 为全量替换语义；不传表示仅切换模型、参数保持不变
    - 向后兼容：只传 provider/model（无 role/parameter）时等价于 role=chat_model 且参数不变
    """
    try:
        selection = select_chat_model(
            payload.provider,
            payload.model,
            role=payload.role,
            parameter=payload.parameter,
        )
    except ChatModelConfigurationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse(content={
        "state": "succeed",
        "message": f"已切换 {selection['role']} -> {selection['provider']} / {selection['model']}",
        "current": selection,
        "effective_parameter": env_manager.get_role_parameter(selection["role"]),
        "selection": env_manager.get_model_selection(),
    })
