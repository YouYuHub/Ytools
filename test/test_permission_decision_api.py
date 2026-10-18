"""Exercise the HTTP decision route with the actual generation/resume lifecycle."""
import asyncio
import json
import httpx
import pytest
from fastapi import FastAPI

from test_permission_generation import environment, configure_model, request, Stream
from memory import permission_store as store
from factory import chat_factory as cf
from factory.agent_runtime.permission_runtime import PermissionRuntime
from routers.tool_approval_router import api_permission_router


def client():
    app = FastAPI()
    app.include_router(api_permission_router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def payload(a, decision="approve"):
    return {"session_id": a["session"], "epoch": a["epoch"], "decision": decision}


@pytest.mark.parametrize("restart", [False, True])
def test_http_confirmation_returns_real_powershell_result_once(environment, monkeypatch, restart):
    command = 'Get-Date -Format "yyyy年MM月dd日 HH:mm:ss dddd"'
    calls = configure_model(monkeypatch, {"command": command, "shell": "powershell"})
    monkeypatch.setattr(cf, "_generation_use_worker", lambda: False)
    monkeypatch.setattr(cf, "_SESSION_STREAMS", {})

    async def run():
        req, first = request(), Stream()
        generation = asyncio.create_task(cf._run_chat_generation(req, first))
        await asyncio.wait_for(first.requested.wait(), 8)
        a = store.pending(req.session_id)[0]
        if restart:
            first.permission_suspend = True
            generation.cancel()
            with pytest.raises(asyncio.CancelledError):
                await generation
            with store.database() as db:
                db.execute("UPDATE tasks SET owner=-1 WHERE id=?", (a["task"],))
        async with client() as api:
            response = await api.post(f'/tool_approvals/{a["id"]}/decision', json=payload(a))
            assert response.status_code == 200, response.text
            assert response.json()["resumed"] is restart
            if restart:
                generation = cf._get_session_stream(req.session_id).task
            await asyncio.wait_for(generation, 15)
        rows = [json.loads(line) for line in (environment / "history" / "permission-e2e_chat.jsonl").read_text(encoding="utf-8").splitlines()]
        rounds = [r for r in rows if r.get("event") == "chat_round"]
        assert len(rounds) == 1
        results = [e for e in rounds[0]["events"] if e.get("role") == "tool"]
        assert len(results) == 1
        result = json.loads(results[0]["result"])
        assert result["exit_code"] == 0 and "年" in result["stdout"]
        assert len(calls) == 2 and store.task(a["task"])["state"] == "completed"
    asyncio.run(run())


def test_missing_checkpoint_is_rejected_without_claiming_task(environment):
    task = store.create_task("s", {"root": str(environment)})
    with store.database() as db:
        db.execute("UPDATE tasks SET state='suspended',owner=-1 WHERE id=?", (task["id"],))
    with pytest.raises(ValueError, match="检查点不存在"):
        store.resume(task["id"])
    assert store.task(task["id"])["state"] == "suspended"


def test_committed_decision_wakes_waiter_without_ipc(environment):
    async def run():
        ready = asyncio.Event()
        async def emit(event):
            if event["phase"] == "requested":
                ready.set()
        runtime = PermissionRuntime("s", {}, emit, lambda: False)
        task = asyncio.create_task(runtime.authorize([(0, {"id": "call"}, "run_command", {"command": "echo safe"})], {"run_command": "__builtin__"}))
        await asyncio.wait_for(ready.wait(), 5)
        a = store.pending("s")[0]
        # No notify() here: simulate an IPC message lost while reconnecting.
        store.decide(a["id"], "s", a["epoch"], "approve")
        allowed, denied = await asyncio.wait_for(task, 3)
        assert len(allowed) == 1 and not denied
        runtime.close("completed")
    asyncio.run(run())


def test_cancelled_approval_reports_terminal_state_and_never_executes(environment):
    task = store.create_task("s", {"root": str(environment)})
    a = store.request_approval(task["id"], "main", "call", task["epoch"], {"args": {"command": "echo safe"}})
    store.set_task_state(task["id"], "cancelled")
    async def run():
        async with client() as api:
            response = await api.post(f'/tool_approvals/{a["id"]}/decision', json=payload(a))
            assert response.status_code == 409 and "任务已取消" in response.json()["detail"]
            response = await api.get("/tool_approvals/pending", params={"session_id": "s", "approval_ids": a["id"]})
            assert response.json()["approvals"] == []
            status = response.json()["states"][0]
            assert status["state"] == "cancelled" and status["execution_state"] is None
            other = await api.get("/tool_approvals/pending", params={"session_id": "other", "approval_ids": a["id"]})
            assert other.json()["states"] == []
    asyncio.run(run())
    assert store.execution(task["id"], "main", "call") is None


def test_failed_resume_dispatch_can_be_retried_in_same_backend(environment, monkeypatch):
    task = store.create_task("s", {"root": str(environment), "request": {}})
    store.checkpoint(task["id"], "main", {"messages": []})
    with store.database() as db:
        db.execute("UPDATE tasks SET state='suspended',owner=-1 WHERE id=?", (task["id"],))
    attempts = []
    async def resume(task_id):
        attempts.append(task_id)
        if len(attempts) == 1:
            raise ValueError("模拟启动失败")
    monkeypatch.setattr(cf, "resume_permission_task", resume)
    async def run():
        async with client() as api:
            url = f'/tool_approvals/tasks/{task["id"]}/resume'
            first = await api.post(url, json={"session_id": "s"})
            assert first.status_code == 409
            assert store.task(task["id"])["state"] == "suspended"
            second = await api.post(url, json={"session_id": "s"})
            assert second.status_code == 200, second.text
            assert len(attempts) == 2
    asyncio.run(run())


@pytest.mark.parametrize("approved_index", [0, 1])
@pytest.mark.parametrize("decision_order", [(0, 1), (1, 0)])
@pytest.mark.parametrize("restart", [False, True])
def test_two_commands_mixed_decisions_execute_only_allowed_call(environment, monkeypatch, approved_index, decision_order, restart):
    """Use the real HTTP route and PowerShell; a denied marker must not exist."""
    monkeypatch.setattr(cf, "_generation_use_worker", lambda: False)
    monkeypatch.setattr(cf, "_SESSION_STREAMS", {})
    model_requests = []
    async def model(request=None, **kwargs):
        model_requests.append(request.model_dump())
        if len(model_requests) == 1:
            calls = [{"index": i, "id": f"call-{i}", "type": "function", "function": {
                "name": "run_command", "arguments": json.dumps({
                    "shell": "powershell", "timeout_seconds": 10,
                    "command": f"Add-Content -LiteralPath 'marker-{i}.txt' -Value 'executed'; Write-Output 'result-{i}'",
                })}} for i in range(2)]
            yield "data: " + json.dumps({"tool_calls": calls, "finish_reason": "tool_calls"}) + "\n\n"
        else:
            yield 'data: {"content":"已根据允许和拒绝的结果完成处理","finish_reason":"stop"}\n\n'
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(cf.ChatLLM, "chat_completions", model)

    async def run():
        req, stream = request(), Stream()
        generation = asyncio.create_task(cf._run_chat_generation(req, stream))
        try:
            await asyncio.wait_for(stream.requested.wait(), 8)
            pending = {a["call_id"]: a for a in store.pending(req.session_id)}
            assert len(pending) == 2
            task_id = pending["call-0"]["task"]
            if restart:
                stream.permission_suspend = True; generation.cancel()
                with pytest.raises(asyncio.CancelledError): await generation
                with store.database() as db:
                    db.execute("UPDATE tasks SET owner=-1 WHERE id=?", (task_id,))
            async with client() as api:
                for position, index in enumerate(decision_order):
                    a = pending[f"call-{index}"]
                    response = await api.post(f'/tool_approvals/{a["id"]}/decision',
                        json=payload(a, "approve" if index == approved_index else "deny"))
                    assert response.status_code == 200, response.text
                    if restart and position == 0:
                        generation = cf._get_session_stream(req.session_id).task
                        pending = {a["call_id"]: a for a in store.pending(req.session_id)}
                    if position == 0:
                        assert not list(environment.glob("marker-*.txt")), "Batch still waits for its other approval"
                await asyncio.wait_for(generation, 15)
            assert (environment / f"marker-{approved_index}.txt").read_text(encoding="utf-8-sig").splitlines() == ["executed"]
            denied_index = 1 - approved_index
            assert not (environment / f"marker-{denied_index}.txt").exists()
            assert len(model_requests) == 2
            tools = {m["tool_call_id"]: json.loads(m["content"]) for m in model_requests[1]["messages"] if m.get("role") == "tool"}
            assert set(tools) == {"call-0", "call-1"}
            assert tools[f"call-{approved_index}"]["exit_code"] == 0
            denied = tools[f"call-{denied_index}"]
            assert denied["code"] == "PERMISSION_DENIED" and denied["executed"] is False
            rows = [json.loads(line) for line in (environment / "history" / "permission-e2e_chat.jsonl").read_text(encoding="utf-8").splitlines()]
            rounds = [r for r in rows if r.get("event") == "chat_round"]
            results = [e for e in rounds[0]["events"] if e.get("role") == "tool"]
            assert len(results) == 2 and {e["tool_call_id"] for e in results} == {"call-0", "call-1"}
            rejected = next(e for e in results if e["tool_call_id"] == f"call-{denied_index}")
            assert rejected["tool_is_error"] is True
            assert store.task(task_id)["state"] == "completed"
            assert not store.pending(req.session_id)
        finally:
            if not generation.done():
                stream.permission_suspend = True; generation.cancel()
                try: await generation
                except asyncio.CancelledError: pass
    asyncio.run(run())


def suspend(task_id):
    with store.database() as db:
        db.execute("UPDATE tasks SET state='suspended',owner=-1 WHERE id=?", (task_id,))


def test_resume_poll_timeout_releases_task_to_suspended(environment, monkeypatch):
    from routers import tool_approval_router as router
    task = store.create_task("s", {"root": str(environment), "request": {}})
    store.checkpoint(task["id"], "main", {"messages": []})
    old = store.request_approval(task["id"], "main", "call", task["epoch"],
                                 {"args": {}, "digest": "d", "paths": [], "sources": [], "tool": "run_command"})
    suspend(task["id"])
    monkeypatch.setattr(cf, "resume_permission_task", lambda task_id: asyncio.sleep(0))
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda delay: real_sleep(0))
    async def run():
        with pytest.raises(ValueError, match="尚未就绪"):
            await router.resume_approval(store.approval(old["id"]))
    asyncio.run(run())
    current = store.task(task["id"])
    assert current["state"] == "suspended" and current["owner"] == -1
    assert store.approval(old["id"])["state"] == "invalidated"


