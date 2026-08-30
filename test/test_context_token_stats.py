import tempfile
import unittest
from pathlib import Path

import memory.chat_memory as chat_memory
from config import ChatLLMRequest
from factory.agent_runtime import context_compaction as compaction
from memory.chat_memory import ChatMemoryManager


class ContextTokenStatsTests(unittest.IsolatedAsyncioTestCase):
    """get_context_token_stats 的统计口径与 get_context_messages 保持一致。"""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._orig_root = chat_memory.HISTORY_ROOT
        chat_memory.HISTORY_ROOT = Path(self._tmp)
        self.session_id = "stats_test"
        self.manager = ChatMemoryManager(self.session_id)

    def tearDown(self):
        chat_memory.HISTORY_ROOT = self._orig_root

    async def _add_round(self, question: str, tool_result: str = "ok"):
        await self.manager.add_chat_history({"role": "user", "content": question})
        await self.manager.add_chat_history({
            "role": "assistant",
            "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {"name": "read_data", "arguments": "{}"},
            }],
        })
        await self.manager.add_chat_history({
            "role": "tool",
            "tool_call_id": "call_1",
            "tool_name": "read_data",
            "arguments": "{}",
            "result": tool_result,
        })
        await self.manager.add_chat_history({
            "role": "assistant",
            "content": "完成",
            "done": "[DONE]",
        })

    async def test_empty_session_stats(self):
        stats = await self.manager.get_context_token_stats()
        self.assertEqual(stats["rounds"]["total"], 0)
        self.assertEqual(stats["rounds"]["summarized"], 0)
        self.assertEqual(stats["rounds"]["retained"], 0)
        self.assertEqual(stats["messages_tokens"], 0)
        self.assertFalse(stats["has_context_summary"])
        self.assertGreater(stats["context_token_limit"], 0)
        self.assertEqual(stats["estimated_budget_ratio"], 0.0)

    async def test_stats_includes_pending_round_during_stream(self):
        await self.manager.add_chat_history({"role": "user", "content": "进行中的问题"})
        stats = await self.manager.get_context_token_stats()
        self.assertEqual(stats["rounds"]["total"], 1)
        self.assertEqual(stats["rounds"]["retained"], 1)
        self.assertEqual(len(stats["round_tokens"]), 1)
        self.assertEqual(stats["round_tokens"][0]["status"], "running")
        self.assertGreater(stats["round_tokens"][0]["tokens"], 0)

        await self.manager.add_chat_history({"role": "assistant", "content": "正在生成"})
        updated = await self.manager.get_context_token_stats()
        self.assertGreater(
            updated["round_tokens"][0]["tokens"],
            stats["round_tokens"][0]["tokens"],
        )

    async def test_stats_counts_retained_rounds_and_estimates_tokens(self):
        await self._add_round("第一问", tool_result="结果A")
        await self._add_round("第二问", tool_result="结果B" * 50)
        await self._add_round("第三问", tool_result="结果C")
        stats = await self.manager.get_context_token_stats()
        self.assertEqual(stats["rounds"]["total"], 3)
        self.assertEqual(stats["rounds"]["summarized"], 0)
        self.assertEqual(stats["rounds"]["retained"], 3)
        self.assertEqual(stats["rounds"]["max_rounds"], 20)
        self.assertEqual(len(stats["round_tokens"]), 3)
        self.assertGreater(stats["messages_tokens"], 0)
        self.assertEqual(
            stats["request_context_tokens"],
            stats["messages_tokens"] + stats["tool_definition_tokens"],
        )
        self.assertGreater(stats["estimated_budget_ratio"], 0.0)

    async def test_stats_respects_max_rounds(self):
        for index in range(3):
            await self._add_round(f"第{index + 1}问")
        stats = await self.manager.get_context_token_stats(max_rounds=1)
        self.assertEqual(stats["rounds"]["total"], 3)
        self.assertEqual(stats["rounds"]["retained"], 1)
        self.assertEqual(len(stats["round_tokens"]), 1)
        self.assertEqual(stats["round_tokens"][0]["question"], "第3问")

    async def test_stats_accounts_for_context_summary(self):
        for index in range(3):
            await self._add_round(f"第{index + 1}问")
        await self.manager.update_context_summary({
            "summary": "早期两轮已被压缩",
            "key_facts": [],
            "open_items": [],
            "tool_state": [],
            "source_round_count": 2,
        })
        stats = await self.manager.get_context_token_stats()
        self.assertEqual(stats["rounds"]["summarized"], 2)
        # 窗口 20：第 3 轮未压缩，完整对话占窗口；前 2 轮问题保真
        self.assertEqual(stats["rounds"]["retained"], 1)
        self.assertEqual(stats["history_context_mode"], "summary_only")
        self.assertEqual(stats["raw_history_rounds_sent"], 1)
        self.assertEqual(len(stats["round_tokens"]), 1)
        self.assertEqual(stats["round_tokens"][0]["question"], "第3问")
        # 保真索引只含已压缩的第 1、2 轮（第 3 轮不重复）
        self.assertEqual(stats["recent_questions_count"], 2)
        self.assertTrue(stats["has_context_summary"])
        self.assertGreater(stats["summary_text_length"], 0)

    async def test_stats_includes_tool_definition_tokens(self):
        await self._add_round("查询一下")
        without_tools = await self.manager.get_context_token_stats()
        with_tools = await self.manager.get_context_token_stats(
            tools=[{
                "type": "function",
                "function": {
                    "name": "read_data",
                    "description": "读取数据",
                    "parameters": {"type": "object", "properties": {}},
                },
            }]
        )
        self.assertGreater(with_tools["tool_definition_tokens"], 0)
        self.assertEqual(with_tools["messages_tokens"], without_tools["messages_tokens"])
        self.assertEqual(
            with_tools["request_context_tokens"],
            with_tools["messages_tokens"] + with_tools["tool_definition_tokens"],
        )

    async def test_stats_accumulates_compress_usage_counts(self):
        await self.manager.add_history_compression_usage({
            "prompt_tokens": 100,
            "completion_tokens": 20,
        })
        stats = await self.manager.get_context_token_stats()
        self.assertEqual(stats["history_compress_count"], 1)
        self.assertEqual(stats["context_compress_count"], 1)

    async def test_round_compress_usage_counted_in_stats(self):
        await self.manager.add_chat_history({"role": "user", "content": "带压缩的一轮"})
        await self.manager.add_chat_history({
            "role": "assistant",
            "tool_calls": [{
                "id": "call_2",
                "type": "function",
                "function": {"name": "read_data", "arguments": "{}"},
            }],
        })
        await self.manager.add_chat_history({
            "role": "tool",
            "tool_call_id": "call_2",
            "tool_name": "read_data",
            "arguments": "{}",
            "result": "大结果",
        })
        await self.manager.update_current_round_compaction(
            "本轮摘要内容", 1, {"prompt_tokens": 50, "completion_tokens": 10}
        )
        await self.manager.add_chat_history({
            "role": "assistant",
            "content": "完成",
            "done": "[DONE]",
        })
        stats = await self.manager.get_context_token_stats()
        self.assertEqual(stats["round_tokens"][0]["compress_count"], 1)
        self.assertEqual(stats["context_compress_count"], 1)

    async def test_meta_usage_totals_idempotent_across_recomputes(self):
        """多次重算 meta 不得重复累计压缩 usage（活动检查点 + 历史累计）。"""
        await self.manager.add_chat_history({"role": "user", "content": "幂等性验证"})
        await self.manager.add_chat_history({
            "role": "assistant",
            "tool_calls": [{
                "id": "call_3",
                "type": "function",
                "function": {"name": "read_data", "arguments": "{}"},
            }],
        })
        await self.manager.add_chat_history({
            "role": "tool",
            "tool_call_id": "call_3",
            "tool_name": "read_data",
            "arguments": "{}",
            "result": "结果A",
        })
        await self.manager.update_current_round_compaction(
            "第一次摘要", 1, {"prompt_tokens": 100, "completion_tokens": 20}
        )
        await self.manager.update_current_round_compaction(
            "第二次摘要", 1, {"prompt_tokens": 150, "completion_tokens": 30}
        )
        await self.manager.add_history_compression_usage({
            "prompt_tokens": 200,
            "completion_tokens": 40,
        })
        first = await self.manager.get_session_meta()
        expected_usage = dict(first["usage"])
        expected_compress = dict(first["compress_usage"])
        for _ in range(3):
            again = await self.manager.get_session_meta()
            self.assertEqual(again["usage"], expected_usage)
            self.assertEqual(again["compress_usage"], expected_compress)
        self.assertEqual(expected_compress.get("compression_count"), 3)
        self.assertEqual(expected_usage.get("prompt_tokens"), 450)
        self.assertEqual(expected_usage.get("completion_tokens"), 90)

    async def test_meta_usage_totals_idempotent_after_round_finalize(self):
        """轮次收尾后检查点移除，usage 只从 chat_round 条目合并一次。"""
        await self.manager.add_chat_history({"role": "user", "content": "收尾幂等性"})
        await self.manager.add_chat_history({
            "role": "assistant",
            "tool_calls": [{
                "id": "call_4",
                "type": "function",
                "function": {"name": "read_data", "arguments": "{}"},
            }],
        })
        await self.manager.add_chat_history({
            "role": "tool",
            "tool_call_id": "call_4",
            "tool_name": "read_data",
            "arguments": "{}",
            "result": "结果B",
        })
        await self.manager.update_current_round_compaction(
            "活动期摘要", 1, {"prompt_tokens": 300, "completion_tokens": 60}
        )
        await self.manager.add_chat_history({
            "role": "assistant",
            "content": "完成",
            "done": "[DONE]",
        })
        finalized = await self.manager.get_session_meta()
        self.assertNotIn("_active_round_compaction", finalized)
        self.assertEqual(finalized["compress_usage"].get("compression_count"), 1)
        self.assertEqual(finalized["usage"].get("prompt_tokens"), 300)
        for _ in range(3):
            again = await self.manager.get_session_meta()
            self.assertEqual(again["usage"], finalized["usage"])
            self.assertEqual(again["compress_usage"], finalized["compress_usage"])


