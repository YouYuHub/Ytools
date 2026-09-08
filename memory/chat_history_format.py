"""会话历史与摘要的格式化工具。

`memory/chat_memory.py` 负责持久化会话元数据，
`factory.agent_runtime.context_compaction` 负责基于这些历史做上下文压缩。
两者都需要把同一个 `chat_round` 条目展平成模型上下文消息，
都需要把 `context_summary` 结构渲染成可读文本，
还需要根据 `compress_content` / `compress_index` 跳过已经被单轮摘要覆盖的工具轨迹，
因此把它们集中到本模块，避免两处平行实现。
"""
# from __future__ import annotations
# 标准库
import json
import re
from typing import Any
# 自定义模块
from factory.agent_runtime.chat_runtime import estimate_text_tokens
from memory.chat_round_store import merge_usage_dict
from memory.file_memory import content_part_to_text

# 摘要块的结构化段落：字段名 -> 渲染/解析用的标准标题。
# 顺序即渲染顺序；所有字段均为可选，空列表/空字符串渲染时整体省略。
SUMMARY_SECTION_FIELDS: list[tuple[str, str]] = [
    ("objective", "【任务目标】"),
    ("completed", "【已完成工作】"),
    ("decisions", "【关键设计决定】"),
    ("open_items", "【未解决问题】"),
    ("files", "【重要文件】"),
    ("key_facts", "【保留事实】"),
    ("tool_state", "【工具状态】"),
]

# 压缩轮次原始问题保留区的 token 预算：保留最近的问题，超出从最旧截断
RECENT_QUESTIONS_TOKEN_BUDGET = 10_000

# 解析时兼容的标题别名（模型可能写出相近标题），映射到标准字段。
_SECTION_ALIASES: dict[str, tuple[str, ...]] = {
    "objective": ("【任务目标】", "【目标】", "【当前任务目标】"),
    "completed": ("【已完成工作】", "【已完成】", "【完成的工作】"),
    "decisions": ("【关键设计决定】", "【关键决定】", "【设计决定】", "【关键决策】"),
    "open_items": ("【未解决问题】", "【未完成事项】", "【未解决】", "【待办】", "【下一步】", "【阻塞】"),
    "files": ("【重要文件】", "【文件索引】", "【关键文件】", "【文件】"),
    "key_facts": ("【保留事实】", "【关键事实】"),
    "tool_state": ("【工具状态】",),
}

_BULLET_PREFIX = re.compile(r"^(?:[-*•]|\d+[.)])\s*")


def _split_segment_items(segment: str) -> list[str]:
    """把标题段落按行拆成条目：去空行、去 bullet/序号前缀、去标题残留行。"""
    items: list[str] = []
    for line in segment.splitlines():
        line = line.strip().lstrip(":：").strip()
        if not line:
            continue
        if any(line.startswith(alias) for aliases in _SECTION_ALIASES.values() for alias in aliases):
            continue
        line = _BULLET_PREFIX.sub("", line).strip()
        if line:
            items.append(line)
    return items


def _round_question(round_entry: Any) -> str | None:
    """提取一个历史轮次的原始用户问题。"""
    if not isinstance(round_entry, dict):
        return None
    question = round_entry.get("question")
    if isinstance(question, str) and question.strip() and question.strip() != "停止任务":
        return question.strip()
    events = round_entry.get("events")
    if isinstance(events, list):
        for event in events:
            if not isinstance(event, dict) or event.get("role") != "user":
                continue
            # 多模态消息 content 为部件列表：取文本部件（不带媒体占位符，
            # 问题/标题口径保持纯文本）
            normalized = content_part_to_text(
                event.get("content"), include_media_labels=False
            ).strip()
            if normalized and normalized != "停止任务":
                return normalized
    return None


