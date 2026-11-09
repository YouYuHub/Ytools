"""暂存消息（引导/队列）持久化一期测试：侧车存取 + 路由 + 注入快照。"""
import asyncio
import os
import sys
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from factory import chat_factory
from memory.chat_memory import ChatMemoryManager
from routers import outbox_router

TEST_SESSION = "outbox_ut_session"


def _cleanup():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "history_files"
    for suffix in ("", ".outbox"):
        try:
            (root / f"{TEST_SESSION}_chat.jsonl{suffix}").unlink()
        except OSError:
            pass
        try:
            (root / "sidecars" / f"{TEST_SESSION}_chat.jsonl{suffix}").unlink()
        except OSError:
            pass


@pytest.fixture()
def manager():
    _cleanup()
    try:
        yield ChatMemoryManager(TEST_SESSION)
    finally:
        _cleanup()


def test_upsert_list_remove_roundtrip(manager):
    async def go():
        assert await manager.get_session_outbox() == []
        await manager.upsert_session_outbox({"id": "o1", "kind": "steer", "text": "a"})
        await manager.upsert_session_outbox({"id": "o2", "kind": "queue", "text": "b",
                                             "quotes": [{"text": "q"}],
                                             "docs": [{"filename": "a.pdf", "uploaded": True}]})
        entries = await manager.get_session_outbox()
        assert [(e["id"], e["kind"]) for e in entries] == [("o1", "steer"), ("o2", "queue")]
        assert entries[1]["quotes"] == [{"text": "q"}]
        # 按 id 幂等更新
        await manager.upsert_session_outbox({"id": "o1", "kind": "steer", "text": "a2"})
        entries = await manager.get_session_outbox()
        assert [e["text"] for e in entries if e["id"] == "o1"] == ["a2"]
        assert await manager.remove_session_outbox(["o1", "missing"]) == 1
        assert [e["id"] for e in await manager.get_session_outbox()] == ["o2"]
        assert await manager.remove_session_outbox(["o2"]) == 1
        assert await manager.get_session_outbox() == []
        # 空列表删除侧车文件
        assert not manager._outbox_sidecar_path.exists()
    asyncio.run(go())


def test_normalize_rejects_invalid(manager):
    async def go():
        for bad in ({"kind": "steer"}, {"id": "x", "kind": "nope"},
                    {"id": "x", "kind": "steer", "text": "y" * 50001},
                    {"id": "  ", "kind": "queue"}):
            with pytest.raises(ValueError):
                await manager.upsert_session_outbox(bad)
        assert await manager.get_session_outbox() == []
    asyncio.run(go())


def test_corrupt_sidecar_reads_empty(manager):
    manager._outbox_sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    manager._outbox_sidecar_path.write_text("{broken", encoding="utf-8")
    assert asyncio.run(manager.get_session_outbox()) == []


def test_routes_submit_get_remove(manager):
    async def go():
        with patch.object(outbox_router, "get_chat_memory_manager", AsyncMock(return_value=manager)), \
                patch.object(chat_factory, "peek_injected_messages", AsyncMock(return_value=[])):
            got = await outbox_router.get_outbox(TEST_SESSION)
            assert got == {"entries": [], "injected": [], "running": False}
            saved = await outbox_router.submit_outbox(outbox_router.OutboxSubmit(
                session_id=TEST_SESSION, id="o1", kind="queue", text="hi"))
            assert saved["entry"]["id"] == "o1"
            got = await outbox_router.get_outbox(TEST_SESSION)
            assert [e["id"] for e in got["entries"]] == ["o1"]
            removed = await outbox_router.remove_outbox(outbox_router.OutboxRemove(
                session_id=TEST_SESSION, ids=["o1"]))
            assert removed == {"removed": 1}
    asyncio.run(go())


def test_routes_reject_bad_payload(manager):
    import pydantic
    # schema 层拒绝超长（真实 HTTP 下 FastAPI 转 422）
    with pytest.raises(pydantic.ValidationError):
        outbox_router.OutboxSubmit(session_id=TEST_SESSION, id="o1", kind="steer", text="x" * 50001)
    # 存储层兜底校验（防御性，与 schema 同口径）
    async def go():
        with pytest.raises(ValueError):
            await manager.upsert_session_outbox({"id": "o1", "kind": "nope"})
    asyncio.run(go())


