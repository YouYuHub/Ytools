"""Account/CSRF/default-deny tests without importing main or launching MCP."""
import dataclasses
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from security.app import install_security
from security.settings import SecuritySettings
from security.principal import current

PASSWORD = "correct horse battery staple 24"


@pytest.fixture
def secured(tmp_path, monkeypatch):
    from memory import chat_memory, file_memory
    monkeypatch.setattr(chat_memory, "HISTORY_ROOT", tmp_path / "history")
    monkeypatch.setattr(file_memory, "HISTORY_ROOT", tmp_path / "uploads")
    settings = SecuritySettings(tmp_path / "identity", require_mfa=False, body_bytes=4096)
    app = FastAPI()
    @app.get("/ui/account.html")
    @app.get("/ui/index.html")
    def page():
        from fastapi.responses import HTMLResponse
        return HTMLResponse("<main>fixture account</main>")
    @app.get("/chat_history/file")
    def file(session_id: str):
        return {"session": session_id, "actor": current().user_id}
    @app.post("/chat_with_tool")
    def create():
        return {"ok": True}
    @app.get("/unreviewed_new_route")
    def unreviewed():
        pytest.fail("an unreviewed route must never execute")
    @app.get("/file/get_session_document")
    def document(kind: str = "html", session_id: str = "s"):
        from fastapi.responses import Response
        return Response("<p>example</p>", media_type="application/pdf" if kind == "pdf" else "text/html")
    @app.get("/tools/list")
    def tools(refresh: bool = False):
        assert not refresh, "ordinary users must never trigger forced tool discovery"
        return {"tools": []}
    @app.post("/chat_config/models/select")
    def write_global():
        pytest.fail("query session ID must not turn a global body into a session update")
    store = install_security(app, settings)
    admin = store.create_user("admin", PASSWORD, "admin", bootstrap=True)
    member = store.create_user("member", PASSWORD, actor=admin["id"])
    with store.database(True) as db:
        db.execute("UPDATE users SET must_change=0 WHERE id=?", (member["id"],))
    client = TestClient(app, base_url=settings.origin)
    return client, store, admin, member


def login(client, name="admin"):
    response = client.post("/auth/login", json={"username": name, "password": PASSWORD},
                           headers={"Origin": "http://127.0.0.1:48621"})
    assert response.status_code == 200, response.text
    return {"Origin": "http://127.0.0.1:48621", "X-CSRF-Token": response.json()["csrf"]}


def test_anonymous_is_denied_before_business_logic(secured):
    client, _, _, _ = secured
    assert client.get("/chat_history/file?session_id=anything").status_code == 401
    assert client.get("/openapi.json").status_code == 401
    assert client.get("/auth/status").json()["registration"] is False
    assert client.post("/auth/register", json={}, headers={"Origin": client.base_url.__str__().rstrip("/")}).status_code == 401


def test_login_requires_origin_and_cookie_is_http_only(secured):
    client, _, _, _ = secured
    assert client.post("/auth/login", json={"username": "admin", "password": PASSWORD}).status_code == 403
    response = client.post("/auth/login", json={"username": "admin", "password": PASSWORD}, headers={"Origin": "http://evil.invalid"})
    assert response.status_code == 403
    headers = login(client)
    assert client.get("/auth/me").json()["user"]["username"] == "admin"
    assert client.post("/auth/logout").status_code == 403
    assert client.post("/auth/logout", headers={"Origin": headers["Origin"]}).status_code == 403
    assert client.post("/auth/logout", headers=headers).status_code == 200
    assert client.get("/auth/me").status_code == 401


def test_non_ascii_csrf_is_rejected_without_server_error_or_logout(secured):
    client, _, _, _ = secured
    login(client)
    response = client.post('/auth/logout', headers={b'Origin': b'http://127.0.0.1:48621', b'X-CSRF-Token': b'\xff'})
    assert response.status_code == 403 and response.headers.get('X-CSRF-Required') == 'true'
    assert client.get('/auth/me').status_code == 200


@pytest.mark.parametrize('host', ['[invalid', 'localhost/not-a-host', 'someone@localhost'])
def test_malformed_host_is_a_client_error(secured, host):
    client, _, _, _ = secured
    assert client.get('/auth/status', headers={'Host': host}).status_code == 400


def test_malformed_referer_is_a_client_error(secured):
    client, _, _, _ = secured
    assert client.post('/auth/login', json={}, headers={'Referer': 'http://[invalid'}).status_code == 403


def test_oversized_length_is_a_client_error(secured):
    client, _, _, _ = secured
    assert client.post('/auth/login', json={}, headers={
        'Origin': 'http://127.0.0.1:48621', 'Content-Length': '9' * 5000}).status_code == 413


