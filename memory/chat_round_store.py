# from __future__ import annotations
# 标准库
import json
from dataclasses import dataclass, field
# from datetime import datetime
from typing import Any
# 自定义模块
from util.timestamp_utils import now_str  # 单一来源：见 memory/timestamp_utils.py
from memory.file_memory import content_part_to_text  # 多模态消息的文本提取


# 轮次状态白名单：必须与 ChatRoundStore.record_message 中的合法 finalize 值保持一致
_VALID_ROUND_STATUSES = {"done", "error", "stopped", "interrupted"}
# 合法事件角色：与历史文件中实际出现的 role 字段保持一致
_VALID_EVENT_ROLES = {"user", "assistant", "tool", "system"}


def merge_usage_dict(total: dict[str, Any], delta: dict[str, Any]) -> None:
    if not isinstance(delta, dict):
        return
    for key, value in delta.items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            existed = total.get(key, 0)
            if not isinstance(existed, (int, float)) or isinstance(existed, bool):
                existed = 0
            total[key] = existed + value
        elif isinstance(value, dict):
            child = total.get(key)
            if not isinstance(child, dict):
                child = {}
                total[key] = child
            merge_usage_dict(child, value)


def compression_usage_values(value: Any) -> dict[str, Any]:
    """提取压缩 usage 的数值字段，排除统计辅助字段。"""
    if not isinstance(value, dict):
        return {}
    return {
        key: item
        for key, item in value.items()
        if key not in {"compression_count", "last"}
    }


def merge_compression_usage(total: dict[str, Any], source: Any) -> bool:
    """合并一份原始或已累计的压缩 usage，并保留最后一次结果。"""
    if not isinstance(source, dict):
        return False
    payload = compression_usage_values(source)
    if not payload and isinstance(source.get("last"), dict):
        payload = compression_usage_values(source["last"])
    if not payload:
        return False
    merge_usage_dict(total, payload)
    try:
        source_count = int(source.get("compression_count", 1) or 1)
    except (TypeError, ValueError):
        source_count = 1
    source_count = max(1, source_count)
    try:
        existing_count = int(total.get("compression_count", 0) or 0)
    except (TypeError, ValueError):
        existing_count = 0
    total["compression_count"] = max(0, existing_count) + source_count
    last = source.get("last")
    if not isinstance(last, dict):
        last = source
    try:
        total["last"] = json.loads(json.dumps(last, ensure_ascii=False))
    except (TypeError, ValueError):
        total["last"] = dict(last)
    return True


