"""Read-only JSONL history search, with a disposable incremental SQLite index.

Only readable conversation fields are indexed: never raw model requests, remote
state, headers or base64 media. Search doesn't instantiate a memory manager or
rewrite a history file. SQLite is stdlib; literal substring matching is done in
SQLite's native code, without the build cost/disk growth of a trigram FTS index.
"""
import hashlib
import json
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any

_INDEX_VERSION = 3
_lock = threading.Lock()


class SearchCancelled(Exception):
    pass


class SearchSnapshotChanged(Exception):
    pass


def _check(cancel: threading.Event) -> None:
    if cancel.is_set():
        raise SearchCancelled()


def _text(value: Any) -> str:
    """Extract text/attachment labels without serializing multimodal bytes."""
    if isinstance(value, str):
        return "" if value.startswith("data:") else value
    if isinstance(value, list):
        return "\n".join(filter(None, (_text(item) for item in value)))
    if isinstance(value, dict):
        kind = value.get("type", "")
        kind = kind if isinstance(kind, str) else ""
        if kind == "base64":
            return ""
        if kind in {"text", "input_text", "output_text"}:
            return _text(value.get("text"))
        if kind in {"image_url", "input_image", "input_audio", "audio", "video_url", "input_file", "file", "document", "image"}:
            labels = [_text(value.get(key)) for key in ("name", "filename", "file_name", "media_ref", "url", "image_url") if value.get(key)]
            for key in ("file", "image_url", "video_url", "input_audio"):
                nested = value.get(key)
                if isinstance(nested, dict):
                    labels.extend(_text(nested.get(label)) for label in ("filename", "url", "media_ref") if nested.get(label))
                    if key == "input_audio" and str(nested.get("data", "")).startswith("media://"):
                        labels.append(nested["data"])
            return "\n".join(filter(None, labels))
        # Tool JSON remains searchable, but binary and opaque protocol fields do not.
        return "\n".join(str(key) + ": " + _text(item) for key, item in value.items()
                         if key not in {"file_data", "base64", "encrypted_content", "signature", "provider_state"})
    return "" if value is None else str(value)


def _units(record: dict, round_no: int, event_index: int | None):
    event = record.get("event")
    if event == "model_response":
        return
    role = record.get("role")
    if event == "sub_agent":
        for field in ("task", "reasoning_content", "content", "arguments", "result", "final_reply", "todos", "error"):
            text = _text(record.get(field)).strip()
            if text:
                yield round_no, event_index, "sub_agent", field, text, record.get("timestamp", ""), record.get("agent_id", "")
        return
    if event == "context_compaction":
        for field in ("summary_text", "compress_content", "content", "error"):
            text = _text(record.get(field)).strip()
            if text:
                yield round_no, event_index, "compaction", field, text, record.get("timestamp", ""), ""
        return
    if role not in {"user", "assistant", "tool"}:
        return
    fields = ("reasoning_content", "content", "error") if role == "assistant" else ("content",)
    if role == "tool":
        fields = ("arguments", "result", "content")
    for field in fields:
        text = _text(record.get(field)).strip()
        if text:
            yield round_no, event_index, role, field, text, record.get("timestamp", ""), ""
    for quote in record.get("quotes", []) if isinstance(record.get("quotes"), list) else []:
        text = _text(quote.get("text") if isinstance(quote, dict) else quote).strip()
        if text:
            yield round_no, event_index, role, "quote", text, record.get("timestamp", ""), ""
    if role == "assistant":
        for call in record.get("tool_calls", []) if isinstance(record.get("tool_calls"), list) else []:
            function = call.get("function", {}) if isinstance(call, dict) else {}
            function = function if isinstance(function, dict) else {}
            text = _text(function.get("arguments")).strip()
            if text:
                yield round_no, event_index, "tool", "arguments", text, record.get("timestamp", ""), ""


