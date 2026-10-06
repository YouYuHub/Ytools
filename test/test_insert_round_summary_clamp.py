# -*- coding: utf-8 -*-
"""历史编辑/插入保留压缩快照，并校验轮次索引与原文补充行为。"""
import shutil
import tempfile
import unittest
from pathlib import Path

import memory.chat_memory as chat_memory
from config import ChatLLMRequest
from factory.agent_runtime import context_compaction as compaction
from memory.chat_history_format import (
    render_context_summary,
    shift_context_summary_for_inserted_round,
)
from memory.chat_memory import ChatMemoryManager


class ShiftContextSummaryTests(unittest.TestCase):
    """插入轮次只平移摘要索引，不改摘要快照正文。"""

    def test_shifts_cursor_blocks_and_question_numbers(self):
        summary = {
            "source_round_count": 5,
            "blocks": [{"summary": "旧摘要", "round_start": 1, "round_end": 5}],
            "recent_questions": ["问题一", "问题二", "问题三", "问题四", "问题五"],
            "recent_question_numbers": [1, 2, 3, 4, 5],
        }
        shifted = shift_context_summary_for_inserted_round(summary, 2)
        self.assertEqual(shifted["source_round_count"], 6)
        self.assertEqual(shifted["blocks"][0]["round_start"], 1)
        self.assertEqual(shifted["blocks"][0]["round_end"], 6)
        self.assertEqual(shifted["recent_question_numbers"], [1, 2, 4, 5, 6])
        self.assertEqual(shifted["recent_questions"], summary["recent_questions"])
        # 原摘要正文和源对象均保持不变。
        self.assertEqual(summary["source_round_count"], 5)
        self.assertEqual(len(summary["recent_questions"]), 5)

    def test_insert_after_summary_coverage_keeps_cursor(self):
        summary = {"source_round_count": 3, "summary": "旧摘要"}
        self.assertIs(shift_context_summary_for_inserted_round(summary, 3), summary)


