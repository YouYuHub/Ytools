import asyncio
import json
import os
import threading
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from memory import chat_memory, session_listing


def write_history(root, sid, meta=None, body=b"unreadable-history-body\xff\xff"):
    path = root / (sid + "_chat.jsonl")
    header = {"_meta": meta if meta is not None else {"title": "title " + sid, "updated_at": "2026-10-17 12:00:00", "user_questions": []}}
    path.write_bytes(json.dumps(header, ensure_ascii=False).encode() + b"\n" + body)
    return path


def test_batch_reads_only_headers_without_managers_writes_or_private_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(chat_memory, "HISTORY_ROOT", tmp_path)
    one = write_history(tmp_path, "one", {"title": "名称", "updated_at": "2026-10-17 12:00:00", "created_at": "2026-10-01 10:00:00", "group_id": "g", "user_questions": [], "model_selection": {"apiKey": "test-key-private"}, "responses_state": {"head": "private-pointer"}})
    two = write_history(tmp_path, "two", {"title": "session_two", "user_questions": [None, "首个问题"], "updated_at": "2026-10-16 00:00:00"})
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in (one, two)}
    before_tree = {p.name for p in tmp_path.iterdir()}
    with patch.object(chat_memory, "ChatMemoryManager", side_effect=AssertionError("No manager on list")), \
         patch.object(chat_memory, "_read_jsonlines", side_effect=AssertionError("No full history parse")), \
         patch.object(chat_memory, "_recompute_meta_from_entries", side_effect=AssertionError("No repair")):
        rows = session_listing.list_session_summaries()
        assert [row["id"] for row in rows] == ["one", "two"]
        assert rows[0] == {"id": "one", "filename": "one_chat.jsonl", "title": "名称", "updated_at": "2026-10-17 12:00:00", "created_at": "2026-10-01 10:00:00", "group_id": "g"}
        assert rows[1]["title"] == "首个问题"
        assert session_listing.list_session_summaries("absent") == []
        assert session_listing.list_session_summaries("one_chat.jsonl") == rows[:1]
    assert {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in (one, two)} == before
    assert {p.name for p in tmp_path.iterdir()} == before_tree


def test_bad_empty_oversized_and_busy_headers_do_not_force_full_reads(tmp_path, monkeypatch):
    monkeypatch.setattr(chat_memory, "HISTORY_ROOT", tmp_path)
    (tmp_path / "broken_chat.jsonl").write_bytes(b"not json\n" + b"x" * 10000)
    (tmp_path / "empty_chat.jsonl").touch()
    (tmp_path / "big_chat.jsonl").write_bytes(b'{"event":"chat_round","content":"' + b"x" * 10000)
    write_history(tmp_path, "busy")
    monkeypatch.setattr(session_listing, "MAX_SUMMARY_HEADER_BYTES", 256)
    original = Path.open
    class GuardedFile:
        def __init__(self, fp): self.fp = fp
        def __enter__(self): return self
        def __exit__(self, *args): self.fp.close()
        def readline(self, count):
            assert count == 257
            return self.fp.readline(count)
    def guarded(path, *args, **kwargs):
        if path.name == "busy_chat.jsonl": raise PermissionError("busy")
        return GuardedFile(original(path, *args, **kwargs))
    monkeypatch.setattr(Path, "open", guarded)
    rows = session_listing.list_session_summaries()
    assert {row["id"] for row in rows} == {"broken", "empty", "big", "busy"}
    assert all(row["title"] == row["id"] and row["updated_at"] for row in rows)


