"""One path authority for legacy and account-scoped session data.

The catalog chooses a layout; filesystem presence alone never changes ownership.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
import os
import re

_SYSTEM = ContextVar("storage_system_identity", default=None)
_REQUIRED = ContextVar("storage_identity_required", default=False)
_STORE = None


def install(store):
    global _STORE
    _STORE = store


@contextmanager
def system_access(store, *, reason):
    if not reason or not reason.strip():
        raise ValueError("System storage access needs a reason")
    token = _SYSTEM.set(store)
    try:
        yield
    finally:
        _SYSTEM.reset(token)


def identity():
    from security.principal import current
    principal = current()
    if principal:
        return principal.store
    store = _SYSTEM.get()
    if store is None and _REQUIRED.get():
        raise PermissionError("Storage access requires a user or explicit system identity")
    return store


def require_identity():
    return identity()


def _row(sid, store=None):
    from security.principal import current
    from memory.session_catalog import get
    principal = current()
    store = store or identity()
    if store is None:
        # Standalone legacy library use remains compatible; production request
        # scopes explicitly require an identity, including background workers.
        return None
    if principal and principal.store is store:
        cache = principal.resource_cache.setdefault("session_catalog", {})
        key = store.resource_key("session", sid)
        if key not in cache:
            cache[key] = get(store, sid)
        return cache[key]
    return get(store, sid)


def _safe(value):
    return re.sub(r"[^\w .-]+", "_", str(value)).replace("..", "_").strip(" ._-")


def session_root(root, sid, *, store=None):
    row = _row(sid, store)
    if row and row["layout"] == 1:
        owner = row["owner"]
        if not re.fullmatch(r"[0-9a-f]{32}", owner):
            raise ValueError("Invalid account storage identifier")
        directory = Path(root) / "users" / owner / "sessions"
        return _existing_alias(directory, _safe(row["id"]))
    return None


def chat_path(root, sid, *, store=None):
    row = _row(sid, store)
    canonical = row["id"] if row else sid
    directory = session_root(root, sid, store=store) or Path(root)
    return _existing_alias(directory, _safe(canonical) + "_chat.jsonl")


def _existing_alias(directory, name):
    path = directory / name
    if os.name != "nt" or path.exists() or not directory.is_dir():
        return path
    # Older account paths used the submitted casing, including Unicode folds
    # that NTFS does not treat as aliases. Read them without moving user data.
    matches = [p for p in directory.iterdir() if p.name.casefold() == name.casefold()]
    if len(matches) > 1:
        raise ValueError("会话目录存在大小写冲突，请先对账")
    return matches[0] if matches else path


def sidecar_path(root, file_path):
    path = Path(file_path)
    return (path.parent / "sidecars") if path.parent != Path(root) else Path(root) / "sidecars"


def files_path(root, sid, *, legacy_root=None, upload_id=None, store=None):
    directory = session_root(root, sid, store=store)
    if upload_id is None:
        import json
        try:
            with chat_path(root, sid, store=store).open("rb") as stream:
                first = stream.readline(2 * 1024 * 1024 + 1)
            if len(first) <= 2 * 1024 * 1024:
                upload_id = json.loads(first.decode("utf-8-sig")).get("_meta", {}).get("upload_id")
        except (OSError, ValueError, AttributeError):
            pass
    upload_id = _safe(upload_id or sid)
    if directory:
        canonical = _safe(_row(sid, store)["id"])
        same = upload_id.casefold() == canonical.casefold() if os.name == "nt" else upload_id == canonical
        return directory / "files" if same else directory / "uploads" / upload_id
    _check_legacy_upload_owner(root, sid, upload_id, store)
    return Path(legacy_root or Path(root) / "session_files") / upload_id


def _check_legacy_upload_owner(root, sid, upload_id, store):
    """Fail closed when legacy headers assign one upload folder to two owners.

    Read only catalog-owned header metadata for this authorization decision;
    never return another account's names or content to the caller.
    """
    from security.principal import current
    principal = current()
    if principal is None:
        return
    store = store or principal.store
    row = _row(sid, store)
    if row is None:
        return
    cache_key = ("legacy_upload_owners", str(Path(root).resolve()))
    owners = principal.resource_cache.get(cache_key)
    if owners is None:
        import json
        from memory.session_catalog import rows
        owners = {}
        for entry in rows(store):
            key = store.resource_key("session", _safe(entry["id"]))
            owners.setdefault(key, set()).add(entry["owner"])
            if entry["layout"] != 0:
                continue
            path = _existing_alias(Path(root), _safe(entry["id"]) + "_chat.jsonl")
            if path.is_symlink():
                continue
            try:
                with path.open("rb") as stream:
                    first = stream.readline(2 * 1024 * 1024 + 1)
                if len(first) > 2 * 1024 * 1024:
                    continue
                meta = json.loads(first.decode("utf-8-sig")).get("_meta", {})
                upload = meta.get("upload_id") if isinstance(meta, dict) else None
                if isinstance(upload, str) and _safe(upload):
                    owners.setdefault(store.resource_key("session", _safe(upload)), set()).add(entry["owner"])
            except (OSError, ValueError, AttributeError):
                continue
        principal.resource_cache[cache_key] = owners
    if owners.get(store.resource_key("session", upload_id), set()) - {row["owner"]}:
        from memory.identity_store import Denied
        raise Denied("旧会话附件归属存在冲突，请管理员对账；不能读取或分享该目录")


def protocol_path(root, sid, protocol, *, store=None):
    import hashlib
    directory = session_root(root, sid, store=store)
    return directory / "sidecars" / protocol if directory else (
        Path(root) / "sidecars" / protocol / hashlib.sha256(str(sid).encode()).hexdigest())


def iter_chat_paths(root, *, store=None, include_blocked=False):
    from security.principal import current
    from memory.session_catalog import rows, BLOCKED
    principal = current()
    store = store or identity()
    result = {p.resolve(): p for p in Path(root).glob("*.jsonl")}
    if store:
        for row in rows(store, principal.user_id if principal and store is principal.store else None):
            if include_blocked or row["state"] not in BLOCKED:
                path = chat_path(root, row["id"], store=store)
                if path.is_file():
                    result[path.resolve()] = path
    return list(result.values())


def remap_legacy_attachment(path):
    """Read-only compatibility for absolute attachment paths in old messages."""
    from memory.chat_memory import HISTORY_ROOT
    from memory.session_catalog import rows
    from security.principal import current
    path = Path(path).expanduser()
    if path.exists():
        return path
    try:
        relative = path.resolve().relative_to((HISTORY_ROOT / "session_files").resolve())
    except ValueError:
        return path
    if len(relative.parts) < 2:
        return path
    store, principal = identity(), current()
    if store is None:
        return path
    for row in rows(store, principal.user_id if principal else None):
        if row["layout"] != 1 or row["state"] != "active":
            continue
        directory = files_path(HISTORY_ROOT, row["id"], store=store)
        # Migrated sessions retain the original upload_id in their header.
        upload_id = directory.name if directory.parent.name == "uploads" else _safe(row["id"])
        if relative.parts[0] == upload_id:
            target = directory.joinpath(*relative.parts[1:])
            if target.resolve().is_relative_to(directory.resolve()):
                return target
    return path