class InsertRoundSummarySnapshotTests(unittest.IsolatedAsyncioTestCase):
    """插入/摘要落盘行为。"""

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

    async def test_legacy_clamp_wrapper_preserves_snapshot_without_writing(self):
        await self._seed_rounds(5)
        await self._seed_summary(5)
        self.manager.clamp_context_summary_for_insert_round(3)
        summary = await self.manager.get_context_summary()
        self.assertEqual(summary["source_round_count"], 5)
        self.assertEqual(summary["blocks"][0]["round_end"], 5)
        self.assertEqual(summary["recent_question_numbers"], [1, 2, 3, 4, 5])
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

    async def test_legacy_clamp_wrapper_does_not_write_or_raise(self):
        await self._seed_rounds(5)
        await self._seed_summary(5)
        original = chat_memory._write_meta_and_entries

        def failing_write(file_path, meta, entries):
            raise OSError("文件被占用（模拟 WinError 5）")

        chat_memory._write_meta_and_entries = failing_write
        try:
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

    async def test_insert_finish_shifts_summary_indices_and_tracks_raw_round(self):
        """插入收尾保留摘要快照，平移锚点并原文补入新轮。"""
        await self._seed_rounds(5)
        await self._seed_summary(5)
        self.manager.set_insert_round(3)
        await self._finish_insert_round("【回答模型提问】回答内容")

        self.assertEqual(self._round_count(), 6, "回答轮应插入第 3 轮之后")
        summary = await self.manager.get_context_summary()
        self.assertIsNotNone(summary, "插入收尾后摘要不应被清空")
        self.assertEqual(summary["source_round_count"], 6)
        self.assertEqual(summary["blocks"][0]["round_end"], 6)
        self.assertEqual(summary["recent_question_numbers"], [1, 2, 3, 5, 6])
        meta, _ = chat_memory._load_meta_and_entries(self.manager._file_path, self.session_id)
        self.assertEqual(meta["context_summary_raw_rounds"], [4])
        self.assertIn("历史事实", render_context_summary(summary))

    async def test_insert_finish_shifts_summary_without_request_preprocessing(self):
        """摘要只在插入轮成功落盘后调整索引，不在请求开始时改写。"""
        await self._seed_rounds(5)
        await self._seed_summary(5)
        self.manager.set_insert_round(3)
        await self._finish_insert_round("【回答模型提问】回答内容")

        self.assertEqual(self._round_count(), 6)
        summary = await self.manager.get_context_summary()
        self.assertIsNotNone(summary)
        self.assertEqual(summary["source_round_count"], 6)
        meta, _ = chat_memory._load_meta_and_entries(self.manager._file_path, self.session_id)
        self.assertEqual(meta["context_summary_raw_rounds"], [4])

    async def test_stop_current_round_insert_keeps_summary(self):
        """用户停止（stop_current_round）中断收尾的插入分支同样保留摘要。"""
        await self._seed_rounds(5)
        await self._seed_summary(5)
        self.manager.set_insert_round(3)
        # 模拟中断收尾：先记录回答轮消息，再走 stop_current_round
        await self.manager.add_chat_history({"role": "user", "content": "【回答模型提问】中断的回答"})
        await self.manager.add_chat_history({"role": "assistant", "content": "部分输出"})
        await self.manager.stop_current_round()

        summary = await self.manager.get_context_summary()
        self.assertIsNotNone(summary, "中断插入收尾后摘要不应被清空")
        self.assertEqual(summary["source_round_count"], 6)
        meta, _ = chat_memory._load_meta_and_entries(self.manager._file_path, self.session_id)
        self.assertEqual(meta["context_summary_raw_rounds"], [4])


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

    async def test_insert_before_first_round_uses_empty_history_and_keeps_suffix(self):
        await self._seed_rounds(3)
        await self.manager.update_context_summary({
            "source_round_count": 2,
            "blocks": [{"summary": "后续轮次秘密", "round_start": 1, "round_end": 2}],
        })
        self.manager.set_insert_round(0)
        self.assertEqual(await self.manager.get_context_messages(), [])
        await self.manager.add_chat_history({"role": "user", "content": "插入问题"})
        await self.manager.add_chat_history({"role": "assistant", "content": "插入回答"})
        await self.manager.add_chat_history({"role": "assistant", "done": "[DONE]"})
        _, entries = chat_memory._load_meta_and_entries(
            self.manager._file_path, self.session_id
        )
        rounds = [row for row in entries if row.get("event") == "chat_round"]
        self.assertEqual([row["question"] for row in rounds],
                         ["插入问题", "问题1", "问题2", "问题3"])
        summary = await self.manager.get_context_summary()
        self.assertEqual(summary["source_round_count"], 3)
        self.assertEqual(summary["blocks"][0]["round_start"], 2)
        self.assertEqual(summary["blocks"][0]["round_end"], 3)
        meta, _ = chat_memory._load_meta_and_entries(self.manager._file_path, self.session_id)
        self.assertEqual(meta["context_summary_raw_rounds"], [1])

    async def test_insert_before_first_round_temporarily_skips_legacy_unindexed_summary(self):
        await self._seed_rounds(2)
        await self.manager.update_context_summary("包含后续对话的旧格式摘要")
        self.manager.set_insert_round(0)

        self.assertEqual(await self.manager.get_context_messages(), [])
        self.assertIn(
            "包含后续对话的旧格式摘要",
            render_context_summary(await self.manager.get_context_summary()),
        )

    async def test_stopped_insert_before_first_round_keeps_suffix(self):
        await self._seed_rounds(2)
        self.manager.set_insert_round(0)
        await self.manager.add_chat_history({"role": "user", "content": "中断问题"})
        await self.manager.add_chat_history({"role": "assistant", "content": "部分回答"})
        await self.manager.stop_current_round()
        _, entries = chat_memory._load_meta_and_entries(
            self.manager._file_path, self.session_id
        )
        rounds = [row for row in entries if row.get("event") == "chat_round"]
        self.assertEqual([row["question"] for row in rounds],
                         ["中断问题", "问题1", "问题2"])

    async def test_cutoff_with_summary_inside_prefix(self):
        await self._seed_rounds(4)
        await self.manager.update_context_summary({
            "source_round_count": 3,
            "blocks": [{"summary": "已压缩事实", "round_start": 1, "round_end": 3}],
        })
        self.manager.set_insert_round(3)
        messages = await self.manager.get_context_messages()
        text = "\n".join(str(item.get("content")) for item in messages)
        # 摘要覆盖的轮次全部位于插入点之前，可以直接使用摘要。
        self.assertIn("已压缩事实", text)
        self.assertNotIn("问题4", text)

    async def test_insert_request_temporarily_omits_summary_crossing_prefix(self):
        await self._seed_rounds(3)
        await self.manager.update_context_summary({
            "source_round_count": 3,
            "blocks": [{"summary": "问题3的后续秘密", "round_start": 1, "round_end": 3}],
        })
        await self.manager.add_context_compaction_event({
            "event": "context_compaction", "scope": "session", "phase": "done",
            "summary_text": "问题3的后续秘密",
        })
        self.manager.set_insert_round(1)
        messages = await self.manager.get_context_messages()
        text = "\n".join(str(item.get("content")) for item in messages)
        self.assertIn("问题1", text)
        self.assertNotIn("问题2", text)
        self.assertNotIn("问题3", text)
        self.assertNotIn("后续秘密", text)
        summary = await self.manager.get_context_summary()
        self.assertIn("问题3的后续秘密", render_context_summary(summary))
        _, entries = chat_memory._load_meta_and_entries(
            self.manager._file_path, self.session_id
        )
        historical = [row for row in entries if row.get("event") == "context_compaction"]
        self.assertEqual(len(historical), 1)
        self.assertFalse(historical[0].get("invalidated", False))

    async def test_strict_insert_keeps_summary_before_position(self):
        await self._seed_rounds(3)
        await self.manager.update_context_summary({
            "source_round_count": 1,
            "blocks": [{"summary": "第一轮事实", "round_start": 1, "round_end": 1}],
        })
        self.manager.set_insert_round(2)
        messages = await self.manager.get_context_messages()
        text = "\n".join(str(item.get("content")) for item in messages)
        self.assertIn("第一轮事实", text)
        self.assertIn("问题2", text)
        self.assertNotIn("问题3", text)


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

    async def test_inserted_raw_override_does_not_discard_or_recompress_snapshot(self):
        await self._seed_rounds(5, "回答" * 2000)
        await self.manager.update_context_summary({
            "source_round_count": 5,
            "blocks": [{"summary": "历史事实", "round_start": 1, "round_end": 5}],
            "recent_questions": [f"问题{i}" for i in range(1, 6)],
            "recent_question_numbers": list(range(1, 6)),
        })
        # 回答插入第 3 轮后：摘要游标和问题索引平移，新轮以原文补充。
        self.manager.set_insert_round(3)
        await self.manager.add_chat_history({"role": "user", "content": "插入的新问题"})
        await self.manager.add_chat_history({"role": "assistant", "content": "插入的新回答"})
        await self.manager.add_chat_history({"role": "assistant", "done": "[DONE]"})
        summary = await self.manager.get_context_summary()
        self.assertEqual(summary["source_round_count"], 6)
        meta, _ = chat_memory._load_meta_and_entries(self.manager._file_path, self.session_id)
        self.assertEqual(meta["context_summary_raw_rounds"], [4])

        # 新摘要游标已经覆盖原有历史；检查触发预算时不会因插入而把摘要清空，
        # 也不会自动从第一轮重新压缩。
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
        self.assertEqual(compacted, 0)
        self.assertFalse(self.captured_requests, "插入本身不应触发重压缩")
        final_summary = await self.manager.get_context_summary()
        self.assertEqual(final_summary["source_round_count"], 6)
        rendered = render_context_summary(final_summary)
        self.assertIn("历史事实", rendered)
        messages = await self.manager.get_context_messages()
        context_text = "\n".join(str(message.get("content")) for message in messages)
        self.assertIn("插入的新问题", context_text)
        self.assertIn("插入的新回答", context_text)
        self.assertIn("第 4 轮在摘要生成后被编辑或插入", context_text)


if __name__ == "__main__":
    unittest.main()
