"""One path authority for account-scoped session data.

有身份时所有会话统一使用账号目录布局：
  history_files/users/<owner>/sessions/<sid>/<sid>_chat.jsonl
  history_files/users/<owner>/sessions/<sid>/files/ | uploads/<upload_id>/
  history_files/users/<owner>/sessions/<sid>/sidecars/
归属唯一真源是 session_catalog；无登记的 ID 直接拒绝。
无身份离线库调用回退根目录布局（单用户，无归属概念）。
"""
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
import re

_SYSTEM = ContextVar("storage_system_identity", default=None)
_REQUIRED = ContextVar("storage_identity_required", default=False)


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
        return None
    if principal and principal.store is store:
        cache = principal.resource_cache.setdefault("session_catalog", {})
        key = store.resource_key(sid)
        if key not in cache:
            cache[key] = get(store, sid)
        return cache[key]
    return get(store, sid)


def safe_session_id(value):
    """会话 ID 清洗唯一真源：保留 Unicode 词字符/空格/点/横线，其余转下划线并拦截 `..`。"""
    return re.sub(r"[^\w .-]+", "_", str(value)).replace("..", "_").strip(" ._-")


def session_root(root, sid, *, store=None):
    store = store or identity()
    row = _row(sid, store)
    if row is None:
        if store is not None:
            raise ValueError("会话未登记，拒绝访问")
        return None
    owner = row["owner"]
    if not re.fullmatch(r"[0-9a-f]{32}", owner):
        raise ValueError("Invalid account storage identifier")
    return Path(root) / "users" / owner / "sessions" / safe_session_id(row["id"])


def chat_path(root, sid, *, store=None):
    store = store or identity()
    row = _row(sid, store)
    if row is None:
        if store is not None:
            raise ValueError("会话未登记，拒绝访问")
        return Path(root) / (safe_session_id(sid) + "_chat.jsonl")
    directory = Path(root) / "users" / row["owner"] / "sessions" / safe_session_id(row["id"])
    if not re.fullmatch(r"[0-9a-f]{32}", row["owner"]):
        raise ValueError("Invalid account storage identifier")
    return directory / (safe_session_id(row["id"]) + "_chat.jsonl")


def sidecar_path(file_path):
    return Path(file_path).parent / "sidecars"


def files_path(root, sid, *, upload_id=None, store=None):
    store = store or identity()
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
    upload_id = safe_session_id(upload_id or sid)
    if directory is None:
        return Path(root) / "session_files" / upload_id
    canonical = safe_session_id(_row(sid, store)["id"])
    return directory / "files" if upload_id == canonical else directory / "uploads" / upload_id


def protocol_path(root, sid, protocol, *, store=None):
    import hashlib
    directory = session_root(root, sid, store=store)
    if directory is None:
        return Path(root) / "sidecars" / protocol / hashlib.sha256(str(sid).encode()).hexdigest()
    return directory / "sidecars" / protocol


def iter_chat_paths(root, *, store=None, include_blocked=False):
    from security.principal import current
    from memory.session_catalog import rows, BLOCKED
    principal = current()
    store = store or identity()
    if store is None:
        return [p for p in Path(root).glob("*.jsonl") if p.is_file() and not p.is_symlink()]
    result = {}
    scope = principal.user_id if principal and store is principal.store else None
    for row in rows(store, scope):
        if include_blocked or row["state"] not in BLOCKED:
            path = chat_path(root, row["id"], store=store)
            if path.is_file():
                result[path.resolve()] = path
    return list(result.values())