class RoundCompactionUsagePropagationTests(unittest.IsolatedAsyncioTestCase):
    """单轮压缩结果应把压缩模型 usage 传给调用方（事件/记忆透传的数据源）。"""

    def setUp(self):
        self._original_chat = compaction.ChatLLM.chat_completions
        self._original_resolver = compaction.resolve_context_compaction_model_config
        self._original_context_limit = compaction.resolve_model_max_input_tokens
        self.captured_requests = []

        def fake_chat_completions(*, request=None, stream=False, model_config=None, **_kwargs):
            self.captured_requests.append({"request": request, "stream": stream})
            return {
                "content": "任务仍需继续；工具已返回关键结果。",
                "usage": {
                    "prompt_tokens": 1234,
                    "completion_tokens": 56,
                    "total_tokens": 1290,
                },
            }

        compaction.ChatLLM.chat_completions = staticmethod(fake_chat_completions)
        compaction.resolve_context_compaction_model_config = lambda _settings: {
            "selected_provider_name": "Test Provider",
            "selected_model_name": "Test Model",
            "selected_model_id": "test-model",
            "apiType": "chat-completions",
            "maxInputTokens": 20_000,
            "maxOutputTokens": 512,
        }

    def tearDown(self):
        compaction.ChatLLM.chat_completions = staticmethod(self._original_chat)
        compaction.resolve_context_compaction_model_config = self._original_resolver
        compaction.resolve_model_max_input_tokens = self._original_context_limit

    async def test_round_compaction_result_carries_usage(self):
        large_result = "关键输出" * 4000
        messages = [
            {"role": "system", "content": "系统提示"},
            {"role": "user", "content": "请处理这个任务"},
            {
                "role": "assistant",
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read_data", "arguments": "{}"},
                }],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": large_result,
                "_tool_name": "read_data",
            },
        ]
        request = ChatLLMRequest(
            session_id="round_usage_propagation_test",
            messages=[{"role": "user", "content": "请处理这个任务"}],
            max_tokens=512,
        )
        request.tools = []

        result = await compaction.compact_active_round_context_if_needed(
            messages,
            request,
            settings=compaction.ContextCompactionSettings(
                keep_rounds=1,
                trigger_ratio=0.5,
                summary_budget_ratio=0.2,
                oversized_reject_factor=1.5,
                max_oversized_rejections=3,
            ),
        )

        self.assertTrue(result.triggered)
        self.assertEqual(result.usage, {
            "prompt_tokens": 1234,
            "completion_tokens": 56,
            "total_tokens": 1290,
        })

    async def test_round_compaction_usage_is_none_when_missing(self):
        def fake_no_usage(*, request=None, stream=False, model_config=None, **_kwargs):
            return {"content": "任务仍需继续；工具已返回关键结果。"}

        compaction.ChatLLM.chat_completions = staticmethod(fake_no_usage)
        messages = [
            {"role": "system", "content": "系统提示"},
            {"role": "user", "content": "请处理这个任务"},
            {
                "role": "assistant",
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read_data", "arguments": "{}"},
                }],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": "关键输出" * 4000,
                "_tool_name": "read_data",
            },
        ]
        request = ChatLLMRequest(
            session_id="round_usage_propagation_test",
            messages=[{"role": "user", "content": "请处理这个任务"}],
            max_tokens=512,
        )
        request.tools = []

        result = await compaction.compact_active_round_context_if_needed(
            messages,
            request,
            settings=compaction.ContextCompactionSettings(
                keep_rounds=1,
                trigger_ratio=0.5,
                summary_budget_ratio=0.2,
                oversized_reject_factor=1.5,
                max_oversized_rejections=3,
            ),
        )

        self.assertTrue(result.triggered)
        self.assertIsNone(result.usage)


if __name__ == "__main__":
    unittest.main()
