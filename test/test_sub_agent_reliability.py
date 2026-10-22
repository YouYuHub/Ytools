"""Child-specific budgets, cancellation, progress and cooperative file editing."""
import asyncio
import copy
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from factory.agent_runtime import context_compaction as compaction
from factory.agent_runtime import sub_agent as sub
from factory.agent_runtime.builtin_tools import try_execute_builtin_file_tool
from factory.agent_runtime.sub_agent_files import SubAgentFileAccess
from memory.chat_round_store import ChatRoundStore


def context(**kwargs):
    events = []
    async def emit(payload):
        events.append(payload)
    ctx = sub.SubAgentContext(
        agent_id=sub.new_agent_id(), parent_agent_id="main", parent_tool_call_id=sub.new_agent_id(),
        parent_tool_index=0, agent_index=0, session_id="child-reliability", task="完成文件核对",
        initial_todo=None, tools=[], tool_servers={}, max_rounds=5, timeout_seconds=0,
        reply_max_chars=1000, emit_event=emit, stop_checker=lambda: False,
    )
    return replace(ctx, **kwargs), events


@pytest.fixture
def model(monkeypatch, tmp_path):
    config = {"apiType": "chat-completions", "maxInputTokens": 8192}
    monkeypatch.setattr(sub.SubAgentRunner, "_resolve_request_model_config", lambda self: (config, {}))
    monkeypatch.setattr(sub, "resolve_sub_agent_vision_enabled", lambda *args: False)
    monkeypatch.setattr(sub, "load_sub_agent_limits", lambda: {"max_concurrent": 1})
    settings = compaction.ContextCompactionSettings(.8, .15, 0, 3)
    monkeypatch.setattr(sub, "load_context_compaction_settings", lambda: settings)
    monkeypatch.setattr(compaction, "resolve_context_compaction_model_config", lambda settings: {"maxInputTokens": 100000})
    # A large parent window must not hide the smaller child's need to compact.
    monkeypatch.setattr(compaction, "resolve_model_max_input_tokens", lambda **kwargs: 100000)
    from memory import chat_memory
    monkeypatch.setattr(chat_memory, "HISTORY_ROOT", tmp_path / "history")
    requests = []
    async def stream(request=None, **kwargs):
        requests.append(request)
        yield 'data: {"content":"核对完成。","finish_reason":"stop"}\n\n'
        yield 'data: [DONE]\n\n'
    monkeypatch.setattr(sub.ChatLLM, "chat_completions", stream)
    return config, requests


@pytest.mark.parametrize("api", ["chat-completions", "messages", "responses"])
def test_large_child_tool_trace_compacts_with_own_window(model, monkeypatch, api):
    config, requests = model
    config.update(apiType=api, responses_mode="stateless")
    summaries = []
    async def summarize(text, request, **kwargs):
        summaries.append(text)
        return compaction.SummaryBuildResult("已核对文件，保留发现与后续工作。", False, False,
                                             {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60})
    monkeypatch.setattr(compaction, "summarize_context_text", summarize)
    ctx, events = context()
    runner = sub.SubAgentRunner(ctx)
    runner.messages.extend([
        {"role": "assistant", "content": "", "tool_calls": [{"id": "read", "type": "function",
          "function": {"name": "read_file", "arguments": '{"path":"a.txt"}'}}]},
        {"role": "tool", "tool_call_id": "read", "content": "内容与证据。" * 10000},
    ])
    result = asyncio.run(runner.run())
    assert result.status == "done", result.error
    assert len(summaries) == len(requests) == 1
    assert any("本轮已执行工具摘要" in str(m.get("content")) for m in runner.messages)
    assert any(m.get("content") == ctx.task for m in runner.messages)
    assert not any(m.get("role") == "tool" for m in runner.messages)
    assert result.usage_total["total_tokens"] == 60
    assert any(e.get("activity") == "compacting" for e in events)


