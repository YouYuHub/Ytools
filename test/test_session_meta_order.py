"""会话元数据 updated_at 语义测试：
- 读取 meta 不得改写 updated_at（之前每次读取都会把 updated_at 刷新为当前时间，
  导致前端列表按“最近更新”排序时失真成“按 meta 读取顺序”）
- 真实写入（新增轮次 / 重命名）必须刷新 updated_at
运行：python -m unittest test.test_session_meta_order -v
"""
import asyncio
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from memory import chat_memory


class SessionMetaOrderTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._orig_root = chat_memory.HISTORY_ROOT
        chat_memory.HISTORY_ROOT = Path(self._tmp)

    def tearDown(self):
        chat_memory.HISTORY_ROOT = self._orig_root
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _write_file_with_meta(self, session_id, updated_at):
        fp = chat_memory._get_chat_history_file(session_id)
        meta = chat_memory._default_meta(session_id)
        meta["created_at"] = "2026-08-01 08:00:00"
        meta["updated_at"] = updated_at
        fp.write_text(json.dumps({"_meta": meta}, ensure_ascii=False) + "\n", encoding="utf-8")
        return fp

    def test_read_meta_preserves_updated_at(self):
        self._write_file_with_meta("sess_a", "2026-08-01 10:00:00")
        manager = chat_memory.ChatMemoryManager("sess_a")

        async def run():
            m1 = await manager.get_session_meta()
            m2 = await manager.get_session_meta()
            return m1, m2

        m1, m2 = asyncio.run(run())
        self.assertEqual(m1["updated_at"], "2026-08-01 10:00:00")
        self.assertEqual(m2["updated_at"], m1["updated_at"])
        self.assertEqual(m1["record_count"], 0)

    def test_load_meta_and_entries_preserves_updated_at(self):
        fp = self._write_file_with_meta("sess_b", "2026-08-01 10:00:00")
        meta1, _ = chat_memory._load_meta_and_entries(fp, "sess_b")
        meta2, _ = chat_memory._load_meta_and_entries(fp, "sess_b")
        self.assertEqual(meta1["updated_at"], "2026-08-01 10:00:00")
        self.assertEqual(meta2["updated_at"], meta1["updated_at"])

    def test_manager_init_does_not_bump_updated_at(self):
        # 后端重启后创建文件管理器（构造函数会重写文件）也不得改写更新时间
        self._write_file_with_meta("sess_e", "2026-08-01 10:00:00")
        chat_memory.ChatMemoryManager("sess_e")
        fp = chat_memory._get_chat_history_file("sess_e")
        first_line = json.loads(fp.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(first_line["_meta"]["updated_at"], "2026-08-01 10:00:00")

    def test_completed_round_bumps_updated_at(self):
        self._write_file_with_meta("sess_c", "2026-08-01 10:00:00")
        manager = chat_memory.ChatMemoryManager("sess_c")

        async def run():
            await manager.add_chat_history({"role": "user", "content": "你好"})
            await manager.add_chat_history({"role": "assistant", "content": "你好！"})
            await manager.add_chat_history({"role": "assistant", "done": "[DONE]"})
            return await manager.get_session_meta()

        meta = asyncio.run(run())
        self.assertNotEqual(meta["updated_at"], "2026-08-01 10:00:00")
        self.assertEqual(meta["record_count"], 1)
        self.assertEqual(meta["user_questions"], ["你好"])

    def test_rename_bumps_updated_at(self):
        self._write_file_with_meta("sess_d", "2026-08-01 10:00:00")
        manager = chat_memory.ChatMemoryManager("sess_d")
        meta = asyncio.run(manager.update_session_title("新标题"))
        self.assertNotEqual(meta["updated_at"], "2026-08-01 10:00:00")
        self.assertEqual(meta["title"], "新标题")

    def test_updated_at_not_affected_by_read_order(self):
        # 两个会话读取顺序反过来之后 updated_at 不被改写，排序可稳定复现
        self._write_file_with_meta("older", "2026-08-01 10:00:00")
        self._write_file_with_meta("newer", "2026-08-08 18:33:35")

        manager_older = chat_memory.ChatMemoryManager("older")
        manager_newer = chat_memory.ChatMemoryManager("newer")
        meta_newer = asyncio.run(manager_newer.get_session_meta())
        meta_older = asyncio.run(manager_older.get_session_meta())
        meta_older_again = asyncio.run(manager_older.get_session_meta())

        self.assertEqual(meta_older["updated_at"], "2026-08-01 10:00:00")
        self.assertEqual(meta_older_again["updated_at"], "2026-08-01 10:00:00")
        self.assertEqual(meta_newer["updated_at"], "2026-08-08 18:33:35")
        # newer 始终排在 older 前面这条结论成立（即 updated_at 可反映真实先后）
        self.assertLess(meta_older["updated_at"], meta_newer["updated_at"])


if __name__ == "__main__":
    unittest.main()