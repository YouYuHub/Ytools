"""Local accounts with opaque cookies; registration is deliberately absent."""
import asyncio
import time
from typing import Literal
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator
from memory.identity_store import Denied

from security.principal import current, require_admin

router = APIRouter()


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Credentials(Payload):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class Password(Payload):
    password: str = Field(min_length=1, max_length=128)


class Code(Payload):
    code: str = Field(min_length=1, max_length=64)


class Reauth(Password):
    code: str = Field(default="", max_length=64)


class MfaTarget(Payload):
    mode: Literal["add", "replace"] = "add"
    label: str = Field(default="Google Authenticator", min_length=1, max_length=32)

    @field_validator("label")
    @classmethod
    def valid_label(cls, value):
        from security.crypto import display_name
        return display_name(value)


class MfaChange(Reauth, MfaTarget):
    pass


class MfaLoginChange(Code, MfaTarget):
    pass


class MfaOperation(Payload):
    operation_id: str = Field(min_length=32, max_length=32, pattern="^[0-9a-f]+$")


class MfaComplete(MfaOperation):
    code: str = Field(min_length=6, max_length=6, pattern="^[0-9]+$")


class NewPassword(Payload):
    old_password: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=15, max_length=128)


class NewUsername(Reauth):
    username: str = Field(min_length=2, max_length=64)

    @field_validator("username")
    @classmethod
    def valid_username(cls, value):
        from security.crypto import username
        return username(value)


class NewUser(Credentials):
    password: str = Field(min_length=15, max_length=128)
    role: Literal["member", "operator", "admin"] = "member"
    name: str | None = Field(default=None, max_length=32)
    trusted: bool = False

    @field_validator("username")
    @classmethod
    def valid_username(cls, value):
        from security.crypto import username
        return username(value)

    @field_validator("name")
    @classmethod
    def valid_name(cls, value):
        from security.crypto import display_name
        return display_name(value) if value is not None else None


class AdminPassword(Payload):
    password: str = Field(min_length=15, max_length=128)


class AccountLimits(Payload):
    daily_calls: int = Field(ge=0, le=1000000, strict=True)
    daily_tokens: int = Field(ge=0, le=1000000000000, strict=True)
    storage_bytes: int | None = Field(default=None, ge=0, le=1000000000000000, strict=True)
    concurrent_models: int | None = Field(default=None, ge=0, le=1024, strict=True)


class ChangeUser(Payload):
    role: Literal["member", "operator", "admin"] | None = None
    enabled: bool | None = None
    trusted: bool = False


class ChatSession(Payload):
    session_id: str = Field(min_length=1, max_length=256)


class PreferencesPatch(Payload):
    ui: dict
    only_if_unset: StrictBool = False


@router.post("/chat_history/ensure_session")
async def ensure_session(payload: ChatSession):
    from security.authorization import session
    identifier = await asyncio.to_thread(session, payload.session_id, create=True)
    return {"session_id": identifier}


def principal():
    value = current()
    if value is None:
        raise HTTPException(401, "请先登录")
    return value


def recent_admin():
    value = require_admin()
    if not value.mfa_verified or time.time() - value.recent_auth > 300:
        raise HTTPException(403, "请先完成近期身份验证", headers={"X-Reauthenticate": "required"})
    return value


def payload_for(value):
    from security.preferences import snapshot
    return {"user": value.store.user(value.user_id), "csrf": value.csrf,
            "account_panel": True,
            "account_sharing": True,
            "history_paging": True,
            "preferences": snapshot(value) if value.mfa_verified else None,
            "mfa_verified": value.mfa_verified,
            "execution_allowed": value.admin or (value.role == "operator" and value.store.settings.trusted_operators)}


def cookie_response(store, cookie, **extra):
    value = store.authenticate(cookie)
    if value is None:
        raise HTTPException(401, "登录已失效")
    response = JSONResponse({**payload_for(value), **extra})
    settings = store.settings
    response.set_cookie(settings.cookie_name, cookie, httponly=True, secure=settings.production,
                        samesite="lax", path="/")
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/auth/status")
async def status(request: Request):
    store = request.app.state.identity_store
    return {"initialized": await asyncio.to_thread(store.initialized), "registration": False,
            "production": store.settings.production, "login_mfa_management": True, "username_change": True,
            "account_preferences": True, "account_panel": True, "account_sharing": True, "history_paging": True}


