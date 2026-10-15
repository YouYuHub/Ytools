"""Short-lived, resource-bound preview tickets; no UI pairing is required."""
import base64
import hashlib
import hmac
import json
import secrets
import time
MEDIA_TICKET_KEY = secrets.token_urlsafe(32)


def ticket(session, source):
    data = json.dumps({"session": session, "source": source, "expires": time.time() + 1800}, ensure_ascii=False).encode()
    body = base64.urlsafe_b64encode(data).decode()
    return body + "." + hmac.new(MEDIA_TICKET_KEY.encode(), body.encode(), hashlib.sha256).hexdigest()


def verify_ticket(value, session, source):
    try:
        body, signature = value.rsplit(".", 1)
        if not hmac.compare_digest(signature, hmac.new(MEDIA_TICKET_KEY.encode(), body.encode(), hashlib.sha256).hexdigest()):
            return False
        data = json.loads(base64.urlsafe_b64decode(body))
        return data["session"] == session and data["source"] == source and data["expires"] > time.time()
    except (ValueError, KeyError, TypeError):
        return False
