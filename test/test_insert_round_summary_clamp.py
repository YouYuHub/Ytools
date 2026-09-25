# -*- coding: utf-8 -*-
"""回答插入（insert_round）摘要钳制 + 摘要写盘失败可见化 测试。

覆盖用户报告的两个压缩问题：
1. "回答模型提问后立即大规模压缩"：旧实现整份失效累计摘要，下次任务全部
   轮次退回未压缩，auto 压缩从第 1 轮整段重压。修复为覆盖范围钳制到插入点，
   摘要保留、仅未覆盖轮次参与后续压缩。
2. "压缩失败后不从上次位置/上次摘要继续"：update_context_summary 写盘失败
   静默跳过会让摘要不生效、下次从原始历史重压。修复为抛出，调用方区分
   关键/非关键路径。
"""
import shutil
import tempfile
import unittest
from pathlib import Path

import memory.chat_memory as chat_memory
from config import ChatLLMRequest
from factory.agent_runtime import context_compaction as compaction
from memory.chat_history_format import (
    clamp_context_summary_to_rounds,
    normalize_context_summary,
    render_context_summary,
)
from memory.chat_memory import ChatMemoryManager


class ClampContextSummaryTests(unittest.TestCase):
    """clamp_context_summary_to_rounds 纯函数行为。"""

    def test_shrinks_cursor_block_and_question_numbers(self):
        summary = {
            "source_round_count": 5,
            "blocks": [{"summary": "旧摘要", "round_start": 1, "round_end": 5}],
            "recent_questions": ["问题一", "问题二", "问题三", "问题四", "问题五"],
            "recent_question_numbers": [1, 2, 3, 4, 5],
        }
        clamped = clamp_context_summary_to_rounds(summary, 3)
        self.assertEqual(clamped["source_round_count"], 3)
        self.assertEqual(clamped["blocks"][0]["round_end"], 3)
        self.assertEqual(clamped["blocks"][0]["round_start"], 1)
        self.assertEqual(clamped["recent_question_numbers"], [1, 2, 3])
        self.assertEqual(clamped["recent_questions"], ["问题一", "问题二", "问题三"])
        # 原对象不被就地修改
        self.assertEqual(summary["source_round_count"], 5)
        self.assertEqual(len(summary["recent_questions"]), 5)

    def test_returns_same_object_when_cursor_within_limit(self):
        summary = {"source_round_count": 3, "blocks": []}
        self.assertIs(clamp_context_summary_to_rounds(summary, 5), summary)
        self.assertIs(clamp_context_summary_to_rounds(summary, 3), summary)

    def test_drops_blocks_beyond_limit(self):
        summary = {
            "source_round_count": 6,
            "blocks": [
                {"summary": "第一段", "round_start": 1, "round_end": 2},
                {"summary": "第二段", "round_start": 4, "round_end": 6},
            ],
        }
        clamped = clamp_context_summary_to_rounds(summary, 3)
        self.assertEqual(len(clamped["blocks"]), 1)
        self.assertEqual(clamped["blocks"][0]["round_end"], 2)

    def test_clamps_top_level_legacy_range(self):
        summary = {
            "source_round_count": 5,
            "round_start": 1,
            "round_end": 5,
            "summary": "旧版结构",
        }
        clamped = clamp_context_summary_to_rounds(summary, 2)
        self.assertEqual(clamped["round_end"], 2)
        self.assertEqual(clamped["source_round_count"], 2)

    def test_invalid_input_passthrough(self):
        self.assertIsNone(clamp_context_summary_to_rounds(None, 3))
        self.assertEqual(clamp_context_summary_to_rounds("文本摘要", 3), "文本摘要")
        summary = {"source_round_count": 5}
        self.assertIs(clamp_context_summary_to_rounds(summary, None), summary)


