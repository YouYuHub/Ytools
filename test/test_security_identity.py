"""Identity boundary tests use temporary storage, never the operator's accounts."""
import dataclasses
import time

import pyotp
import pytest

from memory.identity_store import IdentityStore, Denied, Throttled, Unavailable
from security import crypto
from security.settings import SecuritySettings

PASSWORD = "correct horse battery staple 24"


@pytest.fixture
def identity(tmp_path):
    settings = SecuritySettings(tmp_path / "identity", require_mfa=False)
    store = IdentityStore(settings)
    store.prepare()
    admin = store.create_user("admin", PASSWORD, "admin", bootstrap=True)
    return store, admin


def test_password_hashing_and_input_bounds():
    first, second = crypto.password_hash(PASSWORD), crypto.password_hash(PASSWORD)
    assert first.startswith("$argon2id$") and first != second
    assert crypto.password_matches(first, PASSWORD)
    assert not crypto.password_matches(first, "incorrect")
    assert not crypto.password_matches(None, PASSWORD)
    for value in ("short", "a" * 129):
        with pytest.raises(ValueError):
            crypto.password_hash(value)


def test_bootstrap_and_last_admin_protection(identity):
    store, admin = identity
    with pytest.raises(Denied):
        store.create_user("other", PASSWORD, "admin", bootstrap=True)
    with pytest.raises(Denied):
        store.change_user(admin["id"], admin["id"], enabled=False)
    with pytest.raises(Denied):
        store.change_user(admin["id"], admin["id"], role="member")


def test_opaque_cookie_and_csrf_never_stored_in_plaintext(identity):
    store, admin = identity
    cookie = store.login("ADMIN", PASSWORD, "loopback", "test device")
    principal = store.authenticate(cookie)
    assert principal.user_id == admin["id"] and principal.admin and principal.mfa_verified
    assert principal.csrf and principal.csrf != cookie
    with store.database() as db:
        row = dict(db.execute("SELECT * FROM login_sessions").fetchone())
    assert cookie.split(".")[1] not in str(row)
    assert principal.csrf not in str(row)
    assert store.authenticate(cookie + "wrong") is None
    assert store.authenticate("malformed") is None
    store.revoke(admin["id"], principal.login_id)
    assert store.authenticate(cookie) is None


def test_disabled_and_role_changes_revoke_sessions(identity):
    store, admin = identity
    user = store.create_user("member", PASSWORD, actor=admin["id"])
    cookie = store.login("member", PASSWORD)
    assert store.authenticate(cookie)
    store.change_user(admin["id"], user["id"], enabled=False)
    assert store.authenticate(cookie) is None
    with pytest.raises(Denied):
        store.login("member", PASSWORD)


def test_non_admin_cannot_create_or_elevate(identity):
    store, admin = identity
    member = store.create_user("member", PASSWORD, actor=admin["id"])
    with pytest.raises(Denied):
        store.create_user("other", PASSWORD, "admin", actor=member["id"])
    with pytest.raises(Denied):
        store.change_user(member["id"], member["id"], role="admin")


def test_login_expiry_and_password_change(identity):
    store, admin = identity
    cookie = store.login("admin", PASSWORD)
    with store.database(True) as db:
        db.execute("UPDATE login_sessions SET last_seen=?", (time.time() - 2000,))
    assert store.authenticate(cookie) is None
    cookie = store.login("admin", PASSWORD)
    store.reset_password(admin["id"], admin["id"], PASSWORD + " changed", old_password=PASSWORD)
    assert store.authenticate(cookie) is None
    with pytest.raises(Denied):
        store.login("admin", PASSWORD)


def test_totp_enrollment_encryption_and_replay_protection(identity):
    store, admin = identity
    store.settings = dataclasses.replace(store.settings, require_mfa=True)
    cookie = store.login("admin", PASSWORD)
    principal = store.authenticate(cookie)
    assert not principal.mfa_verified
    enrolled = store.start_mfa(principal, PASSWORD)
    code = pyotp.TOTP(enrolled["secret"]).now()
    codes, verified_cookie = store.confirm_mfa(principal, code)
    assert len(codes) == 10 and len(set(codes)) == 10
    assert store.authenticate(cookie) is None
    assert store.authenticate(verified_cookie).mfa_verified
    with store.database() as db:
        stored = db.execute("SELECT mfa_secret FROM users WHERE id=?", (admin["id"],)).fetchone()[0]
    assert enrolled["secret"] not in stored and store.decrypt(stored) == enrolled["secret"]
    next_cookie = store.login("admin", PASSWORD)
    next_principal = store.authenticate(next_cookie)
    with pytest.raises(Denied):
        store.verify_mfa(next_principal, code)
    recovered_cookie = store.verify_mfa(next_principal, codes[0])
    assert store.authenticate(next_cookie) is None
    assert store.authenticate(recovered_cookie).mfa_verified
    with pytest.raises(Denied):
        store.verify_mfa(next_principal, codes[0])


def test_ownership_is_exclusive_and_unknown_is_unowned(identity):
    store, admin = identity
    member = store.create_user("member", PASSWORD, actor=admin["id"])
    assert store.owner("legacy") is None
    store.register("new", member["id"])
    assert store.owned(member["id"]) == {"new"}
    assert not store.owned(admin["id"])
    with pytest.raises(Denied):
        store.register("new", admin["id"])


def test_rate_limits_are_persistent_and_bounded(identity):
    store, _ = identity
    for _ in range(12):
        store.throttle("one-ip", "unknown")
    with pytest.raises(Throttled):
        store.throttle("another-ip", "unknown")
    with store.database() as db:
        assert db.execute("SELECT count(*) FROM login_limits").fetchone()[0] == 3


def test_storage_loss_and_key_corruption_fail_closed(identity):
    store, _ = identity
    encoded = store.encrypt("test-only-secret")
    store.key_path.write_bytes(b"invalid-key")
    with pytest.raises(Unavailable):
        store.decrypt(encoded)
    store.path.unlink()
    with pytest.raises(Unavailable):
        store.initialized()
    assert not store.path.exists()


def test_secret_fields_are_not_in_profile_or_audit(identity):
    store, admin = identity
    cookie = store.login("admin", PASSWORD)
    assert "password_hash" not in str(store.user(admin["id"]))
    audit = str(store.audit_entries())
    assert PASSWORD not in audit and cookie not in audit
