import asyncio
from pathlib import Path
from urllib.parse import urlsplit
import urllib.request
import urllib.error

from fastapi import APIRouter, HTTPException, Request, Query
from fastapi.responses import Response, RedirectResponse
from pydantic import BaseModel
from memory import permission_store as store
from memory.chat_memory import normalize_session_id, resolve_session_work_dir
from factory.agent_runtime.permission_policy import plan_tool, inside, protected, resolve_path
from routers.permissions_auth import ticket, verify_ticket

api_permission_router = APIRouter()


class PolicyPayload(BaseModel):
    mode: str | None = None
    session_id: str | None = None


class DecisionPayload(BaseModel):
    session_id: str
    epoch: str
    decision: str


class PreviewPayload(BaseModel):
    session_id: str
    source: str


class TaskPayload(BaseModel):
    session_id: str


def public(approval):
    return {"id": approval["id"], "task_id": approval["task"], "epoch": approval["epoch"],
            "session_id": approval["session"], "agent_id": approval["agent"], "tool_call_id": approval["call_id"],
            "state": approval["state"], "task_state": approval["task_state"], **approval["plan"]}


@api_permission_router.get("/chat_config/permissions")
def get_permissions(session_id: str | None = None):
    session = normalize_session_id(session_id) if session_id else None
    root, _ = resolve_session_work_dir(session or "default")
    return {**store.policy(session), "root": root or str(Path.cwd())}


@api_permission_router.post("/chat_config/permissions")
def set_permissions(request: Request, payload: PolicyPayload):
    try:
        return store.set_policy(payload.mode, normalize_session_id(payload.session_id) if payload.session_id else None)
    except ValueError as error:
        raise HTTPException(400, str(error))


@api_permission_router.get("/tool_approvals/pending")
def pending(request: Request, session_id: str, approval_ids: list[str] | None = Query(None)):
    session = normalize_session_id(session_id)
    approvals = [public(item) for item in store.pending(session)]
    tasks = []
    for item in store.resumable(session):
        with store.database() as db:
            unknown = bool(db.execute("SELECT 1 FROM executions WHERE task=? AND state='executing'", (item["id"],)).fetchone())
        tasks.append({"id": item["id"], "session_id": session, "state": item["state"], "root": item["data"]["root"], "unknown_execution": unknown})
    states = []
    # Query only approvals visible/tracked by this browser, including cancelled
    # ones that no longer belong in the actionable list.
    for identifier in list(dict.fromkeys(approval_ids or []))[:200]:
        item = store.approval(identifier)
        if item and item["session"] == session:
            execution = store.execution(item["task"], item["agent"], item["call_id"])
            states.append({**public(item), "execution_state": execution["state"] if execution else None})
    return {"approvals": approvals, "tasks": tasks, "states": states}


@api_permission_router.post("/tool_approvals/tasks/{task_id}/resume")
async def resume_task(request: Request, task_id: str, payload: TaskPayload):
    saved = store.task(task_id)
    if not saved or saved["session"] != normalize_session_id(payload.session_id) or saved["data"].get("preview"):
        raise HTTPException(404, "任务不存在")
    claimed = False
    try:
        store.pending(saved["session"])
        current = store.resume(task_id)
        claimed = True
        from factory.chat_factory import resume_permission_task
        await resume_permission_task(current["id"])
        return {"state": "running"}
    except (ValueError, RuntimeError) as error:
        if claimed and store.task(task_id)["state"] == "running":
            store.release_resume(task_id, current["epoch"])
        raise HTTPException(409, str(error))


@api_permission_router.post("/tool_approvals/tasks/{task_id}/cancel")
async def cancel_task(request: Request, task_id: str, payload: TaskPayload):
    task = store.task(task_id)
    if not task or task["session"] != normalize_session_id(payload.session_id):
        raise HTTPException(404, "任务不存在")
    if task["state"] == "cancelled":
        return {"state": "cancelled"}
    if task["state"] not in {"running", "suspended"}:
        raise HTTPException(409, "任务已经结束")
    # Close known, unexecuted provider tool slots before retiring this task.
    if not task["data"].get("preview"):
        from factory.agent_runtime.permission_runtime import close_pending_native
        close_pending_native(task_id, task["session"], "用户放弃任务")
        from factory.chat_factory import stop_chat_task
        await stop_chat_task(task["session"])
    store.set_task_state(task_id, "cancelled")
    return {"state": "cancelled"}


async def resume_approval(old):
    current = store.resume(old["task"])
    snapshot = store.load_checkpoint(old["task"])
    if snapshot.get("preview"):
        plan = old["plan"]
        if any(str(Path(path).resolve()) != path for path in plan["paths"]):
            store.release_resume(current["id"], current["epoch"])
            raise ValueError("媒体真实路径已变化，请重新发起预览")
        fresh = store.request_approval(current["id"], "preview", old["call_id"], current["epoch"], plan)
        return fresh
    from factory.chat_factory import resume_permission_task
    try:
        await resume_permission_task(current["id"])
    except Exception:
        store.release_resume(current["id"], current["epoch"])
        raise
    for _ in range(300):
        for candidate in store.pending(old["session"]):
            if candidate["task"] == old["task"] and candidate["agent"] == old["agent"] and candidate["call_id"] == old["call_id"] and candidate["epoch"] == current["epoch"]:
                if candidate["plan"]["digest"] != old["plan"]["digest"]:
                    raise ValueError("调用参数或服务配置已变化，请查看新的审批卡片后重新确认")
                return candidate
        if store.task(current["id"])["state"] not in {"running", "suspended"}:
            raise ValueError("恢复任务未能继续，请查看会话中的错误信息并重新发送")
        await asyncio.sleep(0.05)
    # The worker never re-requested (dead, stuck or too slow): release the
    # dispatch back to suspended so the task never strands in running with
    # nobody watching it (owner is this backend process, so liveness checks
    # would never reclaim it). A live worker winds down on suspension.
    store.release_resume(current["id"], current["epoch"])
    raise ValueError("任务恢复尚未就绪，请刷新审批列表；不会自动执行")


