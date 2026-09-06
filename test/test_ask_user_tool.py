"""内置 ask_user 工具（向用户提问）测试。"""
import asyncio
import unittest

from factory.agent_runtime import builtin_tools
from memory.chat_memory import ChatMemoryManager, _load_meta_and_entries

TEST_SESSION = "ask_ut_session"


def _cleanup():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "history_files"
    chat = root / f"{TEST_SESSION}_chat.jsonl"
    for suffix in ("", ".pending"):
        target = root / f"{TEST_SESSION}_chat.jsonl{suffix}"
        try:
            target.unlink()
        except OSError:
            pass


class AskUserToolDefinitionTests(unittest.TestCase):
    def test_is_builtin_tool_covers_ask_user(self):
        self.assertTrue(builtin_tools.is_builtin_tool("ask_user"))
        self.assertTrue(builtin_tools.is_builtin_tool("todo_write"))
        self.assertTrue(builtin_tools.is_builtin_tool("check_tool_exists"))
        self.assertFalse(builtin_tools.is_builtin_tool("list_dir"))

    def test_inject_ask_user_only_when_enabled(self):
        tools, servers = builtin_tools.inject_builtin_tools([], {}, include_ask_user=True)
        names = {t["function"]["name"] for t in tools}
        self.assertIn("ask_user", names)
        self.assertNotIn("check_tool_exists", names)  # 未选外部工具时不注入
        self.assertEqual(servers["ask_user"], "__builtin__")

        tools2, _ = builtin_tools.inject_builtin_tools([], {}, include_ask_user=False)
        self.assertEqual(tools2, [])

        # 与 todo 并行注入互不影响
        tools3, servers3 = builtin_tools.inject_builtin_tools(
            [], {}, include_todo=True, include_ask_user=True)
        names3 = {t["function"]["name"] for t in tools3}
        self.assertEqual(names3, {"todo_write", "ask_user"})
        self.assertEqual(servers3["todo_write"], "__builtin__")
        self.assertEqual(servers3["ask_user"], "__builtin__")

    def test_normalize_ask_questions_valid(self):
        raw = [
            {"question": "使用哪个方案？", "options": ["方案A", "方案B", "", "方案A", "x" * 200]},
            {"question": "补充说明？"},
        ]
        normalized = builtin_tools.normalize_ask_questions(raw)
        self.assertEqual(normalized[0]["question"], "使用哪个方案？")
        # 空串被丢弃、重复去重、超长（>100 字符）被丢弃
        self.assertEqual(normalized[0]["options"], ["方案A", "方案B"])
        self.assertEqual(normalized[1], {"question": "补充说明？", "options": [], "multiple": False})

    def test_normalize_ask_questions_multiple_flag(self):
        normalized = builtin_tools.normalize_ask_questions([
            {"question": "喜欢哪些语言？", "options": ["Python", "Go"], "multiple": True},
            {"question": "确认执行？", "multiple": "yes"},   # 非空即视为真
            {"question": "单选题", "multiple": 0},
        ])
        self.assertTrue(normalized[0]["multiple"])
        self.assertTrue(normalized[1]["multiple"])
        self.assertFalse(normalized[2]["multiple"])

    def test_normalize_ask_questions_tolerates_single_object_and_json_string(self):
        # 单个对象自动包一层数组
        self.assertEqual(
            builtin_tools.normalize_ask_questions({"question": "选哪个？", "options": ["A", "B"]}),
            [{"question": "选哪个？", "options": ["A", "B"], "multiple": False}])
        # JSON 字符串自动解析
        self.assertEqual(
            builtin_tools.normalize_ask_questions('[{"question": "确认执行？"}]'),
            [{"question": "确认执行？", "options": [], "multiple": False}])
        # 非法 JSON 字符串仍拒绝
        self.assertIsNone(builtin_tools.normalize_ask_questions("{bad json"))

    def test_normalize_ask_questions_invalid(self):
        self.assertIsNone(builtin_tools.normalize_ask_questions("not-a-list"))
        self.assertIsNone(builtin_tools.normalize_ask_questions([]))
        self.assertIsNone(builtin_tools.normalize_ask_questions([{"options": ["a"]}]))  # 缺 question
        self.assertIsNone(builtin_tools.normalize_ask_questions([{"question": ""}]))
        self.assertIsNone(builtin_tools.normalize_ask_questions([{"question": " "}]))
        self.assertIsNone(builtin_tools.normalize_ask_questions(
            [{"question": "x" * 201}]))
        self.assertIsNone(builtin_tools.normalize_ask_questions(["纯字符串"]))
        self.assertIsNone(builtin_tools.normalize_ask_questions(
            [{"question": str(i)} for i in range(4)]))  # 超过 3 个问题

    def test_normalize_ask_questions_options_truncated_to_six(self):
        raw = [{"question": "选一个", "options": [f"选项{i}" for i in range(10)]}]
        normalized = builtin_tools.normalize_ask_questions(raw)
        self.assertEqual(len(normalized[0]["options"]), 6)


