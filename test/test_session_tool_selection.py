"""会话独立工具选择（_meta.tool_selection）行为验证：

- 解析顺序：会话覆盖 → 全局默认（mcp_servers.json 的 inputs 键）；
- 全局配置读取失败时发警告并按无默认工具处理；
- update 规整（非法条目丢弃）与清除语义（空 dict/None 清除覆盖）；
- clear_chat_history 清空历史时保留 tool_selection（与 work_dir 一致）；
- meta 重算对 tool_selection 的规范化（有效保留 / 空或非法清除）。
"""
import asyncio
import sys
import uuid
from pathlib import Path

sys.path.insert(0, r"C:\Users\Administrator\Desktop\python学习录\main_study\large_model\agent_tool_sse")

from memory.chat_memory import (
    ChatMemoryManager,
    _recompute_meta_from_entries,
    cleanup_chat_memory_manager,
    get_chat_memory_manager,
    read_session_meta_value,
    resolve_session_tool_selection,
)


def _new_sid() -> str:
    return f"toolsel_test_{uuid.uuid4().hex}"


def _cleanup(sid: str) -> None:
    asyncio.run(cleanup_chat_memory_manager(sid))
    ChatMemoryManager.delete_chat_session_file(sid)
    lock_file = Path("history_files") / f"{sid}_chat.jsonl.lock"
    if lock_file.exists():
        lock_file.unlink()


def test_default_meta_contains_tool_selection_none():
    sid = _new_sid()
    try:
        manager = ChatMemoryManager(sid)
        assert read_session_meta_value(sid, "tool_selection") is None
        assert manager._file_path.exists(), "创建 manager 会写入 _meta"
    finally:
        _cleanup(sid)
    print("PASS: 新会话 _meta.tool_selection 默认 None（未覆盖）")


def test_update_normalizes_and_clears():
    sid = _new_sid()
    try:
        manager = asyncio.run(get_chat_memory_manager(sid))
        # 非法输入类型 → ValueError
        try:
            asyncio.run(manager.update_session_tool_selection("not-a-dict"))
            raise AssertionError("非字典输入应抛 ValueError")
        except ValueError:
            pass
        # 非法条目被丢弃，合法条目保留并去重
        meta = asyncio.run(manager.update_session_tool_selection({
            "sysServer": ["create_file", "", 123, "create_file"],
            "": ["ignored"],
            "sysServer2": "not-a-list",
        }))
        assert meta["tool_selection"] == {"sysServer": ["create_file"]}
        assert read_session_meta_value(sid, "tool_selection") == {"sysServer": ["create_file"]}
        # 空 dict / None → 清除覆盖
        meta = asyncio.run(manager.update_session_tool_selection({}))
        assert "tool_selection" not in meta
        assert read_session_meta_value(sid, "tool_selection") is None
    finally:
        _cleanup(sid)
    print("PASS: 会话工具选择写入规整 + 空值清除覆盖")


def test_resolve_uses_override_first():
    sid = _new_sid()
    override = {"sysServer": ["create_file"], "pipeIpcMcp": ["setup_pipe"]}
    try:
        manager = asyncio.run(get_chat_memory_manager(sid))
        asyncio.run(manager.update_session_tool_selection(override))
        effective, warning = resolve_session_tool_selection(sid)
        assert effective == override, "会话覆盖优先于全局默认"
        assert warning is None
    finally:
        _cleanup(sid)
    print("PASS: 会话工具选择覆盖优先于全局默认")


def test_resolve_falls_back_to_global_inputs():
    sid = _new_sid()
    global_inputs = {"sysServer": ["list_dir"], "pipeIpcMcp": []}
    try:
        import memory.chat_memory as cm
        saved = cm.get_global_tool_inputs
        cm.get_global_tool_inputs = lambda *a, **k: (global_inputs, ["sysServer", "pipeIpcMcp"], None)
        try:
            effective, warning = resolve_session_tool_selection(sid)
            assert effective == global_inputs, "未覆盖时回退全局默认 inputs"
            assert warning is None
        finally:
            cm.get_global_tool_inputs = saved
    finally:
        _cleanup(sid)
    print("PASS: 未覆盖会话回退全局默认工具选择")


def test_resolve_global_read_error_warns():
    sid = _new_sid()
    try:
        import memory.chat_memory as cm
        saved = cm.get_global_tool_inputs
        cm.get_global_tool_inputs = lambda *a, **k: ({}, [], "mcp_servers.json 解析失败")
        try:
            effective, warning = resolve_session_tool_selection(sid)
            assert effective is None
            assert warning and "读取失败" in warning
        finally:
            cm.get_global_tool_inputs = saved
    finally:
        _cleanup(sid)
    print("PASS: 全局配置读取失败 → 警告 + 按无默认工具处理")


def test_clear_chat_history_preserves_tool_selection():
    sid = _new_sid()
    try:
        manager = asyncio.run(get_chat_memory_manager(sid))
        asyncio.run(manager.update_session_tool_selection({"sysServer": ["create_file"]}))
        asyncio.run(manager.add_chat_history({"role": "user", "content": "问题"}))
        asyncio.run(manager.clear_chat_history())
        assert read_session_meta_value(sid, "tool_selection") == {"sysServer": ["create_file"]}, \
            "清空历史不应重置会话工具选择"
    finally:
        _cleanup(sid)
    print("PASS: clear_chat_history 保留 tool_selection")


def test_recompute_normalizes_tool_selection():
    keep = _recompute_meta_from_entries(
        "s", {"title": "t", "tool_selection": {"sysServer": ["a", "a", 1, ""]}}, [], bump_updated_at=False
    )
    assert keep.get("tool_selection") == {"sysServer": ["a"]}, "合法选择应规整保留"
    popped = _recompute_meta_from_entries("s", {"title": "t", "tool_selection": {}}, [], bump_updated_at=False)
    assert "tool_selection" not in popped, "空选择应被重算清除（恢复跟随全局）"
    popped = _recompute_meta_from_entries("s", {"title": "t", "tool_selection": "bad"}, [], bump_updated_at=False)
    assert "tool_selection" not in popped, "非 dict 选择应被重算清除"
    kept_none = _recompute_meta_from_entries("s", {"title": "t", "tool_selection": None}, [], bump_updated_at=False)
    assert kept_none.get("tool_selection", "missing") is None, "显式 None 表示未覆盖，应保留"
    print("PASS: meta 重算规范化 tool_selection")


if __name__ == "__main__":
    test_default_meta_contains_tool_selection_none()
    test_update_normalizes_and_clears()
    test_resolve_uses_override_first()
    test_resolve_falls_back_to_global_inputs()
    test_resolve_global_read_error_warns()
    test_clear_chat_history_preserves_tool_selection()
    test_recompute_normalizes_tool_selection()
    print("ALL PASS")
