"""reasoning_content 请求整形与历史落盘隔离的单元测试。

覆盖：请求副本剥离历史思考、最新工具轮回退/裁剪/占位，以及
chat_round_store 对运行时消息和检查点的深拷贝隔离。
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from factory import chat_factory as cf


class MessagesDebugSummaryTests(unittest.TestCase):
    def test_reasoning_preview_is_shown_separately_from_message_limit(self):
        reasoning = "思" * 120
        summary = cf._messages_debug_summary([
            {"role": "assistant", "content": "回复内容", "reasoning_content": reasoning},
        ])
        expected_preview = repr("思" * 100 + "…")
        self.assertIn(f"reasoning_content[120]={expected_preview}", summary)

    def test_messages_without_reasoning_keep_compact_summary(self):
        summary = cf._messages_debug_summary([{"role": "user", "content": "问题"}])
        self.assertIn("'content': '问题'", summary)
        self.assertNotIn("reasoning_content", summary)


class ReasoningForToolCallTests(unittest.TestCase):
    """_reasoning_content_for_tool_call 的长度语义与占位兜底。"""

    def test_negative_limit_returns_full(self):
        self.assertEqual(
            cf._reasoning_content_for_tool_call("abc", -1), "abc"
        )

    def test_positive_limit_keeps_tail(self):
        self.assertEqual(
            cf._reasoning_content_for_tool_call("abcdef", 3), "def"
        )

    def test_zero_limit_returns_placeholder(self):
        self.assertEqual(cf._reasoning_content_for_tool_call("abcdef", 0), "...")

    def test_empty_source_returns_placeholder(self):
        for source in ("", "   ", None, 123):
            self.assertEqual(cf._reasoning_content_for_tool_call(source, -1), "...")

    def test_placeholder_never_empty(self):
        # 最新工具调用的回传字段即使来源缺失，也不返回空串/None
        result = cf._reasoning_content_for_tool_call("", 100)
        self.assertTrue(result and result.strip())


class PersistedHistoryNotPollutedTests(unittest.TestCase):
    """落盘检查点与运行时消息互相隔离，后续修改不污染已记录的数据。"""

    def _record(self, reasoning="真实思考"):
        return {
            "role": "assistant",
            "content": "先查一下",
            "reasoning_content": reasoning,
            "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {"name": "search_files", "arguments": "{}"},
            }],
        }

    def test_record_message_deep_copies_runtime_dict(self):
        from memory.chat_round_store import ChatRoundStore

        store = ChatRoundStore(session_id="ut-persist")
        record = self._record()
        store.record_message(record)
        # 模拟记录后的运行时修改
        record["reasoning_content"] = "修改后的思考"
        events = store.pending_round["events"]
        self.assertEqual(events[0]["reasoning_content"], "真实思考")

    def test_finalize_round_keeps_original_reasoning_after_runtime_mutation(self):
        from memory.chat_round_store import ChatRoundStore

        store = ChatRoundStore(session_id="ut-persist")
        record = self._record()
        store.record_message(record)
        record["reasoning_content"] = "修改后的思考"
        completed = store.finalize_round("done")
        self.assertEqual(completed["events"][0]["reasoning_content"], "真实思考")

    def test_checkpoint_snapshot_immune_to_later_mutation(self):
        from memory.chat_round_store import ChatRoundStore

        store = ChatRoundStore(session_id="ut-persist")
        record = self._record()
        store.record_message(record)
        checkpoint = store.snapshot_pending_round()
        record["reasoning_content"] = "修改后的思考"
        self.assertEqual(checkpoint["events"][0]["reasoning_content"], "真实思考")

    def test_deep_copy_preserves_nested_structures(self):
        from memory.chat_round_store import ChatRoundStore

        store = ChatRoundStore(session_id="ut-persist")
        record = self._record()
        record["extra"] = {"nested": {"list": [1, 2]}}
        store.record_message(record)
        record["extra"]["nested"]["list"].append(3)
        events = store.pending_round["events"]
        self.assertEqual(events[0]["extra"]["nested"]["list"], [1, 2])


class CopyForRequestTests(unittest.TestCase):
    """_copy_for_request：思考整形只作用于请求副本，运行时 messages 保持真实。"""

    def _messages(self):
        return [
            {"role": "user", "content": "hi"},
            self._assistant_msg("第一轮", reasoning="第一轮真实思考"),
            {"role": "tool", "tool_call_id": "call_1", "content": "结果"},
            self._assistant_msg("第二轮"),  # 模型未输出思考的轮
            {"role": "tool", "tool_call_id": "call_2", "content": "结果2"},
            self._assistant_msg("普通回答", reasoning="最终思考"),
        ]

    @staticmethod
    def _assistant_msg(content, reasoning=None, tool_calls=1):
        message = {"role": "assistant", "content": content}
        if reasoning is not None:
            message["reasoning_content"] = reasoning
        if tool_calls:
            message["tool_calls"] = [{
                "id": f"call_{index}",
                "type": "function",
                "function": {"name": "tool", "arguments": "{}"},
            } for index in range(1, tool_calls + 1)]
        return message

    def test_original_messages_untouched(self):
        messages = self._messages()
        copied = cf._copy_for_request(messages)
        # 运行时 messages 不被原地修改（字段剥离只发生在副本上）：
        # 有思考的保留原值，无思考的也不会凭空出现占位
        self.assertEqual(messages[1]["reasoning_content"], "第一轮真实思考")
        self.assertNotIn("reasoning_content", messages[3])
        # 副本：旧 assistant（[1] 工具轮、[3] 无思考的工具轮）历史思考一律
        # 不回传（字段剥离）；最终回答（最新 assistant，非工具轮）保留真实思考
        self.assertNotIn("reasoning_content", copied[1])
        self.assertNotIn("reasoning_content", copied[3])
        self.assertEqual(copied[5]["reasoning_content"], "最终思考")

    def test_request_copy_isolates_big_fields(self):
        # 非 assistant 消息与未修改的 assistant 复用原对象（不深拷贝 base64）
        media_msg = {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}
        messages = [
            media_msg,
            self._assistant_msg("旧轮", reasoning="旧思考"),
            {"role": "tool", "tool_call_id": "call_1", "content": "结果"},
            self._assistant_msg("最新", reasoning="新思考"),
            {"role": "tool", "tool_call_id": "call_2", "content": "结果"},
        ]
        copied = cf._copy_for_request(messages)
        # 被剥离思考字段的 assistant 是新 dict；其余原样引用
        self.assertIsNot(copied[1], messages[1])
        self.assertIs(copied[0], messages[0])
        self.assertIs(copied[3], messages[3])
        # 原始消息完全未被改写
        self.assertEqual(messages[1]["reasoning_content"], "旧思考")

    def test_latest_tool_round_without_reasoning_falls_back_to_previous_thinking(self):
        # 最新工具轮未输出思考时，副本向前回溯最近一次真实思考回传；
        # 历史工具轮的思考不回传（字段剥离），请求里只保留最新一条真实思考；
        # 运行时 messages 不受请求整形影响，历史思考保持真实
        messages = [
            {"role": "user", "content": "hi"},
            self._assistant_msg("第一轮", reasoning="第一轮真实思考"),
            {"role": "tool", "tool_call_id": "call_1", "content": "结果"},
            self._assistant_msg("第二轮"),  # 最新工具轮缺思考
            {"role": "tool", "tool_call_id": "call_2", "content": "结果"},
        ]
        copied = cf._copy_for_request(messages)
        self.assertEqual(copied[3]["reasoning_content"], "第一轮真实思考")
        self.assertNotIn("reasoning_content", copied[1])
        # 运行时 messages 中第一轮思考仍真实完整，落盘不受影响
        self.assertEqual(messages[1]["reasoning_content"], "第一轮真实思考")
        self.assertNotIn("reasoning_content", messages[3])

    def test_fallback_applies_limit_rules_in_copy(self):
        # 回退思考同样遵守回传长度配置：正数保留末尾 N 字符，0 回占位符
        messages = [
            {"role": "user", "content": "hi"},
            self._assistant_msg("第一轮", reasoning="abcdefghijklmnopqrstuvwxyz"),
            {"role": "tool", "tool_call_id": "call_1", "content": "结果"},
            self._assistant_msg("第二轮"),  # 最新工具轮缺思考
            {"role": "tool", "tool_call_id": "call_2", "content": "结果"},
        ]
        with patch.object(cf, "_load_reasoning_return_max_length", return_value=5):
            copied = cf._copy_for_request(messages)
        self.assertEqual(copied[3]["reasoning_content"], "vwxyz")
        with patch.object(cf, "_load_reasoning_return_max_length", return_value=0):
            copied2 = cf._copy_for_request(messages)
        self.assertEqual(copied2[3]["reasoning_content"], "...")

    def test_latest_missing_without_history_gets_placeholder(self):
        messages = [
            {"role": "user", "content": "hi"},
            self._assistant_msg("提问"),
        ]
        copied = cf._copy_for_request(messages)
        self.assertEqual(copied[1]["reasoning_content"], "...")
        self.assertNotIn("reasoning_content", messages[1])

    def test_non_tool_old_assistant_reasoning_stripped_in_copy(self):
        messages = [
            {"role": "user", "content": "hi"},
            self._assistant_msg("普通回答", reasoning="旧回答思考", tool_calls=0),
            self._assistant_msg("下一轮", reasoning="新思考", tool_calls=0),
        ]
        copied = cf._copy_for_request(messages)
        self.assertNotIn("reasoning_content", copied[1])
        self.assertEqual(copied[2]["reasoning_content"], "新思考")
        # 原始 messages 不受影响
        self.assertEqual(messages[1]["reasoning_content"], "旧回答思考")

    def test_limit_semantics_apply_in_copy(self):
        messages = [
            {"role": "user", "content": "hi"},
            self._assistant_msg("提问", reasoning="abcdefghijklmnopqrstuvwxyz"),
        ]
        with patch.object(cf, "_load_reasoning_return_max_length", return_value=5):
            copied = cf._copy_for_request(messages)
        # 最新工具轮的思考在副本中按回传长度裁剪（运行时 messages 保存
        # 原始全文，裁剪是纯"回传"语义，只发生在请求副本上）
        self.assertEqual(copied[1]["reasoning_content"], "vwxyz")
        self.assertEqual(
            messages[1]["reasoning_content"], "abcdefghijklmnopqrstuvwxyz"
        )
        with patch.object(cf, "_load_reasoning_return_max_length", return_value=0):
            messages2 = [{"role": "user", "content": "hi"}, self._assistant_msg("提问")]
            copied2 = cf._copy_for_request(messages2)
        self.assertEqual(copied2[1]["reasoning_content"], "...")

    def test_limit_zero_keeps_placeholder_for_latest_tool_round(self):
        # limit==0：最新工具轮即使有真实思考，回传也只给占位符
        # （上游字段校验要求字段存在），落盘/运行时不受影响
        messages = [
            {"role": "user", "content": "hi"},
            self._assistant_msg("提问", reasoning="真实思考全文"),
        ]
        with patch.object(cf, "_load_reasoning_return_max_length", return_value=0):
            copied = cf._copy_for_request(messages)
        self.assertEqual(copied[1]["reasoning_content"], "...")
        self.assertEqual(messages[1]["reasoning_content"], "真实思考全文")

    def test_limit_zero_drops_non_tool_latest_reasoning_in_copy(self):
        # 非工具轮不强制回传思考：limit==0 时副本直接剥离字段
        messages = [
            {"role": "user", "content": "hi"},
            self._assistant_msg("普通回答", reasoning="最终思考", tool_calls=0),
        ]
        with patch.object(cf, "_load_reasoning_return_max_length", return_value=0):
            copied = cf._copy_for_request(messages)
        self.assertNotIn("reasoning_content", copied[1])
        self.assertEqual(messages[1]["reasoning_content"], "最终思考")

    def test_non_tool_latest_reasoning_trimmed_in_copy(self):
        # 非工具轮已有思考：副本按回传长度裁剪（保留末尾 N 字符）
        messages = [
            {"role": "user", "content": "hi"},
            self._assistant_msg("普通回答", reasoning="abcdefghijklmnopqrstuvwxyz", tool_calls=0),
        ]
        with patch.object(cf, "_load_reasoning_return_max_length", return_value=5):
            copied = cf._copy_for_request(messages)
        self.assertEqual(copied[1]["reasoning_content"], "vwxyz")
        self.assertEqual(
            messages[1]["reasoning_content"], "abcdefghijklmnopqrstuvwxyz"
        )


if __name__ == "__main__":
    unittest.main()
