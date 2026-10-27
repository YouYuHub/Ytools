"""Offline, streaming managed-data backups and non-destructive restore drills."""
import contextlib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import socket
import sqlite3
import tempfile
import time
import zipfile

from security.local_credentials import private_file
from util.file_lock import cross_process_lock


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def assert_idle(root, origin=None):
    from memory.permission_store import pid_alive
    path = Path(root) / "history_files/state/permission_runtime.sqlite3"
    if path.exists():
        with contextlib.closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            if any(pid_alive(row[0]) for row in db.execute("SELECT owner FROM tasks WHERE state='running'")):
                raise RuntimeError("仍有存活任务，拒绝维护")
    if origin:
        from urllib.parse import urlsplit
        url = urlsplit(origin)
        # The configured public origin may be a reverse proxy. Also probe the
        # local backend port; callers must stop all writers before maintenance.
        from config import DEFAULT_SERVICE_PORT
        for host, port in {("127.0.0.1", int(DEFAULT_SERVICE_PORT)),
                           (url.hostname, url.port or (443 if url.scheme == "https" else 80))}:
            if host not in {"127.0.0.1", "localhost", "::1"}:
                continue
            with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET) as probe:
                probe.settimeout(.3)
                if probe.connect_ex((host, port)) == 0:
                    raise RuntimeError("服务仍在监听，请先停止写入服务后维护")


@contextlib.contextmanager
def maintenance(root, origin=None):
    root = Path(root).resolve()
    with cross_process_lock(root / "history_files/lock/storage-maintenance.lock", timeout=0):
        assert_idle(root, origin)
        marker = root / "history_files/state/storage-maintenance.json"
        from util.atomic_json import write_json
        from util.process_identity import process_identity
        write_json(marker, {"pid": os.getpid(), "identity": process_identity(os.getpid()), "started": time.time()})
        try:
            yield
        finally:
            marker.unlink(missing_ok=True)


def check_maintenance(root):
    marker = Path(root) / "history_files/state/storage-maintenance.json"
    if marker.exists():
        from memory.permission_store import pid_alive
        from util.process_identity import process_identity
        record = json.loads(marker.read_text(encoding="utf-8"))
        if (pid_alive(record["pid"]) and (not record.get("identity") or
                                         record["identity"] == process_identity(record["pid"]))):
            raise RuntimeError("受管存储正在备份或迁移，暂不启动服务")


def inventory(root, security_dir):
    root, security_dir = Path(root).resolve(), Path(security_dir).resolve()
    paths = []
    for directory in (root / "history_files", root / "prompt/md_files", root / "setting"):
        if directory.exists():
            for item in directory.rglob("*"):
                if item.is_symlink() or getattr(item, "is_junction", lambda: False)():
                    raise ValueError("备份不跟随符号链接或目录联接")
                if item.is_file():
                    paths.append(item)
    if (root / ".env").is_file():
        paths.append(root / ".env")
    for path in paths:
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError("备份中存在符号链接或越界文件，需人工处理")
        if path.is_relative_to(security_dir):
            continue  # Never copy old bootstrap passwords or earlier backups.
        relative = path.relative_to(root).as_posix()
        if (relative.startswith("history_files/lock/") or path.name.endswith(("-wal", "-shm", "-journal"))
                or relative == "history_files/state/storage-maintenance.json"):
            continue
        yield relative, path
    for name in ("identity.sqlite3", "mfa.key"):
        path = security_dir / name
        if path.is_symlink():
            raise ValueError("Security snapshot cannot follow symlinks")
        if path.exists():
            yield "history_files/state/security/" + name, path


def _sqlite(path):
    with path.open("rb") as stream:
        return stream.read(16) == b"SQLite format 3\x00"


def security_fingerprint(database):
    queries = [
        "SELECT id,password_hash,role,enabled,auth_version,mfa_secret,must_change FROM users ORDER BY id",
        "SELECT id,owner,state FROM session_catalog ORDER BY id",
        "SELECT id,user_id,label,secret FROM mfa_devices ORDER BY id",
        "SELECT * FROM mfa_state ORDER BY user_id",
        "SELECT * FROM recovery_codes ORDER BY user_id,hash",
        "SELECT * FROM mfa_recovery_required ORDER BY user_id",
        "SELECT user_id,data FROM account_limits ORDER BY user_id",
    ]
    with contextlib.closing(sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True)) as db:
        rows = [db.execute(sql).fetchall() for sql in queries]
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='username_history'").fetchone():
            history = db.execute("SELECT * FROM username_history ORDER BY username").fetchall()
            if history:
                rows.append({"username_history": history})
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='user_preferences'").fetchone():
            preferences = db.execute("SELECT * FROM user_preferences ORDER BY user_id").fetchall()
            if preferences:
                rows.append({"user_preferences": preferences})
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()


def _secure_directory(target):
    if os.name != "nt":
        Path(target).chmod(0o700)
        return
    import subprocess
    import csv
    import re
    options = dict(capture_output=True, text=True, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
    sid = next(csv.reader(subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], **options).stdout.strip().splitlines()))[-1]
    if not re.fullmatch(r"S-1-(?:\d+-)*\d+", sid):
        raise RuntimeError("Cannot determine private directory owner")
    subprocess.run(["icacls", str(target), "/inheritance:r", "/grant:r", "*" + sid + ":(OI)(CI)F"], **options)
    # mkdtemp(0700) also installs explicit privileged ACEs on recent Python.
    subprocess.run(["icacls", str(target), "/remove:g", "*S-1-3-4", "*S-1-5-18", "*S-1-5-32-544"], **options)