@dataclass
class ChatRoundStore:
    session_id: str
    pending_round: dict[str, Any] | None = None

    def new_round(self, question: str | None = None) -> dict[str, Any]:
        return {
            "event": "chat_round",
            "question": question or "",
            "started_at": now_str(),
            "events": [],
            "completion_count": 0,
            "usage_total": {},
            "status": "running",
        }

    def record_message(self, record: dict[str, Any]) -> dict[str, Any] | None:
        role = record.get("role")
        if self.pending_round is None:
            if role == "assistant" and (record.get("done") == "[DONE]" or record.get("error") is not None):
                return None
            if role == "user" and record.get("content") == "停止任务":
                return None
            # 多模态消息 content 为部件列表：取文本部件作为轮次问题
            # （不带媒体占位符，保证会话标题是纯文本）
            initial_question = None
            if role == "user":
                initial_question = (
                    content_part_to_text(record.get("content"), include_media_labels=False).strip() or None
                )
            self.pending_round = self.new_round(question=initial_question)

        self.pending_round["events"].append(record)
        if role == "user" and not self.pending_round.get("question"):
            question_text = content_part_to_text(
                record.get("content"), include_media_labels=False
            ).strip()
            if question_text:
                self.pending_round["question"] = question_text

        if role == "assistant" and record.get("done") == "[DONE]":
            return self.finalize_round("done")
        if role == "assistant" and record.get("error") is not None:
            return self.finalize_round("error")
        if role == "user" and record.get("content") == "停止任务":
            return self.finalize_round("stopped")
        return None

    def add_usage(self, usage_total: dict[str, Any], completion_count: int = 0) -> str:
        if self.pending_round is None:
            return "忽略无进行中的轮次"
        if isinstance(usage_total, dict):
            merge_usage_dict(self.pending_round["usage_total"], usage_total)
        if isinstance(completion_count, int) and completion_count > 0:
            self.pending_round["completion_count"] = completion_count
        return "记录成功"

    def snapshot_pending_round(self) -> dict[str, Any] | None:
        """当前待收尾轮次的深拷贝快照（含已积累的全部事件）。

        供持久层写入 `_meta._pending_round_checkpoint` 检查点：轮次收尾前
        事件只存在于内存，服务重启/崩溃会导致工具调用、思考过程、工具结果
        全部丢失；检查点让崩溃后的轮次可恢复为 interrupted 历史。
        """
        if self.pending_round is None:
            return None
        return json.loads(json.dumps(self.pending_round, ensure_ascii=False))

    def update_compression_usage(self, usage: dict[str, Any]) -> bool:
        """记录一次压缩模型调用，并同步计入本轮 usage_total。"""
        if self.pending_round is None:
            return False
        payload = compression_usage_values(usage)
        if not payload:
            return False
        merge_usage_dict(self.pending_round["usage_total"], payload)
        return True

    def current_tool_result_count(self) -> int:
        """返回当前 pending round 已记录的工具结果事件数量。

        压缩游标按工具结果数量计数，而不是按 events 下标计数。这样中间插入
        assistant、usage 或 done 事件后，游标仍能稳定定位到同一段工具轨迹。
        """
        if self.pending_round is None:
            return 0
        events = self.pending_round.get("events")
        if not isinstance(events, list):
            return 0
        return sum(
            1
            for event in events
            if isinstance(event, dict) and event.get("role") == "tool"
        )

    def update_compaction(
        self,
        content: str,
        compress_index: int,
        compress_blocks: list[dict[str, Any]] | None = None,
        compress_usage: dict[str, Any] | None = None,
    ) -> bool:
        """以单轮压缩完成事件更新当前轮的累计工具上下文摘要。

        `compress_blocks` 可选：摘要块列表，每块 `{"content": 文本, "index": 覆盖游标}`；
        新模式通常只有一个累计块。压缩状态只保存于 `events`，不再写入
        `chat_round` 顶层字段。
        """
        if self.pending_round is None:
            return False
        normalized_content = str(content or "").strip()
        try:
            parsed_index = int(compress_index)
        except (TypeError, ValueError):
            return False
        tool_result_count = self.current_tool_result_count()
        if not normalized_content or parsed_index <= 0 or tool_result_count <= 0:
            return False
        normalized_index = min(parsed_index, tool_result_count)
        event: dict[str, Any] = {
            "event": "context_compaction",
            "scope": "round",
            "phase": "done",
            "role": "assistant",
            "summary_text": normalized_content,
            "compress_index": normalized_index,
        }
        if isinstance(compress_blocks, list) and compress_blocks:
            normalized_blocks: list[dict[str, Any]] = []
            for raw_block in compress_blocks:
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
                    normalized_blocks.append({
                        "content": block_content.strip(),
                        "index": min(block_index, tool_result_count),
                    })
            if normalized_blocks:
                event["compress_blocks"] = normalized_blocks
        if isinstance(compress_usage, dict) and compress_usage:
            event["compress_usage"] = dict(compress_usage)
        return self.record_compaction_event(event)

    def record_compaction_event(self, record: dict[str, Any]) -> bool:
        """把单轮压缩事件追加到当前 `chat_round.events`。"""
        if self.pending_round is None or not _is_valid_event(record):
            return False
        if (
            record.get("event") != "context_compaction"
            or record.get("scope") != "round"
            or record.get("phase") not in {"start", "done", "aborted"}
        ):
            return False
        self.pending_round["events"].append(dict(record))
        if record.get("phase") == "done":
            usage = record.get("compress_usage")
            if isinstance(usage, dict):
                self.update_compression_usage(usage)
        return True

    def finalize_round(self, status: str) -> dict[str, Any] | None:
        if self.pending_round is None:
            return None
        completed = self.pending_round
        completed["status"] = status
        completed["ended_at"] = now_str()
        if not isinstance(completed.get("completion_count"), int) or completed.get("completion_count", 0) < 0:
            completed["completion_count"] = 0
        self.pending_round = None
        return completed

    def reset(self) -> None:
        self.pending_round = None


