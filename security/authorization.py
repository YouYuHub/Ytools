"""Object authorization shared by HTTP and storage entry points."""
from fastapi import HTTPException
from security.principal import current


def owned_sessions():
    principal = current()
    if principal is None:
        return None
    if "session" not in principal.resource_cache:
        principal.resource_cache["session"] = principal.store.owned(principal.user_id)
    return principal.resource_cache["session"]


def visible_session(identifier):
    principal = current()
    if principal is None:
        from memory.storage_layout import require_identity
        require_identity()
        return True
    from memory.storage_layout import _row
    from memory.session_catalog import BLOCKED
    row = _row(identifier)
    return principal.store.resource_key(identifier) in owned_sessions() and (not row or row["state"] not in BLOCKED)


def session(identifier, *, create=False, cleanup=False):
    from memory.storage_layout import require_identity
    require_identity()
    principal = current()
    if principal is None:
        return identifier  # Internal/library calls are not an HTTP auth boundary.
    from memory.chat_memory import normalize_session_id
    identifier = normalize_session_id(identifier)
    owner = principal.store.owner(identifier)
    if owner == principal.user_id:
        from memory.storage_layout import _row
        from memory.session_catalog import BLOCKED
        row = _row(identifier)
        if row and row["state"] in BLOCKED and not (cleanup and row["state"] in {"deleted", "cleanup_pending"}):
            raise HTTPException(404, "会话已删除或正在清理")
        return row["id"] if row else identifier
    if create and owner is None:
        # 会话 ID 全局唯一且永不释放；无登记即全新，直接单事务注册。
        from security.rate_limits import charge
        charge(principal.store, principal.user_id, "new-sessions", 120, 3600)
        if not principal.admin:
            with principal.store.database() as db:
                count = db.execute("SELECT count(*) FROM session_catalog WHERE owner=? AND state NOT IN ('deleted','cleanup_pending')",
                                   (principal.user_id,)).fetchone()[0]
            if count >= 10000:
                raise HTTPException(429, "账号会话数量已达上限，请联系管理员")
        principal.store.register(identifier, principal.user_id)
        principal.resource_cache.pop("session", None)
        return principal.store.resource_key(identifier)
    raise HTTPException(404, "会话不存在或无权访问")


def can_execute(principal=None):
    principal = principal or current()
    return principal is None or principal.admin or (
        principal.role == "operator" and principal.store.settings.trusted_operators)


def require_execution():
    principal = current()
    if not can_execute(principal):
        raise HTTPException(403, "当前账号未获执行能力；需要隔离执行器或明确授权的可信操作员")


def visible_group(identifier):
    principal = current()
    if principal is None:
        return True
    if "group_ids" not in principal.resource_cache:
        from memory.chat_memory import _read_session_groups_raw
        try:
            principal.resource_cache["group_ids"] = {g["id"] for g in _read_session_groups_raw()}
        except ValueError:
            principal.resource_cache["group_ids"] = set()
    return identifier in principal.resource_cache["group_ids"]


def require_group(identifier):
    if not visible_group(identifier):
        raise HTTPException(404, "分组不存在或无权访问")


def execution_active(session_id=None):
    """Background authorization survives logout but not account disable/role loss."""
    import time
    principal = current()
    if principal is None:
        return True
    cached = principal.resource_cache.get("execution_lease")
    now = time.monotonic()
    if cached and now - cached[0] < 1:
        return cached[1]
    try:
        user = principal.store.user(principal.user_id)
        allowed = bool(user and user["enabled"] and user["role"] == principal.role)
        if session_id:
            allowed = allowed and principal.store.owner(session_id) == principal.user_id
    except Exception:
        allowed = False
    principal.resource_cache["execution_lease"] = (now, allowed)
    return allowed


def tool_boundary(name, args, context):
    if current() is not None and (not can_execute() or not execution_active(context.get("session"))):
        raise PermissionError("账号或工作区执行授权已失效，不能继续执行工具")