def backup(root, security_dir, destination):
    root, destination = Path(root).resolve(), Path(destination).resolve()
    if destination.is_relative_to(root / "history_files"):
        raise ValueError("备份输出必须在受管数据目录之外")
    security_dir = Path(security_dir).resolve()
    with contextlib.closing(sqlite3.connect((security_dir / "identity.sqlite3").as_uri() + "?mode=ro", uri=True)) as db:
        needs_key = bool(db.execute("SELECT count(*) FROM users WHERE mfa_secret IS NOT NULL").fetchone()[0])
    if needs_key and not (security_dir / "mfa.key").is_file():
        raise ValueError("MFA accounts require their encryption key in the backup")
    manifest = {"version": 1, "created": time.time(), "files": [], "mfa_required": needs_key,
                "security_fingerprint": security_fingerprint(security_dir / "identity.sqlite3"),
                "scope": "Managed history, attachments, versions, state databases, prompts, settings and .env; external tool workspaces excluded",
                "security_directory": "history_files/state/security"}
    with tempfile.TemporaryDirectory() as temp, private_file(destination) as (private, _):
        _secure_directory(temp)
        with zipfile.ZipFile(private.buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True) as archive:
            for index, (name, source) in enumerate(inventory(root, security_dir)):
                database = _sqlite(source)
                if database:
                    snapshot = Path(temp) / str(index)
                    with contextlib.closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as src:
                        with contextlib.closing(sqlite3.connect(snapshot)) as dst:
                            src.backup(dst)
                    source = snapshot
                record = {"path": name, "size": source.stat().st_size, "sha256": digest(source), "sqlite": database}
                archive.write(source, name)
                manifest["files"].append(record)
                if database:
                    source.unlink()
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
    verify(destination)
    return {"files": len(manifest["files"]), "bytes": sum(f["size"] for f in manifest["files"]), "archive": str(destination)}


def _members(archive):
    if archive.getinfo("manifest.json").file_size > 64 * 1024 * 1024:
        raise ValueError("Backup manifest is too large")
    manifest = json.loads(archive.read("manifest.json"))
    if manifest.get("version") != 1 or not isinstance(manifest.get("files"), list):
        raise ValueError("Unsupported backup manifest")
    names, total = set(), 0
    for record in manifest["files"]:
        name = record["path"]
        path = PurePosixPath(name)
        if (not name or "\\" in name or ":" in name or path.is_absolute() or ".." in path.parts
                or name != path.as_posix() or name.casefold() in names or name == "manifest.json"):
            raise ValueError("Unsafe or duplicate archive path")
        names.add(name.casefold())
        entry = archive.getinfo(name)
        total += entry.file_size
        if entry.file_size != record["size"] or total > 1024 ** 4 or (entry.external_attr >> 16) & 0o170000 == 0o120000:
            raise ValueError("Invalid archive member")
    if len(archive.infolist()) != len(names) + 1:
        raise ValueError("Unexpected archive members")
    required = {"history_files/state/security/identity.sqlite3"}
    # An initialized backup is a set: the encrypted authenticators cannot be
    # restored independently of the key. Refuse incomplete security snapshots.
    identity_record = next((r for r in manifest["files"] if r["path"] in required), None)
    if identity_record is not None and not identity_record.get("sqlite"):
        raise ValueError("Identity snapshot must be SQLite")
    if manifest.get("mfa_required") and "history_files/state/security/mfa.key" not in names:
        raise ValueError("Backup is missing the MFA encryption key")
    return manifest


def verify(archive_path, restored=None):
    with zipfile.ZipFile(archive_path) as archive:
        manifest = _members(archive)
        for record in manifest["files"]:
            with archive.open(record["path"]) as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != record["sha256"]:
                    raise ValueError("Backup checksum mismatch: " + record["path"])
            if restored:
                path = Path(restored) / record["path"]
                if digest(path) != record["sha256"]:
                    raise ValueError("Restore checksum mismatch: " + record["path"])
                if record["sqlite"]:
                    with contextlib.closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
                        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                            raise ValueError("Restored SQLite integrity check failed")
        return {"files": len(manifest["files"]), "bytes": sum(r["size"] for r in manifest["files"]), "verified": True}


def restore(archive_path, target):
    target = Path(target).resolve()
    if target.exists():
        raise ValueError("恢复目标必须是全新的目录，禁止覆盖")
    # A private directory protects every extracted secret through inheritance.
    # Python 3.13+ interprets mkdir(0700) on Windows by adding explicit SYSTEM
    # and Administrators ACEs. Start with inherited permissions, then replace
    # them before extracting any data; children inherit the private root ACL.
    target.mkdir(mode=0o777 if os.name == "nt" else 0o700, parents=True)
    _secure_directory(target)
    with zipfile.ZipFile(archive_path) as archive:
        manifest = _members(archive)
        for record in manifest["files"]:
            path = target / record["path"]
            path.parent.mkdir(mode=0o777 if os.name == "nt" else 0o700, parents=True, exist_ok=True)
            with archive.open(record["path"]) as src, path.open("xb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
            if os.name != "nt":
                path.chmod(0o600)
    return verify(archive_path, target)