class InsertRoundClampManagerTests(unittest.IsolatedAsyncioTestCase):
    """ChatMemoryManager.clamp_context_summary_for_insert_round 落盘行为。"""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._orig_root = chat_memory.HISTORY_ROOT
        chat_memory.HISTORY_ROOT = Path(self._tmp)
        self.session_id = "insert_clamp_test"
        self.manager = ChatMemoryManager(self.session_id)

    def tearDown(self):
        chat_memory.HISTORY_ROOT = self._orig_root
        shutil.rmtree(self._tmp, ignore_errors=True)

    async def _seed_rounds(self, count: int):
        for index in range(1, count + 1):
            await self.manager.add_chat_history({"role": "user", "content": f"问题{index}"})
            await self.manager.add_chat_history({"role": "assistant", "content": f"回答{index}"})
            await self.manager.add_chat_history({"role": "assistant", "done": "[DONE]"})

    async def _seed_summary(self, source_count: int):
        await self.manager.update_context_summary({
            "source_round_count": source_count,
            "blocks": [{
                "summary": "历史事实",
                "round_start": 1,
                "round_end": source_count,
            }],
            "recent_questions": [f"问题{i}" for i in range(1, source_count + 1)],
            "recent_question_numbers": list(range(1, source_count + 1)),
        })

    async def test_clamp_writes_adjusted_summary(self):
        await self._seed_rounds(5)
        await self._seed_summary(5)
        self.manager.clamp_context_summary_for_insert_round(3)
        summary = await self.manager.get_context_summary()
        self.assertEqual(summary["source_round_count"], 3)
        self.assertEqual(summary["blocks"][0]["round_end"], 3)
        self.assertEqual(summary["recent_question_numbers"], [1, 2, 3])
        # 摘要正文仍在（不是整份失效）
        rendered = render_context_summary(summary)
        self.assertIn("历史事实", rendered)

    async def test_clamp_within_limit_keeps_summary_untouched(self):
        await self._seed_rounds(5)
        await self._seed_summary(3)
        writes = []
        original = chat_memory._write_meta_and_entries

        def counting_write(file_path, meta, entries):
            writes.append(1)
            return original(file_path, meta, entries)

        chat_memory._write_meta_and_entries = counting_write
        try:
            self.manager.clamp_context_summary_for_insert_round(5)
        finally:
            chat_memory._write_meta_and_entries = original
        summary = await self.manager.get_context_summary()
        self.assertEqual(summary["source_round_count"], 3)
        # 游标已在插入点之内：无需写盘
        self.assertEqual(writes, [])

    async def test_clamp_write_failure_raises(self):
        await self._seed_rounds(5)
        await self._seed_summary(5)
        original = chat_memory._write_meta_and_entries

        def failing_write(file_path, meta, entries):
            raise OSError("文件被占用（模拟 WinError 5）")

        chat_memory._write_meta_and_entries = failing_write
        try:
            with self.assertRaises(RuntimeError):
                self.manager.clamp_context_summary_for_insert_round(3)
        finally:
            chat_memory._write_meta_and_entries = original

    async def test_update_context_summary_write_failure_raises(self):
        await self._seed_rounds(2)
        original = chat_memory._write_meta_and_entries

        def failing_write(file_path, meta, entries):
            raise OSError("文件被占用（模拟 WinError 5）")

        chat_memory._write_meta_and_entries = failing_write
        try:
            with self.assertRaises(RuntimeError):
                await self.manager.update_context_summary({
                    "source_round_count": 1,
                    "summary": "新摘要",
                })
        finally:
            chat_memory._write_meta_and_entries = original

    async def _finish_insert_round(self, answer_text: str):
        """模拟回答插入轮的收尾写入（user 开始 -> done 收尾触发插入分支）。"""
        await self.manager.add_chat_history({"role": "user", "content": answer_text})
        await self.manager.add_chat_history({"role": "assistant", "content": "确认"})
        await self.manager.add_chat_history({"role": "assistant", "done": "[DONE]"})

    def _round_count(self):
        meta, entries = chat_memory._load_meta_and_entries(
            self.manager._file_path, self.session_id
        )
        return sum(
            1 for entry in entries
            if isinstance(entry, dict) and entry.get("event") == "chat_round"
        )

    async def test_insert_finish_keeps_clamped_summary(self):
        """插入收尾后摘要保留（钳制值），不再被清空触发整段重压。"""
        await self._seed_rounds(5)
        await self._seed_summary(5)
        self.manager.set_insert_round(3)
        self.manager.clamp_context_summary_for_insert_round(3)
        await self._finish_insert_round("【回答模型提问】回答内容")

        self.assertEqual(self._round_count(), 6, "回答轮应插入第 3 轮之后")
        summary = await self.manager.get_context_summary()
        self.assertIsNotNone(summary, "插入收尾后摘要不应被清空")
        self.assertEqual(summary["source_round_count"], 3)
        self.assertIn("历史事实", render_context_summary(summary))

    async def test_insert_finish_defensively_clamps_when_request_clamp_skipped(self):
        """请求开始未钳制（写盘失败等）时，收尾写入再做一次防御钳制。"""
        await self._seed_rounds(5)
        await self._seed_summary(5)
        self.manager.set_insert_round(3)
        # 故意跳过 clamp_context_summary_for_insert_round
        await self._finish_insert_round("【回答模型提问】回答内容")

        self.assertEqual(self._round_count(), 6)
        summary = await self.manager.get_context_summary()
        self.assertIsNotNone(summary)
        self.assertEqual(summary["source_round_count"], 3, "收尾防御钳制应生效")

    async def test_stop_current_round_insert_keeps_summary(self):
        """用户停止（stop_current_round）中断收尾的插入分支同样保留摘要。"""
        await self._seed_rounds(5)
        await self._seed_summary(5)
        self.manager.set_insert_round(3)
        self.manager.clamp_context_summary_for_insert_round(3)
        # 模拟中断收尾：先记录回答轮消息，再走 stop_current_round
        await self.manager.add_chat_history({"role": "user", "content": "【回答模型提问】中断的回答"})
        await self.manager.add_chat_history({"role": "assistant", "content": "部分输出"})
        await self.manager.stop_current_round()

        summary = await self.manager.get_context_summary()
        self.assertIsNotNone(summary, "中断插入收尾后摘要不应被清空")
        self.assertEqual(summary["source_round_count"], 3)


