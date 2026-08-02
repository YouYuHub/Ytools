import json
from typing import Any, Dict, List

try:
    import env_manager as _env_manager
except ImportError:
    _env_manager = None

from config import Message


def parse_bool_like(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "y"}:
        return True
    if text in {"0", "false", "no", "off", "n"}:
        return False
    return default


def parse_int_like(value: Any, default: int) -> int:
    try:
        parsed = int(value)
        return parsed if parsed > 0 else default
    except (TypeError, ValueError):
        return default


def frontend_provides_full_history(messages: List[dict]) -> bool:
    user_count = 0
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "user":
            user_count += 1
        if role in {"assistant", "tool"}:
            return True
    return user_count > 1


def resolve_model_max_input_tokens(default: int = 8192) -> int:
    if _env_manager is None:
        return default

    chat_config = _env_manager.get_default_chat_config()
    if not isinstance(chat_config, dict):
        return default

    model_config = chat_config.get("selected_model")
    candidates = [model_config, chat_config]
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        for key in ("maxInputTokens", "max_input_tokens", "contextLength", "context_length"):
            try:
                parsed = int(candidate.get(key))
            except (TypeError, ValueError):
                continue
            if parsed > 0:
                return parsed
    return default


def estimate_text_tokens(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, (dict, list, tuple)):
        try:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except Exception:
            text = str(value)
    else:
        text = str(value)
    if not text:
        return 0
    ascii_chars = sum(1 for char in text if ord(char) < 128)
    non_ascii_chars = len(text) - ascii_chars
    return max(1, ascii_chars // 4 + non_ascii_chars)


def estimate_message_tokens(message: dict[str, Any]) -> int:
    if not isinstance(message, dict):
        return estimate_text_tokens(message)
    total = 4
    for key in ("role", "name", "content", "reasoning_content", "tool_call_id", "finish_reason"):
        total += estimate_text_tokens(message.get(key))
    tool_calls = message.get("tool_calls")
    if tool_calls is not None:
        total += estimate_text_tokens(tool_calls)
    return total


def estimate_messages_tokens(messages: List[dict[str, Any]]) -> int:
    return sum(estimate_message_tokens(message) for message in messages if isinstance(message, dict))


def estimate_tool_definition_tokens(tools: List[dict[str, Any]] | None) -> int:
    if not tools:
        return 0
    return sum(estimate_text_tokens(tool) for tool in tools if tool is not None)


def build_request_messages(messages: List[dict]) -> list[Message]:
    return [
        Message(**({k: v for k, v in msg.items() if not str(k).startswith("_")}))
        if isinstance(msg, dict) else msg
        for msg in messages
    ]


class UsageAccumulator:
    def __init__(self) -> None:
        self._usage_by_id: Dict[str, dict] = {}
        self._fingerprints: set[str] = set()

    def collect(self, event_payload: dict[str, Any]) -> None:
        usage = event_payload.get("usage")
        if not isinstance(usage, dict):
            return

        usage_fingerprint = json.dumps(usage, ensure_ascii=False, sort_keys=True)
        if usage_fingerprint in self._fingerprints:
            return

        completion_id = event_payload.get("id")
        if completion_id is not None and str(completion_id).strip():
            lookup_id = str(completion_id)
            if lookup_id in self._usage_by_id:
                return
            self._usage_by_id[lookup_id] = usage
            self._fingerprints.add(usage_fingerprint)
            return

        generated_id = f"no_id_{len(self._usage_by_id) + 1}"
        self._usage_by_id[generated_id] = usage
        self._fingerprints.add(usage_fingerprint)

    @property
    def count(self) -> int:
        return len(self._usage_by_id)

    def merge_to(self, target: dict[str, Any], merge_func) -> None:
        for usage in self._usage_by_id.values():
            merge_func(target, usage)
