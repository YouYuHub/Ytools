import concurrent.futures
import json
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from memory import chat_memory, history_search, session_archives as archives, session_listing


def history(root, sid, title=None):
    path = root / (sid + "_chat.jsonl")
    path.write_text(json.dumps({"_meta": {"title": title or sid, "updated_at": "2026-10-21 12:00:00", "group_id": "original-group"}}) + "\n" +
        json.dumps({"event": "chat_round", "events": [{"role": "user", "content": "common question"}, {"role": "assistant", "content": "answer"}]}) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(chat_memory, "HISTORY_ROOT", tmp_path)
    monkeypatch.setattr(chat_memory, "LOCK_ROOT", tmp_path / "locks")
    monkeypatch.setattr(chat_memory, "UPLOAD_HISTORY_ROOT", tmp_path / "uploads")
    return tmp_path


@pytest.fixture
def api(root):
    from routers.chat_router import api_chat_router
    app = FastAPI(); app.include_router(api_chat_router)
    with TestClient(app) as client:
        yield client


def test_archive_is_idempotent_durable_and_does_not_touch_history_or_pending_task(root):
    path = history(root, "long", "保留标题")
    sidecar = root / "sidecars"; sidecar.mkdir()
    pending = sidecar / "long_chat.jsonl.pending"; pending.write_text('{"status":"running"}')
    before = path.read_bytes(), path.stat().st_mtime_ns, pending.read_bytes()
    with patch.object(chat_memory, "ChatMemoryManager", side_effect=AssertionError("No manager or history rewrite")):
        first = archives.set_archived("long", True)
        assert first == archives.set_archived("long_chat.jsonl", True)
        assert not session_listing.list_session_summaries()
        assert session_listing.list_session_summaries(archive_scope="all")[0]["archived"]
        rows = archives.list_archived()
        assert rows["sessions"][0]["title"] == "保留标题" and rows["sessions"][0]["group_id"] == "original-group"
    # A worker can keep writing the existing JSONL without carrying archive flags.
    assert (path.read_bytes(), path.stat().st_mtime_ns, pending.read_bytes()) == before
    assert archives.snapshot()["long"]["archived_at"] == first["archived_at"]
    path.write_text(path.read_text(encoding="utf-8") + json.dumps({"event": "chat_round", "events": []}) + "\n", encoding="utf-8")
    assert not session_listing.list_session_summaries()
    archives.set_archived("long", False)
    assert session_listing.list_session_summaries()[0]["group_id"] == "original-group"
    assert archives.list_archived()["total"] == 0


def test_parallel_mutations_do_not_lose_other_archive_flags(root):
    for i in range(12): history(root, "s" + str(i))
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda i: archives.set_archived("s" + str(i), True), range(12)))
    assert len(archives.snapshot()) == 12
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda i: archives.set_archived("s" + str(i), False), range(6)))
    assert set(archives.snapshot()) == {"s" + str(i) for i in range(6, 12)}


def test_listing_filters_titles_and_ids_paginates_and_skips_missing_files(root):
    for i in range(6):
        history(root, "s" + str(i), "主题 " + str(i)); archives.set_archived("s" + str(i), True)
    (root / "s5_chat.jsonl").unlink()
    first = archives.list_archived(limit=2)
    assert first["total"] == first["archived_count"] == 5 and first["next_offset"] == 2
    second = archives.list_archived(limit=2, offset=2)
    assert not {r["id"] for r in first["sessions"]} & {r["id"] for r in second["sessions"]}
    assert archives.list_archived(q="主题 2")["sessions"][0]["id"] == "s2"
    assert archives.list_archived(q="S3")["sessions"][0]["id"] == "s3"