def _normalize_recent_questions(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [
        str(question).strip()
        for question in value
        if isinstance(question, str) and question.strip()
    ]


def _normalize_summary_block(value: Any) -> dict[str, Any] | None:
    """规整单个摘要块结构；内容全空时返回 None。"""
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        return {
            "summary": text,
            **{field: [] for field, _ in SUMMARY_SECTION_FIELDS},
        }
    if not isinstance(value, dict):
        return None
    normalized = dict(value)
    summary_text = normalized.get("summary")
    if not isinstance(summary_text, str) or not summary_text.strip():
        summary_text = normalized.get("content")
    normalized["summary"] = summary_text.strip() if isinstance(summary_text, str) else ""
    for field, _header in SUMMARY_SECTION_FIELDS:
        field_value = normalized.get(field)
        if isinstance(field_value, list):
            normalized[field] = [str(item).strip() for item in field_value if str(item).strip()]
        elif isinstance(field_value, str) and field_value.strip():
            normalized[field] = [field_value.strip()]
        else:
            normalized[field] = []
    if not normalized["summary"] and not any(
        normalized[field] for field, _header in SUMMARY_SECTION_FIELDS
    ):
        return None
    return normalized


def _round_compression_state(round_entry: dict[str, Any]) -> tuple[str | None, int]:
    """读取并安全规整单轮压缩状态。

    `compress_index` 表示已纳入摘要的工具结果事件数量，而不是 `events` 的
    数组下标。新格式优先使用 `events` 中最新的 round done 事件；旧格式再回退到
    `compress_blocks` / `compress_content` 顶层字段。
    """
    if not isinstance(round_entry, dict):
        return None, 0
    events = round_entry.get("events")
    tool_result_count = sum(
        1
        for event in events or []
        if isinstance(event, dict) and event.get("role") == "tool"
    )
    if tool_result_count <= 0:
        return None, 0
    embedded_blocks: list[tuple[str, int]] = []
    events = round_entry.get("events")
    if isinstance(events, list):
        for event in events:
            if not isinstance(event, dict):
                continue
            if (
                event.get("event") != "context_compaction"
                or event.get("scope") != "round"
                or event.get("phase") != "done"
            ):
                continue
            content = event.get("summary_text")
            if not isinstance(content, str) or not content.strip():
                content = event.get("content")
            try:
                event_index = int(event.get("compress_index", 0) or 0)
            except (TypeError, ValueError):
                event_index = 0
            event_usage = event.get("compress_usage")
            if event_index <= 0 and isinstance(event_usage, dict):
                try:
                    event_index = int(event_usage.get("compress_index", 0) or 0)
                except (TypeError, ValueError):
                    event_index = 0
            if isinstance(content, str) and content.strip() and event_index > 0:
                embedded_blocks.append((content.strip(), event_index))
    if embedded_blocks:
        content, latest_index = embedded_blocks[-1]
        return content, min(latest_index, tool_result_count)

    raw_blocks = round_entry.get("compress_blocks")
    if isinstance(raw_blocks, list) and raw_blocks:
        valid_blocks: list[tuple[str, int]] = []
        for raw_block in raw_blocks:
            if not isinstance(raw_block, dict):
                continue
            block_content = raw_block.get("content")
            try:
                block_index = int(raw_block.get("index", 0) or 0)
            except (TypeError, ValueError):
                block_index = 0
            if (
                isinstance(block_content, str)
                and block_content.strip()
                and block_index > 0
            ):
                valid_blocks.append((block_content.strip(), block_index))
        if valid_blocks:
            joined_content = "\n\n".join(block_text for block_text, _ in valid_blocks)
            latest_index = max(block_index for _, block_index in valid_blocks)
            return joined_content, min(latest_index, tool_result_count)
    content = round_entry.get("compress_content")
    if not isinstance(content, str) or not content.strip():
        return None, 0
    try:
        compress_index = int(round_entry.get("compress_index", 0) or 0)
    except (TypeError, ValueError):
        return None, 0
    if compress_index <= 0:
        return None, 0
    return content.strip(), min(compress_index, tool_result_count)


def _compressed_tool_call_keys(
    round_entry: dict[str, Any],
    compress_index: int,
) -> tuple[set[str], set[int]]:
    """找出压缩游标之前的工具调用，兼容旧记录缺少 tool_call_id 的情况。"""
    events = round_entry.get("events")
    if not isinstance(events, list) or compress_index <= 0:
        return set(), set()

    skipped_ids: set[str] = set()
    tool_result_ids: list[str] = []
    for event in events:
        if not isinstance(event, dict) or event.get("role") != "tool":
            continue
        if len(tool_result_ids) >= compress_index:
            break
        call_id = event.get("tool_call_id")
        if isinstance(call_id, str) and call_id.strip():
            tool_result_ids.append(call_id)
            skipped_ids.add(call_id)

    all_tool_calls: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict) or event.get("role") != "assistant":
            continue
        tool_calls = event.get("tool_calls")
        if isinstance(tool_calls, list):
            all_tool_calls.extend(
                tool_call for tool_call in tool_calls if isinstance(tool_call, dict)
            )

    skipped_objects: set[int] = set()
    for tool_call in all_tool_calls:
        call_id = tool_call.get("id")
        if isinstance(call_id, str) and call_id in skipped_ids:
            skipped_objects.add(id(tool_call))

    # 没有合法 call_id 的旧事件无法通过 ID 匹配，按工具调用顺序补足游标。
    fallback_needed = max(0, compress_index - len(skipped_ids))
    if fallback_needed:
        for tool_call in all_tool_calls:
            call_id = tool_call.get("id")
            if isinstance(call_id, str) and call_id in skipped_ids:
                continue
            skipped_objects.add(id(tool_call))
            fallback_needed -= 1
            if fallback_needed <= 0:
                break
    return skipped_ids, skipped_objects


def _filter_uncompressed_tool_calls(
    tool_calls: Any,
    skipped_ids: set[str],
    skipped_objects: set[int],
) -> list[dict[str, Any]]:
    if not isinstance(tool_calls, list):
        return []
    remaining: list[dict[str, Any]] = []
    for tool_call in tool_calls:
        if not isinstance(tool_call, dict):
            continue
        call_id = tool_call.get("id")
        if id(tool_call) in skipped_objects:
            continue
        if isinstance(call_id, str) and call_id in skipped_ids:
            continue
        remaining.append(tool_call)
    return remaining


