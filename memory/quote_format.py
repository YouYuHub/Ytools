# -*- coding: utf-8 -*-
"""聊天引用（选中文本引用到提问）的规整、校验与模型视图序列化。

设计文档：docs/quote_selection_design.md（结构化方案）。

两种视图分离：
- **历史视图**：JSONL 用户事件保存原始 content 与 `quotes` 数组快照，
  供前端回放/编辑/复制、标题与问题索引使用（问题正文保持纯净）；
- **模型视图**：在送入聊天模型或压缩模型之前，把引用快照序列化为
  `<quote_list><li>…</li></quote_list>` 前置到该条 user 消息的文本部分
  （多模态消息作为第一个 text 部件），传给上游提供商的消息只含标准字段。

同一序列化函数用于：当前轮模型请求（chat_factory）、历史轮次重新进入模型
上下文（chat_history_format）、上下文压缩读取历史时的输入视图。JSONL 原始
历史不写入生成后的标签串（标题/最近问题索引只取问题正文）。
"""
from __future__ import annotations

from typing import Any

# 首版限制（前后端一致；前端镜像见 H5/js/quote_utils.js）
MAX_QUOTES = 5                    # 最多引用段数
MAX_QUOTE_CHARS = 4000            # 单段引用最大字符数
MAX_QUOTES_TOTAL_CHARS = 12000    # 引用合计最大字符数


def _normalize_text(value: Any) -> str:
    """规整单段引用文本：统一换行符为 \\n，去掉首尾空白（保留内部空白/换行）。"""
    if not isinstance(value, str):
        return ""
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    return text.strip()


def _normalize_source(value: Any) -> dict[str, Any] | None:
    """规整引用来源信息（仅用于卡片提示与可选定位，不作为模型回答依据）。"""
    if not isinstance(value, dict):
        return None
    source: dict[str, Any] = {}
    role = value.get("role")
    if isinstance(role, str) and role.strip() in {"user", "assistant"}:
        source["role"] = role.strip()
    session_id = value.get("session_id")
    if isinstance(session_id, str) and session_id.strip():
        source["session_id"] = session_id.strip()
    round_number = value.get("round")
    if isinstance(round_number, int) and not isinstance(round_number, bool) and round_number > 0:
        source["round"] = round_number
    event_index = value.get("event_index")
    if isinstance(event_index, int) and not isinstance(event_index, bool) and event_index >= 0:
        source["event_index"] = event_index
    return source or None


def normalize_quotes(raw: Any, *, strict: bool = False) -> list[dict[str, Any]]:
    """把任意输入的引用数组规整为标准结构。

    标准结构：[{"text": 纯文本, "source": {role?, session_id?, round?}?}]
    （前端草稿中的 `id` 仅作 UI 标识，序列化前统一丢弃）。

    strict=True（请求校验）：任何不合法项（非数组/超量/空文本/超长/合计超限）
    抛 ValueError，由路由层转 400 可读错误，不默默截断；
    strict=False（读取历史/容错路径）：跳过非法项、超量截断、超长裁剪，
    尽量保住可用内容。
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        if strict:
            raise ValueError("quotes 必须是数组")
        return []
    if strict and len(raw) > MAX_QUOTES:
        raise ValueError(f"引用最多 {MAX_QUOTES} 段（当前 {len(raw)} 段）")

    result: list[dict[str, Any]] = []
    total_chars = 0
    for item in raw:
        if len(result) >= MAX_QUOTES:
            break
        if not isinstance(item, dict):
            if strict:
                raise ValueError("引用条目必须是对象")
            continue
        text = _normalize_text(item.get("text"))
        if not text:
            if strict:
                raise ValueError("引用内容不能为空")
            continue
        if len(text) > MAX_QUOTE_CHARS:
            if strict:
                raise ValueError(
                    f"单段引用最多 {MAX_QUOTE_CHARS} 个字符（当前 {len(text)} 个）"
                )
            text = text[:MAX_QUOTE_CHARS]
        if total_chars + len(text) > MAX_QUOTES_TOTAL_CHARS:
            if strict:
                raise ValueError(
                    f"引用合计最多 {MAX_QUOTES_TOTAL_CHARS} 个字符"
                    f"（当前合计 {total_chars + len(text)} 个）"
                )
            # 容错路径：装不下就到此为止，保住已收集的引用
            break
        total_chars += len(text)
        entry: dict[str, Any] = {"text": text}
        source = _normalize_source(item.get("source"))
        if source:
            entry["source"] = source
        result.append(entry)
    return result


def escape_xml_text(text: str) -> str:
    """XML 文本转义：按 &、<、>、"、' 的顺序替换。

    引用里出现 `</li>`、代码或换行时也必须仍作为引用内容（成对 `</li>`，
    不能把第二个 `<li>` 当作结束标签）。
    """
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def serialize_quotes_for_model(quotes: Any) -> str:
    """把引用快照序列化为模型视图的 `<quote_list>` 文本块；无引用返回空串。"""
    normalized = normalize_quotes(quotes, strict=False)
    if not normalized:
        return ""
    lines = ["<quote_list>"]
    for quote in normalized:
        lines.append(f"<li>{escape_xml_text(quote['text'])}</li>")
    lines.append("</quote_list>")
    return "\n".join(lines)


def content_with_quotes(content: Any, quotes: Any) -> Any:
    """把引用块前置到一条 user 消息的文本部分（不修改入参）。

    - 字符串 content：`<quote_list>…</quote_list>\\n\\n原问题`；
    - 多部件列表：组装文本作为第一个 text 部件，其余媒体部件保持原顺序；
    - 无引用时原样返回。
    """
    block = serialize_quotes_for_model(quotes)
    if not block:
        return content
    if isinstance(content, list):
        return [{"type": "text", "text": block}] + list(content)
    text = content if isinstance(content, str) else ""
    return block + ("\n\n" + text if text else "")


def _find_user_event(round_entry: Any) -> dict[str, Any] | None:
    if not isinstance(round_entry, dict):
        return None
    events = round_entry.get("events")
    if not isinstance(events, list):
        return None
    for event in events:
        if isinstance(event, dict) and event.get("role") == "user":
            return event
    return None


def round_entry_user_quotes(round_entry: Any) -> list[dict[str, Any]]:
    """从历史轮次条目提取用户引用快照（容错：跳过非法项）。"""
    event = _find_user_event(round_entry)
    if event is None:
        return []
    return normalize_quotes(event.get("quotes"), strict=False)
