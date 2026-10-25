"""Use maintained cryptographic implementations; no password fallback."""
import hashlib
import hmac
import re
import secrets
import threading
import unicodedata

_HASH_SLOTS = threading.BoundedSemaphore(4)
_HASHER = None
_DUMMY = None
_LOCK = threading.Lock()


def hasher():
    global _HASHER, _DUMMY
    with _LOCK:
        if _HASHER is None:
            from argon2 import PasswordHasher
            _HASHER = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=1)
            _DUMMY = _HASHER.hash(secrets.token_urlsafe(32))
    return _HASHER


def username(value):
    value = unicodedata.normalize("NFKC", str(value)).casefold()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.@+-]{1,63}", value):
        raise ValueError("用户名需为 2–64 个英文字母、数字或 _.@+-，以字母或数字开头")
    return value


def validate_password(password):
    if not isinstance(password, str) or not 15 <= len(password) <= 128 or len(password.encode("utf-8")) > 1024:
        raise ValueError("密码须为 15–128 个字符，不会自动去空格或截断")
    return password


def password_hash(password):
    validate_password(password)
    with _HASH_SLOTS:
        return hasher().hash(password)


def password_matches(encoded, password):
    if not isinstance(password, str) or not 1 <= len(password) <= 128 or len(password.encode("utf-8")) > 1024:
        return False
    from argon2.exceptions import VerificationError, InvalidHashError
    with _HASH_SLOTS:
        instance = hasher()
        try:
            matched = instance.verify(encoded or _DUMMY, password)
            return bool(encoded and matched)
        except (VerificationError, InvalidHashError):
            return False


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def csrf(secret, identifier):
    return hmac.new(secret.encode("ascii"), ("csrf:" + identifier).encode("ascii"), hashlib.sha256).hexdigest()


def display_name(value):
    value = str(value).strip()
    if not value or len(value) > 32 or any(ord(c) < 32 for c in value):
        raise ValueError("显示名须为 1–32 个字符，不能包含控制字符")
    return value
