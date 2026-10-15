"""Saved permission checkpoints must share history/delete round numbering."""
import asyncio
import json
import pytest
from test_permission_generation import environment
from memory import permission_store as store, chat_memory
from factory import chat_factory as cf
from routers import chat_router


@pytest.mark.parametrize("mode", ["truncate", "single"])
def test_saved_round_preview_is_read_only_and_actual_delete_cancels_task(environment, monkeypatch, mode):
    sid = "saved-permission-edit"
    manager = chat_memory.ChatMemoryManager(sid)
    monkeypatch.setattr(cf, "_generation_use_worker", lambda: False)
    monkeypatch.setattr(cf, "_get_session_stream", lambda _: None)
    async def get_manager(_): return manager
    monkeypatch.setattr(cf, "get_chat_memory_manager", get_manager)
    monkeypatch.setattr(chat_router, "get_chat_memory_manager", get_manager)
    monkeypatch.setattr(chat_router, "peek_worker_proxy", lambda _: None)
    monkeypatch.setattr(chat_router, "is_chat_stream_running", lambda _: False)
    async def run():
        await manager.add_chat_history({"role": "user", "content": "现在几点"})
        await manager.add_chat_history({"role": "assistant", "tool_calls": [{"id": "call", "type": "function", "function": {"name": "run_command", "arguments": "{}"}}]})
        pending = manager._round_store.snapshot_pending_round()
        task = store.create_task(sid, {"request": {}, "mode": "ask", "root": str(environment)})
        store.checkpoint(task["id"], "main", {"round": 1, "pending_round": pending})
        store.set_task_state(task["id"], "suspended")
        plan = {"tool": "run_command", "args": {}, "root": str(environment), "digest": "stable"}
        approval = store.request_approval(task["id"], "main", "call", task["epoch"], plan)
        before = manager._file_path.read_text(encoding="utf-8")
        # Database fallback also works if the sidecar file is missing after restart.
        manager._pending_checkpoint_path.unlink()
        view = await manager.get_file_text()
        assert len([json.loads(line) for line in view.splitlines() if '"chat_round"' in line]) == 1
        result = await manager.delete_rounds(1, mode=mode, dry_run=True)
        assert result["total_rounds"] == 1 and result["planned_rounds"][0]["round"] == 1
        assert manager._file_path.read_text(encoding="utf-8") == before
        assert store.approval(approval["id"])["state"] == "pending"
        assert store.load_checkpoint(task["id"])
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as invalid:
            await chat_router.delete_chat_history_rounds(chat_router.DeleteRoundsRequest(session_id=sid, start_round=99, mode=mode))
        assert invalid.value.status_code == 400
        assert store.task(task["id"])["state"] == "suspended"
        response = await chat_router.delete_chat_history_rounds(chat_router.DeleteRoundsRequest(session_id=sid, start_round=1, mode=mode, delete_files=False))
        assert json.loads(response.body)["removed_rounds"] == 1
        assert store.task(task["id"])["state"] == "cancelled"
        assert not store.load_checkpoint(task["id"])
        assert not store.pending(sid)
        assert not any(json.loads(line).get("event") == "chat_round" for line in manager._file_path.read_text(encoding="utf-8").splitlines())
        await cf.stop_chat_task(sid)
        assert not any(json.loads(line).get("event") == "chat_round" for line in (await manager.get_file_text()).splitlines())
    asyncio.run(run())


def test_reading_history_after_restart_is_not_a_running_generation(environment, monkeypatch):
    sid = "read-only-after-restart"
    monkeypatch.setattr(chat_router, "peek_worker_proxy", lambda _: None)
    monkeypatch.setattr(chat_router, "is_chat_stream_running", lambda _: False)
    async def run():
        manager = await chat_memory.get_chat_memory_manager(sid)
        await manager.add_chat_history({"role": "user", "content": "现在几点"})
        await manager.add_chat_history({"role": "assistant", "done": "[DONE]"})
        await manager.get_file_text()
        # Reading/caching history does not create a generation stop signal.
        assert not hasattr(manager, "stop_requested")
        assert await chat_memory.get_chat_memory_manager(sid) is manager
        status = await chat_router.chat_stream_status(sid)
        assert json.loads(status.body)["running"] is False
        preview = await chat_router.delete_chat_history_rounds(chat_router.DeleteRoundsRequest(session_id=sid, start_round=1, dry_run=True))
        assert json.loads(preview.body)["total_rounds"] == 1
        # Repeated preview also works after the first request caches a manager.
        preview = await chat_router.delete_chat_history_rounds(chat_router.DeleteRoundsRequest(session_id=sid, start_round=1, dry_run=True))
        assert json.loads(preview.body)["total_rounds"] == 1
        await chat_memory.cleanup_chat_memory_manager(sid)
    asyncio.run(run())


@pytest.mark.parametrize("source", ["worker", "stream"])
def test_active_generation_blocks_edit_and_status_reports_running(environment, monkeypatch, source):
    from types import SimpleNamespace
    from fastapi import HTTPException
    monkeypatch.setattr(chat_router, "peek_worker_proxy", lambda _: SimpleNamespace(is_generation_running=lambda: True) if source == "worker" else None)
    monkeypatch.setattr(chat_router, "is_chat_stream_running", lambda _: source == "stream")
    async def run():
        status = await chat_router.chat_stream_status("active")
        assert json.loads(status.body)["running"] is True
        with pytest.raises(HTTPException) as error:
            await chat_router.delete_chat_history_rounds(chat_router.DeleteRoundsRequest(session_id="active", start_round=1, dry_run=True))
        assert error.value.status_code == 409
    asyncio.run(run())
