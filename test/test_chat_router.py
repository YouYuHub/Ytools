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
        keep_rounds=20,
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
        self.assertIn('"env_name":"CHAT_WORK_DIR"', payload)
        self.assertIn('"env_value":', payload)
        self.assertIn('"env_file":', payload)

    def test_manual_compaction_enforces_budget_after_confirmation(self):
        """手动确认后不应再受 keep_rounds 自动保护限制。"""
        captured = []

        class ManagerStub:
            async def get_context_token_stats(self, **_kwargs):
                return {"request_context_tokens": 5000, "messages_tokens": 5000}

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


if __name__ == "__main__":
    unittest.main()
