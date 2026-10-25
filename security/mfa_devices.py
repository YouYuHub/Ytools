"""Independent TOTP devices; confirmed replacement never creates an MFA gap."""
import secrets
import re
import time
import uuid

from memory.identity_store import Denied
from security import crypto

MAX_DEVICES = 5


def consume_code(store, db, user, code):
    """Each device keeps its own replay counter; recovery codes stay one-use."""
    code = str(code).strip()
    counter = store._counter(store.decrypt(user["mfa_secret"]), code, user["mfa_counter"])
    if counter is not None:
        db.execute("UPDATE users SET mfa_counter=? WHERE id=?", (counter, user["id"]))
        return
    for device in db.execute("SELECT id,secret,counter FROM mfa_devices WHERE user_id=? ORDER BY created,id", (user["id"],)):
        counter = store._counter(store.decrypt(device["secret"]), code, device["counter"])
        if counter is not None:
            db.execute("UPDATE mfa_devices SET counter=? WHERE id=? AND user_id=?", (counter, device["id"], user["id"]))
            return
    compact = re.sub(r"[\s-]", "", code).lower()
    recovery = compact if re.fullmatch(r"[0-9a-f]{24}", compact) else code
    if not db.execute("DELETE FROM recovery_codes WHERE user_id=? AND hash=?", (user["id"], crypto.digest(recovery))).rowcount:
        raise Denied("验证码无效或已使用，请使用当前验证器的下一组动态码或恢复码")


def _account(db, principal, *, pending_login=False):
    user = db.execute("SELECT * FROM users WHERE id=? AND enabled=1", (principal.user_id,)).fetchone()
    login = db.execute("SELECT * FROM login_sessions WHERE id=? AND user_id=?", (principal.login_id, principal.user_id)).fetchone()
    if (not user or not login or not user["mfa_secret"]
            or user["auth_version"] != principal.auth_version or login["auth_version"] != user["auth_version"]
            or login["expires"] <= time.time() or time.time() - login["last_seen"] >= principal.store.settings.idle_seconds):
        raise Denied("请先登录并完成当前验证器的验证")
    if pending_login:
        if login["verified"] or principal.mfa_verified or time.time() - login["created"] >= 300:
            raise Denied("请重新登录，或在账号与安全中管理验证器")
        if user["must_change"]:
            raise Denied("请先完成登录并修改初始密码，再管理验证器")
    elif not login["verified"] or not principal.mfa_verified:
        raise Denied("请先登录并完成当前验证器的验证")
    return user


def _state(db, user_id):
    row = db.execute("SELECT version,primary_label FROM mfa_state WHERE user_id=?", (user_id,)).fetchone()
    return (row[0], row[1]) if row else (0, "原有验证器")


def _changed(db, user_id, label=None):
    version, current_label = _state(db, user_id)
    db.execute("INSERT OR REPLACE INTO mfa_state VALUES(?,?,?)", (user_id, version + 1, label if label is not None else current_label))
    db.execute("DELETE FROM mfa_changes WHERE user_id=?", (user_id,))


def _listing(db, user_id):
    _, label = _state(db, user_id)
    return [{"id": "primary", "label": label, "primary": True}] + [
        {"id": row["id"], "label": row["label"], "primary": False, "created": row["created"]}
        for row in db.execute("SELECT id,label,created FROM mfa_devices WHERE user_id=? ORDER BY created,id", (user_id,))]


def listing(store, principal):
    with store.database() as db:
        _account(db, principal)
        remaining = db.execute("SELECT count(*) FROM recovery_codes WHERE user_id=?", (principal.user_id,)).fetchone()[0]
        return {"devices": _listing(db, principal.user_id), "max_devices": MAX_DEVICES, "recovery_codes_remaining": remaining}


def _proof(store, db, user, password, code):
    if not crypto.password_matches(user["password_hash"], password):
        raise Denied("当前密码不正确")
    consume_code(store, db, user, code)


def regenerate_recovery(store, principal, password, code):
    store.throttle(principal.login_id, principal.user_id, scope="mfa_manage")
    with store.database(True) as db:
        user = _account(db, principal)
        if user["must_change"]:
            raise Denied("请先修改初始密码")
        _proof(store, db, user, password, code)
        codes = [secrets.token_hex(12) for _ in range(10)]
        db.execute("DELETE FROM recovery_codes WHERE user_id=?", (principal.user_id,))
        db.executemany("INSERT INTO recovery_codes VALUES(?,?)", [(principal.user_id, crypto.digest(item)) for item in codes])
        db.execute("DELETE FROM login_sessions WHERE user_id=? AND id!=?", (principal.user_id, principal.login_id))
        _changed(db, principal.user_id)
        cookie = store._rotate_login(db, principal)
        store._audit(db, principal.user_id, "mfa.recovery.regenerate", principal.user_id)
    return cookie, {"recovery_codes": codes}


def begin_from_login(store, principal, code, mode, label):
    # The pending, five-minute cookie already proves the password. A current
    # factor is still required before issuing any new enrollment secret.
    return begin(store, principal, None, code, mode, label, pending_login=True)


