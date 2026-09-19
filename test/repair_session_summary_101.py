# -*- coding: utf-8 -*-
"""一次性修复：yt-2026-09-12_23.16.59_101 会话删除第 72 轮后被作废的累计摘要。

从删除前的 .bak 备份读回 context_summary，按删除轮次 {72} 调整游标/块范围/
问题编号后写回当前会话文件（等价于"删除时不再丢弃摘要"的代码修复对已
损坏会话的追溯应用）。
"""
import asyncio
import io
import json
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from memory import chat_memory  # noqa: E402
from memory.chat_history_format import (  # noqa: E402
    adjust_context_summary_for_deleted_rounds,
    normalize_context_summary,
)

SESSION_ID = "yt-2026-09-12_23.16.59_101"
DELETED_ROUNDS = [72]

history_root = chat_memory.HISTORY_ROOT
session_path = history_root / f"{SESSION_ID}_chat.jsonl"
bak_path = history_root / "sidecars" / f"{SESSION_ID}_chat.jsonl.bak"
safety_copy = history_root / "sidecars" / f"{SESSION_ID}_chat.jsonl.pre-summary-restore.bak"


def read_meta(path: Path) -> dict:
    with io.open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if isinstance(obj, dict) and "_meta" in obj:
                return obj["_meta"]
    raise RuntimeError(f"{path} 缺少 _meta 首行")


def main() -> None:
    assert session_path.exists(), session_path
    assert bak_path.exists(), bak_path

    if not safety_copy.exists():
        shutil.copy2(session_path, safety_copy)
        print(f"[1/4] 当前文件安全备份 -> {safety_copy.name}")

    bak_summary = normalize_context_summary(read_meta(bak_path).get("context_summary"))
    assert bak_summary, "bak 中没有可恢复的 context_summary"
    old_cursor = bak_summary.get("source_round_count")

    adjusted = adjust_context_summary_for_deleted_rounds(bak_summary, DELETED_ROUNDS)
    print(
        f"[2/4] 摘要调整: source_round_count {old_cursor} -> "
        f"{adjusted.get('source_round_count')}, "
        f"blocks={[(b.get('round_start'), b.get('round_end')) for b in adjusted.get('blocks', [])]}, "
        f"recent_questions {len(bak_summary.get('recent_questions') or [])} -> "
        f"{len(adjusted.get('recent_questions') or [])}"
    )

    manager = chat_memory.ChatMemoryManager(SESSION_ID)
    meta, entries = chat_memory._load_meta_and_entries(session_path, SESSION_ID)
    round_questions = [
        (entry.get("question") or "") for entry in entries
        if isinstance(entry, dict) and entry.get("event") == "chat_round"
    ]
    assert len(round_questions) == 79, f"当前轮次数异常: {len(round_questions)}"
    message = asyncio.run(manager.update_context_summary(adjusted))
    print(f"[3/4] 写回当前会话: {message}（轮次 {len(round_questions)} 轮未变动）")

    restored = normalize_context_summary(read_meta(session_path).get("context_summary"))
    assert restored, "写回后读取失败"
    numbers = restored.get("recent_question_numbers") or []
    questions = restored.get("recent_questions") or []
    print(
        f"[4/4] 复核: source_round_count={restored.get('source_round_count')}, "
        f"blocks={[(b.get('round_start'), b.get('round_end')) for b in restored.get('blocks', [])]}, "
        f"recent_question_numbers={numbers}"
    )
    # 旧第 72 轮的问题（标题模型刷新 bug 那轮）必须已从问题索引移除；
    # 新编号 72 现在对应原第 73 轮（编号整体前移后的合法条目）
    removed_question = "标题模型现在生效了"
    assert not any(
        isinstance(q, str) and q.startswith(removed_question) for q in questions
    ), "被删轮次的问题仍留在问题索引中"
    assert len(numbers) == len(questions), "编号与问题列表失配"
    print("OK: 摘要已恢复且编号已按删除第 72 轮平移")


if __name__ == "__main__":
    main()