def test_archived_content_search_uses_scope_before_counting_and_revision(root):
    history(root, "active"); archived = history(root, "archived")
    first = history_search.search_history("common", root=root, limit=1)
    before = archived.read_bytes(), archived.stat().st_mtime_ns
    archives.set_archived("archived", True)
    with patch.object(history_search, "_read_file", side_effect=AssertionError("Archive must not reindex unchanged histories")):
        assert history_search.search_history("common", root=root)["session_count"] == 1
        assert history_search.search_history("common", root=root, archive_scope="all")["session_count"] == 2
        result = history_search.search_history("common", root=root, archive_scope="archived")
        assert result["total"] == result["session_count"] == 1 and result["results"][0]["archived"]
        assert history_search.search_history("common", root=root, archive_scope="active")["results"][0]["session_id"] == "active"
        with pytest.raises(history_search.SearchSnapshotChanged):
            history_search.search_history("common", root=root, offset=1, revision=first["revision"])
    assert (archived.read_bytes(), archived.stat().st_mtime_ns) == before


def test_archive_api_missing_sessions_validation_and_normal_list_exclusion(root, api):
    history(root, "one")
    payload = {"session_id": "one", "archived": True}
    assert api.put("/chat_history/archive", json=payload).json()["archived"]
    assert api.get("/chat_history/summaries").json() == []
    assert api.get("/chat_history/sessions").json() == []
    assert api.get("/chat_history/summaries", params={"session_id": "one", "archive_scope": "all"}).json()[0]["archived"]
    assert api.get("/chat_history/archives").json()["sessions"][0]["id"] == "one"
    result = api.get("/chat_history/search", params={"q": "common", "archive_scope": "archived"}).json()
    assert result["session_count"] == 1 and result["results"][0]["archived"]
    for extra in ({"archived": "false"}, {"archived": 1}, {"session_id": ""}, {"extra": 1}):
        assert api.put("/chat_history/archive", json={**payload, **extra}).status_code == 422
    for value in (True, False):
        assert api.put("/chat_history/archive", json={"session_id": "missing", "archived": value}).status_code == 404
    assert not (root / "missing_chat.jsonl").exists()
    assert api.get("/chat_history/archives", params={"limit": 200}).status_code == 422
    assert api.get("/chat_history/summaries", params={"archive_scope": "bad"}).status_code == 422


def test_archived_session_can_be_opened_with_live_metadata_without_persisting_flags_in_jsonl(root, api):
    path = history(root, "archive_meta_read")
    archives.set_archived("archive_meta_read", True)
    response = api.get("/chat_history/meta", params={"session_id": "archive_meta_read"})
    assert response.status_code == 200 and response.json()["archived"]
    opened = api.get("/chat_history/file", params={"session_id": "archive_meta_read"})
    assert opened.status_code == 200 and "common question" in opened.text
    assert "archived" not in json.loads(path.read_text(encoding="utf-8").splitlines()[0])["_meta"]
    assert not session_listing.list_session_summaries()


def test_delete_removes_archive_flag_before_reusing_session_id(root, monkeypatch):
    from chat.protocols import native_store, responses_store
    monkeypatch.setattr(native_store, "session_directory", lambda sid: root / "native" / sid)
    monkeypatch.setattr(responses_store, "session_directory", lambda sid: root / "responses" / sid)
    history(root, "remove"); archives.set_archived("remove", True)
    chat_memory.ChatMemoryManager.delete_chat_session_file("remove")
    assert not archives.snapshot() and not (root / "remove_chat.jsonl").exists()
    history(root, "remove")
    assert session_listing.list_session_summaries()[0]["id"] == "remove"


def test_corrupt_archive_state_is_reported_without_overwriting_or_revealing_hidden_sessions(root, api):
    history(root, "one")
    path = root / "session_archives.json"; path.write_text("broken", encoding="utf-8")
    assert api.get("/chat_history/archives").status_code == 503
    assert api.get("/chat_history/summaries").status_code == 503
    assert api.put("/chat_history/archive", json={"session_id": "one", "archived": True}).status_code == 503
    assert path.read_text() == "broken"
