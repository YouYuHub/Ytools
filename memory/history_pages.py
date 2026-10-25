"""Read-only round pages over original JSONL; bounded, disposable offset indexes.

The source remains authoritative. A changed snapshot is rejected rather than
joining pages from different edits. No manager initialization or repair writes.
"""
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import threading

from fastapi import HTTPException

_CACHE = OrderedDict()
_LOCK = threading.Lock()
_CACHE_BYTES = 16 * 1024 * 1024
_PAGE_BYTES = 2 * 1024 * 1024
_DETAIL_BYTES = 32 * 1024


def _fingerprint(info):
    # Windows path stat and open-handle stat can expose different ctime
    # semantics after overwrite. Identity, size and modification time agree.
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns if os.name != "nt" else 0


def _changed(stage="revision"):
    return HTTPException(409, "会话内容已变化，请重新加载历史", headers={"X-History-Changed": "true", "X-History-Snapshot-Stage":stage})


def _text(content):
    if isinstance(content, str):
        return content[:240]
    if isinstance(content, list):
        return "\n".join(p.get("text", "")[:240] for p in content
                         if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str))[:240]
    return ""


def _timestamp(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() if isinstance(value, str) else None
    except (ValueError, OverflowError, OSError):
        return None


def _summary(value):
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (ValueError, RecursionError):
            return value[:256] + "\n（展开加载完整结果）"
    else:
        parsed = value
    if not isinstance(parsed, dict):
        return "展开加载完整结果"
    fields = {"success", "state", "status", "error", "message", "exit_code", "path", "display_path",
              "needs_attention", "lines_added", "lines_removed", "history_recorded", "history_reason", "diff_skipped"}
    result = {key: item[:240] if isinstance(item, str) else item for key, item in parsed.items()
              if key in fields and (isinstance(item, (str, bool, int, float)) or item is None)}
    if isinstance(parsed.get("files"), list):
        result["files"] = [json.loads(_summary({key:value for key,value in item.items() if key != "files"}))
                           for item in parsed["files"][:100] if isinstance(item, dict)]
    result["_preview"] = "展开加载完整结果"
    return json.dumps(result, ensure_ascii=False)


def _project(raw, number, revision):
    row = json.loads(raw)
    if row.get("event") != "chat_round" or not isinstance(row.get("events"), list):
        return raw
    changed = False
    for event_index, event in enumerate(row["events"]):
        if not isinstance(event, dict) or event.get("role") != "tool" or event.get("tool_name") in {"ask_user", "sub_agent"}:
            continue
        result, diff = event.get("result"), event.get("file_diff")
        if len(json.dumps([result, diff], ensure_ascii=False).encode("utf-8")) <= _DETAIL_BYTES:
            continue
        event["result"] = _summary(result)
        if isinstance(diff, dict):
            event["file_diff"] = {key:diff[key] for key in ("path","lines_added","lines_removed") if key in diff}
        event["_history_detail"] = {"round": number, "event_index": event_index, "revision":revision}
        changed = True
    return json.dumps(row,ensure_ascii=False) if changed else raw


@dataclass(frozen=True)
class Index:
    fingerprint: tuple
    revision: str
    header: tuple | None
    units: tuple
    questions: tuple
    rounds: int
    weight: int


def _build(path, wanted):
    units, questions, windows, loose = [], [], [], []
    header = None
    rounds = 0
    content_digest = hashlib.sha256()
    with path.open("rb") as stream:
        if _fingerprint(os.fstat(stream.fileno())) != wanted:
            raise _changed("index-open")
        line_number = 0
        while True:
            offset = stream.tell()
            raw = stream.readline()
            if not raw:
                break
            try:
                row = json.loads(raw.decode("utf-8-sig"))
            except (UnicodeError, ValueError, RecursionError):
                line_number += 1
                continue
            if isinstance(row, dict):
                if line_number == 0 and isinstance(row.get("_meta"), dict):
                    header = offset, len(raw)
                    content_digest.update(json.dumps(row["_meta"].get("usage", {}), sort_keys=True).encode())
                elif row.get("event") == "chat_round" and isinstance(row.get("events"), list):
                    content_digest.update(raw)
                    rounds += 1
                    units.append((offset, len(raw), rounds))
                    events = row["events"]
                    windows.append((rounds, _timestamp(row.get("started_at") or (events[0].get("timestamp") if events and isinstance(events[0], dict) else None)),
                                    _timestamp(row.get("ended_at") or (events[-1].get("timestamp") if events and isinstance(events[-1], dict) else None))))
                    for number, event in enumerate(events):
                        if isinstance(event, dict) and event.get("role") == "user":
                            questions.append({"round": rounds, "event_index": number, "text": _text(event.get("content"))})
                elif row.get("event") == "context_compaction":
                    content_digest.update(raw)
                    anchor = row.get("display_round")
                    if not isinstance(anchor, int) or isinstance(anchor, bool) or anchor < 1:
                        anchor = None
                    loose.append((offset, len(raw), anchor, max(1, rounds), _timestamp(row.get("timestamp")), row.get("scope")))
            line_number += 1
        if _fingerprint(os.fstat(stream.fileno())) != wanted or _fingerprint(path.stat()) != wanted:
            raise _changed("index-read")
    for offset, length, anchor, physical, timestamp, scope in loose:
        if anchor is None and scope == "session" and timestamp is not None:
            anchor = next((number for number, start, end in reversed(windows)
                           if start is not None and end is not None and start <= timestamp < end), None)
        units.append((offset, length, min(max(1, rounds), anchor or physical)))
    units.sort()
    # Header-only changes (title, context diagnostics, tool selection) do not
    # invalidate identical round content. Offsets still follow the full file
    # fingerprint and are rebuilt after every physical rewrite.
    revision = content_digest.hexdigest()
    weight = len(units) * 192 + sum(256 + len(q["text"]) * 4 for q in questions)
    return Index(wanted, revision, header, tuple(units), tuple(questions), rounds, weight)


def _index(path):
    wanted = _fingerprint(path.stat())
    key = str(path)
    with _LOCK:
        cached = _CACHE.get(key)
        if cached and cached.fingerprint == wanted:
            _CACHE.move_to_end(key)
            return cached
    index = _build(path, wanted)
    with _LOCK:
        _CACHE.pop(key, None)
        if index.weight <= _CACHE_BYTES:
            _CACHE[key] = index
        while len(_CACHE) > 8 or sum(i.weight for i in _CACHE.values()) > _CACHE_BYTES:
            _CACHE.popitem(last=False)
    return index


def page(session_id, *, before=None, around=None, limit=20, revision=None, include_questions=True):
    from memory import chat_memory
    from security.authorization import session
    sid = session(session_id)
    path = chat_memory._get_chat_history_file(sid)
    root = Path(chat_memory.HISTORY_ROOT).resolve()
    try:
        relative = path.absolute().relative_to(root)
        current = root
        for part in relative.parts:
            current /= part
            if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
                raise HTTPException(404, "会话不存在或无权访问")
        index = _index(path)
        if revision and revision != index.revision:
            raise _changed()
        limit = max(1, min(100, int(limit)))
        if around is not None:
            target = max(1, min(index.rounds, int(around)))
            start = max(1, target - limit // 2)
            end = min(index.rounds, start + limit - 1)
        else:
            end = min(index.rounds, int(before)-1 if before is not None else index.rounds)
            start = max(1, end-limit+1)
            target = end
        # Respect a soft byte budget at whole-round boundaries; a single large
        # round remains intact instead of silently dropping tool/body content.
        sizes = {}
        for _, length, number in index.units:
            sizes[number] = sizes.get(number, 0) + length
        while start < end and sum(sizes.get(n, 0) for n in range(start, end+1)) > _PAGE_BYTES:
            if target-start >= end-target:
                start += 1
            else:
                end -= 1
        selected = [(offset, length, number) for offset, length, number in index.units if start <= number <= end or index.rounds == 0]
        if index.header:
            selected.insert(0, (*index.header, None))
        chunks = []
        with path.open("rb") as stream:
            if _fingerprint(os.fstat(stream.fileno())) != index.fingerprint:
                raise _changed("page-open")
            for offset, length, number in selected:
                stream.seek(offset)
                raw = stream.read(length).decode("utf-8-sig").rstrip("\r\n")
                chunks.append(_project(raw, number, index.revision) if number is not None else raw)
            if _fingerprint(os.fstat(stream.fileno())) != index.fingerprint or _fingerprint(path.stat()) != index.fingerprint:
                raise _changed("page-read")
        # Always provide a header for the parser; no disk format is changed.
        if not index.header:
            chunks.insert(0, '{"_meta":{}}')
        from security.redaction import history
        result = {"session_id": sid, "revision": index.revision, "text": history("\n".join(chunks)+"\n"),
                  "round_offset": max(0, start-1), "start_round": start if index.rounds else 0,
                  "end_round": end, "total_rounds": index.rounds, "has_older": start > 1,
                  "has_newer": end < index.rounds}
        if include_questions:
            result["questions"] = list(index.questions)
        return result
    except FileNotFoundError:
        raise HTTPException(404, "Chat session file not found") from None


def detail(session_id, round_no, event_index, revision):
    from memory import chat_memory
    from security.authorization import session
    sid = session(session_id)
    # Reuse page's path/junction and current-snapshot checks, without requesting
    # or returning another round's tool data.
    snapshot = page(sid, around=round_no, limit=1, revision=revision, include_questions=False)
    if snapshot["start_round"] != round_no:
        raise HTTPException(404, "工具记录不存在")
    path = chat_memory._get_chat_history_file(sid)
    index = _index(path)
    if index.revision != revision:
        raise _changed()
    with path.open("rb") as stream:
        if _fingerprint(os.fstat(stream.fileno())) != index.fingerprint:
            raise _changed()
        for offset,length,number in index.units:
            if number != round_no:
                continue
            stream.seek(offset)
            row = json.loads(stream.read(length).decode("utf-8-sig"))
            if row.get("event") != "chat_round":
                continue
            events = row.get("events", [])
            if not 0 <= event_index < len(events) or not isinstance(events[event_index], dict) or events[event_index].get("role") != "tool":
                raise HTTPException(404, "工具记录不存在")
            if _fingerprint(os.fstat(stream.fileno())) != index.fingerprint or _fingerprint(path.stat()) != index.fingerprint:
                raise _changed()
            return {"event": events[event_index], "revision":revision}
    raise HTTPException(404, "工具记录不存在")
