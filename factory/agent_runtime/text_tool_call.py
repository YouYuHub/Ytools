"""Keep a model's text-form tool-call envelope out of user-visible streams.

Some providers emit ``<tool_call>`` markup as ordinary content instead of a
structured ``tool_calls`` delta.  This is not an executable tool call.
"""

import json
import re


_PREFIXES = ("@@<tool_call>", "<tool_call>", "<tool_calls>", "@@TodoWrite@@")
_ENVELOPE = re.compile(
    r"\s*(?:@@<tool_call>\s*)?<tool_call>\s*(\{.*\})\s*</tool_call>\s*",
    re.DOTALL,
)
_XML_ENVELOPE = re.compile(
    r"\s*<tool_calls>\s*<invoke\s+name\s*=\s*(['\"])([^'\"]+)\1\s*>"
    r".*?</invoke>\s*</tool_calls>\s*", re.DOTALL,
)


def text_tool_call_reminder(name: str) -> str:
    reminder = (
        "刚才的回复只有写在正文中的工具调用标记，系统没有执行它。"
        "请勿输出 <tool_call>、<tool_calls>、@@TodoWrite@@ 标签或工具调用 JSON。"
        "若任务已经完成，请直接用自然语言答复用户；"
        "确需使用工具时，请通过原生 tool_calls 发起调用。"
    )
    if name == "todo_write":
        reminder += "若计划已全部 done 或计划没有变化，不要再次调用 todo_write。"
    return reminder


def text_tool_call_name(content: str) -> str | None:
    """Return the named tool only when the entire reply is a tool-call envelope."""
    xml_match = _XML_ENVELOPE.fullmatch(content)
    if xml_match:
        return xml_match.group(2).strip() or None
    if content.lstrip().startswith("@@TodoWrite@@"):
        try:
            payload = json.loads(content.lstrip()[len("@@TodoWrite@@"):])
        except (TypeError, ValueError):
            return None
        return "todo_write" if isinstance(payload, dict) and "todos" in payload else None
    match = _ENVELOPE.fullmatch(content)
    if match is None:
        return None
    try:
        payload = json.loads(match.group(1))
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    name = payload.get("name")
    return name if isinstance(name, str) and name.strip() else None


class TextToolCallBuffer:
    """Stream normal prose, buffering only replies that begin like tool markup."""

    def __init__(self) -> None:
        self._pending = ""
        self._passthrough = False

    def push(self, content: str) -> str:
        if self._passthrough:
            return content
        self._pending += content
        candidate = self._pending.lstrip()
        if not candidate or any(prefix.startswith(candidate) for prefix in _PREFIXES):
            return ""
        if any(candidate.startswith(prefix) for prefix in _PREFIXES):
            return ""
        self._passthrough = True
        visible, self._pending = self._pending, ""
        return visible

    def finish(self) -> tuple[str, str | None]:
        """Return (held prose, tool name). A named envelope is never displayed."""
        if self._passthrough:
            return "", None
        held, self._pending = self._pending, ""
        tool_name = text_tool_call_name(held)
        if tool_name:
            return "", tool_name
        if any(held.lstrip().startswith(prefix) for prefix in _PREFIXES):
            # 完整示例后跟解释文字属于正常正文，不按工具调用重试。
            for closing in ("</tool_call>", "</tool_calls>"):
                if closing in held and held.rsplit(closing, 1)[1].strip():
                    return held, None
            if held.lstrip().startswith("@@TodoWrite@@"):
                payload_text = held.lstrip()[len("@@TodoWrite@@"):].lstrip()
                try:
                    _, end = json.JSONDecoder().raw_decode(payload_text)
                    if payload_text[end:].strip():
                        return held, None
                except ValueError:
                    pass
            # An incomplete/malformed envelope is still not a user answer.
            return "", "<invalid>"
        return held, None
