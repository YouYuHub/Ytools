"""Small, read-only session summaries; independent of manager initialization.

单次 catalog 查询 + 单次归档快照，内存过滤后再逐个读首行；不再逐会话查库。
"""
import json
import threading
from datetime import datetime
from pathlib import Path

from util.timestamp_utils import DEFAULT_TIMESTAMP_FORMAT

# 首行可能就是超长轮次而非 _meta 行；列表绝不解析整轮。
MAX_SUMMARY_HEADER_BYTES = 2 * 1024 * 1024


def _summary(path: Path, session_id: str) -> dict | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        modified = path.stat().st_mtime
    except OSError:
        return None
    meta = {}
    try:
        with path.open("rb") as stream:
            first = stream.readline(MAX_SUMMARY_HEADER_BYTES + 1)
        if len(first) <= MAX_SUMMARY_HEADER_BYTES:
            row = json.loads(first.decode("utf-8-sig"))
            if isinstance(row, dict) and isinstance(row.get("_meta"), dict):
                meta = row["_meta"]
    except (OSError, ValueError, UnicodeError, RecursionError):
        # Broken or busy files remain discoverable without a full scan or
        # repair write. Opening that session retains the normal recovery path.
        pass
    title = meta.get("title")
    if not isinstance(title, str) or not title.strip() or title == "session_" + session_id:
        questions = meta.get("user_questions")
        title = next((q.strip()[:40] for q in questions if isinstance(q, str) and q.strip()), "") if isinstance(questions, list) else ""
    updated = meta.get("updated_at")
    created = meta.get("created_at")
    group = meta.get("group_id")
    return {
        "id": session_id, "filename": path.name, "title": title.strip() if isinstance(title, str) and title.strip() else session_id,
        "updated_at": updated if isinstance(updated, str) and updated.strip() else datetime.fromtimestamp(modified).strftime(DEFAULT_TIMESTAMP_FORMAT),
        "created_at": created if isinstance(created, str) else "",
        "group_id": group.strip() if isinstance(group, str) and group.strip() else None,
    }


def list_session_summaries(session_id: str | None = None, archive_scope: str = "active", *, archive_entries=None) -> list[dict]:
    from memory import chat_memory
    from memory.session_archives import snapshot, status
    from memory.session_catalog import BLOCKED, rows as catalog_rows
    from memory.storage_layout import chat_path, identity
    from security.principal import current
    if archive_scope not in {"active", "archived", "all"}:
        raise ValueError("无效的归档范围")
    store = identity()
    principal = current()
    archived = snapshot() if archive_entries is None else archive_entries

    def attach(row):
        if row["id"] in archived:
            row.update(status(row["id"], archived))
        return row

    if store is None:
        # 离线库模式：无归属概念，直接扫描根目录。
        from memory.storage_layout import iter_chat_paths
        rows = []
        for path in iter_chat_paths(chat_memory.HISTORY_ROOT):
            sid = chat_memory.normalize_session_id(path.name)
            if archive_scope != "all" and (sid in archived) != (archive_scope == "archived"):
                continue
            row = _summary(path, sid)
            if row:
                rows.append(attach(row))
        rows.sort(key=lambda row: (row["updated_at"], row["created_at"], row["filename"]), reverse=True)
        if session_id is not None:
            sid = chat_memory.normalize_session_id(session_id)
            return [row for row in rows if row["id"] == sid][:1]
        return rows
    scope = principal.user_id if principal and store is principal.store else None
    catalog = {row["id"]: row for row in catalog_rows(store, scope)}

    def selected(sid):
        row = catalog.get(sid)
        if row is None or row["state"] in BLOCKED:
            return False
        return archive_scope == "all" or (sid in archived) == (archive_scope == "archived")

    if session_id is not None:
        sid = chat_memory.normalize_session_id(session_id)
        if not selected(sid):
            return []
        row = _summary(chat_path(chat_memory.HISTORY_ROOT, sid, store=store), sid)
        return [attach(row)] if row else []
    rows = []
    for sid in catalog:
        if not selected(sid):
            continue  # 归档/状态过滤必须在打开文件/读取首行之前完成。
        row = _summary(chat_path(chat_memory.HISTORY_ROOT, sid, store=store), sid)
        if row:
            rows.append(attach(row))
    # Metadata timestamps use a fixed sortable format; the browser merges local
    # recency afterwards. Filename is a deterministic tie breaker.
    rows.sort(key=lambda row: (row["updated_at"], row["created_at"], row["filename"]), reverse=True)
    return rows


def recover_pending_sessions(stop: threading.Event) -> None:
    """Materialize orphaned rounds only, never execute tools or resume tasks."""
    from memory import chat_memory
    from memory.permission_store import has_restorable_round
    from util.logger import get_logger
    logger = get_logger(__name__)
    root = chat_memory.HISTORY_ROOT
    from memory.storage_layout import iter_chat_paths, sidecar_path
    candidates = [sidecar_path(p) / (p.name + ".pending") for p in iter_chat_paths(root)]
    seen = set()
    for path in candidates:
        if stop.is_set():
            break
        sid = chat_memory.normalize_session_id(path.name.removesuffix(".pending"))
        if sid in seen:
            continue
        seen.add(sid)
        history = chat_memory._get_chat_history_file(sid)
        try:
            # 无检查点是正常态（.pending 随轮次完成即删）：静默跳过，只有
            # 真实存在但读/解析失败的检查点文件才告警。
            if path.is_symlink() or not path.is_file() or history.is_symlink() or not history.is_file():
                continue
            pending = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(pending, dict) or pending.get("event") != "chat_round":
                continue
            if chat_memory._pid_alive(pending.get("writer_pid")) or has_restorable_round(sid) or stop.is_set():
                continue
            # Existing locks, deduplication and recovery checks remain authoritative.
            chat_memory.ChatMemoryManager(sid)
        except (OSError, ValueError, RecursionError):
            logger.warning("跳过无法恢复的会话检查点：%s", path.name)
        except Exception:
            logger.exception("后台恢复会话检查点失败：%s", path.name)
