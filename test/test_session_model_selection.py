"""会话独立模型选择（_meta.model_selection + ambient 覆盖）行为验证：

- 解析顺序：按角色独立「会话覆盖 → 全局默认」；覆盖的模型已不存在时警告并回退；
- update 规整（角色校验/条目规整/清除语义）与 clear_chat_history 保留；
- meta 重算对 model_selection 的规范化（有效保留 / 空或非法清除）；
- env_manager ambient 覆盖的任务隔离与读取链（get_role_selection / 参数回退）。
"""
import asyncio
import sys
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memory.chat_memory import (
    ChatMemoryManager,
    _recompute_meta_from_entries,
    cleanup_chat_memory_manager,
    get_chat_memory_manager,
    read_session_meta_value,
    resolve_session_model_selection,
)


def _new_sid() -> str:
    return f"modelsel_test_{uuid.uuid4().hex}"


def _cleanup(sid: str) -> None:
    asyncio.run(cleanup_chat_memory_manager(sid))
    ChatMemoryManager.delete_chat_session_file(sid)
    lock_file = Path("history_files") / "lock" / f"{sid}_chat.jsonl.lock"
    if lock_file.exists():
        lock_file.unlink()


def test_default_meta_contains_model_selection_none():
    sid = _new_sid()
    try:
        ChatMemoryManager(sid)
        assert read_session_meta_value(sid, "model_selection") is None
    finally:
        _cleanup(sid)
    print("PASS: 新会话 _meta.model_selection 默认 None（未覆盖）")


def test_update_session_model_selection_validation_and_clear():
    sid = _new_sid()
    try:
        manager = asyncio.run(get_chat_memory_manager(sid))
        # 未知角色 → ValueError
        try:
            asyncio.run(manager.update_session_model_selection("subagent_model", {
                "ownership_name": "P", "model_name": "M",
            }))
            raise AssertionError("未知角色应抛 ValueError")
        except ValueError:
            pass
        # 缺少 model_name 的条目 → ValueError
        try:
            asyncio.run(manager.update_session_model_selection("chat_model", {"ownership_name": "P"}))
            raise AssertionError("缺少 model_name 应抛 ValueError")
        except ValueError:
            pass
        # 合法条目 → 落盘并规整（parameter 迁移为分桶结构）
        meta = asyncio.run(manager.update_session_model_selection("chat_model", {
            "ownership_name": "Provider A",
            "model_name": "Model A",
            "parameter": {"temperature": 0.3},
            "api_type": "chat_completions",
        }))
        stored = meta["model_selection"]["chat_model"]
        assert stored["ownership_name"] == "Provider A"
        assert stored["parameter"] == {"chat_completions": {"temperature": 0.3}}, stored
        # 追加第二个角色
        asyncio.run(manager.update_session_model_selection("title_model", {
            "ownership_name": "Provider B", "model_name": "Model B",
        }))
        assert set(read_session_meta_value(sid, "model_selection").keys()) == {"chat_model", "title_model"}
        # 清除单个角色
        meta = asyncio.run(manager.update_session_model_selection("chat_model", None))
        assert "chat_model" not in meta["model_selection"]
        # 清除最后一个角色 → 字段整体移除
        meta = asyncio.run(manager.update_session_model_selection("title_model", None))
        assert "model_selection" not in meta
        assert read_session_meta_value(sid, "model_selection") is None
    finally:
        _cleanup(sid)
    print("PASS: 会话模型选择写入校验/规整 + 按角色清除 + 空选择移除字段")


def test_resolve_overrides_per_role_and_falls_back():
    sid = _new_sid()
    try:
        import memory.chat_memory as cm
        fake_global = {
            "chat_model": {"ownership_name": "G", "model_name": "GlobalChat", "parameter": {}, "api_type": "chat_completions"},
            "compaction_model": {"ownership_name": "G", "model_name": "GlobalCompact", "parameter": {}, "api_type": "chat_completions"},
            "title_model": {"ownership_name": None, "model_name": None, "parameter": {}, "api_type": None},
        }
        manager = asyncio.run(get_chat_memory_manager(sid))
        asyncio.run(manager.update_session_model_selection("chat_model", {
            "ownership_name": "S", "model_name": "SessionChat",
        }))
        asyncio.run(manager.update_session_model_selection("compaction_model", {
            "ownership_name": "S", "model_name": "GoneModel",
        }))
        with patch.object(cm, "_get_global_model_selection", return_value=fake_global), \
                patch.object(
                    cm, "_get_model_config",
                    side_effect=lambda p, m: None if m == "GoneModel" else {"name": m},
                ):
            effective, warnings = resolve_session_model_selection(sid)
            assert effective["chat_model"]["model_name"] == "SessionChat", "会话覆盖角色应生效"
            assert effective["compaction_model"]["model_name"] == "GlobalCompact", "失效覆盖应回退全局"
            assert warnings and "compaction_model" in warnings[0], "失效覆盖应产生警告"
    finally:
        _cleanup(sid)
    print("PASS: 按角色覆盖生效；失效覆盖警告并回退全局")


