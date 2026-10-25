"""Durable accounts, opaque login sessions, resource ownership and security audit.

Authentication state is independent of chat JSONL and task recovery journals.
Every connection checks that the initialized DB still exists; loss fails closed.
"""
import contextlib
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
import uuid
from pathlib import Path

from security import crypto
from security.principal import Principal


class Unavailable(RuntimeError):
    pass


class Denied(ValueError):
    pass


class Throttled(Denied):
    pass


class IdentityStore:
    def __init__(self, settings):
        self.settings = settings
        self.root = Path(settings.directory)
        self.path = self.root / "identity.sqlite3"
        self.key_path = self.root / "mfa.key"

    def prepare(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            self.root.chmod(0o700)
        with contextlib.closing(sqlite3.connect(self.path, timeout=10)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
              CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY);
              INSERT OR IGNORE INTO schema_version VALUES(1);
              CREATE TABLE IF NOT EXISTS users(
                id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, display_name TEXT NOT NULL,
                password_hash TEXT NOT NULL, role TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                auth_version INTEGER NOT NULL DEFAULT 1, mfa_secret TEXT, mfa_counter INTEGER NOT NULL DEFAULT -1,
                created REAL NOT NULL, must_change INTEGER NOT NULL DEFAULT 0);
              CREATE TABLE IF NOT EXISTS login_sessions(
                id TEXT PRIMARY KEY, secret_hash TEXT NOT NULL, user_id TEXT NOT NULL,
                auth_version INTEGER NOT NULL, created REAL NOT NULL, last_seen REAL NOT NULL,
                expires REAL NOT NULL, verified INTEGER NOT NULL, recent_auth REAL NOT NULL,
                pending_mfa TEXT, pending_expires REAL, label TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS login_user ON login_sessions(user_id);
              CREATE TABLE IF NOT EXISTS recovery_codes(user_id TEXT, hash TEXT, PRIMARY KEY(user_id,hash));
              CREATE TABLE IF NOT EXISTS login_limits(key TEXT PRIMARY KEY, started REAL, count INTEGER);
              CREATE TABLE IF NOT EXISTS resources(kind TEXT, id TEXT, owner TEXT NOT NULL, created REAL,
                PRIMARY KEY(kind,id));
              CREATE INDEX IF NOT EXISTS resource_owner ON resources(owner,kind);
              CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, actor TEXT, action TEXT NOT NULL,
                resource TEXT, result TEXT NOT NULL, details TEXT NOT NULL, created REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS account_limits(user_id TEXT PRIMARY KEY, data TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS account_usage(
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL, day TEXT NOT NULL,
                reserved INTEGER NOT NULL, tokens INTEGER, state TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS usage_owner_day ON account_usage(user_id,day);
              CREATE TABLE IF NOT EXISTS mfa_devices(
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL, label TEXT NOT NULL,
                secret TEXT NOT NULL, counter INTEGER NOT NULL DEFAULT -1, created REAL NOT NULL);
              CREATE INDEX IF NOT EXISTS mfa_device_owner ON mfa_devices(user_id);
              CREATE TABLE IF NOT EXISTS mfa_state(
                user_id TEXT PRIMARY KEY, version INTEGER NOT NULL, primary_label TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS mfa_changes(
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL, login_id TEXT NOT NULL,
                mode TEXT NOT NULL, label TEXT NOT NULL, secret TEXT NOT NULL,
                expires REAL NOT NULL, base_version INTEGER NOT NULL);
              CREATE INDEX IF NOT EXISTS mfa_change_owner ON mfa_changes(user_id,login_id);
              CREATE TABLE IF NOT EXISTS mfa_recovery_required(user_id TEXT PRIMARY KEY);
            """)
            version = db.execute("SELECT max(version) FROM schema_version").fetchone()[0]
            if version not in {1, 2, 3, 4, 5, 6, 7}:
                raise Unavailable("账号数据库版本不受支持")
            if "must_change" not in {row[1] for row in db.execute("PRAGMA table_info(users)")}:
                db.execute("ALTER TABLE users ADD COLUMN must_change INTEGER NOT NULL DEFAULT 0")
            db.execute("INSERT OR IGNORE INTO schema_version VALUES(2)")
            db.execute("INSERT OR IGNORE INTO schema_version VALUES(3)")
            db.execute("INSERT OR IGNORE INTO schema_version VALUES(4)")
            from memory.session_catalog import prepare
            prepare(db)
            from security.resource_limits import prepare as prepare_limits
            prepare_limits(db)
            db.execute("INSERT OR IGNORE INTO schema_version VALUES(5)")
            from security.usernames import prepare as prepare_usernames
            prepare_usernames(db)
            db.execute("INSERT OR IGNORE INTO schema_version VALUES(6)")
            from security.preferences import prepare as prepare_preferences
            prepare_preferences(db)
            db.execute("INSERT OR IGNORE INTO schema_version VALUES(7)")
            db.commit()
        if os.name != "nt":
            self.path.chmod(0o600)

    @contextlib.contextmanager
    def database(self, write=False):
        if not self.path.is_file():
            raise Unavailable("账号数据库不可用，已拒绝访问")
        try:
            db = sqlite3.connect(f"file:{self.path.as_posix()}?mode=rw", uri=True, timeout=10)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            if write:
                db.execute("BEGIN IMMEDIATE")
            try:
                with db:
                    yield db
            finally:
                db.close()
        except sqlite3.Error as exc:
            raise Unavailable("账号存储暂不可用，已拒绝访问") from exc

    @staticmethod
    def _audit(db, actor, action, resource="", result="allowed", **details):
        db.execute("INSERT INTO audit(actor,action,resource,result,details,created) VALUES(?,?,?,?,?,?)",
                   (actor, action, resource, result, json.dumps(details, ensure_ascii=False), time.time()))

    def audit(self, actor, action, resource="", result="allowed", **details):
        with self.database(True) as db:
            self._audit(db, actor, action, resource, result, **details)

    def _fernet(self, create=False):
        from cryptography.fernet import Fernet
        if create and not self.key_path.exists():
            try:
                fd = os.open(self.key_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(Fernet.generate_key())
                    stream.flush()
                    os.fsync(stream.fileno())
            except FileExistsError:
                pass
        try:
            return Fernet(self.key_path.read_bytes())
        except (OSError, ValueError) as exc:
            raise Unavailable("MFA 加密密钥不可用，不能重新生成覆盖") from exc

    def encrypt(self, value):
        return self._fernet().encrypt(value.encode()).decode()

    def decrypt(self, value):
        from cryptography.fernet import InvalidToken
        try:
            return self._fernet().decrypt(value.encode()).decode()
        except InvalidToken as exc:
            raise Unavailable("MFA 密钥无法解密，已拒绝访问") from exc

    def initialized(self):
        with self.database() as db:
            return bool(db.execute("SELECT 1 FROM users WHERE role='admin' AND enabled=1").fetchone())

    def create_user(self, name, password, role="member", display=None, *, actor=None, bootstrap=False):
        name = crypto.username(name)
        if role not in {"admin", "operator", "member"}:
            raise ValueError("角色无效")
        if bootstrap and role != "admin":
            raise ValueError("首个账号必须是管理员")
        encoded = crypto.password_hash(password)
        identifier = uuid.uuid4().hex
        self._fernet(create=True)
        with self.database(True) as db:
            if bootstrap and db.execute("SELECT 1 FROM users").fetchone():
                raise Denied("首管理员已经创建，请使用账号管理功能")
            if not bootstrap:
                self._admin(db, actor)
            from security.usernames import reserved
            if reserved(db, name):
                raise ValueError("用户名已被保留，请使用其他名称")
            try:
                db.execute("INSERT INTO users(id,username,display_name,password_hash,role,created,must_change) VALUES(?,?,?,?,?,?,?)",
                           (identifier, name, crypto.display_name(display or name[:32]), encoded, role, time.time(), int(not bootstrap)))
            except sqlite3.IntegrityError as exc:
                raise ValueError("用户名已存在") from exc
            self._audit(db, actor or identifier, "user.create", identifier, role=role)
        return self.user(identifier)

    @staticmethod
    def _admin(db, actor):
        row = db.execute("SELECT * FROM users WHERE id=? AND enabled=1 AND role='admin'", (actor,)).fetchone()
        if not row:
            raise Denied("需要有效管理员身份")

    @staticmethod
    def _public(row, recovery_required=False):
        return {"id": row["id"], "username": row["username"], "name": row["display_name"], "role": row["role"],
                "enabled": bool(row["enabled"]), "mfa_enabled": bool(row["mfa_secret"]), "created": row["created"],
                "password_change_required": bool(row["must_change"]), "mfa_setup_required": recovery_required}

    def user(self, identifier):
        with self.database() as db:
            row = db.execute("SELECT * FROM users WHERE id=?", (identifier,)).fetchone()
            required = bool(db.execute("SELECT 1 FROM mfa_recovery_required WHERE user_id=?", (identifier,)).fetchone())
            return self._public(row, required) if row else None

    def users(self):
        with self.database() as db:
            required = {row[0] for row in db.execute("SELECT user_id FROM mfa_recovery_required")}
            return [self._public(row, row["id"] in required) for row in db.execute("SELECT * FROM users ORDER BY created")]

    def profile(self, identifier, name):
        name = crypto.display_name(name)
        with self.database(True) as db:
            db.execute("UPDATE users SET display_name=? WHERE id=? AND enabled=1", (name, identifier))
            self._audit(db, identifier, "user.profile", identifier)
        return self.user(identifier)

    def change_user(self, actor, identifier, *, role=None, enabled=None):
        if role is not None and role not in {"admin", "operator", "member"}:
            raise ValueError("角色无效")
        with self.database(True) as db:
            self._admin(db, actor)
            row = db.execute("SELECT * FROM users WHERE id=?", (identifier,)).fetchone()
            if row is None:
                raise Denied("账号不存在")
            next_role, next_enabled = role or row["role"], row["enabled"] if enabled is None else int(enabled)
            if row["role"] == "admin" and row["enabled"] and (next_role != "admin" or not next_enabled):
                if db.execute("SELECT count(*) FROM users WHERE enabled=1 AND role='admin'").fetchone()[0] <= 1:
                    raise Denied("不能禁用或降级最后一个有效管理员")
            db.execute("UPDATE users SET role=?,enabled=?,auth_version=auth_version+1 WHERE id=?", (next_role, next_enabled, identifier))
            db.execute("DELETE FROM login_sessions WHERE user_id=?", (identifier,))
            self._audit(db, actor, "user.change", identifier, role=next_role, enabled=bool(next_enabled))
        return self.user(identifier)

    def reset_password(self, actor, identifier, password, *, old_password=None):
        encoded = crypto.password_hash(password)
        with self.database(True) as db:
            if actor != identifier:
                self._admin(db, actor)
            row = db.execute("SELECT * FROM users WHERE id=?", (identifier,)).fetchone()
            if row is None or (actor == identifier and not crypto.password_matches(row["password_hash"], old_password)):
                raise Denied("当前密码不正确")
            db.execute("UPDATE users SET password_hash=?,auth_version=auth_version+1,must_change=? WHERE id=?",
                       (encoded, int(actor != identifier), identifier))
            db.execute("DELETE FROM login_sessions WHERE user_id=?", (identifier,))
            self._audit(db, actor, "user.password", identifier)

    def throttle(self, address, name, *, scope="login"):
        now = time.time()
        exceeded = False
        with self.database(True) as db:
            for key, maximum in ((scope + ":ip:" + crypto.digest(address), 60),
                                 (scope + ":user:" + crypto.digest(name), 12)):
                row = db.execute("SELECT * FROM login_limits WHERE key=?", (key,)).fetchone()
                started, count = (row["started"], row["count"] + 1) if row and now - row["started"] < 600 else (now, 1)
                db.execute("INSERT OR REPLACE INTO login_limits VALUES(?,?,?)", (key, started, count))
                exceeded = exceeded or count > maximum
            db.execute("DELETE FROM login_limits WHERE (key NOT LIKE 'api:%' AND started<?) OR started<?", (now - 1200, now - 86400))
        if exceeded:
            raise Throttled("尝试过于频繁，请稍后重试")

    def login(self, name, password, address="", label=""):
        try:
            normalized = crypto.username(name)
        except ValueError:
            normalized = "invalid"
        self.throttle(address, normalized)
        with self.database() as db:
            row = db.execute("SELECT * FROM users WHERE username=?", (normalized,)).fetchone()
            recovery_required = bool(row and db.execute("SELECT 1 FROM mfa_recovery_required WHERE user_id=?", (row["id"],)).fetchone())
        if not crypto.password_matches(row["password_hash"] if row else None, password) or not row or not row["enabled"]:
            self.audit(None, "login", crypto.digest(normalized), "denied")
            raise Denied("用户名、密码或账号状态不正确")
        now = time.time()
        identifier, secret = secrets.token_urlsafe(18), secrets.token_urlsafe(32)
        needs_mfa = recovery_required or bool(row["mfa_secret"]) or (self.settings.require_mfa and row["role"] in {"admin", "operator"})
        expires = now + (300 if needs_mfa else self.settings.absolute_seconds)
        with self.database(True) as db:
            fresh = db.execute("SELECT * FROM users WHERE id=?", (row["id"],)).fetchone()
            if not fresh["enabled"] or fresh["auth_version"] != row["auth_version"] or fresh["username"] != normalized:
                raise Denied("账号状态已改变，请重新登录")
            db.execute("DELETE FROM login_sessions WHERE expires<? OR last_seen<?", (now, now - self.settings.idle_seconds))
            db.execute("INSERT INTO login_sessions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       (identifier, crypto.digest(secret), row["id"], row["auth_version"], now, now, expires,
                        int(not needs_mfa), now, None, None, str(label)[:160]))
            self._audit(db, row["id"], "login.password", identifier)
        return identifier + "." + secret

    def authenticate(self, cookie):
        if not isinstance(cookie, str) or len(cookie) > 256 or not re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", cookie):
            return None
        identifier, secret = cookie.split(".", 1)
        now = time.time()
        with self.database() as db:
            row = db.execute("SELECT s.*,u.username,u.role,u.enabled,u.must_change,u.auth_version AS current_version FROM login_sessions s JOIN users u ON u.id=s.user_id WHERE s.id=?", (identifier,)).fetchone()
            if (not row or not hmac.compare_digest(row["secret_hash"], crypto.digest(secret)) or not row["enabled"]
                    or row["auth_version"] != row["current_version"] or row["expires"] <= now
                    or now - row["last_seen"] >= self.settings.idle_seconds):
                return None
            if now - row["last_seen"] >= 60:
                db.execute("UPDATE login_sessions SET last_seen=? WHERE id=?", (now, identifier))
        return Principal(row["user_id"], row["username"], row["role"], identifier, row["auth_version"],
                         bool(row["verified"]), crypto.csrf(secret, identifier), self, row["recent_auth"], bool(row["must_change"]))

    def live(self, principal):
        now = time.time()
        with self.database() as db:
            return bool(db.execute("SELECT 1 FROM login_sessions s JOIN users u ON u.id=s.user_id WHERE s.id=? AND u.enabled=1 AND u.auth_version=s.auth_version AND s.expires>? AND s.last_seen>?",
                                   (principal.login_id, now, now - self.settings.idle_seconds)).fetchone())

    def revoke(self, user_id, login_id=None):
        with self.database(True) as db:
            db.execute("DELETE FROM mfa_changes WHERE user_id=?" + (" AND login_id=?" if login_id else ""),
                       (user_id, login_id) if login_id else (user_id,))
            db.execute("DELETE FROM login_sessions WHERE user_id=?" + (" AND id=?" if login_id else ""),
                       (user_id, login_id) if login_id else (user_id,))
            self._audit(db, user_id, "login.revoke", login_id or "all")

    def sessions(self, user_id):
        with self.database() as db:
            return [dict(row) for row in db.execute("SELECT id,created,last_seen,expires,label FROM login_sessions WHERE user_id=? AND expires>? ORDER BY created DESC", (user_id, time.time()))]

    def _rotate_login(self, db, principal, *, verified=True):
        login = db.execute("SELECT * FROM login_sessions WHERE id=? AND user_id=?", (principal.login_id, principal.user_id)).fetchone()
        user = db.execute("SELECT * FROM users WHERE id=? AND enabled=1", (principal.user_id,)).fetchone()
        if (not login or not user or user["auth_version"] != principal.auth_version
                or login["expires"] <= time.time()):
            raise Denied("登录已失效，请重新登录")
        identifier, secret = secrets.token_urlsafe(18), secrets.token_urlsafe(32)
        db.execute("DELETE FROM mfa_changes WHERE user_id=? AND login_id=?", (principal.user_id, principal.login_id))
        db.execute("UPDATE login_sessions SET id=?,secret_hash=?,verified=?,expires=?,recent_auth=?,pending_mfa=NULL,pending_expires=NULL WHERE id=?",
                   (identifier, crypto.digest(secret), int(verified), time.time() + self.settings.absolute_seconds,
                    time.time(), principal.login_id))
        return identifier + "." + secret

    def start_mfa(self, principal, password):
        import pyotp
        with self.database(True) as db:
            row = db.execute("SELECT * FROM users WHERE id=?", (principal.user_id,)).fetchone()
            if row["mfa_secret"]:
                raise Denied("MFA 已启用，请在账号与安全的验证器管理中添加或更换")
            if not crypto.password_matches(row["password_hash"], password):
                raise Denied("当前密码不正确")
            login = db.execute("SELECT expires FROM login_sessions WHERE id=? AND user_id=?", (principal.login_id, principal.user_id)).fetchone()
            if not login or login["expires"] <= time.time():
                raise Denied("登录已过期，请重新登录")
            expires_at = min(time.time() + 300, login["expires"])
            secret = pyotp.random_base32()
            db.execute("UPDATE login_sessions SET pending_mfa=?,pending_expires=? WHERE id=? AND user_id=?",
                       (self.encrypt(secret), expires_at, principal.login_id, principal.user_id))
        # SHA-1, six digits and a 30-second period are standard TOTP defaults
        # understood by both Google and Microsoft Authenticator.
        return {"secret": secret, "uri": pyotp.TOTP(secret).provisioning_uri(principal.username, issuer_name="Ytools"), "expires_at": expires_at}

    @staticmethod
    def _counter(secret, code, last=-1):
        import pyotp
        if not re.fullmatch(r"[0-9]{6}", str(code)):
            return None
        now = int(time.time()) // 30
        totp = pyotp.TOTP(secret)
        for counter in (now - 1, now, now + 1):
            if counter > last and hmac.compare_digest(totp.at(counter * 30), str(code)):
                return counter
        return None

    def confirm_mfa(self, principal, code):
        recovery = [secrets.token_hex(12) for _ in range(10)]
        self.throttle(principal.login_id, principal.user_id, scope="mfa")
        with self.database(True) as db:
            login = db.execute("SELECT * FROM login_sessions WHERE id=? AND user_id=?", (principal.login_id, principal.user_id)).fetchone()
            if not login or not login["pending_mfa"] or login["pending_expires"] <= time.time():
                raise Denied("MFA 设置已过期，请重新开始")
            counter = self._counter(self.decrypt(login["pending_mfa"]), code)
            if counter is None:
                raise Denied("验证码无效或已使用")
            if db.execute("SELECT mfa_secret FROM users WHERE id=?", (principal.user_id,)).fetchone()[0]:
                raise Denied("MFA 状态已改变")
            db.execute("UPDATE users SET mfa_secret=?,mfa_counter=? WHERE id=?", (login["pending_mfa"], counter, principal.user_id))
            db.execute("DELETE FROM mfa_recovery_required WHERE user_id=?", (principal.user_id,))
            db.executemany("INSERT INTO recovery_codes VALUES(?,?)", [(principal.user_id, crypto.digest(value)) for value in recovery])
            db.execute("DELETE FROM login_sessions WHERE user_id=? AND id!=?", (principal.user_id, principal.login_id))
            cookie = self._rotate_login(db, principal)
            self._audit(db, principal.user_id, "mfa.enable", principal.user_id)
        return recovery, cookie

    def verify_mfa(self, principal, code):
        self.throttle(principal.login_id, principal.user_id, scope="mfa")
        with self.database(True) as db:
            row = db.execute("SELECT * FROM users WHERE id=? AND enabled=1", (principal.user_id,)).fetchone()
            if not row or not row["mfa_secret"]:
                raise Denied("MFA 未设置")
            from security.mfa_devices import consume_code
            consume_code(self, db, row, code)
            cookie = self._rotate_login(db, principal)
            self._audit(db, principal.user_id, "mfa.verify", principal.login_id)
        return cookie

    def reauthenticate(self, principal, password, code=""):
        self.throttle(principal.login_id, principal.user_id, scope="reauth")
        with self.database() as db:
            row = db.execute("SELECT * FROM users WHERE id=? AND enabled=1", (principal.user_id,)).fetchone()
        if not row or not crypto.password_matches(row["password_hash"], password):
            raise Denied("验证失败")
        if row["mfa_secret"]:
            return self.verify_mfa(principal, code)
        else:
            with self.database(True) as db:
                return self._rotate_login(db, principal, verified=principal.mfa_verified)

    def owner(self, kind, identifier):
        identifier = self.resource_key(kind, identifier)
        with self.database() as db:
            row = db.execute("SELECT owner FROM resources WHERE kind=? AND id=?", (kind, identifier)).fetchone()
            return row[0] if row else None

    def register(self, kind, identifier, user_id):
        identifier = self.resource_key(kind, identifier)
        with self.database(True) as db:
            row = db.execute("SELECT owner FROM resources WHERE kind=? AND id=?", (kind, identifier)).fetchone()
            if row and row[0] != user_id:
                raise Denied("资源不存在或无权访问")
            db.execute("INSERT OR IGNORE INTO resources VALUES(?,?,?,?)", (kind, identifier, user_id, time.time()))
            if kind == "session":
                db.execute("INSERT OR IGNORE INTO session_catalog VALUES(?,?,'reserved',0,?)",
                           (identifier, user_id, time.time()))
        from security.principal import current
        principal = current()
        if principal and principal.user_id == user_id:
            principal.resource_cache.pop(kind, None)

    def owned(self, kind, user_id):
        with self.database() as db:
            return {row[0] for row in db.execute("SELECT id FROM resources WHERE kind=? AND owner=?", (kind, user_id))}

    @staticmethod
    def resource_key(kind, identifier):
        return str(identifier).casefold() if os.name == "nt" and kind == "session" else str(identifier)

    def audit_entries(self, limit=100):
        with self.database() as db:
            return [dict(row) for row in db.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (min(200, max(1, limit)),))]

    def local_recover(self, name, password=None, *, reset_mfa=False):
        """Offline OS-authorized recovery. Never exposed by an HTTP route."""
        encoded = crypto.password_hash(password) if password is not None else None
        with self.database(True) as db:
            row = db.execute("SELECT * FROM users WHERE username=?", (crypto.username(name),)).fetchone()
            if row is None:
                raise Denied("账号不存在")
            if encoded:
                db.execute("UPDATE users SET password_hash=?,auth_version=auth_version+1,must_change=0 WHERE id=?", (encoded, row["id"]))
            if reset_mfa:
                db.execute("UPDATE users SET mfa_secret=NULL,mfa_counter=-1,auth_version=auth_version+1 WHERE id=?", (row["id"],))
                db.execute("DELETE FROM recovery_codes WHERE user_id=?", (row["id"],))
                db.execute("DELETE FROM mfa_devices WHERE user_id=?", (row["id"],))
                db.execute("DELETE FROM mfa_state WHERE user_id=?", (row["id"],))
                db.execute("INSERT OR IGNORE INTO mfa_recovery_required VALUES(?)", (row["id"],))
            db.execute("DELETE FROM login_sessions WHERE user_id=?", (row["id"],))
            db.execute("DELETE FROM mfa_changes WHERE user_id=?", (row["id"],))
            self._audit(db, "local-cli", "user.recovery", row["id"], password_changed=bool(encoded), mfa_reset=reset_mfa)
        return self.user(row["id"])