def _resolve_round_user_content(round_entry: dict[str, Any]) -> str | None:
    """提取轮次内第一条非“停止任务”的 user 文本；没有时回退到 question 字段。"""
    events = round_entry.get("events")
    if isinstance(events, list):
        for event in events:
            if not isinstance(event, dict):
                continue
            if event.get("role") != "user":
                continue
            # 多模态消息 content 为部件列表：取文本部件
            normalized_user = content_part_to_text(event.get("content")).strip()
            if normalized_user and normalized_user != "停止任务":
                return normalized_user
    question = round_entry.get("question")
    if isinstance(question, str):
        normalized_question = question.strip()
        if normalized_question and normalized_question != "停止任务":
            return normalized_question
    return None


def _round_entry_to_text_context_messages(round_entry: dict[str, Any]) -> list[dict[str, Any]]:
    """默认模式：工具结果不回传，仅把工具调用参数渲染成 assistant 文本行。

    用户消息为纯文本口径：多模态消息的媒体部件替换为 [图片] 等占位引用
    （content_part_to_text），不回传图片数据本身；仅当前轮用户消息保留
    原始多部件 content 并在发送上游前解析为 base64。media:// 落盘引用
    不膨胀历史 JSONL。
    """
    events = round_entry.get("events")
    if not isinstance(events, list):
        return []
    round_user_content = _resolve_round_user_content(round_entry)
    compression_content, compress_index = _round_compression_state(round_entry)
    skipped_ids, skipped_objects = _compressed_tool_call_keys(round_entry, compress_index)
    assistant_fragments: list[str] = []
    if compression_content:
        assistant_fragments.append(f"【本轮工具压缩摘要】\n{compression_content}")
    for event in events:
        if not isinstance(event, dict):
            continue
        if event.get("role") != "assistant":
            continue
        tool_calls = event.get("tool_calls")
        remaining_tool_calls = _filter_uncompressed_tool_calls(
            tool_calls,
            skipped_ids,
            skipped_objects,
        )
        if compress_index > 0 and isinstance(tool_calls, list) and tool_calls and not remaining_tool_calls:
            # 该 assistant 事件的工具调用已经全部被压缩摘要覆盖。
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
        for tool_call in remaining_tool_calls:
            tool_call_line = format_tool_call_history_line(tool_call)
            if tool_call_line:
                assistant_fragments.append(tool_call_line)
    if not assistant_fragments:
        # 轮次被中断/停止/出错且没有任何助手输出时，仍要回传该轮用户消息：
        # 数据已落盘（含崩溃恢复的 interrupted 轮），丢掉它会让"请继续上面的
        # 任务"这类后续提问在模型侧完全失忆。仅剩"停止任务"等无效内容时
        # round_user_content 为空，仍返回空列表。
        if round_user_content:
            return [{"role": "user", "content": round_user_content}]
        return []
    merged_assistant_fragments: list[str] = []
    for text in assistant_fragments:
        if not merged_assistant_fragments or merged_assistant_fragments[-1] != text:
            merged_assistant_fragments.append(text)
    messages: list[dict[str, Any]] = []
    # 用户消息为纯文本口径（媒体部件为 [图片] 等占位引用）；轮次无 events
    # 中的 user 消息时回退 question 字段的纯文本
    if round_user_content:
        messages.append({"role": "user", "content": round_user_content})
    messages.append({"role": "assistant", "content": "\n".join(merged_assistant_fragments)})
    return messages


def _truncate_tool_result_text(text: str, max_length: int) -> str:
    """按最大长度裁剪单个工具结果；负数表示不裁剪（完整回传）。"""
    if max_length < 0 or len(text) <= max_length:
        return text
    return text[:max_length] + "\n…（工具结果过长，按配置已截断）…"


def _format_tool_calls_for_context(tool_calls: list[Any]) -> list[dict[str, Any]]:
    """把历史 tool_calls 事件规整为 Chat Completions 规范的 tool_calls 结构。

    - 保留 id（缺失时生成 call_N 占位），type 缺省 function
    - function.arguments 统一为 JSON 字符串
    """
    formatted: list[dict[str, Any]] = []
    for tool_call in tool_calls:
        if not isinstance(tool_call, dict):
            continue
        function_info = tool_call.get("function")
        if not isinstance(function_info, dict):
            continue
        tool_name = function_info.get("name")
        if not isinstance(tool_name, str) or not tool_name.strip():
            continue
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
        call_id = tool_call.get("id")
        if not isinstance(call_id, str) or not call_id.strip():
            call_id = f"call_{len(formatted)}"
        formatted.append({
            "id": call_id,
            "type": tool_call.get("type") or "function",
            "function": {"name": tool_name, "arguments": arguments_text},
        })
    return formatted