def test_resume_poll_worker_death_reports_without_recovery(environment, monkeypatch):
    from routers import tool_approval_router as router
    task = store.create_task("s", {"root": str(environment), "request": {}})
    store.checkpoint(task["id"], "main", {"messages": []})
    old = store.request_approval(task["id"], "main", "call", task["epoch"],
                                 {"args": {}, "digest": "d", "paths": [], "sources": [], "tool": "run_command"})
    suspend(task["id"])
    async def finish(task_id):
        store.set_task_state(task_id, "failed")
    monkeypatch.setattr(cf, "resume_permission_task", finish)
    async def run():
        with pytest.raises(ValueError, match="未能继续"):
            await router.resume_approval(store.approval(old["id"]))
    asyncio.run(run())
    assert store.task(task["id"])["state"] == "failed"


def test_decide_on_invalidated_approval_guides_to_resume(environment):
    task = store.create_task("s", {"root": str(environment), "request": {}})
    store.checkpoint(task["id"], "main", {"messages": []})
    old = store.request_approval(task["id"], "main", "call", task["epoch"], {"args": {}})
    suspend(task["id"])
    current = store.resume(task["id"])
    store.release_resume(task["id"], current["epoch"])
    async def run():
        async with client() as api:
            response = await api.post(f'/tool_approvals/{old["id"]}/decision', json=payload(old))
            assert response.status_code == 409
            assert "恢复原任务" in response.json()["detail"]
    asyncio.run(run())