def test_clear_chat_history_preserves_model_selection():
    sid = _new_sid()
    try:
        manager = asyncio.run(get_chat_memory_manager(sid))
        asyncio.run(manager.update_session_model_selection("chat_model", {
            "ownership_name": "P", "model_name": "M",
        }))
        asyncio.run(manager.add_chat_history({"role": "user", "content": "问题"}))
        asyncio.run(manager.clear_chat_history())
        stored = read_session_meta_value(sid, "model_selection")
        assert stored and stored["chat_model"]["model_name"] == "M", "清空历史不应重置会话模型选择"
    finally:
        _cleanup(sid)
    print("PASS: clear_chat_history 保留 model_selection")


def test_invalid_selection_repair_is_persisted_once_and_preserves_valid_roles():
    import memory.chat_memory as cm
    sid = _new_sid()
    try:
        manager = asyncio.run(get_chat_memory_manager(sid))
        for role in ("chat_model", "compaction_model", "sub_agent_model"):
            asyncio.run(manager.update_session_model_selection(role, {
                "ownership_name": "P", "model_name": "Gone", "parameter": {"temperature": 1.8},
            }))
        asyncio.run(manager.update_session_model_selection("title_model", {
            "ownership_name": "P", "model_name": "Valid", "parameter": {"temperature": 0.2},
        }))
        valid_title = read_session_meta_value(sid, "model_selection")["title_model"]
        global_selection = {role: {
            "ownership_name": "P", "model_name": "Fallback",
            "parameter": {"chat_completions": {"temperature": 0.7}},
            "api_type": "chat_completions", "headers": [],
        } for role in ("chat_model", "compaction_model", "sub_agent_model", "title_model")}
        with patch.object(cm, "_get_global_model_selection", return_value=global_selection), patch.object(
            cm, "_get_model_config", side_effect=lambda p, m: {"name": m} if m in ("Fallback", "Valid") else None
        ):
            before = manager._file_path.read_bytes()
            _, warnings = resolve_session_model_selection(sid)
            assert len(warnings) == 3
            assert manager._file_path.read_bytes() == before, "GET 解析应保持只读"
            effective, warnings = resolve_session_model_selection(sid, repair_invalid=True)
            assert len(warnings) == 3 and all("回退配置已保存" in w for w in warnings)
            stored = read_session_meta_value(sid, "model_selection")
            assert stored["title_model"] == valid_title
            assert stored["chat_model"] == effective["chat_model"]
            assert stored["chat_model"]["parameter"]["chat_completions"]["temperature"] == 0.7
            after = manager._file_path.read_bytes()
            assert resolve_session_model_selection(sid, repair_invalid=True)[1] == []
            assert manager._file_path.read_bytes() == after, "再次解析不应写盘或重复警告"
    finally:
        _cleanup(sid)


def test_invalid_selection_without_global_model_is_cleared():
    import memory.chat_memory as cm
    sid = _new_sid()
    try:
        manager = asyncio.run(get_chat_memory_manager(sid))
        asyncio.run(manager.update_session_model_selection("sub_agent_model", {
            "ownership_name": "P", "model_name": "Gone",
        }))
        with patch.object(cm, "_get_global_model_selection", return_value={}), patch.object(cm, "_get_model_config", return_value=None):
            resolve_session_model_selection(sid, repair_invalid=True)
            assert read_session_meta_value(sid, "model_selection") is None
            assert resolve_session_model_selection(sid)[1] == []
    finally:
        _cleanup(sid)


