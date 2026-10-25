"""Authenticated page bootstrap; private identity is never cached as an asset."""
import json
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from routers.auth_router import payload_for
from security.principal import current
from util.ui_pages import PAGE_BY_FILE


class AccountStaticFiles(StaticFiles):
    async def get_response(self, path, scope):
        filename = 'index.html' if path in {'', '.'} else path
        page = PAGE_BY_FILE.get(filename)
        if page and page.authenticated:
            principal = current()
            if principal is None or not principal.mfa_verified:
                raise HTTPException(401, "请先完成登录")
            source = (Path(self.directory) / filename).read_text(encoding="utf-8")
            context = json.dumps(payload_for(principal), ensure_ascii=True).replace("<", "\\u003c")
            bootstrap = '<script id="account-context" type="application/json">' + context + '</script>'
            if "<head>" not in source:
                raise HTTPException(503, "页面初始化失败")
            return HTMLResponse(source.replace("<head>", "<head>\n" + bootstrap, 1),
                                headers={"Cache-Control": "private, no-store"})
        return await super().get_response(path, scope)
