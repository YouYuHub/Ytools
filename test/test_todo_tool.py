"""内置 todo_write 工具（模型自我规划）测试。"""
import asyncio
import json
import os
import unittest
from pathlib import Path

from factory.agent_runtime import builtin_tools
from memory.chat_memory import ChatMemoryManager

TEST_SESSION = "todo_ut_session"


def _cleanup():
    root = Path(__file__).resolve().parents[1] / "history_files"
    chat = root / f"{TEST_SESSION}_chat.jsonl"
    if chat.exists():
        chat.unlink()


class TodoToolDefinitionTests(unittest.TestCase):
    def test_is_builtin_tool_covers_todo(self):
        self.assertTrue(builtin_tools.is_builtin_tool("todo_write"))
        self.assertTrue(builtin_tools.is_builtin_tool("check_tool_exists"))
        self.assertFalse(builtin_tools.is_builtin_tool("list_dir_item"))

    def test_inject_todo_only_when_enabled(self):
        tools, servers = builtin_tools.inject_builtin_tools([], {}, include_todo=True)
        names = {t["function"]["name"] for t in tools}
        self.assertIn("todo_write", names)
        self.assertNotIn("check_tool_exists", names)  # 未选外部工具时不注入
        self.assertEqual(servers["todo_write"], "__builtin__")

        tools2, servers2 = builtin_tools.inject_builtin_tools([], {}, include_todo=False)
        self.assertEqual(tools2, [])
        self.assertNotIn("todo_write", servers2)

    def test_normalize_todo_items(self):
        valid = [
            {"content": "读取文件", "status": "done"},
            {"content": "分析内容", "status": "in_progress"},
            {"content": "输出结论", "status": "pending"},
        ]
        self.assertEqual(builtin_tools.normalize_todo_items(valid), valid)
        self.assertEqual(builtin_tools.normalize_todo_items([{"content": "只有内容"}]),
                         [{"content": "只有内容", "status": "pending"}])
        self.assertIsNone(builtin_tools.normalize_todo_items("not-a-list"))
        self.assertIsNone(builtin_tools.normalize_todo_items([{"status": "done"}]))
        self.assertIsNone(builtin_tools.normalize_todo_items([{"content": "x", "status": "weird"}]))
        self.assertIsNone(builtin_tools.normalize_todo_items([{"content": "", "status": "pending"}]))
        self.assertIsNone(builtin_tools.normalize_todo_items(
            [{"content": "x" * 201, "status": "pending"}]))
        self.assertIsNone(builtin_tools.normalize_todo_items(
            [{"content": str(i), "status": "pending"} for i in range(21)]))


class TodoApplyTests(unittest.TestCase):
    def setUp(self):
        _cleanup()
        self.manager = ChatMemoryManager(TEST_SESSION)

    def tearDown(self):
        _cleanup()

    def test_apply_session_todo_persists_and_reports(self):
        from factory import chat_factory

        result, items = asyncio.run(chat_factory._apply_session_todo(
            self.manager,
            {"todos": [
                {"content": "读取配置", "status": "done"},
                {"content": "执行分析", "status": "in_progress"},
            ]},
        ))
        self.assertIsNotNone(items)
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["done"], 1)
        todo = asyncio.run(self.manager.get_session_todo())
        self.assertEqual([item["content"] for item in todo], ["读取配置", "执行分析"])
        # 落盘到 _meta.todo
        import memory.chat_memory as cm
        with self.manager._lock:
            meta, _ = cm._load_meta_and_entries(self.manager._file_path, TEST_SESSION)
        self.assertEqual(meta["todo"][0]["status"], "done")

    def test_apply_session_todo_invalid_args(self):
        from factory import chat_factory

        result, items = asyncio.run(chat_factory._apply_session_todo(self.manager, {"todos": "bad"}))
        self.assertIsNone(items)
        self.assertIn("todos 参数无效", result["error"])


if __name__ == "__main__":
    unittest.main()
