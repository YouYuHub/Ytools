"""Disposable accounts only: transactional rename, MFA, reservations and HTTP."""
import contextlib
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import time

import pyotp
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from memory.identity_store import IdentityStore, Denied
from security import crypto, usernames
from security.principal import current
from security.settings import SecuritySettings

PASSWORD = "temporary username fixture password 2026"


@pytest.fixture
def accounts(tmp_path, monkeypatch):
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    from memory import chat_memory
    monkeypatch.setattr(chat_memory, "HISTORY_ROOT", tmp_path / "history_files")
    store = IdentityStore(SecuritySettings(tmp_path / "history_files/state/security", require_mfa=False))
    store.prepare()
    admin = store.create_user("admin", PASSWORD, "admin", bootstrap=True)
    member = store.create_user("member", PASSWORD, actor=admin["id"])
    with store.database(True) as db:
        db.execute("UPDATE users SET must_change=0")
    store.register("session", "fixed-session", member["id"])
    return store, admin, member, now


def login(store, user, secret=None):
    cookie = store.login(store.user(user["id"])["username"], PASSWORD)
    principal = store.authenticate(cookie)
    if secret:
        cookie = store.verify_mfa(principal, pyotp.TOTP(secret).at(time.time() - 30))
        principal = store.authenticate(cookie)
    return principal, cookie


def enroll(store, user):
    secret = pyotp.random_base32()
    with store.database(True) as db:
        db.execute("UPDATE users SET mfa_secret=?,mfa_counter=-1 WHERE id=?", (store.encrypt(secret), user["id"]))
    return secret


def test_member_rename_keeps_id_data_limits_and_password_but_revokes_all_logins(accounts, tmp_path):
    store, admin, member, _ = accounts
    principal, first = login(store, member)
    _, second = login(store, member)
    from security import quotas
    quotas.update(store, admin["id"], member["id"], {"daily_calls": 7})
    data = tmp_path / "history_files/users" / member["id"] / "sessions/fixed-session/history.txt"
    data.parent.mkdir(parents=True)
    data.write_bytes(b"unchanged history and versions")
    with store.database() as db:
        password_hash = db.execute("SELECT password_hash FROM users WHERE id=?", (member["id"],)).fetchone()[0]
    result = usernames.change(store, principal, "Member.New", PASSWORD)
    assert result["username"] == "member.new" and result["relogin_required"]
    assert store.authenticate(first) is None and store.authenticate(second) is None
    assert store.owner("session", "fixed-session") == member["id"]
    assert store.user(member["id"])["name"] == "member"
    assert quotas.limits(store, store.user(member["id"]))["daily_calls"] == 7
    assert data.read_bytes() == b"unchanged history and versions"
    with store.database() as db:
        assert db.execute("SELECT password_hash FROM users WHERE id=?", (member["id"],)).fetchone()[0] == password_hash
        audit = db.execute("SELECT details FROM audit WHERE action='user.username.change'").fetchone()[0]
    assert json.loads(audit) == {"previous": "member", "username": "member.new"}
    with pytest.raises(Denied):
        store.login("member", PASSWORD)
    changed, _ = login(store, member)
    assert changed.user_id == member["id"] and changed.username == "member.new"


@pytest.mark.parametrize("role", ["admin", "operator"])
def test_privileged_accounts_require_enrolled_mfa_even_with_local_mfa_disabled(accounts, role):
    store, admin, _, _ = accounts
    user = admin if role == "admin" else store.create_user("operator", PASSWORD, role, actor=admin["id"])
    with store.database(True) as db:
        db.execute("UPDATE users SET must_change=0")
    principal, cookie = login(store, user)
    assert usernames.status(store, principal)["mfa_setup_required"]
    with pytest.raises(Denied, match="必须先绑定"):
        usernames.change(store, principal, "new-login", PASSWORD)
    assert store.authenticate(cookie) and store.user(user["id"])["username"] == user["username"]