def test_deep_json_is_rejected_without_server_error(secured):
    client, _, _, _ = secured
    response = client.post('/auth/login', content='{"nested":' + '[' * 1500 + '0' + ']' * 1500 + '}', headers={
        'Origin': 'http://127.0.0.1:48621', 'Content-Type': 'application/json'})
    assert response.status_code == 422


@pytest.mark.parametrize('value', ['NaN', 'Infinity', '-Infinity', '1e9999'])
def test_non_finite_json_values_are_client_errors(secured, value):
    client, _, _, _ = secured
    assert client.post('/auth/login', content='{"username":' + value + ',"password":"sample"}', headers={
        'Origin': 'http://127.0.0.1:48621', 'Content-Type': 'application/json'}).status_code == 422


def test_long_bracket_text_is_not_treated_as_nested_json(secured):
    client, _, _, _ = secured
    headers = login(client)
    assert client.post('/chat_with_tool', json={'session_id': 'bracket-prompt', 'temperature': 1.25,
        'messages': [{'role': 'user', 'content': '[' * 1500 + ']' * 1500}]}, headers=headers).status_code == 200


def test_identity_is_not_a_chat_id_and_other_owner_is_denied(secured):
    client, store, admin, member = secured
    store.register("session", "private-a", admin["id"])
    login(client, "member")
    assert client.get("/chat_history/file?session_id=private-a").status_code == 404
    assert client.get("/chat_history/file?session_id=unknown").status_code == 404
    store.register("session", "private-b", member["id"])
    assert client.get("/chat_history/file?session_id=private-b").json()["actor"] == member["id"]
    assert client.get("/chat_history/file?session_id=private-a", headers={"Authorization": "Bearer private-a"}).status_code == 404


def test_members_cannot_elevate_or_execute_and_admin_can_register_new(secured):
    client, store, admin, _ = secured
    headers = login(client, "member")
    assert client.get("/admin/users").status_code == 403
    assert client.post("/chat_with_tool", json={"session_id": "member-new", "tool_names": ["run_command"]}, headers=headers).status_code == 403
    assert client.post("/chat_with_tool", json={"session_id": "member-new", "tool_names": []}, headers=headers).status_code == 200
    headers = login(client)
    assert client.post("/chat_with_tool", json={"session_id": "admin-new"}, headers=headers).status_code == 200
    assert store.owner("session", "admin-new") == admin["id"]


def test_unknown_routes_fail_closed_and_bad_hosts_are_rejected(secured):
    client, _, _, _ = secured
    login(client)
    assert client.get("/unreviewed_new_route").status_code == 404
    assert client.get("/auth/me", headers={"Host": "attacker.invalid"}).status_code == 400
    assert client.get("/auth/me", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403


def test_size_cap_is_before_json_parse(secured):
    client, _, _, _ = secured
    headers = login(client)
    response = client.post("/chat_with_tool", content=b"x" * 4097,
                           headers={**headers, "Content-Type": "application/json"})
    assert response.status_code == 413


def test_internal_fields_cannot_be_spoofed(secured):
    client, _, _, _ = secured
    headers = login(client)
    response = client.post("/chat_with_tool", json={"session_id": "new", "usage_role": "title"}, headers=headers)
    assert response.status_code == 422


def test_missing_database_does_not_create_anonymous_instance(secured):
    client, store, _, _ = secured
    login(client)
    store.path.unlink()
    response = client.get("/auth/me")
    assert response.status_code == 503
    assert not store.path.exists()


def test_security_headers_and_no_cache(secured):
    client, _, _, _ = secured
    login(client)
    response = client.get("/auth/me")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "no-store" in response.headers["Cache-Control"]
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]


def test_only_authenticated_account_panel_allows_same_origin_framing(secured):
    client, *_ = secured
    token = "a" * 32
    anonymous = client.get("/ui/account.html?panel=" + token, follow_redirects=False)
    assert anonymous.status_code == 303 and anonymous.headers["X-Frame-Options"] == "DENY"
    login(client, "member")
    panel = client.get("/ui/account.html?panel=" + token)
    assert panel.status_code == 200
    assert panel.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert "frame-ancestors 'self'" in panel.headers["Content-Security-Policy"]
    assert "no-store" in panel.headers["Cache-Control"]
    for path in ("/ui/account.html", "/ui/account.html?panel=invalid", "/ui/index.html?panel=" + token, "/auth/me?panel=" + token):
        response = client.get(path)
        assert response.headers["X-Frame-Options"] == "DENY"
        assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    foreign = client.get("/ui/account.html?panel=" + token, headers={"Sec-Fetch-Site":"cross-site"})
    assert foreign.status_code == 403 and foreign.headers["X-Frame-Options"] == "DENY"


