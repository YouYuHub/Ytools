"""Personal settings: isolated accounts, files and HTTP; never real credentials."""
import contextlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import env_manager
from memory.identity_store import IdentityStore
from security.app import install_security
from security.principal import CURRENT, Principal
from security.settings import SecuritySettings
from security import preferences

PASSWORD = "account preference fixture password 2026"


@pytest.fixture
def accounts(tmp_path, monkeypatch):
    from memory import chat_memory, file_memory
    monkeypatch.setattr(chat_memory, "HISTORY_ROOT", tmp_path / "history")
    monkeypatch.setattr(file_memory, "HISTORY_ROOT", tmp_path / "uploads")
    settings = tmp_path / "config"
    settings.mkdir()
    (settings / ".env").write_text("NETWORK_RETRY_MAX_ATTEMPTS=9\n", encoding="utf-8")
    env_manager.init_path(settings)
    app = FastAPI()
    from routers.chat_config_router import api_chat_config_router
    from routers.speech_config_router import api_speech_config_router
    from routers.tool_approval_router import api_permission_router as permissions_router
    from routers.task_cost_router import api_task_cost_router as costs_router
    for router in (api_chat_config_router, api_speech_config_router, permissions_router, costs_router):
        app.include_router(router)
    store = install_security(app, SecuritySettings(tmp_path / "identity", require_mfa=False))
    admin = store.create_user("admin", PASSWORD, "admin", bootstrap=True)
    alice = store.create_user("alice", PASSWORD, actor=admin["id"])
    bob = store.create_user("bob", PASSWORD, actor=admin["id"])
    with store.database(True) as db:
        db.execute("UPDATE users SET must_change=0")
    return app, store, admin, alice, bob


@contextlib.contextmanager
def identity(store, user):
    principal = Principal(user["id"], user["username"], user["role"], "", 0, True, "", store)
    token = CURRENT.set(principal)
    try:
        yield principal
    finally:
        CURRENT.reset(token)


def client_for(app, name):
    client = TestClient(app, base_url="http://127.0.0.1:48621")
    response = client.post("/auth/login", json={"username": name, "password": PASSWORD},
                           headers={"Origin": str(client.base_url).rstrip("/")})
    assert response.status_code == 200, response.text
    client.headers.update({"Origin": str(client.base_url).rstrip("/"), "X-CSRF-Token": response.json()["csrf"]})
    return client


def test_ui_persists_on_server_logout_login_and_new_browser(accounts):
    app, store, _, alice, _ = accounts
    first = client_for(app, "alice")
    values = {"theme": "light", "accent": "pink", "task_status_visible": False,
              "sidebar_width": 400, "groups_expanded": True}
    assert first.patch("/user/preferences", json={"ui": values}).json()["ui"] == values
    assert first.post("/auth/logout").status_code == 200
    assert first.get("/user/preferences").status_code == 401
    second = client_for(app, "alice")
    assert second.get("/auth/me").json()["preferences"]["ui"] == values
    assert client_for(app, "bob").get("/user/preferences").json()["ui"] == {}
    reopened = IdentityStore(store.settings)
    reopened.prepare()
    with identity(reopened, alice):
        assert preferences.snapshot()["ui"] == values


@pytest.mark.parametrize("bad", [{"theme": []}, {"accent": {}}, {"theme": "oops"},
    {"task_status_visible": 0}, {"sidebar_width": 219}, {"sidebar_width": True},
    {"groups_expanded": "yes"}, {"role": "admin"}, {"runtime": {"MODEL_API_KEY": "no"}}])
def test_ui_rejects_invalid_and_sensitive_fields(accounts, bad):
    client = client_for(accounts[0], "alice")
    assert client.patch("/user/preferences", json={"ui": bad}).status_code == 422
    assert client.get("/user/preferences").json()["ui"] == {}


