"""Two authenticated accounts share a server but never an HTTP data namespace."""
import asyncio
import dataclasses
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app_factory import create_app
from security.principal import CURRENT
from security.settings import SecuritySettings

PASSWORD = "correct horse battery staple 24"


@pytest.fixture
def accounts(tmp_path, monkeypatch):
    from memory import chat_memory, file_memory, file_history, task_template_store
    from prompt import prompt_manager
    history = tmp_path / "history"
    history.mkdir()
    monkeypatch.setattr(chat_memory, "HISTORY_ROOT", history)
    monkeypatch.setattr(chat_memory, "LOCK_ROOT", tmp_path / "locks")
    monkeypatch.setattr(chat_memory, "_session_managers", {})
    monkeypatch.setattr(file_memory, "HISTORY_ROOT", history / "session_files")
    monkeypatch.setattr(file_history, "HISTORY_DIFF_ROOT", history / "session_files")
    monkeypatch.setattr(task_template_store, "ROOT", tmp_path / "templates")
    monkeypatch.setattr(prompt_manager, "MD_FILES_DIR", tmp_path / "prompts")
    from factory.agent_runtime import builtin_tools
    monkeypatch.setattr(builtin_tools, "_BG_LOG_DIR", tmp_path / "commands")
    app = create_app(security_settings=SecuritySettings(tmp_path / "identity", require_mfa=False))
    store = app.state.identity_store
    first = store.create_user("first", PASSWORD, "admin", bootstrap=True)
    second = store.create_user("second", PASSWORD, "admin", actor=first["id"])
    member = store.create_user("member", PASSWORD, actor=first["id"])
    with store.database(True) as db:
        db.execute("UPDATE users SET must_change=0")
    clients = []
    for user in (first, second, member):
        client = TestClient(app, base_url=store.settings.origin)
        response = client.post("/auth/login", json={"username": user["username"], "password": PASSWORD},
                               headers={"Origin": store.settings.origin})
        assert response.status_code == 200
        client.headers.update({"Origin": store.settings.origin, "X-CSRF-Token": response.json()["csrf"]})
        clients.append(client)
    for identifier, user, text in (("chat-a", first, "shared alpha confidential"), ("chat-b", second, "shared beta confidential")):
        store.register(identifier, user["id"])
        directory = history / "users" / user["id"] / "sessions" / identifier
        directory.mkdir(parents=True)
        chat_memory._write_meta_and_entries(directory / (identifier + "_chat.jsonl"), chat_memory._default_meta(identifier), [
            {"event": "chat_round", "question": text, "status": "completed",
             "events": [{"role": "assistant", "content": text}], "timestamp": "2026-10-24 12:00:00"}])
    return app, store, (first, second, member), clients, history


def test_lists_search_and_counts_are_owner_scoped(accounts):
    _, _, _, (first, second, _), _ = accounts
    for client, mine, foreign in ((first, "chat-a", "chat-b"), (second, "chat-b", "chat-a")):
        listing = client.get("/chat_history/summaries").json()
        assert mine in str(listing) and foreign not in str(listing)
        search = client.get("/chat_history/search?q=shared").json()
        assert mine in str(search) and foreign not in str(search)
        assert client.get("/chat_history/file", params={"session_id": foreign}).status_code == 404


def test_groups_same_name_and_archive_mutation_do_not_cross_users(accounts):
    _, _, _, (first, second, _), _ = accounts
    group_a = first.post("/chat_history/groups", json={"name": "same"}).json()
    group_b = second.post("/chat_history/groups", json={"name": "same"}).json()
    # Router wraps group creation in a result envelope.
    group_a = group_a.get("group", group_a)
    group_b = group_b.get("group", group_b)
    assert group_a["id"] != group_b["id"]
    assert first.put("/chat_history/group_assign", json={"session_id": "chat-a", "group_id": group_b["id"]}).status_code == 404
    assert first.delete("/chat_history/groups/" + group_b["id"]).status_code == 404
    assert first.put("/chat_history/archive", json={"session_id": "chat-a", "archived": True}).status_code == 200
    assert second.put("/chat_history/archive", json={"session_id": "chat-b", "archived": True}).status_code == 200
    assert first.get("/chat_history/archives").json()["archived_count"] == 1
    assert first.put("/chat_history/archive", json={"session_id": "chat-a", "archived": False}).status_code == 200
    assert second.get("/chat_history/archives").json()["archived_count"] == 1


def test_private_prompts_and_template_names(accounts):
    _, _, _, (first, second, _), _ = accounts
    for client, text in ((first, "alpha"), (second, "beta")):
        response = client.post("/prompts/create", json={"name": "same", "content": text})
        assert response.status_code == 200, response.text
        assert client.get("/prompts/read?name=same").json()["content"] == text
        response = client.post("/task_templates", json={"name": "same", "prompt": text})
        assert response.status_code == 200, response.text
        assert len(client.get("/task_templates").json()["templates"]) == 1
    assert first.get("/prompts/read?name=same").json()["content"] == "alpha"


