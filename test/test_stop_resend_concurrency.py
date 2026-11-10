"""端到端：停止后立即重发，旧任务写不进新轮历史（不丢不乱）。

模拟真实场景（worker 模式下主进程与生成进程内存隔离）：
  t0  轮 A 开始，lease_A.activate() 写侧车
  t1  用户停止 → 主进程只有会话 ID，通过侧车 revoke
  t2  用户立即重发 → 轮 B 开始，lease_B.activate() 覆盖侧车
  t3  轮 A 的迟到工具结果尝试写历史 → 必须被拒（epoch 不匹配）
  t4  轮 B 正常写历史 → 必须成功

断言重点：写入被拒绝时不得产生任何文件内容变化（不丢不乱）。
"""
import json
import tempfile
import unittest
from pathlib import Path

from factory.agent_runtime.round_lease import (
    RoundAbandoned,
    check_lease_file,
    lease_path_for,
    new_lease,
    revoke_lease_file,
)


class WriteGuardLeaseEnforcementTests(unittest.TestCase):
    """用真实 ChatMemoryManager 的 _assert_lease_writable 验证拦截。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.chat_file = Path(self.tmp.name) / "sess_chat.jsonl"
        self.chat_file.write_text(
            json.dumps({"_meta": {"session_id": "sess"}}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _manager(self):
        from memory.chat_memory import ChatMemoryManager
        mgr = ChatMemoryManager.__new__(ChatMemoryManager)
        mgr.session_id = "sess"
        mgr._file_path = self.chat_file
        mgr._round_lease = None
        mgr._lease_epoch = None
        return mgr

    def test_revoked_lease_rejects_write(self):
        mgr = self._manager()
        lease = new_lease("sess", lease_path_for(self.chat_file))
        lease.activate()
        mgr._round_lease = lease
        mgr._assert_lease_writable()          # 未撤销：通过

        lease.revoke("user_stop")
        with self.assertRaises(RoundAbandoned):
            mgr._assert_lease_writable()

        before = self.chat_file.read_text(encoding="utf-8")
        with self.assertRaises(RoundAbandoned):
            mgr._assert_lease_writable()
        after = self.chat_file.read_text(encoding="utf-8")
        self.assertEqual(before, after, "被拒绝的写入不得改动历史文件")

    def test_cross_process_revocation_blocks_stale_writer(self):
        """worker 侧没有共享对象，只能靠侧车判定。"""
        mgr = self._manager()
        worker_lease = new_lease("sess", lease_path_for(self.chat_file))
        worker_lease.activate()
        # worker 侧不挂对象，只靠 epoch 读侧车（模拟真实 worker 写路径）
        mgr._lease_epoch = worker_lease.epoch
        mgr._assert_lease_writable()          # 侧车未撤销：通过

        revoke_lease_file(lease_path_for(self.chat_file), "user_stop")

        with self.assertRaises(RoundAbandoned):
            mgr._assert_lease_writable()

    def test_stop_then_resend_isolates_rounds(self):
        """核心场景：停止 → 立即重发 → 旧轮写入被拒、新轮可写。"""
        path = lease_path_for(self.chat_file)
        mgr = self._manager()

        old_lease = new_lease("sess", path)
        old_lease.activate()
        mgr._lease_epoch = old_lease.epoch

        # t1: 主进程停止（只有会话 ID）
        self.assertTrue(revoke_lease_file(path, "user_stop"))
        with self.assertRaises(RoundAbandoned):
            mgr._assert_lease_writable()

        # t2: 用户立即重发，新轮接管
        new = new_lease("sess", path)
        new.activate()
        mgr._lease_epoch = new.epoch
        mgr._assert_lease_writable()          # 新轮可写

        # t3: 旧轮迟到写入（例如 300s 后才返回的 MCP 工具）
        self.assertTrue(check_lease_file(path, old_lease.epoch),
                        "旧 epoch 必须被判定为不可写")
        self.assertFalse(check_lease_file(path, new.epoch),
                         "新 epoch 必须可写")

    def test_no_lease_keeps_legacy_behaviour(self):
        """未启用 lease 的路径（管理写入）不受影响。"""
        mgr = self._manager()
        mgr._assert_lease_writable()          # 不应抛


class FinalizeFallbackTests(unittest.TestCase):
    def test_soft_finalize_bypasses_lease_check(self):
        """软停止定稿必须能写盘（令牌刚被自己撤销）。"""
        import inspect
        from factory import chat_factory
        src = inspect.getsource(chat_factory._finalize_round_locally)
        self.assertIn("check_lease=False", src,
                      "定稿写入不能因令牌已撤销而被拦，否则轮次永远写不进历史")

    def test_soft_finalize_prefers_checkpoint_recovery(self):
        import inspect
        from factory import chat_factory
        src = inspect.getsource(chat_factory._finalize_round_locally)
        self.assertIn("recover_checkpoint=True", src,
                      "定稿需从 .pending 检查点恢复未落盘的当前轮")

    def test_soft_finalize_guarded_by_epoch(self):
        """硬停止是后台执行：新轮已接管时必须跳过，不能错杀新轮。"""
        import inspect
        from factory import chat_factory
        src = inspect.getsource(chat_factory._finalize_round_locally)
        self.assertIn("expected_epoch", src)
        self.assertIn("!= expected_epoch", src, "必须比对 epoch")

    def test_soft_stop_has_time_bound(self):
        """定稿要重写整份 JSONL，必须有时间上限否则响应被拖死。"""
        import inspect
        from factory import chat_factory
        src = inspect.getsource(chat_factory.stop_chat_task)
        self.assertIn("wait_for", src)
        self.assertIn("_finalize_round_locally", src)

    def test_hard_stop_retries_finalize_with_epoch(self):
        import inspect
        from factory import chat_factory
        src = inspect.getsource(chat_factory._hard_stop)
        self.assertIn("_finalize_round_locally", src)
        self.assertIn("stop_epoch", src)

    def test_status_reports_winding_down(self):
        """运行态查询需要区分“已停止但仍在收尾”与“正在运行”。"""
        from pathlib import Path as P
        root = P(__file__).resolve().parents[1]
        src = (root / "routers" / "chat_router.py").read_text(encoding="utf-8")
        start = src.index("async def chat_stream_status")
        body = src[start: start + 2200]
        self.assertIn("winding_down", body)
        self.assertIn("_round_lease_revoked", body)


if __name__ == "__main__":
    unittest.main()
