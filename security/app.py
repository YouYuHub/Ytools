"""Install the mandatory HTTP boundary without starting runtime services."""
from fastapi import Request
from fastapi.responses import JSONResponse

from memory.identity_store import IdentityStore, Denied, Throttled, Unavailable
from security.middleware import SecurityMiddleware
from security.settings import SecuritySettings


def install_security(app, settings=None):
    try:
        import argon2
        import pyotp
        from cryptography.fernet import Fernet
    except ImportError as exc:
        raise RuntimeError("安全依赖缺失，请在服务 Python 环境安装 requirements.lock.txt") from exc
    from memory.chat_memory import HISTORY_ROOT
    from memory.storage_backup import check_maintenance
    check_maintenance(HISTORY_ROOT.parent)
    store = IdentityStore(settings or SecuritySettings.from_env())
    store.prepare()
    app.state.identity_store = store
    from routers.auth_router import router
    app.include_router(router, tags=["Accounts"])

    @app.exception_handler(Denied)
    async def denied(request: Request, error):
        return JSONResponse({"detail": str(error)}, status_code=401 if request.url.path.startswith("/auth/") else 403)

    @app.exception_handler(Throttled)
    async def throttled(request: Request, error):
        return JSONResponse({"detail": str(error)}, status_code=429, headers={"Retry-After": "600"})

    @app.exception_handler(Unavailable)
    async def unavailable(request: Request, error):
        return JSONResponse({"detail": str(error)}, status_code=503)

    app.add_middleware(SecurityMiddleware, store=store, application=app)
    return store
