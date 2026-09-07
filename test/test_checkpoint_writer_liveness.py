"""检查点写者存活校验测试（防止进行中轮次被误恢复成 interrupted 历史）：

背景 bug：生成任务运行在每会话独立 worker 进程，主进程因读接口新建
管理器实例时，旧逻辑会无条件把 .pending 检查点恢复成 interrupted 轮次
并清掉检查点——worker 继续收尾同一轮次后落盘 done，同一问题重复记录两次。

- 检查点写者进程仍存活 → 不恢复（保留检查点）；
- 检查点写者进程已死亡 → 正常恢复为 interrupted；
- 恢复幂等：同 started_at + 首个用户事件的轮次已在历史时不重复追加；
- 检查点快照附带 writer_pid 字段。
运行：python -m unittest test.test_checkpoint_writer_liveness -v
"""
import asyncio
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from memory import chat_memory


class _LivenessTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._orig_root = chat_memory.HISTORY_ROOT
        chat_memory.HISTORY_ROOT = Path(self._tmp)

    def tearDown(self):
        chat_memory.HISTORY_ROOT = self._orig_root
        shutil.rmtree(self._tmp, ignore_errors=True)


class PidAliveTests(_LivenessTestBase):
    def test_current_process_alive(self):
        self.assertTrue(chat_memory._pid_alive(os.getpid()))

    def test_invalid_pid_not_alive(self):
        self.assertFalse(chat_memory._pid_alive(None))
        self.assertFalse(chat_memory._pid_alive("123"))
        self.assertFalse(chat_memory._pid_alive(0))
        self.assertFalse(chat_memory._pid_alive(-1))


class CheckpointWriterLivenessTests(_LivenessTestBase):
    SESSION = "sess_writer_alive"

    def _checkpoint(self, writer_pid):
        return {
            "event": "chat_round",
            "question": "进行中的一轮",
            "started_at": "2026-09-07 10:00:00",
            "events": [
                {"timestamp": "2026-09-07 10:00:00", "role": "user", "content": "进行中的一轮"},
                {
                    "timestamp": "2026-09-07 10:00:01", "role": "assistant",
                    "tool_calls": [{"id": "call_1", "type": "function",
                                    "function": {"name": "run_command", "arguments": "{}"}}],
                },
            ],
            "completion_count": 0,
            "usage_total": {},
            "status": "running",
            "writer_pid": writer_pid,
        }

    def _new_manager(self):
        return chat_memory.ChatMemoryManager(self.SESSION)

    def _rounds(self, manager):
        meta, entries = chat_memory._load_meta_and_entries(manager._file_path, self.SESSION)
        return meta, [e for e in entries if e.get("event") == "chat_round"]

    def test_checkpoint_snapshot_includes_writer_pid(self):
        manager = self._new_manager()
        asyncio.run(manager.add_chat_history({"role": "user", "content": "你好"}))
        checkpoint = json.loads(manager._pending_checkpoint_path.read_text(encoding="utf-8"))
        self.assertEqual(checkpoint.get("writer_pid"), os.getpid())

    def test_living_writer_checkpoint_not_recovered(self):
        manager = self._new_manager()
        pending = manager._pending_checkpoint_path
        # 模拟活跃 worker 写入的检查点：写者 = 当前进程（存活）
        pending.write_text(json.dumps(self._checkpoint(os.getpid()), ensure_ascii=False), encoding="utf-8")
        # 主进程"读接口"新建管理器触发恢复逻辑
        self._new_manager()
        meta, rounds = self._rounds(self._new_manager())
        # 未产生 interrupted 轮次，检查点原样保留
        self.assertEqual(len(rounds), 0)
        self.assertTrue(pending.exists())

    def test_dead_writer_checkpoint_recovered(self):
        manager = self._new_manager()
        pending = manager._pending_checkpoint_path
        # 写者 PID 指向一个不可能存在的进程
        pending.write_text(json.dumps(self._checkpoint(-2147483000), ensure_ascii=False), encoding="utf-8")
        self._new_manager()
        meta, rounds = self._rounds(self._new_manager())
        self.assertEqual(len(rounds), 1)
        self.assertEqual(rounds[0]["status"], "interrupted")
        self.assertFalse(pending.exists())

    def test_recovery_is_idempotent_for_recorded_round(self):
        manager = self._new_manager()
        pending = manager._pending_checkpoint_path
        checkpoint = self._checkpoint(-2147483000)
        pending.write_text(json.dumps(checkpoint, ensure_ascii=False), encoding="utf-8")
        self._new_manager()  # 第一次恢复 → interrupted 落盘
        # 构造"恢复后轮次已正常收尾"的历史：同 started_at、同首个用户事件
        meta, entries = chat_memory._load_meta_and_entries(manager._file_path, self.SESSION)
        for entry in entries:
            if entry.get("event") == "chat_round":
                entry["status"] = "done"
                entry["ended_at"] = "2026-09-07 10:00:10"
                entry["events"].append({"timestamp": "2026-09-07 10:00:10", "role": "assistant", "done": "[DONE]"})
        chat_memory._write_meta_and_entries(manager._file_path, meta, entries)
        # 第二次恢复（检查点残留场景）不得重复追加轮次
        self._new_manager()
        meta2, rounds2 = self._rounds(self._new_manager())
        self.assertEqual(len(rounds2), 1)
        self.assertEqual(len(meta2["user_questions"]), 1)


if __name__ == "__main__":
    unittest.main()
