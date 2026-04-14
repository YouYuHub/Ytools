""" 记录当前轮次对话上传的文件信息，持久化到项目 history 文件夹中 """
from __future__ import annotations

import json
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

HISTORY_ROOT = Path(__file__).resolve().parents[1] / "history_files" / "upload"
HISTORY_ROOT.mkdir(parents=True, exist_ok=True)


def _safe_session_id(session_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", session_id)


def _get_session_dir(session_id: str) -> Path:
    safe_id = _safe_session_id(session_id)
    return HISTORY_ROOT / safe_id


def _safe_filename(filename: str) -> str:
    filename = Path(filename).name
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", filename)
    return safe_name.strip("_.-") or "uploaded_file"


def _get_history_path(session_id: str, filename: str) -> Path:
    session_dir = _get_session_dir(session_id)
    session_dir.mkdir(parents=True, exist_ok=True)
    safe_session = _safe_session_id(session_id)
    safe_name = _safe_filename(filename)
    return session_dir / f"{safe_name}.json"


def _write_json_file(file_path: Path, data: dict[str, Any]) -> None:
    with file_path.open("w", encoding="utf-8") as fp:
        fp.write(json.dumps(data, ensure_ascii=False, indent=2))


def _read_json_file(file_path: Path) -> dict[str, Any] | None:
    if not file_path.exists():
        return None
    try:
        with file_path.open("r", encoding="utf-8") as fp:
            return json.load(fp)
    except (json.JSONDecodeError, OSError):
        return None


def _get_record_timestamp(file_path: Path) -> float:
    data = _read_json_file(file_path)
    if not data:
        try:
            return float(file_path.stat().st_mtime)
        except OSError:
            return 0.0
    timestamp = data.get("timestamp")
    if isinstance(timestamp, str):
        try:
            return datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S").timestamp()
        except ValueError:
            pass
    try:
        return float(file_path.stat().st_mtime)
    except OSError:
        return 0.0


def _list_session_files(session_id: str) -> list[Path]:
    session_dir = _get_session_dir(session_id)
    if not session_dir.exists():
        return []
    files = list(session_dir.glob("*.json"))
    files.sort(key=_get_record_timestamp)
    return files


class FileMemoryManager:
    """文件上传历史管理器 - 持久化到文件，支持多会话隔离"""

    def __init__(self, session_id: str):
        self._lock = threading.Lock()
        self._session_id = session_id
        self._session_dir = _get_session_dir(session_id)
        self._session_dir.mkdir(parents=True, exist_ok=True)

    def add_file_memory(self, file_info: dict[str, Any], max_items: int = 10) -> str:
        """
        将解析后的文件信息保存为单独 JSON 文件，并可按数量保留历史
        Args:
            file_info: 文件信息字典，包含 filename, type, content, size 等字段
            max_items: 最多保存的历史文件数，超过时删除最旧文件
        Returns:
            操作结果字符串
        """
        if not file_info.get("filename"):
            raise ValueError("file_info 必须包含 filename")
        record = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        record.update(file_info)
        with self._lock:
            file_path = _get_history_path(self._session_id, file_info["filename"])
            _write_json_file(file_path, record)
            if max_items and max_items > 0:
                all_files = _list_session_files(self._session_id)
                if len(all_files) > max_items:
                    for old_file in all_files[: len(all_files) - max_items]:
                        old_file.unlink(missing_ok=True)
        return f"保存成功: {file_path.name}"

    def get_file_memory(self, number: int = -1) -> list[dict[str, Any]]:
        """
        获取最近文件上传历史列表
        Args:
            number: 获取最后的指定数量，-1 表示所有
        Returns:
            文件历史记录列表
        """
        if not hasattr(number, "__int__"):
            raise ValueError("number 参数必须为整数")
        with self._lock:
            files = _list_session_files(self._session_id)
            entries: list[dict[str, Any]] = []
            for file_path in reversed(files):
                data = _read_json_file(file_path)
                if data is not None:
                    entries.append(data)
        return entries[:number] if number > 0 else entries

    def get_file_memory_chat(self, number: int = -1) -> list[dict[str, Any]]:
        """
        获取最近文件上传历史列表
        Args:
            number: 获取最后的指定数量，-1 表示所有
        Returns:
            文件历史记录列表
        """
        if not hasattr(number, "__int__"):
            raise ValueError("number 参数必须为整数")
        with self._lock:
            files = _list_session_files(self._session_id)
            entries: list[dict[str, Any]] = []
            for file_path in reversed(files):
                data = _read_json_file(file_path)
                if data is not None:
                    entries.append({
                        "filename": data.get("filename", "unknown"),
                        "content": data.get("content", ""),
                    })
        return entries[:number] if number > 0 else entries

    def get_file_memory_text(self, number: int = -1, max_total_chars: int = 3000) -> str:
        """
        获取最近文件历史的纯文本摘要，用于LLM上下文
        Args:
            number: 获取的记录数量，-1 表示全部
            max_total_chars: 最大返回字符数
        Returns:
            纯文本格式的文件历史摘要
        """
        records = self.get_file_memory(number)
        summary_parts = []
        total_chars = 0
        for record in records:
            content = record.get("content", "")
            if not content:
                continue
            part = (
                f"文件: {record.get('filename', '')} | "
                f"类型: {record.get('type', '')} | "
                f"大小: {record.get('size', '')} | "
                f"内容: {content}"
            )
            if total_chars + len(part) > max_total_chars:
                remaining = max_total_chars - total_chars
                if remaining <= 0:
                    break
                summary_parts.append(part[:remaining])
                break
            summary_parts.append(part)
            total_chars += len(part)
        return "\n".join(summary_parts)

    def clear_file_memory(self) -> str:
        """
        清空当前会话的所有文件上传历史
        Returns:
            操作结果字符串
        """
        with self._lock:
            for file_path in _list_session_files(self._session_id):
                file_path.unlink(missing_ok=True)
        return "清空成功"

    def delete_file_memory(self, filename: str) -> int:
        """
        删除当前会话中指定文件名的历史文件记录
        Args:
            filename: 原始上传文件名
        Returns:
            删除的文件数量
        """
        safe_name = _safe_filename(filename)
        file_path = _get_history_path(self._session_id, filename)
        deleted_count = 0
        with self._lock:
            if file_path.exists():
                file_path.unlink(missing_ok=True)
                deleted_count = 1
            else:
                for existing_path in _list_session_files(self._session_id):
                    if existing_path.stem.endswith(f"_{safe_name}"):
                        existing_path.unlink(missing_ok=True)
                        deleted_count += 1
        return deleted_count


# 会话管理器注册表（用于缓存不同session_id的管理器实例）
_session_managers: dict[str, FileMemoryManager] = {}
_session_lock = threading.Lock()


async def get_file_memory_manager(session_id: str) -> FileMemoryManager:
    """
    根据 session_id 获取或创建文件记忆管理器实例
    Args:
        session_id: 会话ID
    Returns:
        FileMemoryManager实例
    """
    with _session_lock:
        if session_id not in _session_managers:
            _session_managers[session_id] = FileMemoryManager(session_id)
        return _session_managers[session_id]


async def cleanup_file_memory_manager(session_id: str) -> None:
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
    print("FileMemoryManager 并发安全测试")
    print("=" * 60)

    # 测试1：基本功能
    print("\n【测试1】基本功能测试")

    # 为不同session创建独立的管理器
    manager_1 = get_file_memory_manager("session_1")
    manager_2 = get_file_memory_manager("session_2")

    # 测试会话1
    manager_1.add_file_memory({"filename": "test1.pdf", "type": "pdf", "size": 1024})
    manager_1.add_file_memory({"filename": "test2.docx", "type": "docx", "size": 2048})
    print(f"Session 1 文件: {manager_1.get_file_memory()}")

    # 测试会话2
    manager_2.add_file_memory({"filename": "another.txt", "type": "txt", "size": 512})
    print(f"Session 2 文件: {manager_2.get_file_memory()}")

    # 验证隔离
    print(f"\n验证隔离 - Session 1: {len(manager_1.get_file_memory())} 个文件")
    print(f"验证隔离 - Session 2: {len(manager_2.get_file_memory())} 个文件")

    # 测试2：数量限制
    print("\n【测试2】数量限制测试（最多10个文件）")
    manager_limit = get_file_memory_manager("session_limit")
    for i in range(12):
        manager_limit.add_file_memory({
            "filename": f"file_{i+1}.txt",
            "type": "txt",
            "size": 100 * (i + 1)
        })
    files = manager_limit.get_file_memory(10)
    print(f"添加12个文件后保留: {len(files)} 个文件")
    print(f"文件名: {[f['filename'] for f in files]}")

    # 测试3：文本摘要
    print("\n【测试3】文本摘要测试")
    manager_text = get_file_memory_manager("session_text")
    manager_text.add_file_memory({"filename": "文档1.pdf", "type": "pdf", "size": 1024})
    manager_text.add_file_memory({"filename": "文档2.docx", "type": "docx", "size": 2048})
    manager_text.add_file_memory({"filename": "文档3.txt", "type": "txt", "size": 512})
    text_summary = manager_text.get_file_memory_text(max_total_chars=100)
    print(f"文本摘要（限制100字符）:\n{text_summary}")

    # 测试4：清理功能
    print("\n【测试4】清理功能测试")
    print(f"清理前 Session 1: {len(manager_1.get_file_memory())} 个文件")
    manager_1.clear_file_memory()
    print(f"清理后 Session 1: {len(manager_1.get_file_memory())} 个文件")
    print(f"Session 2 未受影响: {len(manager_2.get_file_memory())} 个文件")

    # 测试5：并发安全测试
    print("\n【测试5】并发安全测试")
    import time

    errors = []

    def worker(session_id, num_files):
        try:
            manager = get_file_memory_manager(session_id)
            for i in range(num_files):
                manager.add_file_memory({
                    "filename": f"Thread-{session_id}-File-{i}.txt",
                    "type": "txt",
                    "size": 100 * i
                })
                time.sleep(0.001)  # 模拟一些工作
        except Exception as e:
            errors.append(str(e))

    # 创建多个线程同时操作不同的session
    threads = []
    for i in range(5):
        t = threading.Thread(target=worker, args=(f"concurrent_session_{i}", 15))
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
        manager = get_file_memory_manager(session_id)
        files = manager.get_file_memory(10)
        print(f"  {session_id}: {len(files)} 个文件")

    # 清理测试数据
    for i in range(5):
        cleanup_file_memory_manager(f"concurrent_session_{i}")
    cleanup_file_memory_manager("session_1")
    cleanup_file_memory_manager("session_2")
    cleanup_file_memory_manager("session_limit")
    cleanup_file_memory_manager("session_text")

    print("\n" + "=" * 60)
    print("所有测试完成！")
    print("=" * 60)
