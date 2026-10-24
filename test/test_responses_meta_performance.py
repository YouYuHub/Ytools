"""Responses mirrors remain durable without rewriting the history hot path."""
import json
from pathlib import Path

import pytest

from memory import chat_memory as memory


@pytest.fixture
def history(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "HISTORY_ROOT", tmp_path / "history")
    monkeypatch.setattr(memory, "LOCK_ROOT", tmp_path / "locks")
    memory.HISTORY_ROOT.mkdir()
    path = memory._get_chat_history_file("perf-meta")
    meta = memory._default_meta("perf-meta")
    entries = [{"event": "chat_round", "question": "hello", "events": [], "status": "completed"}]
    memory._write_meta_and_entries(path, meta, entries)
    return path, entries


def test_mirror_never_reads_or_rewrites_whole_history(history, monkeypatch):
    path, _ = history
    original, modified = path.read_bytes(), path.stat().st_mtime_ns
    state = {"generation": 1, "status": "request_pending", "pending_request_ref": "r1"}
    with monkeypatch.context() as m:
        m.setattr(memory, "_read_jsonlines", lambda *args: pytest.fail("whole history read"))
        memory.update_responses_session_meta("perf-meta", state)
        assert memory.read_session_meta_value("perf-meta", "responses_state") == state
        assert memory._read_meta_first_line(path)["responses_state"] == state
    assert path.read_bytes() == original
    assert path.stat().st_mtime_ns == modified
    sidecar = memory._responses_meta_path(path)
    before = sidecar.stat().st_mtime_ns
    memory.update_responses_session_meta("perf-meta", state)
    assert sidecar.stat().st_mtime_ns == before
    meta, _ = memory._load_meta_and_entries(path, "perf-meta")
    assert meta["responses_state"] == state


def test_next_history_write_folds_state_and_edit_invalidates_overlay(history):
    path, entries = history
    state = {"generation": 1, "status": "ready", "head_response_id": "response1"}
    memory.update_responses_session_meta("perf-meta", state)
    meta, loaded = memory._load_meta_and_entries(path, "perf-meta")
    assert loaded == entries
    memory._write_meta_and_entries(path, meta, loaded)
    assert json.loads(path.read_text(encoding="utf-8").splitlines()[0])["_meta"]["responses_state"] == state
    # An explicit remote-chain invalidation must win over the older sidecar.
    meta["responses_state"] = {"generation": 2, "status": "invalidated"}
    memory._write_meta_and_entries(path, meta, loaded)
    assert memory._read_meta_first_line(path)["responses_state"] == meta["responses_state"]


def test_raw_download_contains_latest_state(history):
    path, _ = history
    state = {"status": "ready", "generation": 3}
    memory.update_responses_session_meta("perf-meta", state)
    response = memory.ChatMemoryManager.get_chat_session_file("perf-meta")
    assert Path(response.path) == path
    assert json.loads(path.read_text(encoding="utf-8").splitlines()[0])["_meta"]["responses_state"] == state


def test_replaced_history_ignores_old_mirror_and_corrupt_sidecar(history):
    path, _ = history
    memory.update_responses_session_meta("perf-meta", {"status": "old"})
    new_meta = memory._default_meta("perf-meta")
    new_meta["title"] = "replaced"
    memory._write_meta_and_entries(path, new_meta, [])
    assert "responses_state" not in memory._read_meta_first_line(path)
    memory._responses_meta_path(path).write_text("broken", encoding="utf-8")
    assert memory._read_meta_first_line(path)["title"] == "replaced"


def test_atomic_mirror_retry_keeps_previous_state(history, monkeypatch):
    path, _ = history
    old = {"status": "ready"}
    memory.update_responses_session_meta("perf-meta", old)
    monkeypatch.setattr(memory, "_ATOMIC_WRITE_RETRY_DELAYS", ())
    monkeypatch.setattr(memory.os, "replace", lambda *args: (_ for _ in ()).throw(PermissionError("busy")))
    with pytest.raises(PermissionError):
        memory.update_responses_session_meta("perf-meta", {"status": "pending"})
    assert memory._read_meta_first_line(path)["responses_state"] == old
