"""内置 todo_write 工具（模型自我规划）测试。

覆盖扩展后的校验语义：
- 硬性错误整体拒绝，并返回带条目序号的纠错原因；
- id 缺失 → 继承上一版计划或本地自增分配；id 重复 → 重新分配；
- 多个 in_progress → 仅保留首个，其余降级 pending；
- 工具结果回写 message（自带进度统计）/ todos / warnings。
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
    for suffix in ("", ".pending", ".todo"):
        # 侧车统一在 sidecars/ 子目录（与旧版同目录位置都清，防历史残留）
        for base in (root, root / "sidecars"):
            chat = base / f"{TEST_SESSION}_chat.jsonl{suffix}"
            if chat.exists():
                try:
                    chat.unlink()
                except OSError:
                    pass


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
        # 返回已精简：message 自带进度统计（不再有独立的 current plan state 块）
        self.assertNotIn("current plan state", result)
        self.assertEqual([i["id"] for i in result["todos"]], ["1", "2"])
        # 三态文案：首次创建走「已创建」，非完成态 plan_complete=False
        self.assertIn("任务计划已创建", result["message"])
        self.assertFalse(result["plan_complete"])
        self.assertNotIn("warnings", result)  # 提交合规时无告警
        todo = asyncio.run(self.manager.get_session_todo())
        self.assertEqual([item["content"] for item in todo], ["读取配置", "执行分析"])
        self.assertEqual([item["id"] for item in todo], ["1", "2"])
        # 落盘到侧车文件（todo 真源：<session>_chat.jsonl.todo）
        import memory.chat_memory as cm
        with self.manager._lock:
            sidecar = self.manager._read_todo_sidecar()
        self.assertEqual(sidecar[0]["status"], "done")
        self.assertTrue(self.manager._todo_sidecar_path.exists())

    def test_apply_session_todo_update_and_complete_states(self):
        from factory import chat_factory

        # 首次提交：建立计划（铺垫 prev_items）
        asyncio.run(chat_factory._apply_session_todo(
            self.manager,
            {"todos": [
                {"id": "1", "content": "读取配置", "status": "in_progress"},
                {"id": "2", "content": "执行分析", "status": "pending"},
            ]},
        ))
        # 第二次提交：非全完成 → 「已更新」
        result, items = asyncio.run(chat_factory._apply_session_todo(
            self.manager,
            {"todos": [
                {"id": "1", "content": "读取配置", "status": "done"},
                {"id": "2", "content": "执行分析", "status": "in_progress"},
            ]},
        ))
        self.assertIn("任务计划已更新", result["message"])
        self.assertFalse(result["plan_complete"])

        # 全部完成 → 「全部完成」提示 + plan_complete=True
        result_all_done, items_all_done = asyncio.run(chat_factory._apply_session_todo(
            self.manager,
            {"todos": [
                {"id": "1", "content": "读取配置", "status": "done"},
                {"id": "2", "content": "执行分析", "status": "done"},
            ]},
        ))
        self.assertIn("任务规划全部完成", result_all_done["message"])
        self.assertIn("汇总执行结果", result_all_done["message"])
        self.assertTrue(result_all_done["plan_complete"])
        self.assertEqual(items_all_done[1]["status"], "done")

    def test_replace_todo_context_summary_terminal_state(self):
        # 摘要终态回归：模型最后一次 todo 调用（全部 done）后，历史 todo
        # 调用/结果被替换为摘要——终态摘要必须自带"全部完成+请汇总答复"
        # 收官指令，否则下一轮摘要只剩一列 ✓，模型无从判断计划已结束
        from factory import chat_factory

        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "tool_calls": [{
                "id": "t1", "function": {"name": "todo_write", "arguments": "{}"}
            }]},
            {"role": "tool", "tool_call_id": "t1", "_tool_name": "todo_write", "content": "ok"},
            {"role": "assistant", "content": "任务执行完毕"},
        ]
        chat_factory._replace_todo_context(messages, [
            {"content": "准备测试", "status": "done"},
            {"content": "汇总结果", "status": "done"},
        ])
        # todo 调用与结果被替换，摘要追加在消息末尾（末尾指令靠近最新上下文）
        self.assertEqual(len(messages), 3)
        self.assertEqual(messages[1]["content"], "任务执行完毕")
        summary = messages[2]["content"]
        self.assertIn("任务规划已全部完成（2/2 项）", summary)
        self.assertIn("汇总执行结果直接答复用户", summary)
        self.assertIn("✓ 汇总结果", summary)

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

    def test_todo_sidecar_survives_meta_rewrites(self):
        """竞态回归：轮次收尾/压缩全量重写 _meta 后，todo 不得退回旧快照。

        背景：todo 曾存于 _meta，而 _meta 由轮次收尾/压缩/标题等多条写路径
        全量重写（主进程与 worker 多进程并发），后写者用旧快照覆盖导致模型
        已收到 plan_complete、下一轮系统提示却仍是中间态 → 重复调用 todo_write。
        侧车化后只有 update_session_todo 一个写方，免疫此类覆盖。
        """
        import memory.chat_memory as cm

        v_done = [{"id": "1", "content": "步骤一", "status": "done"},
                  {"id": "2", "content": "步骤二", "status": "done"}]
        asyncio.run(self.manager.update_session_todo(v_done))
        # 模拟其他写路径的全量重写（读到的 base_meta 无 todo 字段）
        with self.manager._write_guard():
            meta, entries = cm._load_meta_and_entries(self.manager._file_path, TEST_SESSION)
            meta.pop("todo", None)
            cm._write_meta_and_entries(self.manager._file_path, meta, entries)
        todo = asyncio.run(self.manager.get_session_todo())
        self.assertEqual([t["status"] for t in todo], ["done", "done"])
        # 前端数据源（meta 合并）同样拿到侧车新值
        merged = asyncio.run(self.manager.get_session_meta())
        self.assertEqual([t["status"] for t in merged["todo"]], ["done", "done"])

    def test_todo_sidecar_fallback_to_meta_for_legacy_sessions(self):
        """兼容迁移：旧会话只有 _meta.todo（无侧车）时读取回退且不报错。"""
        import memory.chat_memory as cm

        legacy = [{"id": "1", "content": "旧计划", "status": "done"}]
        with self.manager._write_guard():
            meta, entries = cm._load_meta_and_entries(self.manager._file_path, TEST_SESSION)
            meta["todo"] = legacy
            cm._write_meta_and_entries(self.manager._file_path, meta, entries)
        self.assertFalse(self.manager._todo_sidecar_path.exists())
        todo = asyncio.run(self.manager.get_session_todo())
        self.assertEqual(todo, legacy)  # 回退读到旧值，侧车仍未创建
        # 首次 update 后侧车成为唯一真源
        asyncio.run(self.manager.update_session_todo(
            [{"id": "1", "content": "新计划", "status": "in_progress"}]))
        todo2 = asyncio.run(self.manager.get_session_todo())
        self.assertEqual(todo2[0]["content"], "新计划")

    def test_replace_todo_context_no_dangling_tool_call(self):
        """悬空回归（400 空响应体根因）：归并 todo 时必须同步清除 tool_calls。

        历史 bug：assistant（正文 + 单独一个 todo_write 调用）被归并时只删除
        配对 tool 结果、保留 tool_calls 声明 → 悬空调用 → 上游严格校验
        返回 400 空响应体，重试同一 payload 必然全败（每次 todo 收官后必现）。
        """
        from factory import chat_factory

        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "我先更新计划。", "tool_calls": [
                {"id": "call_todo_1", "type": "function",
                 "function": {"name": "todo_write", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "call_todo_1",
             "_tool_name": "todo_write", "content": "ok"},
            {"role": "assistant", "content": "继续执行"},
        ]
        chat_factory._replace_todo_context(messages, [
            {"content": "步骤一", "status": "done"},
        ])
        # 全上下文无悬空：每个声明的 id 都有配对结果（或声明已清除）
        pending = []
        for message in messages:
            if message.get("role") == "assistant" and message.get("tool_calls"):
                pending.extend(tc.get("id") for tc in message["tool_calls"])
            elif message.get("role") == "tool":
                if message.get("tool_call_id") in pending:
                    pending.remove(message["tool_call_id"])
        self.assertEqual(pending, [])
        # 正文保留、声明清除
        self.assertEqual(messages[1]["content"], "我先更新计划。")
        self.assertNotIn("tool_calls", messages[1])


if __name__ == "__main__":
    unittest.main()
