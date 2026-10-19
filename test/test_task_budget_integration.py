import asyncio
import json

from test_permission_generation import environment, request, Stream
from factory import chat_factory as cf
from factory.agent_runtime.task_costs import CURRENT, BudgetTracker, network_attempt, tracked_call
from memory import task_cost_store as costs
from config import ChatLLMRequest


def test_main_pauses_then_continues_without_repeating_model_or_tool(environment, monkeypatch):
    calls, executions = [], []
    async def model(req, *args):
        calls.append(req)
        if len(calls) == 1:
            frame = {"tool_calls": [{"index": 0, "id": "call-once", "type": "function", "function": {"name": "run_command", "arguments": json.dumps({"command": "echo safe"})}}], "finish_reason": "tool_calls"}
        else:
            frame = {"content": "完成", "finish_reason": "stop"}
        yield "data: " + json.dumps(frame) + "\n\n"
        yield 'data: {"id":"same-provider-id","usage":{"prompt_tokens":10,"completion_tokens":5}}\n\n'
        yield 'data: [DONE]\n\n'
    monkeypatch.setattr(cf.ChatLLM, "_unmetered_chat_completions", model)
    monkeypatch.setattr(cf.ChatLLM, "_resolve_chat_config", lambda config=None: cf.require_default_chat_config())
    monkeypatch.setattr(cf, "try_execute_builtin_command_tool", lambda *args: executions.append(args) or {"stdout": "done"})
    costs.save_config({"model_calls": 1})
    async def run():
        stream = Stream("approve")
        running = asyncio.create_task(cf._run_chat_generation(request(), stream))
        for _ in range(100):
            await asyncio.sleep(.02)
            task = costs.latest("permission-e2e")
            if task and task["state"] == "paused": break
        else: raise AssertionError("budget did not pause")
        assert len(calls) == 1 and executions == []
        costs.continue_task(task["id"], "permission-e2e", task["version"], {"model_calls": 3})
        await asyncio.wait_for(running, 5)
        assert len(calls) == 2 and len(executions) == 1
        final = costs.status(task["id"])
        assert final["state"] == "completed"
        assert final["usage"]["total_tokens"] == 30
        assert final["model_calls"] == 2 and final["tool_calls"] == 1
        assert any(e.get("event") == "task_usage" and e["task"]["state"] == "paused" for e in stream.events)
    asyncio.run(run())


def test_retry_and_separate_wait_for_tasks_preserve_meter_context(environment):
    costs.save_config({"model_calls": 1})
    costs.create_task("retry", "s")
    async def run():
        tracker = BudgetTracker("retry", "s")
        token = CURRENT.set(tracker)
        async def upstream():
            await network_attempt()
            yield 'data: {"error":"retry","retrying":true}\n\n'
            await network_attempt()
            yield 'data: {"content":"ok","usage":{"prompt_tokens":10,"completion_tokens":5}}\n\n'
        generator = tracked_call(upstream, ChatLLMRequest(messages=[{"role": "user", "content": "hi"}]), True, None, {})
        try:
            await asyncio.wait_for(generator.__anext__(), 2)
            next_chunk = asyncio.create_task(generator.__anext__())
            await asyncio.sleep(.04)
            task = costs.status("retry")
            assert task["state"] == "paused" and task["model_calls"] == 1
            costs.continue_task("retry", "s", task["version"], {})
            assert '"content":"ok"' in await asyncio.wait_for(next_chunk, 2)
            await generator.aclose()
            assert costs.status("retry")["model_calls"] == 2
            assert costs.status("retry")["usage"]["total_tokens"] >= 15
        finally:
            CURRENT.reset(token)
    asyncio.run(run())