class ReanswerTruncateSummaryTests(unittest.IsolatedAsyncioTestCase):
    """覆盖式重答（truncate_rounds_for_reanswer）截断后摘要按删除位置平移。"""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._orig_root = chat_memory.HISTORY_ROOT
        chat_memory.HISTORY_ROOT = Path(self._tmp)
        self.session_id = "reanswer_truncate_test"
        self.manager = ChatMemoryManager(self.session_id)

    def tearDown(self):
        chat_memory.HISTORY_ROOT = self._orig_root
        shutil.rmtree(self._tmp, ignore_errors=True)

    async def _seed_rounds(self, count: int):
        for index in range(1, count + 1):
            await self.manager.add_chat_history({"role": "user", "content": f"问题{index}"})
            await self.manager.add_chat_history({"role": "assistant", "content": f"回答{index}"})
            await self.manager.add_chat_history({"role": "assistant", "done": "[DONE]"})

    async def test_truncate_shifts_summary_cursor(self):
        await self._seed_rounds(3)
        # 第 2 轮是提问轮（含 ask_user 调用），其后第 3 轮是回答轮
        meta, entries = chat_memory._load_meta_and_entries(
            self.manager._file_path, self.session_id
        )
        rounds = [e for e in entries if isinstance(e, dict) and e.get("event") == "chat_round"]
        # 改写第 2 轮：加入 ask_user 工具调用
        target = rounds[1]
        target.setdefault("events", []).append({
            "role": "assistant",
            "tool_calls": [{
                "id": "call_ask",
                "type": "function",
                "function": {"name": "ask_user", "arguments": "{}"},
            }],
        })
        # 改写第 3 轮问题为回答格式
        rounds[2]["question"] = "【回答模型提问】第 3 轮是回答"
        chat_memory._write_meta_and_entries(self.manager._file_path, meta, entries)
        await self.manager.update_context_summary({
            "source_round_count": 3,
            "blocks": [{"summary": "覆盖三轮的摘要", "round_start": 1, "round_end": 3}],
            "recent_questions": ["问题1", "问题2", "回答3"],
            "recent_question_numbers": [1, 2, 3],
        })
        removed = await self.manager.truncate_rounds_for_reanswer()
        self.assertEqual(removed, 1, "应删除提问轮之后的回答轮")
        summary = await self.manager.get_context_summary()
        self.assertIsNotNone(summary, "截断后摘要不应整体作废")
        self.assertEqual(summary["source_round_count"], 2)
        self.assertEqual(summary["blocks"][0]["round_end"], 2)
        self.assertEqual(summary["recent_question_numbers"], [1, 2])


