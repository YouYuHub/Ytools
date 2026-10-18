"""历史 GET 不创建未知会话，也不扩大长期管理器缓存。"""
import asyncio

import pytest
from fastapi import HTTPException

from memory import chat_memory as cm
from routers.chat_router import get_chat_history_file, get_chat_history_meta


@pytest.mark.parametrize("endpoint", [get_chat_history_file, get_chat_history_meta])
def test_unknown_history_get_has_no_side_effects(tmp_path, monkeypatch, endpoint):
    monkeypatch.setattr(cm, "HISTORY_ROOT", tmp_path)
    monkeypatch.setattr(cm, "LOCK_ROOT", tmp_path / "lock")
    before = len(cm._session_managers)
    before_entries = set(tmp_path.iterdir())
    with pytest.raises(HTTPException) as caught:
        asyncio.run(endpoint(session_id="missing-session"))
    assert caught.value.status_code == 404
    assert set(tmp_path.iterdir()) == before_entries
    assert not (tmp_path / "missing-session_chat.jsonl").exists()
    assert not (tmp_path / "lock").exists()
    assert len(cm._session_managers) == before


def test_existing_history_read_does_not_rewrite_or_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(cm, "HISTORY_ROOT", tmp_path)
    monkeypatch.setattr(cm, "LOCK_ROOT", tmp_path / "lock")
    file_path = tmp_path / "existing_chat.jsonl"
    file_path.write_text('{"_meta":{"title":"已有会话"}}\n', encoding="utf-8")
    original = file_path.stat().st_mtime_ns
    before = len(cm._session_managers)
    response = asyncio.run(get_chat_history_file(session_id="existing"))
    assert "已有会话" in response.body.decode("utf-8")
    asyncio.run(get_chat_history_meta(session_id="existing"))
    assert file_path.stat().st_mtime_ns == original
    assert len(cm._session_managers) == before
    assert not (tmp_path / "lock").exists()
    assert not (tmp_path / "sidecars").exists()


def test_manager_cache_evicts_idle_entries_but_keeps_active_round(tmp_path, monkeypatch):
    monkeypatch.setattr(cm, "HISTORY_ROOT", tmp_path)
    monkeypatch.setattr(cm, "LOCK_ROOT", tmp_path / "lock")
    monkeypatch.setattr(cm, "_session_managers", {})
    monkeypatch.setattr(cm, "_SESSION_MANAGER_MAX_CACHED", 2)
    active = asyncio.run(cm.get_chat_memory_manager("active"))
    active._round_store.pending_round = {"event": "chat_round"}
    asyncio.run(cm.get_chat_memory_manager("idle"))
    newest = asyncio.run(cm.get_chat_memory_manager("newest"))
    assert list(cm._session_managers) == ["active", "newest"]
    assert cm._session_managers["active"] is active
    assert cm._session_managers["newest"] is newest