def test_selection_repair_rechecks_disk_and_preserves_concurrent_user_choice():
    import memory.chat_memory as cm
    sid = _new_sid()
    try:
        manager = asyncio.run(get_chat_memory_manager(sid))
        asyncio.run(manager.update_session_model_selection("chat_model", {"ownership_name": "P", "model_name": "Gone"}))
        original_read = cm.read_session_meta_value

        def read_then_switch(*args):
            stale = original_read(*args)
            asyncio.run(manager.update_session_model_selection("chat_model", {"ownership_name": "P", "model_name": "NewChoice"}))
            return stale

        with patch.object(cm, "read_session_meta_value", side_effect=read_then_switch), patch.object(
            cm, "_get_global_model_selection", return_value={"chat_model": {"ownership_name": "P", "model_name": "Fallback"}}
        ), patch.object(cm, "_get_model_config", side_effect=lambda p, m: None if m == "Gone" else {"name": m}):
            effective, warnings = resolve_session_model_selection(sid, repair_invalid=True)
            assert effective["chat_model"]["model_name"] == "NewChoice"
            assert warnings == []
            assert original_read(sid, "model_selection")["chat_model"]["model_name"] == "NewChoice"
    finally:
        _cleanup(sid)


def test_recompute_normalizes_model_selection():
    keep = _recompute_meta_from_entries("s", {"title": "t", "model_selection": {
        "chat_model": {"ownership_name": "P", "model_name": "M", "parameter": {}, "api_type": "chat_completions"},
        "title_model": {"ownership_name": None, "model_name": None, "parameter": {}, "api_type": None},
    }}, [], bump_updated_at=False)
    assert set(keep["model_selection"].keys()) == {"chat_model"}, "未配置角色的空条目应被剔除"
    popped = _recompute_meta_from_entries("s", {"title": "t", "model_selection": {}}, [], bump_updated_at=False)
    assert "model_selection" not in popped, "空选择应被重算清除"
    popped = _recompute_meta_from_entries("s", {"title": "t", "model_selection": "bad"}, [], bump_updated_at=False)
    assert "model_selection" not in popped, "非 dict 选择应被重算清除"
    kept_none = _recompute_meta_from_entries("s", {"title": "t", "model_selection": None}, [], bump_updated_at=False)
    assert kept_none.get("model_selection", "missing") is None, "显式 None 表示未覆盖，应保留"
    print("PASS: meta 重算规范化 model_selection")


def test_ambient_override_scopes_and_read_chain():
    """ambient 覆盖：按角色叠加、任务隔离、require/get_role_selection 读取链生效。"""
    import env_manager
    env_manager.init_path(Path(sys.path[0]))  # 测试进程内加载项目真实 models.json
    catalog = [m for m in env_manager.list_available_models()
               if str(m.get("api_type") or "").casefold() == "chat-completions"]
    if len(catalog) < 2:
        print("SKIP: models.json 中 chat-completions 模型不足两个，无法验证 ambient 切换")
        return
    global_selection = env_manager.get_global_model_selection()
    original = global_selection["chat_model"]
    target = next(
        m for m in catalog
        if not (m["provider_name"] == original["ownership_name"] and m["model_name"] == original["model_name"])
    )
    async def scenario():
        token = env_manager.set_ambient_model_selection({"chat_model": {
            "ownership_name": target["provider_name"],
            "model_name": target["model_name"],
        }})
        try:
            config = env_manager.require_default_chat_config()
            assert config["selected_provider_name"] == target["provider_name"], "ambient 应改变聊天模型解析"
            role_selection = env_manager.get_role_selection("chat_model")
            assert role_selection["model_name"] == target["model_name"], "get_role_selection 应读取 ambient"
        finally:
            env_manager.reset_ambient_model_selection(token)
        assert env_manager.get_role_selection("chat_model")["model_name"] == original["model_name"], \
            "reset 后应恢复全局默认"
        # 未覆盖角色不受影响
        assert env_manager.get_role_selection("title_model") == global_selection["title_model"]
    asyncio.run(scenario())
    print("PASS: ambient 覆盖按角色叠加且读取链生效（require_default_chat_config/get_role_selection）")


if __name__ == "__main__":
    test_default_meta_contains_model_selection_none()
    test_update_session_model_selection_validation_and_clear()
    test_resolve_overrides_per_role_and_falls_back()
    test_clear_chat_history_preserves_model_selection()
    test_recompute_normalizes_model_selection()
    test_ambient_override_scopes_and_read_chain()
    print("ALL PASS")