def test_profile_is_account_data_and_no_env_paths(accounts):
    _, _, _, (first, second, _), _ = accounts
    assert first.post("/user/profile", json={"name": "私人名字"}).status_code == 200
    profile = first.get("/user/profile").json()
    assert profile["name"] == "私人名字" and "env_file" not in profile
    assert second.get("/user/profile").json()["name"] != "私人名字"


def test_stray_root_files_are_invisible_to_all_accounts(accounts):
    _, store, _, (first, second, _), history = accounts
    (history / "legacy_chat.jsonl").write_text('{"_meta":{}}\n', encoding="utf-8")
    assert "legacy" not in str(first.get("/chat_history/summaries").json())
    assert first.get("/chat_history/file", params={"session_id": "legacy"}).status_code == 404
    assert store.owner("legacy") is None
    assert first.post("/chat_history/ensure_session", json={"session_id": "fresh"}).status_code == 200
    assert second.post("/chat_history/ensure_session", json={"session_id": "fresh"}).status_code == 404
    assert not list((history / "users").rglob("fresh_chat.jsonl"))


def test_mixed_batch_export_and_state_controls_are_denied(accounts):
    _, _, _, (first, _, member), _ = accounts
    assert first.get("/chat_history/export_zip?session_ids=chat-a,chat-b").status_code == 404
    assert first.post("/stop_chat?session_id=chat-b").status_code == 404
    assert member.post("/chat_config/permissions", json={"mode": "full_control"}).status_code == 403
    assert member.post("/tools/diagnostics", json={"server_id": "anything", "action": "call", "tool_name": "work"}).status_code == 403


def test_stale_account_tab_cannot_load_the_new_cookie_accounts_data(accounts):
    _,_,users,(first,second,_),_=accounts
    response=second.get('/chat_history/summaries',headers={'X-Ytools-Expected-Account':users[0]['id']})
    assert response.status_code==409 and response.headers.get('X-Ytools-Account-Changed')=='true'
    assert 'shared beta confidential' not in response.text and 'chat-b' not in response.text
    assert second.get('/chat_history/summaries',headers={'X-Ytools-Expected-Account':users[1]['id']}).status_code==200
    assert second.post('/auth/logout',headers={'X-Ytools-Expected-Account':users[0]['id']}).status_code==409
    assert second.get('/auth/me').status_code==200


def test_new_password_gate_and_account_page_bootstrap(accounts):
    app, store, users, (first, _, _), _ = accounts
    new_user = first.post("/admin/users", json={"username": "new-member", "password": PASSWORD, "role": "member"})
    assert new_user.status_code == 200
    client = TestClient(app, base_url=store.settings.origin)
    response = client.post("/auth/login", json={"username": "new-member", "password": PASSWORD}, headers={"Origin": store.settings.origin})
    assert response.json()["user"]["password_change_required"]
    assert client.get("/chat_history/summaries").status_code == 403
    page = client.get("/ui/", follow_redirects=False)
    assert page.status_code == 303 and page.headers["location"] == "/ui/account.html"
    account = client.get("/ui/account.html")
    assert account.status_code == 200 and 'id="account-context"' in account.text
    editor = client.get("/ui/editor.html?session_id=chat-a", follow_redirects=False)
    assert editor.status_code == 303 and editor.headers["location"] == "/ui/account.html"


def test_file_editor_has_private_identity_bootstrap_and_owned_apis(accounts):
    app, store, users, (first, second, _), _ = accounts
    for client, user in ((first, users[0]), (second, users[1])):
        response = client.get("/ui/editor.html?session_id=chat-a&key=example")
        assert response.status_code == 200
        assert 'id="account-context"' in response.text
        assert user['id'] in response.text and 'private, no-store' in response.headers['cache-control']
        assert response.text.index('id="account-context"') < response.text.index('js/security_client.js') < response.text.index('js/theme.js')
    anonymous = TestClient(app, base_url=store.settings.origin)
    response = anonymous.get('/ui/editor.html?session_id=chat-a', follow_redirects=False)
    assert response.status_code == 303 and response.headers['location'] == '/ui/login.html'
    assert first.get('/file_diff/list?session_id=chat-b').status_code == 404
    assert first.get('/ui/favicon-editor.png').status_code == 200


def test_static_mount_does_not_publish_dev_directories(accounts):
    _, _, _, (first, _, _), _ = accounts
    for path in ("/ui/package.json", "/ui/node_modules/playwright/package.json", "/ui/test_h5/api.test.js"):
        assert first.get(path).status_code == 404
    assert first.get("/ui/login.js").status_code == 200