def test_peek_inline_returns_snapshot():
    stream = SimpleNamespace(done=False, injected_messages=deque([
        {"message_id": "m1", "content": "hello", "quotes": [{"text": "q"}]},
        {"message_id": "m2", "content": [{"type": "text", "text": "world"}]},
        {"message_id": "m3", "content": [
            {"type": "text", "text": "see"},
            {"type": "image_url", "image_url": {"url": "media://a.png"}},
            {"type": "video_url", "video_url": {"url": "media://b.mp4"}},
        ]},
    ]))
    with patch.object(chat_factory, "_generation_use_worker", return_value=False), \
            patch.object(chat_factory, "_get_session_stream", return_value=stream):
        snapshot = asyncio.run(chat_factory.peek_injected_messages("ut"))
    assert [(m["message_id"], m["text"]) for m in snapshot] == [("m1", "hello"), ("m2", "world"), ("m3", "see")]
    assert snapshot[0]["quotes"] == [{"text": "q"}]
    assert snapshot[2]["media"] == [{"media_ref": "media://a.png", "kind": "image"},
                                      {"media_ref": "media://b.mp4", "kind": "video"}]
    # 只读：队列长度不变
    assert len(stream.injected_messages) == 3


def test_peek_no_stream_returns_empty():
    with patch.object(chat_factory, "_generation_use_worker", return_value=False), \
            patch.object(chat_factory, "_get_session_stream", return_value=None):
        assert asyncio.run(chat_factory.peek_injected_messages("ut")) == []


def test_peek_worker_roundtrip():
    class Proxy:
        def add_handler(self, handler): self.handler = handler
        def remove_handler(self, handler): self.handler = None
        def peek_injected_messages(self, **kwargs):
            self.handler({"type": "injected_snapshot", "request_id": kwargs["request_id"],
                          "messages": [{"message_id": "w1", "content": "hi", "quotes": []}]})
            return True
    with patch.object(chat_factory, "_generation_use_worker", return_value=True), \
            patch.object(chat_factory, "peek_worker_proxy", return_value=Proxy()):
        snapshot = asyncio.run(chat_factory.peek_injected_messages("ut"))
    assert [(m["message_id"], m["text"]) for m in snapshot] == [("w1", "hi")]


def test_peek_worker_absent_returns_empty():
    with patch.object(chat_factory, "_generation_use_worker", return_value=True), \
            patch.object(chat_factory, "peek_worker_proxy", return_value=None):
        assert asyncio.run(chat_factory.peek_injected_messages("ut")) == []


def test_peek_worker_timeout_returns_none():
    class SilentProxy:
        def add_handler(self, handler): self.handler = handler
        def remove_handler(self, handler): self.handler = None
        def peek_injected_messages(self, **kwargs):
            return True  # 受理但永不回包
    with patch.object(chat_factory, "_generation_use_worker", return_value=True), \
            patch.object(chat_factory, "peek_worker_proxy", return_value=SilentProxy()):
        assert asyncio.run(chat_factory.peek_injected_messages("ut", timeout=0.05)) is None


@pytest.fixture()
def isolated_storage(tmp_path, monkeypatch):
    from memory import chat_memory
    from memory.identity_store import IdentityStore
    from security.settings import SecuritySettings
    from security.principal import CURRENT
    root = tmp_path / "history_files"
    root.mkdir()
    monkeypatch.setattr(chat_memory, "HISTORY_ROOT", root)
    monkeypatch.setattr(chat_memory, "_session_managers", {})
    store = IdentityStore(SecuritySettings(root / "state/security", require_mfa=False))
    store.prepare()
    admin = store.create_user("admin", "correct horse battery staple 25", "admin", bootstrap=True)
    member = store.create_user("member", "correct horse battery staple 25", actor=admin["id"])
    principal = store.authenticate(store.login("member", "correct horse battery staple 25"))
    token = CURRENT.set(principal)
    try:
        yield root
    finally:
        CURRENT.reset(token)


def test_delete_session_clears_outbox_sidecar(isolated_storage):
    from security.authorization import session as authorize_session
    authorize_session("gone", create=True)
    manager = ChatMemoryManager("gone")
    asyncio.run(manager.upsert_session_outbox({"id": "o1", "kind": "queue", "text": "x"}))
    assert manager._outbox_sidecar_path.exists()
    manager.delete_chat_session_file("gone")
    assert not manager._outbox_sidecar_path.exists()
