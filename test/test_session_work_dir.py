"""会话独立工作目录（_meta.work_dir）行为验证：

- 解析顺序：会话覆盖 → 全局默认（DEFAULT_CHAT_WORK_DIR）→ 当前 cwd；
- 覆盖目录失效时回退全局默认并发警告（决策：warning + 回退，不终止任务）；
- clear_chat_history 清空历史时保留 work_dir（决策：目录是持久配置）；
- meta 重算对 work_dir 的规范化（有效保留 / 非字符串清除）。
"""
import asyncio
import sys
import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, r"C:\Users\Administrator\Desktop\python学习录\main_study\large_model\agent_tool_sse")

from memory.chat_memory import (
    ChatMemoryManager,
    _recompute_meta_from_entries,
    cleanup_chat_memory_manager,
    get_chat_memory_manager,
    read_session_meta_value,
    resolve_session_work_dir,
)


def _new_sid() -> str:
    return f"workdir_test_{uuid.uuid4().hex}"


def _cleanup(sid: str) -> None:
    asyncio.run(cleanup_chat_memory_manager(sid))
    ChatMemoryManager.delete_chat_session_file(sid)
    lock_file = Path("history_files") / "lock" / f"{sid}_chat.jsonl.lock"
    if lock_file.exists():
        lock_file.unlink()


def test_default_meta_contains_work_dir_none():
    sid = _new_sid()
    try:
        manager = ChatMemoryManager(sid)
        with manager._write_guard():
            meta, _ = __import__("memory.chat_memory", fromlist=["_load_meta_and_entries"])._load_meta_and_entries(
                manager._file_path, manager.session_id
            )
        assert meta.get("work_dir", "missing") is None, "新会话 _meta.work_dir 应为 None（未覆盖）"
        assert read_session_meta_value(sid, "work_dir") is None
    finally:
        _cleanup(sid)
    print("PASS: 新会话 _meta.work_dir 默认 None（惰性，不预填）")


def test_read_session_meta_value_is_read_only():
    """读不存在的会话绝不创建文件。"""
    sid = _new_sid()
    value = read_session_meta_value(sid, "work_dir")
    assert value is None
    assert not (Path("history_files") / f"{sid}_chat.jsonl").exists(), "纯读取不应创建会话文件"
    print("PASS: read_session_meta_value 只读不建文件")


def test_resolve_falls_back_to_default_when_no_override():
    sid = _new_sid()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            with patch("memory.chat_memory.get_persisted_work_dir", return_value=tmp), \
                    patch("memory.chat_memory.resolve_work_dir", lambda p: Path(str(p)).resolve()):
                effective, warning = resolve_session_work_dir(sid)
                assert effective == str(Path(tmp).resolve()), "未覆盖时应回退全局默认"
                assert warning is None
    finally:
        _cleanup(sid)
    print("PASS: 未覆盖会话回退全局默认目录")


def test_resolve_uses_override_first():
    sid = _new_sid()
    try:
        with tempfile.TemporaryDirectory() as override_dir, tempfile.TemporaryDirectory() as default_dir:
            manager = asyncio.run(get_chat_memory_manager(sid))
            asyncio.run(manager.update_session_work_dir(override_dir))
            with patch("memory.chat_memory.get_persisted_work_dir", return_value=default_dir), \
                    patch("memory.chat_memory.resolve_work_dir", lambda p: Path(str(p)).resolve()):
                effective, warning = resolve_session_work_dir(sid)
                assert effective == str(Path(override_dir).resolve()), "会话覆盖优先于全局默认"
                assert warning is None
    finally:
        _cleanup(sid)
    print("PASS: 会话覆盖目录优先于全局默认")


def test_resolve_invalid_override_warns_and_falls_back():
    sid = _new_sid()
    try:
        with tempfile.TemporaryDirectory() as default_dir:
            manager = asyncio.run(get_chat_memory_manager(sid))
            # 先设置真实存在的目录，随后让它消失（模拟目录被删除/移动）
            holder = tempfile.TemporaryDirectory()
            gone_dir = holder.name
            asyncio.run(manager.update_session_work_dir(gone_dir))
            holder.cleanup()
            stored_override = read_session_meta_value(sid, "work_dir")
            with patch("memory.chat_memory.get_persisted_work_dir", return_value=default_dir):
                effective, warning = resolve_session_work_dir(sid)
                assert effective == str(Path(default_dir).resolve()), "覆盖失效应回退全局默认"
                assert warning and "已失效" in warning and stored_override in warning, "应发出失效警告"
    finally:
        _cleanup(sid)
    print("PASS: 覆盖目录失效 → 警告 + 回退默认（不终止）")


def test_update_session_work_dir_validation_and_clear():
    sid = _new_sid()
    try:
        manager = asyncio.run(get_chat_memory_manager(sid))
        # 非法目录 → ValueError
        try:
            asyncio.run(manager.update_session_work_dir(r"Z:\__不存在的目录__\nope"))
            raise AssertionError("设置不存在的目录应抛 ValueError")
        except ValueError:
            pass
        # 合法目录 → 落盘
        with tempfile.TemporaryDirectory() as tmp:
            meta = asyncio.run(manager.update_session_work_dir(tmp))
            resolved = str(Path(tmp).resolve())
            assert meta["work_dir"] == resolved
            assert read_session_meta_value(sid, "work_dir") == resolved
            # 空串 → 清除覆盖
            meta = asyncio.run(manager.update_session_work_dir(""))
            assert "work_dir" not in meta
            assert read_session_meta_value(sid, "work_dir") is None
    finally:
        _cleanup(sid)
    print("PASS: 目录写入校验 + 空值清除覆盖")


def test_clear_chat_history_preserves_work_dir():
    sid = _new_sid()
    try:
        manager = asyncio.run(get_chat_memory_manager(sid))
        with tempfile.TemporaryDirectory() as tmp:
            asyncio.run(manager.update_session_work_dir(tmp))
            asyncio.run(manager.add_chat_history({"role": "user", "content": "问题"}))
            asyncio.run(manager.clear_chat_history())
            assert read_session_meta_value(sid, "work_dir") == str(Path(tmp).resolve()), \
                "清空历史不应重置会话工作目录"
    finally:
        _cleanup(sid)
    print("PASS: clear_chat_history 保留 work_dir")


def test_recompute_normalizes_work_dir():
    base = {"title": "t", "work_dir": "  "}
    meta = _recompute_meta_from_entries("s", base, [], bump_updated_at=False)
    assert "work_dir" not in meta, "空白 work_dir 应被重算清除"
    keep = _recompute_meta_from_entries("s", {"title": "t", "work_dir": r"C:\some\dir"}, [], bump_updated_at=False)
    assert keep.get("work_dir") == r"C:\some\dir", "字符串 work_dir 应原样保留（存在性不在重算时校验）"
    print("PASS: meta 重算规范化 work_dir")


if __name__ == "__main__":
    test_default_meta_contains_work_dir_none()
    test_read_session_meta_value_is_read_only()
    test_resolve_falls_back_to_default_when_no_override()
    test_resolve_uses_override_first()
    test_resolve_invalid_override_warns_and_falls_back()
    test_update_session_work_dir_validation_and_clear()
    test_clear_chat_history_preserves_work_dir()
    test_recompute_normalizes_work_dir()
    print("ALL PASS")
