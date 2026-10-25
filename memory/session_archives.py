"""Durable local archive flags, independent of large/running JSONL histories."""
import json
from datetime import datetime, timezone
from pathlib import Path

from util.atomic_json import write_json
from util.file_lock import cross_process_lock


def _root(root=None):
    if root is not None:
        return Path(root)
    from memory import chat_memory
    return chat_memory.HISTORY_ROOT


def snapshot(root=None, *, filtered=True):
    path = _root(root) / "sidecars" / "session_archives.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(raw, dict) or raw.get("version") != 1 or not isinstance(raw.get("sessions"), dict):
        raise ValueError("归档状态文件格式异常，请检查 sidecars/session_archives.json")
    if any(not isinstance(sid, str) or not isinstance(item, dict) or not isinstance(item.get("archived_at"), str)
           for sid, item in raw["sessions"].items()):
        raise ValueError("归档状态条目异常，请检查 sidecars/session_archives.json")
    if filtered:
        from security.authorization import owned_sessions
        allowed = owned_sessions()
        if allowed is not None:
            from security.principal import current
            principal = current()
            return {sid: value for sid, value in raw["sessions"].items() if principal.store.resource_key("session", sid) in allowed}
    return raw["sessions"]


def status(session_id, entries=None):
    from memory.chat_memory import normalize_session_id
    sid = normalize_session_id(session_id)
    item = (snapshot() if entries is None else entries).get(sid)
    return {"archived": item is not None, "archived_at": item["archived_at"] if item else None}


def set_archived(session_id, archived):
    from memory import chat_memory
    sid = chat_memory.normalize_session_id(session_id)
    root = _root()
    with cross_process_lock(root / "sidecars" / "session_archives.lock"):
        path = chat_memory._get_chat_history_file(sid)
        if path.is_symlink() or not path.is_file():
            raise FileNotFoundError("会话不存在，无法更改归档状态")
        entries = snapshot(root, filtered=False)
        if archived:
            entries.setdefault(sid, {"archived_at": datetime.now(timezone.utc).isoformat()})
        else:
            entries.pop(sid, None)
        write_json(root / "sidecars" / "session_archives.json", {"version": 1, "sessions": entries})
    return {"session_id": sid, **status(sid, entries)}


def forget(session_id):
    """Run after history deletion; shares the mutation lock with archive writes."""
    from memory.chat_memory import normalize_session_id
    root = _root()
    if not (root / "sidecars" / "session_archives.json").exists():
        return
    with cross_process_lock(root / "sidecars" / "session_archives.lock"):
        entries = snapshot(root, filtered=False)
        if entries.pop(normalize_session_id(session_id), None) is not None:
            write_json(root / "sidecars" / "session_archives.json", {"version": 1, "sessions": entries})


def list_archived(*, q="", limit=50, offset=0):
    from memory import chat_memory
    from memory.session_listing import _summary
    entries = snapshot()
    rows = []
    for sid, item in entries.items():
        if chat_memory.normalize_session_id(sid) != sid:
            continue
        row = _summary(chat_memory._get_chat_history_file(sid), sid)
        if row:
            rows.append({**row, "archived": True, "archived_at": item["archived_at"]})
    count = len(rows)
    needle = q.strip().casefold()
    rows = [row for row in rows if not needle or needle in row["title"].casefold() or needle in row["id"].casefold()]
    rows.sort(key=lambda row: (row["archived_at"], row["updated_at"], row["id"]), reverse=True)
    page = rows[offset:offset + limit]
    return {"sessions": page, "total": len(rows), "archived_count": count, "offset": offset,
            "next_offset": offset + len(page) if offset + len(page) < len(rows) else None}
