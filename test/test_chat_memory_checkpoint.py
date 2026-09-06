"""会话文件原子写的重试与降级测试（模拟 Windows WinError 5 文件占用场景）：

- 轮次检查点侧车（.pending）写入遇文件占用时按指数退避重试，
  占用释放后成功覆盖写入；
- 重试耗尽仍占用：检查点写入不抛错（抛错会经 add_chat_history 打断
  整个生成轮次），保留旧检查点继续；
- 主历史文件 _write_meta_and_entries 重试耗尽仍抛 OSError，交由上层处理。
运行：python -m unittest test.test_chat_memory_checkpoint -v
"""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from memory import chat_memory


class CheckpointWriteRetryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._orig_root = chat_memory.HISTORY_ROOT
        chat_memory.HISTORY_ROOT = Path(self._tmp)
        # 测试中让退避瞬时完成
        self._sleep_patcher = mock.patch("memory.chat_memory.time.sleep", lambda _s: None)
        self._sleep_patcher.start()

    def tearDown(self):
        self._sleep_patcher.stop()
        chat_memory.HISTORY_ROOT = self._orig_root
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _checkpoint(self, question):
        return {"event": "chat_round", "question": question, "status": "running", "events": []}

    def test_checkpoint_retries_until_lock_released(self):
        manager = chat_memory.ChatMemoryManager("sess_retry")
        real_replace = os.replace
        calls = {"n": 0}

        def flaky_replace(src, dst):
            calls["n"] += 1
            if calls["n"] <= 3:
                raise PermissionError(5, "拒绝访问")
            return real_replace(src, dst)

        checkpoint = self._checkpoint("占用释放后应写入成功")
        with mock.patch("memory.chat_memory.os.replace", side_effect=flaky_replace):
            manager._write_pending_checkpoint(checkpoint)

        pending = manager._pending_checkpoint_path
        self.assertTrue(pending.exists())
        self.assertEqual(json.loads(pending.read_text(encoding="utf-8")), checkpoint)
        self.assertFalse(pending.with_name(pending.name + ".tmp").exists())
        self.assertEqual(calls["n"], 4)

    def test_checkpoint_gives_up_keeps_old_and_does_not_raise(self):
        manager = chat_memory.ChatMemoryManager("sess_stale")
        pending = manager._pending_checkpoint_path
        old = self._checkpoint("旧检查点")
        pending.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")

        def always_deny(src, dst):
            raise PermissionError(5, "拒绝访问")

        with mock.patch("memory.chat_memory.os.replace", side_effect=always_deny):
            # 不抛出即为通过：检查点失败不能打断当前轮次
            manager._write_pending_checkpoint(self._checkpoint("新检查点"))

        self.assertEqual(json.loads(pending.read_text(encoding="utf-8")), old)

    def test_main_history_write_raises_after_retries_exhausted(self):
        manager = chat_memory.ChatMemoryManager("sess_raise")
        meta, entries = chat_memory._load_meta_and_entries(manager._file_path, "sess_raise")
        meta["title"] = "重命名测试"

        def always_deny(src, dst):
            raise PermissionError(5, "拒绝访问")

        with mock.patch("memory.chat_memory.os.replace", side_effect=always_deny):
            with self.assertRaises(PermissionError):
                chat_memory._write_meta_and_entries(manager._file_path, meta, entries)


if __name__ == "__main__":
    unittest.main()