def test_http_boundary_csrf_expected_account_and_seed_merge(accounts):
    app, store, _, alice, bob = accounts
    anon = TestClient(app, base_url="http://127.0.0.1:48621", headers={"Origin": "http://127.0.0.1:48621"})
    assert anon.get("/user/preferences").status_code == 401
    assert anon.patch("/user/preferences", json={"ui": {}}).status_code == 401
    client = client_for(app, "alice")
    assert client.patch("/user/preferences", json={"ui": {"theme": "dark"}},
                        headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert client.patch("/user/preferences", json={"ui": {"theme": "dark"}},
                        headers={"X-Ytools-Expected-Account": bob["id"]}).status_code == 409
    assert client.patch("/user/preferences", json={"ui": {"theme": "dark"}}).status_code == 200
    result = client.patch("/user/preferences", json={"ui": {"theme": "light", "accent": "green"}, "only_if_unset": True})
    assert result.json()["ui"] == {"theme": "dark", "accent": "green"}
    assert client.patch("/user/preferences", json={"ui": {}, "only_if_unset": "false"}).status_code == 422


def test_parallel_patch_merges_fields_and_runtime_snapshot_is_per_generation(accounts):
    _, store, _, alice, bob = accounts
    def change(field, value):
        with identity(store, alice):
            preferences.update("ui", {field: value})
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda pair: change(*pair), [("theme", "light"), ("accent", "pink")]))
    with identity(store, alice) as generation:
        assert preferences.snapshot()["ui"] == {"theme": "light", "accent": "pink"}
        preferences.set_runtime({"NETWORK_RETRY_MAX_ATTEMPTS": 3})
        assert env_manager.load_var("NETWORK_RETRY_MAX_ATTEMPTS") == "3"
        with identity(store, alice):
            preferences.set_runtime({"NETWORK_RETRY_MAX_ATTEMPTS": 7})
        assert env_manager.load_var("NETWORK_RETRY_MAX_ATTEMPTS") == "3"
    with identity(store, alice):
        assert env_manager.load_var("NETWORK_RETRY_MAX_ATTEMPTS") == "7"
    with identity(store, bob):
        assert preferences.snapshot()["ui"] == {}
        assert env_manager.load_var("NETWORK_RETRY_MAX_ATTEMPTS", "instance") == "9"


def test_personal_chat_settings_never_write_global_files(accounts):
    app, store, admin, alice, bob = accounts
    file = env_manager.get_env_file_path()
    before = Path(file).read_bytes()
    global_values = dict(env_manager.env_vars)
    client = client_for(app, "alice")
    for path, payload in [
        ("context_return", {"reasoning_max_length": 121, "tool_result_max_length": 122}),
        ("mcp_tools", {"call_timeout_seconds": 23}),
        ("network_retry", {"max_attempts": 4}),
        ("compaction_retry", {"max_attempts": 5}),
        ("video_read_limit", {"max_seconds": 72}),
        ("tool_concurrency", {"mcp_tool_workers": 3, "sub_agent_max_concurrent": 2}),
        ("sub_agent_limits", {"max_rounds": 7, "timeout_seconds": 88}),
        ("sub_agent_retry", {"final_reply_max_attempts": 4, "stream_error_max_attempts": 3, "todo_remind_max": 2}),
        ("speech_input", {"language": "ja-JP"}),
    ]:
        response = client.post("/chat_config/" + path, json=payload)
        assert response.status_code == 200, (path, response.text)
    assert client.get("/chat_config/speech_input").json()["source"] == "account"
    from memory import file_memory
    with identity(store, alice):
        assert env_manager.load_var("NETWORK_RETRY_MAX_ATTEMPTS") == "4"
        assert file_memory.video_max_read_seconds() == 72
        assert preferences.runtime_value("SPEECH_INPUT_LANGUAGE") == "ja-JP"
        with pytest.raises(ValueError):
            env_manager.set_env_vars({"NETWORK_RETRY_MAX_ATTEMPTS": 3, "MODEL_API_KEY": "forbidden"})
    with identity(store, bob):
        assert preferences.runtime_value("NETWORK_RETRY_MAX_ATTEMPTS") is None
        assert preferences.runtime_value("SPEECH_INPUT_LANGUAGE") is None
    assert Path(file).read_bytes() == before
    assert env_manager.env_vars == global_values
    assert client.post("/chat_config/models/select", json={}).status_code == 403
    assert client.post("/task_costs/pricing", json={}).status_code == 403
    assert client.post("/chat_config/permissions", json={"mode": "full_control"}).status_code == 403