def _round_entry_to_tool_result_context_messages(
    round_entry: dict[str, Any],
    max_tool_result_length: int,
) -> list[dict[str, Any]]:
    """工具结果回传模式：按 Chat Completions 规范展开为有序消息。

    顺序：user → assistant(tool_calls) → tool(tool_call_id, 截断结果) → assistant(最终答复)。
    tool 事件缺少 tool_call_id 或无法匹配时，回退挂到最近一次工具调用上。
    """
    events = round_entry.get("events")
    if not isinstance(events, list):
        return []
    round_user_content = _resolve_round_user_content(round_entry)
    messages: list[dict[str, Any]] = []
    # 用户消息为纯文本口径（媒体部件为 [图片] 等占位引用），历史轮次不回传
    # 图片数据；仅当前轮用户消息保留原始多部件 content 并在发送前解析
    if round_user_content:
        messages.append({"role": "user", "content": round_user_content})
    compression_content, compress_index = _round_compression_state(round_entry)
    skipped_ids, skipped_objects = _compressed_tool_call_keys(round_entry, compress_index)
    pending_text: list[str] = []
    if compression_content:
        pending_text.append(f"【本轮工具压缩摘要】\n{compression_content}")
    tool_calls_by_id: dict[str, dict[str, Any]] = {}
    last_tool_call_id: str | None = None
    tool_result_position = 0

    def _append_text(text: str) -> None:
        if not pending_text or pending_text[-1] != text:
            pending_text.append(text)

    def _flush_pending_text() -> None:
        if not pending_text:
            return
        messages.append({"role": "assistant", "content": "\n".join(pending_text)})
        pending_text.clear()

    for event in events:
        if not isinstance(event, dict):
            continue
        role = event.get("role")
        if role == "user":
            continue
        if role == "tool":
            _flush_pending_text()
            tool_result_position += 1
            if tool_result_position <= compress_index:
                continue
            content = event.get("result")
            if content is None:
                content = event.get("content")
            if isinstance(content, (dict, list)):
                try:
                    content = json.dumps(content, ensure_ascii=False)
                except TypeError:
                    content = str(content)
            if content is None:
                content = ""
            else:
                content = str(content)
            tool_call_id = event.get("tool_call_id")
            if not (isinstance(tool_call_id, str) and tool_call_id.strip()):
                tool_call_id = None
            if tool_call_id is None or tool_call_id not in tool_calls_by_id:
                if last_tool_call_id is not None:
                    tool_call_id = last_tool_call_id
                else:
                    continue
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": _truncate_tool_result_text(content, max_tool_result_length),
            })
            continue
        if role != "assistant":
            continue
        if event.get("done") == "[DONE]" or event.get("error") is not None:
            continue
        tool_calls = event.get("tool_calls")
        remaining_tool_calls = _filter_uncompressed_tool_calls(
            tool_calls,
            skipped_ids,
            skipped_objects,
        )
        if compress_index > 0 and isinstance(tool_calls, list) and tool_calls and not remaining_tool_calls:
            continue
        content = event.get("content")
        if isinstance(content, str):
            normalized_content = content.strip()
            if normalized_content:
                lowered = normalized_content.lower()
                if not (lowered.startswith("<think>") and lowered.endswith("</think>")):
                    _append_text(normalized_content)
        if not remaining_tool_calls:
            continue
        formatted_tool_calls = _format_tool_calls_for_context(remaining_tool_calls)
        if not formatted_tool_calls:
            continue
        # 同一条 assistant 事件既带文本又带 tool_calls：文本并入该消息，不再单独成条
        assistant_message: dict[str, Any] = {"role": "assistant"}
        if pending_text:
            assistant_message["content"] = "\n".join(pending_text)
            pending_text.clear()
        assistant_message["tool_calls"] = formatted_tool_calls
        messages.append(assistant_message)
        for formatted_call in formatted_tool_calls:
            call_id = formatted_call["id"]
            tool_calls_by_id[call_id] = formatted_call
            last_tool_call_id = call_id
    _flush_pending_text()
    # has_round_output 为 False 时 messages 只包含初始用户消息（或为空）：
    # 中断/停止且无助手输出的轮次同样要保留用户问题，不因无回复而整轮丢弃。
    return messages




def parse_summary_sections(text: Any) -> dict[str, Any]:
    """把压缩模型输出的分段纯文本按标题切分，回填到结构化字段。

    标题之前的内容归入 summary；未发现任何标题时全部归入 summary
    （与旧版纯文本摘要行为一致）。同字段标题重复出现时内容按出现顺序追加。
    """
    result: dict[str, Any] = {"summary": "", **{field: [] for field, _ in SUMMARY_SECTION_FIELDS}}
    if not isinstance(text, str):
        return result
    text = text.strip()
    if not text:
        return result
    positions: list[tuple[int, str, int]] = []
    for field, aliases in _SECTION_ALIASES.items():
        for alias in aliases:
            index = text.find(alias)
            if index >= 0:
                positions.append((index, field, len(alias)))
    if not positions:
        result["summary"] = text
        return result
    positions.sort(key=lambda item: item[0])
    result["summary"] = text[:positions[0][0]].strip()
    for i, (index, field, alias_length) in enumerate(positions):
        end = positions[i + 1][0] if i + 1 < len(positions) else len(text)
        segment = text[index + alias_length:end]
        result[field].extend(_split_segment_items(segment))
    return result