class TruncateForReanswerTests(unittest.TestCase):
    """覆盖式重新回答：truncate_rounds_for_reanswer 各场景。"""

    def setUp(self):
        _cleanup()
        self.manager = ChatMemoryManager(TEST_SESSION)

    def tearDown(self):
        _cleanup()

    def _seed_ask_history(self):
        # 轮1：普通任务（用户消息 → 模型回复 → done）
        yield self.manager.add_chat_history({"role": "user", "content": "帮我规划周末"})
        yield self.manager.add_chat_history({"role": "assistant", "content": "好的"})
        yield self.manager.add_chat_history({"role": "assistant", "done": "[DONE]"})
        # 轮2：ask_user 提问轮（同一轮内：用户消息 → 模型调用 ask_user → 占位结果 → done）
        yield self.manager.add_chat_history({
            "role": "assistant",
            "tool_calls": [{"id": "c1", "type": "function",
                            "function": {"name": "ask_user", "arguments": "{}"}}],
        })
        yield self.manager.add_chat_history({"role": "tool", "tool_call_id": "c1", "content": "waiting_user"})
        yield self.manager.add_chat_history({"role": "assistant", "done": "[DONE]"})

    def _seed(self):
        for coro in self._seed_ask_history():
            asyncio.run(coro)

    def _round_count(self):
        import memory.chat_memory as cm
        with self.manager._lock:
            _, entries = cm._load_meta_and_entries(self.manager._file_path, TEST_SESSION)
        return len(entries)

    def test_no_ask_round_returns_zero(self):
        asyncio.run(self.manager.add_chat_history({"role": "user", "content": "普通消息"}))
        asyncio.run(self.manager.add_chat_history({"role": "assistant", "done": "[DONE]"}))
        self.assertEqual(asyncio.run(self.manager.truncate_rounds_for_reanswer()), 0)
        self.assertEqual(self._round_count(), 1)

    def test_first_answer_no_truncation(self):
        self._seed()
        self.assertEqual(asyncio.run(self.manager.truncate_rounds_for_reanswer()), 0)
        self.assertEqual(self._round_count(), 2)

    def test_reanswer_removes_old_answer_rounds(self):
        self._seed()
        # 旧回答轮（含模型回复）
        asyncio.run(self.manager.add_chat_history(
            {"role": "user", "content": "【回答模型提问】\n1. 用哪个方案？\n   答：B方案"}))
        asyncio.run(self.manager.add_chat_history({"role": "assistant", "content": "已收到：B方案"}))
        asyncio.run(self.manager.add_chat_history({"role": "assistant", "done": "[DONE]"}))
        self.assertEqual(self._round_count(), 3)
        removed = asyncio.run(self.manager.truncate_rounds_for_reanswer())
        self.assertEqual(removed, 1)
        self.assertEqual(self._round_count(), 2)
        # meta 聚合同步重算：user_questions 不再包含旧回答文本
        import memory.chat_memory as cm
        with self.manager._lock:
            meta, entries = cm._load_meta_and_entries(self.manager._file_path, TEST_SESSION)
        self.assertNotIn("【回答模型提问】", "\n".join(meta.get("user_questions", [])))
        self.assertEqual(meta["record_count"], 2)

    def test_protects_normal_task_after_ask(self):
        self._seed()
        # 提问轮之后是普通新任务（非回答格式）→ 不截断
        asyncio.run(self.manager.add_chat_history({"role": "user", "content": "换个话题，聊聊天气"}))
        asyncio.run(self.manager.add_chat_history({"role": "assistant", "done": "[DONE]"}))
        self.assertEqual(asyncio.run(self.manager.truncate_rounds_for_reanswer()), 0)
        self.assertEqual(self._round_count(), 3)


if __name__ == "__main__":
    unittest.main()
