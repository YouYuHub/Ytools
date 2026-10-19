"""Budget configuration, authoritative usage snapshots and explicit continuation."""
import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from memory import task_cost_store as store
from memory.chat_memory import normalize_session_id

api_task_cost_router = APIRouter(prefix="/task_costs")


class BudgetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str | None = None
    limits: dict[str, Any] | None


class PricingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    currency: str
    models: dict[str, dict[str, Any]]


class ContinueBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(min_length=1)
    version: int = Field(ge=0)
    limits: dict[str, Any]


@api_task_cost_router.get("/config")
def get_config(session_id: str | None = None):
    from env_manager import list_available_models
    result = store.config(normalize_session_id(session_id) if session_id else None)
    result["models"] = [{"key": json.dumps([m["provider_name"], m["model_name"]], ensure_ascii=False),
                         "label": m["provider_name"] + " / " + m["model_name"]}
                        for m in list_available_models()]
    return result


@api_task_cost_router.post("/config")
def set_config(payload: BudgetConfig):
    try:
        return store.save_config(payload.limits, normalize_session_id(payload.session_id) if payload.session_id else None)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@api_task_cost_router.post("/pricing")
def set_pricing(payload: PricingConfig):
    try:
        store.save_pricing(payload.model_dump())
        return {"ok": True}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@api_task_cost_router.get("/usage")
def get_usage(session_id: str = Query(min_length=1)):
    from memory import permission_store
    session = normalize_session_id(session_id)
    permission_store.pending(session)  # reconcile a worker that exited while waiting
    summary = store.session_summary(session)
    changed = False
    for task in summary["tasks"]:
        if task["state"] not in {"running", "paused"}:
            continue
        control = permission_store.task(task["id"]) if task["kind"] == "chat" else None
        if control and control["state"] != "running":
            store.finish(task["id"], control["state"])
            changed = True
        elif task["kind"] != "chat" and not permission_store.pid_alive(task.get("owner")):
            store.finish(task["id"], "failed")
            changed = True
    return store.session_summary(session) if changed else summary


@api_task_cost_router.post("/{task_id}/continue")
def continue_task(task_id: str, payload: ContinueBudget):
    try:
        from memory import permission_store
        session = normalize_session_id(payload.session_id)
        permission_store.pending(session)
        control = permission_store.task(task_id)
        if control and control["state"] != "running":
            raise ValueError("任务已结束或挂起，请先恢复原任务")
        return store.continue_task(task_id, session, payload.version, payload.limits)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


class StopBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(min_length=1)


@api_task_cost_router.post("/{task_id}/stop")
async def stop_task(task_id: str, payload: StopBudget):
    data = store.status(task_id)
    session = normalize_session_id(payload.session_id)
    if not data or data["session_id"] != session:
        raise HTTPException(404, "任务不属于当前会话")
    if data["state"] not in {"running", "paused"}:
        raise HTTPException(409, "任务已结束或挂起，请刷新后重试")
    if data["kind"] == "chat":
        from routers.chat_router import stop_chat
        await stop_chat(session)
    store.finish(task_id, "cancelled")
    return {"ok": True}
