"""Streaming ASGI identity/CSRF boundary; no buffering of response SSE bodies."""
import asyncio
import hmac
import json
import math
import re
import time
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlsplit

from fastapi import HTTPException
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Match

from memory.identity_store import Denied, Throttled, Unavailable
from security import authorization
from security.principal import CURRENT
from security.route_policy import LEGACY_ROUTES
from util.ui_pages import PRIVATE_PAGES, PUBLIC_PAGES, PASSWORD_CHANGE_PAGES

SAFE = {"GET", "HEAD", "OPTIONS"}
PUBLIC = {("GET", "/"), ("GET", "/auth/status"), ("POST", "/auth/login")}
PENDING_MFA = {"/auth/me", "/auth/csrf", "/auth/logout", "/auth/mfa/start", "/auth/mfa/confirm", "/auth/mfa/verify",
               "/auth/mfa/manage/login_start"}
CREATE_SESSION = {"/chat_with_tool", "/file/upload_session_files", "/file/upload_session_media", "/chat_history/ensure_session"}
EXECUTION = {"/inject_message", "/task_templates/start", "/chat_history/upload_chat_file",
             "/chat_history/import_package", "/chat_context/responses_rebuild"}
KNOWN_AUTH = {
    ("GET", "/auth/status"), ("POST", "/auth/login"), ("GET", "/auth/me"), ("GET", "/auth/csrf"),
    ("POST", "/auth/logout"), ("POST", "/auth/logout_all"), ("GET", "/auth/sessions"),
    ("DELETE", "/auth/sessions/{identifier}"), ("POST", "/auth/mfa/start"), ("POST", "/auth/mfa/confirm"),
    ("POST", "/auth/mfa/verify"), ("POST", "/auth/reauthenticate"), ("POST", "/user/password"),
    ("GET", "/admin/users"), ("POST", "/admin/users"), ("PATCH", "/admin/users/{identifier}"),
    ("POST", "/admin/users/{identifier}/password"), ("GET", "/admin/audit"),
    ("POST", "/chat_history/ensure_session"),
    ("GET", "/user/usage"), ("GET", "/admin/users/{identifier}/limits"),
    ("PUT", "/admin/users/{identifier}/limits"),
    ("GET", "/auth/mfa/devices"), ("DELETE", "/auth/mfa/devices/{identifier}"),
    ("POST", "/auth/mfa/manage/start"), ("POST", "/auth/mfa/manage/confirm"), ("POST", "/auth/mfa/manage/cancel"),
    ("POST", "/auth/mfa/manage/login_start"),
    ("POST", "/auth/mfa/recovery/regenerate"),
    ("GET", "/user/username"), ("POST", "/user/username"),
    ("GET", "/user/preferences"), ("PATCH", "/user/preferences"),
}


class BodyTooLarge(Exception):
    pass


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("JSON 数值必须是有限值")
    return number


def _json_object(body):
    try:
        data = json.loads(body, parse_constant=_finite_float, parse_float=_finite_float)
    except (ValueError, RecursionError):
        raise HTTPException(422, "请求体必须是 JSON 对象") from None
    if not isinstance(data, dict):
        raise HTTPException(422, "请求体必须是 JSON 对象")
    # Do not recursively walk or copy the body. Deep extras can otherwise make
    # validation/error encoding fail; large prompt and base64 strings stay cheap.
    stack = [iter(data.values())]
    while stack:
        if len(stack) > 128:
            raise HTTPException(422, "JSON 嵌套不能超过 128 层")
        try:
            item = next(stack[-1])
        except StopIteration:
            stack.pop()
            continue
        if isinstance(item, dict):
            stack.append(iter(item.values()))
        elif isinstance(item, list):
            stack.append(iter(item))
    return data


def _headers(scope):
    return {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope.get("headers", [])}


