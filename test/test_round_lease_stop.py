"""写令牌（round lease）：停止后旧任务写不进历史，且停止接口不等资源回收。

核心断言（P0 + P1）：
1. lease 撤销后写入被 `RoundAbandoned` 拒绝；
2. 跨进程侧车能把撤销状态传给 worker（对象不共享）；
3. 侧车 epoch 与当前轮不一致时（新轮已接管）同样拒绝旧轮写入；
4. `stop_chat_task` 软停止不等待 worker（不等 `_wait_worker_idle`）；
5. 硬停止在后台线程执行，失败不影响软停止结果；
6. 停止后立即重发：新轮 lease 的 activate 会覆盖旧撤销标记，新轮可写。
"""
import asyncio
import inspect
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from factory.agent_runtime.round_lease import (
    RoundAbandoned,
    check_lease_file,
    clear_lease_file,
    lease_path_for,
    new_lease,
    revoke_lease_file,
)


class RoundLeaseUnitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.chat_file = Path(self.tmp.name) / "s_chat.jsonl"
        self.lease_path = lease_path_for(self.chat_file)

    def tearDown(self):
        self.tmp.cleanup()

    def test_revoke_blocks_writes(self):
        lease = new_lease("s", self.lease_path)
        lease.activate()
        lease.assert_writable()          # 未撤销：不抛
        self.assertTrue(lease.revoke("user_stop"))
        with self.assertRaises(RoundAbandoned):
            lease.assert_writable()

    def test_revoke_is_idempotent(self):
        lease = new_lease("s", self.lease_path)
        lease.activate()
        self.assertTrue(lease.revoke())
        self.assertFalse(lease.revoke(), "重复撤销返回 False")

    def test_cross_process_sidecar_carries_revocation(self):
        """worker 侧（无共享对象）只能读侧车判断。"""
        lease = new_lease("s", self.lease_path)
        lease.activate()
        self.assertFalse(check_lease_file(self.lease_path, lease.epoch),
                         "刚 activate 时不应视为撤销")
        # 模拟主进程：只有会话 ID，不知道 worker 的 lease 对象
        self.assertTrue(revoke_lease_file(self.lease_path, "user_stop"))
        # worker 侧写前检查
        self.assertTrue(check_lease_file(self.lease_path, lease.epoch),
                        "撤销后 worker 必须能看到")

    def test_epoch_mismatch_blocks_old_writer(self):
        """新轮接管后，旧轮迟到写入同样必须被拒绝。"""
        old = new_lease("s", self.lease_path)
        old.activate()
        new = new_lease("s", self.lease_path)
        new.activate()                                    # 新轮覆盖侧车 epoch
        self.assertTrue(check_lease_file(self.lease_path, old.epoch),
                        "旧 epoch 与新侧车不符 → 禁止写入")
        self.assertFalse(check_lease_file(self.lease_path, new.epoch),
                         "新 epoch 可写")

    def test_new_round_after_stop_can_write(self):
        """停止后立即重发：新轮 activate 覆盖撤销标记。"""
        old = new_lease("s", self.lease_path)
        old.activate()
        revoke_lease_file(self.lease_path, "user_stop")
        self.assertTrue(check_lease_file(self.lease_path, old.epoch))

        fresh = new_lease("s", self.lease_path)
        fresh.activate()
        fresh.assert_writable()
        self.assertFalse(check_lease_file(self.lease_path, fresh.epoch),
                         "新轮必须可写")
        # 旧轮仍然被拒（epoch 不匹配）
        self.assertTrue(check_lease_file(self.lease_path, old.epoch))

    def test_revoke_without_epoch_is_noop(self):
        self.assertFalse(revoke_lease_file(self.lease_path), "无侧车时无可撤销")
        self.assertFalse(check_lease_file(self.lease_path, "whatever"))

    def test_missing_epoch_check_is_permissive(self):
        """未启用 lease 的旧路径（epoch=None）不拦写入。"""
        self.assertFalse(check_lease_file(self.lease_path, None))


class StopSoftHardSeparationTests(unittest.TestCase):
    def test_stop_chat_task_signature_has_soft(self):
        from factory import chat_factory
        sig = inspect.signature(chat_factory.stop_chat_task)
        self.assertIn("soft", sig.parameters)
        self.assertTrue(sig.parameters["soft"].default, "默认应为软停止（立即返回）")

    def test_soft_stop_does_not_wait_worker(self):
        """核心：软停止路径不得包含 _wait_worker_idle（那是硬停止的事）。"""
        from factory import chat_factory
        src = inspect.getsource(chat_factory.stop_chat_task)
        # 去掉注释再判断，避免文档里提到而误判
        code = "\n".join(line for line in src.splitlines()
                         if not line.strip().startswith("#"))
        self.assertNotIn("_wait_worker_idle", code,
                         "软停止不能在请求线程里等 worker 退出")

    def test_soft_stop_schedules_hard_stop(self):
        from factory import chat_factory
        src = inspect.getsource(chat_factory.stop_chat_task)
        self.assertIn("_schedule_hard_stop", src)

    def test_hard_stop_is_exception_tolerant(self):
        from factory import chat_factory
        src = inspect.getsource(chat_factory._hard_stop)
        self.assertIn("except Exception", src, "硬停止失败不能向上抛")

    def test_router_exposes_phase(self):
        from pathlib import Path as P
        root = P(__file__).resolve().parents[1]
        src = (root / "routers" / "chat_router.py").read_text(encoding="utf-8")
        start = src.index("async def stop_chat(")
        body = src[start:src.index("class StopSubAgentRequest", start)]
        self.assertIn("phase", body)
        self.assertIn("soft", body)