def trim_recent_questions(
    questions: list[str],
    token_budget: int = RECENT_QUESTIONS_TOKEN_BUDGET,
) -> list[str]:
    """保留最近（末尾）的原始问题，累计估算不超过 token 预算；超出时从最旧开始丢弃。

    至少保留最新一条问题（即使单条超过预算），保证模型总能读到最近一次任务意图。
    """
    normalized = [str(question).strip() for question in questions if str(question).strip()]
    result: list[str] = []
    total = 0
    for question in reversed(normalized):
        question_tokens = estimate_text_tokens(question)
        if result and total + question_tokens > token_budget:
            break
        result.append(question)
        total += question_tokens
    result.reverse()
    return result


def trim_recent_question_items(
    items: list[tuple[int | None, str]],
    token_budget: int = RECENT_QUESTIONS_TOKEN_BUDGET,
) -> list[tuple[int | None, str]]:
    """按 token 预算保留最近的（轮次, 问题）条目；超出时从最旧开始丢弃。

    与 `trim_recent_questions` 同规则：至少保留最新一条（即使单条超过预算），
    保证模型总能读到最近一次任务意图。
    """
    result: list[tuple[int | None, str]] = []
    total = 0
    for item in reversed(items):
        question_tokens = estimate_text_tokens(item[1])
        if result and total + question_tokens > token_budget:
            break
        result.append(item)
        total += question_tokens
    result.reverse()
    return result


def extract_recent_questions(
    round_entries: list[dict[str, Any]],
    token_budget: int = RECENT_QUESTIONS_TOKEN_BUDGET,
) -> list[str]:
    """从全部历史轮次提取原始用户问题，并保留最近 token 预算内的部分。"""
    questions = []
    for round_entry in round_entries:
        question = _round_question(round_entry)
        if question:
            questions.append(question)
    return trim_recent_questions(questions, token_budget)


def extract_recent_question_items(
    round_entries: list[dict[str, Any]],
    token_budget: int = RECENT_QUESTIONS_TOKEN_BUDGET,
) -> list[tuple[int | None, str]]:
    """从全部历史轮次提取（轮次编号, 原始用户问题），保留最近 token 预算内的部分。

    轮次编号按 `chat_round` 在历史中的出现顺序从 1 计数（与累计摘要的
    `round_start/round_end` 覆盖范围同一编号体系），让模型能对齐
    "本轮问题 / 上轮问题 / 上上轮问题……"。无法确定编号时为 None。
    """
    items: list[tuple[int | None, str]] = []
    for index, round_entry in enumerate(round_entries, start=1):
        question = _round_question(round_entry)
        if question:
            items.append((index, question))
    return trim_recent_question_items(items, token_budget)


def split_context_window(
    round_entries: list[dict[str, Any]],
    summarized_count: int,
    keep_rounds: int,
    token_budget: int = RECENT_QUESTIONS_TOKEN_BUDGET,
) -> tuple[list[tuple[int | None, str]], list[dict[str, Any]]]:
    """按总轮次窗口切分历史，决定"保真问题索引"与"完整对话轮次"的组成。

    `keep_rounds` 为模型上下文保留的**总轮次窗口**（HISTORY_COMPACT_KEEP_ROUNDS）：

    - 未压缩轮次（summarized_count 之后的轮次）优先占窗口，以**完整对话**回传
      （含助手回答、工具调用等），其问题不再重复进入保真索引；
    - 剩余窗口从最新往回分配给已压缩轮次的原始用户问题（保真索引，
      受 token_budget 限制，超出从最旧丢弃）；
    - `keep_rounds <= 0` 表示无限窗口：全部未压缩轮次完整回传、
      全部已压缩轮次问题进入保真索引（仍受 token_budget 限制）。

    返回 (question_items, raw_rounds_in_window)：
    - question_items: (全局轮次编号, 问题) 列表，编号与累计摘要
      round_start/round_end 同一体系；
    - raw_rounds_in_window: 窗口内未压缩轮次，按时间顺序（最旧在前）。
    """
    safe_count = max(0, summarized_count)
    compressed = round_entries[:safe_count]
    raw = round_entries[safe_count:]
    if keep_rounds is not None and keep_rounds > 0:
        raw_in_window = raw[-keep_rounds:] if len(raw) > keep_rounds else raw
        remaining = keep_rounds - len(raw_in_window)
    else:
        raw_in_window = raw
        remaining = len(compressed)
    compressed_in_window = compressed[-remaining:] if remaining > 0 and compressed else []
    start_number = safe_count - len(compressed_in_window) + 1
    items: list[tuple[int | None, str]] = []
    for offset, entry in enumerate(compressed_in_window):
        question = _round_question(entry)
        if question:
            items.append((start_number + offset, question))
    items = trim_recent_question_items(items, token_budget)
    return items, raw_in_window