def test_summary_route_legacy_list_and_missing_lookup_are_read_only(tmp_path, monkeypatch):
    from routers.chat_router import api_chat_router
    monkeypatch.setattr(chat_memory, "HISTORY_ROOT", tmp_path)
    write_history(tmp_path, "sample")
    app = FastAPI(); app.include_router(api_chat_router)
    with patch.object(chat_memory, "ChatMemoryManager", side_effect=AssertionError("No manager")), TestClient(app) as client:
        response = client.get("/chat_history/summaries")
        assert response.status_code == 200
        assert response.json()[0]["id"] == "sample"
        assert client.get("/chat_history/summaries", params={"session_id": "missing"}).json() == []
        assert client.get("/chat_history/sessions").json() == ["sample_chat.jsonl"]
    assert not (tmp_path / "missing_chat.jsonl").exists()


def test_summary_route_offloads_io_from_event_loop(monkeypatch):
    from routers.chat_router import list_chat_history_summaries
    entered, release = threading.Event(), threading.Event()
    def slow(_sid):
        entered.set()
        assert release.wait(2)
        return []
    async def run():
        with patch.object(session_listing, "list_session_summaries", slow):
            task = asyncio.create_task(list_chat_history_summaries())
            try:
                for _ in range(100):
                    if entered.is_set(): break
                    await asyncio.sleep(.005)
                assert entered.is_set() and not task.done()
            finally: release.set()
            assert await task == []
    asyncio.run(run())


def test_background_recovery_only_visits_dead_unmanaged_checkpoints(tmp_path, monkeypatch):
    from memory import permission_store
    monkeypatch.setattr(chat_memory, "HISTORY_ROOT", tmp_path)
    sidecars = tmp_path / "sidecars"; sidecars.mkdir()
    for sid, pid in (("dead", -1), ("live", os.getpid()), ("paused", -1), ("missing", -1)):
        if sid != "missing": write_history(tmp_path, sid)
        (sidecars / (sid + "_chat.jsonl.pending")).write_text(json.dumps({"event": "chat_round", "writer_pid": pid}))
    write_history(tmp_path, "normal")
    calls = []
    monkeypatch.setattr(permission_store, "has_restorable_round", lambda sid: sid == "paused")
    monkeypatch.setattr(chat_memory, "ChatMemoryManager", lambda sid: calls.append(sid))
    session_listing.recover_pending_sessions(threading.Event())
    assert calls == ["dead"]
    stop = threading.Event(); stop.set()
    session_listing.recover_pending_sessions(stop)
    assert calls == ["dead"]
    assert not (tmp_path / "missing_chat.jsonl").exists()


def test_real_orphan_recovery_preserves_partial_events_and_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(chat_memory, "HISTORY_ROOT", tmp_path)
    monkeypatch.setattr(chat_memory, "LOCK_ROOT", tmp_path / "lock")
    path = write_history(tmp_path, "orphan", body=b"")
    sidecars = tmp_path / "sidecars"; sidecars.mkdir()
    pending_path = sidecars / "orphan_chat.jsonl.pending"
    events = [{"role": "user", "content": "问题"}, {"role": "assistant", "content": "已保存的部分回复"}]
    pending_path.write_text(json.dumps({"event": "chat_round", "status": "running", "writer_pid": -1, "started_at": "2026-10-17 12:00:00", "events": events}), encoding="utf-8")
    session_listing.recover_pending_sessions(threading.Event())
    rounds = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    assert rounds[-1]["events"] == events and rounds[-1]["status"] == "interrupted"
    saved = path.read_bytes()
    session_listing.recover_pending_sessions(threading.Event())
    assert path.read_bytes() == saved


def test_recovery_silent_without_checkpoint_but_warns_on_corrupt_one(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(chat_memory, "HISTORY_ROOT", tmp_path)
    write_history(tmp_path, "clean")
    write_history(tmp_path, "broken")
    (tmp_path / "sidecars").mkdir()
    (tmp_path / "sidecars" / "broken_chat.jsonl.pending").write_text("{not json", encoding="utf-8")
    with caplog.at_level("WARNING", logger="memory.session_listing"):
        session_listing.recover_pending_sessions(threading.Event())
    assert "clean_chat.jsonl.pending" not in caplog.text
    assert "broken_chat.jsonl.pending" in caplog.text
