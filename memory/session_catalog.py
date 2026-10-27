"""Immutable ownership plus recoverable session lifecycle, independent of JSONL.

会话归属唯一真源：单表 session_catalog(id,owner,state,updated)，不再双写 resources。
所有会话统一使用账号目录布局，不再区分新旧 layout。
"""
import time

BLOCKED = {"deleting", "deleted", "cleanup_pending"}


def prepare(db):
    db.executescript("""
      CREATE TABLE IF NOT EXISTS session_catalog(
        id TEXT PRIMARY KEY, owner TEXT NOT NULL, state TEXT NOT NULL,
        updated REAL NOT NULL);
      CREATE INDEX IF NOT EXISTS session_catalog_owner ON session_catalog(owner,state);
    """)
    columns = {row[1] for row in db.execute("PRAGMA table_info(session_catalog)")}
    if "layout" in columns:
        # 旧库一次性收敛：保留归属与状态，丢弃 layout 列。
        db.executescript("""
          CREATE TABLE session_catalog_new(
            id TEXT PRIMARY KEY, owner TEXT NOT NULL, state TEXT NOT NULL,
            updated REAL NOT NULL);
          INSERT OR IGNORE INTO session_catalog_new(id,owner,state,updated)
            SELECT id,owner,state,updated FROM session_catalog;
          DROP TABLE session_catalog;
          ALTER TABLE session_catalog_new RENAME TO session_catalog;
          CREATE INDEX IF NOT EXISTS session_catalog_owner ON session_catalog(owner,state);
        """)
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='resources'").fetchone():
        db.execute("""INSERT OR IGNORE INTO session_catalog(id,owner,state,updated)
            SELECT id,owner,'reserved',created FROM resources WHERE kind='session'""")
        db.execute("DROP TABLE resources")


def get(store, identifier):
    with store.database() as db:
        row = db.execute("SELECT * FROM session_catalog WHERE id=?",
                         (store.resource_key(identifier),)).fetchone()
        return dict(row) if row else None


def rows(store, owner=None):
    with store.database() as db:
        sql = "SELECT * FROM session_catalog"
        return [dict(row) for row in db.execute(sql + (" WHERE owner=?" if owner else ""),
                                               (owner,) if owner else ())]


def set_state(store, identifier, state):
    if state not in {"reserved", "active", "missing", *BLOCKED}:
        raise ValueError("Invalid session state")
    with store.database(True) as db:
        db.execute("UPDATE session_catalog SET state=?,updated=? WHERE id=?",
                   (state, time.time(), store.resource_key(identifier)))
    from security.principal import current
    principal = current()
    if principal:
        principal.resource_cache.pop("session_catalog", None)


def reconcile(store, root, *, apply=False):
    """No adoption or deletion. Missing files never release a globally reserved ID."""
    from memory.storage_layout import chat_path
    from pathlib import Path
    registered = {r["id"]: r for r in rows(store)}
    report = {"changes": [], "unowned": []}
    for sid, row in registered.items():
        path = chat_path(root, sid, store=store)
        exists = path.is_file()
        from memory.storage_layout import files_path, protocol_path, sidecar_path
        leftovers = exists or files_path(root, sid, store=store).exists() or any(
            protocol_path(root, sid, p, store=store).exists() for p in ("messages", "responses")) or any(
            sidecar_path(path).glob(path.name + ".*"))
        state = row["state"]
        desired = ("cleanup_pending" if leftovers else "deleted") if state in BLOCKED else (
            "active" if exists else ("missing" if state == "active" else state))
        if desired != state:
            report["changes"].append({"id": sid, "from": state, "to": desired})
            if apply:
                set_state(store, sid, desired)
    for path in (Path(root) / "users").glob("*/sessions/*/*_chat.jsonl"):
        sid = store.resource_key(path.name.removesuffix("_chat.jsonl"))
        if sid not in registered:
            report["unowned"].append(sid)
    return report