def normalize_context_summary(summary: Any) -> dict[str, Any] | None:
    """把任意输入的摘要数据规整成统一结构（兼容单块与多块）。

    返回结构包含:
    - blocks: 摘要块列表，每块含 summary 与结构化段落字段
      （objective / completed / decisions / open_items / files / key_facts / tool_state）
    - 上述字段在顶层拼接后的兼容视图（保持历史字段名，避免破坏旧调用方）
    - source_round_count: 已被压缩的轮次数
    - recent_questions: 全部历史原始用户问题索引（最近 ≤10k tokens，超出从最旧截断）
    """
    if summary is None:
        return None
    if isinstance(summary, str):
        text = summary.strip()
        if not text:
            return None
        return {
            "summary": text,
            **{field: [] for field, _ in SUMMARY_SECTION_FIELDS},
            "blocks": [{"summary": text, **{field: [] for field, _ in SUMMARY_SECTION_FIELDS}}],
            "source_round_count": 0,
            "recent_questions": [],
        }
    if not isinstance(summary, dict):
        return None
    normalized = dict(summary)
    recent_questions = trim_recent_questions(
        _normalize_recent_questions(normalized.get("recent_questions"))
    )
    # 轮次编号与 recent_questions 一一对应（长度不一致时丢弃，宁可退化为纯列表）
    raw_numbers = normalized.get("recent_question_numbers")
    if isinstance(raw_numbers, list) and len(raw_numbers) == len(recent_questions):
        normalized["recent_question_numbers"] = [
            number if isinstance(number, int) and number > 0 else None
            for number in raw_numbers
        ]
    else:
        normalized.pop("recent_question_numbers", None)
    blocks: list[dict[str, Any]] = []
    raw_blocks = normalized.get("blocks")
    if isinstance(raw_blocks, list) and raw_blocks:
        for raw_block in raw_blocks:
            block = _normalize_summary_block(raw_block)
            if block is not None:
                blocks.append(block)
    if not blocks:
        single = _normalize_summary_block(normalized)
        if single is not None:
            blocks.append(single)
    if not blocks:
        # 问题索引可以独立存在：历史已被外部流程清理或摘要尚未生成时，
        # 仍允许上下文构建器回传最近的原始用户问题。
        if not recent_questions:
            return None
        normalized["blocks"] = []
        normalized["summary"] = ""
        for field, _header in SUMMARY_SECTION_FIELDS:
            normalized[field] = []
        try:
            parsed_count = int(normalized.get("source_round_count") or 0)
        except (TypeError, ValueError):
            parsed_count = 0
        normalized["source_round_count"] = max(0, parsed_count)
        normalized["recent_questions"] = recent_questions
        normalized["recent_questions_scope"] = normalized.get(
            "recent_questions_scope", "all_history"
        )
        return normalized
    normalized["blocks"] = blocks
    normalized["summary"] = "\n\n".join(
        block["summary"] for block in blocks if block.get("summary")
    )
    for field, _header in SUMMARY_SECTION_FIELDS:
        flattened: list[str] = []
        for block in blocks:
            flattened.extend(block.get(field) or [])
        normalized[field] = flattened
    source_round_count = normalized.get("source_round_count")
    try:
        parsed_count = int(source_round_count)
    except (TypeError, ValueError):
        parsed_count = 0
    normalized["source_round_count"] = max(0, parsed_count)
    normalized["recent_questions"] = recent_questions
    normalized["recent_questions_scope"] = normalized.get(
        "recent_questions_scope", "all_history"
    )
    return normalized


def render_recent_questions_message(summary: Any) -> dict[str, Any] | None:
    """把问题列表或 `context_summary.recent_questions` 渲染为独立 system 消息。

    放在摘要消息之后，作为"用户最近问题"锚点；
    问题为空时返回 None。渲染前会再次按预算收紧（防御旧数据超预算）。

    `summary` 为 (轮次编号, 问题) 列表时按"第 N 轮"标注真实轮次编号，
    让模型能区分本轮 / 上轮 / 上上轮问题；编号未知（None）时退化为纯问题列表。
    """
    if isinstance(summary, (list, tuple)):
        if summary and isinstance(summary[0], (list, tuple)):
            items = [
                (item[0] if isinstance(item, (list, tuple)) and len(item) > 0 else None,
                 item[1] if isinstance(item, (list, tuple)) and len(item) > 1 else "")
                for item in summary
            ]
            items = trim_recent_question_items(items)
            if not items:
                return None
            lines = ["【用户最近问题】（按轮次，最旧在前）"]
            for round_number, question in items:
                text = str(question).strip()
                if not text:
                    continue
                try:
                    number = int(round_number) if round_number is not None else None
                except (TypeError, ValueError):
                    number = None
                lines.append(
                    f"- 第 {number} 轮：{text}" if number is not None else f"- {text}"
                )
            return {"role": "system", "content": "\n".join(lines)}
        questions = trim_recent_questions([
            question for question in summary if isinstance(question, str)
        ])
    else:
        normalized = normalize_context_summary(summary)
        if not normalized:
            return None
        questions = trim_recent_questions(
            _normalize_recent_questions(normalized.get("recent_questions"))
        )
        # 摘要状态中保存的轮次编号与问题一一对应时，按"第 N 轮"渲染
        numbers = normalized.get("recent_question_numbers")
        if (
            isinstance(numbers, list)
            and len(numbers) == len(questions)
            and any(number is not None for number in numbers)
        ):
            lines = ["【用户最近问题】（按轮次，最旧在前）"]
            for number, question in zip(numbers, questions):
                lines.append(
                    f"- 第 {number} 轮：{question}" if number is not None else f"- {question}"
                )
            return {"role": "system", "content": "\n".join(lines)}
    if not questions:
        return None
    lines = ["【用户最近问题】"]
    lines.extend(f"- {question}" for question in questions)
    return {"role": "system", "content": "\n".join(lines)}


