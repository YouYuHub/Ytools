"""Sidebar snapshots must be read-only and include every execution mode."""
import asyncio
import json
from types import SimpleNamespace

import httpx
from fastapi import FastAPI
import factory.chat_factory as chat
import factory.session_worker as workers
from routers import chat_router as router


def status(running):
    return SimpleNamespace(is_running=lambda: running, is_generation_running=lambda: running)


def test_registry_snapshots_do_not_start_workers_or_create_history(monkeypatch):
    monkeypatch.setattr(chat, "_SESSION_STREAMS", {"inline": status(True), "done": status(False)})
    monkeypatch.setattr(workers, "_WORKER_PROXIES", {"worker": status(True), "idle": status(False)})
    monkeypatch.setattr(workers, "get_worker_proxy", lambda *_: (_ for _ in ()).throw(AssertionError("must not spawn")))
    assert chat.get_running_chat_session_ids() == ["inline"]
    assert workers.get_running_worker_session_ids() == ["worker"]


def test_batch_snapshot_merges_workers_inline_and_manual_compaction(monkeypatch):
    monkeypatch.setattr(chat, "_SESSION_STREAMS", {"a": status(True), "both": status(True), "done": status(False)})
    monkeypatch.setattr(workers, "_WORKER_PROXIES", {"b": status(True), "both": status(True), "idle": status(False)})
    monkeypatch.setattr(router, "_MANUAL_COMPACTION_JOBS", {
        "压缩 会话": {"task": SimpleNamespace(done=lambda: False)},
        "both": {"task": SimpleNamespace(done=lambda: False)},
        "done": {"task": SimpleNamespace(done=lambda: True)},
        "flagged": {"task": SimpleNamespace(done=lambda: False), "done": True},
    })
    monkeypatch.setattr(router, "get_chat_memory_manager", lambda *_: (_ for _ in ()).throw(AssertionError("must not load history")))
    response = asyncio.run(router.chat_stream_statuses())
    rows = json.loads(response.body)["sessions"]
    assert rows == [
        {"session_id": "a", "chat_running": True, "compacting": False},
        {"session_id": "b", "chat_running": True, "compacting": False},
        {"session_id": "both", "chat_running": True, "compacting": True},
        {"session_id": "压缩 会话", "chat_running": False, "compacting": True},
    ]


def test_http_batch_is_empty_after_all_tasks_end(monkeypatch):
    monkeypatch.setattr(chat, "_SESSION_STREAMS", {"ended": status(False)})
    monkeypatch.setattr(workers, "_WORKER_PROXIES", {"idle": status(False)})
    monkeypatch.setattr(router, "_MANUAL_COMPACTION_JOBS", {})
    app = FastAPI()
    app.include_router(router.api_chat_router)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/chat_stream/statuses")
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            assert response.json() == {"sessions": []}
    asyncio.run(run())
