import unittest

from memory.chat_history_format import (
    format_tool_call_history_line,
    normalize_context_summary,
    render_context_summary,
    round_entry_to_context_messages,
)


class ChatHistoryFormatTests(unittest.TestCase):
    def test_round_entry_to_context_messages_skips_think_and_done(self):
        round_entry = {
            "question": "查时间",
            "events": [
                {"role": "user", "content": "现在几点？"},
                {"role": "assistant", "reasoning_content": "调用 run_pipe_command"},
                {"role": "assistant", "content": "<think>先取时间</think>"},
                {"role": "assistant", "tool_calls": [
                    {"function": {"name": "run_pipe_command", "arguments": "{}"}}
                ]},
                {"role": "tool", "content": "14:20"},
                {"role": "assistant", "content": "现在是 14:20"},
                {"role": "assistant", "content": "现在是 14:20"},
                {"role": "assistant", "done": "[DONE]"},
            ],
        }
        messages = round_entry_to_context_messages(round_entry)
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0], {"role": "user", "content": "现在几点？"})
        self.assertIn("[调用工具] run_pipe_command({})", messages[1]["content"])
        self.assertIn("现在是 14:20", messages[1]["content"])
        # 重复文本应该被去重，不出现两次
        self.assertEqual(messages[1]["content"].count("现在是 14:20"), 1)

    def test_format_tool_call_history_line_handles_missing_parts(self):
        self.assertIsNone(format_tool_call_history_line({"function": {}}))
        self.assertIsNone(format_tool_call_history_line(None))
        self.assertEqual(
            format_tool_call_history_line({"function": {"name": "foo"}}),
            "[调用工具] foo({})",
        )

    def test_normalize_context_summary_handles_string_and_dict(self):
        self.assertIsNone(normalize_context_summary(None))
        self.assertIsNone(normalize_context_summary("   "))
        self.assertEqual(normalize_context_summary("简短摘要")["summary"], "简短摘要")
        normalized = normalize_context_summary({
            "content": "历史摘要",  # 兼容旧字段名
            "key_facts": ["a", 1, "  ", "b"],
            "open_items": "一项",
            "tool_state": [],
            "source_round_count": "5",
        })
        self.assertEqual(normalized["summary"], "历史摘要")
        self.assertEqual(normalized["key_facts"], ["a", "1", "b"])
        self.assertEqual(normalized["open_items"], ["一项"])
        self.assertEqual(normalized["tool_state"], [])
        self.assertEqual(normalized["source_round_count"], 5)

    def test_render_context_summary_includes_all_sections(self):
        rendered = render_context_summary({
            "summary": "核心结论",
            "key_facts": ["事实1"],
            "open_items": ["待办1"],
            "tool_state": ["工具运行了"],
        })
        self.assertIn("核心结论", rendered)
        self.assertIn("- 事实1", rendered)
        self.assertIn("- 待办1", rendered)
        self.assertIn("- 工具运行了", rendered)


if __name__ == "__main__":
    unittest.main()