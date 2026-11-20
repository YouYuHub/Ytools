# coding: utf-8
"""引导消息单条删除（delete_rounds mode=single + event_index）回归。

用户实测问题：运行中插入的 user 引导消息点击「删除」后，整轮（含最初提问与
全部回复）被一起删掉。现改为只摘除该条事件，本轮其余内容与编号保持不变；
首条提问不可按单条删除（承载轮次身份），仍须走整轮删除。
"""
import asyncio
import json

import pytest

from memory import chat_memory as cm
from memory import file_memory as fm


@pytest.fixture(autouse=True)
def isolated_history(tmp_path, monkeypatch):
    root = tmp_path / "history"
    monkeypatch.setattr(cm, "HISTORY_ROOT", root)
    monkeypatch.setattr(cm, "LOCK_ROOT", root / "lock")
    monkeypatch.setattr(cm, "_session_managers", {})
    monkeypatch.setattr(fm, "HISTORY_ROOT", root / "session_files")
    monkeypatch.setattr(fm, "_session_managers", {})


async def _seed_round_with_guide(manager):
    await manager.add_chat_history({"role": "user", "content": "原始任务提问"})
    await manager.add_chat_history({"role": "assistant", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "run_command", "arguments": "{}"}}]})
    await manager.add_chat_history({"role": "tool", "tool_call_id": "c1", "tool_name": "run_command", "result": "ok"})
    # 中途注入的引导消息（events[3]）
    await manager.add_chat_history({"role": "user", "content": "顺便把日志也清理一下", "timestamp": "2026-11-20 12:00:05"})
    await manager.add_chat_history({"role": "assistant", "content": "好的，已处理"})
    await manager.add_chat_history({"role": "assistant", "done": "[DONE]"})
    # 第二轮：验证轮次编号不受影响
    await manager.add_chat_history({"role": "user", "content": "第二轮提问"})
    await manager.add_chat_history({"role": "assistant", "done": "[DONE]"})


def _rounds(manager):
    return [json.loads(line) for line in manager._file_path.read_text(encoding="utf-8").splitlines()
            if '"chat_round"' in line]


def test_delete_guide_event_keeps_round_and_other_messages():
    async def run():
        manager = cm.ChatMemoryManager("guide-delete")
        await _seed_round_with_guide(manager)
        result = await manager.delete_rounds(1, mode="single", event_index=3, delete_files=False)
        assert result["state"] == "succeed" and result["scope"] == "message"
        assert result["removed_rounds"] == 0 and result["removed_messages"] == 1
        rounds = _rounds(manager)
        assert len(rounds) == 2, "轮次数量不变"
        events = rounds[0]["events"]
        contents = [e.get("content") for e in events if e.get("role") == "user"]
        assert contents == ["原始任务提问"], "只删引导消息，首条提问保留"
        assert any(e.get("role") == "tool" for e in events), "工具轨迹保留"
        assert rounds[0]["question"] == "原始任务提问"
        assert rounds[1]["question"] == "第二轮提问"
    asyncio.run(run())


def test_dry_run_previews_without_writing():
    async def run():
        manager = cm.ChatMemoryManager("guide-dry")
        await _seed_round_with_guide(manager)
        before = manager._file_path.read_bytes()
        plan = await manager.delete_rounds(1, mode="single", event_index=3, dry_run=True)
        assert plan["state"] == "planned" and plan["scope"] == "message"
        assert plan["planned_rounds"][0]["event_index"] == 3
        assert "顺便" in plan["planned_rounds"][0]["question"]
        assert manager._file_path.read_bytes() == before
    asyncio.run(run())


def test_first_question_and_non_user_events_are_rejected():
    async def run():
        manager = cm.ChatMemoryManager("guide-guard")
        await _seed_round_with_guide(manager)
        with pytest.raises(ValueError, match="首条提问"):
            await manager.delete_rounds(1, mode="single", event_index=0)
        with pytest.raises(ValueError, match="不是可删除的用户消息"):
            await manager.delete_rounds(1, mode="single", event_index=1)  # assistant tool_calls
        with pytest.raises(ValueError, match="超出该轮范围"):
            await manager.delete_rounds(1, mode="single", event_index=99)
        with pytest.raises(ValueError, match="仅在 mode=single"):
            await manager.delete_rounds(1, mode="truncate", event_index=3)
        # 拒绝路径不得写盘
        rounds = _rounds(manager)
        assert len(rounds[0]["events"]) == 6
    asyncio.run(run())


def test_media_cleanup_only_removes_unreferenced_guide_attachments(tmp_path):
    async def run():
        manager = cm.ChatMemoryManager("guide-media")
        media_dir = fm.HISTORY_ROOT / "guide-media" / "media"
        media_dir.mkdir(parents=True)
        (media_dir / "guide-only.png").write_bytes(b"PNG")
        (media_dir / "shared.png").write_bytes(b"PNG")
        await manager.add_chat_history({"role": "user", "content": [
            {"type": "text", "text": "看这张图"},
            {"type": "image_url", "image_url": {"url": "media://shared.png"}},
        ]})
        await manager.add_chat_history({"role": "user", "content": [
            {"type": "text", "text": "补充这两张"},
            {"type": "image_url", "image_url": {"url": "media://guide-only.png"}},
            {"type": "image_url", "image_url": {"url": "media://shared.png"}},
        ]})
        await manager.add_chat_history({"role": "assistant", "done": "[DONE]"})
        result = await manager.delete_rounds(1, mode="single", event_index=1, delete_files=True)
        assert result["files_cleanup"]["removed"], result
        assert not (media_dir / "guide-only.png").exists(), "仅引导消息引用的附件应清理"
        assert (media_dir / "shared.png").exists(), "首条提问仍引用的附件必须保留"
    asyncio.run(run())


def test_router_passes_event_index(monkeypatch):
    from routers import chat_router

    async def run():
        manager = cm.ChatMemoryManager("guide-router")
        await _seed_round_with_guide(manager)

        async def get_manager(_):
            return manager

        monkeypatch.setattr(chat_router, "get_chat_memory_manager", get_manager)
        monkeypatch.setattr(chat_router, "peek_worker_proxy", lambda _: None)
        monkeypatch.setattr(chat_router, "is_chat_stream_running", lambda _: False)
        response = await chat_router.delete_chat_history_rounds(chat_router.DeleteRoundsRequest(
            session_id="guide-router", start_round=1, mode="single", event_index=3, delete_files=False))
        body = json.loads(response.body)
        assert body["scope"] == "message" and body["removed_messages"] == 1
        assert len(_rounds(manager)) == 2
    asyncio.run(run())
