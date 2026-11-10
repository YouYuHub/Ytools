"""方案 A：停止等待的代次校验——停止后立即重发不得误杀新任务。

回归背景：`_wait_worker_idle` 原先只看 is_generation_running（共享布尔量）。
用户点停止后 15 秒内重发消息，新的 start_generation 会把该标志重新置 True，
旧 stop 便误以为“旧任务仍在收尾”，等到超时 terminate()，把正在跑新消息的
worker 强杀 —— 表现就是新消息被吃、还报“worker 崩溃”。
"""
import asyncio
import unittest
from unittest.mock import MagicMock

from factory import chat_factory


def _proxy(seq_values, running=True):
    """seq_values: 每次调用 generation_seq() 依次返回的值（最后一次复用）。"""
    proxy = MagicMock()
    proxy.is_generation_running.return_value = running
    calls = {"n": 0}

    def seq():
        idx = min(calls["n"], len(seq_values) - 1)
        calls["n"] += 1
        return seq_values[idx]

    proxy.generation_seq.side_effect = seq
    return proxy


class StopWaitGenerationSeqTests(unittest.TestCase):
    def test_new_generation_takes_over_no_terminate(self):
        """代次变化 = 新任务接管：立即退让，不强杀。"""
        # 代次从 1 变 2（用户重发），running 一直 True
        proxy = _proxy([1, 1, 2, 2, 2])
        asyncio.run(chat_factory._wait_worker_idle(proxy, timeout=5.0))
        self.assertFalse(proxy.terminate.called, "新任务接管后不得强杀 worker")

    def test_same_seq_still_terminates_on_timeout(self):
        """代次不变（真正卡死）：超时仍强杀，保留崩溃兜底。"""
        proxy = _proxy([7])  # 代次始终 7
        asyncio.run(chat_factory._wait_worker_idle(proxy, timeout=0.3))
        self.assertTrue(proxy.terminate.called, "同一轮卡死仍需超时强杀")

    def test_returns_immediately_when_idle(self):
        """本来就不在跑：零等待返回，不调用 terminate。"""
        proxy = _proxy([3], running=False)
        asyncio.run(chat_factory._wait_worker_idle(proxy, timeout=5.0))
        self.assertFalse(proxy.terminate.called)

    def test_proxy_without_seq_keeps_legacy_behaviour(self):
        """兼容没有 generation_seq 的代理（旧 mock/其它实现）：沿用原语义。"""
        proxy = MagicMock(spec=["is_generation_running", "terminate"])
        proxy.is_generation_running.return_value = True
        asyncio.run(chat_factory._wait_worker_idle(proxy, timeout=0.3))
        self.assertTrue(proxy.terminate.called)


class SeqIncrementTests(unittest.TestCase):
    def test_start_generation_increments_seq(self):
        from factory.session_worker import SessionWorkerProxy

        proxy = SessionWorkerProxy("seq-test-session")
        proxy._ensure_process = lambda: None
        proxy._send = lambda payload: True
        before = proxy.generation_seq()
        proxy.start_generation({"session_id": "seq-test-session"})
        self.assertEqual(proxy.generation_seq(), before + 1)
        proxy.start_generation({"session_id": "seq-test-session"})
        self.assertEqual(proxy.generation_seq(), before + 2)


if __name__ == "__main__":
    unittest.main()