def _is_valid_event(event: Any) -> bool:
    """判断单条事件是否具备基本结构。允许事件缺少 timestamp（写入时回填）。"""
    if not isinstance(event, dict):
        return False
    role = event.get("role")
    if not isinstance(role, str) or role not in _VALID_EVENT_ROLES:
        return False
    if event.get("event") == "context_compaction":
        return (
            role == "assistant"
            and event.get("scope") == "round"
            and event.get("phase") in {"start", "done", "aborted"}
        )
    # 至少需要下列字段之一，避免空事件
    if (
        "content" not in event
        and "tool_calls" not in event
        and "done" not in event
        and "error" not in event
        and "reasoning_content" not in event
        and "tool_call_id" not in event
        and "function_call" not in event
    ):
        return False
    return True


def parse_round_entry(entry: Any) -> dict[str, Any] | None:
    """将外部传入的 dict 规整为合法 `chat_round` 条目，无法解析时返回 None。

    与 ChatRoundStore 写入的格式完全对齐：
    - event == "chat_round"
    - question: 非空字符串
    - events: 列表，至少包含一条 user 事件
    - status: 必须是 _VALID_ROUND_STATUSES 之一
     - started_at / ended_at: 时间戳字符串
     - usage_total: dict
     - completion_count: 整数
    可选的 `compress_content` / `compress_index` / `compress_blocks` 会被保留并按工具结果数量收紧。
    """
    if not isinstance(entry, dict):
        return None
    if entry.get("event") != "chat_round":
        return None

    question = entry.get("question")
    if not isinstance(question, str):
        return None
    question = question.strip()
    if not question:
        return None

    events = entry.get("events")
    if not isinstance(events, list) or not events:
        return None
    normalized_events: list[dict[str, Any]] = []
    has_user_event = False
    for event in events:
        if not _is_valid_event(event):
            continue
        if event.get("role") == "user":
            has_user_event = True
        normalized_events.append(event)
    if not has_user_event or not normalized_events:
        return None

    status = entry.get("status")
    if not isinstance(status, str) or status not in _VALID_ROUND_STATUSES:
        return None

    started_at = entry.get("started_at")
    if not isinstance(started_at, str) or not started_at.strip():
        started_at = now_str()
    ended_at = entry.get("ended_at")
    if not isinstance(ended_at, str) or not ended_at.strip():
        ended_at = started_at

    usage_total = entry.get("usage_total")
    if not isinstance(usage_total, dict):
        usage_total = {}

    completion_count = entry.get("completion_count")
    if not isinstance(completion_count, int) or completion_count < 0:
        completion_count = 0

    normalized_round = {
        "event": "chat_round",
        "question": question,
        "started_at": started_at,
        "events": normalized_events,
        "completion_count": completion_count,
        "usage_total": usage_total,
        "status": status,
        "ended_at": ended_at,
    }

    compress_content = entry.get("compress_content")
    try:
        compress_index = int(entry.get("compress_index", 0) or 0)
    except (TypeError, ValueError):
        compress_index = 0
    tool_result_count = sum(
        1
        for event in normalized_events
        if isinstance(event, dict) and event.get("role") == "tool"
    )
    if (
        isinstance(compress_content, str)
        and compress_content.strip()
        and compress_index > 0
        and tool_result_count > 0
    ):
        normalized_round["compress_content"] = compress_content.strip()
        normalized_round["compress_index"] = min(compress_index, tool_result_count)
    compress_blocks = entry.get("compress_blocks")
    if isinstance(compress_blocks, list) and compress_blocks:
        normalized_blocks: list[dict[str, Any]] = []
        for raw_block in compress_blocks:
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
                and tool_result_count > 0
            ):
                normalized_blocks.append({
                    "content": block_content.strip(),
                    "index": min(block_index, tool_result_count),
                })
        if normalized_blocks:
            normalized_round["compress_blocks"] = normalized_blocks
    return normalized_round
