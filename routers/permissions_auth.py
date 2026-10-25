"""Short-lived, resource-bound preview tickets; no UI pairing is required."""
import base64
import hashlib
import hmac
import json
import secrets
import time
MEDIA_TICKET_KEY = secrets.token_urlsafe(32)


def _key():
    from security.principal import current
    principal = current()
    if principal:
        try:
            return hmac.new(principal.store.key_path.read_bytes(), b"Ytools:media-ticket:v1", hashlib.sha256).digest()
        except OSError as exc:
            raise RuntimeError("预览签名密钥不可用") from exc
    return MEDIA_TICKET_KEY.encode()


def ticket(session, source):
    from security.principal import current
    principal = current()
    identity = {"user": principal.user_id, "login": principal.login_id, "version": principal.auth_version,
                "action": "preview"} if principal else {}
    data = json.dumps({"session": session, "source": source, "expires": time.time() + (300 if principal else 1800), **identity}, ensure_ascii=False).encode()
    body = base64.urlsafe_b64encode(data).decode()
    return body + "." + hmac.new(_key(), body.encode(), hashlib.sha256).hexdigest()


def verify_ticket(value, session, source):
    try:
        if not isinstance(value, str) or len(value) > 16384:
            return False
        body, signature = value.rsplit(".", 1)
        if not hmac.compare_digest(signature, hmac.new(_key(), body.encode(), hashlib.sha256).hexdigest()):
            return False
        data = json.loads(base64.urlsafe_b64decode(body))
        from security.principal import current
        principal = current()
        if principal and any(data.get(key) != expected for key, expected in {
            "user": principal.user_id, "login": principal.login_id, "version": principal.auth_version, "action": "preview"}.items()):
            return False
        return data["session"] == session and data["source"] == source and data["expires"] > time.time()
    except (ValueError, KeyError, TypeError):
        return False
