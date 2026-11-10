"""worker 代次隔离：软停止后新轮不等旧任务，「旧轮迟到事件」不能污染新轮。

这是“停止延迟 <5s”的关键安全前提：

旧设计用 `await _interrupt_worker_task(...)` 等旧任务完全退出才开新轮——
安全性靠时间（等它结束），所以慢。
新设计在写令牌已撤销时只 cancel 旧任务、不等它，立刻开新轮——安全性靠
代次 + 写令牌（它写不进历史、它的迟到事件被代次过滤）。
"""
import ast
import inspect
import unittest
from pathlib import Path


class WorkerGenerationIsolationTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1]
        self.source = (root / "factory" / "session_worker.py").read_text(encoding="utf-8")

    def _func_source(self, name: str) -> str:
        tree = ast.parse(self.source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                src = ast.get_source_segment(self.source, node)
                if src:
                    return src
        self.fail(f"未找到函数 {name}")

    def test_adapter_carries_generation(self):
        from factory.session_worker import _WorkerStreamAdapter
        import queue
        a = _WorkerStreamAdapter("s", queue.Queue())
        self.assertTrue(hasattr(a, "generation"), "adapter 必须携带代次")

    def test_task_done_carries_generation(self):
        from factory.session_worker import _WorkerStreamAdapter
        import asyncio
        import queue
        q = queue.Queue()
        a = _WorkerStreamAdapter("s", q, generation=7)
        asyncio.run(a.finish())
        evt = q.get(timeout=1)
        self.assertEqual(evt["type"], "task_done")
        self.assertEqual(evt["generation"], 7, "task_done 必须带代次，否则会误清新轮")

    def test_all_adapter_events_tagged(self):
        """所有事件都走 _put，自动补齐 generation。"""
        from factory.session_worker import _WorkerStreamAdapter
        import asyncio
        import queue
        q = queue.Queue()
        a = _WorkerStreamAdapter("s", q, generation=3)
        a.emit("data: {}\n\n")
        a.set_round_start()
        evt1 = q.get(timeout=1)
        evt2 = q.get(timeout=1)
        self.assertEqual(evt1.get("generation"), 3)
        self.assertEqual(evt2.get("generation"), 3)

    def test_dispatch_drops_stale_generation(self):
        """旧代次 task_done 不得清掉新轮运行标志，也不得喂给 handler。"""
        from factory.session_worker import SessionWorkerProxy
        proxy = SessionWorkerProxy.__new__(SessionWorkerProxy)
        import threading
        proxy.session_id = "s"
        proxy._state_lock = threading.Lock()
        proxy._generation_running = False
        proxy._latest_generation = 0
        proxy._alive = True
        proxy._handlers = []
        seen = []
        proxy._handlers.append(lambda e: seen.append(e))

        # 新轮开始（代次 2）
        proxy._dispatch({"type": "task_started", "generation": 2})
        self.assertTrue(proxy._generation_running)
        self.assertEqual(len(seen), 1)

        # 旧轮（代次 1）迟到收尾：不得清标志、不得喂 handler
        proxy._dispatch({"type": "task_done", "generation": 1})
        self.assertTrue(proxy._generation_running,
                        "旧代次 task_done 不能把新轮误判为已结束")
        self.assertEqual(len(seen), 1, "旧代次事件应被丢弃")

        # 新轮正常收尾（代次 2）
        proxy._dispatch({"type": "task_done", "generation": 2})
        self.assertFalse(proxy._generation_running)
        self.assertEqual(len(seen), 2)

    def test_generate_path_skips_wait_when_lease_revoked(self):
        """写令牌已撤销时，新 generate 不等旧任务完全退出。"""
        src = self._func_source("_worker_loop")
        self.assertIn("_lease_already_revoked", src,
                      "开新轮必须能识别“上一轮已被软停止”")
        self.assertIn("_reap_task_in_background", src,
                      "旧任务应交付后台监控，而不是阻塞命令循环")
        # 关键：撤销分支里不能出现 await 旧任务
        idx = src.index("_lease_already_revoked(session_id)")
        else_at = src.index("else:", idx)
        revoked_branch = src[idx:else_at]
        self.assertIn("_reap_task_in_background", revoked_branch)
        self.assertNotIn("await _interrupt_worker_task", revoked_branch,
                         "已撤销分支不能阻塞等旧任务")

    def test_unsignalled_interrupt_still_waits(self):
        """普通打断（未停止）仍等收尾，避免工具结果落盘交错。"""
        src = self._func_source("_worker_loop")
        self.assertIn("await _interrupt_worker_task(session_id, old_task, user_stop=False)", src)

    def test_reap_is_non_blocking(self):
        src = self._func_source("_reap_task_in_background")
        self.assertIn("create_task", src)
        self.assertNotIn("current_task = ", src)


if __name__ == "__main__":
    unittest.main()