def _asset(path):
    if path in PUBLIC_PAGES or path in {"/ui/login.css", "/ui/login.js", "/ui/favicon-squid.svg", "/ui/favicon-editor.png"}:
        return True
    suffix = path.rsplit(".", 1)[-1].lower()
    return (path.startswith(("/ui/js/", "/ui/style/", "/ui/fonts/")) and suffix in {
        "js", "css", "woff", "woff2", "ttf", "svg", "png", "jpg", "jpeg", "gif", "webp", "ico"}
        and "/node_modules/" not in path and "/test" not in path)


class SecurityMiddleware:
    def __init__(self, app, *, store, application):
        self.app, self.store, self.application = app, store, application

    def _route(self, scope):
        from security.routes import effective_routes
        for route in effective_routes(self.application):
            match, child = route.matches(scope)
            if match == Match.FULL:
                return getattr(route, "path", ""), child.get("path_params", {})
        return None, {}

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            return await self.app(scope, receive, send)
        path, method, headers = scope.get("path", ""), scope["method"], _headers(scope)
        settings, started, principal, streaming, finished = self.store.settings, False, None, False, False
        cookie = ""
        async def safe_send(message):
            nonlocal started, streaming, finished
            if message["type"] == "http.response.start":
                started = True
                entries = list(message.get("headers", []))
                content_type = next((value.decode("latin-1").split(";", 1)[0].lower() for key, value in entries if key.lower() == b"content-type"), "")
                streaming = content_type == "text/event-stream"
                if path.startswith(("/file/get_", "/permissions/media")) and content_type not in {"application/json"}:
                    safe_media = content_type.startswith(("audio/", "video/")) or content_type in {
                        "image/png", "image/jpeg", "image/gif", "image/webp", "image/avif", "image/apng", "application/pdf", "text/plain"}
                    if not safe_media:
                        entries = [(key, value) for key, value in entries if key.lower() not in {b"content-disposition", b"content-security-policy"}]
                        entries += [(b"content-disposition", b"attachment"),
                                    (b"content-security-policy", b"sandbox; default-src 'none'; frame-ancestors 'none'")]
                    elif content_type in {"application/pdf", "text/plain"}:
                        entries = [(key, value) for key, value in entries if key.lower() not in {b"x-frame-options", b"content-security-policy"}]
                        entries += [(b"x-frame-options", b"SAMEORIGIN"),
                                    (b"content-security-policy", b"sandbox; frame-ancestors 'self'")]
                existing = {key.lower() for key, _ in entries}
                # Only the authenticated account panel can be embedded by our own origin.
                account_panel = (path == "/ui/account.html" and content_type == "text/html"
                                 and message["status"] == 200 and principal is not None and principal.mfa_verified
                                 and re.search(rb"(?:^|&)panel=[a-f0-9]{32}(?:&|$)", scope.get("query_string", b"")))
                for key, value in ((b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"no-referrer"),
                                   (b"x-frame-options", b"SAMEORIGIN" if account_panel else b"DENY"),
                                   (b"content-security-policy", b"frame-ancestors 'self'; object-src 'none'; base-uri 'self'" if account_panel else b"frame-ancestors 'none'; object-src 'none'; base-uri 'self'")):
                    if key not in existing:
                        entries.append((key, value))
                if not _asset(path):
                    entries = [(key, value) for key, value in entries if key.lower() != b"cache-control"]
                    entries.append((b"cache-control", b"private, no-store"))
                if principal:
                    entries.append((b"x-ytools-account", principal.user_id.encode("ascii")))
                message = {**message, "headers": entries}
            elif message["type"] == "http.response.body" and not message.get("more_body"):
                finished = True
            await send(message)
        try:
            host = headers.get("host", "")
            try:
                parts = urlsplit("//" + host)
                parsed_host = parts.hostname
                if parts.username is not None or parts.password is not None or parts.path or parts.query or parts.fragment:
                    raise ValueError()
                if parts.port is not None and not 1 <= parts.port <= 65535:
                    raise ValueError()
            except ValueError:
                raise HTTPException(400, "不受信任的 Host") from None
            allowed_hosts = {settings.host} | ({"127.0.0.1", "localhost", "::1", "testserver"} if not settings.production else set())
            if parsed_host not in allowed_hosts:
                raise HTTPException(400, "不受信任的 Host")
            if settings.production and scope.get("scheme") != "https":
                raise HTTPException(400, "生产部署仅接受经可信代理转发的 HTTPS")
            if method in {"GET", "HEAD"} and path in {"/ui", "/index", "/index.html"}:
                query = scope.get("query_string", b"").decode("latin-1")
                return await RedirectResponse("/ui/" + ("?" + query if query else ""), status_code=303)(scope, receive, safe_send)
            if path.startswith("/ui/") and path not in PRIVATE_PAGES and not _asset(path):
                raise HTTPException(404, "资源不存在")
            if _asset(path):
                return await self.app(scope, receive, safe_send)
            if method not in SAFE:
                origin = headers.get("origin")
                if origin is None:
                    referer = headers.get("referer", "")
                    try:
                        parts = urlsplit(referer)
                        origin = f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else None
                    except ValueError:
                        origin = None
                if origin != settings.origin:
                    raise HTTPException(403, "请求来源不受信任")
            if headers.get("sec-fetch-site") == "cross-site" and path != "/auth/login":
                raise HTTPException(403, "不允许跨站访问")
            jar = SimpleCookie()
            try:
                jar.load(headers.get("cookie", ""))
                cookie = jar[settings.cookie_name].value if settings.cookie_name in jar else ""
            except Exception:
                cookie = ""
            public = (method, path) in PUBLIC
            if not public:
                principal = await asyncio.to_thread(self.store.authenticate, cookie)
                if principal is None:
                    if method == "GET" and path in PRIVATE_PAGES:
                        return await RedirectResponse("/ui/login.html", status_code=303)(scope, receive, safe_send)
                    raise HTTPException(401, "请先登录")
                expected_account = headers.get("x-ytools-expected-account")
                if expected_account and expected_account != principal.user_id:
                    raise HTTPException(409, "登录账号已变化，请重新打开页面", headers={"X-Ytools-Account-Changed":"true"})
                if not principal.mfa_verified and path not in PENDING_MFA:
                    if path.startswith("/ui/"):
                        return await RedirectResponse("/ui/login.html", status_code=303)(scope, receive, safe_send)
                    raise HTTPException(403, "请先完成 MFA 验证", headers={"X-MFA-Required": "true"})
                if principal.password_change_required and path not in PENDING_MFA | {"/user/password", "/auth/reauthenticate"} | PASSWORD_CHANGE_PAGES:
                    if path.startswith("/ui/"):
                        return await RedirectResponse("/ui/account.html", status_code=303)(scope, receive, safe_send)
                    raise HTTPException(403, "请先修改管理员设置的初始密码", headers={"X-Password-Change-Required": "true"})
                if method not in SAFE and not hmac.compare_digest(headers.get("x-csrf-token", "").encode("latin-1"), principal.csrf.encode("ascii")):
                    raise HTTPException(403, "CSRF 验证失败，请刷新登录状态", headers={"X-CSRF-Required": "true"})
            template, path_params = self._route(scope)
            if not path.startswith("/ui/") and path not in {"/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"}:
                if (method, template) not in LEGACY_ROUTES | KNOWN_AUTH:
                    raise HTTPException(404, "接口不存在或尚未登记授权策略")
            if path.startswith("/admin/") or path in {"/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"}:
                if not principal or not principal.admin:
                    raise HTTPException(403, "需要管理员权限")
            request_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
            raw_upload = request_type == "application/octet-stream"
            if raw_upload and path not in {"/file/upload_session_files", "/file/upload_session_media", "/chat_history/upload_chat_file", "/chat_history/import_preview", "/chat_history/import_package"}:
                raise HTTPException(415, "该接口只接受 JSON 请求")
            cap = settings.upload_bytes if raw_upload else settings.body_bytes
            raw_length = headers.get("content-length")
            if raw_length is not None and (len(raw_length) > 20 or not raw_length.isdecimal() or int(raw_length) > cap):
                raise HTTPException(413, "请求体超过允许的大小")
            received = 0
            upload_charged = 0
            if principal:
                from security.rate_limits import charge, charge_many
                charges = [("requests", 1800, 60, 1)]
                if method not in SAFE:
                    charges.append(("writes", 180, 60, 1))
                await asyncio.to_thread(charge_many, self.store, principal.user_id, charges)
            async def limited_receive():
                nonlocal received, upload_charged
                message = await receive()
                received += len(message.get("body", b""))
                if received > cap:
                    raise BodyTooLarge()
                if principal and raw_upload:
                    # Reserve rounded 8 MiB blocks before handing bytes to any
                    # parser/storage. Disconnected uploads still consume quota.
                    rounded = ((received + 8 * 1024 * 1024 - 1) // (8 * 1024 * 1024)) * (8 * 1024 * 1024)
                    if rounded > upload_charged:
                        await asyncio.to_thread(charge, self.store, principal.user_id, "upload-bytes", 2 * 1024**3, 86400, rounded - upload_charged)
                        upload_charged = rounded
                return message
            body, replay, data = b"", [], {}
            buffered_request = method not in SAFE and not raw_upload
            if buffered_request:
                chunks = []
                while True:
                    message = await limited_receive()
                    if message["type"] == "http.disconnect":
                        return
                    replay.append(message)
                    chunks.append(message.get("body", b""))
                    if not message.get("more_body"):
                        break
                body = b"".join(chunks)
                if body:
                    if request_type and request_type != "application/json" and not (request_type.startswith("application/") and request_type.endswith("+json")):
                        raise HTTPException(415, "该接口只接受 JSON 请求")
                    data = _json_object(body)
            async def replay_receive():
                return replay.pop(0) if replay else await limited_receive()
            token = CURRENT.set(principal)
            from memory.storage_layout import _REQUIRED, _SYSTEM
            required_token = _REQUIRED.set(True)
            system_token = _SYSTEM.set(None)
            try:
                if principal and not path.startswith(("/auth/", "/admin/", "/ui/")):
                    query = parse_qs(scope.get("query_string", b"").decode("utf-8", "replace"))
                    await asyncio.to_thread(self._authorize, principal, path, method, template, path_params, query, data)
                app_task = asyncio.create_task(self.app(scope, replay_receive if buffered_request else limited_receive, safe_send))
                try:
                    while not app_task.done():
                        done, _ = await asyncio.wait({app_task}, timeout=2)
                        if done:
                            break
                        try:
                            live = principal is None or await asyncio.to_thread(self.store.live, principal)
                        except Unavailable:
                            live = False
                        if not live:
                            app_task.cancel()
                            await asyncio.gather(app_task, return_exceptions=True)
                            if started and streaming and not finished:
                                await safe_send({"type": "http.response.body", "body": b'data: {"error":"Login expired or revoked","error_type":"authentication","retrying":false}\n\ndata: [DONE]\n\n', "more_body": False})
                            elif not started:
                                await JSONResponse({"detail": "登录已失效"}, status_code=401)(scope, receive, safe_send)
                            return
                    await app_task
                finally:
                    if not app_task.done():
                        app_task.cancel()
                        await asyncio.gather(app_task, return_exceptions=True)
            finally:
                _SYSTEM.reset(system_token)
                _REQUIRED.reset(required_token)
                CURRENT.reset(token)
        except (HTTPException, BodyTooLarge, Denied, Unavailable, Throttled) as exc:
            if started:
                raise
            if isinstance(exc, HTTPException):
                code, detail, extra = exc.status_code, exc.detail, exc.headers
            elif isinstance(exc, BodyTooLarge):
                code, detail, extra = 413, "请求体超过允许的大小", None
            elif isinstance(exc, Unavailable):
                code, detail, extra = 503, str(exc), None
            elif isinstance(exc, Throttled):
                code, detail, extra = 429, str(exc), {"Retry-After": "600"}
            else:
                code, detail, extra = 403, str(exc), None
            await JSONResponse({"detail": detail}, status_code=code, headers=extra)(scope, receive, safe_send)

    def _authorize(self, principal, path, method, template, params, query, data):
        def value(name):
            return data.get(name) if name in data else (query.get(name) or [None])[0]
        refresh_values = [data.get("refresh"), *query.get("refresh", [])]
        force_refresh = any(str(item).strip().lower() in {"1", "true", "on", "yes", "t", "y"} for item in refresh_values)
        if path == "/tools/diagnostics" or (path == "/tools/list" and force_refresh):
            if not principal.admin or time.time() - principal.recent_auth > 300:
                raise HTTPException(403, "工具诊断与强制发现需要近期验证的管理员")
        from security.preferences import PERSONAL_PATHS
        if (path == "/change_chat_dir" or (path.startswith("/chat_config/") and method not in SAFE and path not in PERSONAL_PATHS)):
            if not principal.admin:
                if path not in {"/chat_config/models/select", "/chat_config/tool_selection", "/chat_config/retitle_setting", "/chat_config/generate_title"} or not data.get("session_id"):
                    raise HTTPException(403, "全局配置、主机目录和执行权限由管理员管理")
                def custom_headers(item):
                    if isinstance(item, dict):
                        return any((key.lower() in {"headers", "extra_headers", "_custom_headers", "api_key", "apikey"} and bool(entry)) or custom_headers(entry) for key, entry in item.items())
                    if isinstance(item, list):
                        return any(custom_headers(entry) for entry in item)
                    return False
                if custom_headers(data):
                    raise HTTPException(403, "自定义认证请求头由管理员管理")
        if path == "/task_costs/pricing":
            if not principal.admin:
                raise HTTPException(403, "全局预算与计价由管理员管理")
        if path in EXECUTION or (method not in SAFE and path.startswith(("/file_diff/", "/tool_approvals/", "/terminal/"))):
            authorization.require_execution()
        if path == "/file/get_local_file" or path == "/permissions/media":
            authorization.require_execution()
        identifiers = []
        for key in ("session_id", "source_session_id"):
            for item in [data.get(key), *query.get(key, [])]:
                if item:
                    identifiers.append(str(item))
        for batch in [data.get("session_ids"), *query.get("session_ids", [])]:
            if batch:
                identifiers.extend(batch if isinstance(batch, list) else str(batch).split(","))
        needs_session = (path.startswith(("/file/", "/file_diff/", "/terminal/", "/tool_approvals/", "/permissions/", "/chat_context/"))
                         or path in {"/chat_with_tool", "/stop_chat", "/stop_sub_agent", "/inject_message", "/cancel_inject_message",
                                     "/chat_history/file", "/chat_history/page", "/chat_history/event", "/chat_history/meta", "/chat_history/title", "/chat_history/delete_file",
                                     "/chat_history/archive", "/chat_history/delete_rounds", "/chat_history/remove_media_tag",
                                     "/chat_history/group_assign"})
        if needs_session and not identifiers:
            raise HTTPException(422, "请明确指定会话 ID")
        if len(identifiers) > 100:
            raise HTTPException(422, "一次最多操作 100 个会话")
        # Raw JSONL imports resolve a source label to an owned destination in
        # storage, including safe copies of foreign/reserved IDs.
        for identifier in ([] if path == "/chat_history/upload_chat_file" else identifiers):
            authorization.session(identifier.strip(), create=path in CREATE_SESSION and method == "POST",
                                  cleanup=path == "/chat_history/delete_file" and method == "DELETE")
        for group_id in [params.get("group_id"), data.get("group_id"), *query.get("group_id", [])]:
            if group_id:
                authorization.require_group(str(group_id))
        # Imports carry opaque raw bodies; storage validates every target before
        # replacing it. An ordinary role cannot import execution policy snapshots.
        if path == "/chat_with_tool":
            if not authorization.can_execute(principal) and data.get("tool_names"):
                raise HTTPException(403, "普通成员仅可使用无工具聊天")
            if any(key in data for key in ("usage_role", "usage_agent", "protocol_config", "wire_protocol", "responses_context", "owner_id", "user_id")):
                raise HTTPException(422, "请求不能指定服务端身份或协议内部字段")
