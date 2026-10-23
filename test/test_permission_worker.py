"""Real Windows spawn + worker Pipe, with a local deterministic model/tool."""
import asyncio
import json
import multiprocessing
import time
from pathlib import Path
import pytest
from test_permission_generation import environment, request
from memory import permission_store as store


def worker(root, cmd, events, real_command=False, commands=None):
    from factory import chat_factory as cf
    from factory.session_worker import _worker_loop
    from memory import chat_memory, file_memory
    import env_manager
    root = Path(root)
    store.ROOT = root / "control"
    chat_memory.HISTORY_ROOT = root / "history"
    chat_memory.LOCK_ROOT = root / "locks"
    file_memory.HISTORY_ROOT = root / "uploads"
    chat_memory.resolve_session_work_dir = lambda session: (str(root), None)
    env_manager.init_path = lambda *args: None
    cf.require_default_chat_config = lambda: {"selected_provider_name": "test", "selected_model_name": "test", "selected_model_id": "test", "vision": False, "maxInputTokens": 1000000}
    cf.resolve_model_max_input_tokens = lambda default=8192: 1000000
    cf.load_all_tools = lambda: asyncio.sleep(0)
    cf.tool_registry.ALL_TOOLS = []; cf.tool_registry.TOOL_MCP_SERVERS = {}
    cf.compact_session_history_if_needed = lambda *args, **kwargs: asyncio.sleep(0)
    original_execute = cf.try_execute_builtin_command_tool
    def execute(*args):
        with (root / "executions.txt").open("a", encoding="utf-8") as file: file.write("executed\n")
        return original_execute(*args) if real_command else {"stdout": "safe"}
    cf.try_execute_builtin_command_tool = execute
    async def model(request=None, **kwargs):
        with (root / "model_calls.txt").open("a", encoding="utf-8") as file: file.write("model\n")
        if any(message.role == "tool" for message in request.messages):
            yield 'data: {"content":"完成","finish_reason":"stop"}\n\n'
        else:
            args = commands or [{"command": 'Get-Date -Format "yyyy年MM月dd日 HH:mm:ss dddd"', "shell": "powershell", "yield_time_ms": 30000} if real_command else {"command": "echo safe"}]
            calls = [{"index": i, "id": f"call-{i + 1}", "type": "function", "function": {"name": "run_command", "arguments": json.dumps(arguments)}} for i, arguments in enumerate(args)]
            yield "data: " + json.dumps({"tool_calls": calls, "finish_reason": "tool_calls"}) + "\n\n"
        yield "data: [DONE]\n\n"
    cf.ChatLLM.chat_completions = model
    asyncio.run(_worker_loop("permission-e2e", cmd, events))


