import asyncio
import json
import unittest
from unittest.mock import patch

from fastapi import HTTPException

import routers.chat_router as chat_router
from factory.agent_runtime.context_compaction import ContextCompactionSettings
from routers.chat_router import get_chat_work_dir_config


def _settings():
    return ContextCompactionSettings(

        trigger_ratio=0.8,
        summary_budget_ratio=0.2,
        oversized_reject_factor=1.5,
        max_oversized_rejections=3,
    )


class ChatRouterTests(unittest.TestCase):
    def test_work_dir_status_endpoint_is_read_only_and_uses_project_env(self):
        response = asyncio.run(get_chat_work_dir_config())
        self.assertEqual(response.status_code, 200)
        payload = response.body.decode("utf-8")
        self.assertIn('"read_only":true', payload)
        self.assertIn('"source":"project .env"', payload)
        self.assertIn('"env_name":"DEFAULT_CHAT_WORK_DIR"', payload)
        self.assertIn('"env_value":', payload)
        self.assertIn('"env_file":', payload)

    def test_manual_compaction_enforces_budget_after_confirmation(self):
        """手动确认后不应再受 keep_rounds 自动保护限制。"""
        captured = []

        class ManagerStub:
            async def get_file_text(self):
                return ""

            async def get_session_meta(self):
                return {"usage": {}}

            async def get_context_token_stats(self, **_kwargs):
                return {"request_context_tokens": 5000, "messages_tokens": 5000}

        manager = ManagerStub()

        async def get_manager(_session_id):
            return manager

        async def fake_compact(_manager, _request, **kwargs):
            self.assertEqual(_request.responses_mode, "stateless")
            captured.append(kwargs)
            return 1

        async def run():
            with patch.object(chat_router, "get_chat_memory_manager", get_manager), \
                    patch("env_manager.get_role_parameter", return_value={"responses_mode": "stateless"}), \
                    patch.object(chat_router, "load_context_compaction_settings", return_value=_settings()), \
                    patch.object(chat_router, "resolve_summary_total_budget", return_value=1234), \
                    patch.object(chat_router, "compact_session_history_if_needed", fake_compact):
                response = await chat_router.compact_chat_context_manual(
                    session_id="manual_router_test",
                    stream=False,
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(json.loads(response.body)["compressed_rounds"], 1)

                stream_response = await chat_router.compact_chat_context_manual(
                    session_id="manual_router_test",
                    stream=True,
                )
                async for _chunk in stream_response.body_iterator:
                    pass

        asyncio.run(run())
        self.assertEqual(len(captured), 2)
        for kwargs in captured:
            self.assertEqual(kwargs["budget_tokens"], 1234)
            self.assertTrue(kwargs["enforce"])
            self.assertTrue(kwargs["force_all"])

    def test_manual_compaction_rejects_context_below_summary_budget(self):
        """上下文估算低于摘要预算时拒绝手动压缩并提示；force 可跳过守卫。"""
        captured = []

        class ManagerStub:
            async def get_context_token_stats(self, **_kwargs):
                return {"request_context_tokens": 100, "messages_tokens": 100}

            async def get_file_text(self):
                return ""

            async def get_session_meta(self):
                return {"usage": {}}

        manager = ManagerStub()

        async def get_manager(_session_id):
            return manager

        async def fake_compact(_manager, _request, **kwargs):
            captured.append(kwargs)
            return 1

        async def run():
            with patch.object(chat_router, "get_chat_memory_manager", get_manager), \
                    patch.object(chat_router, "load_context_compaction_settings", return_value=_settings()), \
                    patch.object(chat_router, "resolve_summary_total_budget", return_value=1234), \
                    patch.object(chat_router, "compact_session_history_if_needed", fake_compact):
                # stream=false：返回 400 与明确提示，且不发起压缩
                with self.assertRaises(HTTPException) as ctx:
                    await chat_router.compact_chat_context_manual(
                        session_id="manual_router_test",
                        stream=False,
                    )
                self.assertEqual(ctx.exception.status_code, 400)
                self.assertIn("低于摘要预算", str(ctx.exception.detail))
                self.assertIn("1234", str(ctx.exception.detail))
                self.assertEqual(captured, [])

                # stream=true：以 compaction_manual_result 结果帧带回提示，不发起压缩
                stream_response = await chat_router.compact_chat_context_manual(
                    session_id="manual_router_test",
                    stream=True,
                )
                frames = []
                async for chunk in stream_response.body_iterator:
                    frames.append(chunk)
                result_frames = [f for f in frames if "compaction_manual_result" in f]
                self.assertEqual(len(result_frames), 1)
                self.assertIn("低于摘要预算", result_frames[0])
                self.assertNotIn("context_compaction", "".join(frames))
                self.assertEqual(captured, [])

                # force=true：跳过下限守卫，正常执行压缩
                response = await chat_router.compact_chat_context_manual(
                    session_id="manual_router_test",
                    stream=False,
                    force=True,
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(len(captured), 1)

        asyncio.run(run())
        chat_router._MANUAL_COMPACTION_JOBS.pop("manual_router_test", None)

    def test_manual_compaction_survives_consumer_disconnect_and_reattach(self):
        """SSE 断开不再取消后台压缩：任务继续执行，attach 可收看剩余进度。"""
        class ManagerStub:
            def __init__(self):
                self.events = []

            async def get_context_token_stats(self, **_kwargs):
                return {"request_context_tokens": 5000, "messages_tokens": 5000}

            async def get_file_text(self):
                return ""

            async def get_session_meta(self):
                return {"usage": {}}

            async def add_context_compaction_event(self, payload):
                self.events.append(payload)

        manager = ManagerStub()

        async def get_manager(_session_id):
            return manager

        started = asyncio.Event()
        release = asyncio.Event()

        async def fake_compact(_manager, _request, event_emitter=None, **_kwargs):
            started.set()
            await event_emitter({"event": "context_compaction", "scope": "session",
                                 "phase": "start", "context_summary": "preview"})
            await release.wait()
            await event_emitter({"event": "context_compaction", "scope": "session",
                                 "phase": "done", "summary_text": "final"})
            return 2

        async def run():
            session = "manual_reattach_test"
            chat_router._MANUAL_COMPACTION_JOBS.pop(session, None)
            try:
                with patch.object(chat_router, "get_chat_memory_manager", get_manager), \
                        patch.object(chat_router, "load_context_compaction_settings", return_value=_settings()), \
                        patch.object(chat_router, "resolve_summary_total_budget", return_value=1234), \
                        patch.object(chat_router, "compact_session_history_if_needed", fake_compact):
                    first = await chat_router.compact_chat_context_manual(session_id=session, stream=True)
                    iterator = first.body_iterator.__aiter__()
                    await started.wait()
                    # 读取到 start 帧后模拟刷新断开：关闭消费端，不取消后台任务
                    # （首帧可能是 task_usage 预算事件，跳过直到 start）
                    first_chunk = ""
                    for _ in range(5):
                        first_chunk = await iterator.__anext__()
                        if '"phase": "start"' in first_chunk or '"phase":"start"' in first_chunk:
                            break
                    self.assertIn("context_compaction", first_chunk)
                    if hasattr(iterator, "aclose"):
                        await iterator.aclose()
                    # 后台任务仍在运行（未被取消），状态接口可见
                    status = await chat_router.manual_compaction_status(session_id=session)
                    self.assertIn('"running":true', status.body.decode("utf-8"))
                    # 新建压缩应被拒绝（409），附接应成功
                    with self.assertRaises(HTTPException) as ctx:
                        await chat_router.compact_chat_context_manual(session_id=session, stream=True)
                    self.assertEqual(ctx.exception.status_code, 409)
                    second = await chat_router.compact_chat_context_manual(
                        session_id=session, stream=True, attach=True)
                    # 放行后台任务，附接端应收到 done + 结果帧
                    release.set()
                    frames = []
                    async for chunk in second.body_iterator:
                        frames.append(chunk)
                    joined = "".join(frames)
                    self.assertIn('"phase": "done"', joined)
                    self.assertIn("compaction_manual_result", joined)
                    self.assertIn('"compressed_rounds": 2', joined)
                    # 任务完成后状态恢复空闲
                    status = await chat_router.manual_compaction_status(session_id=session)
                    self.assertIn('"running":false', status.body.decode("utf-8"))
            finally:
                release.set()
                job = chat_router._MANUAL_COMPACTION_JOBS.pop(session, None)
                if job and job.get("task") and not job["task"].done():
                    job["task"].cancel()
                    try:
                        await job["task"]
                    except Exception:
                        pass

        asyncio.run(run())

    def test_manual_compaction_stop_cancels_running_task(self):
        """显式停止取消后台任务并补写 aborted；无任务时返回 stopped=false。"""
        class ManagerStub:
            def __init__(self):
                self.events = []

            async def get_context_token_stats(self, **_kwargs):
                return {"request_context_tokens": 5000, "messages_tokens": 5000}

            async def get_file_text(self):
                return ""

            async def get_session_meta(self):
                return {"usage": {}}

            async def add_context_compaction_event(self, payload):
                self.events.append(payload)

        manager = ManagerStub()

        async def get_manager(_session_id):
            return manager

        started = asyncio.Event()
        release = asyncio.Event()

        async def fake_compact(_manager, _request, event_emitter=None, **_kwargs):
            started.set()
            await event_emitter({"event": "context_compaction", "scope": "session",
                                 "phase": "start"})
            await release.wait()
            return 0

        async def run():
            session = "manual_stop_test"
            chat_router._MANUAL_COMPACTION_JOBS.pop(session, None)
            try:
                with patch.object(chat_router, "get_chat_memory_manager", get_manager), \
                        patch.object(chat_router, "load_context_compaction_settings", return_value=_settings()), \
                        patch.object(chat_router, "resolve_summary_total_budget", return_value=1234), \
                        patch.object(chat_router, "compact_session_history_if_needed", fake_compact):
                    # 无任务时停止返回 false
                    idle = await chat_router.stop_manual_compaction(session_id=session)
                    self.assertIn('"stopped":false', idle.body.decode("utf-8"))
                    # 启动任务后停止
                    response = await chat_router.compact_chat_context_manual(session_id=session, stream=True)
                    iterator = response.body_iterator.__aiter__()
                    await started.wait()
                    await iterator.__anext__()
                    stopped = await chat_router.stop_manual_compaction(session_id=session)
                    self.assertIn('"stopped":true', stopped.body.decode("utf-8"))
                    release.set()
                    # 消费端应正常结束（sentinel + 结果帧 + DONE），不抛异常
                    frames = []
                    try:
                        async for chunk in response.body_iterator:
                            frames.append(chunk)
                    except Exception as exc:
                        self.fail(f"停止后消费端不应抛异常：{exc}")
                    self.assertTrue(any("compaction_manual_result" in f for f in frames))
                    self.assertTrue(any("[DONE]" in f for f in frames))
                    # 后台补写了 aborted 闭合 start
                    self.assertTrue(any(e.get("phase") == "aborted" for e in manager.events))
            finally:
                release.set()
                job = chat_router._MANUAL_COMPACTION_JOBS.pop(session, None)
                if job and job.get("task") and not job["task"].done():
                    job["task"].cancel()
                    try:
                        await job["task"]
                    except Exception:
                        pass

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
