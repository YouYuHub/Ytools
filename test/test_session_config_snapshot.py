# -*- coding: utf-8 -*-
"""会话配置快照（ensure_session_config_snapshot）单元测试

验证：首次发起会话任务时把全局默认（工作目录/工具选择/模型选择）固化为
会话独立 _meta 覆盖；幂等（快照后全局变化不再跟随）；已有会话覆盖不覆盖。
"""
import asyncio
import json
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from memory import chat_memory
from memory.chat_memory import ChatMemoryManager

_FAKE_WORK_DIR = "D:/fake_work_root"
# 快照内部用 str(Path(...)) 归一化，Windows 下为反斜杠形态
EXPECTED_WORK_DIR = str(Path(_FAKE_WORK_DIR))
_FAKE_MODEL_SELECTION = {
    "chat_model": {"ownership_name": "p1", "model_name": "m-chat", "api_type": "chat-completions"},
    "compaction_model": {"ownership_name": "p1", "model_name": "m-compact", "api_type": "chat-completions"},
    "title_model": {"ownership_name": "p1", "model_name": "m-title", "api_type": "chat-completions"},
}


def _make_patchers(tool_inputs=None, tool_error=None):
    """统一打桩全局三项读取器（chat_memory 内的名字）。"""
    return [
        patch.object(chat_memory, "get_persisted_work_dir", return_value=_FAKE_WORK_DIR),
        patch.object(chat_memory, "resolve_work_dir", return_value=Path(_FAKE_WORK_DIR)),
        patch.object(
            chat_memory, "get_global_tool_inputs",
            return_value=(dict(tool_inputs or {"sysServer": ["list_dir"]}), ["sysServer"], tool_error),
        ),
        patch.object(
            chat_memory, "_get_global_model_selection",
            return_value=json.loads(json.dumps(_FAKE_MODEL_SELECTION)),
        ),
    ]


class SessionConfigSnapshotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.session_id = f"snap_ut_{uuid.uuid4().hex[:8]}"
        self.manager = ChatMemoryManager(self.session_id)
        for patcher in _make_patchers(tool_inputs={"sysServer": ["list_dir"]}):
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        try:
            self.manager._file_path.unlink(missing_ok=True)
        except OSError:
            pass

    async def test_first_snapshot_writes_all_three(self):
        written, meta = await self.manager.ensure_session_config_snapshot()
        self.assertTrue(written)
        self.assertIn("config_snapshot_at", meta)
        self.assertEqual(meta["work_dir"], EXPECTED_WORK_DIR)
        self.assertEqual(meta["tool_selection"], {"sysServer": ["list_dir"]})
        self.assertEqual(meta["model_selection"]["chat_model"]["model_name"], "m-chat")
        # 落盘复核（读首行 _meta）
        stored = chat_memory.read_session_meta_value(self.session_id, "tool_selection")
        self.assertEqual(stored, {"sysServer": ["list_dir"]})

    async def test_snapshot_idempotent_globals_change_not_followed(self):
        first, _ = await self.manager.ensure_session_config_snapshot()
        self.assertTrue(first)
        # 全局"变化"（换工具集/换模型）后再次发起任务
        second_manager = ChatMemoryManager(self.session_id)
        for patcher in [
            patch.object(chat_memory, "get_global_tool_inputs", return_value=({"otherServer": ["new_tool"]}, ["otherServer"], None)),
            patch.object(chat_memory, "_get_global_model_selection", return_value={"chat_model": {"ownership_name": "pX", "model_name": "m-x", "api_type": "chat-completions"}}),
        ]:
            patcher.start()
            self.addCleanup(patcher.stop)
        again, meta = await second_manager.ensure_session_config_snapshot()
        self.assertFalse(again)
        # 仍保持首次快照的值，未跟随全局变化
        self.assertEqual(meta["tool_selection"], {"sysServer": ["list_dir"]})
        self.assertEqual(meta["model_selection"]["chat_model"]["model_name"], "m-chat")

    async def test_existing_overrides_not_overwritten(self):
        await self.manager.update_session_work_dir(_FAKE_WORK_DIR)
        await self.manager.update_session_tool_selection({"myServer": ["my_tool"]})
        written, meta = await self.manager.ensure_session_config_snapshot()
        self.assertTrue(written)
        self.assertEqual(meta["tool_selection"], {"myServer": ["my_tool"]})
        self.assertEqual(meta["work_dir"], EXPECTED_WORK_DIR)

    async def test_global_read_failure_skips_item_gracefully(self):
        for patcher in [
            patch.object(chat_memory, "get_global_tool_inputs", return_value=({}, [], "mcp_servers.json 读取失败")),
            patch.object(chat_memory, "_get_global_model_selection", side_effect=RuntimeError("boom")),
        ]:
            patcher.start()
            self.addCleanup(patcher.stop)
        written, meta = await self.manager.ensure_session_config_snapshot()
        self.assertTrue(written)  # 快照本身成功（幂等标记落盘）
        # 默认 meta 模板这三键恒存在（None=未覆盖）；失败项保持 None 而非写入全局值
        self.assertIsNone(meta["tool_selection"])
        self.assertIsNone(meta["model_selection"])
        self.assertEqual(meta["work_dir"], EXPECTED_WORK_DIR)


if __name__ == "__main__":
    unittest.main()
