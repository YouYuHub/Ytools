import asyncio
import json
from unittest.mock import patch

import pytest
from config import ChatLLMRequest
from memory import permission_store as store, chat_memory, file_memory
from factory import chat_factory as cf
from factory.agent_runtime.permission_runtime import notify


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "ROOT", tmp_path / "control")
    monkeypatch.setattr(chat_memory, "HISTORY_ROOT", tmp_path / "history")
    monkeypatch.setattr(chat_memory, "LOCK_ROOT", tmp_path / "locks")
    monkeypatch.setattr(file_memory, "HISTORY_ROOT", tmp_path / "uploads")
    (tmp_path / "history").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cf, "require_default_chat_config", lambda: {"selected_provider_name": "test", "selected_model_name": "test", "selected_model_id": "test", "vision": False, "maxInputTokens": 1000000})
    monkeypatch.setattr(cf, "resolve_model_max_input_tokens", lambda default=8192: 1000000)
    monkeypatch.setattr(cf, "load_all_tools", lambda: asyncio.sleep(0))
    monkeypatch.setattr(cf.tool_registry, "ALL_TOOLS", [])
    monkeypatch.setattr(cf.tool_registry, "TOOL_MCP_SERVERS", {})
    monkeypatch.setattr(cf, "compact_session_history_if_needed", lambda *a, **kw: asyncio.sleep(0))
    return tmp_path


class Stream:
    def __init__(self, decide=None):
        self.question_text = ""; self.user_stop_requested = False; self.permission_suspend = False
        self.events = []; self.decide = decide; self.requested = asyncio.Event()
    def emit(self, chunk):
        for line in chunk.splitlines():
            if line.startswith("data:"):
                try: event = json.loads(line[5:])
                except ValueError: continue
                self.events.append(event)
                if event.get("event") == "tool_approval" and event.get("phase") == "requested":
                    self.requested.set()
                    if self.decide:
                        a = event["approval"]
                        store.decide(a["id"], a["session_id"], a["epoch"], self.decide)
                        notify(a["id"])
    def notify(self): pass
    async def finish(self): pass
    def set_round_start(self): pass


def configure_model(monkeypatch, command_args=None):
    model_calls = []
    async def model(request=None, **kwargs):
        model_calls.append(request)
        if len(model_calls) == 1:
            yield "data: " + json.dumps({"tool_calls": [{"index": 0, "id": "call-1", "type": "function", "function": {"name": "run_command", "arguments": json.dumps(command_args or {"command": "echo safe"})}}], "finish_reason": "tool_calls"}) + "\n\n"
        else:
            yield 'data: {"content":"完成","finish_reason":"stop"}\n\n'
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(cf.ChatLLM, "chat_completions", model)
    return model_calls


def request():
    return ChatLLMRequest(session_id="permission-e2e", messages=[{"role": "user", "content": "执行测试"}], tool_names=["run_command"], use_backend_history=False)


def test_main_deny_preserves_model_tool_loop_without_executing(environment, monkeypatch):
    model_calls = configure_model(monkeypatch)
    executions = []
    monkeypatch.setattr(cf, "try_execute_builtin_command_tool", lambda *args: executions.append(args) or {"stdout": "done"})
    async def run():
        stream = Stream("deny")
        await cf._run_chat_generation(request(), stream)
        assert not executions
        assert len(model_calls) == 2
        assert any(event.get("content") == "完成" for event in stream.events)
        approval_event = next(event for event in stream.events if event.get("event") == "tool_approval")
        assert approval_event["approval"]["round"] == 1
        streamed_call = next(event["tool_calls"][0] for event in stream.events if event.get("tool_calls"))
        assert streamed_call["id"] == approval_event["approval"]["tool_call_id"]
        returned = next(event["tool_return"] for event in stream.events if "tool_return" in event)
        assert returned["tool_call_id"] == "call-1"
        history = (environment / "history" / "permission-e2e_chat.jsonl").read_text(encoding="utf-8")
        assert "PERMISSION_DENIED" in history and "tool_approval" in history
    asyncio.run(run())


@pytest.mark.parametrize("backend_history", [False, True])
def test_main_wait_shutdown_resume_once_without_resending_user_or_model_call(environment, monkeypatch, backend_history):
    model_calls = configure_model(monkeypatch)
    executions = []
    monkeypatch.setattr(cf, "try_execute_builtin_command_tool", lambda *args: executions.append(args) or {"stdout": "done"})
    async def run():
        req = request(); req.use_backend_history = backend_history
        first = Stream()
        running = asyncio.create_task(cf._run_chat_generation(req, first))
        await asyncio.wait_for(first.requested.wait(), 5)
        assert not executions and len(model_calls) == 1
        approval = store.pending("permission-e2e")[0]
        first.permission_suspend = True; running.cancel()
        with pytest.raises(asyncio.CancelledError): await running
        assert store.task(approval["task"])["state"] == "suspended"
        with store.database() as db:
            db.execute("UPDATE tasks SET owner=-1 WHERE id=?", (approval["task"],))
        store.resume(approval["task"])
        resumed = Stream("approve")
        await cf._run_chat_generation(req, resumed, permission_resume_id=approval["task"])
        assert len(executions) == 1
        assert len(model_calls) == 2, "Saved tool-producing completion must be replayed locally"
        rows = [json.loads(line) for line in (environment / "history" / "permission-e2e_chat.jsonl").read_text(encoding="utf-8").splitlines()]
        rounds = [row for row in rows if row.get("event") == "chat_round"]
        assert len(rounds) == 1
        assert len([event for event in rounds[0]["events"] if event.get("role") == "user"]) == 1
        assert len([event for event in rounds[0]["events"] if event.get("role") == "tool"]) == 1
        assert store.task(approval["task"])["state"] == "completed"
    asyncio.run(run())
