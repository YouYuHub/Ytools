# coding: utf-8
"""验证上传 upload_id 记录与会话删除连带清理上传目录的行为。"""
import asyncio
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import HTTPException

import memory.chat_memory as chat_memory
import memory.file_memory as file_memory
from memory.chat_memory import (
    ChatMemoryManager,
    get_chat_memory_manager,
    normalize_session_id,
)
from memory.file_memory import get_file_memory_manager, get_upload_dir_name


class UploadIdAndDeleteTest(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.sid = "test_upload_del"
        # 清掉可能残留的测试数据
        ChatMemoryManager.delete_chat_session_file(self.sid)

    def tearDown(self):
        try:
            ChatMemoryManager.delete_chat_session_file(self.sid)
        except Exception:
            pass
        self.loop.run_until_complete(file_memory.cleanup_file_memory_manager(self.sid))
        self.loop.run_until_complete(chat_memory.cleanup_chat_memory_manager(self.sid))
        self.loop.close()

    def _make_upload_record(self):
        """在 upload/<sid>/ 下创建一个真实的文件记忆记录（模拟上传落盘）。"""
        manager = self.loop.run_until_complete(get_file_memory_manager(self.sid))
        return manager.add_file_memory(
            {"filename": "测试文档.txt", "type": "txt", "content": "hello", "size": 5}
        )

    def _assert_upload_dir_exists(self, exists: bool):
        upload_dir = file_memory._get_session_dir(self.sid)
        self.assertEqual(upload_dir.exists(), exists, f"上传目录应{'存在' if exists else '不存在'}: {upload_dir}")
        return upload_dir

    def test_01_upload_id_recorded_in_meta(self):
        """上传目录按 session_id 命名，并写入会话 jsonl 的 _meta.upload_id。"""
        self._make_upload_record()
        upload_id = get_upload_dir_name(self.sid)
        manager = self.loop.run_until_complete(get_chat_memory_manager(self.sid))
        meta = self.loop.run_until_complete(manager.update_session_upload_id(upload_id))
        self.assertEqual(meta.get("upload_id"), upload_id)
        # 会话文件已创建，且后续写入（如改标题）不会丢失 upload_id
        self.assertTrue(manager._file_path.exists())
        meta2 = self.loop.run_until_complete(manager.update_session_title("新标题"))
        self.assertEqual(meta2.get("upload_id"), upload_id)
        self.assertEqual(meta2.get("title"), "新标题")

    def test_02_delete_removes_jsonl_and_upload_dir(self):
        """删除会话：jsonl 与上传目录一并删除（通过记录的 upload_id）。"""
        self._make_upload_record()
        sid = self.sid
        upload_id = get_upload_dir_name(sid)
        manager = self.loop.run_until_complete(get_chat_memory_manager(sid))
        self.loop.run_until_complete(manager.update_session_upload_id(upload_id))
        self._assert_upload_dir_exists(True)

        result = ChatMemoryManager.delete_chat_session_file(sid)
        self.assertEqual(result["state"], "succeed")
        self.assertIn("会话文件", result["describe"])
        self.assertIn("上传目录", result["describe"])
        self.assertFalse(manager._file_path.exists(), "jsonl 应被删除")
        self._assert_upload_dir_exists(False)

    def test_03_delete_fallback_without_upload_id(self):
        """旧记录（无 upload_id 字段）：按 session_id 推导目录名删除。"""
        self._make_upload_record()
        sid = self.sid
        manager = self.loop.run_until_complete(get_chat_memory_manager(sid))
        meta = self.loop.run_until_complete(manager.get_session_meta())
        self.assertIsNone(meta.get("upload_id"), "本场景不写入 upload_id")
        self._assert_upload_dir_exists(True)

        result = ChatMemoryManager.delete_chat_session_file(sid)
        self.assertEqual(result["state"], "succeed")
        self.assertFalse(manager._file_path.exists())
        self._assert_upload_dir_exists(False)

    def test_04_delete_orphan_upload_dir(self):
        """无 jsonl 但存在上传目录（孤儿）：仍能删除上传目录。"""
        upload_dir = file_memory._get_session_dir(self.sid)
        upload_dir.mkdir(parents=True, exist_ok=True)
        (upload_dir / "a.json").write_text("{}", encoding="utf-8")
        result = ChatMemoryManager.delete_chat_session_file(self.sid)
        self.assertEqual(result["state"], "succeed")
        self._assert_upload_dir_exists(False)

    def test_05_delete_raises_oserror_on_failure(self):
        """删除失败必须抛出 OSError（路由层会转 HTTP 500 给前端）。"""
        upload_dir = file_memory._get_session_dir(self.sid)
        upload_dir.mkdir(parents=True, exist_ok=True)
        (upload_dir / "a.json").write_text("{}", encoding="utf-8")
        with mock.patch.object(
            chat_memory, "_delete_path_with_retry",
            side_effect=OSError(5, "文件被占用"),
        ):
            with self.assertRaises(OSError):
                ChatMemoryManager.delete_chat_session_file(self.sid)

    def test_06_router_returns_500_on_failure(self):
        """/chat_history/delete_file 删除失败返回 HTTP 500（detail 给前端）。"""
        from routers.chat_router import delete_chat_history

        with mock.patch.object(
            ChatMemoryManager, "delete_chat_session_file",
            side_effect=OSError(5, "mock 删除失败"),
        ):
            with self.assertRaises(HTTPException) as ctx:
                self.loop.run_until_complete(delete_chat_history(self.sid))
        self.assertEqual(ctx.exception.status_code, 500)
        self.assertIn("删除会话文件失败", ctx.exception.detail)

    def test_07_router_success_deletes_and_cleans_registries(self):
        """路由层成功删除后清理聊天与文件管理器注册表。"""
        from routers.chat_router import delete_chat_history

        self._make_upload_record()
        self._assert_upload_dir_exists(True)
        response = self.loop.run_until_complete(delete_chat_history(self.sid))
        result = json.loads(response.body)
        self.assertEqual(result.get("state"), "succeed")
        self._assert_upload_dir_exists(False)
        with file_memory._session_lock:
            self.assertNotIn(self.sid, file_memory._session_managers)
        with chat_memory._session_lock:
            self.assertNotIn(self.sid, chat_memory._session_managers)


if __name__ == "__main__":
    unittest.main(verbosity=2)