def test_signed_preview_grants_are_bound_to_login(accounts):
    _, store, users, _, _ = accounts
    from routers.permissions_auth import ticket, verify_ticket
    cookie_a = store.login("first", PASSWORD)
    cookie_b = store.login("first", PASSWORD)
    token = CURRENT.set(store.authenticate(cookie_a))
    try:
        grant = ticket("chat-a", "/temporary/example.png")
        assert verify_ticket(grant, "chat-a", "/temporary/example.png")
    finally:
        CURRENT.reset(token)
    token = CURRENT.set(store.authenticate(cookie_b))
    try:
        assert not verify_ticket(grant, "chat-a", "/temporary/example.png")
    finally:
        CURRENT.reset(token)


def test_worker_identity_comes_from_parent_and_disable_stops_tool_boundary(accounts, monkeypatch):
    _, store, users, _, _ = accounts
    from factory import session_worker
    from security.authorization import execution_active
    seen = []
    async def generate(*args):
        from security.principal import current
        seen.append(current().user_id)
        assert execution_active("chat-b")
        store.change_user(users[0]["id"], users[1]["id"], enabled=False)
        current().resource_cache.pop("execution_lease", None)
        assert not execution_active("chat-b")
    monkeypatch.setattr(session_worker, "_worker_generate_authorized", generate)
    asyncio.run(session_worker._worker_generate("chat-b", {}, object(), security_identity={"directory": str(store.root), "user_id": users[1]["id"]}))
    assert seen == [users[1]["id"]] and CURRENT.get() is None


def test_all_application_routes_require_a_reviewed_policy(accounts):
    from fastapi.routing import APIRoute
    from security.middleware import KNOWN_AUTH
    from security.route_policy import LEGACY_ROUTES
    app, *_ = accounts
    from security.routes import effective_routes
    actual = {(method, route.path) for route in effective_routes(app)
              if isinstance(getattr(route, "original_route", route), APIRoute) for method in route.methods}
    assert actual <= LEGACY_ROUTES | KNOWN_AUTH, actual - LEGACY_ROUTES - KNOWN_AUTH


def test_admin_limits_and_disable_revoke_without_cross_user_effect(accounts):
    _, store, users, (first, second, member), _ = accounts
    path = "/admin/users/" + users[2]["id"] + "/limits"
    assert member.get(path).status_code == 403
    assert first.put(path, json={"daily_calls": 3, "daily_tokens": 10000}).status_code == 200
    assert member.get("/user/usage").json()["limits"]["daily_calls"] == 3
    assert first.post("/admin/users/" + users[2]["id"] + "/password", json={"password": "short"}).status_code == 422
    assert first.patch("/admin/users/" + users[2]["id"], json={"enabled": False}).status_code == 200
    assert member.get("/auth/me").status_code == 401
    assert second.get("/auth/me").status_code == 200


def test_member_model_headers_are_redacted_but_conversation_bytes_are_preserved(accounts):
    _, store, users, _, _ = accounts
    from security.redaction import for_member, history
    token = CURRENT.set(store.authenticate(store.login("member", PASSWORD)))
    try:
        payload = {"role_info": {"effective_headers": [{"name": "Authorization", "value": "private"}], "selection": {"headers": [{"value": "private"}], "model": "M"}}}
        assert "private" not in json.dumps(for_member(payload))
        text = json.dumps({"_meta": {"model_selection": payload}}) + '\n{"event":"chat_round","content":"keep Authorization: literal"}\n'
        cleaned = history(text)
        assert "private" not in cleaned
        assert cleaned.partition("\n")[2] == text.partition("\n")[2]
    finally:
        CURRENT.reset(token)


def test_plain_member_chat_forces_empty_tools_at_real_route(accounts, monkeypatch):
    from routers import chat_router
    from security.principal import current
    _, _, users, (_, _, member), _ = accounts
    captured = []
    async def generate(request):
        captured.append((request.tool_names, current().user_id))
        yield 'data: {"content":"example"}\n\ndata: [DONE]\n\n'
    monkeypatch.setattr(chat_router, "tool_chat_server", generate)
    response = member.post("/chat_with_tool", json={"session_id": "plain-chat", "messages": [{"role": "user", "content": "hello"}]})
    assert response.status_code == 200 and "example" in response.text
    assert captured == [([], users[2]["id"])]
    response = member.post("/chat_with_tool", json={"session_id": "plain-chat", "tool_names": ["run_command"], "messages": []})
    assert response.status_code == 403 and len(captured) == 1


def test_plain_member_worker_cannot_cross_any_tool_boundary(accounts):
    from security.authorization import tool_boundary, execution_active
    _, store, users, _, _ = accounts
    store.register("plain-chat", users[2]["id"])
    token = CURRENT.set(store.authenticate(store.login("member", PASSWORD)))
    try:
        assert execution_active("plain-chat")
        with pytest.raises(PermissionError):
            tool_boundary("run_command", {}, {"session": "plain-chat"})
    finally:
        CURRENT.reset(token)