def test_spawn_shutdown_restart_and_decision_pipe(environment):
    ctx = multiprocessing.get_context("spawn")
    active = []
    def start(resume_id=None):
        child_cmd, cmd = ctx.Pipe(duplex=False)
        events, child_events = ctx.Pipe(duplex=False)
        process = ctx.Process(target=worker, args=(str(environment), child_cmd, child_events))
        process.start(); child_cmd.close(); child_events.close(); active.append(process)
        cmd.send({"type": "generate", "request": request().model_dump(exclude_unset=True), "permission_resume_id": resume_id})
        return process, cmd, events
    def wait_event(connection, predicate):
        end = time.monotonic() + 12
        while time.monotonic() < end:
            if not connection.poll(0.1): continue
            event = connection.recv()
            if event.get("type") == "sse":
                for line in event.get("chunk", event.get("data", "")).splitlines():
                    if line.startswith("data:") and line[5:].strip() != "[DONE]":
                        item = json.loads(line[5:])
                        if predicate(item): return item
            if predicate(event): return event
        raise AssertionError("worker event timeout")
    try:
        process, cmd, events = start()
        approval = wait_event(events, lambda e: e.get("event") == "tool_approval" and e.get("phase") == "requested")["approval"]
        assert not (environment / "executions.txt").exists()
        cmd.send({"type": "shutdown"}); process.join(5)
        assert not process.is_alive()
        assert store.task(approval["task_id"])["state"] == "suspended"
        store.resume(approval["task_id"])
        process, cmd, events = start(approval["task_id"])
        fresh = wait_event(events, lambda e: e.get("event") == "tool_approval" and e.get("phase") == "requested")["approval"]
        assert fresh["epoch"] != approval["epoch"]
        with pytest.raises(ValueError): store.decide(approval["id"], "permission-e2e", approval["epoch"], "approve")
        store.decide(fresh["id"], "permission-e2e", fresh["epoch"], "approve")
        cmd.send({"type": "approval_decision", "approval_id": fresh["id"]})
        wait_event(events, lambda e: e.get("type") == "task_done")
        assert (environment / "executions.txt").read_text().splitlines() == ["executed"]
        assert len((environment / "model_calls.txt").read_text().splitlines()) == 2
        assert store.task(fresh["task_id"])["state"] == "completed"
        cmd.send({"type": "shutdown"}); process.join(5)
    finally:
        for process in active:
            if process.is_alive(): process.terminate()
            process.join(3)