def render_summary_block(block: dict[str, Any]) -> str:
    """把单个摘要块渲染成给模型看的多行文本（不含块标题）。

    首行为覆盖范围标注（如"覆盖轮次 1-5"，让模型感知摘要的时间边界），
    随后是 summary 正文与按 SUMMARY_SECTION_FIELDS 顺序的非空段落；
    段落内容为空时整体省略。
    """
    normalized = _normalize_summary_block(block)
    if not normalized:
        return ""
    lines: list[str] = []
    try:
        round_start = int(normalized.get("round_start") or 0)
        round_end = int(normalized.get("round_end") or 0)
    except (TypeError, ValueError):
        round_start = round_end = 0
    if round_start > 0 and round_end >= round_start:
        range_label = (
            f"覆盖轮次 {round_start}"
            if round_end == round_start
            else f"覆盖轮次 {round_start}-{round_end}"
        )
        lines.append(f"（{range_label}）")
    if normalized.get("summary"):
        lines.append(normalized["summary"])
    for field, header in SUMMARY_SECTION_FIELDS:
        items = normalized.get(field) or []
        if items:
            lines.append(header)
            lines.extend(f"- {item}" for item in items)
    return "\n".join(lines)


def render_context_summary(summary: Any) -> str | None:
    """把摘要结构渲染成给模型看的累计多行文本。

    输入可以是 `str`、`dict`（含 blocks 多块结构），或 `None`（返回 `None`）。
    新格式将 `blocks` 维护为一个累计摘要块，因此不会因为只回传最近几块而
    丢失早期历史；旧格式中的多个块也全部渲染，待下一次压缩时归并为累计块。
    始终先走 `normalize_context_summary`，因此容忍历史遗留的 `content` 字段。
    """
    normalized = normalize_context_summary(summary)
    if not normalized:
        return None
    blocks = normalized.get("blocks") or []
    if not blocks:
        return None
    if len(blocks) == 1:
        return f"【历史压缩摘要】\n{render_summary_block(blocks[0])}"
    sections = []
    for index, block in enumerate(blocks, start=1):
        sections.append(f"【历史压缩摘要 {index}】\n{render_summary_block(block)}")
    return "\n\n".join(sections)


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


def round_entry_compression_usage(round_entry: dict[str, Any]) -> dict[str, Any]:
    """提取当前轮压缩 usage；新格式从 events 汇总，旧格式回退顶层字段。"""
    if not isinstance(round_entry, dict):
        return {}
    total: dict[str, Any] = {}
    compression_count = 0
    events = round_entry.get("events")
    if isinstance(events, list):
        for event in events:
            if not isinstance(event, dict) or event.get("event") != "context_compaction":
                continue
            if event.get("scope") != "round" or event.get("phase") != "done":
                continue
            usage = event.get("compress_usage")
            if isinstance(usage, dict):
                merge_usage_dict(
                    total,
                    {
                        key: value
                        for key, value in usage.items()
                        if key not in {"compression_count", "last"}
                    },
                )
                try:
                    source_count = int(usage.get("compression_count", 1) or 1)
                except (TypeError, ValueError):
                    source_count = 1
                compression_count += max(1, source_count)
    if total:
        total["compression_count"] = compression_count
        return total
    usage = round_entry.get("compress_usage")
    return dict(usage) if isinstance(usage, dict) else {}


