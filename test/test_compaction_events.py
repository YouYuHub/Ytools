import tempfile
import unittest
from pathlib import Path

import memory.chat_memory as chat_memory
from memory.chat_memory import ChatMemoryManager


class CompactionEventPersistenceTests(unittest.IsolatedAsyncioTestCase):
    """压缩事件落盘：SSE 推送与 JSONL 保存同一份 payload，字段一致且不污染统计。"""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._orig_root = chat_memory.HISTORY_ROOT
        chat_memory.HISTORY_ROOT = Path(self._tmp)
        self.session_id = "compaction_event_test"
        self.manager = ChatMemoryManager(self.session_id)

    def tearDown(self):
        chat_memory.HISTORY_ROOT = self._orig_root

    async def _add_round(self, question: str):
        await self.manager.add_chat_history({"role": "user", "content": question})
        await self.manager.add_chat_history({"role": "assistant", "content": "回答"})
        await self.manager.add_chat_history({"role": "assistant", "done": "[DONE]"})

    def _load_entries(self):
        _, entries = chat_memory._load_meta_and_entries(
            self.manager._file_path, self.session_id
        )
        return entries

    async def test_compaction_events_are_appended_verbatim_with_timestamp(self):
        await self.manager.add_chat_history({"role": "user", "content": "压缩当前轮"})
        await self.manager.add_chat_history({"role": "tool", "content": "轨迹"})
        payload = {
            "event": "context_compaction",
            "scope": "round",
            "phase": "start",
            "role": "assistant",
            "compress_context": "【上下文摘要】\n...",
        }
        result = await self.manager.add_context_compaction_event(payload)
        self.assertEqual(result, "记录成功")
        await self.manager.add_chat_history({"role": "assistant", "done": "[DONE]"})

        entries = self._load_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["event"], "chat_round")
        saved = next(
            event for event in entries[0]["events"]
            if event.get("event") == "context_compaction"
        )
        for key in ("event", "scope", "phase", "role", "compress_context"):
            self.assertEqual(saved[key], payload[key])
        self.assertTrue(saved.get("timestamp"))

    async def test_compaction_events_do_not_pollute_usage_or_question_stats(self):
        await self._add_round("问题一")
        await self.manager.add_chat_history({
            "role": "assistant",
            "content": "完成",
            "done": "[DONE]",
        })
        await self.manager.add_context_compaction_event({
            "event": "context_compaction",
            "scope": "session",
            "phase": "done",
            "role": "assistant",
            "summary_usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        })

        meta = await self.manager.get_session_meta()
        self.assertEqual(meta["user_questions"], ["问题一"])
        self.assertNotIn("prompt_tokens", meta.get("usage", {}))
        entries = self._load_entries()
        self.assertEqual(sum(1 for e in entries if e.get("event") == "chat_round"), 1)
        self.assertEqual(sum(1 for e in entries if e.get("event") == "context_compaction"), 1)

    async def test_manual_session_compaction_phases_are_independent_rows_between_rounds(self):
        """空闲手动压缩按追加顺序落在旧历史后、后续对话前。"""
        await self._add_round("压缩前的问题")
        await self.manager.add_context_compaction_event({
            "event": "context_compaction", "scope": "session", "phase": "start",
            "role": "assistant", "context_summary": "待压缩历史",
        })
        await self.manager.add_context_compaction_event({
            "event": "context_compaction", "scope": "session", "phase": "done",
            "role": "assistant", "summary_text": "累计摘要",
        })
        await self._add_round("压缩后的问题")

        entries = self._load_entries()
        self.assertEqual(
            [(entry.get("event"), entry.get("phase")) for entry in entries],
            [
                ("chat_round", None),
                ("context_compaction", "start"),
                ("context_compaction", "done"),
                ("chat_round", None),
            ],
        )
        self.assertNotIn("display_round", entries[1])
        self.assertNotIn("display_event_index", entries[1])
        self.assertNotIn("display_round", entries[2])
        self.assertNotIn("display_event_index", entries[2])
        self.assertTrue(entries[1].get("timestamp"))
        self.assertTrue(entries[2].get("timestamp"))

    async def test_session_compaction_events_capture_current_round_event_anchors(self):
        """活动轮次中的跨轮压缩独立落行并锚定各自发生的事件边界。"""
        await self._add_round("上一轮")
        await self.manager.add_chat_history({"role": "user", "content": "当前轮"})
        await self.manager.add_chat_history({"role": "tool", "tool_name": "read_file", "result": "内容"})
        await self.manager.add_context_compaction_event({
            "event": "context_compaction", "scope": "session", "phase": "start",
            "role": "assistant", "context_summary": "待压缩历史",
        })
        await self.manager.add_chat_history({"role": "assistant", "content": "继续回答"})
        await self.manager.add_context_compaction_event({
            "event": "context_compaction", "scope": "session", "phase": "done",
            "role": "assistant", "summary_text": "累计摘要",
        })
        await self.manager.add_chat_history({"role": "assistant", "done": "[DONE]"})

        entries = self._load_entries()
        self.assertEqual(
            [entry.get("event") for entry in entries],
            ["chat_round", "context_compaction", "context_compaction", "chat_round"],
        )
        start, done = entries[1], entries[2]
        self.assertEqual((start["display_round"], start["display_event_index"]), (2, 2))
        self.assertEqual((done["display_round"], done["display_event_index"]), (2, 3))
        self.assertTrue(any(
            event.get("role") == "assistant" and event.get("content") == "继续回答"
            for event in entries[3]["events"]
        ))

    async def test_insert_round_relocates_anchored_compaction_rows_before_round(self):
        """回答插入（ask_user 再答）收尾：锚定到插入轮次的跨轮压缩行重排到轮次行之前。

        回答轮执行中触发的跨轮压缩先独立落盘（display_round 锚定到插入轮次），
        轮次行最后才以"插入第 N 轮之后"收尾——不重排会让压缩行滞留在其锚定
        轮次行之后（旧前端把压缩块兜底显示到会话末尾）。
        """
        await self._add_round("提问轮")
        self.manager.set_insert_round(1)
        await self.manager.add_chat_history({"role": "user", "content": "【回答模型提问】继续"})
        await self.manager.add_context_compaction_event({
            "event": "context_compaction", "scope": "session", "phase": "start",
            "role": "assistant", "context_summary": "待压缩历史",
        })
        await self.manager.add_context_compaction_event({
            "event": "context_compaction", "scope": "session", "phase": "done",
            "role": "assistant", "summary_text": "累计摘要",
        })
        await self.manager.add_chat_history({"role": "assistant", "done": "[DONE]"})

        entries = self._load_entries()
        # 行序契约：压缩行先于其锚定的轮次行（而不是滞留在插入的轮次行之后）
        self.assertEqual(
            [entry.get("event") for entry in entries],
            ["chat_round", "context_compaction", "context_compaction", "chat_round"],
        )
        self.assertEqual(entries[1]["display_round"], 2)
        self.assertEqual(entries[2]["display_round"], 2)
        self.assertTrue(any(
            event.get("role") == "user" and event.get("content") == "【回答模型提问】继续"
            for event in entries[3]["events"]
        ))

    async def test_replace_round_relocates_anchored_compaction_rows_before_round(self):
        """编辑重发（原地重跑）收尾：锚定到目标轮次的跨轮压缩行重排到替换位置之前。"""
        await self._add_round("问题一")
        await self._add_round("问题二")
        self.manager.set_target_round(1)
        await self.manager.add_chat_history({"role": "user", "content": "问题一（编辑后）"})
        await self.manager.add_context_compaction_event({
            "event": "context_compaction", "scope": "session", "phase": "start",
            "role": "assistant", "context_summary": "待压缩历史",
        })
        await self.manager.add_context_compaction_event({
            "event": "context_compaction", "scope": "session", "phase": "done",
            "role": "assistant", "summary_text": "累计摘要",
        })
        await self.manager.add_chat_history({"role": "assistant", "done": "[DONE]"})

        entries = self._load_entries()
        # 压缩行重排到替换位置之前；替换后的轮次行与后续轮次保持原顺序
        self.assertEqual(
            [entry.get("event") for entry in entries],
            ["context_compaction", "context_compaction", "chat_round", "chat_round"],
        )
        self.assertEqual(entries[0]["display_round"], 1)
        self.assertEqual(entries[1]["display_round"], 1)
        self.assertTrue(any(
            event.get("role") == "user" and event.get("content") == "问题一（编辑后）"
            for event in entries[2]["events"]
        ))
        self.assertTrue(any(
            event.get("role") == "user" and event.get("content") == "问题二"
            for event in entries[3]["events"]
        ))

    async def test_stop_round_relocates_anchored_compaction_rows_before_round(self):
        """停止收尾（编辑重发后中断）：同样重排锚定压缩行到轮次行之前。"""
        await self._add_round("问题一")
        await self._add_round("问题二")
        self.manager.set_target_round(1)
        await self.manager.add_chat_history({"role": "user", "content": "问题一（编辑后）"})
        await self.manager.add_context_compaction_event({
            "event": "context_compaction", "scope": "session", "phase": "start",
            "role": "assistant", "context_summary": "待压缩历史",
        })
        await self.manager.add_context_compaction_event({
            "event": "context_compaction", "scope": "session", "phase": "done",
            "role": "assistant", "summary_text": "累计摘要",
        })
        await self.manager.stop_current_round()

        entries = self._load_entries()
        self.assertEqual(
            [entry.get("event") for entry in entries],
            ["context_compaction", "context_compaction", "chat_round", "chat_round"],
        )
        self.assertEqual(entries[2]["status"], "stopped")
        self.assertEqual(entries[3]["status"], "done")

    async def test_invalid_compaction_event_rejected(self):
        result = await self.manager.add_context_compaction_event({"role": "assistant", "x": 1})
        self.assertEqual(result, "忽略非法压缩事件")
        self.assertEqual(len(self._load_entries()), 0)

    async def test_orphan_compaction_starts_detected_and_aborted(self):
        """任务中断遗留（有 start 无 done/aborted）的压缩事件被检测并可标记中断。"""
        await self._add_round("问题一")
        # session scope：start 后正常 done -> 不算孤儿
        await self.manager.add_context_compaction_event({
            "event": "context_compaction", "scope": "session", "phase": "start",
            "role": "assistant", "context_summary": "待压缩",
        })
        await self.manager.add_context_compaction_event({
            "event": "context_compaction", "scope": "session", "phase": "done",
            "role": "assistant", "summary_usage": {"total_tokens": 10},
        })
        # round scope：start 无 done -> 活动检查点中的孤儿
        await self.manager.add_chat_history({"role": "user", "content": "当前任务"})
        await self.manager.add_context_compaction_event({
            "event": "context_compaction", "scope": "round", "phase": "start",
            "role": "assistant", "compress_context": "待压缩轨迹",
        })

        orphans = await self.manager.find_orphan_compaction_events()
        self.assertEqual(len(orphans), 1)
        self.assertEqual(orphans[0]["scope"], "round")
        self.assertEqual(orphans[0]["phase"], "start")

        result = await self.manager.mark_compaction_aborted(orphans[0])
        self.assertEqual(result, "记录成功")

        # 标记后不再有孤儿
        self.assertEqual(await self.manager.find_orphan_compaction_events(), [])
        meta = await self.manager.get_session_meta()
        active_events = meta["_active_round_compaction"]["events"]
        aborted = [e for e in active_events if e.get("phase") == "aborted"]
        self.assertEqual(len(aborted), 1)
        self.assertEqual(aborted[0]["scope"], "round")
        self.assertEqual(aborted[0]["reason"], "task_interrupted")


if __name__ == "__main__":
    unittest.main()
