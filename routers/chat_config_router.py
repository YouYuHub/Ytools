# coding: utf-8
from typing import Any, Optional
from pathlib import Path
import json
import sys
from dataclasses import replace
# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from config import (
    DEFAULT_HISTORY_TRIGGER_RATIO,
    DEFAULT_MAX_OVERSIZED_REJECTIONS,
    DEFAULT_MCP_TOOL_CALL_TIMEOUT_SECONDS,
    DEFAULT_NETWORK_RETRY_MAX_ATTEMPTS,
    DEFAULT_OVERSIZED_REJECT_FACTOR,
    DEFAULT_ONE_TASK_MAX_WORKERS,
    DEFAULT_REASONING_RETURN_MAX_LENGTH,
    DEFAULT_SUB_AGENT_MAX_CONCURRENT,
    DEFAULT_SUMMARY_BUDGET_RATIO,
    DEFAULT_TOOL_RESULT_RETURN_MAX_LENGTH,
    set_current_dir,
    get_current_dir,
    get_persisted_work_dir,
    resolve_work_dir,
    PROJECT_ROOT,
    HistoryCompactionConfig,
    ChatModelSelection,
    ContextReturnConfig,
    McpToolConfig,
    McpToolSelection,
    NetworkRetryConfig,
    SessionWorkDirConfig,
    ToolConcurrencyConfig,
)
import env_manager
from env_manager import (
    ChatModelConfigurationError,
    get_current_model_selection,
    get_env_file_path,
    init_path,
    list_available_models,
    load_var,
    reset_ambient_model_selection,
    select_chat_model,
    set_ambient_model_selection,
    set_env_vars,
)
from factory.agent_runtime.chat_runtime import (
    parse_return_length,
    resolve_model_max_input_tokens,
)
from factory.agent_runtime.context_compaction import (
    get_context_compaction_defaults,
    get_context_compaction_model_status,
    load_context_compaction_settings,
    resolve_config_max_input_tokens,
    resolve_context_compaction_model_config,
    resolve_context_compaction_threshold,
    resolve_summary_total_budget,
)
from memory.chat_memory import (
    normalize_session_id,
    resolve_session_model_selection,
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


def _effective_threshold_payload(settings) -> dict:
    """有效压缩阈值明细，前端据此展示真实触发点。

    阈值 = max(1024, min(聊天模型窗口, 压缩模型窗口) × trigger_ratio)：
    取两者较小窗口保证摘要能同时放进两边上下文；因此当压缩模型窗口
    小于聊天模型窗口时，实际触发点会低于"聊天窗口 × 比例"的直觉预期
    （例如 428k 聊天 + 100k 压缩模型 × 0.7 → 70k，而非 300k）。
    """
    chat_window = resolve_model_max_input_tokens(default=8192)
    try:
        compaction_window = resolve_config_max_input_tokens(
            resolve_context_compaction_model_config(settings),
            default=8192,
        )
    except ChatModelConfigurationError:
        compaction_window = chat_window
    return {
        "value": resolve_context_compaction_threshold(settings),
        "chat_window": chat_window,
        "compaction_window": compaction_window,
        "window": min(chat_window, compaction_window),
        "trigger_ratio": settings.trigger_ratio,
        "formula": "max(1024, min(聊天窗口, 压缩模型窗口) × trigger_ratio)",
    }


def _history_compaction_config_payload(session_id: str | None = None) -> dict:
    settings = load_context_compaction_settings()
    # 模型窗口按会话生效口径解析（会话独立模型选择覆盖全局默认），与
    # token_stats / 生成任务一致；session_id 为空时保持纯全局口径。
    ambient_token = None
    if session_id:
        try:
            effective_selection, _ = resolve_session_model_selection(
                normalize_session_id(session_id)
            )
            ambient_token = set_ambient_model_selection(effective_selection)
        except Exception:
            ambient_token = None
    try:
        payload = {
            "keep_rounds": settings.keep_rounds,
            "trigger_ratio": settings.trigger_ratio,
            "summary_budget_ratio": settings.summary_budget_ratio,
            "summary_total_budget": resolve_summary_total_budget(settings),
            "effective_threshold": _effective_threshold_payload(settings),
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
    finally:
        if ambient_token is not None:
            reset_ambient_model_selection(ambient_token)
    return payload


@api_chat_config_router.post("/change_chat_dir")
async def change_chat_dir(new_dir: str):
    """
    更改全局默认聊天工作目录（.env 的 DEFAULT_CHAT_WORK_DIR）。

    语义说明：该目录只作为「新会话的初始工作目录 + 未覆盖会话的默认目录」，
    不影响已显式设置独立目录的会话；会话级目录请用 POST /chat_config/work_dir。
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
        set_env_vars({"DEFAULT_CHAT_WORK_DIR": current_dir})
        return {
            "state": "succeed",
            "message": f"全局默认聊天工作目录已更改为: {current_dir}",
            "current_dir": current_dir,
            "persisted_env": load_var("DEFAULT_CHAT_WORK_DIR"),
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@api_chat_config_router.get("/chat_config/work_dir")
async def get_chat_work_dir_config(session_id: str | None = None):
    """返回工作目录配置；携带 session_id 时附加该会话的独立目录信息。

    会话字段说明：
    - session_dir：会话在 _meta.work_dir 中记录的覆盖值（未设置时为 null）；
    - session_dir_valid：覆盖目录当前是否仍然存在；
    - effective_dir：会话实际生效的工作目录（覆盖值 → 全局默认 → 进程 cwd）；
    - is_overridden：会话是否设置了独立目录；
    - warning：目录失效回退等警告信息。
    """
    current_dir = get_current_dir()
    persisted_dir = get_persisted_work_dir()
    normalized_current = _normalized_dir_for_compare(current_dir)
    normalized_persisted = _normalized_dir_for_compare(persisted_dir)
    payload: dict[str, Any] = {
        "state": "succeed",
        "current_dir": current_dir,
        "persisted_dir": persisted_dir,
        # 校验后的全局默认目录（新会话的初始目录）：persisted 失效时回退进程 cwd
        "default_dir": str(resolve_work_dir(persisted_dir) or current_dir),
        "is_consistent": bool(
            normalized_current
            and normalized_persisted
            and normalized_current == normalized_persisted
        ),
        "env_name": "DEFAULT_CHAT_WORK_DIR",
        "env_value": load_var("DEFAULT_CHAT_WORK_DIR"),
        "env_file": get_env_file_path(),
        "source": "project .env",
        "read_only": True,
    }
    if session_id:
        from memory.chat_memory import (
            read_session_meta_value,
            resolve_session_work_dir,
            normalize_session_id,
        )
        normalized_session_id = normalize_session_id(session_id)
        session_dir = read_session_meta_value(normalized_session_id, "work_dir")
        session_dir = session_dir if isinstance(session_dir, str) and session_dir.strip() else None
        effective_dir, warning = resolve_session_work_dir(normalized_session_id)
        payload.update({
            "session_id": normalized_session_id,
            "session_dir": session_dir,
            "session_dir_valid": bool(session_dir and resolve_work_dir(session_dir)),
            "effective_dir": effective_dir or current_dir,
            "is_overridden": bool(session_dir and resolve_work_dir(session_dir)),
            "warning": warning,
        })
    return JSONResponse(content=payload)


@api_chat_config_router.post("/chat_config/work_dir")
async def set_chat_session_work_dir(payload: SessionWorkDirConfig):
    """设置/清除会话独立工作目录（写入会话 _meta.work_dir）。

    - work_dir 非空：校验目录存在后写入 _meta；对正在运行的任务不生效
      （运行中任务使用启动时解析的目录），下一轮生成任务开始时生效；
    - work_dir 为空串/None：清除覆盖，恢复跟随全局默认 DEFAULT_CHAT_WORK_DIR；
    - 会话尚未产生任何历史文件时会顺带创建（写入 _meta 需要落盘）。
    """
    from memory.chat_memory import (
        get_chat_memory_manager,
        normalize_session_id,
        resolve_session_work_dir,
    )
    try:
        session_id = normalize_session_id(payload.session_id)
        manager = await get_chat_memory_manager(session_id)
        meta = await manager.update_session_work_dir(payload.work_dir or "")
        effective_dir, warning = resolve_session_work_dir(session_id)
        return JSONResponse(content={
            "state": "succeed",
            "message": (
                f"会话 [{session_id}] 工作目录已设置为: {meta.get('work_dir')}"
                if meta.get("work_dir")
                else f"会话 [{session_id}] 已恢复跟随全局默认工作目录"
            ),
            "session_id": session_id,
            "session_dir": meta.get("work_dir"),
            "effective_dir": effective_dir,
            "warning": warning,
            "updated_at": meta.get("updated_at"),
        })
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@api_chat_config_router.get("/chat_config/history_compaction")
async def get_history_compaction_config(
    session_id: Optional[str] = Query(
        None,
        description="会话 ID；传入后模型窗口按该会话生效模型口径计算",
    ),
):
    """获取跨轮历史与单轮工具上下文的完整压缩策略。"""
    return JSONResponse(content=_history_compaction_config_payload(session_id))


@api_chat_config_router.post("/chat_config/history_compaction")
async def update_history_compaction_config(
    payload: HistoryCompactionConfig,
    session_id: Optional[str] = Query(
        None,
        description="会话 ID；传入后返回的配置按该会话生效模型口径计算",
    ),
):
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
        "config": _history_compaction_config_payload(session_id),
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


# ---------- MCP 工具执行配置 ----------

_MCP_TOOL_ENV_NAME = "MCP_TOOL_CALL_TIMEOUT_SECONDS"


def _parse_tool_call_timeout(value: Any, default: float) -> float:
    """解析工具执行超时秒数；非法或负数回退默认值（0 合法=不限制）。"""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return float(default)
    return parsed if parsed >= 0 else float(default)


def _mcp_tool_config_payload() -> dict:
    return {
        "call_timeout_seconds": _parse_tool_call_timeout(
            load_var(_MCP_TOOL_ENV_NAME, DEFAULT_MCP_TOOL_CALL_TIMEOUT_SECONDS),
            DEFAULT_MCP_TOOL_CALL_TIMEOUT_SECONDS,
        ),
        "defaults": {
            "call_timeout_seconds": DEFAULT_MCP_TOOL_CALL_TIMEOUT_SECONDS,
        },
        "semantics": {
            "0": "不限制超时（工具可能永久阻塞）",
            "positive": "单次工具执行（含连接/初始化/调用）超过 N 秒后中止，"
                        "超时错误会作为工具结果反馈给模型",
        },
        "env_names": {
            "call_timeout_seconds": _MCP_TOOL_ENV_NAME,
        },
        "memory_state": {
            _MCP_TOOL_ENV_NAME: env_manager.env_vars.get(_MCP_TOOL_ENV_NAME),
        },
    }


@api_chat_config_router.get("/chat_config/mcp_tools")
async def get_mcp_tool_config():
    """获取 MCP 工具执行超时配置。"""
    return JSONResponse(content=_mcp_tool_config_payload())


@api_chat_config_router.post("/chat_config/mcp_tools")
async def update_mcp_tool_config(payload: McpToolConfig):
    """实时更新 MCP 工具执行超时并持久化。

    - 写回项目 .env（set_env_vars 同时同步内存 env_vars），下次调用立即生效
    - 0 表示不限制（保持旧行为，工具卡死时只能靠手动停止取消任务）
    """
    timeout_seconds = _parse_tool_call_timeout(
        payload.call_timeout_seconds, DEFAULT_MCP_TOOL_CALL_TIMEOUT_SECONDS
    )
    if payload.call_timeout_seconds < 0:
        raise HTTPException(
            status_code=400,
            detail="call_timeout_seconds 不能为负数；0 表示不限制，正数为超时秒数",
        )
    try:
        updated = set_env_vars({
            _MCP_TOOL_ENV_NAME: timeout_seconds,
        })
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse(content={
        "state": "succeed",
        "updated": updated,
        "config": _mcp_tool_config_payload(),
        # 与其他配置接口一致：返回内存 env 中的原始值，便于调用方核对落盘结果
        "memory_state": {
            _MCP_TOOL_ENV_NAME: env_manager.env_vars.get(_MCP_TOOL_ENV_NAME),
        },
    })


# ---------- 网络请求失败重试配置 ----------

_NETWORK_RETRY_ENV_NAME = "NETWORK_RETRY_MAX_ATTEMPTS"


def _parse_network_retry_max_attempts(value: Any, default: int) -> int:
    """解析重试次数配置；非整数回退默认值（0 与负数合法=不限制）。"""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return int(default)
    return parsed


def _network_retry_config_payload() -> dict:
    return {
        "max_attempts": _parse_network_retry_max_attempts(
            load_var(_NETWORK_RETRY_ENV_NAME, DEFAULT_NETWORK_RETRY_MAX_ATTEMPTS),
            DEFAULT_NETWORK_RETRY_MAX_ATTEMPTS,
        ),
        "defaults": {
            "max_attempts": DEFAULT_NETWORK_RETRY_MAX_ATTEMPTS,
        },
        "semantics": {
            "0_or_negative": "不限制重试次数（连续失败会一直重试，直到手动停止任务）",
            "positive": "同一轮请求连续失败达到 N 次后终止当前任务",
        },
        "env_names": {"max_attempts": _NETWORK_RETRY_ENV_NAME},
        "memory_state": env_manager.env_vars.get(_NETWORK_RETRY_ENV_NAME),
    }


@api_chat_config_router.get("/chat_config/network_retry")
async def get_network_retry_config():
    """获取模型网络请求失败重试次数配置。"""
    return JSONResponse(content=_network_retry_config_payload())


@api_chat_config_router.post("/chat_config/network_retry")
async def update_network_retry_config(payload: NetworkRetryConfig):
    """实时更新网络请求失败重试次数并持久化。

    - 写回项目 .env（set_env_vars 同时同步内存 env_vars），下一次模型请求立即生效
    - 0 或负数表示不限制（保持旧行为，一直重试直到手动停止）
    """
    max_attempts = _parse_network_retry_max_attempts(
        payload.max_attempts, DEFAULT_NETWORK_RETRY_MAX_ATTEMPTS
    )
    try:
        updated = set_env_vars({
            _NETWORK_RETRY_ENV_NAME: max_attempts,
        })
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse(content={
        "state": "succeed",
        "updated": updated,
        "config": _network_retry_config_payload(),
    })


# ---------- 工具并发执行配置 ----------

_TOOL_CONCURRENCY_ENV_NAMES = {
    "mcp_tool_workers": "ONE_TASK_MAX_WORKERS",
    "sub_agent_max_concurrent": "SUB_AGENT_MAX_CONCURRENT",
}


def _parse_concurrency_value(value: Any, default: int) -> int:
    """解析并发数配置；非法值回退默认值（并发数必须 >= 1）。"""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return int(default)
    return parsed if parsed >= 1 else int(default)


def _tool_concurrency_config_payload() -> dict:
    return {
        "mcp_tool_workers": _parse_concurrency_value(
            load_var(_TOOL_CONCURRENCY_ENV_NAMES["mcp_tool_workers"], DEFAULT_ONE_TASK_MAX_WORKERS),
            DEFAULT_ONE_TASK_MAX_WORKERS,
        ),
        "sub_agent_max_concurrent": _parse_concurrency_value(
            load_var(
                _TOOL_CONCURRENCY_ENV_NAMES["sub_agent_max_concurrent"],
                DEFAULT_SUB_AGENT_MAX_CONCURRENT,
            ),
            DEFAULT_SUB_AGENT_MAX_CONCURRENT,
        ),
        "defaults": {
            "mcp_tool_workers": DEFAULT_ONE_TASK_MAX_WORKERS,
            "sub_agent_max_concurrent": DEFAULT_SUB_AGENT_MAX_CONCURRENT,
        },
        "semantics": {
            "mcp_tool_workers": "同一轮多个 MCP 工具调用并发执行的线程池大小；"
                                "实际并发数=该值与工具数取较小者",
            "sub_agent_max_concurrent": "同一父轮并发子智能体数量上限；超出的子任务排队执行",
        },
        "env_names": dict(_TOOL_CONCURRENCY_ENV_NAMES),
        "memory_state": {
            name: env_manager.env_vars.get(env_name)
            for name, env_name in _TOOL_CONCURRENCY_ENV_NAMES.items()
        },
    }


@api_chat_config_router.get("/chat_config/tool_concurrency")
async def get_tool_concurrency_config():
    """获取工具并发执行配置（MCP 工具线程池 + 子智能体并发上限）。"""
    return JSONResponse(content=_tool_concurrency_config_payload())


@api_chat_config_router.post("/chat_config/tool_concurrency")
async def update_tool_concurrency_config(payload: ToolConcurrencyConfig):
    """实时更新工具并发执行配置并持久化。

    - 写回项目 .env（set_env_vars 同时同步内存 env_vars），下一次工具执行立即生效
    - 两个并发数都必须 >= 1（Pydantic ge=1 与后端读取时的下限裁剪共同保证）
    """
    update_env = {
        _TOOL_CONCURRENCY_ENV_NAMES["mcp_tool_workers"]: payload.mcp_tool_workers,
        _TOOL_CONCURRENCY_ENV_NAMES["sub_agent_max_concurrent"]: payload.sub_agent_max_concurrent,
    }
    try:
        updated = set_env_vars(update_env)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse(content={
        "state": "succeed",
        "updated": updated,
        "config": _tool_concurrency_config_payload(),
        "memory_state": _tool_concurrency_config_payload()["memory_state"],
    })


_MODEL_ROLES = ("chat_model", "compaction_model", "title_model", "sub_agent_model")


# ---------- 工具选择（全局默认 = setting/mcp_servers.json 的 inputs 键；会话级 = _meta.tool_selection） ----------

# 内存中的全局工具选择快照（服务名 -> 工具名数组）。
# 由 GET/POST 接口与配置热重载线程（mcp_servers.json 变更回调）共同保持与磁盘一致
_MCP_TOOL_INPUTS_MEMORY: dict[str, list[str]] | None = None


def _mcp_servers_config_path() -> Path:
    from config import get_mcp_servers_config_path
    return get_mcp_servers_config_path()


def _read_mcp_servers_file() -> dict:
    from config import read_mcp_servers_config
    return read_mcp_servers_config()


def _normalize_tool_inputs(raw: Any) -> dict[str, list[str]]:
    from config import normalize_tool_inputs as _normalize
    return _normalize(raw)


def sync_tool_selection_memory(data: dict) -> dict[str, list[str]]:
    """把磁盘 mcp_servers.json 的 inputs 规整后同步进内存快照（配置热重载回调入口）。

    新增/删除服务时同步补 [] / 移除失效服务，返回同步后的快照。
    """
    global _MCP_TOOL_INPUTS_MEMORY
    servers = configured_server_names(data)
    inputs = _normalize_tool_inputs(data.get("inputs"))
    for server in servers:
        inputs.setdefault(str(server), [])
    _MCP_TOOL_INPUTS_MEMORY = inputs
    return inputs


# 兼容旧内部引用：规整后的 servers 读取统一走 config.configured_server_names
def _configured_server_names(data: dict) -> dict:
    from config import configured_server_names as _configured
    return _configured(data)


def _resolve_session_selection(session_id: str) -> tuple[str, dict[str, list[str]] | None, dict[str, list[str]] | None, bool, str | None]:
    """解析会话工具选择（供 GET/POST 接口回显）。

    Returns:
        (规整后的 session_id, 会话覆盖选择或 None, 生效选择或 None, 是否覆盖, 警告或 None)
    """
    from memory.chat_memory import (
        normalize_session_id as _normalize_session_id,
        read_session_meta_value,
        resolve_session_tool_selection,
    )
    normalized = _normalize_session_id(session_id)
    override = read_session_meta_value(normalized, "tool_selection")
    override_normalized = _normalize_tool_inputs(override) if override is not None else None
    effective, warning = resolve_session_tool_selection(normalized)
    return normalized, override_normalized, effective, bool(override_normalized), warning


@api_chat_config_router.get("/chat_config/tool_selection")
async def get_tool_selection(session_id: str | None = None):
    """读取已保存的工具选择（前端页面刷新时调用以恢复勾选）。

    每次都以磁盘 mcp_servers.json 的 `inputs` 键为准并同步内存；
    携带 `session_id` 时附加该会话的独立选择信息：
    - session_selection：会话在 _meta.tool_selection 中记录的覆盖值（未设置时为 null）；
    - effective_selection：会话实际生效的选择（会话覆盖 → 全局 inputs）；
    - is_overridden：会话是否设置了独立工具选择；
    - warning：全局配置读取失败等警告信息。
    """
    global _MCP_TOOL_INPUTS_MEMORY
    data = _read_mcp_servers_file()
    servers = _configured_server_names(data)
    inputs = _normalize_tool_inputs(data.get("inputs"))
    for server in servers:
        inputs.setdefault(str(server), [])
    _MCP_TOOL_INPUTS_MEMORY = inputs
    payload: dict[str, Any] = {
        "state": "succeed",
        "inputs": inputs,
        "servers": [str(server) for server in servers],
        "config_path": str(_mcp_servers_config_path()),
        "memory_state": _MCP_TOOL_INPUTS_MEMORY,
    }
    if session_id:
        normalized_id, session_selection, effective_selection, is_overridden, warning = (
            _resolve_session_selection(session_id)
        )
        if normalized_id:
            payload.update({
                "session_id": normalized_id,
                "session_selection": session_selection,
                "effective_selection": effective_selection if effective_selection is not None else inputs,
                "is_overridden": is_overridden,
                "warning": warning,
            })
    return JSONResponse(content=payload)


@api_chat_config_router.post("/chat_config/tool_selection")
async def update_tool_selection(payload: McpToolSelection):
    """保存前端选择的工具。

    - 不携带 session_id（全局默认，新建会话前的选择）：全量替换写回
      setting/mcp_servers.json 的 inputs 键，其余内容保持不变；键必须为
      `servers` 中已配置的服务名，未知服务报 400（伪服务 __builtin__ 除外，
      其下存放前端在内置工具分组勾选的 todo_write / ask_user 等）；
    - 携带 session_id（会话内选择）：写入该会话 _meta.tool_selection，
      不修改全局 inputs；空 inputs 表示清除会话覆盖、恢复跟随全局默认；
      规整后为空的非法条目会被丢弃。
    """
    global _MCP_TOOL_INPUTS_MEMORY
    from factory.agent_runtime.builtin_tools import BUILTIN_TOOL_SERVER_KEY
    incoming = _normalize_tool_inputs(payload.inputs)
    if payload.session_id and str(payload.session_id).strip():
        from memory.chat_memory import get_chat_memory_manager, normalize_session_id
        session_id = normalize_session_id(payload.session_id)
        manager = await get_chat_memory_manager(session_id)
        meta = await manager.update_session_tool_selection(incoming or None)
        session_selection = meta.get("tool_selection")
        effective = _normalize_tool_inputs(session_selection) if session_selection else None
        return JSONResponse(content={
            "state": "succeed",
            "message": (
                f"会话 [{session_id}] 工具选择已保存（{sum(len(names) for names in effective.values()) if effective else 0} 项）"
                if session_selection
                else f"会话 [{session_id}] 已恢复跟随全局默认工具选择"
            ),
            "session_id": session_id,
            "session_selection": session_selection,
            "effective_selection": effective or {},
            "is_overridden": bool(session_selection),
            "updated_at": meta.get("updated_at"),
        })
    data = _read_mcp_servers_file()
    servers = _configured_server_names(data)
    # 仅勾选内置工具时也允许保存（此时可以没有配置任何 MCP 服务器）
    if not servers and not (set(incoming) - {BUILTIN_TOOL_SERVER_KEY}):
        raise HTTPException(status_code=400, detail="setting/mcp_servers.json 未配置任何 MCP 服务器，无法保存工具选择")
    unknown = [
        server for server in incoming
        if server not in servers and server != BUILTIN_TOOL_SERVER_KEY
    ]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"未知的服务名: {'、'.join(unknown)}；已配置: {'、'.join(str(s) for s in servers)}",
        )
    # 内置工具伪服务仅在传入时保留（未提及 = 未勾选 = 不写入，保持 inputs 干净）
    inputs = {str(server): incoming.get(str(server), []) for server in servers}
    if BUILTIN_TOOL_SERVER_KEY in incoming:
        inputs = {BUILTIN_TOOL_SERVER_KEY: incoming[BUILTIN_TOOL_SERVER_KEY], **inputs}
    try:
        from config import write_mcp_servers_inputs
        write_mcp_servers_inputs(inputs)
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


def _role_info_payload(role: str, is_overridden: bool = False) -> dict:
    """单个角色（chat/compaction/title/sub_agent）的完整配置信息，供 GET /chat_config/models 展示。

    会话级展示时由调用方先设置 ambient 模型选择覆盖（set_ambient_model_selection），
    本函数读取的 selection/effective_parameter 即为该会话的生效结果。
    """
    selection = env_manager.get_role_selection(role)
    models = list_available_models()
    if role in ("compaction_model", "sub_agent_model"):
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
        "is_overridden": is_overridden,
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
async def list_chat_models(role: str = "chat_model", session_id: str | None = None):
    """列出 models.json 中所有可用的 provider / model 组合，并返回指定角色的完整配置。

    查询参数 `role`（chat_model / compaction_model / title_model / sub_agent_model，默认 chat_model）：
    `role_info` 返回该角色的当前选择（provider/model/api_type/parameter 分桶）、
    生效参数（effective_parameter，按回退链解析）与可选模型列表
    （compaction_model / sub_agent_model 只列 chat-completions 协议）。

    查询参数 `session_id`（可选）：携带时按「会话覆盖 → 全局默认」解析该会话的
    生效模型选择，`role_info` 展示会话生效结果并附加 `is_overridden`；响应额外
    返回 `session_selection`（会话覆盖值，未设置为 null）、`effective_selection`
    （全角色合并结果）与 `warning`（失效覆盖回退提示）。

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
    payload: dict[str, Any] = {
        "state": "succeed",
        "count": len(models),
        "current": get_current_model_selection(),
        "selection": env_manager.get_global_model_selection(),
        "models": models,
        "role": role,
        "role_info": _role_info_payload(role),
    }
    if session_id and str(session_id).strip():
        from memory.chat_memory import (
            normalize_session_id,
            read_session_meta_value,
            resolve_session_model_selection,
        )
        from env_manager import normalize_model_selection, reset_ambient_model_selection, set_ambient_model_selection
        normalized = normalize_session_id(session_id)
        effective_selection, warnings = resolve_session_model_selection(normalized)
        override = read_session_meta_value(normalized, "model_selection")
        override_valid = {
            role_key: entry for role_key, entry in normalize_model_selection(override).items()
            if entry.get("ownership_name") and entry.get("model_name")
        } if override is not None else {}
        ambient_token = set_ambient_model_selection(effective_selection)
        try:
            payload["role_info"] = _role_info_payload(role, is_overridden=bool(override_valid.get(role)))
        finally:
            reset_ambient_model_selection(ambient_token)
        payload.update({
            "session_id": normalized,
            "session_selection": override_valid or None,
            "effective_selection": effective_selection,
            "warning": warnings or None,
        })
    return JSONResponse(content=payload)


@api_chat_config_router.post("/chat_config/models/select")
async def select_active_chat_model(payload: ChatModelSelection):
    """选择三种模型（chat/compaction/title）并配置参数。

    - 不携带 session_id（全局默认）：写入 models.json 顶层 `model_selection.<role>`
      （含 parameter），并同步内存，实时生效；
    - 携带 session_id（会话级）：写入该会话 `_meta.model_selection.<role>`，
      不修改全局配置；`clear=true` 时清除该角色的会话覆盖、恢复跟随全局默认；
    - 先校验 models.json 目录中存在该组合（compaction_model 必须为 chat-completions 协议）
    - 不再写 .env 的 CHAT_OWNERSHIP_NANE / CHAT_MODEL_NAME
    - parameter 为全量替换语义；不传表示仅切换模型、参数保持不变
      （会话级未传时：已有会话覆盖沿用其参数，首次覆盖沿用全局该角色的参数）
    - 向后兼容：只传 provider/model（无 role/parameter）时等价于 role=chat_model 且参数不变
    """
    if payload.session_id and str(payload.session_id).strip():
        from memory.chat_memory import (
            get_chat_memory_manager,
            normalize_session_id,
            resolve_session_model_selection,
        )
        from env_manager import build_model_selection_entry, reset_ambient_model_selection, set_ambient_model_selection
        session_id = normalize_session_id(payload.session_id)
        if payload.role not in _MODEL_ROLES:
            raise HTTPException(status_code=400, detail=f"role 必须是 {'/'.join(_MODEL_ROLES)} 之一，当前为 {payload.role!r}")
        try:
            manager = await get_chat_memory_manager(session_id)
            if payload.clear:
                meta = await manager.update_session_model_selection(payload.role, None)
                message = f"会话 [{session_id}] {payload.role} 已恢复跟随全局默认模型"
            else:
                existing = (await manager.get_session_model_selection()) or {}
                inherit = (existing.get(payload.role) or {}).get("parameter")
                if inherit is None:
                    inherit = (env_manager.get_global_model_selection().get(payload.role) or {}).get("parameter")
                entry = build_model_selection_entry(
                    payload.provider,
                    payload.model,
                    payload.role,
                    parameter=payload.parameter,
                    inherit_parameter=inherit,
                )
                meta = await manager.update_session_model_selection(payload.role, entry)
                message = f"会话 [{session_id}] {payload.role} 已切换为 {entry['ownership_name']} / {entry['model_name']}"
        except ChatModelConfigurationError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        effective_selection, warnings = resolve_session_model_selection(session_id)
        ambient_token = set_ambient_model_selection(effective_selection)
        try:
            role_info = _role_info_payload(payload.role, is_overridden=not payload.clear)
        finally:
            reset_ambient_model_selection(ambient_token)
        return JSONResponse(content={
            "state": "succeed",
            "message": message,
            "session_id": session_id,
            "session_selection": meta.get("model_selection"),
            "effective_selection": effective_selection,
            "warning": warnings or None,
            "role": payload.role,
            "role_info": role_info,
        })
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
        "selection": env_manager.get_global_model_selection(),
    })