def test_mfa_pending_cannot_reach_business_or_admin(secured):
    client, store, _, _ = secured
    store.settings = dataclasses.replace(store.settings, require_mfa=True)
    headers = login(client)
    assert client.get("/auth/me").json()["mfa_verified"] is False
    assert client.get("/admin/users").status_code == 403
    assert client.get("/chat_history/file?session_id=x").status_code == 403
    assert client.post("/auth/mfa/start", json={"password": PASSWORD}, headers=headers).status_code == 200


def test_conflicting_body_and_duplicate_query_cannot_bypass_owner_check(secured):
    client, store, admin, member = secured
    store.register("session", "mine", admin["id"])
    store.register("session", "theirs", member["id"])
    headers = login(client)
    assert client.get("/chat_history/file?session_id=mine&session_id=theirs").status_code == 404
    assert client.post("/chat_with_tool?session_id=theirs", json={"session_id": "mine"}, headers=headers).status_code == 404


@pytest.mark.parametrize("content_type", ["application/JSON", "application/vendor+json", ""])
def test_alternate_json_headers_cannot_hide_body_ownership(secured, content_type):
    client, store, admin, member = secured
    store.register("session", "mine", admin["id"])
    store.register("session", "theirs", member["id"])
    headers = login(client)
    if content_type:
        headers["Content-Type"] = content_type
    response = client.post("/chat_with_tool?session_id=mine", content=json.dumps({"session_id": "theirs"}), headers=headers)
    assert response.status_code == 404


def test_empty_json_logout_is_valid_and_raw_json_disguise_is_rejected(secured):
    client, _, _, _ = secured
    headers = login(client)
    response = client.post("/chat_with_tool?session_id=x", content=b'{"session_id":"x"}', headers={**headers, "Content-Type": "application/octet-stream"})
    assert response.status_code == 415
    assert client.post("/auth/logout_all", headers={**headers, "Content-Type": "application/json"}).status_code == 200


def test_query_scope_cannot_authorize_global_config_body(secured):
    client, store, _, member = secured
    store.register("session", "mine", member["id"])
    headers = login(client, "member")
    for path in ("/chat_config/models/select",):
        assert client.post(path + "?session_id=mine", json={}, headers=headers).status_code == 403


@pytest.mark.parametrize("query", ["refresh=True", "refresh=on", "refresh=YES", "refresh=false&refresh=true"])
def test_force_tool_refresh_uses_framework_boolean_semantics(secured, query):
    client, *_ = secured
    login(client, "member")
    assert client.get("/tools/list?" + query).status_code == 403


def test_active_content_is_downloaded_and_pdf_keeps_same_origin_preview(secured):
    client, store, admin, _ = secured
    store.register("session", "s", admin["id"])
    login(client)
    html = client.get("/file/get_session_document?session_id=s")
    assert html.headers["Content-Disposition"] == "attachment"
    assert "sandbox" in html.headers["Content-Security-Policy"]
    pdf = client.get("/file/get_session_document?session_id=s&kind=pdf")
    assert pdf.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert "sandbox" in pdf.headers["Content-Security-Policy"]


def test_production_cookie_is_secure_host_only(secured):
    client, store, _, _ = secured
    store.settings = dataclasses.replace(store.settings, production=True, origin="https://chat.example.test")
    from fastapi.testclient import TestClient
    browser = TestClient(client.app, base_url=store.settings.origin)
    response = browser.post("/auth/login", json={"username": "admin", "password": PASSWORD}, headers={"Origin": store.settings.origin})
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("__Host-ytools_session=") and "Secure" in cookie and "HttpOnly" in cookie
    assert "Domain=" not in cookie


def test_revoked_login_closes_sse_without_waiting_for_upstream(secured, monkeypatch):
    import asyncio
    from fastapi.responses import StreamingResponse
    client, store, _, _ = secured
    closed = []
    async def events():
        try:
            yield "data: started\n\n"
            await asyncio.sleep(30)
            yield "data: too-late\n\n"
        finally:
            closed.append(True)
    client.app.add_api_route("/chat_stream/status", lambda: StreamingResponse(events(), media_type="text/event-stream"))
    login(client)
    monkeypatch.setattr(store, "live", lambda _: False)
    response = client.get("/chat_stream/status")
    assert "Login expired or revoked" in response.text and "too-late" not in response.text and closed
