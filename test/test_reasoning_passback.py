"""reasoning_content 回传修复的单元测试。

覆盖：
- _retain_latest_reasoning：旧工具轮占位符化、最新轮保留真实思考、
  最新轮缺失时回退上下文最近真实思考、非工具轮剥离思考、无 assistant 消息容错
- _reasoning_content_for_tool_call：limit 语义（负数全量/正数末尾截断/0 占位）
  与空来源占位
- _latest_reasoning_content：跳过占位符取最近真实思考
- 落盘解耦：chat_round_store.record_message 深拷贝运行时 dict，
  占位化/裁剪只影响模型请求，JSONL 落盘永远是模型原始输出
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from factory import chat_factory as cf


def _assistant(content="", reasoning=None, tool_calls=None):
    message = {"role": "assistant", "content": content}
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    if tool_calls:
        message["tool_calls"] = [
            {
                "id": f"call_{index}",
                "type": "function",
                "function": {"name": f"tool_{index}", "arguments": "{}"},
            }
            for index in range(1, tool_calls + 1)
        ]
    return message


class RetainLatestReasoningTests(unittest.TestCase):
    """_retain_latest_reasoning 的保留/占位/剥离行为。"""

    def test_old_tool_rounds_become_placeholder_latest_keeps_reasoning(self):
        messages = [
            {"role": "user", "content": "hi"},
            _assistant("先查一下", reasoning="第一轮思考", tool_calls=1),
            {"role": "tool", "tool_call_id": "call_1", "content": "结果"},
            _assistant("再查一次", reasoning="第二轮思考", tool_calls=1),
            {"role": "tool", "tool_call_id": "call_2", "content": "结果2"},
        ]
        cf._retain_latest_reasoning(messages)
        self.assertEqual(messages[1]["reasoning_content"], "...")
        # 最新一条（最后 assistant）保留真实思考
        self.assertEqual(messages[3]["reasoning_content"], "第二轮思考")

    def test_latest_missing_reasoning_falls_back_to_context_latest(self):
        messages = [
            {"role": "user", "content": "hi"},
            _assistant(reasoning="更早的真实思考", tool_calls=1),
            {"role": "tool", "tool_call_id": "call_1", "content": "结果"},
            _assistant(tool_calls=1),  # 最新一条缺思考（如流式中断未产出）
            {"role": "tool", "tool_call_id": "call_2", "content": "结果"},
        ]
        cf._retain_latest_reasoning(messages)
        self.assertEqual(messages[1]["reasoning_content"], "...")
        self.assertEqual(messages[3]["reasoning_content"], "更早的真实思考")

    def test_latest_missing_without_history_uses_placeholder(self):
        messages = [
            {"role": "user", "content": "hi"},
            _assistant(tool_calls=1),
        ]
        cf._retain_latest_reasoning(messages)
        self.assertEqual(messages[1]["reasoning_content"], "...")

    def test_non_tool_assistant_reasoning_removed(self):
        messages = [
            {"role": "user", "content": "hi"},
            _assistant("普通回答", reasoning="最终回答的思考"),
            _assistant(tool_calls=1),
            {"role": "tool", "tool_call_id": "call_1", "content": "结果"},
        ]
        cf._retain_latest_reasoning(messages)
        # 旧的非工具 assistant：思考字段彻底移除（不产生占位噪声）
        self.assertNotIn("reasoning_content", messages[1])
        # 最新一条是工具轮且缺思考：回退上下文最近真实思考（含非工具轮的）
        self.assertEqual(messages[2]["reasoning_content"], "最终回答的思考")

    def test_limit_zero_uses_placeholder_even_for_latest(self):
        messages = [
            {"role": "user", "content": "hi"},
            _assistant(reasoning="第一轮", tool_calls=1),
            {"role": "tool", "tool_call_id": "call_1", "content": "结果"},
            _assistant(tool_calls=1),  # 最新缺思考
        ]
        with patch.object(cf, "_load_reasoning_return_max_length", return_value=0):
            cf._retain_latest_reasoning(messages)
        self.assertEqual(messages[1]["reasoning_content"], "...")
        self.assertEqual(messages[3]["reasoning_content"], "...")

    def test_no_assistant_messages_is_noop(self):
        messages = [{"role": "user", "content": "hi"}]
        cf._retain_latest_reasoning(messages)
        self.assertEqual(messages, [{"role": "user", "content": "hi"}])


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
        # 字段必须存在：即使来源缺失，也不允许返回空串/None
        result = cf._reasoning_content_for_tool_call("", 100)
        self.assertTrue(result and result.strip())


class LatestReasoningContentTests(unittest.TestCase):
    """_latest_reasoning_content 跳过占位符取最近真实思考。"""

    def test_returns_latest_real_reasoning(self):
        messages = [
            _assistant(reasoning="旧思考", tool_calls=1),
            _assistant(reasoning="新思考", tool_calls=1),
        ]
        self.assertEqual(cf._latest_reasoning_content(messages), "新思考")

    def test_skips_placeholders(self):
        messages = [
            _assistant(reasoning="真实思考", tool_calls=1),
            _assistant(reasoning="...", tool_calls=1),
        ]
        self.assertEqual(cf._latest_reasoning_content(messages), "真实思考")

    def test_missing_returns_empty(self):
        self.assertEqual(cf._latest_reasoning_content([]), "")
        self.assertEqual(cf._latest_reasoning_content([{"role": "user", "content": "x"}]), "")


class PersistedHistoryNotPollutedTests(unittest.TestCase):
    """落盘数据与运行时 messages 解耦：占位化只影响模型请求，不污染历史。

    回归背景：旧实现里 record_message 直接把运行时 dict 引用挂进
    pending_round.events，下一轮 _retain_latest_reasoning 原地把旧工具轮的
    reasoning_content 改成 "..."，轮次收尾序列化时落盘的思考过程失真。
    """

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
        # 模拟下一轮运行时占位化（原地修改同一个 dict）
        record["reasoning_content"] = cf._REASONING_PLACEHOLDER
        events = store.pending_round["events"]
        self.assertEqual(events[0]["reasoning_content"], "真实思考")

    def test_finalize_round_keeps_original_reasoning_after_runtime_mutation(self):
        from memory.chat_round_store import ChatRoundStore

        store = ChatRoundStore(session_id="ut-persist")
        record = self._record()
        store.record_message(record)
        record["reasoning_content"] = cf._REASONING_PLACEHOLDER
        completed = store.finalize_round("done")
        self.assertEqual(completed["events"][0]["reasoning_content"], "真实思考")

    def test_checkpoint_snapshot_immune_to_later_mutation(self):
        from memory.chat_round_store import ChatRoundStore

        store = ChatRoundStore(session_id="ut-persist")
        record = self._record()
        store.record_message(record)
        checkpoint = store.snapshot_pending_round()
        record["reasoning_content"] = cf._REASONING_PLACEHOLDER
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

    def test_end_to_end_placeholder_only_in_runtime_not_in_round(self):
        # 端到端模拟：入轮次 → 运行时占位化 → 收尾，轮内数据保持真实思考
        from memory.chat_round_store import ChatRoundStore

        store = ChatRoundStore(session_id="ut-persist")
        record = self._record(reasoning="第一轮真实思考")
        store.record_message(record)
        messages = [
            record,
            self._record(reasoning="第二轮真实思考"),
        ]
        cf._retain_latest_reasoning(messages)  # 旧轮（record）被 in-place 占位
        completed = store.finalize_round("done")
        self.assertEqual(record["reasoning_content"], "...")
        self.assertEqual(completed["events"][0]["reasoning_content"], "第一轮真实思考")


class CopyForRequestTests(unittest.TestCase):
    """_copy_for_request：占位化只作用于请求副本，运行时 messages 保持真实。"""

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
        # 运行时 messages 不被原地修改（占位化只发生在副本上）：
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
        # 历史工具轮的思考不回传（字段剥离），请求里永远只有一条真实思考；
        # 运行时 messages 不被占位化污染，历史思考保持真实
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
