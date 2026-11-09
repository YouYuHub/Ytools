"""暂存消息（引导/队列）持久化接口：一期文本+引用。

前端输入框上方的暂存区此前只在内存，刷新页面即丢失。本模块把“未发送”
条目落盘到会话侧车（<session>_chat.jsonl.outbox），打开会话时拉取恢复；
“已提交待消费”（injected）以后端实时注入队列为准（后端重启即消失，不恢复）。
"""
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from typing import Any

from memory.chat_memory import get_chat_memory_manager, normalize_session_id

api_outbox_router = APIRouter(prefix="/outbox")


class OutboxSubmit(BaseModel):
    model_config = ConfigDict(extra="ignore")

    session_id: str = Field(min_length=1)
    id: str = Field(min_length=1, max_length=128)
    kind: str = Field(pattern="^(steer|queue)$")
    text: str = Field(default="", max_length=50000)
    media: list = Field(default_factory=list)
    quotes: list = Field(default_factory=list)
    docs: list = Field(default_factory=list)


class OutboxRemove(BaseModel):
    model_config = ConfigDict(extra="ignore")

    session_id: str = Field(min_length=1)
    ids: list[str] = Field(default_factory=list)


@api_outbox_router.get("")
async def get_outbox(session_id: str = Query(min_length=1)):
    """读取暂存：未发送条目（侧车）+ 待消费注入（后端实时队列快照）。"""
    session = normalize_session_id(session_id)
    manager = await get_chat_memory_manager(session)
    entries = await manager.get_session_outbox()
    injected: list[dict[str, Any]] | None = []
    try:
        from factory.chat_factory import is_chat_stream_running, peek_injected_messages
        from factory.session_worker import peek_worker_proxy
        proxy = peek_worker_proxy(session)
        running = bool(proxy is not None and proxy.is_generation_running()) or is_chat_stream_running(session)
        snapshot = await peek_injected_messages(session)
        if snapshot is None:
            # 查询失败（未知）：返回 null，前端跳过 injected 对账（不得当空处理）
            injected = None
        else:
            for item in snapshot:
                injected.append({
                    "id": item.get("message_id") or "",
                    "text": item.get("text") or "",
                    "quotes": item.get("quotes") or [],
                    "media": item.get("media") or [],
                })
    except Exception:
        # 未知：injected=null 跳过对账；running 置 true 兜底（宁可延迟转换，不误转）
        injected = None
        running = True
    return {"entries": entries, "injected": injected, "running": running}


@api_outbox_router.post("/submit")
async def submit_outbox(payload: OutboxSubmit):
    """新增或更新一条暂存（按 id 幂等）。"""
    session = normalize_session_id(payload.session_id)
    manager = await get_chat_memory_manager(session)
    try:
        entry = await manager.upsert_session_outbox({
            "id": payload.id,
            "kind": payload.kind,
            "text": payload.text,
            "media": payload.media,
            "quotes": payload.quotes,
            "docs": payload.docs,
        })
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"entry": entry}


@api_outbox_router.post("/remove")
async def remove_outbox(payload: OutboxRemove):
    """按 id 删除暂存条目（发送成功/撤回/消费后调用）。"""
    session = normalize_session_id(payload.session_id)
    manager = await get_chat_memory_manager(session)
    removed = await manager.remove_session_outbox(payload.ids)
    return {"removed": removed}
