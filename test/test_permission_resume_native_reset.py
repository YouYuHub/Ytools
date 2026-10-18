"""Resume rewinds natively started Responses slots the ledger proves never ran."""
import asyncio

import pytest

from test_permission_generation import environment  # noqa: F401 (fixture reuse)
from test_responses_api import CONFIG, answer, commit, turn  # noqa: F401 (CONFIG unused, keeps import stable)
from chat.protocols import responses_store as native
from chat.protocols.errors import ResponsesProtocolError
from memory import permission_store as store
from factory.agent_runtime.permission_runtime import PermissionRuntime, reset_unexecuted_native


def emit(event):
    return asyncio.sleep(0)


def checkpoint_with_call(task_id, reference, call_id, agent="main"):
    assistant = {"role": "assistant", "provider_state": reference,
                 "tool_calls": [{"id": call_id, "type": "function",
                                 "function": {"name": "run_command", "arguments": "{}"}}]}
    store.checkpoint(task_id, agent, {"assistant": assistant, "messages": [assistant]})


def suspend_like_death(task_id):
    store.task(task_id)
    with store.database() as db:
        db.execute("UPDATE tasks SET owner=-1 WHERE id=?", (task_id,))


def test_resume_rewinds_running_slot_without_execution(environment):
    reference = commit(turn([{"role": "user", "content": "q"}]),
                       answer("resp_t", "", calls=[("call_1", "run_command", "{}")]))
    native.tool_started("s", reference, "call_1")
    runtime = PermissionRuntime("s", {}, emit, lambda: False)
    checkpoint_with_call(runtime.id, reference, "call_1")
    runtime.close("suspended")
    suspend_like_death(runtime.id)
    assert native.load_record("s", reference)["tool_status"]["call_1"]["status"] == "running"
    store.resume(runtime.id)
    resumed = PermissionRuntime("s", {}, emit, lambda: False, resume_id=runtime.id)
    try:
        assert native.load_record("s", reference)["tool_status"]["call_1"]["status"] == "planned"
        native.tool_started("s", reference, "call_1")
        assert native.load_record("s", reference)["tool_status"]["call_1"]["status"] == "running"
    finally:
        resumed.close("completed")


def test_reset_skips_executing_ledger_slot(environment):
    reference = commit(turn([{"role": "user", "content": "q"}]),
                       answer("resp_t", "", calls=[("call_1", "run_command", "{}")]))
    native.tool_started("s", reference, "call_1")
    runtime = PermissionRuntime("s", {}, emit, lambda: False)
    try:
        checkpoint_with_call(runtime.id, reference, "call_1")
        store.claim_execution(runtime.id, runtime.epoch, "main", "call_1")
        assert reset_unexecuted_native(runtime.id) == 0
        assert native.load_record("s", reference)["tool_status"]["call_1"]["status"] == "running"
        with pytest.raises(ResponsesProtocolError, match="状态未知"):
            native.tool_started("s", reference, "call_1")
    finally:
        runtime.close("completed")


def test_reset_rewinds_completed_ledger_cache(environment):
    reference = commit(turn([{"role": "user", "content": "q"}]),
                       answer("resp_t", "", calls=[("call_1", "run_command", "{}")]))
    native.tool_started("s", reference, "call_1")
    runtime = PermissionRuntime("s", {}, emit, lambda: False)
    try:
        checkpoint_with_call(runtime.id, reference, "call_1")
        store.completed(runtime.id, "main", "call_1", {"stdout": "cached"})
        assert reset_unexecuted_native(runtime.id) == 1
        assert native.load_record("s", reference)["tool_status"]["call_1"]["status"] == "planned"
    finally:
        runtime.close("completed")


def test_reset_ignores_non_responses_and_missing_slots(environment):
    runtime = PermissionRuntime("s", {}, emit, lambda: False)
    try:
        checkpoint_with_call(runtime.id, {"protocol": "messages", "record_id": "x"}, "call_9")
        assert reset_unexecuted_native(runtime.id) == 0
        assert reset_unexecuted_native("0" * 32) == 0
    finally:
        runtime.close("completed")


def test_reset_tool_matrix(environment):
    reference = commit(turn([{"role": "user", "content": "q"}]),
                       answer("resp_t", "", calls=[("call_1", "run_command", "{}")]))
    assert native.reset_tool("s", reference, "call_1") is False
    native.tool_started("s", reference, "call_1")
    assert native.reset_tool("s", reference, "call_1") is True
    assert native.load_record("s", reference)["tool_status"]["call_1"]["status"] == "planned"
    assert native.reset_tool("s", reference, "call_1") is False
    record = native.load_record("s", reference)
    record["tool_status"]["call_1"] = {"status": "unknown"}
    native._write(native._record_path("s", record["record_id"]), record)
    assert native.reset_tool("s", reference, "call_1") is True
    native.tool_completed("s", reference, "call_1", "done")
    assert native.reset_tool("s", reference, "call_1") is False
    assert native.reset_tool("s", reference, "call_missing") is False
    assert native.reset_tool("s", {"protocol": "responses", "scope": "chat_model",
                                  "record_id": "f" * 32}, "call_1") is False
    assert native.reset_tool("s", {"protocol": "messages"}, "call_1") is False
    assert native.reset_tool("s", None, "call_1") is False
