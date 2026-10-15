import asyncio
import json
import pytest
from test_permission_generation import environment, configure_model, request, Stream
from memory import permission_store as store
from factory import chat_factory as cf
from factory.agent_runtime.permission_runtime import PermissionRuntime, permission_urlopen


def test_full_control_executes_without_confirmation(environment, monkeypatch):
    configure_model(monkeypatch)
    store.set_policy("full_control")
    executions = []
    monkeypatch.setattr(cf, "try_execute_builtin_command_tool", lambda *args: executions.append(args) or {"stdout": "done"})
    async def run():
        stream = Stream()
        await cf._run_chat_generation(request(), stream)
        assert len(executions) == 1
        assert not any(e.get("event") == "tool_approval" for e in stream.events)
    asyncio.run(run())


def test_parent_completed_file_and_child_wait_resume_without_duplicate_execution(environment, monkeypatch):
    from factory.agent_runtime import sub_agent as sub
    parent_config = cf.require_default_chat_config()
    child_config = {**parent_config, "selected_model_id": "child"}
    monkeypatch.setattr(sub.SubAgentRunner, "_resolve_request_model_config", lambda self: (child_config, {}))
    monkeypatch.setattr(sub, "resolve_sub_agent_vision_enabled", lambda *args: False)
    counts = {"parent": 0, "child": 0}
    def tc(index, name, args, identifier):
        return {"index": index, "id": identifier, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
    async def model(request=None, model_config=None, **kwargs):
        kind = "child" if model_config.get("selected_model_id") == "child" else "parent"
        counts[kind] += 1
        if counts[kind] == 1:
            calls = [tc(0, "run_command", {"command": "echo safe"}, "child-command")] if kind == "child" else [tc(0, "read_file", {"full_file_name": "a.txt"}, "file"), tc(1, "sub_agent", {"task": "执行测试"}, "dispatch")]
            yield "data: " + json.dumps({"tool_calls": calls, "finish_reason": "tool_calls"}) + "\n\n"
        else:
            yield 'data: {"content":"完成","finish_reason":"stop"}\n\n'
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(cf.ChatLLM, "chat_completions", model)
    (environment / "a.txt").write_text("one", encoding="utf-8")
    reads, commands = [], []
    real_file = cf.try_execute_builtin_file_tool
    monkeypatch.setattr(cf, "try_execute_builtin_file_tool", lambda *args: reads.append(args) or real_file(*args))
    monkeypatch.setattr(sub, "try_execute_builtin_command_tool", lambda *args: commands.append(args) or {"stdout": "safe"})
    async def run():
        req = request(); req.tool_names = ["read_file", "sub_agent", "run_command"]
        first = Stream()
        running = asyncio.create_task(cf._run_chat_generation(req, first))
        await asyncio.wait_for(first.requested.wait(), 5)
        approval = store.pending(req.session_id)[0]
        assert approval["agent"] != "main"
        assert approval["plan"]["round"] == 1
        assert approval["plan"]["seq"] == 1
        assert approval["plan"]["parent_tool_call_id"] == "dispatch"
        assert len(reads) == 1 and not commands
        first.permission_suspend = True; running.cancel()
        with pytest.raises(asyncio.CancelledError): await running
        with store.database() as db: db.execute("UPDATE tasks SET owner=-1 WHERE id=?", (approval["task"],))
        store.resume(approval["task"])
        await cf._run_chat_generation(req, Stream("approve"), permission_resume_id=approval["task"])
        assert len(reads) == 1 and len(commands) == 1
        assert counts == {"parent": 2, "child": 2}
        assert store.task(approval["task"])["state"] == "completed"
    asyncio.run(run())


def test_search_does_not_read_protected_control_files(environment, monkeypatch):
    from factory.agent_runtime.builtin_tools import execute_search_files
    from factory.agent_runtime import permission_policy
    monkeypatch.setattr(permission_policy, "APP_ROOT", environment)
    (environment / "public.txt").write_text("needle", encoding="utf-8")
    (environment / "permission_secret.txt").write_text("needle secret", encoding="utf-8")
    async def run():
        runtime = PermissionRuntime("s", {}, lambda event: asyncio.sleep(0), lambda: False)
        call = {"id": "one"}
        items, _ = await runtime.authorize([(0, call, "search_files", {"dir_path": ".", "pattern": "needle"})], {"search_files": "__builtin__"})
        runtime.begin_call("main", "one")
        result = execute_search_files(items[0][3])
        assert result["skipped_permission"] >= 1 and result["total_matches"] == 1
        assert not result["scan_complete"]
        runtime.close("completed")
    asyncio.run(run())


def test_network_redirect_cannot_cross_approved_origin(environment, monkeypatch):
    import urllib.request
    captured = []
    class Opener:
        def open(self, request, timeout): return "safe"
    def build(handler):
        captured.append(handler()); return Opener()
    monkeypatch.setattr(urllib.request, "build_opener", build)
    async def run():
        runtime = PermissionRuntime("s", {}, lambda event: asyncio.sleep(0), lambda: False)
        runtime.network_grants.add("https://example.com/a.png")
        assert permission_urlopen("https://example.com/a.png", timeout=1) == "safe"
        with pytest.raises(PermissionError): captured[0].redirect_request(None, None, 302, "", {}, "https://elsewhere.com/b.png")
        with pytest.raises(PermissionError): permission_urlopen("https://example.com/other.png", timeout=1)
        runtime.close("completed")
    asyncio.run(run())


def test_child_wait_does_not_consume_active_timeout(environment, monkeypatch):
    from factory.agent_runtime import sub_agent as sub
    from factory.agent_runtime import permission_runtime as permission_module
    from types import SimpleNamespace
    import time
    from factory.agent_runtime.permission_runtime import run_with_active_timeout, notify
    from test_sub_agent import _make_retry_context
    configure_model(monkeypatch)
    monkeypatch.setattr(sub.SubAgentRunner, "_resolve_request_model_config", lambda self: (cf.require_default_chat_config(), {}))
    order = []
    # Simulate a long human wait without making a 200ms filesystem budget depend
    # on machine load. Keep asyncio's scheduler on the real monotonic clock.
    elapsed_wait = [0.0]
    clock = SimpleNamespace(monotonic=lambda: time.monotonic() + elapsed_wait[0])
    monkeypatch.setattr(permission_module, "time", clock)
    monkeypatch.setattr(sub, "time", clock)
    monkeypatch.setattr(sub, "try_execute_builtin_command_tool", lambda *args: order.append("execution") or {"stdout": "done"})
    async def run():
        async def emit(event):
            if event["phase"] == "requested":
                await asyncio.sleep(0.01)
                elapsed_wait[0] += 10
                a = event["approval"]; store.decide(a["id"], a["session_id"], a["epoch"], "approve"); notify(a["id"])
        runtime = PermissionRuntime("s", {}, emit, lambda: False)
        ctx, _ = _make_retry_context(initial_todo=[])
        ctx.permission_runtime = runtime; ctx.session_id = "s"; ctx.tools = []; ctx.tool_servers = {"run_command": "__builtin__"}
        async def child_event(event): order.append(event["phase"])
        ctx.emit_event = child_event
        result = await run_with_active_timeout(sub.SubAgentRunner(ctx), 2)
        assert result.status == "done"
        assert runtime.paused_time(ctx.agent_id) >= 10
        assert order.index("tool_start") < order.index("execution")
        runtime.close("completed")
    asyncio.run(run())


def test_stopping_waiting_child_retires_both_approval_entries_and_keeps_parent_running(environment, monkeypatch):
    from factory.agent_runtime import sub_agent as sub
    from test_sub_agent import _make_retry_context
    configure_model(monkeypatch)
    monkeypatch.setattr(sub.SubAgentRunner, "_resolve_request_model_config", lambda self: (cf.require_default_chat_config(), {}))
    commands = []
    monkeypatch.setattr(sub, "try_execute_builtin_command_tool", lambda *args: commands.append(args) or {"stdout": "done"})
    async def run():
        requested = asyncio.Event()
        events = []
        async def emit(event):
            events.append(event)
            if event["phase"] == "requested": requested.set()
        runtime = PermissionRuntime("s", {}, emit, lambda: False)
        ctx, _ = _make_retry_context(initial_todo=[])
        ctx.permission_runtime = runtime; ctx.session_id = "s"; ctx.tools = []; ctx.tool_servers = {"run_command": "__builtin__"}
        batch = asyncio.create_task(sub.run_sub_agent_batch([ctx]))
        await asyncio.wait_for(requested.wait(), 5)
        approval = store.pending("s")[0]
        assert sub.request_sub_agent_stop("s", ctx.agent_id)
        result = await asyncio.wait_for(batch, 5)
        assert result[0]["_sub_agent"]["status"] == "stopped"
        assert not commands
        assert store.task(runtime.id)["state"] == "running"
        assert not store.pending("s")
        assert any(e["phase"] == "cancelled" and e["approval"]["id"] == approval["id"] for e in events)
        with pytest.raises(ValueError): store.decide(approval["id"], "s", runtime.epoch, "approve")
        runtime.close("completed")
    asyncio.run(run())


def test_failure_closes_native_tool_slots_without_claiming_unexecuted_tools(environment):
    from chat.protocols import native_store
    async def run():
        runtime = PermissionRuntime("s", {}, lambda event: asyncio.sleep(0), lambda: False)
        ids = ["known", "pending", "uncertain"]
        blocks = [{"type": "tool_use", "id": identifier, "name": "read_file", "input": {"full_file_name": "a.txt"}} for identifier in ids]
        reference = native_store.save_record("s", {}, {"messages": [{"role": "user", "content": "test"}]}, {"content": blocks, "stop_reason": "tool_use"})
        assistant = {"role": "assistant", "provider_state": reference, "tool_calls": [{"id": identifier} for identifier in ids]}
        runtime.save("main", {"assistant": assistant, "messages": [assistant]})
        runtime.finish_call("main", "known", {"ok": True})
        store.claim_execution(runtime.id, runtime.epoch, "main", "uncertain")
        runtime.close("failed")
        status = native_store.load_record("s", reference)["tool_status"]
        assert status["known"]["status"] == "completed" and not status["known"]["is_error"]
        assert status["pending"]["is_error"] and "未执行" in status["pending"]["content"]
        assert status["uncertain"]["is_error"] and "结果未知" in status["uncertain"]["content"]
        assert not store.execution(runtime.id, "main", "pending")
        assert store.load_checkpoint(runtime.id) is None
    asyncio.run(run())
