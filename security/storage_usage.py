"""Exact account disk measurements with one metadata walk, without content reads.

Do not cache totals across requests: checkpoints, protocol journals and trusted
host tools can change files outside the large-upload admission lock.
"""
import os
from pathlib import Path
import stat
import time


def _paths(store, user_id):
    from memory.chat_memory import HISTORY_ROOT
    from memory.session_catalog import rows
    from memory.storage_layout import chat_path, files_path, protocol_path, sidecar_path
    from prompt.prompt_manager import MD_FILES_DIR
    from memory.task_template_store import ROOT as templates
    root = Path(HISTORY_ROOT)
    yield root / "users" / user_id
    yield MD_FILES_DIR / user_id
    yield templates / user_id
    for row in rows(store, user_id):
        if row["layout"]:
            continue
        sid = row["id"]
        history = chat_path(root, sid, store=store)
        yield history
        yield files_path(root, sid, store=store)
        yield from sidecar_path(root, history).glob(history.name + ".*")
        for protocol in ("messages", "responses"):
            yield protocol_path(root, sid, protocol, store=store)


def measure(store, user_id):
    started = time.perf_counter()
    total = files = 0
    seen = set()

    def accept(path, info):
        nonlocal total, files
        # Reject reparse points as well as symlinks, including Windows junctions.
        if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
            return False
        key = os.path.normcase(os.path.abspath(path))
        if key in seen:
            return False
        seen.add(key)
        if stat.S_ISDIR(info.st_mode):
            return True
        if stat.S_ISREG(info.st_mode) and not os.path.basename(path).endswith((".tmp", "-shm")):
            total += info.st_size
            files += 1
        return False

    pending = []
    for path in _paths(store, user_id):
        try:
            if accept(path, os.stat(path, follow_symlinks=False)):
                pending.append(path)
        except FileNotFoundError:
            pass
    while pending:
        path = pending.pop()
        try:
            with os.scandir(path) as entries:
                for entry in entries:
                    try:
                        if accept(entry.path, entry.stat(follow_symlinks=False)):
                            pending.append(entry.path)
                    except FileNotFoundError:
                        pass  # Concurrent atomic replacement/deletion; never hide access errors.
        except FileNotFoundError:
            pass
    from util.api_timing import mark
    mark("storage_checked", storage_scan_ms=round((time.perf_counter()-started)*1000, 3), storage_files=files)
    return total