@api_permission_router.post("/tool_approvals/{approval_id}/decision")
async def decide(request: Request, approval_id: str, payload: DecisionPayload):
    if payload.decision not in {"approve", "deny"}:
        raise HTTPException(400, "请选择 approve 或 deny")
    old = store.approval(approval_id)
    if not old or old["session"] != normalize_session_id(payload.session_id):
        raise HTTPException(404, "审批不存在")
    if old["epoch"] != payload.epoch:
        raise HTTPException(409, "审批代次已变化，请刷新")
    resumed = False
    try:
        # Only still-unresolved requests can resume a stopped service, never old
        # invalidated IDs from another browser after a new epoch has been created.
        store.pending(old["session"])
        old = store.approval(approval_id)
        if old["task_state"] in {"cancelled", "completed", "failed"}:
            label = {"cancelled": "已取消", "completed": "已结束", "failed": "异常结束"}[old["task_state"]]
            raise ValueError(f"任务{label}，此审批不能继续执行；请重新发送任务")
        if old["task_state"] == "suspended":
            if old["state"] not in {"pending", "approved", "denied"}:
                raise ValueError("该审批已失效；如任务仍挂起，请在权限面板恢复原任务或放弃")
            old = await resume_approval(old)
            resumed = True
        result = store.decide(old["id"], old["session"], old["epoch"], payload.decision)
        from factory.session_worker import peek_worker_proxy
        from factory.agent_runtime.permission_runtime import notify
        proxy = peek_worker_proxy(old["session"])
        if proxy is not None and proxy.is_generation_running():
            proxy._send({"type": "approval_decision", "approval_id": old["id"]})
        else:
            notify(old["id"])
        value = {"approval": public(result), "resumed": resumed}
        if result["agent"] == "preview":
            store.finish_approval(old["id"], "completed")
            store.set_task_state(old["task"], "completed")
            if payload.decision == "approve":
                value["ticket"] = ticket(old["session"], old["plan"]["args"]["references"][0])
        return value
    except (ValueError, RuntimeError) as error:
        raise HTTPException(409, str(error))


@api_permission_router.post("/tool_approvals/preview")
def preview(request: Request, payload: PreviewPayload):
    session = normalize_session_id(payload.session_id)
    root, _ = resolve_session_work_dir(session)
    context = {"session": session, "mode": store.policy(session)["mode"], "root": root or str(Path.cwd())}
    plan = plan_tool("read_media", {"references": [payload.source]}, context)
    source = plan["args"]["references"][0]
    if not plan["needs_approval"]:
        return {"ticket": ticket(session, source), "source": source}
    for item in store.pending(session):
        if item["agent"] == "preview" and item["plan"]["digest"] == plan["digest"]:
            return {"approval": public(item), "source": source}
    current = store.create_task(session, {"mode": context["mode"], "root": context["root"], "request": {}, "preview": True})
    store.checkpoint(current["id"], "main", {"preview": True})
    approval = store.request_approval(current["id"], "preview", "preview", current["epoch"], plan)
    return {"approval": public(approval), "source": source}


@api_permission_router.get("/permissions/media")
async def media(request: Request, session_id: str, source: str, grant: str = ""):
    session = normalize_session_id(session_id)
    root, _ = resolve_session_work_dir(session)
    network = urlsplit(source).scheme in {"http", "https"}
    actual = source if network else resolve_path(source, root or str(Path.cwd()))
    full_control = store.policy(session)["mode"] == "full_control"
    allowed = full_control or (not network and inside(actual, root or str(Path.cwd())) and not protected(actual))
    if not allowed and not verify_ticket(grant, session, actual):
        raise HTTPException(403, "外部媒体需要用户确认")
    if network and full_control:
        return RedirectResponse(actual, status_code=302)
    if not network:
        from routers.file_router import _file_response_with_range
        from memory.file_memory import media_mime_type
        path = Path(actual)
        if not path.is_file():
            raise HTTPException(404, "媒体不存在")
        return _file_response_with_range(request.headers, path, media_mime_type(path.name))
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    def download():
        with urllib.request.build_opener(NoRedirect).open(actual, timeout=30) as response:
            data = response.read(32 * 1024 * 1024 + 1)
            if len(data) > 32 * 1024 * 1024:
                raise ValueError("媒体超过32MB预览上限")
            return data, response.headers.get("Content-Type", "application/octet-stream")
    try:
        data, kind = await asyncio.to_thread(download)
        return Response(data, media_type=kind)
    except (OSError, ValueError, urllib.error.URLError) as error:
        raise HTTPException(400, "外部媒体读取失败：" + str(error))