def test_admin_can_rename_admin_without_losing_role_or_rebinding_mfa(accounts):
    store, admin, _, now = accounts
    secret = enroll(store, admin)
    principal, _ = login(store, admin, secret)
    with store.database() as db:
        cipher = db.execute("SELECT mfa_secret FROM users WHERE id=?", (admin["id"],)).fetchone()[0]
    usernames.change(store, principal, "new-admin", PASSWORD, pyotp.TOTP(secret).at(now))
    assert store.user(admin["id"])["role"] == "admin" and store.initialized()
    assert store.user(admin["id"])["username"] == "new-admin"
    with store.database() as db:
        assert db.execute("SELECT mfa_secret FROM users WHERE id=?", (admin["id"],)).fetchone()[0] == cipher
    pending = store.authenticate(store.login("new-admin", PASSWORD))
    assert not pending.mfa_verified
    verified = store.authenticate(store.verify_mfa(pending, pyotp.TOTP(secret).at(now + 30)))
    assert verified.admin and verified.user_id == admin["id"] and verified.username == "new-admin"
    with pytest.raises(ValueError, match="保留"):
        store.create_user("ADMIN", PASSWORD, actor=admin["id"])


def test_enrolled_member_also_requires_fresh_factor_and_replay_is_rejected(accounts):
    store, _, member, now = accounts
    secret = enroll(store, member)
    principal, cookie = login(store, member, secret)
    with pytest.raises(Denied, match="验证码"):
        usernames.change(store, principal, "new-member", PASSWORD, pyotp.TOTP(secret).at(now - 30))
    assert store.authenticate(cookie)
    usernames.change(store, principal, "new-member", PASSWORD, pyotp.TOTP(secret).at(now))


def test_second_authenticator_and_recovery_codes_are_supported(accounts):
    store, _, member, now = accounts
    primary = enroll(store, member)
    second = pyotp.random_base32()
    principal, _ = login(store, member, primary)
    with store.database(True) as db:
        db.execute("INSERT INTO mfa_devices VALUES(?,?,?,?,-1,?)", ("d" * 32, member["id"], "fixture", store.encrypt(second), now))
    usernames.change(store, principal, "with-second", PASSWORD, pyotp.TOTP(second).at(now))
    assert store.user(member["id"])["mfa_enabled"]
    with store.database() as db:
        assert db.execute("SELECT counter FROM mfa_devices").fetchone()[0] == now // 30


def test_recovery_proof_is_consumed_once_and_pending_enrollment_is_cancelled(accounts):
    store, admin, _, now = accounts
    secret = enroll(store, admin)
    principal, _ = login(store, admin, secret)
    recovery = "a" * 24
    with store.database(True) as db:
        db.execute("INSERT INTO recovery_codes VALUES(?,?)", (admin["id"], crypto.digest(recovery)))
        db.execute("INSERT INTO mfa_changes VALUES(?,?,?,?,?,?,?,?)",
                   ("e" * 32, admin["id"], principal.login_id, "add", "fixture", store.encrypt(secret), now + 300, 0))
    usernames.change(store, principal, "with-recovery", PASSWORD, recovery)
    with store.database() as db:
        assert db.execute("SELECT count(*) FROM recovery_codes").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM mfa_changes").fetchone()[0] == 0


def test_conflict_does_not_consume_factor_or_revoke_cookie(accounts):
    store, admin, member, now = accounts
    secret = enroll(store, member)
    principal, cookie = login(store, member, secret)
    with pytest.raises(usernames.ChangeError) as conflict:
        usernames.change(store, principal, "ADMIN", PASSWORD, pyotp.TOTP(secret).at(now))
    assert conflict.value.status == 409 and store.authenticate(cookie)
    with store.database() as db:
        assert db.execute("SELECT mfa_counter FROM users WHERE id=?", (member["id"],)).fetchone()[0] == now // 30 - 1
    usernames.change(store, principal, "available", PASSWORD, pyotp.TOTP(secret).at(now))


def test_wrong_password_and_missing_factor_do_not_change_account(accounts):
    store, _, member, _ = accounts
    principal, cookie = login(store, member)
    with pytest.raises(Denied, match="密码"):
        usernames.change(store, principal, "new-name", "incorrect password")
    assert store.authenticate(cookie)
    assert store.user(member["id"])["username"] == "member"


