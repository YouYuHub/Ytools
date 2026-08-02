from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from memory.timestamp_utils import now_str  # 单一来源：见 memory/timestamp_utils.py


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
            initial_question = record.get("content") if role == "user" and isinstance(record.get("content"), str) else None
            self.pending_round = self.new_round(question=initial_question)

        self.pending_round["events"].append(record)
        if role == "user" and not self.pending_round.get("question"):
            content = record.get("content")
            if isinstance(content, str) and content.strip():
                self.pending_round["question"] = content.strip()

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