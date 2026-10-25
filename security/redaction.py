"""Field-level projections for configuration; never rewrite conversation text."""
import json
from security.principal import current

PRIVATE_KEYS = {"headers", "effective_headers", "extra_headers", "_custom_headers", "api_key", "apikey", "authorization",
                "password", "secret", "access_token", "refresh_token", "env_file", "env_value"}


def configuration(value):
    if isinstance(value, dict):
        return {key: configuration(item) for key, item in value.items() if key.lower() not in PRIVATE_KEYS}
    if isinstance(value, list):
        return [configuration(item) for item in value]
    return value


def for_member(value):
    principal = current()
    return configuration(value) if principal and not principal.admin else value


def history(text):
    principal = current()
    if not principal or principal.admin or not text:
        return text
    first, separator, rest = text.partition("\n")
    try:
        row = json.loads(first)
        if isinstance(row, dict) and isinstance(row.get("_meta"), dict):
            row["_meta"] = configuration(row["_meta"])
            return json.dumps(row, ensure_ascii=False) + separator + rest
    except (ValueError, TypeError):
        pass
    return text