def test_cooldown_is_account_scoped_and_retired_name_stays_reserved_after_expiry(accounts):
    store, _, member, now = accounts
    principal, _ = login(store, member)
    usernames.change(store, principal, "new-name", PASSWORD)
    principal, cookie = login(store, member)
    assert not usernames.status(store, principal)["can_change"]
    with pytest.raises(usernames.ChangeError) as cooldown:
        usernames.change(store, principal, "newer-name", PASSWORD)
    assert cooldown.value.status == 429 and cooldown.value.retry_after >= 30 * 86400
    assert store.authenticate(cookie)
    with store.database(True) as db:
        db.execute("UPDATE username_history SET retired=?", (now - usernames.COOLDOWN_SECONDS - 1,))
    with pytest.raises(usernames.ChangeError) as retired:
        usernames.change(store, principal, "member", PASSWORD)
    assert retired.value.status == 409
    usernames.change(store, principal, "newer-name", PASSWORD)


def test_two_accounts_cannot_race_to_claim_the_same_normalized_name(accounts):
    store, admin, member, _ = accounts
    other = store.create_user("another", PASSWORD, actor=admin["id"])
    with store.database(True) as db:
        db.execute("UPDATE users SET must_change=0")
    first, _ = login(store, member)
    second, _ = login(store, other)
    def attempt(principal):
        try:
            usernames.change(store, principal, "Shared.Name", PASSWORD)
            return "success"
        except usernames.ChangeError as error:
            return error.status
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(attempt, [first, second]))
    assert sorted(map(str, results)) == ["409", "success"]
    with store.database() as db:
        assert db.execute("SELECT count(*) FROM username_history").fetchone()[0] == 1


def test_same_account_parallel_renames_commit_only_once(accounts):
    store, _, member, _ = accounts
    principal, _ = login(store, member)
    def attempt(name):
        try:
            usernames.change(store, principal, name, PASSWORD)
            return True
        except Denied:
            return False
    with ThreadPoolExecutor(2) as pool:
        assert sum(pool.map(attempt, ["first-name", "second-name"])) == 1
    with store.database() as db:
        assert db.execute("SELECT count(*) FROM username_history").fetchone()[0] == 1


def test_password_login_started_before_rename_cannot_create_an_old_name_session(accounts, monkeypatch):
    store, _, member, _ = accounts
    principal, _ = login(store, member)
    original, changed = crypto.password_matches, []
    def race(encoded, password):
        matched = original(encoded, password)
        if not changed:
            changed.append(True)
            usernames.change(store, principal, "changed-during-login", PASSWORD)
        return matched
    monkeypatch.setattr(crypto, "password_matches", race)
    with pytest.raises(Denied):
        store.login("member", PASSWORD)
    assert store.authenticate(store.login("changed-during-login", PASSWORD)).user_id == member["id"]


@pytest.mark.parametrize("mutation", ["disabled", "expired", "unverified", "initial_password"])
def test_invalid_account_or_login_state_cannot_change_username(accounts, mutation):
    store, _, member, _ = accounts
    principal, _ = login(store, member)
    statements = {"disabled": "UPDATE users SET enabled=0", "expired": "UPDATE login_sessions SET expires=0",
                  "unverified": "UPDATE login_sessions SET verified=0", "initial_password": "UPDATE users SET must_change=1"}
    with store.database(True) as db:
        db.execute(statements[mutation])
    with pytest.raises(Denied):
        usernames.change(store, principal, "new-name", PASSWORD)


def app_for(store):
    from security.app import install_security
    app = FastAPI()
    @app.get("/chat_history/file")
    def history(session_id: str):
        return {"owner": current().user_id}
    install_security(app, store.settings)
    return app


def http_login(client, store, username="member"):
    response = client.post("/auth/login", json={"username": username, "password": PASSWORD}, headers={"Origin": store.settings.origin})
    assert response.status_code == 200
    return {"Origin": store.settings.origin, "X-CSRF-Token": response.json()["csrf"]}


