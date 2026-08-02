# coding: utf-8
from typing import Optional
from pathlib import Path
import sys
# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field


from config import (
    set_current_dir,
    get_current_dir,
    get_persisted_work_dir,
    PROJECT_ROOT,
    HistoryCompactionConfig,
    ChatModelSelection,
)
from env_manager import (
    ChatModelConfigurationError,
    env_vars,
    get_current_model_selection,
    get_env_file_path,
    init_path,
    list_available_models,
    load_var,
    select_chat_model,
    set_env_vars,
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

    []
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
    """获取历史压缩配置。

    同步返回 ``load_var`` 解析后的值与 ``env_vars`` 内存快照，
    便于前端核对磁盘与内存是否一致。
    """
    keep_rounds = int(load_var("HISTORY_COMPACT_KEEP_ROUNDS", 4) or 4)
    trigger_ratio = float(load_var("HISTORY_COMPACT_TRIGGER_RATIO", 0.8) or 0.8)
    return JSONResponse(content={
        "keep_rounds": keep_rounds,
        "trigger_ratio": trigger_ratio,
        "env_names": {
            "keep_rounds": "HISTORY_COMPACT_KEEP_ROUNDS",
            "trigger_ratio": "HISTORY_COMPACT_TRIGGER_RATIO",
        },
        "memory_state": {
            "HISTORY_COMPACT_KEEP_ROUNDS": env_vars.get("HISTORY_COMPACT_KEEP_ROUNDS"),
            "HISTORY_COMPACT_TRIGGER_RATIO": env_vars.get("HISTORY_COMPACT_TRIGGER_RATIO"),
        },
    })


@api_chat_config_router.post("/chat_config/history_compaction")
async def update_history_compaction_config(payload: HistoryCompactionConfig):
    """更新历史压缩配置并同步写回 .env。

    - 先调用 ``init_path(PROJECT_ROOT)`` 重新读盘，确保上游任何缓存被刷掉。
    - 调用 ``set_env_vars`` 写盘并同步 ``env_vars`` 全局字典。
    - 返回前再校验内存与磁盘一致，不一致则强制覆盖。
    - 响应中增加 ``memory_state`` 字段，便于前端确认内存已更新。
    """
    try:
        updated = set_env_vars({
            "HISTORY_COMPACT_KEEP_ROUNDS": int(payload.keep_rounds),
            "HISTORY_COMPACT_TRIGGER_RATIO": float(payload.trigger_ratio),
        })
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # 防御性兜底：如果 env_vars 在 init_path -> set_env_vars 之间被再次重置，
    # 这里强制把新值写回内存，保证后续聊天请求立刻使用新压缩配置。
    expected = {
        "HISTORY_COMPACT_KEEP_ROUNDS": str(int(payload.keep_rounds)),
        "HISTORY_COMPACT_TRIGGER_RATIO": str(float(payload.trigger_ratio)),
    }
    for key, value in expected.items():
        if env_vars.get(key) != value:
            env_vars[key] = value

    return JSONResponse(content={
        "state": "succeed",
        "updated": updated,
        "config": {
            "keep_rounds": int(load_var("HISTORY_COMPACT_KEEP_ROUNDS", 4) or 4),
            "trigger_ratio": float(load_var("HISTORY_COMPACT_TRIGGER_RATIO", 0.8) or 0.8),
        },
        "memory_state": {
            "HISTORY_COMPACT_KEEP_ROUNDS": env_vars.get("HISTORY_COMPACT_KEEP_ROUNDS"),
            "HISTORY_COMPACT_TRIGGER_RATIO": env_vars.get("HISTORY_COMPACT_TRIGGER_RATIO"),
        },
    })


@api_chat_config_router.get("/chat_config/models")
async def list_chat_models():
    """列出 models.json 中所有可用的 provider / model 组合，并返回当前选择。

    每次调用都会基于项目根目录重新执行 `init_path()`，
    让前端在编辑 models.json 后能看到最新内容。
    """
    try:
        init_path(PROJECT_ROOT)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"重新加载 models.json 失败：{exc}")

    models = list_available_models()
    return JSONResponse(content={
        "state": "succeed",
        "count": len(models),
        "current": get_current_model_selection(),
        "models": models,
    })


@api_chat_config_router.post("/chat_config/models/select")
async def select_active_chat_model(payload: ChatModelSelection):
    """更新 .env 中当前使用的 provider 与 model 字段。

    - 先校验 models.json 中存在该组合
    - 同步写回 .env 的 CHAT_OWNERSHIP_NANE / CHAT_MODEL_NAME
    - 内存中已加载的 settings_vars / env_vars 也会立即生效，
      下次聊天请求会使用新选择
    """
    try:
        selection = select_chat_model(payload.provider, payload.model)
    except ChatModelConfigurationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # 防御性兜底：select_chat_model 内部已经显式同步过 env_vars，
    # 但如果该路由被并发调用、或上游 init_path 又触发了字典重置，
    # 这里再做一次最终验证，确保返回前内存中的选择就是新值。
    memory_provider = env_vars.get("CHAT_OWNERSHIP_NANE")
    memory_model = env_vars.get("CHAT_MODEL_NAME")
    if memory_provider != selection["provider"] or memory_model != selection["model"]:
        env_vars["CHAT_OWNERSHIP_NANE"] = selection["provider"]
        env_vars["CHAT_MODEL_NAME"] = selection["model"]

    return JSONResponse(content={
        "state": "succeed",
        "message": f"已切换到 {selection['provider']} / {selection['model']}",
        "current": selection,
        "memory_state": {
            "CHAT_OWNERSHIP_NANE": env_vars.get("CHAT_OWNERSHIP_NANE"),
            "CHAT_MODEL_NAME": env_vars.get("CHAT_MODEL_NAME"),
        },
    })
