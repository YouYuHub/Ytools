"""内置 todo_write 工具（模型自我规划）测试。

覆盖扩展后的校验语义：
- 硬性错误整体拒绝，并返回带条目序号的纠错原因；
- id 缺失 → 继承上一版计划或本地自增分配；id 重复 → 重新分配；
- 多个 in_progress → 仅保留首个，其余降级 pending；
- 工具结果回写 current plan state / todos / warnings。
"""
import asyncio
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from factory.agent_runtime import builtin_tools
from memory.chat_memory import ChatMemoryManager

TEST_SESSION = "todo_ut_session"


def _cleanup():
    root = Path(__file__).resolve().parents[1] / "history_files"
    chat = root / f"{TEST_SESSION}_chat.jsonl"
    if chat.exists():
        chat.unlink()


class TodoToolDefinitionTests(unittest.TestCase):
    def test_definition_declares_id(self):
        params = builtin_tools.TODO_TOOL_DEFINITION["function"]["parameters"]
        items = params["properties"]["todos"]["items"]
        self.assertEqual(items["required"], ["id", "content", "status"])
        self.assertEqual(
            params["properties"]["todos"]["items"]["properties"]["status"]["enum"],
            ["pending", "in_progress", "done"],
        )
        self.assertEqual(params["properties"]["todos"].get("maxItems"), 20)

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

    def test_normalize_items_without_id(self):
        items, notices, error = builtin_tools.normalize_todo_items(
            [{"content": "读取配置", "status": "done"},
             {"content": "执行分析", "status": "in_progress"}]
        )
        self.assertIsNone(error)
        self.assertEqual(items, [
            {"id": "1", "content": "读取配置", "status": "done"},
            {"id": "2", "content": "执行分析", "status": "in_progress"},
        ])
        self.assertTrue(any("2 个步骤未携带 id" in n for n in notices))

    def test_normalize_items_inherit_prev_id(self):
        prev = [{"id": "7", "content": "分析代码", "status": "pending"}]
        items, notices, error = builtin_tools.normalize_todo_items(
            [{"content": "分析代码", "status": "pending"},
             {"content": "运行测试"}],
            prev_items=prev,
        )
        self.assertIsNone(error)
        # 同 content+status 步骤继承原 id；新步骤分配未占用 id
        self.assertEqual(items, [
            {"id": "7", "content": "分析代码", "status": "pending"},
            {"id": "1", "content": "运行测试", "status": "pending"},
        ])

    def test_normalize_items_duplicated_id(self):
        items, notices, error = builtin_tools.normalize_todo_items([
            {"id": "1", "content": "步骤一", "status": "pending"},
            {"id": "1", "content": "步骤二", "status": "pending"},
        ])
        self.assertIsNone(error)
        self.assertEqual([item["id"] for item in items], ["1", "2"])
        self.assertTrue(any("id 重复" in n for n in notices))

    def test_normalize_items_multiple_in_progress(self):
        items, notices, error = builtin_tools.normalize_todo_items([
            {"id": "1", "content": "第一步", "status": "in_progress"},
            {"id": "2", "content": "第二步", "status": "in_progress"},
            {"id": "3", "content": "第三步", "status": "pending"},
        ])
        self.assertIsNone(error)
        self.assertEqual(
            [item["status"] for item in items],
            ["in_progress", "pending", "pending"],
        )
        self.assertTrue(any("in_progress" in n for n in notices))

    def test_normalize_items_hard_errors(self):
        normalize = builtin_tools.normalize_todo_items
        items, notices, error = normalize("not-a-list")
        self.assertIsNone(items)
        self.assertIn("对象数组", error)
        items, notices, error = normalize([{"content": "x", "status": "weird"}])
        self.assertIsNone(items)
        self.assertIn("第 1 项 status 非法", error)
        items, notices, error = normalize(
            [{"content": "x" * 201, "status": "pending"}])
        self.assertIsNone(items)
        self.assertIn("content 超长", error)
        items, notices, error = normalize([{"status": "done"}])
        self.assertIsNone(items)
        self.assertIn("content 不能为空", error)
        items, notices, error = normalize(["step"])
        self.assertIsNone(items)
        self.assertIn("第 1 项必须是对象", error)
        items, notices, error = normalize(
            [{"content": str(i), "status": "pending"} for i in range(21)])
        self.assertIsNone(items)
        self.assertIn("超过上限", error)


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
                {"id": "1", "content": "读取配置", "status": "done"},
                {"id": "2", "content": "执行分析", "status": "in_progress"},
            ]},
        ))
        self.assertIsNotNone(items)
        self.assertEqual(result["current plan state"],
                         {"total": 2, "done": 1, "in_progress": 1, "pending": 0})
        self.assertEqual([i["id"] for i in result["todos"]], ["1", "2"])
        self.assertNotIn("warnings", result)  # 提交合规时无告警
        todo = asyncio.run(self.manager.get_session_todo())
        self.assertEqual([item["content"] for item in todo], ["读取配置", "执行分析"])
        self.assertEqual([item["id"] for item in todo], ["1", "2"])
        # 落盘到 _meta.todo
        import memory.chat_memory as cm
        with self.manager._lock:
            meta, _ = cm._load_meta_and_entries(self.manager._file_path, TEST_SESSION)
        self.assertEqual(meta["todo"][0]["status"], "done")

    def test_apply_session_todo_missing_id_inherits_prev(self):
        from factory import chat_factory

        # 第一版：模型提交 id，落盘 _meta.todo
        asyncio.run(chat_factory._apply_session_todo(
            self.manager,
            {"todos": [
                {"id": "4", "content": "读取配置", "status": "done"},
                {"id": "9", "content": "执行分析", "status": "in_progress"},
            ]},
        ))
        # 第二版：模型偷懒不带 id → 同 content+status 继承原 id
        result, items = asyncio.run(chat_factory._apply_session_todo(
            self.manager,
            {"todos": [
                {"content": "读取配置", "status": "done"},
                {"content": "写入报告", "status": "pending"},
            ]},
        ))
        self.assertEqual([item["id"] for item in items], ["4", "1"])
        self.assertTrue(any("自动分配/继承" in w for w in result["warnings"]))
        todo = asyncio.run(self.manager.get_session_todo())
        self.assertEqual([item["id"] for item in todo], ["4", "1"])

    def test_apply_session_todo_invalid_args(self):
        from factory import chat_factory

        result, items = asyncio.run(chat_factory._apply_session_todo(
            self.manager, {"todos": "bad"}))
        self.assertIsNone(items)
        self.assertIn("todos 需要是对象数组", result["error"])


if __name__ == "__main__":
    unittest.main()