def test_http_boundary_and_successful_relogin_preserve_owner(accounts):
    store, _, member, _ = accounts
    app = app_for(store)
    with TestClient(app, base_url=store.settings.origin) as client, TestClient(app, base_url=store.settings.origin) as other:
        assert client.get("/user/username").status_code == 401
        headers = http_login(client, store)
        other_headers = http_login(other, store)
        assert client.get("/auth/status").json()["username_change"]
        assert client.post("/user/username", json={"username": "renamed", "password": PASSWORD}).status_code == 403
        payload = {"username": "renamed", "password": PASSWORD}
        assert client.post("/user/username", json={**payload, "user_id": "other"}, headers=headers).status_code == 422
        response = client.post("/user/username", json=payload, headers=headers)
        assert response.status_code == 200 and "Max-Age=0" in response.headers["set-cookie"]
        assert other.get("/auth/me").status_code == 401
        headers = http_login(client, store, "renamed")
        assert client.get("/chat_history/file", params={"session_id": "fixed-session"}).json()["owner"] == member["id"]
        response = client.post("/user/username", json={"username": "third-name", "password": PASSWORD}, headers=headers)
        assert response.status_code == 429 and int(response.headers["retry-after"]) > 0
        admin_headers = http_login(other, store, "admin")
        response = other.post("/admin/users", json={"username": "member", "password": PASSWORD}, headers=admin_headers)
        assert response.status_code == 400


def test_schema_upgrade_preserves_accounts_and_existing_names(accounts):
    store, admin, member, _ = accounts
    before = store.users()
    with store.database(True) as db:
        db.execute("DROP TABLE username_history")
        db.execute("DELETE FROM schema_version WHERE version>=6")
    store.prepare()
    assert store.users() == before
    with store.database() as db:
        assert db.execute("SELECT max(version) FROM schema_version").fetchone()[0] == 7


def test_private_backup_restore_preserves_retired_names(accounts, tmp_path):
    from memory import storage_backup as backup
    store, admin, member, _ = accounts
    principal, _ = login(store, member)
    usernames.change(store, principal, "new-login", PASSWORD)
    archive, restored = tmp_path / "before.zip", tmp_path / "restored"
    backup.backup(tmp_path, store.root, archive)
    backup.restore(archive, restored)
    target = IdentityStore(SecuritySettings(restored / "history_files/state/security", require_mfa=False))
    target.prepare()
    assert target.authenticate(target.login("new-login", PASSWORD)).user_id == member["id"]
    with pytest.raises(ValueError, match="保留"):
        target.create_user("member", PASSWORD, actor=admin["id"])
    assert backup.security_fingerprint(store.path) == backup.security_fingerprint(target.path)


def test_failure_after_factor_consumption_rolls_back_name_factor_and_session_revocation(accounts, monkeypatch):
    store, _, member, now = accounts
    secret = enroll(store, member)
    principal, cookie = login(store, member, secret)
    def fail_audit(*args, **kwargs):
        raise RuntimeError("injected audit write failure")
    monkeypatch.setattr(store, "_audit", fail_audit)
    with pytest.raises(RuntimeError, match="injected"):
        usernames.change(store, principal, "must-not-commit", PASSWORD, pyotp.TOTP(secret).at(now))
    assert store.authenticate(cookie) and store.user(member["id"])["username"] == "member"
    with store.database() as db:
        assert db.execute("SELECT count(*) FROM username_history").fetchone()[0] == 0
        assert db.execute("SELECT mfa_counter FROM users WHERE id=?", (member["id"],)).fetchone()[0] == now // 30 - 1


def test_password_only_pending_mfa_cookie_cannot_reach_username_mutations(accounts):
    store, _, member, _ = accounts
    enroll(store, member)
    with TestClient(app_for(store), base_url=store.settings.origin) as client:
        headers = http_login(client, store)
        assert client.get("/user/username").status_code == 403
        response = client.post("/user/username", json={"username": "blocked", "password": PASSWORD}, headers=headers)
        assert response.status_code == 403 and response.headers.get("X-MFA-Required")
        assert store.user(member["id"])["username"] == "member"
