""" 记录当前轮次对话所有工具调用的历史 - 支持多会话文件持久化 """
from __future__ import annotations

import json
import re
# import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

# fastapi
from fastapi import HTTPException
from fastapi.responses import FileResponse

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


class ChatMemoryManager:
    """工具调用历史管理器 - 持久化到文件，支持多会话隔离"""

    def __init__(self, session_id: str):
        self._run_task = True
        self._lock = threading.Lock()
        self._file_path = _get_chat_history_file(session_id)
        self._file_path.parent.mkdir(parents=True, exist_ok=True)
        self._file_path.touch(exist_ok=True)

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
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        if isinstance(input_text, str):
            record["content"] = input_text
        elif isinstance(input_text, dict):
            record.update(input_text)
        else:
            record["content"] = str(input_text)
        # print(f"add_chat_history: {record}")
        with self._lock:
            _write_jsonline(self._file_path, record)
        return "记录成功"

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
            entries = _read_jsonlines(self._file_path)
        return entries[-number:] if number > 0 else entries

    async def clear_chat_history(self) -> str:
        """
        清空当前会话的所有工具调用历史
        Returns:
            操作结果字符串
        """
        with self._lock:
            self._file_path.write_text("", encoding="utf-8")
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
    def delete_chat_session_file_line(session_id: str, startline: int, endline: int) -> dict[str, str]:
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
            with manager._file_path.open("r", encoding="utf-8") as fp:
                lines = fp.readlines()
            total_lines = len(lines)
            if total_lines == 0:
                return {
                    "state": "failed",
                    "describe": "当前会话历史文件为空，无可删除行"
                }
            # 调整行号范围,确保在有效范围内
            valid_start = max(1, startline)
            valid_end = min(endline, total_lines)
            if valid_start > valid_end:
                return {
                    "state": "failed",
                    "describe": f"指定的行号范围超出文件范围,当前总行数为 {total_lines}"
                }
            # 保留不在删除范围内的行
            remaining_lines = [
                line for index, line in enumerate(lines, start=1)
                if index < valid_start or index > valid_end
            ]
            deleted_count = valid_end - valid_start + 1
            with manager._file_path.open("w", encoding="utf-8") as fp:
                fp.writelines(remaining_lines)
        return {
            "state": "succeed",
            "describe": f"已删除第 {valid_start} 到 {valid_end} 行,共 {deleted_count} 行。原始行数 {total_lines},剩余行数 {len(remaining_lines)}"
        }


# 会话管理器注册表（用于缓存不同session_id的管理器实例）
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
