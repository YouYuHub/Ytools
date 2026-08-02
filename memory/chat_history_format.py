"""会话历史与摘要的格式化工具。

`memory/chat_memory.py` 负责持久化会话元数据，
`factory/chat_factory.py` 负责基于这些历史做上下文压缩。
两者都需要把同一个 `chat_round` 条目展平成模型上下文消息，
都需要把 `context_summary` 结构渲染成可读文本，
因此把它们集中到本模块，避免两处平行实现。
"""
from __future__ import annotations

import json
from typing import Any


def normalize_context_summary(summary: Any) -> dict[str, Any] | None:
    """把任意输入的摘要数据规整成统一结构。

    返回结构包含:
    - summary: 主摘要文本
    - key_facts / open_items / tool_state: 列表字段
    - source_round_count: 已被压缩的轮次数
    """
    if summary is None:
        return None
    if isinstance(summary, str):
        text = summary.strip()
        if not text:
            return None
        return {
            "summary": text,
            "key_facts": [],
            "open_items": [],
            "tool_state": [],
            "source_round_count": 0,
        }
    if not isinstance(summary, dict):
        return None
    normalized = dict(summary)
    summary_text = normalized.get("summary")
    if not isinstance(summary_text, str) or not summary_text.strip():
        summary_text = normalized.get("content")
    if not isinstance(summary_text, str) or not summary_text.strip():
        return None
    normalized["summary"] = summary_text.strip()
    for key in ("key_facts", "open_items", "tool_state"):
        value = normalized.get(key)
        if isinstance(value, list):
            normalized[key] = [str(item).strip() for item in value if str(item).strip()]
        elif isinstance(value, str) and value.strip():
            normalized[key] = [value.strip()]
        else:
            normalized[key] = []
    source_round_count = normalized.get("source_round_count")
    try:
        parsed_count = int(source_round_count)
    except (TypeError, ValueError):
        parsed_count = 0
    normalized["source_round_count"] = max(0, parsed_count)
    return normalized


def render_context_summary(summary: Any) -> str | None:
    """把摘要结构渲染成给模型看的多行文本。

    输入可以是 `str`、`dict`，或 `None`（返回 `None`）。
    始终先走 `normalize_context_summary`，因此容忍历史遗留的 `content` 字段。
    """
    normalized = normalize_context_summary(summary)
    if not normalized:
        return None
    lines = ["【历史压缩摘要】", normalized["summary"]]
    key_facts = normalized.get("key_facts") or []
    if key_facts:
        lines.append("【保留事实】")
        lines.extend(f"- {fact}" for fact in key_facts)
    open_items = normalized.get("open_items") or []
    if open_items:
        lines.append("【未完成事项】")
        lines.extend(f"- {item}" for item in open_items)
    tool_state = normalized.get("tool_state") or []
    if tool_state:
        lines.append("【工具状态】")
        lines.extend(f"- {item}" for item in tool_state)
    return "\n".join(lines)


def format_tool_call_history_line(tool_call: dict[str, Any]) -> str | None:
    """把单条 `tool_calls` 渲染成一行历史可读文本，例如 `[调用工具] foo({...})`。"""
    if not isinstance(tool_call, dict):
        return None
    function_info = tool_call.get("function")
    if not isinstance(function_info, dict):
        return None
    tool_name = function_info.get("name")
    if not isinstance(tool_name, str) or not tool_name.strip():
        return None
    arguments = function_info.get("arguments")
    if isinstance(arguments, str):
        arguments_text = arguments.strip() or "{}"
    elif arguments is None:
        arguments_text = "{}"
    else:
        try:
            arguments_text = json.dumps(arguments, ensure_ascii=False)
        except TypeError:
            arguments_text = str(arguments)
    return f"[调用工具] {tool_name}({arguments_text})"


def round_entry_to_context_messages(round_entry: dict[str, Any]) -> list[dict[str, Any]]:
    """把一个 `chat_round` 条目展平成可用于模型上下文的消息列表。

    输出的格式与 `ChatMemoryManager.get_context_messages` 历史行为保持一致：
    - 跳过 `done/error/think` 等内部事件
    - 用户侧只保留第一条非“停止任务”文本
    - assistant 内容按行拼接，遇到连续重复文本会去重
    """
    if not isinstance(round_entry, dict):
        return []
    events = round_entry.get("events")
    if not isinstance(events, list):
        return []

    round_user_content: str | None = None
    assistant_fragments: list[str] = []

    for event in events:
        if not isinstance(event, dict):
            continue
        role = event.get("role")

        if role == "user" and round_user_content is None:
            user_content = event.get("content")
            if isinstance(user_content, str):
                normalized_user = user_content.strip()
                if normalized_user and normalized_user != "停止任务":
                    round_user_content = normalized_user

        if role != "assistant":
            continue
        if event.get("done") == "[DONE]" or event.get("error") is not None:
            continue

        content = event.get("content")
        if isinstance(content, str):
            normalized_content = content.strip()
            if normalized_content:
                lowered = normalized_content.lower()
                if not (lowered.startswith("<think>") and lowered.endswith("</think>")):
                    assistant_fragments.append(normalized_content)

        tool_calls = event.get("tool_calls")
        if isinstance(tool_calls, list):
            for tool_call in tool_calls:
                tool_call_line = format_tool_call_history_line(tool_call)
                if tool_call_line:
                    assistant_fragments.append(tool_call_line)

    if not round_user_content:
        question = round_entry.get("question")
        if isinstance(question, str):
            normalized_question = question.strip()
            if normalized_question and normalized_question != "停止任务":
                round_user_content = normalized_question

    if not assistant_fragments:
        return []

    merged_assistant_fragments: list[str] = []
    for text in assistant_fragments:
        if not merged_assistant_fragments or merged_assistant_fragments[-1] != text:
            merged_assistant_fragments.append(text)

    messages: list[dict[str, Any]] = []
    if round_user_content:
        messages.append({"role": "user", "content": round_user_content})
    messages.append({"role": "assistant", "content": "\n".join(merged_assistant_fragments)})
    return messages