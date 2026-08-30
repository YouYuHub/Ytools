"""聊天运行时的无状态辅助函数。

这里不包含会话编排或 MCP 调用，只负责请求参数、消息和 token 的轻量估算，
供聊天工厂与上下文压缩器共同使用。
"""
import json
from typing import Any, Dict, List

try:
    import env_manager
except ImportError:
    env_manager = None

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


def parse_return_length(value: Any, default: int) -> int:
    """解析可配置的“回传长度”：允许任意整数（0=不回传，负数=全部回传）。"""
    try:
        return int(value)
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


def resolve_config_max_input_tokens(
    model_config: dict[str, Any] | None,
    default: int = 8192,
) -> int:
    """从已解析的模型配置中读取最大输入上下文长度。"""
    if not isinstance(model_config, dict):
        return default
    candidates = [model_config, model_config.get("selected_model")]
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


def resolve_model_max_input_tokens(default: int = 8192) -> int:
    if env_manager is None:
        return default
    return resolve_config_max_input_tokens(env_manager.get_default_chat_config(), default)


def _serialize_for_estimate(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(exclude_none=True)
    if hasattr(value, "dict"):
        return value.dict(exclude_none=True)
    return value


def estimate_text_tokens(value: Any) -> int:
    if value is None:
        return 0
    value = _serialize_for_estimate(value)
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


# 多模态部件按固定占位计费：base64/data URL 数据按字符估算会虚高数万倍，
# 会误触发上下文预算检查；主流多模态模型单图成本约 1k token 量级，取固定占位值
MULTIMODAL_PART_PLACEHOLDER_TOKENS = 1024


def _estimate_content_value_tokens(content: Any) -> int:
    """content 支持 str 与多部件列表：媒体部件按固定占位计费，文本部件按字符估算。"""
    if not isinstance(content, list):
        return estimate_text_tokens(content)
    total = 0
    for part in content:
        if isinstance(part, str):
            total += estimate_text_tokens(part)
            continue
        if not isinstance(part, dict):
            continue
        if str(part.get("type") or "") == "text":
            total += estimate_text_tokens(part.get("text"))
            continue
        # 媒体部件：data URL / base64 / media:// 引用一律按固定占位计费
        total += MULTIMODAL_PART_PLACEHOLDER_TOKENS
    return total


def estimate_message_tokens(message: dict[str, Any]) -> int:
    if not isinstance(message, dict):
        return estimate_text_tokens(message)
    total = 4
    total += _estimate_content_value_tokens(message.get("content"))
    for key in ("role", "name", "reasoning_content", "tool_call_id", "finish_reason"):
        total += estimate_text_tokens(message.get(key))
    tool_calls = message.get("tool_calls")
    if tool_calls is not None:
        total += estimate_text_tokens(tool_calls)
    return total


def estimate_messages_tokens(messages: List[dict[str, Any]]) -> int:
    return sum(estimate_message_tokens(message) for message in messages if isinstance(message, dict))


def estimate_tool_definition_tokens(tools: List[Any] | None) -> int:
    if not tools:
        return 0
    return sum(estimate_text_tokens(tool) for tool in tools if tool is not None)


def estimate_request_context_tokens(
    messages: List[dict[str, Any]],
    tools: List[Any] | None = None,
) -> int:
    """估算一次 Chat Completions 请求的输入上下文（消息 + 工具 schema）。"""
    return estimate_messages_tokens(messages) + estimate_tool_definition_tokens(tools)


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