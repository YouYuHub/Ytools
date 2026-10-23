"""Bounded, short-lived result snapshots. Cursors never contain filesystem paths."""
from collections import OrderedDict
import hashlib
import json
import re
import threading
import time
import uuid

_cache = OrderedDict()
_lock = threading.Lock()
_TTL = 300
_MAX_WEIGHT = 32 * 1024 * 1024
MAX_SNAPSHOT_CHARS = 2 * 1024 * 1024


def _signature(args):
    from .permission_runtime import CURRENT
    current = CURRENT.get()
    from pathlib import Path
    query = {k: v for k, v in args.items() if not k.startswith("_") and k not in {"cursor", "max_results"}}
    defaults = {'pattern': '*', 'file_pattern': '', 'ignore_case': False, 'is_regex': False,
                'context_lines': 0, 'max_depth': 6, 'max_scanned_files': 3000,
                'max_scan_seconds': 10, 'max_file_mb': 2, 'exclude_dirs': [], 'no_default_excludes': False,
                'count_total': False, 'respect_gitignore': False}
    query = {key: value for key, value in query.items() if key not in defaults or value != defaults[key]}
    query["dir_path"] = str(Path(args.get("dir_path") or ".").expanduser().resolve())
    return hashlib.sha256(json.dumps([current.session if current else "", query],
                                    sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _prune():
    now = time.monotonic()
    for key in list(_cache):
        if _cache[key]["expires"] < now:
            _dispose(_cache.pop(key))
    while len(_cache) > 16 or sum(v["weight"] for v in _cache.values()) > _MAX_WEIGHT:
        _dispose(_cache.popitem(last=False)[1])


def _dispose(snapshot):
    producer = snapshot.get('producer')
    if producer:
        if not producer.exhausted:
            snapshot['metadata'].update(capture_truncated=True, scan_pending=False)
        try:
            producer.close()
        except ValueError:  # a concurrently running page will release its last reference shortly
            pass


def resume(args, *, files=False):
    cursor = args.get("cursor")
    if not cursor:
        return None
    if not isinstance(cursor, str) or not re.fullmatch(r"[a-f0-9]{32}:\d{1,8}:\d{1,10}", cursor):
        raise ValueError("cursor 无效；请使用上次返回的 next_cursor")
    key, index, offset = cursor.split(":")
    with _lock:
        _prune()
        snapshot = _cache.get(key)
        if not snapshot or snapshot['files'] != files or snapshot["signature"] != _signature(args):
            raise ValueError("CURSOR_EXPIRED：结果已过期或查询改变，请重新搜索")
        _cache.move_to_end(key)
    from .search_ignore import unchanged as version_unchanged
    from .permission_runtime import guard_path
    from .tool_control import check_tool_control
    with snapshot['lock']:
        for path, version in snapshot["versions"].items():
            check_tool_control()
            if not guard_path(path, search=True):
                raise PermissionError("搜索结果路径权限已改变，请重新搜索")
            try:
                unchanged = version_unchanged(path, version)
            except OSError:
                unchanged = False
            if not unchanged:
                raise ValueError("FILE_QUERY_CHANGED：搜索范围已变化，请移除 cursor 重新搜索")
        return _locked_page(key, snapshot, args, int(index), int(offset))


def create(args, items, metadata, versions, *, files=False, producer=None):
    key = uuid.uuid4().hex
    weight = sum(len(item) * 4 + 64 for item in items) + len(versions) * 512
    snapshot = {"signature": _signature(args), "items": list(items), "metadata": metadata,
                "versions": versions if producer else dict(versions), "files": files, "weight": weight,
                "producer": producer, "lock": threading.RLock(), "chars": sum(map(len, items)),
                "expires": time.monotonic() + _TTL}
    with _lock:
        _cache[key] = snapshot
        _prune()
    return _page(key, snapshot, args, 0, 0)


def _page(key, snapshot, args, index, offset):
    with snapshot['lock']:
        return _locked_page(key, snapshot, args, index, offset)


def _locked_page(key, snapshot, args, index, offset):
    from util.tool_arguments import int_arg
    budget = max(512, min(60000, int(args.get("_max_chars") or 60000)))
    limit = int_arg(args, "max_results", 50, minimum=1, maximum=200)
    items = snapshot["items"]
    if index > len(items) or index == len(items) and offset or index < len(items) and offset >= len(items[index]):
        raise ValueError("cursor 位置无效，请重新搜索")
    producer = snapshot.get('producer')
    if producer and not producer.exhausted:
        needed, space = limit, budget - 400
        for item in items[index:]:
            needed -= 1
            space -= len(item) - (offset if needed == limit - 1 else 0)
        while needed > 0 and space > 0 and not producer.exhausted:
            item = producer.next()
            if item is None:
                break
            if snapshot['chars'] + len(item) > MAX_SNAPSHOT_CHARS:
                snapshot['metadata'].update(capture_truncated=True, scan_pending=False)
                producer.close()
                break
            items.append(item)
            snapshot['chars'] += len(item)
            needed -= 1
            space -= len(item)
        snapshot['weight'] = snapshot['chars'] * 4 + len(snapshot['versions']) * 512 + producer.buffer_chars * 4
        with _lock:
            _prune()
    if index > len(items) or index == len(items) and offset or index < len(items) and offset >= len(items[index]):
        raise ValueError("cursor 位置无效，请重新搜索")
    data = dict(snapshot["metadata"])
    prefix = data.pop("_prefix", "")[:max(0, budget - 320)]
    room = budget - len(prefix) - 220
    chunks, paths = [], []
    consumed = 0
    while index < len(items) and consumed < limit and room > 0:
        item = items[index]
        continuation = "[同一区块续读]\n" + item.split("\n", 1)[0][:min(80, room // 3)] + "\n" if offset else ""
        if snapshot["files"]:
            if len(item) + 1 > room:
                if not chunks:
                    raise ValueError("单条路径超过当前结果预算；请缩短 dir_path 或增大模型上下文")
                break
            chunk = item
        else:
            available = room - len(continuation) - 1
            if available <= 0:
                break
            end = min(len(item), offset + available)
            if end < len(item):
                newline = item.rfind("\n", offset + 1, end)
                if newline > offset:
                    end = newline + 1
            chunk = continuation + item[offset:end]
        chunks.append(chunk)
        if snapshot["files"]:
            paths.append(item)
        advance = len(chunk) - len(continuation)
        offset += advance
        room -= len(chunk) + 1
        consumed += 1
        if offset >= len(item):
            index += 1
            offset = 0
        else:
            break
    more = index < len(items) or bool(producer and not producer.exhausted)
    shown = sum(line.startswith(">") for chunk in chunks for line in chunk.splitlines())
    summary = (f"[本页 {len(paths)} 个路径]" if snapshot["files"] else
               f"[展示 {shown}/{data.get('total_matches', 0)} 处命中；统计仅覆盖已扫描部分。]")
    empty = ("（无匹配结果）" if data.get("scan_complete", True) else "（已扫描部分无匹配；不能判定不存在）") if not items else ""
    data.update(message="\n".join(filter(None, [prefix, summary, empty, *(chunks if not snapshot['files'] else []),
                '[捕获结果达到上限；请缩小范围或用 read_file 读取原文]' if data.get('capture_truncated') else '',
                "[用同样查询及 next_cursor 续读]" if more else ""])),
                has_more=more, next_cursor=f"{key}:{index}:{offset}" if more else None,
                truncated=more or not data.get("scan_complete", True) or bool(data.get("capture_truncated")),
                output_truncated=more or bool(data.get("capture_truncated")), displayed_matches=shown, displayed_blocks=len(chunks))
    if snapshot["files"]:
        data["files"] = paths
    return data