@router.post("/auth/login")
async def login(request: Request, payload: Credentials):
    store = request.app.state.identity_store
    address = request.client.host if request.client else "unknown"
    cookie = await asyncio.to_thread(store.login, payload.username, payload.password, address,
                                     request.headers.get("user-agent", ""))
    return cookie_response(store, cookie)


@router.get("/auth/me")
async def me():
    return payload_for(principal())


@router.get("/auth/csrf")
async def csrf():
    return {"csrf": principal().csrf}


@router.post("/auth/logout")
async def logout():
    value = principal()
    await asyncio.to_thread(value.store.revoke, value.user_id, value.login_id)
    response = JSONResponse({"ok": True})
    response.delete_cookie(value.store.settings.cookie_name, path="/", secure=value.store.settings.production,
                           httponly=True, samesite="lax")
    return response


@router.post("/auth/logout_all")
async def logout_all():
    value = principal()
    await asyncio.to_thread(value.store.revoke, value.user_id)
    response = JSONResponse({"ok": True})
    response.delete_cookie(value.store.settings.cookie_name, path="/", secure=value.store.settings.production)
    return response


@router.get("/auth/sessions")
async def sessions():
    value = principal()
    return {"sessions": await asyncio.to_thread(value.store.sessions, value.user_id), "current": value.login_id}


@router.delete("/auth/sessions/{identifier}")
async def revoke_session(identifier: str):
    value = principal()
    await asyncio.to_thread(value.store.revoke, value.user_id, identifier)
    return {"ok": True}


@router.post("/auth/mfa/start")
async def start_mfa(payload: Password):
    value = principal()
    return await mfa_action(value.store.start_mfa, value, payload.password)


@router.post("/auth/mfa/confirm")
async def confirm_mfa(payload: Code):
    value = principal()
    codes, cookie = await mfa_action(value.store.confirm_mfa, value, payload.code)
    return cookie_response(value.store, cookie, recovery_codes=codes)


@router.post("/auth/mfa/verify")
async def verify_mfa(payload: Code):
    value = principal()
    cookie = await mfa_action(value.store.verify_mfa, value, payload.code)
    return cookie_response(value.store, cookie)


@router.post("/auth/reauthenticate")
async def reauthenticate(payload: Reauth):
    value = principal()
    cookie = await asyncio.to_thread(value.store.reauthenticate, value, payload.password, payload.code)
    return cookie_response(value.store, cookie)


async def mfa_action(action, *args):
    from memory.identity_store import Denied, Throttled
    try:
        return await asyncio.to_thread(action, *args)
    except Throttled:
        raise
    except Denied as error:
        # Invalid factor/enrollment proof does not invalidate the login cookie.
        raise HTTPException(403, str(error)) from error


@router.get("/auth/mfa/devices")
async def mfa_devices():
    from security.mfa_devices import listing
    value = principal()
    return await mfa_action(listing, value.store, value)


@router.post("/auth/mfa/manage/start")
async def mfa_manage_start(payload: MfaChange):
    from security.mfa_devices import begin
    value = principal()
    cookie, binding = await mfa_action(begin, value.store, value, payload.password, payload.code, payload.mode, payload.label)
    return cookie_response(value.store, cookie, binding=binding)


@router.post("/auth/mfa/manage/login_start")
async def mfa_manage_login_start(payload: MfaLoginChange):
    from security.mfa_devices import begin_from_login
    value = principal()
    cookie, binding = await mfa_action(begin_from_login, value.store, value, payload.code, payload.mode, payload.label)
    return cookie_response(value.store, cookie, binding=binding)


@router.post("/auth/mfa/manage/confirm")
async def mfa_manage_confirm(payload: MfaComplete):
    from security.mfa_devices import complete
    value = principal()
    cookie, result = await mfa_action(complete, value.store, value, payload.operation_id, payload.code)
    return cookie_response(value.store, cookie, **result)


@router.post("/auth/mfa/manage/cancel")
async def mfa_manage_cancel(payload: MfaOperation):
    from security.mfa_devices import cancel
    value = principal()
    return await mfa_action(cancel, value.store, value, payload.operation_id)


@router.post("/auth/mfa/recovery/regenerate")
async def mfa_recovery_regenerate(payload: Reauth):
    from security.mfa_devices import regenerate_recovery
    value = principal()
    cookie, result = await mfa_action(regenerate_recovery, value.store, value, payload.password, payload.code)
    return cookie_response(value.store, cookie, **result)