class WriteGuardLeaseWiringTests(unittest.TestCase):
    def test_generation_paths_check_lease(self):
        from memory import chat_memory
        hist = inspect.getsource(chat_memory.ChatMemoryManager.add_chat_history)
        fin = inspect.getsource(chat_memory.ChatMemoryManager._finish_current_round)
        self.assertIn("_assert_lease_writable", hist,
                      "add_chat_history 是主要写入口，必须校验写令牌")
        self.assertIn("_assert_lease_writable", fin,
                      "_finish_current_round 也要校验（可用 check_lease 关闭）")

    def test_management_writes_not_blocked(self):
        """删除/重命名等管理操作不能被停止流程拦住。"""
        from memory import chat_memory
        guard = inspect.getsource(chat_memory.ChatMemoryManager._write_guard)
        # 只看代码行，忽略注释/文档（里面会提到 _assert_lease_writable 说明原因）
        code_lines = []
        in_doc = False
        for line in guard.splitlines():
            stripped = line.strip()
            if stripped.startswith('"""') or stripped.endswith('"""'):
                in_doc = not in_doc if stripped.count('"""') == 1 else in_doc
                continue
            if in_doc or stripped.startswith("#"):
                continue
            code_lines.append(line)
        code = "\n".join(code_lines)
        self.assertNotIn("_assert_lease_writable", code,
                         "通用写守卫不能做 lease 校验，否则停止后删不掉会话")
        delete = inspect.getsource(chat_memory.ChatMemoryManager.delete_rounds)
        self.assertNotIn("_assert_lease_writable", delete)

    def test_round_abandoned_is_quietly_swallowed(self):
        from factory import chat_factory
        src = inspect.getsource(chat_factory)
        self.assertIn("except RoundAbandoned", src,
                      "旧任务迟到收尾必须安静退出，不能推 error 帧给用户")


class DispatchStampTests(unittest.TestCase):
    """派发戳 + 收养：覆盖 pre-run 窗口（worker 启动/工具发现/前置压缩）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.chat = Path(self.tmp.name) / "s.jsonl"
        self.path = lease_path_for(self.chat)

    def tearDown(self):
        self.tmp.cleanup()

    def test_dispatch_then_adopt_keeps_epoch(self):
        from factory.agent_runtime.round_lease import adopt_dispatch_lease, stamp_dispatch_lease
        stamped = stamp_dispatch_lease(self.path, "s")
        adopted = adopt_dispatch_lease(self.path, "s")
        self.assertIsNotNone(adopted)
        self.assertEqual(adopted.epoch, stamped.epoch, "收养必须保持 epoch 一致")
        self.assertFalse(adopted.revoked())

    def test_stop_inside_prerun_window_aborts_round(self):
        """派发后、开跑前点停止：收养到的 lease 必须是已撤销态。"""
        from factory.agent_runtime.round_lease import adopt_dispatch_lease, stamp_dispatch_lease
        stamp_dispatch_lease(self.path, "s")
        self.assertTrue(revoke_lease_file(self.path, "user_stop"))
        adopted = adopt_dispatch_lease(self.path, "s")
        self.assertIsNotNone(adopted)
        self.assertTrue(adopted.revoked(), "停止在 pre-run 窗口内必须先拦住")
        with self.assertRaises(RoundAbandoned):
            adopted.assert_writable()

    def test_adopt_only_consumes_dispatch_stamp(self):
        """上一轮遗留的 round 戳不能被当成本轮派发戳。"""
        from factory.agent_runtime.round_lease import adopt_dispatch_lease, stamp_round_lease
        stamp_round_lease(self.path, "s")
        self.assertIsNone(adopt_dispatch_lease(self.path, "s"),
                          "非 dispatch 戳不可收养")

    def test_previous_revoked_round_does_not_block_next_round(self):
        """关键回归：上轮停止后的撤销标记不能把下一轮直接判死。"""
        from factory.agent_runtime.round_lease import (
            adopt_dispatch_lease, stamp_dispatch_lease, stamp_round_lease,
        )
        stamp_round_lease(self.path, "s")
        revoke_lease_file(self.path, "user_stop")   # 上一轮被停止
        self.assertIsNone(adopt_dispatch_lease(self.path, "s"))
        fresh = stamp_round_lease(self.path, "s")   # 新一轮自建
        self.assertFalse(fresh.revoked())
        fresh.assert_writable()
        self.assertFalse(check_lease_file(self.path, fresh.epoch))

    def test_adopt_is_idempotent_only_once(self):
        from factory.agent_runtime.round_lease import adopt_dispatch_lease, stamp_dispatch_lease
        stamp_dispatch_lease(self.path, "s")
        self.assertIsNotNone(adopt_dispatch_lease(self.path, "s"))
        self.assertIsNone(adopt_dispatch_lease(self.path, "s"),
                          "派发戳只能被消费一次")


if __name__ == "__main__":
    unittest.main()
