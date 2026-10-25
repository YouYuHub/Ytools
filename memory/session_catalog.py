"""Immutable ownership plus recoverable session lifecycle, independent of JSONL."""
import time

BLOCKED = {"deleting", "deleted", "cleanup_pending"}


def prepare(db):
    db.executescript("""
      CREATE TABLE IF NOT EXISTS session_catalog(
        id TEXT PRIMARY KEY, owner TEXT NOT NULL, state TEXT NOT NULL,
        layout INTEGER NOT NULL DEFAULT 0, updated REAL NOT NULL);
      CREATE INDEX IF NOT EXISTS session_catalog_owner ON session_catalog(owner,state);
    """)
    db.execute("""INSERT OR IGNORE INTO session_catalog
        SELECT id,owner,'reserved',0,created FROM resources WHERE kind='session'""")


def get(store, identifier):
    with store.database() as db:
        row = db.execute("SELECT * FROM session_catalog WHERE id=?",
                         (store.resource_key("session", identifier),)).fetchone()
        return dict(row) if row else None


def rows(store, owner=None):
    with store.database() as db:
        sql = "SELECT * FROM session_catalog"
        return [dict(row) for row in db.execute(sql + (" WHERE owner=?" if owner else ""),
                                               (owner,) if owner else ())]


def set_state(store, identifier, state, *, layout=None):
    if state not in {"reserved", "active", "missing", *BLOCKED}:
        raise ValueError("Invalid session state")
    with store.database(True) as db:
        db.execute("UPDATE session_catalog SET state=?,layout=coalesce(?,layout),updated=? WHERE id=?",
                   (state, layout, time.time(), store.resource_key("session", identifier)))
    from security.principal import current
    principal = current()
    if principal:
        principal.resource_cache.pop("session_catalog", None)


def reconcile(store, root, *, apply=False):
    """No adoption or deletion. Missing files never release a globally reserved ID."""
    from memory.storage_layout import chat_path, iter_chat_paths
    registered = {r["id"]: r for r in rows(store)}
    report = {"changes": [], "unowned": [], "conflicts": []}
    for sid, row in registered.items():
        path = chat_path(root, sid, store=store)
        legacy = root / (sid + "_chat.jsonl")
        if row["layout"] and legacy.exists() and path.exists():
            report["conflicts"].append(sid)
            continue
        exists = path.is_file()
        from memory.storage_layout import files_path, protocol_path, sidecar_path
        leftovers = exists or files_path(root, sid, store=store).exists() or any(
            protocol_path(root, sid, p, store=store).exists() for p in ("messages", "responses")) or any(
            sidecar_path(root, path).glob(path.name + ".*"))
        state = row["state"]
        desired = ("cleanup_pending" if leftovers else "deleted") if state in BLOCKED else (
            "active" if exists else ("missing" if state == "active" else state))
        if desired != state:
            report["changes"].append({"id": sid, "from": state, "to": desired})
            if apply:
                set_state(store, sid, desired)
    for path in iter_chat_paths(root, store=store, include_blocked=True):
        sid = store.resource_key("session", path.name.removesuffix("_chat.jsonl"))
        if sid not in registered:
            report["unowned"].append(sid)
    return report
