# -*- coding: utf-8 -*-
"""worker 崩溃/生成任务逃逸异常的可见化测试。

此前行为：worker 内生成任务 fire-and-forget、进程崩溃只合成 task_done——
用户侧"任务终止且什么提示没有"。修复后必须推送 error 帧、落盘错误说明
并正常收尾。
"""
import asyncio
import json
import unittest

import factory.session_worker as session_worker


class _FakeAdapter:
    """_WorkerStreamAdapter 的最小替身：记录事件与收尾状态。"""

    def __init__(self):
        self.events = []
        self.done = False

    def emit_event(self, payload):
        self.events.append(payload)

    async def finish(self):
        self.done = True
        self.events.append({"type": "task_done", "synthetic": True})


class UncaughtGenerateErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_escaped_exception_emits_error_and_finish(self):
        adapter = _FakeAdapter()

        async def _boom():
            raise RuntimeError("工具执行导致 worker 异常")

        task = asyncio.create_task(_boom())
        with self.assertRaises(RuntimeError):
            await task
        session_worker._emit_uncaught_generate_error(task, adapter)
        self.assertEqual(
            adapter.events[0].get("error"),
            "生成任务异常终止（未捕获异常）：工具执行导致 worker 异常",
        )
        # task_done 收尾被调度执行
        await asyncio.sleep(0.01)
        self.assertTrue(adapter.done)
        self.assertIn(
            {"type": "task_done", "synthetic": True},
            adapter.events,
        )

    async def test_normal_and_cancelled_tasks_are_silent(self):
        adapter = _FakeAdapter()

        async def _ok():
            return None

        normal = asyncio.create_task(_ok())
        await normal
        session_worker._emit_uncaught_generate_error(normal, adapter)

        cancelled = asyncio.create_task(asyncio.sleep(10))
        cancelled.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancelled
        session_worker._emit_uncaught_generate_error(cancelled, adapter)
        self.assertEqual(adapter.events, [])

    async def test_duplicate_task_done_suppressed_when_already_done(self):
        adapter = _FakeAdapter()
        adapter.done = True

        async def _boom():
            raise RuntimeError("x")

        task = asyncio.create_task(_boom())
        with self.assertRaises(RuntimeError):
            await task
        session_worker._emit_uncaught_generate_error(task, adapter)
        # 已收尾（生成循环正常发出过 task_done）时只补 error 帧，不再重复收尾
        self.assertEqual(len(adapter.events), 1)


class WorkerCrashSynthesisTests(unittest.IsolatedAsyncioTestCase):
    """主进程 reader 感知 worker 退出/崩溃时的合成事件。"""

    async def test_crash_dispatches_error_frame_and_task_done(self):
        proxy = session_worker.SessionWorkerProxy("crash-synthesis-test")
        proxy.set_loop(asyncio.get_running_loop())
        seen = []
        proxy.add_handler(seen.append)
        proxy._generation_running = True  # 模拟生成中 worker 突然退出

        class _EofConn:
            def recv(self):
                raise EOFError

        proxy._read_events(_EofConn())

        types = [evt.get("type") for evt in seen]
        self.assertIn("task_done", types)
        sse_chunks = [evt for evt in seen if evt.get("type") == "sse"]
        self.assertTrue(sse_chunks, "崩溃时应合成 error SSE 帧")
        raw = str(sse_chunks[0]["chunk"])
        self.assertTrue(raw.startswith("data: ") and raw.endswith("\n\n"))
        payload = json.loads(raw[len("data: "):].strip())
        self.assertIn("error", payload)
        done_evt = next(evt for evt in seen if evt.get("type") == "task_done")
        self.assertEqual(done_evt.get("reason"), "worker_exit_or_crash")

    async def test_idle_exit_without_running_generation_is_silent(self):
        """非生成状态下 worker 正常退出：不合成 error 帧。"""
        proxy = session_worker.SessionWorkerProxy("crash-synthesis-idle-test")
        proxy.set_loop(asyncio.get_running_loop())
        seen = []
        proxy.add_handler(seen.append)
        proxy._generation_running = False

        class _EofConn:
            def recv(self):
                raise EOFError

        proxy._read_events(_EofConn())
        types = [evt.get("type") for evt in seen]
        self.assertNotIn("task_done", types)
        self.assertFalse([evt for evt in seen if evt.get("type") == "sse"])
        self.assertIn("worker_exit", types)


class WorkerEventDispatchTests(unittest.IsolatedAsyncioTestCase):
    """worker evt 队列 → 主进程 stream 属性分发（附接回放 marker 依赖）。

    round_started 帧在回放起点之前，附接消费端收不到；marker 的 round /
    question_parts 字段依赖 worker 侧 setter 经 evt 队列同步到主进程 stream。
    """

    def test_adapter_live_round_no_enqueues_normalized_event(self):
        import queue

        evt_queue = queue.Queue()
        adapter = session_worker._WorkerStreamAdapter("adapter-round-test", evt_queue)
        adapter.live_round_no = 3
        self.assertEqual(evt_queue.get_nowait(), {"type": "live_round_no", "round": 3})
        # 非法值（非数字 / <1）归一为 None，前端按旧后端路径回退
        adapter.live_round_no = "bad"
        self.assertEqual(evt_queue.get_nowait(), {"type": "live_round_no", "round": None})
        adapter.live_round_no = 0
        self.assertEqual(evt_queue.get_nowait(), {"type": "live_round_no", "round": None})

    def test_adapter_question_parts_enqueues_event(self):
        import queue

        evt_queue = queue.Queue()
        adapter = session_worker._WorkerStreamAdapter("adapter-parts-test", evt_queue)
        parts = [{"type": "text", "text": "hi"}]
        adapter.question_parts = parts
        self.assertEqual(evt_queue.get_nowait(), {"type": "question_parts", "parts": parts})
        # 非列表值归一为 None
        adapter.question_parts = "not-a-list"
        self.assertEqual(evt_queue.get_nowait(), {"type": "question_parts", "parts": None})

    def test_handler_dispatches_round_and_parts_to_stream(self):
        proxy = session_worker.SessionWorkerProxy("evt-dispatch-test")

        class _Stream:
            def __init__(self):
                self.question_text = ""
                self.question_parts = None
                self.live_round_no = None
                self.round_started = False

            def emit(self, chunk):
                pass

            def set_round_start(self):
                self.round_started = True

        stream = _Stream()
        proxy.bind_stream(stream)
        handler = session_worker._make_worker_event_handler(proxy)
        handler({"type": "live_round_no", "round": 7})
        handler({"type": "question_parts", "parts": [{"type": "text", "text": "看图"}]})
        handler({"type": "question_text", "text": "看图"})
        handler({"type": "round_start"})
        self.assertEqual(stream.live_round_no, 7)
        self.assertEqual(stream.question_parts, [{"type": "text", "text": "看图"}])
        self.assertEqual(stream.question_text, "看图")
        self.assertTrue(stream.round_started)


if __name__ == "__main__":
    unittest.main()