def _read_file(path: Path, cancel: threading.Event):
    title, updated, documents, round_no, skipped = path.stem, "", [], 0, 0
    with path.open("r", encoding="utf-8-sig", errors="replace") as stream:
        for line in stream:
            _check(cancel)
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except (ValueError, RecursionError):
                skipped += 1
                continue
            if not isinstance(record, dict):
                continue
            if isinstance(record.get("_meta"), dict):
                meta = record["_meta"]
                title = str(meta.get("title") or title)
                updated = str(meta.get("updated_at") or "")
            elif record.get("event") == "chat_round" and isinstance(record.get("events"), list):
                round_no += 1  # Same numbering as HistoryParser, including incomplete rounds.
                events = record["events"]
                if not any(isinstance(evt, dict) and evt.get("role") == "user" for evt in events):
                    question = _text(record.get("question")).strip()
                    if question:
                        documents.append((round_no, None, "user", "content", question, record.get("started_at", ""), ""))
                for index, evt in enumerate(events):
                    if isinstance(evt, dict):
                        documents.extend(_units(evt, round_no, index))
            elif record.get("event") == "context_compaction":
                anchor = record.get("display_round")
                documents.extend(_units(record, anchor if isinstance(anchor, int) else 0, None))
    documents.insert(0, (0, None, "session", "filename", path.name, "", ""))
    documents.insert(0, (0, None, "session", "title", title, "", ""))
    return title, updated, documents, skipped


