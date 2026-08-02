""" 记录当前轮次对话所有工具调用的历史 - 支持多会话文件持久化 """
from __future__ import annotations

import json
import re
# import os
import threading
from pathlib import Path
from typing import Any

# fastapi
from fastapi import HTTPException
from fastapi.responses import FileResponse

from memory.chat_round_store import ChatRoundStore, merge_usage_dict
from memory.timestamp_utils import now_str as _now_str

HISTORY_ROOT = Path(__file__).resolve().parents[1] / "history_files"
HISTORY_ROOT.mkdir(parents=True, exist_ok=True)



def _safe_session_id(session_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", session_id)


def _get_chat_history_file(session_id: str) -> Path:
    safe_id = _safe_session_id(session_id)
    return HISTORY_ROOT / f"{safe_id}_chat.jsonl"


def _write_jsonline(file_path: Path, data: dict[str, Any]) -> None:
    with file_path.open("a+", encoding="utf-8") as fp:
        fp.write(json.dumps(data, ensure_ascii=False) + "\n")


def _read_jsonlines(file_path: Path) -> list[dict[str, Any]]:
    if not file_path.exists():
        return []
    entries: list[dict[str, Any]] = []
    with file_path.open("r", encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def _now_str() -> str:
    # 已迁移到 memory.timestamp_utils.now_str；保留本函数作为旧调用方的兼容入口。
    from memory.timestamp_utils import now_str as _impl
    return _impl()


def _default_title(session_id: str) -> str:
    return f"session_{session_id}"


def _default_meta(session_id: str) -> dict[str, Any]:
    now = _now_str()
    return {
        "session_id": session_id,
        "title": _default_title(session_id),
        "user_questions": [],
        "usage": {},
        "created_at": now,
        "updated_at": now,
        "record_count": 0,
        "completion_count": 0,
        "context_summary": None,
    }


def _is_meta_record(entry: Any) -> bool:
    return isinstance(entry, dict) and isinstance(entry.get("_meta"), dict)


# 摘要/历史格式化已迁移到 memory.chat_history_format，本文件仅保留调用入口
from memory.chat_history_format import (
    normalize_context_summary as _normalize_context_summary,
    render_context_summary as _render_context_summary,
    round_entry_to_context_messages as _round_entry_to_context_messages,
    format_tool_call_history_line as _format_tool_call_history_line,
)


def _normalize_usage_record_usage(entry: dict[str, Any]) -> tuple[dict[str, Any], int]:
    usage_total = {}
    completion_count = 0
    if entry.get("event") == "chat_round":
        round_total = entry.get("usage_total")
        if isinstance(round_total, dict):
            usage_total = round_total
        if isinstance(entry.get("completion_count"), int):
            completion_count = entry.get("completion_count", 0)
    return usage_total, completion_count


# 上述格式化函数已迁移至 memory/chat_history_format.py（统一入口）
# 为保留向后兼容，旧名字仍然指向新实现，供其他模块使用。


def _recompute_meta_from_entries(
    session_id: str,
    base_meta: dict[str, Any] | None,
    entries: list[dict[str, Any]]
) -> dict[str, Any]:
    now = _now_str()
    meta = dict(base_meta) if isinstance(base_meta, dict) else _default_meta(session_id)
    if not meta.get("session_id"):
        meta["session_id"] = session_id
    if not meta.get("created_at"):
        meta["created_at"] = now
    title = meta.get("title")
    if not isinstance(title, str) or not title.strip():
        meta["title"] = _default_title(session_id)

    questions: list[str] = []
    usage_total: dict[str, Any] = {}
    completion_count = 0

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("event") == "chat_round":
            question = entry.get("question")
            if isinstance(question, str) and question.strip():
                questions.append(question.strip())
        entry_usage_total, entry_completion_count = _normalize_usage_record_usage(entry)
        if entry_usage_total:
            merge_usage_dict(usage_total, entry_usage_total)
        completion_count += entry_completion_count

    meta["user_questions"] = questions
    meta["usage"] = usage_total
    meta["record_count"] = len(entries)
    meta["completion_count"] = completion_count
    meta["updated_at"] = now
    meta["context_summary"] = _normalize_context_summary(meta.get("context_summary"))

    if (
        meta.get("title") == _default_title(session_id)
        and questions
    ):
        first_q = questions[0]
        meta["title"] = first_q[:40] if first_q else _default_title(session_id)

    return meta


def _load_meta_and_entries(file_path: Path, session_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    rows = _read_jsonlines(file_path)
    if rows and _is_meta_record(rows[0]):
        base_meta = rows[0].get("_meta") or _default_meta(session_id)
        entries = [row for row in rows[1:] if isinstance(row, dict)]
    else:
        base_meta = _default_meta(session_id)
        entries = [row for row in rows if isinstance(row, dict)]
    meta = _recompute_meta_from_entries(session_id, base_meta, entries)
    return meta, entries


def _write_meta_and_entries(file_path: Path, meta: dict[str, Any], entries: list[dict[str, Any]]) -> None:
    with file_path.open("w", encoding="utf-8") as fp:
        fp.write(json.dumps({"_meta": meta}, ensure_ascii=False) + "\n")
        for entry in entries:
            fp.write(json.dumps(entry, ensure_ascii=False) + "\n")


class ChatMemoryManager:
    """工具调用历史管理器 - 持久化到文件，支持多会话隔离"""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self._run_task = True
        self._lock = threading.Lock()
        self._file_path = _get_chat_history_file(session_id)
        self._round_store = ChatRoundStore(session_id)
        self._file_path.parent.mkdir(parents=True, exist_ok=True)
        self._file_path.touch(exist_ok=True)
        with self._lock:
            meta, entries = _load_meta_and_entries(self._file_path, self.session_id)
            _write_meta_and_entries(self._file_path, meta, entries)

    @property
    def run_task(self) -> bool:
        return self._run_task

    @run_task.setter
    def run_task(self, value: bool) -> None:
        """
        优雅控制对话启停功能
        """
        self._run_task = value

    async def add_chat_history(self, input_text: Any) -> str:
        """
        追加工具调用记录到 JSONL 文件中
        Args:
            input_text: 要记录的内容，可以是字符串或字典
        Returns:
            操作结果字符串
        """
        record: dict[str, Any] = {
            "timestamp": _now_str()
        }
        if isinstance(input_text, str):
            record["content"] = input_text
        elif isinstance(input_text, dict):
            record.update(input_text)
        else:
            record["content"] = str(input_text)
        with self._lock:
            meta, entries = _load_meta_and_entries(self._file_path, self.session_id)
            completed_round = self._round_store.record_message(record)
            if completed_round is not None:
                entries.append(completed_round)
                meta = _recompute_meta_from_entries(self.session_id, meta, entries)
                _write_meta_and_entries(self._file_path, meta, entries)
        return "记录成功"

    async def update_current_round_usage(
        self,
        usage_total: dict[str, Any],
        completion_count: int = 0,
    ) -> str:
        """将当前轮次的 usage 汇总挂到内存中的 pending round。"""
        with self._lock:
            return self._round_store.add_usage(usage_total, completion_count)

    async def get_meta(self) -> dict[str, Any]:
        return await self.get_session_meta()

    async def get_session_meta(self) -> dict[str, Any]:
        with self._lock:
            meta, entries = _load_meta_and_entries(self._file_path, self.session_id)
            _write_meta_and_entries(self._file_path, meta, entries)
        return meta

    async def get_session_metadata(self) -> dict[str, Any]:
        return await self.get_session_meta()

    async def get_chat_history(self, number: int = -1) -> list[dict[str, Any]]:
        """
        获取最近工具调用历史列表
        Args:
            number: 获取最后几条数量，-1 表示所有
        Returns:
            历史记录列表
        """
        if not hasattr(number, "__int__"):
            # 抛出类型不匹配错误
            raise TypeError("number 参数必须为整数")
        with self._lock:
            meta, entries = _load_meta_and_entries(self._file_path, self.session_id)
            _write_meta_and_entries(self._file_path, meta, entries)
        return entries[-number:] if number > 0 else entries

    async def get_context_summary(self) -> dict[str, Any] | None:
        with self._lock:
            meta, entries = _load_meta_and_entries(self._file_path, self.session_id)
            _write_meta_and_entries(self._file_path, meta, entries)
        return _normalize_context_summary(meta.get("context_summary"))

    async def update_context_summary(self, summary: dict[str, Any] | str | None) -> str:
        with self._lock:
            meta, entries = _load_meta_and_entries(self._file_path, self.session_id)
            meta["context_summary"] = _normalize_context_summary(summary)
            _write_meta_and_entries(self._file_path, meta, entries)
        return "记录成功"

    async def get_context_messages(self, max_rounds: int = 6) -> list[dict[str, Any]]:
        """
        获取用于模型上下文的历史消息（按轮次聚合后展开）
        Args:
            max_rounds: 最近保留轮次，<=0 表示全部
        Returns:
            符合 ChatCompletion messages 结构的消息列表
        """
        if max_rounds is None:
            max_rounds = 6
        if not hasattr(max_rounds, "__int__"):
            raise TypeError("max_rounds 参数必须为整数")
        max_rounds = int(max_rounds)
        with self._lock:
            meta, entries = _load_meta_and_entries(self._file_path, self.session_id)
            _write_meta_and_entries(self._file_path, meta, entries)

        summary_message = _render_context_summary(meta.get("context_summary"))
        summarized_round_count = 0
        context_summary = _normalize_context_summary(meta.get("context_summary"))
        if context_summary is not None:
            summarized_round_count = min(
                max(0, int(context_summary.get("source_round_count", 0) or 0)),
                len([row for row in entries if isinstance(row, dict) and row.get("event") == "chat_round"]),
            )

        round_entries = [
            row for row in entries
            if isinstance(row, dict) and row.get("event") == "chat_round"
        ]
        if summarized_round_count > 0:
            round_entries = round_entries[summarized_round_count:]
        if max_rounds > 0:
            round_entries = round_entries[-max_rounds:]

        messages: list[dict[str, Any]] = []
        if summary_message:
            messages.append({"role": "system", "content": summary_message})
        for round_entry in round_entries:
            messages.extend(_round_entry_to_context_messages(round_entry))
        return messages

    async def clear_chat_history(self) -> str:
        """
        清空当前会话的所有工具调用历史
        Returns:
            操作结果字符串
        """
        with self._lock:
            self._round_store.reset()
            meta = _default_meta(self.session_id)
            _write_meta_and_entries(self._file_path, meta, [])
        return "清空成功"

    @staticmethod
    def list_chat_sessions():
        """
        列出 HISTORY_ROOT 目录下的所有 jsonl 文件
        :return: 一个包含所有 jsonl 文件路径的列表
        """
        jsonl_files = list(HISTORY_ROOT.glob('*.jsonl'))
        return jsonl_files

    @staticmethod
    def get_chat_session_file(session_id: str):
        """
        返回指定 session_id 的 jsonl 文件响应
        :param session_id: 会话ID
        :return: 文件响应
        """
        chat_history_file = _get_chat_history_file(session_id)
        if chat_history_file.exists():
            return FileResponse(chat_history_file, filename=chat_history_file.name)
        else:
            raise HTTPException(status_code=404, detail="Chat session file not found")

    @staticmethod
    def get_chat_session_meta(session_id: str) -> dict[str, Any]:
        """
        获取指定 session_id 的聊天历史元数据（首行 _meta）
        :param session_id: 会话ID
        :return: 元数据字典
        """
        chat_history_file = _get_chat_history_file(session_id)
        if not chat_history_file.exists():
            raise HTTPException(status_code=404, detail="Chat session file not found")
        manager = ChatMemoryManager(session_id)
        with manager._lock:
            meta, entries = _load_meta_and_entries(manager._file_path, manager.session_id)
            _write_meta_and_entries(manager._file_path, meta, entries)
        return meta

    @staticmethod
    def delete_chat_session_file(session_id: str) -> dict:
        """
        删除指定 session_id 的 jsonl 文件
        :param session_id: 会话 ID
        :return: 操作结果字符串
        """
        chat_history_file = _get_chat_history_file(session_id)
        if chat_history_file.exists():
            try:
                chat_history_file.unlink()
                return {
                    "state": "succeed",
                    "describe": f"Session file {session_id}_chat.jsonl deleted"
                }
            except OSError as e:
                return {
                    "state": "failed",
                    "describe": f"Failed to delete session file {session_id}_chat.jsonl: {str(e)}"
                }
        else:
            return {
                "state": "succeed",
                "describe": f"Session file {session_id}_chat.jsonl not found"
            }

    @staticmethod
    def delete_chat_session_file_line(session_id: str, startline: int, endline: int) -> dict[str, Any]:
        """
        删除指定 session_id 的 jsonl 文件指定行范围
        :param session_id: 会话 ID
        :param startline: 要删除的起始行号(从1开始)
        :param endline: 要删除的结束行号(包含,从1开始)
        :return: 操作结果字典
        """
        # 验证参数
        if startline is None or endline is None:
            raise ValueError("startline 和 endline 参数不能为空")
        try:
            startline = int(startline)
            endline = int(endline)
        except (TypeError, ValueError):
            raise TypeError("startline 和 endline 必须是整数")
        if startline < 1 or endline < 1:
            raise ValueError("行号必须是大于等于 1 的整数")
        if startline > endline:
            raise ValueError(f"起始行号({startline})不能大于结束行号({endline})")
        manager = ChatMemoryManager(session_id)
        with manager._lock:
            meta, entries = _load_meta_and_entries(manager._file_path, manager.session_id)
            total_lines = len(entries)
            if total_lines == 0:
                return {
                    "state": "failed",
                    "describe": "当前会话历史文件为空，无可删除行"
                }
            # 这里的行号以“业务记录行”计数（不包含第一行 _meta）
            valid_start = max(1, startline)
            valid_end = min(endline, total_lines)
            if valid_start > valid_end:
                return {
                    "state": "failed",
                    "describe": f"指定的行号范围超出文件范围,当前总行数为 {total_lines}"
                }
            remaining_entries = [
                entry for index, entry in enumerate(entries, start=1)
                if index < valid_start or index > valid_end
            ]
            deleted_count = valid_end - valid_start + 1
            meta = _recompute_meta_from_entries(manager.session_id, meta, remaining_entries)
            _write_meta_and_entries(manager._file_path, meta, remaining_entries)
        return {
            "state": "succeed",
            "describe": f"已删除第 {valid_start} 到 {valid_end} 行,共 {deleted_count} 行。原始记录数 {total_lines},剩余记录数 {len(remaining_entries)}",
            "meta_after": meta,
            "usage_after": meta.get("usage", {})
        }


# 会话管理器注册表（用于缓存不同 session_id 的管理器实例）
_session_managers: dict[str, ChatMemoryManager] = {}
_session_lock = threading.Lock()


async def get_chat_memory_manager(session_id: str) -> ChatMemoryManager:
    """
    根据 session_id 获取或创建工具记忆管理器实例
    Args:
        session_id: 会话ID
    Returns:
        ChatMemoryManager实例
    """
    with _session_lock:
        if session_id not in _session_managers:
            _session_managers[session_id] = ChatMemoryManager(session_id)
        return _session_managers[session_id]


async def cleanup_chat_memory_manager(session_id: str) -> None:
    """
    清理指定会话的记忆管理器（释放实例引用）
    Args:
        session_id: 会话ID
    """
    with _session_lock:
        if session_id in _session_managers:
            del _session_managers[session_id]


if __name__ == "__main__":
    print("=" * 60)
    print("ChatMemoryManager 并发安全测试")
    print("=" * 60)

    # 测试1：基本功能
    print("\n【测试1】基本功能测试")

    # 为不同session创建独立的管理器
    manager_1 = get_chat_memory_manager("session_1")
    manager_2 = get_chat_memory_manager("session_2")

    # 测试会话1
    manager_1.add_chat_history("工具调用1-1")
    manager_1.add_chat_history("工具调用1-2")
    manager_1.add_chat_history("工具调用1-3")
    print(f"Session 1 历史: {manager_1.get_chat_history()}")

    # 测试会话2
    manager_2.add_chat_history("工具调用2-1")
    manager_2.add_chat_history("工具调用2-2")
    print(f"Session 2 历史: {manager_2.get_chat_history()}")

    # 验证隔离
    print(f"\n验证隔离 - Session 1: {manager_1.get_chat_history()}")
    print(f"验证隔离 - Session 2: {manager_2.get_chat_history()}")

    # 测试2：数量限制
    print("\n【测试2】数量限制测试（最多10条）")
    manager_limit = get_chat_memory_manager("session_limit")
    for i in range(12):
        manager_limit.add_chat_history(f"调用{i+1}")
    history = manager_limit.get_chat_history(10)
    print(f"添加12条后保留: {len(history)} 条")
    print(f"历史记录: {history}")

    # 测试3：文本摘要
    print("\n【测试3】文本摘要测试")
    manager_text = get_chat_memory_manager("session_text")
    manager_text.add_chat_history("第一条很长的工具调用记录" * 10)
    manager_text.add_chat_history("第二条记录")
    manager_text.add_chat_history("第三条记录")
    # text_summary = manager_text.get_chat_history_text(max_total_chars=50)
    # print(f"文本摘要（限制50字符）:\n{text_summary}")

    # 测试4：清理功能
    print("\n【测试4】清理功能测试")
    print(f"清理前 Session 1: {len(manager_1.get_chat_history())} 条")
    manager_1.clear_chat_history()
    print(f"清理后 Session 1: {len(manager_1.get_chat_history())} 条")
    print(f"Session 2 未受影响: {len(manager_2.get_chat_history())} 条")

    # 测试5：并发安全测试
    print("\n【测试5】并发安全测试")
    import time

    errors = []

    def worker(session_id, num_calls):
        try:
            manager = get_chat_memory_manager(session_id)
            for i in range(num_calls):
                manager.add_chat_history(f"Thread-{session_id}-Call-{i}")
                time.sleep(0.001)  # 模拟一些工作
        except Exception as e:
            errors.append(str(e))

    # 创建多个线程同时操作不同的session
    threads = []
    for i in range(5):
        t = threading.Thread(target=worker, args=(f"concurrent_session_{i}", 20))
        threads.append(t)
        t.start()

    # 等待所有线程完成
    for t in threads:
        t.join()

    print(f"并发测试完成，错误数: {len(errors)}")
    if errors:
        print(f"错误详情: {errors[:3]}")

    # 验证每个会话的数据完整性
    for i in range(5):
        session_id = f"concurrent_session_{i}"
        manager = get_chat_memory_manager(session_id)
        history = manager.get_chat_history(10)
        print(f"  {session_id}: {len(history)} 条记录")

    # 清理测试数据
    for i in range(5):
        cleanup_chat_memory_manager(f"concurrent_session_{i}")
    cleanup_chat_memory_manager("session_1")
    cleanup_chat_memory_manager("session_2")
    cleanup_chat_memory_manager("session_limit")
    cleanup_chat_memory_manager("session_text")

    print("\n" + "=" * 60)
    print("所有测试完成！")
    print("=" * 60)
