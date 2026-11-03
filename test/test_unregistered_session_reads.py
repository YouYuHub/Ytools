"""建档瞬间读宽限：未登记会话 id 的只读查询直通空数据，不 404。

回归：新会话首次发送时前端先发 GET /file_diff/list 与
GET /chat_context/token_stats（读按设计不预注册），中间件曾对未登记
id 直接 404，控制台产生两条噪音。现宽限这两个只读端点：
- 未登记 id → 直通，端点返回空/零值（不认领、不建档）；
- 他人会话 → 仍 404；写请求 → 仍 404；其他读路径 → 仍 404。
"""
import asyncio
import json

import pytest
from fastapi import HTTPException

from memory.identity_store import IdentityStore
from security.principal import CURRENT
from security.settings import SecuritySettings

PASSWORD = "correct horse battery staple 25"
FRESH = "yt-fresh-never-registered"


@pytest.fixture
def storage(tmp_path, monkeypatch):
    from memory import chat_memory, file_memory, file_history
    root = tmp_path / "history_files"
    root.mkdir()
    monkeypatch.setattr(chat_memory, "HISTORY_ROOT", root)
    monkeypatch.setattr(chat_memory, "UPLOAD_HISTORY_ROOT", root / "session_files")
    monkeypatch.setattr(chat_memory, "LOCK_ROOT", root / "lock")
    monkeypatch.setattr(chat_memory, "_session_managers", {})
    monkeypatch.setattr(file_memory, "HISTORY_ROOT", root / "session_files")
    monkeypatch.setattr(file_history, "HISTORY_DIFF_ROOT", root / "session_files")
    store = IdentityStore(SecuritySettings(root / "state/security", require_mfa=False))
    store.prepare()
    admin = store.create_user("admin", PASSWORD, "admin", bootstrap=True)
    member = store.create_user("member", PASSWORD, actor=admin["id"])
    principal = store.authenticate(store.login("member", PASSWORD))
    token = CURRENT.set(principal)
    try:
        yield tmp_path, root, store, admin, member, principal
    finally:
        CURRENT.reset(token)


def authorize(storage, path, method, sid):
    """直调中间件鉴权（与真实 HTTP 相同的 session 门禁，不经过端点）。"""
    from security.middleware import SecurityMiddleware
    _, _, store, _, _, principal = storage
    gate = SecurityMiddleware(None, store=store, application=None)
    gate._authorize(principal, path, method, path, {}, {"session_id": [sid]}, {})


def test_allow_unregistered_does_not_claim(storage):
    from security.authorization import session
    _, _, store, _, _, _ = storage
    assert store.owner(FRESH) is None
    assert session(FRESH, allow_unregistered=True) == FRESH
    assert store.owner(FRESH) is None  # 宽限≠认领：读后仍未登记


def test_allow_unregistered_other_owned_still_404(storage):
    from security.authorization import session
    _, _, store, admin, _, _ = storage
    store.register("owned-by-admin", admin["id"])
    with pytest.raises(HTTPException) as exc:
        session("owned-by-admin", allow_unregistered=True)
    assert exc.value.status_code == 404


def test_create_still_registers(storage):
    from security.authorization import session
    _, _, store, _, _, _ = storage
    session("fresh-to-create", create=True)
    assert store.owner("fresh-to-create") is not None


def test_is_session_registered(storage):
    from security.authorization import is_session_registered
    _, _, store, _, member, _ = storage
    assert is_session_registered(FRESH) is False
    store.register("mine", member["id"])
    assert is_session_registered("mine") is True
    token = CURRENT.set(None)
    try:
        assert is_session_registered(FRESH) is None  # 内部调用沿旧路径
    finally:
        CURRENT.reset(token)


def test_middleware_passes_fresh_reads(storage):
    authorize(storage, "/file_diff/list", "GET", FRESH)
    authorize(storage, "/chat_context/token_stats", "GET", FRESH)
    _, _, store, _, _, _ = storage
    assert store.owner(FRESH) is None  # 中间件直通也不建档


def test_middleware_still_strict(storage):
    _, _, store, admin, _, _ = storage
    store.register("owned-by-admin", admin["id"])
    # 他人会话：宽限端点也 404
    with pytest.raises(HTTPException) as exc:
        authorize(storage, "/file_diff/list", "GET", "owned-by-admin")
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException) as exc:
        authorize(storage, "/chat_context/token_stats", "GET", "owned-by-admin")
    assert exc.value.status_code == 404
    # 写请求：未登记 id 仍 404
    with pytest.raises(HTTPException) as exc:
        authorize(storage, "/chat_history/title", "POST", FRESH)
    assert exc.value.status_code == 404
    # 非宽限读路径：未登记 id 仍 404
    with pytest.raises(HTTPException) as exc:
        authorize(storage, "/chat_history/file", "GET", FRESH)
    assert exc.value.status_code == 404


def test_fresh_list_returns_empty(storage):
    from routers.file_history_router import list_changed_files
    authorize(storage, "/file_diff/list", "GET", FRESH)
    response = asyncio.run(list_changed_files(FRESH))
    assert response.status_code == 200
    body = json.loads(response.body.decode("utf-8"))
    assert body["files"] == []
    assert body["stats"] == {"total": 0, "added": 0, "removed": 0}


def test_fresh_token_stats_returns_zero(storage):
    from routers.chat_router import get_chat_context_token_stats
    authorize(storage, "/chat_context/token_stats", "GET", FRESH)
    response = asyncio.run(get_chat_context_token_stats(
        session_id=FRESH, include_tools=True, tool_names=["read_file"],
        round_offset=0, round_limit=40))
    assert response.status_code == 200
    body = json.loads(response.body.decode("utf-8"))
    assert body["exists"] is False
    assert body["request_context_tokens"] == 0


def test_registered_empty_session_reads(storage):
    from routers.file_history_router import list_changed_files
    from routers.chat_router import get_chat_context_token_stats
    from memory.file_history import list_files
    _, _, store, _, member, _ = storage
    store.register("mine-empty", member["id"])
    assert list_files("mine-empty") == []
    response = asyncio.run(list_changed_files("mine-empty"))
    assert response.status_code == 200
    response = asyncio.run(get_chat_context_token_stats(session_id="mine-empty"))
    assert response.status_code == 200
    assert json.loads(response.body.decode("utf-8"))["exists"] is False
