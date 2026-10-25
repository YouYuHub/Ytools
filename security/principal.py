"""Trusted execution identity; browser fields never construct this object."""
from contextvars import ContextVar
from dataclasses import dataclass, field

CURRENT = ContextVar("ytools_principal", default=None)


@dataclass(frozen=True)
class Principal:
    user_id: str
    username: str
    role: str
    login_id: str
    auth_version: int
    mfa_verified: bool
    csrf: str
    store: object
    recent_auth: float = 0
    password_change_required: bool = False
    resource_cache: dict = field(default_factory=dict, compare=False, repr=False)

    @property
    def admin(self):
        return self.role == "admin"


def current():
    return CURRENT.get()


def require_admin():
    from fastapi import HTTPException
    principal = current()
    if principal is None or not principal.admin:
        raise HTTPException(403, "需要管理员权限")
    return principal