class InsertRoundCutoffTests(unittest.IsolatedAsyncioTestCase):
    """insert_round 的上下文截断口径：保留到提问轮（第 N 轮）为止。"""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._orig_root = chat_memory.HISTORY_ROOT
        chat_memory.HISTORY_ROOT = Path(self._tmp)
        self.session_id = "insert_cutoff_test"
        self.manager = ChatMemoryManager(self.session_id)

    def tearDown(self):
        chat_memory.HISTORY_ROOT = self._orig_root
        shutil.rmtree(self._tmp, ignore_errors=True)

    async def _seed_rounds(self, count: int):
        for index in range(1, count + 1):
            await self.manager.add_chat_history({"role": "user", "content": f"问题{index}"})
            await self.manager.add_chat_history({"role": "assistant", "content": f"回答{index}"})
            await self.manager.add_chat_history({"role": "assistant", "done": "[DONE]"})

    async def test_cutoff_keeps_ask_round_only(self):
        await self._seed_rounds(4)
        self.manager.set_insert_round(2)
        self.assertEqual(self.manager._history_cutoff_rounds, 3)
        messages = await self.manager.get_context_messages()
        text = "\n".join(str(item.get("content")) for item in messages)
        self.assertIn("问题1", text)
        self.assertIn("问题2", text)
        self.assertNotIn("问题3", text)
        self.assertNotIn("问题4", text)

    async def test_cutoff_with_summary_clamped(self):
        await self._seed_rounds(4)
        await self.manager.update_context_summary({
            "source_round_count": 3,
            "blocks": [{"summary": "已压缩事实", "round_start": 1, "round_end": 3}],
        })
        self.manager.set_insert_round(3)
        messages = await self.manager.get_context_messages()
        text = "\n".join(str(item.get("content")) for item in messages)
        # 摘要覆盖第 1-3 轮：原始对话只回传第 4 轮之前的窗口；截断到第 3 轮后
        # 无未压缩轮次，仅摘要 system 消息
        self.assertIn("已压缩事实", text)
        self.assertNotIn("问题4", text)