@router.delete("/auth/mfa/devices/{identifier}")
async def mfa_device_remove(identifier: str, payload: Reauth):
    from security.mfa_devices import remove
    value = principal()
    cookie, result = await mfa_action(remove, value.store, value, identifier, payload.password, payload.code)
    return cookie_response(value.store, cookie, **result)


@router.post("/user/password")
async def password(payload: NewPassword):
    value = principal()
    if time.time() - value.recent_auth > 300:
        raise HTTPException(403, "请先完成近期身份验证")
    await asyncio.to_thread(value.store.reset_password, value.user_id, value.user_id, payload.password,
                            old_password=payload.old_password)
    return {"ok": True, "login_required": True}


@router.get("/admin/users")
async def users():
    value = require_admin()
    return {"users": await asyncio.to_thread(value.store.users)}


@router.post("/admin/users")
async def create_user(payload: NewUser):
    value = recent_admin()
    if payload.role in {"admin", "operator"} and not payload.trusted:
        raise HTTPException(422, "请明确确认该账号属于可信实例操作者")
    from memory.identity_store import Denied
    try:
        return await asyncio.to_thread(value.store.create_user, payload.username, payload.password, payload.role,
                                       payload.name, actor=value.user_id)
    except Denied:
        raise
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@router.patch("/admin/users/{identifier}")
async def change_user(identifier: str, payload: ChangeUser):
    value = recent_admin()
    if payload.role in {"admin", "operator"} and not payload.trusted:
        raise HTTPException(422, "请明确确认该账号属于可信实例操作者")
    result = await asyncio.to_thread(value.store.change_user, value.user_id, identifier,
                                     role=payload.role, enabled=payload.enabled)
    if payload.enabled is False or payload.role is not None:
        from security.revocation import stop_known_jobs
        result["jobs"] = await stop_known_jobs(value.store, identifier)
    return result


@router.post("/admin/users/{identifier}/password")
async def reset_password(identifier: str, payload: AdminPassword):
    value = recent_admin()
    if identifier == value.user_id:
        raise HTTPException(400, "请使用个人修改密码入口")
    await asyncio.to_thread(value.store.reset_password, value.user_id, identifier, payload.password)
    return {"ok": True}


@router.get("/admin/audit")
async def audit():
    value = require_admin()
    return {"events": await asyncio.to_thread(value.store.audit_entries)}


@router.get("/user/usage")
async def usage():
    from security.quotas import status
    value = principal()
    return await asyncio.to_thread(status, value.store, value.user_id)


@router.get("/user/preferences")
async def get_preferences():
    from security.preferences import snapshot
    return await asyncio.to_thread(snapshot, principal())


@router.patch("/user/preferences")
async def save_preferences(request: Request, payload: PreferencesPatch):
    from security.preferences import update, validate_ui
    value = principal()
    expected = request.headers.get("X-Ytools-Expected-Account")
    if expected and expected != value.user_id:
        raise HTTPException(409, "登录账号已变化，请重新打开页面")
    try:
        ui = validate_ui(payload.ui)
        return await asyncio.to_thread(update, "ui", ui, only_if_unset=payload.only_if_unset)
    except Denied:
        raise
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@router.get("/user/username")
async def username_status():
    from security.usernames import status
    value = principal()
    return await mfa_action(status, value.store, value)


@router.post("/user/username")
async def change_username(payload: NewUsername):
    from memory.identity_store import Denied, Throttled
    from security.usernames import change, ChangeError
    value = principal()
    try:
        result = await asyncio.to_thread(change, value.store, value, payload.username, payload.password, payload.code)
    except ChangeError as error:
        headers = {"Retry-After": str(error.retry_after)} if error.retry_after else None
        raise HTTPException(error.status, str(error), headers=headers) from error
    except Throttled:
        raise
    except Denied as error:
        raise HTTPException(403, str(error)) from error
    response = JSONResponse(result)
    response.delete_cookie(value.store.settings.cookie_name, path="/", secure=value.store.settings.production,
                           httponly=True, samesite="lax")
    return response


@router.get("/admin/users/{identifier}/limits")
async def get_limits(identifier: str):
    from security.quotas import status
    value = require_admin()
    return await asyncio.to_thread(status, value.store, identifier)


@router.put("/admin/users/{identifier}/limits")
async def set_limits(identifier: str, payload: AccountLimits):
    from security.quotas import update
    value = recent_admin()
    return await asyncio.to_thread(update, value.store, value.user_id, identifier, payload.model_dump(exclude_none=True))