def round_entry_to_minimal_messages(round_entry: dict[str, Any]) -> list[dict[str, Any]]:
    """极简模式：仅保留用户问题与助手文本回答，思考过程与工具调用/结果不传递。

    供切换到更小窗口模型后的最后兜底降级使用：强制压缩与轮次递减后仍放不下
    一轮完整轨迹时，以最小信息量维持对话连续性。
    """
    if not isinstance(round_entry, dict):
        return []
    round_user_content = _resolve_round_user_content(round_entry)
    events = round_entry.get("events")
    answer_fragments: list[str] = []
    if isinstance(events, list):
        for event in events:
            if not isinstance(event, dict) or event.get("role") != "assistant":
                continue
            if event.get("done") == "[DONE]" or event.get("error") is not None:
                continue
            content = event.get("content")
            if isinstance(content, str):
                normalized = content.strip()
                if normalized:
                    lowered = normalized.lower()
                    if not (lowered.startswith("<think>") and lowered.endswith("</think>")):
                        answer_fragments.append(normalized)
    if not answer_fragments:
        # 中断/停止且无任何助手文本的轮次：仍保留用户问题，不整轮丢弃
        if round_user_content:
            return [{"role": "user", "content": round_user_content}]
        return []
    merged: list[str] = []
    for text in answer_fragments:
        if not merged or merged[-1] != text:
            merged.append(text)
    messages: list[dict[str, Any]] = []
    if round_user_content:
        messages.append({"role": "user", "content": round_user_content})
    messages.append({"role": "assistant", "content": "\n".join(merged)})
    return messages


def round_entry_to_context_messages(
    round_entry: dict[str, Any],
    max_tool_result_length: int = 0,
) -> list[dict[str, Any]]:
    """把一个 `chat_round` 条目展平成可用于模型上下文的消息列表。

    - 跳过 `done/error/think` 等内部事件
    - 用户侧只保留第一条非“停止任务”文本
    - assistant 内容按行拼接，遇到连续重复文本会去重

    当 `max_tool_result_length` 非 0 时，历史轮次的工具结果会按 Chat Completions
    规范（assistant.tool_calls → tool.tool_call_id）回传给模型：
    - 0：不回传工具结果（默认，仅保留工具调用参数行）
    - 负数：完整回传每个工具结果
    - 正数：每个工具结果最多回传前 N 个字符，超出部分以省略标记结尾
    """
    if not isinstance(round_entry, dict):
        return []
    events = round_entry.get("events")
    if not isinstance(events, list):
        return []
    if max_tool_result_length == 0:
        return _round_entry_to_text_context_messages(round_entry)
    return _round_entry_to_tool_result_context_messages(round_entry, max_tool_result_length)


def round_entry_to_compaction_text(round_entry: dict[str, Any]) -> str:
    """把一个历史轮次渲染为供压缩模型阅读的完整文本。

    常规后端历史上下文会刻意省略工具原始输出以控制体积；压缩时则必须看到
    工具输出，才能保留路径、数值、错误和最终结论。最终体积由压缩器按目标模型
    的输入窗口统一裁剪。
    """
    if not isinstance(round_entry, dict):
        return ""
    lines: list[str] = []
    question = round_entry.get("question")
    if isinstance(question, str) and question.strip():
        lines.append(f"【用户问题】{question.strip()}")
    compression_content, compress_index = _round_compression_state(round_entry)
    skipped_ids, skipped_objects = _compressed_tool_call_keys(round_entry, compress_index)
    if compression_content:
        lines.append(f"【本轮已有压缩摘要】\n{compression_content}")
    events = round_entry.get("events")
    if not isinstance(events, list):
        return "\n\n".join(lines)
    tool_result_position = 0
    for event in events:
        if not isinstance(event, dict):
            continue
        role = event.get("role")
        if role == "user":
            # 多模态消息 content 为部件列表：取文本部件
            normalized = content_part_to_text(event.get("content")).strip()
            if normalized and normalized != question:
                lines.append(f"【用户】{normalized}")
        elif role == "assistant":
            tool_calls = event.get("tool_calls")
            remaining_tool_calls = _filter_uncompressed_tool_calls(
                tool_calls,
                skipped_ids,
                skipped_objects,
            )
            if compress_index > 0 and isinstance(tool_calls, list) and tool_calls and not remaining_tool_calls:
                continue
            content = event.get("content")
            if isinstance(content, str) and content.strip():
                lines.append(f"【助手】{content.strip()}")
            for tool_call in remaining_tool_calls:
                tool_line = format_tool_call_history_line(tool_call)
                if tool_line:
                    lines.append(tool_line)
            error = event.get("error")
            if error is not None:
                lines.append(f"【助手错误】{error}")
        elif role == "tool":
            tool_result_position += 1
            if tool_result_position <= compress_index:
                continue
            tool_name = event.get("tool_name") or event.get("name") or "未知工具"
            arguments = event.get("arguments")
            result = event.get("result")
            if result is None:
                result = event.get("content")
            if isinstance(arguments, (dict, list)):
                try:
                    arguments = json.dumps(arguments, ensure_ascii=False)
                except TypeError:
                    arguments = str(arguments)
            if arguments is not None:
                lines.append(f"【工具调用】{tool_name}({arguments})")
            if result is not None:
                if isinstance(result, (dict, list)):
                    try:
                        result = json.dumps(result, ensure_ascii=False)
                    except TypeError:
                        result = str(result)
                lines.append(f"【工具结果：{tool_name}】\n{result}")
    return "\n\n".join(lines)