@pytest.mark.parametrize("approved_index", [0, 1])
def test_spawn_worker_mixed_decisions_only_executes_allowed_command(environment, monkeypatch, approved_index):
    import httpx
    from fastapi import FastAPI
    from factory import session_worker
    from routers.tool_approval_router import api_permission_router
    ctx = multiprocessing.get_context("spawn")
    child_cmd, cmd = ctx.Pipe(duplex=False)
    events, child_events = ctx.Pipe(duplex=False)
    commands = [{"command": f"Add-Content -LiteralPath 'marker-{i}.txt' -Value executed; Write-Output result-{i}", "shell": "powershell", "timeout_seconds": 10} for i in range(2)]
    process = ctx.Process(target=worker, args=(str(environment), child_cmd, child_events, True, commands))
    process.start(); child_cmd.close(); child_events.close()
    monkeypatch.setattr(session_worker, "peek_worker_proxy", lambda _: None)

    async def run():
        cmd.send({"type": "generate", "request": request().model_dump(exclude_unset=True)})
        deadline = time.monotonic() + 10
        approvals = []
        while time.monotonic() < deadline:
            approvals = store.pending("permission-e2e")
            if len(approvals) == 2: break
            await asyncio.sleep(.05)
        assert len(approvals) == 2
        by_call = {a["call_id"]: a for a in approvals}
        app = FastAPI(); app.include_router(api_permission_router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
            # Decide the second slot before the worker has registered its waiter.
            for index in (1, 0):
                a = by_call[f"call-{index + 1}"]
                response = await api.post(f'/tool_approvals/{a["id"]}/decision', json={
                    "session_id": "permission-e2e", "epoch": a["epoch"],
                    "decision": "approve" if index == approved_index else "deny",
                })
                assert response.status_code == 200, response.text
        deadline = time.monotonic() + 15
        returned, started, done = {}, set(), False
        while time.monotonic() < deadline:
            if not events.poll(.05):
                await asyncio.sleep(.01); continue
            event = events.recv()
            if isinstance(event, dict) and event.get("type") == "sse":
                for line in event.get("chunk", event.get("data", "")).splitlines():
                    if not line.startswith("data:") or line[5:].strip() == "[DONE]": continue
                    item = json.loads(line[5:])
                    if item.get("tool_start"): started.add(item["tool_start"]["tool_call_id"])
                    if item.get("tool_return"):
                        value = item["tool_return"]; returned[value["tool_call_id"]] = value
            if isinstance(event, dict) and event.get("type") == "task_done":
                done = True; break
        assert done and len(returned) == 2
        allowed_id, denied_id = f"call-{approved_index + 1}", f"call-{2 - approved_index}"
        assert started == {allowed_id}
        denial = json.loads(returned[denied_id]["result"])
        assert denial["code"] == "PERMISSION_DENIED" and denial["executed"] is False
        assert (environment / f"marker-{approved_index}.txt").read_text(encoding="utf-8-sig").splitlines() == ["executed"]
        assert not (environment / f"marker-{1 - approved_index}.txt").exists()
        assert (environment / "executions.txt").read_text().splitlines() == ["executed"]
        assert store.task(approvals[0]["task"])["state"] == "completed"
        cmd.send({"type": "shutdown"})
        await asyncio.to_thread(process.join, 5)
    try:
        asyncio.run(run())
    finally:
        if process.is_alive(): process.terminate()
        process.join(3)


def test_http_resume_and_confirm_with_real_windows_worker_and_lost_ipc(environment, monkeypatch):
    from fastapi import FastAPI
    import httpx
    from factory import chat_factory as cf, session_worker
    from routers.tool_approval_router import api_permission_router
    ctx = multiprocessing.get_context("spawn")
    active, current = [], {}

    def start(resume_id=None):
        child_cmd, cmd = ctx.Pipe(duplex=False)
        events, child_events = ctx.Pipe(duplex=False)
        process = ctx.Process(target=worker, args=(str(environment), child_cmd, child_events, True))
        process.start(); child_cmd.close(); child_events.close(); active.append(process)
        current.update(process=process, cmd=cmd, events=events)
        cmd.send({"type": "generate", "request": request().model_dump(exclude_unset=True), "permission_resume_id": resume_id})

    async def run():
        start()
        for _ in range(200):
            approvals = store.pending("permission-e2e")
            if approvals:
                break
            await asyncio.sleep(.05)
        assert approvals
        old = approvals[0]
        current["cmd"].send({"type": "shutdown"})
        await asyncio.to_thread(current["process"].join, 8)
        assert not current["process"].is_alive()
        assert store.task(old["task"])["state"] == "suspended"
        async def resume(task_id):
            start(task_id)
        monkeypatch.setattr(cf, "resume_permission_task", resume)
        # The route cannot reach a live local waiter. Durable state must wake
        # the actual spawned worker without relying on its IPC notification.
        monkeypatch.setattr(session_worker, "peek_worker_proxy", lambda session: None)
        app = FastAPI(); app.include_router(api_permission_router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
            response = await api.post(f'/tool_approvals/{old["id"]}/decision', json={"session_id": "permission-e2e", "epoch": old["epoch"], "decision": "approve"})
            assert response.status_code == 200, response.text
        end = time.monotonic() + 15
        returned = None
        while time.monotonic() < end:
            if not current["events"].poll(.05):
                await asyncio.sleep(.01)
                continue
            event = current["events"].recv()
            if isinstance(event, dict) and event.get("type") == "sse":
                for line in event.get("chunk", event.get("data", "")).splitlines():
                    if not line.startswith("data:") or line[5:].strip() == "[DONE]":
                        continue
                    item = json.loads(line[5:])
                    if item.get("tool_return"):
                        returned = item["tool_return"]
            if isinstance(event, dict) and event.get("type") == "task_done":
                break
        assert returned and returned["tool_call_id"] == "call-1"
        result = returned["result"]
        if isinstance(result, str): result = json.loads(result)
        assert result['status'] == 'completed' and result['job_id']
        assert result["exit_code"] == 0 and "年" in result["output"]
        assert "年" not in result['message']
        assert (environment / "executions.txt").read_text().splitlines() == ["executed"]
        assert store.task(old["task"])["state"] == "completed"
        current["cmd"].send({"type": "shutdown"})
        await asyncio.to_thread(current["process"].join, 5)
    try:
        asyncio.run(run())
    finally:
        for process in active:
            if process.is_alive(): process.terminate()
            process.join(3)
