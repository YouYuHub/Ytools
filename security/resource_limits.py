"""Account disk admission and process-owned fair model concurrency leases."""
import asyncio
import contextlib
import os
from pathlib import Path
import time

from memory.identity_store import Denied, Throttled
from security.principal import current


def prepare(db):
    db.executescript("""
      CREATE TABLE IF NOT EXISTS model_slots(
        id TEXT PRIMARY KEY, user_id TEXT NOT NULL, pid INTEGER NOT NULL,
        process_identity TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL);
      CREATE INDEX IF NOT EXISTS model_slots_owner ON model_slots(user_id,state,created);
      CREATE TABLE IF NOT EXISTS model_fairness(user_id TEXT PRIMARY KEY, admitted REAL NOT NULL);
    """)


def storage_usage(store, user_id):
    from security.storage_usage import measure
    return measure(store, user_id)


def check_storage(additional=0):
    principal = current()
    if principal is None:
        return
    from security.quotas import limits
    budget = limits(principal.store, {"id": principal.user_id, "role": principal.role}).get("storage_bytes", 0)
    if budget and storage_usage(principal.store, principal.user_id) + max(0, additional) > budget:
        raise Throttled("账号存储额度不足，请清理附件/会话或联系管理员；现有记录仍可读取和导出")


@contextlib.contextmanager
def storage_write(additional=0, *, session_id=None):
    """Serialize bulky writes across workers, counting their final occupied bytes."""
    principal = current()
    if principal is None:
        yield None
        return
    if session_id is not None:
        from memory.chat_memory import LOCK_ROOT, _safe_session_id
        from util.file_lock import cross_process_lock
        with cross_process_lock(LOCK_ROOT / (_safe_session_id(session_id) + "_chat.jsonl.lock")):
            from memory.session_catalog import get, BLOCKED
            row = get(principal.store, session_id)
            if row is None or row["owner"] != principal.user_id or row["state"] in BLOCKED:
                raise Denied("会话已删除或无权写入")
            with storage_write(additional) as available:
                yield available
        return
    from security.quotas import limits
    if not limits(principal.store, {"id": principal.user_id, "role": principal.role}).get("storage_bytes", 0):
        yield None
        return
    from util.file_lock import cross_process_lock
    with cross_process_lock(principal.store.root / "storage-locks" / (principal.user_id + ".lock")):
        budget = limits(principal.store, {"id": principal.user_id, "role": principal.role})["storage_bytes"]
        available = max(0, budget - storage_usage(principal.store, principal.user_id))
        if max(0, additional) > available:
            raise Throttled("账号存储额度不足，请清理附件或联系管理员")
        yield available


def release(identifier):
    principal = current()
    if principal:
        with principal.store.database(True) as db:
            db.execute("DELETE FROM model_slots WHERE id=? AND user_id=?", (identifier, principal.user_id))


def attempt(identifier):
    """One bounded transaction; waiting never holds a DB or OS lock."""
    principal = current()
    if principal is None:
        return True
    from security.quotas import limits
    from util.process_identity import process_identity
    identity = process_identity(os.getpid())
    if identity is None:
        raise Denied("无法验证并发租约的进程身份")
    store = principal.store
    try:
        from env_manager import load_var
        capacity = max(1, int(os.environ.get("YTOOLS_MODEL_CONCURRENCY", load_var("YTOOLS_MODEL_CONCURRENCY", "8"))))
    except ValueError:
        capacity = 8
    with store.database(True) as db:
        user = db.execute("SELECT * FROM users WHERE id=?", (principal.user_id,)).fetchone()
        if not user or not user["enabled"] or user["role"] != principal.role:
            raise Denied("账号授权已失效")
        # Process birth identity prevents a recycled PID from holding an old
        # slot forever. Never expire a live provider request just by its age.
        for row in db.execute("SELECT id,pid,process_identity FROM model_slots").fetchall():
            actual = process_identity(row["pid"])
            from memory.permission_store import pid_alive
            if actual != row["process_identity"] and (actual is not None or not pid_alive(row["pid"])):
                db.execute("DELETE FROM model_slots WHERE id=?", (row["id"],))
        db.execute("INSERT OR IGNORE INTO model_slots VALUES(?,?,?,?, 'waiting',?)",
                   (identifier, principal.user_id, os.getpid(), identity, time.time()))
        own = db.execute("SELECT * FROM model_slots WHERE id=?", (identifier,)).fetchone()
        if own["state"] == "running":
            return True
        running = db.execute("SELECT user_id,count(*) AS n FROM model_slots WHERE state='running' GROUP BY user_id").fetchall()
        counts = {r["user_id"]: r["n"] for r in running}
        if sum(counts.values()) >= capacity:
            return False
        waiting = db.execute("""SELECT s.user_id,min(s.created) AS first,coalesce(f.admitted,0) AS last
            FROM model_slots s LEFT JOIN model_fairness f ON f.user_id=s.user_id
            WHERE s.state='waiting' GROUP BY s.user_id ORDER BY last,first,s.user_id""").fetchall()
        for candidate in waiting:
            candidate_user = db.execute("SELECT * FROM users WHERE id=?", (candidate["user_id"],)).fetchone()
            if not candidate_user or not candidate_user["enabled"]:
                db.execute("DELETE FROM model_slots WHERE user_id=? AND state='waiting'", (candidate["user_id"],))
                continue
            limit = limits(store, candidate_user, db).get("concurrent_models", 2)
            if limit and counts.get(candidate["user_id"], 0) >= limit:
                continue
            first = db.execute("SELECT id FROM model_slots WHERE user_id=? AND state='waiting' ORDER BY created,id LIMIT 1",
                               (candidate["user_id"],)).fetchone()[0]
            if first != identifier:
                return False
            db.execute("UPDATE model_slots SET state='running' WHERE id=?", (identifier,))
            db.execute("INSERT OR REPLACE INTO model_fairness VALUES(?,?)", (principal.user_id, time.time()))
            return True
        return False


async def acquire(identifier, stopped=lambda: False):
    from util.api_timing import mark
    started = time.perf_counter()
    mark("model_slot_wait_start")
    operation = None
    try:
        while True:
            if stopped():
                raise asyncio.CancelledError()
            operation = asyncio.create_task(asyncio.to_thread(attempt, identifier))
            if await asyncio.shield(operation):
                mark("model_slot_acquired", model_slot_wait_ms=round((time.perf_counter()-started)*1000, 3))
                return
            await asyncio.sleep(.15)
    except BaseException:
        # A cancelled to_thread operation may still be finishing its bounded
        # transaction. It never owns a wait loop or starts a provider request.
        if operation is not None:
            await asyncio.gather(operation, return_exceptions=True)
        await asyncio.to_thread(release, identifier)
        raise


def acquire_sync(identifier, stopped=lambda: False):
    from util.api_timing import mark
    started = time.perf_counter()
    mark("model_slot_wait_start")
    try:
        while not stopped():
            if attempt(identifier):
                mark("model_slot_acquired", model_slot_wait_ms=round((time.perf_counter()-started)*1000, 3))
                return
            time.sleep(.15)
        raise asyncio.CancelledError()
    except BaseException:
        release(identifier)
        raise
