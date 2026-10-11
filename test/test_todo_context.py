"""计划以真实工具交互保留，避免提示词里的旧快照及压缩后的状态丢失。"""
import asyncio
import copy
import json
from unittest.mock import AsyncMock

from factory.agent_runtime.todo_runtime import TodoContext, latest_todo_exchanges
from memory.chat_memory import ChatMemoryManager


def exchange(call_id, status="pending", *, todos=None, error=None):
    todos = todos if todos is not None else [{"id": "step-1", "content": "验证计划", "status": status}]
    result = {"success": True, "todos": todos, "plan_complete": bool(todos) and all(t["status"] == "done" for t in todos)}
    if error:
        result = {"success": False, "error": error}
    return [
        {"role": "assistant", "tool_calls": [{"id": call_id, "type": "function", "function": {
            "name": "todo_write", "arguments": json.dumps({"todos": todos}, ensure_ascii=False)}}]},
        {"role": "tool", "tool_call_id": call_id, "_tool_name": "todo_write", "content": json.dumps(result, ensure_ascii=False)},
    ]


def assert_paired(messages):
    pending = set()
    for message in messages:
        if message["role"] == "assistant":
            pending.update(c["id"] for c in message.get("tool_calls", []))
        elif message["role"] == "tool":
            assert message["tool_call_id"] in pending
            pending.remove(message["tool_call_id"])
    assert not pending


def test_latest_native_state_replaces_old_calls_and_system_snapshot():
    old = exchange("old")
    old[0]["tool_calls"].append({"id": "read", "function": {"name": "read_file", "arguments": "{}"}})
    messages = [{"role": "system", "content": "基础指令"}, *old,
                {"role": "tool", "tool_call_id": "read", "content": "file"},
                {"role": "system", "content": "旧计划 0/4", "_todo_summary": True},
                *exchange("new", "done")]
    original = copy.deepcopy(messages)
    context = TodoContext()
    context.sync(messages)
    assert [m["content"] for m in messages if m["role"] == "system"] == ["基础指令"]
    assert [m["tool_call_id"] for m in messages if m["role"] == "tool"] == ["read", "new"]
    assert json.loads(messages[-1]["content"])["todos"][0]["status"] == "done"
    assert original[1]["tool_calls"][0]["id"] == "old"  # 不原地修改声明对象
    assert_paired(messages)


def test_compaction_restores_latest_real_pair_once_and_clear_replaces_it():
    context = TodoContext()
    messages = [{"role": "user", "content": "任务"}, *exchange("latest", "in_progress")]
    context.sync(messages)
    messages[:] = [{"role": "system", "content": "工具轨迹摘要"}, {"role": "user", "content": "任务"}]
    context.sync(messages)
    context.sync(messages)
    assert len(messages) == 4
    assert_paired(messages)
    messages.extend(exchange("clear", todos=[]))
    context.sync(messages)
    assert [m["tool_call_id"] for m in messages if m["role"] == "tool"] == ["clear"]
    assert json.loads(messages[-1]["content"])["todos"] == []


def test_invalid_update_preserves_current_state_and_actionable_error():
    messages = exchange("saved", "in_progress") + exchange("invalid", error="content 不能为空")
    TodoContext().sync(messages)
    assert len(messages) == 4
    assert json.loads(messages[1]["content"])["todos"][0]["status"] == "in_progress"
    assert "content 不能为空" in messages[-1]["content"]
    assert_paired(messages)


def test_prior_task_exchange_restores_before_new_user_instruction():
    context = TodoContext(exchange("previous", "done"))
    messages = [{"role": "system", "content": "基础指令"}, {"role": "user", "content": "执行新任务"}]
    context.sync(messages)
    assert messages[-1]["content"] == "执行新任务"
    assert_paired(messages)


def test_truncated_historical_tool_result_recovers_full_real_plan():
    full = exchange("previous", "in_progress")
    context = TodoContext(full)
    messages = copy.deepcopy(full)
    messages[1]["content"] = messages[1]["content"][:12]
    context.sync(messages)
    assert json.loads(messages[1]["content"])["todos"][0]["status"] == "in_progress"
    assert_paired(messages)


def test_jsonl_events_supported_and_sub_agent_todo_ignored():
    records = exchange("parent")
    records[1]["result"] = records[1].pop("content")
    records.extend([{"event": "sub_agent", "phase": "model_call", "tool_calls": exchange("child")[0]["tool_calls"]},
                    {"event": "sub_agent", "phase": "tool_result", "tool_call_id": "child", "result": "ignored"}])
    assert latest_todo_exchanges(records)[1]["tool_call_id"] == "parent"


def test_history_recovery_obeys_edit_insert_cutoff_and_plan_source():
    manager = object.__new__(ChatMemoryManager)
    old, new = exchange("round-1"), exchange("round-2", "done")
    rows = [{"event": "chat_round", "events": old}, {"event": "chat_round", "events": new}]
    manager._load_for_read = lambda: ({}, rows)
    manager.get_session_todo = AsyncMock(return_value=json.loads(new[1]["content"])["todos"])
    manager._history_cutoff_rounds = None
    assert asyncio.run(manager.get_latest_todo_exchange())[1]["tool_call_id"] == "round-2"
    manager._history_cutoff_rounds = 2
    assert asyncio.run(manager.get_latest_todo_exchange())[1]["tool_call_id"] == "round-1"
    manager._history_cutoff_rounds = 1
    assert asyncio.run(manager.get_latest_todo_exchange()) == []
    manager._history_cutoff_rounds = None
    manager.get_session_todo = AsyncMock(return_value=[])  # 已清空的计划不得从旧记录复活
    assert asyncio.run(manager.get_latest_todo_exchange()) == []
