"""Keep a model's text-form tool-call envelope out of user-visible streams.

Some providers emit ``<tool_call>`` markup as ordinary content instead of a
structured ``tool_calls`` delta.  This is not an executable tool call.
"""

import json
import re


_PREFIXES = ("@@<tool_call>", "<tool_call>")
_ENVELOPE = re.compile(
    r"\s*(?:@@<tool_call>\s*)?<tool_call>\s*(\{.*\})\s*</tool_call>\s*",
    re.DOTALL,
)


def text_tool_call_name(content: str) -> str | None:
    """Return the named tool only when the entire reply is a tool-call envelope."""
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
            # An incomplete/malformed envelope is still not a user answer.
            return "", "<invalid>"
        return held, None