def test_account_budget_permissions_and_username_use_fixed_id(accounts):
    app, store, admin, alice, bob = accounts
    from memory import permission_store, task_cost_store
    global_policy = permission_store.policy()
    global_limits = task_cost_store.config()
    with identity(store, admin):
        permission_store.set_policy("ask")
        task_cost_store.save_config({"tokens": 123})
        assert permission_store.policy()["mode"] == "ask"
        assert task_cost_store.config()["limits"]["tokens"] == 123
    with identity(store, bob):
        assert permission_store.policy() == global_policy
        assert task_cost_store.config()["limits"] == global_limits["limits"]
    client = client_for(app, "alice")
    result = client.post("/task_costs/config", json={"limits": {"tokens": 456}})
    assert result.status_code == 200, result.text
    assert client.get("/task_costs/config").json()["limits"]["tokens"] == 456
    assert task_cost_store.config() == global_limits
    assert permission_store.policy() == global_policy
    with identity(store, alice):
        preferences.update("ui", {"theme": "light"})
    # Renaming a label cannot change the storage key; username-change tests cover the guarded transaction.
    with identity(store, {**alice, "username": "renamed"}):
        assert preferences.snapshot()["ui"]["theme"] == "light"


def test_existing_unlimited_and_clamped_values_keep_their_semantics(accounts):
    app, store, _, alice, _ = accounts
    client = client_for(app, "alice")
    assert client.post("/chat_config/mcp_tools", json={"call_timeout_seconds": 0}).status_code == 200
    assert client.post("/chat_config/network_retry", json={"max_attempts": -1}).status_code == 200
    assert client.post("/chat_config/video_read_limit", json={"max_seconds": -1}).status_code == 200
    from memory.file_memory import video_max_read_seconds
    with identity(store, alice):
        assert float(env_manager.load_var("MCP_TOOL_CALL_TIMEOUT_SECONDS")) == 0
        assert env_manager.load_var("NETWORK_RETRY_MAX_ATTEMPTS") == "-1"
        assert video_max_read_seconds() == 5


def test_compaction_and_worker_consumers_read_account_settings(accounts, monkeypatch):
    app, store, _, alice, bob = accounts
    from routers import chat_config_router
    monkeypatch.setattr(chat_config_router, "resolve_context_compaction_model_config", lambda settings: {})
    client = client_for(app, "alice")
    # A non-admin can adjust behavior without editing model credentials or the instance file.
    result = client.post("/chat_config/history_compaction", json={"trigger_ratio": .6, "summary_budget_ratio": .1,
        "target_tokens": 1200, "oversized_reject_factor": 1.5, "max_oversized_rejections": 3})
    assert result.status_code == 200, result.text
    from factory.agent_runtime.context_compaction import load_context_compaction_settings
    with identity(store, alice):
        settings = load_context_compaction_settings()
        assert settings.trigger_ratio == .6 and settings.target_tokens == 1200
        assert settings.summary_budget_ratio == .1
        assert settings.oversized_reject_factor == 1.5 and settings.max_oversized_rejections == 3
    with identity(store, bob):
        assert preferences.runtime_value("HISTORY_COMPACT_TRIGGER_RATIO") is None


def test_preferences_are_in_private_backup_and_restore_fingerprint(accounts, tmp_path):
    from memory import storage_backup as backup
    _, store, _, alice, _ = accounts
    before = backup.security_fingerprint(store.path)
    with identity(store, alice):
        preferences.update("ui", {"theme": "dark", "accent": "pink"})
        preferences.set_runtime({"NETWORK_RETRY_MAX_ATTEMPTS": 4})
    changed = backup.security_fingerprint(store.path)
    assert before != changed
    archive, target = tmp_path / "private.zip", tmp_path / "restore"
    backup.backup(tmp_path, store.root, archive)
    assert backup.restore(archive, target)["verified"]
    restored = IdentityStore(SecuritySettings(target / "history_files/state/security", require_mfa=False))
    restored.prepare()
    assert backup.security_fingerprint(restored.path) == changed
    with identity(restored, alice):
        assert preferences.snapshot()["ui"] == {"theme": "dark", "accent": "pink"}
        assert env_manager.load_var("NETWORK_RETRY_MAX_ATTEMPTS") == "4"