def _schema(db: sqlite3.Connection) -> None:
    db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    version = db.execute("SELECT value FROM settings WHERE key='version'").fetchone()
    if version and version[0] != str(_INDEX_VERSION):
        db.executescript("DROP TABLE IF EXISTS search_fts; DROP TABLE IF EXISTS documents; DROP TABLE IF EXISTS sessions; DELETE FROM settings;")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            filename TEXT PRIMARY KEY, session_id TEXT, title TEXT, updated TEXT,
            fingerprint TEXT, skipped INTEGER);
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY, filename TEXT, round_no INTEGER, event_index INTEGER,
            role TEXT, field TEXT, text TEXT, search_text TEXT, timestamp TEXT, agent_id TEXT);
        CREATE INDEX IF NOT EXISTS documents_file ON documents(filename);
        CREATE INDEX IF NOT EXISTS documents_scope ON documents(role,field);
    """)
    db.execute("INSERT OR REPLACE INTO settings VALUES ('version',?)", (str(_INDEX_VERSION),))
    db.execute("INSERT OR IGNORE INTO settings VALUES ('revision',?)", (uuid.uuid4().hex,))
    db.commit()


def _fingerprint(path: Path) -> str:
    stat = path.stat()
    return f"{stat.st_mtime_ns}:{stat.st_size}:{stat.st_ino}"


def _sync(db: sqlite3.Connection, root: Path, cancel: threading.Event, *, archive_scope="all", archives=None):
    known = {row[0]: row[1] for row in db.execute("SELECT filename,fingerprint FROM sessions")}
    from memory.storage_layout import iter_chat_paths
    # iter_chat_paths 已按本账户 catalog + 非删除状态过滤，不再逐个鉴权。
    paths = [path for path in iter_chat_paths(root) if path.is_file() and not path.is_symlink()]
    filenames = {path.name for path in paths}
    archives = archives or {}
    changed, skipped_files, checked_files, indexed_files, excluded_files = False, 0, 0, 0, 0
    with db:
        for name in known.keys() - filenames:
            _check(cancel)
            db.execute("DELETE FROM documents WHERE filename=?", (name,))
            db.execute("DELETE FROM sessions WHERE filename=?", (name,))
            changed = True
        for path in paths:
            _check(cancel)
            session_id = path.name[:-11] if path.name.endswith("_chat.jsonl") else path.stem
            if archive_scope != "all" and (session_id in archives) != (archive_scope == "archived"):
                excluded_files += 1
                continue  # 范围外不读正文，也不更新指纹；取消归档后按新指纹补索引。
            checked_files += 1
            try:
                fingerprint = _fingerprint(path)
                if fingerprint == known.get(path.name):
                    continue
                title, updated, documents, skipped = _read_file(path, cancel)
                if _fingerprint(path) != fingerprint:
                    raise OSError("History changed during indexing")
            except OSError:
                # Don't serve stale hits for unreadable/changing/deleted files.
                skipped_files += 1
                if path.name in known:
                    db.execute("DELETE FROM documents WHERE filename=?", (path.name,))
                    db.execute("DELETE FROM sessions WHERE filename=?", (path.name,))
                    changed = True
                continue
            db.execute("DELETE FROM documents WHERE filename=?", (path.name,))
            db.execute("INSERT OR REPLACE INTO sessions VALUES (?,?,?,?,?,?)",
                       (path.name, session_id, title, updated, fingerprint, skipped))
            def rows():
                for round_no, event_index, role, field, text, timestamp, agent_id in documents:
                    _check(cancel)
                    yield path.name, round_no, event_index, role, field, text, text.casefold(), str(timestamp or ""), agent_id
            db.executemany("INSERT INTO documents(filename,round_no,event_index,role,field,text,search_text,timestamp,agent_id) VALUES(?,?,?,?,?,?,?,?,?)", rows())
            indexed_files += 1
            changed = True
        if changed:
            db.execute("UPDATE settings SET value=? WHERE key='revision'", (uuid.uuid4().hex,))
    return {"skipped_files": skipped_files, "checked_files": checked_files,
            "indexed_files": indexed_files, "excluded_files": excluded_files}


def _snippets(text: str, query: str) -> list[dict]:
    """Return up to three excerpts; offsets are relative to each displayed excerpt."""
    folded, needle = text.casefold(), query.casefold()
    positions, offset = [], 0
    # Map casefold offsets correctly (e.g. ß -> ss); avoid a per-character map
    # for the ordinary/large-text case where every character keeps its length.
    mapping = None
    if len(folded) != len(text):
        mapping = [index for index, char in enumerate(text) for _ in char.casefold()]
    while len(positions) < 3:
        at = folded.find(needle, offset)
        if at < 0:
            break
        end = at + len(needle)
        start_char = mapping[at] if mapping is not None else at
        end_char = mapping[end - 1] + 1 if mapping is not None else end
        left, right = max(0, start_char - 64), min(len(text), end_char + 110)
        if not positions or left >= positions[-1][1]:
            positions.append((left, right, start_char, end_char))
        offset = end if not positions else max(end, len(text[:right].casefold()))
    return [{"text": text[left:right], "start": start - left, "end": end - left,
             "prefix": left > 0, "suffix": right < len(text)}
            for left, right, start, end in positions]


def search_history(query: str, *, limit: int = 40, offset: int = 0,
                   revision: str | None = None, cancel: threading.Event | None = None,
                   root: Path | None = None, include_internal: bool = False,
                   include_filenames: bool = False, archive_scope: str = "active") -> dict:
    from memory import chat_memory
    root = Path(root if root is not None else chat_memory.HISTORY_ROOT)
    if archive_scope not in {"all", "active", "archived"}:
        raise ValueError("无效的归档搜索范围")
    query = query.strip()
    if not query or len(query) > 200 or "\x00" in query:
        raise ValueError("搜索文本需为 1–200 个字符，且不能包含空字符")
    cancel = cancel if cancel is not None else threading.Event()
    while not _lock.acquire(timeout=0.05):
        _check(cancel)
    db = None
    try:
        _check(cancel)
        from memory.session_archives import snapshot as archive_snapshot
        archives = archive_snapshot(root)
        index_dir = root / "sidecars"
        index_dir.mkdir(parents=True, exist_ok=True)
        from security.principal import current
        principal = current()
        index_name = "history_search" + ("_" + principal.user_id if principal else "") + ".sqlite3"
        db = sqlite3.connect(index_dir / index_name, timeout=15)
        db.row_factory = sqlite3.Row
        db.set_progress_handler(lambda: int(cancel.is_set()), 1000)
        _schema(db)
        sync_stats = _sync(db, root, cancel, archive_scope=archive_scope, archives=archives)
        if archive_snapshot(root) != archives:
            raise SearchSnapshotChanged("归档状态已变化，请重新搜索")
        current = db.execute("SELECT value FROM settings WHERE key='revision'").fetchone()[0]
        db.execute("CREATE TEMP TABLE archived_sessions(session_id TEXT PRIMARY KEY)")
        db.executemany("INSERT INTO archived_sessions VALUES(?)", ((sid,) for sid in archives))
        db.execute("CREATE TEMP TABLE eligible_sessions(filename TEXT PRIMARY KEY) WITHOUT ROWID")
        membership = ""
        if archive_scope != "all":
            membership = " WHERE session_id " + ("IN" if archive_scope == "archived" else "NOT IN") + " (SELECT session_id FROM archived_sessions)"
        db.execute("INSERT INTO eligible_sessions SELECT filename FROM sessions" + membership)
        archive_revision = hashlib.sha256(json.dumps(archives, sort_keys=True).encode()).hexdigest()
        scope = f"{int(include_internal)}:{int(include_filenames)}:{archive_scope}:{archive_revision}"
        snapshot = hashlib.sha256((current + "\0" + query.casefold() + "\0" + scope).encode()).hexdigest()[:24]
        if revision and revision != snapshot:
            raise SearchSnapshotChanged("会话记录已变化，请重新搜索")
        needle = query.casefold()
        args = [needle]
        # Filter message categories before matching large tool/reasoning bodies;
        # session membership is resolved before documents are visited below.
        categories = ["(d.role='session' AND d.field='title')"]
        categories.append("d.role!='session'" if include_internal else "(d.role IN ('user','assistant') AND d.field='content')")
        if include_filenames:
            categories.append("(d.role='session' AND d.field='filename')")
        # 固定先遍历范围内的会话，再用文件索引定位文档。仅在最终 WHERE 排除
        # 归档会话仍可能先扫描整个 documents 表，达不到 500 -> 50 的效果。
        source = ("FROM eligible_sessions e CROSS JOIN sessions s ON s.filename=e.filename "
                  "CROSS JOIN documents d INDEXED BY documents_file ON d.filename=e.filename WHERE ("
                  + " OR ".join(categories) + ") AND instr(d.search_text,?) > 0")
        # Scan normalized text once; never sort/copy large message bodies just
        # to paginate. Only the selected page reads the original display text.
        matches = db.execute("SELECT d.id,s.filename " + source
                             + " ORDER BY s.updated DESC,s.filename,d.round_no,d.event_index,d.id", args).fetchall()
        total = len(matches)
        session_count = len({row["filename"] for row in matches})
        rows = [db.execute("SELECT d.*,s.session_id,s.title FROM documents d JOIN sessions s ON s.filename=d.filename WHERE d.id=?",
                           (row["id"],)).fetchone() for row in matches[offset:offset + limit]]
        results = []
        for row in rows:
            _check(cancel)
            snippets = _snippets(row["text"], query)
            results.append({key: row[key] for key in ("session_id", "filename", "title", "round_no", "event_index", "role", "field", "timestamp", "agent_id")}
                           | {"id": row["id"], "snippets": snippets, "archived": row["session_id"] in archives})
        skipped_lines = db.execute("SELECT coalesce(sum(skipped),0) FROM sessions WHERE filename IN (SELECT filename FROM eligible_sessions)").fetchone()[0]
        return {"query": query, "results": results, "total": total, "session_count": session_count,
                "include_internal": include_internal, "include_filenames": include_filenames, "archive_scope": archive_scope,
                "offset": offset, "next_offset": offset + len(results) if offset + len(results) < total else None,
                "revision": snapshot, **sync_stats, "skipped_lines": skipped_lines}
    except sqlite3.OperationalError:
        _check(cancel)
        raise
    finally:
        if db is not None:
            db.close()
        _lock.release()