class InsertRoundNoRepressTests(unittest.IsolatedAsyncioTestCase):
    """钳制后自动压缩从插入点之后继续，不再从第 1 轮整段重压。"""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._orig_root = chat_memory.HISTORY_ROOT
        chat_memory.HISTORY_ROOT = Path(self._tmp)
        self.session_id = "insert_no_repress_test"
        self.manager = ChatMemoryManager(self.session_id)
        self.captured_requests = []
        # 压缩调用桩：固定 1 条降级链、零间隔
        self._original_chat = compaction.ChatLLM.chat_completions
        self._original_resolver = compaction.resolve_context_compaction_model_config
        self._original_window = compaction.resolve_model_max_input_tokens
        self._original_retry_attempts = compaction.resolve_compaction_retry_max_attempts
        self._original_retry_interval = compaction._COMPACTION_RETRY_INTERVAL_SECONDS
        compaction.resolve_compaction_retry_max_attempts = lambda: 1
        compaction._COMPACTION_RETRY_INTERVAL_SECONDS = 0
        compaction.resolve_model_max_input_tokens = lambda default=8192: 20_000
        compaction.resolve_context_compaction_model_config = lambda _settings: {
            "selected_provider_name": "Test Provider",
            "selected_model_name": "Test Model",
            "selected_model_id": "test-model",
            "apiType": "chat-completions",
            "maxInputTokens": 20_000,
            "maxOutputTokens": 512,
        }

        def fake_chat_completions(*, request=None, stream=False, model_config=None, **_kwargs):
            self.captured_requests.append(request)
            return {"content": "【目标】压缩后的累计摘要"}

        compaction.ChatLLM.chat_completions = staticmethod(fake_chat_completions)

    def tearDown(self):
        compaction.ChatLLM.chat_completions = staticmethod(self._original_chat)
        compaction.resolve_context_compaction_model_config = self._original_resolver
        compaction.resolve_model_max_input_tokens = self._original_window
        compaction.resolve_compaction_retry_max_attempts = self._original_retry_attempts
        compaction._COMPACTION_RETRY_INTERVAL_SECONDS = self._original_retry_interval
        chat_memory.HISTORY_ROOT = self._orig_root
        shutil.rmtree(self._tmp, ignore_errors=True)

    async def _seed_rounds(self, count: int, payload: str):
        for index in range(1, count + 1):
            await self.manager.add_chat_history({"role": "user", "content": f"问题{index}"})
            await self.manager.add_chat_history({"role": "assistant", "content": payload})
            await self.manager.add_chat_history({"role": "assistant", "done": "[DONE]"})

    async def test_auto_compaction_continues_after_clamp(self):
        await self._seed_rounds(5, "回答" * 2000)
        await self.manager.update_context_summary({
            "source_round_count": 5,
            "blocks": [{"summary": "历史事实", "round_start": 1, "round_end": 5}],
            "recent_questions": [f"问题{i}" for i in range(1, 6)],
            "recent_question_numbers": list(range(1, 6)),
        })
        # 回答插入第 3 轮：摘要覆盖范围钳到第 3 轮
        self.manager.clamp_context_summary_for_insert_round(3)
        summary = await self.manager.get_context_summary()
        self.assertEqual(summary["source_round_count"], 3)

        # 模拟插入完成后收尾压缩（无 force）：配置阈值低于未覆盖历史，
        # 第 4、5 轮需要压缩，但压缩源必须从第 4 轮开始
        request = ChatLLMRequest(messages=[{"role": "user", "content": "新任务"}])
        settings = compaction.ContextCompactionSettings(

            trigger_ratio=0.05,
            summary_budget_ratio=0.2,
            oversized_reject_factor=1.5,
            max_oversized_rejections=3,
        )
        compacted = await compaction.compact_session_history_if_needed(
            self.manager,
            request,
            settings=settings,
            budget_tokens=1024,
        )
        self.assertGreater(compacted, 0, "第 4、5 轮超预算应被压缩")
        self.assertTrue(self.captured_requests, "应发生压缩模型调用")
        first_prompt = "\n".join(
            str(getattr(message, "content", "") or "")
            for message in self.captured_requests[0].messages
        )
        # 压缩源从第 4 轮开始：不再包含第 1-3 轮原文（旧行为整段重压会包含）
        self.assertIn("问题4", first_prompt)
        self.assertNotIn("问题1", first_prompt)
        self.assertNotIn("问题2", first_prompt)
        # 游标从上次位置继续推进（不是从 0 重建）
        final_summary = await self.manager.get_context_summary()
        self.assertGreaterEqual(final_summary["source_round_count"], 4)
        # 摘要正文保留旧事实（拼接合并，不丢弃历史）
        rendered = render_context_summary(final_summary)
        self.assertIn("历史事实", rendered)


if __name__ == "__main__":
    unittest.main()
