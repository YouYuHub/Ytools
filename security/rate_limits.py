"""Persistent fixed-window API limits; never rate-limit individual SSE frames."""
import time
from memory.identity_store import Throttled


def charge(store, identifier, bucket, limit, seconds, amount=1):
    return charge_many(store, identifier, [(bucket, limit, seconds, amount)])


def charge_many(store, identifier, charges):
    """One commit; earlier charges still apply when a later bucket rejects."""
    now = time.time()
    exceeded = False
    with store.database(True) as db:
        for bucket, limit, seconds, amount in charges:
            key = "api:" + identifier + ":" + bucket
            row = db.execute("SELECT started,count FROM login_limits WHERE key=?", (key,)).fetchone()
            started, count = (row[0], row[1]) if row and now - row[0] < seconds else (now, 0)
            if count + amount > limit:
                exceeded = True
                break
            db.execute("INSERT OR REPLACE INTO login_limits VALUES(?,?,?)", (key, started, count + amount))
    if exceeded:
        raise Throttled("请求过于频繁或上传额度不足，请稍后重试")