def test_short_trace_does_not_compact(model, monkeypatch):
    async def unexpected(*args, **kwargs):
        pytest.fail("A small child context must not invoke the summary model")
    monkeypatch.setattr(compaction, "summarize_context_text", unexpected)
    ctx, _ = context()
    runner = sub.SubAgentRunner(ctx)
    runner.messages.extend([{"role": "assistant", "tool_calls": [{"id": "a", "type": "function",
        "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "a", "content": "short"}])
    assert asyncio.run(runner.run()).status == "done"


def test_queue_does_not_consume_child_timeout(model, monkeypatch):
    first, first_events = context()
    second, second_events = context(timeout_seconds=.1)
    original = sub.SubAgentRunner._run_inner
    async def run_inner(self):
        if self.context is first:
            await asyncio.sleep(.25)
        return await original(self)
    monkeypatch.setattr(sub.SubAgentRunner, "_run_inner", run_inner)
    results = asyncio.run(sub.run_sub_agent_batch([first, second]))
    assert [r["_sub_agent"]["status"] for r in results] == ["done", "done"]
    assert next(e for e in second_events if e["phase"] == "start")["queued"]
    assert len([e for e in first_events + second_events if e["phase"] == "done"]) == 2


def test_parent_cancellation_finishes_running_and_queued_children(model, monkeypatch):
    first, first_events = context()
    second, second_events = context()
    async def run_inner(self):
        await asyncio.sleep(10)
    monkeypatch.setattr(sub.SubAgentRunner, "_run_inner", run_inner)
    async def run():
        batch = asyncio.create_task(sub.run_sub_agent_batch([first, second]))
        while not second_events:
            await asyncio.sleep(0)
        batch.cancel()
        with pytest.raises(asyncio.CancelledError):
            await batch
    asyncio.run(run())
    for events in (first_events, second_events):
        assert [e["status"] for e in events if e["phase"] == "done"] == ["stopped"]
    assert not sub._active_sub_agents


def test_timeout_has_one_timeout_terminal_event(model, monkeypatch):
    ctx, events = context(timeout_seconds=.1)
    async def run_inner(self):
        await asyncio.sleep(10)
    monkeypatch.setattr(sub.SubAgentRunner, "_run_inner", run_inner)
    results = asyncio.run(sub.run_sub_agent_batch([ctx]))
    assert results[0]["_sub_agent"]["status"] == "timeout"
    assert [e["status"] for e in events if e["phase"] == "done"] == ["timeout"]
    assert ctx.timeout_requested


def test_file_conflict_blocks_stale_edit_and_survives_checkpoint(tmp_path):
    path = tmp_path / "shared.txt"
    path.write_text("original", encoding="utf-8")
    snapshots = []
    access = SubAgentFileAccess(save=lambda value: snapshots.append(copy.deepcopy(value)))
    def execute(agent, name, **args):
        return access.execute(agent, name, {"path": str(path), **args}, try_execute_builtin_file_tool)
    assert execute("a", "read_file")["success"]
    assert execute("b", "read_file")["success"]
    assert execute("a", "write_file", content="first", mode="overwrite")["success"]
    access = SubAgentFileAccess(snapshots[-1])
    refused = execute("b", "write_file", content="lost update", mode="overwrite")
    assert refused["code"] == "SUB_AGENT_FILE_CONFLICT"
    assert path.read_text(encoding="utf-8") == "first"
    assert execute("b", "read_file")["success"]
    assert execute("b", "write_file", content="first plus second", mode="overwrite")["success"]
    assert path.read_text(encoding="utf-8") == "first plus second"
    assert execute("c", "write_file", content="blind overwrite", mode="overwrite")["code"] == "SUB_AGENT_FILE_CONFLICT"


def test_parallel_children_surface_conflict_without_losing_first_edit(model, monkeypatch, tmp_path):
    path = tmp_path / "shared.txt"
    path.write_text("original", encoding="utf-8")
    tools = [{"type": "function", "function": {"name": name, "parameters": {}}}
             for name in ("read_file", "write_file")]
    first, first_events = context(tools=tools, tool_servers={"read_file": "__builtin__", "write_file": "__builtin__"})
    second, second_events = context(tools=tools, tool_servers=first.tool_servers, parent_tool_index=1)
    monkeypatch.setattr(sub, "load_sub_agent_limits", lambda: {"max_concurrent": 2})
    async def run():
        read_barrier = asyncio.Barrier(2)
        first_written = asyncio.Event()
        async def emit(event):
            first_events.append(event)
            if event["phase"] == "tool_result" and event.get("tool_name") == "write_file":
                first_written.set()
        first.emit_event = emit
        counts = {}
        async def stream(request=None, **kwargs):
            agent = request.usage_agent
            counts[agent] = counts.get(agent, 0) + 1
            index = counts[agent]
            if index == 1:
                name, args = "read_file", {"path": str(path)}
            elif index == 2:
                await read_barrier.wait()
                if agent == second.agent_id:
                    await first_written.wait()
                name, args = "write_file", {"path": str(path), "content": "first" if agent == first.agent_id else "second", "mode": "overwrite"}
            else:
                yield 'data: {"content":"完成核对。","finish_reason":"stop"}\n\n'
                return
            yield "data: " + json.dumps({"tool_calls": [{"index": 0, "id": agent + str(index), "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)}}], "finish_reason": "tool_calls"}) + "\n\n"
        monkeypatch.setattr(sub.ChatLLM, "chat_completions", stream)
        return await asyncio.wait_for(sub.run_sub_agent_batch([first, second]), 10)
    results = asyncio.run(run())
    assert path.read_text(encoding="utf-8") == "first"
    assert all(r["_sub_agent"]["status"] == "done" for r in results)
    assert "文件协作提示" in results[1]["result"]
    assert any(e.get("file_conflict", {}).get("path") for e in second_events)
    assert first.file_access is second.file_access


def test_progress_persists_but_heartbeat_does_not(model, monkeypatch):
    ctx, events = context()
    runner = sub.SubAgentRunner(ctx)
    async def emit(payload):
        events.append(payload)
        if payload["phase"] == "heartbeat":
            runner.done_emitted = True
    ctx.emit_event = emit
    async def tick(seconds):
        pass
    async def run():
        await runner._progress("tools", "read_file")
        await runner._progress("tools", "read_file")
        await runner._emit("notice", message="retry", retry_kind="empty_final")
        # Substitute only this module's sleep, leaving asyncio scheduling intact.
        monkeypatch.setattr(sub, "asyncio", SimpleNamespace(sleep=tick))
        await runner._heartbeat()
    asyncio.run(run())
    assert [e["activity"] for e in events if e["phase"] == "progress"] == ["tools", "retrying"]
    assert next(e for e in events if e["phase"] == "heartbeat")["activity"] == "retrying"
    store = ChatRoundStore("progress")
    store.record_message({"role": "user", "content": "task"})
    for event in events:
        assert store.record_sub_agent_event(event) == (event["phase"] != "heartbeat")
