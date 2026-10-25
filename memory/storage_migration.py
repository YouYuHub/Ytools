"""Offline, journaled, same-volume account directory migration."""
import json
import os
from pathlib import Path
import uuid

from memory import session_catalog as catalog
from memory.storage_layout import _safe, protocol_path
from memory.storage_backup import digest
from util.atomic_json import write_json


def _inside(root, path):
    root, raw = Path(root).resolve(), Path(path).absolute()
    path = raw.resolve()
    if path == root or not path.is_relative_to(root) or not raw.is_relative_to(root):
        raise ValueError("Migration path escaped the managed root")
    for item in [raw, *raw.parents]:
        if item == root:
            break
        if item.is_symlink() or getattr(item, "is_junction", lambda: False)():
            raise ValueError("Migration cannot follow symlinks or junctions")
    return path


def _fingerprint(path):
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        raise ValueError("Migration cannot follow symlinks")
    if path.is_file():
        return {".": digest(path)}
    result = {}
    for item in path.rglob("*"):
        if item.is_symlink() or getattr(item, "is_junction", lambda: False)() or not item.resolve().is_relative_to(path.resolve()):
            raise ValueError("Migration cannot follow symlinks")
        if item.is_file():
            result[item.relative_to(path).as_posix()] = digest(item)
    return result


def plan(store, root):
    root = Path(root).resolve()
    result, claimed = [], set()
    for row in catalog.rows(store):
        if row["layout"] or row["state"] in catalog.BLOCKED:
            continue
        sid = row["id"]
        if _safe(sid) != sid or not __import__("re").fullmatch("[0-9a-f]{32}", row["owner"]):
            raise ValueError("Invalid catalog storage key")
        destination = root / "users" / row["owner"] / "sessions" / sid
        if destination.exists():
            raise ValueError("迁移目标已存在，需先对账，未覆盖任何文件")
        history = root / (sid + "_chat.jsonl")
        upload_id = sid
        if history.exists():
            with history.open("rb") as stream:
                first = stream.readline(2 * 1024 * 1024 + 1)
            if len(first) <= 2 * 1024 * 1024:
                try:
                    upload_id = _safe(json.loads(first.decode("utf-8-sig")).get("_meta", {}).get("upload_id") or sid)
                except (ValueError, AttributeError):
                    pass
        pairs = [(history, destination / history.name),
                 (root / "session_files" / upload_id,
                  destination / "files" if upload_id == sid else destination / "uploads" / upload_id)]
        for protocol in ("messages", "responses"):
            pairs.append((protocol_path(root, sid, protocol, store=store), destination / "sidecars" / protocol))
        for source in (root / "sidecars").glob(history.name + ".*"):
            pairs.append((source, destination / "sidecars" / source.name))
        moves = []
        for source, target in pairs:
            source, target = _inside(root, source), _inside(root, target)
            if not source.exists():
                continue
            if source in claimed:
                raise ValueError("多个会话共享物理附件目录，需人工核实，拒绝自动迁移")
            claimed.add(source)
            moves.append({"source": str(source.relative_to(root)), "target": str(target.relative_to(root))})
        result.append({"id": sid, "moves": moves, "state": row["state"]})
    return result


def rollback(store, root, journal_path):
    root = Path(root).resolve()
    journal_path = _inside(root, journal_path)
    record = json.loads(journal_path.read_text(encoding="utf-8"))
    if record.get("status") == "committed":
        raise ValueError("已提交迁移不能自动回滚；请从完整备份恢复到新目录")
    for move in reversed(record["moves"]):
        source, target = (_inside(root, root / move[key]) for key in ("source", "target"))
        if target.exists():
            if source.exists() or _fingerprint(target) != move["hashes"]:
                raise ValueError("Migration rollback conflict; manual intervention required")
            source.parent.mkdir(parents=True, exist_ok=True)
            os.replace(target, source)
    catalog.set_state(store, record["id"], record["state"], layout=0)
    record["status"] = "rolled_back"
    write_json(journal_path, record)


def migrate(store, root, *, apply=False):
    root = Path(root).resolve()
    journal_root = root / "state/storage-migrations"
    for journal in journal_root.glob("*.json"):
        if json.loads(journal.read_text(encoding="utf-8"))["status"] == "pending":
            raise ValueError("存在中断迁移，请先 rollback-journal：" + str(journal))
    work = plan(store, root)
    if not apply:
        return {"sessions": len(work), "moves": sum(len(row["moves"]) for row in work), "applied": False}
    completed = 0
    for record in work:
        for move in record["moves"]:
            move["hashes"] = _fingerprint(_inside(root, root / move["source"]))
        record["status"] = "pending"
        journal = journal_root / (uuid.uuid4().hex + ".json")
        write_json(journal, record)
        try:
            for move in record["moves"]:
                source, target = (_inside(root, root / move[key]) for key in ("source", "target"))
                if target.exists():
                    raise ValueError("Migration target appeared concurrently")
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source, target)
                if _fingerprint(target) != move["hashes"]:
                    raise ValueError("Migration checksum mismatch")
            catalog.set_state(store, record["id"], record["state"], layout=1)
            record["status"] = "committed"
            write_json(journal, record)
            completed += 1
        except BaseException:
            record["status"] = "pending"
            write_json(journal, record)
            rollback(store, root, journal)
            raise
    return {"sessions": completed, "applied": True}
