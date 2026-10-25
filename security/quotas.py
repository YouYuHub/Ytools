"""Account-wide daily model reservations, shared by workers and all chat IDs.

Unknown interrupted requests retain their reservation. No prompt is stored here.
Token estimates are service accounting, not the provider's invoice/tokenizer.
"""
import datetime
import json

from memory.identity_store import Denied, Throttled
from security.principal import current

DEFAULTS = {"daily_calls": 1000, "daily_tokens": 5_000_000,
            "storage_bytes": 5 * 1024 ** 3, "concurrent_models": 2}


def limits(store, user, db=None):
    if db is None:
        with store.database() as connection:
            return limits(store, user, connection)
    row = db.execute("SELECT data FROM account_limits WHERE user_id=?", (user["id"],)).fetchone()
    base = {key: 0 for key in DEFAULTS} if user["role"] == "admin" else dict(DEFAULTS)
    return {**base, **(json.loads(row[0]) if row else {})}


def update(store, actor, identifier, value):
    with store.database(True) as db:
        store._admin(db, actor)
        if not db.execute("SELECT 1 FROM users WHERE id=?", (identifier,)).fetchone():
            raise Denied("账号不存在")
        user = db.execute("SELECT * FROM users WHERE id=?", (identifier,)).fetchone()
        value = {**limits(store, user, db), **{key: number for key, number in value.items() if number is not None}}
        db.execute("INSERT OR REPLACE INTO account_limits VALUES(?,?)", (identifier, json.dumps(value)))
        store._audit(db, actor, "account.limits", identifier, **value)
    return value


def _day():
    return datetime.datetime.now(datetime.timezone.utc).date().isoformat()


def _used(db, identifier):
    row = db.execute("SELECT count(*),coalesce(sum(coalesce(tokens,reserved)),0) FROM account_usage WHERE user_id=? AND day=? AND state!='cancelled'",
                     (identifier, _day())).fetchone()
    return {"daily_calls": row[0], "daily_tokens": row[1]}


def status(store, identifier):
    user = store.user(identifier)
    if not user:
        raise Denied("账号不存在")
    with store.database() as db:
        result = {"day_utc": _day(), "limits": limits(store, user, db), "used": _used(db, identifier)}
        result["models"] = {row[0]: row[1] for row in db.execute(
            "SELECT state,count(*) FROM model_slots WHERE user_id=? GROUP BY state", (identifier,))}
    from security.resource_limits import storage_usage
    result["storage"] = {"used_bytes": storage_usage(store, identifier), "limit_bytes": result["limits"]["storage_bytes"]}
    return result


def reserve(identifier, tokens):
    principal = current()
    if principal is None:
        return
    from security.resource_limits import check_storage
    check_storage()
    store = principal.store
    with store.database(True) as db:
        user = db.execute("SELECT * FROM users WHERE id=?", (principal.user_id,)).fetchone()
        if not user or not user["enabled"] or user["role"] != principal.role:
            raise Denied("账号授权已失效")
        if db.execute("SELECT 1 FROM account_usage WHERE id=?", (identifier,)).fetchone():
            return
        budget, used = limits(store, user, db), _used(db, principal.user_id)
        if any(budget[key] and used[key] + increment > budget[key] for key, increment in
               (("daily_calls", 1), ("daily_tokens", tokens))):
            raise Throttled("账号当日模型调用额度不足，请联系管理员调整；额度按 UTC 日重置")
        db.execute("INSERT INTO account_usage VALUES(?,?,?,?,NULL,'reserved')", (identifier, principal.user_id, _day(), max(0, tokens)))


def settle(identifier, tokens, *, sent, complete):
    principal = current()
    if principal is None:
        return
    with principal.store.database(True) as db:
        # A crashed/unknown upstream request may still have been billed. Keep
        # at least the reservation; a known usage response replaces it.
        db.execute("UPDATE account_usage SET tokens=CASE WHEN ? THEN ? WHEN ? THEN max(reserved,?) ELSE 0 END,state=? WHERE id=? AND user_id=? AND state='reserved'",
                   (complete, tokens, sent, tokens, "settled" if sent else "cancelled", identifier, principal.user_id))