def begin(store, principal, password, code, mode, label, *, pending_login=False):
    import pyotp
    if mode not in {"add", "replace"}:
        raise Denied("请选择添加或更换验证器")
    label = crypto.display_name(label)
    store.throttle(principal.login_id, principal.user_id, scope="mfa_manage")
    with store.database(True) as db:
        user = _account(db, principal, pending_login=pending_login)
        if mode == "add" and len(_listing(db, principal.user_id)) >= MAX_DEVICES:
            raise Denied("最多保留 5 个验证器，请先移除不再使用的验证器")
        if pending_login:
            consume_code(store, db, user, code)
        else:
            _proof(store, db, user, password, code)
        cookie = store._rotate_login(db, principal)
        operation, secret, expires = uuid.uuid4().hex, pyotp.random_base32(), time.time() + 300
        db.execute("DELETE FROM mfa_changes WHERE expires<=?", (time.time(),))
        db.execute("INSERT INTO mfa_changes VALUES(?,?,?,?,?,?,?,?)", (operation, principal.user_id, cookie.split(".", 1)[0],
            mode, label, store.encrypt(secret), expires, _state(db, principal.user_id)[0]))
        store._audit(db, principal.user_id, "mfa.device.prepare", operation, mode=mode, label=label)
    return cookie, {"operation_id": operation, "mode": mode, "label": label, "secret": secret,
                    "uri": pyotp.TOTP(secret).provisioning_uri(principal.username, issuer_name="Ytools"), "expires_at": expires}


def complete(store, principal, operation, code):
    store.throttle(principal.login_id, principal.user_id, scope="mfa_manage_confirm")
    with store.database(True) as db:
        _account(db, principal)
        pending = db.execute("SELECT * FROM mfa_changes WHERE id=? AND user_id=? AND login_id=?", (operation, principal.user_id, principal.login_id)).fetchone()
        if not pending or pending["expires"] <= time.time() or pending["base_version"] != _state(db, principal.user_id)[0]:
            raise Denied("绑定已过期或配置已变化，请重新生成二维码")
        counter = store._counter(store.decrypt(pending["secret"]), code)
        if counter is None:
            raise Denied("请输入新验证器中 Ytools 条目的 6 位动态码")
        recovery = []
        if pending["mode"] == "add":
            if len(_listing(db, principal.user_id)) >= MAX_DEVICES:
                raise Denied("验证器数量已达上限")
            db.execute("INSERT INTO mfa_devices VALUES(?,?,?,?,?,?)", (operation, principal.user_id, pending["label"], pending["secret"], counter, time.time()))
            _changed(db, principal.user_id)
        else:
            db.execute("UPDATE users SET mfa_secret=?,mfa_counter=? WHERE id=?", (pending["secret"], counter, principal.user_id))
            db.execute("DELETE FROM mfa_devices WHERE user_id=?", (principal.user_id,))
            db.execute("DELETE FROM recovery_codes WHERE user_id=?", (principal.user_id,))
            recovery = [secrets.token_hex(12) for _ in range(10)]
            db.executemany("INSERT INTO recovery_codes VALUES(?,?)", [(principal.user_id, crypto.digest(item)) for item in recovery])
            db.execute("DELETE FROM login_sessions WHERE user_id=? AND id!=?", (principal.user_id, principal.login_id))
            _changed(db, principal.user_id, pending["label"])
        cookie = store._rotate_login(db, principal)
        store._audit(db, principal.user_id, "mfa.device." + pending["mode"], operation, label=pending["label"])
        result = {"mode": pending["mode"], "devices": _listing(db, principal.user_id), "recovery_codes": recovery}
    return cookie, result


def cancel(store, principal, operation):
    with store.database(True) as db:
        _account(db, principal)
        db.execute("DELETE FROM mfa_changes WHERE id=? AND user_id=? AND login_id=?", (operation, principal.user_id, principal.login_id))
    return {"ok": True}


def remove(store, principal, identifier, password, code):
    store.throttle(principal.login_id, principal.user_id, scope="mfa_manage")
    with store.database(True) as db:
        user = _account(db, principal)
        devices = _listing(db, principal.user_id)
        target = next((device for device in devices if device["id"] == identifier), None)
        if not target:
            raise Denied("验证器不存在或不属于当前账号")
        if len(devices) <= 1:
            raise Denied("至少保留一个验证器，请使用更换验证器")
        _proof(store, db, user, password, code)
        if identifier == "primary":
            replacement = db.execute("SELECT * FROM mfa_devices WHERE user_id=? ORDER BY created,id LIMIT 1", (principal.user_id,)).fetchone()
            db.execute("UPDATE users SET mfa_secret=?,mfa_counter=? WHERE id=?", (replacement["secret"], replacement["counter"], principal.user_id))
            db.execute("DELETE FROM mfa_devices WHERE id=? AND user_id=?", (replacement["id"], principal.user_id))
            _changed(db, principal.user_id, replacement["label"])
        else:
            db.execute("DELETE FROM mfa_devices WHERE id=? AND user_id=?", (identifier, principal.user_id))
            _changed(db, principal.user_id)
        db.execute("DELETE FROM login_sessions WHERE user_id=? AND id!=?", (principal.user_id, principal.login_id))
        cookie = store._rotate_login(db, principal)
        store._audit(db, principal.user_id, "mfa.device.remove", identifier, label=target["label"])
        result = {"devices": _listing(db, principal.user_id)}
    return cookie, result
