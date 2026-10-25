"""Self-service login-name changes; identity and resource ownership never move."""
import time

from memory.identity_store import Denied
from security import crypto

COOLDOWN_DAYS = 30
COOLDOWN_SECONDS = COOLDOWN_DAYS * 86400


class ChangeError(Denied):
    def __init__(self, message, *, status=400, retry_after=None):
        super().__init__(message)
        self.status, self.retry_after = status, retry_after


def prepare(db):
    db.executescript("""
      CREATE TABLE IF NOT EXISTS username_history(
        username TEXT PRIMARY KEY, user_id TEXT NOT NULL, retired REAL NOT NULL);
      CREATE INDEX IF NOT EXISTS username_history_owner ON username_history(user_id,retired);
    """)


def reserved(db, username):
    return bool(db.execute("SELECT 1 FROM username_history WHERE username=?", (username,)).fetchone())


def _account(store, db, principal):
    now = time.time()
    user = db.execute("SELECT * FROM users WHERE id=?", (principal.user_id,)).fetchone()
    login = db.execute("SELECT * FROM login_sessions WHERE id=? AND user_id=?",
                       (principal.login_id, principal.user_id)).fetchone()
    if (not user or not user["enabled"] or not login or not login["verified"] or not principal.mfa_verified
            or user["auth_version"] != principal.auth_version or login["auth_version"] != user["auth_version"]
            or user["role"] != principal.role or login["expires"] <= now
            or now - login["last_seen"] >= store.settings.idle_seconds):
        raise Denied("登录已失效，请重新登录后修改用户名")
    if user["must_change"] or db.execute("SELECT 1 FROM mfa_recovery_required WHERE user_id=?", (user["id"],)).fetchone():
        raise Denied("请先完成初始密码修改或验证器恢复")
    return user


def _next_change(db, user_id):
    row = db.execute("SELECT max(retired) FROM username_history WHERE user_id=?", (user_id,)).fetchone()
    return (row[0] + COOLDOWN_SECONDS) if row[0] is not None else 0


def status(store, principal):
    with store.database() as db:
        user = _account(store, db, principal)
        next_change = _next_change(db, user["id"])
        setup = user["role"] in {"admin", "operator"} and not user["mfa_secret"]
        return {"username": user["username"], "cooldown_days": COOLDOWN_DAYS,
                "next_change_at": next_change, "mfa_required": bool(user["mfa_secret"] or setup),
                "mfa_setup_required": setup, "can_change": not setup and next_change <= time.time()}


def change(store, principal, username, password, code=""):
    username = crypto.username(username)
    # A new login cookie or a previous rename cannot reset the attempt bucket.
    store.throttle(principal.user_id, principal.user_id, scope="username_change")
    with store.database() as db:
        snapshot = _account(store, db, principal)
    # Keep the expensive password hash outside the SQLite write transaction.
    if not crypto.password_matches(snapshot["password_hash"], password):
        raise Denied("当前密码不正确")
    with store.database(True) as db:
        user = _account(store, db, principal)
        if user["password_hash"] != snapshot["password_hash"]:
            raise Denied("账号状态已改变，请重新登录")
        if username == user["username"]:
            raise ChangeError("新用户名与当前用户名相同")
        if db.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone() or reserved(db, username):
            raise ChangeError("此用户名已被使用或保留，请选择其他名称", status=409)
        now = time.time()
        remaining = _next_change(db, user["id"]) - now
        if remaining > 0:
            raise ChangeError("登录用户名每 30 天可修改一次，请在冷却期结束后再试", status=429,
                              retry_after=max(1, int(remaining) + 1))
        if user["role"] in {"admin", "operator"} and not user["mfa_secret"]:
            raise Denied("管理员和操作员必须先绑定验证器，再修改登录用户名")
        if user["mfa_secret"]:
            from security.mfa_devices import consume_code
            consume_code(store, db, user, code)
        # Name reservation, factor consumption and session revocation commit
        # together. A conflict/failure never leaves an MFA code consumed alone.
        db.execute("INSERT INTO username_history VALUES(?,?,?)", (user["username"], user["id"], now))
        db.execute("UPDATE users SET username=?,auth_version=auth_version+1 WHERE id=?", (username, user["id"]))
        db.execute("DELETE FROM login_sessions WHERE user_id=?", (user["id"],))
        db.execute("DELETE FROM mfa_changes WHERE user_id=?", (user["id"],))
        store._audit(db, user["id"], "user.username.change", user["id"], previous=user["username"], username=username)
    return {"username": username, "next_change_at": now + COOLDOWN_SECONDS, "relogin_required": True}
