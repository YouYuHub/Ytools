"""check_tool_exists 内置工具的可用性语义测试。

核心场景：用户禁用某工具后，后端注册表仍包含它（存在 ≠ 可用）。
模型使用失败后调用 check_tool_exists 时，必须明确告知"该工具未被当前
轮次任务启用"，而不是返回 exists=True 造成"工具可用"的误解。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from factory.agent_runtime import builtin_tools


class CheckToolExistsTests(unittest.TestCase):
    def setUp(self):
        # 后端全集：todo_write 已注册但被用户禁用；fetch_url 启用
        self.configured_names = {"todo_write", "fetch_url", "run_command"}
        self.configured_servers = {
            "todo_write": "__builtin__", "fetch_url": "sys", "run_command": "sys",
        }
        self.enabled_names = {"fetch_url"}

    def _execute(self, tool_name, enabled=None):
        return builtin_tools.execute_builtin_tool(
            "check_tool_exists",
            {"tool_name": tool_name},
            self.configured_names,
            self.configured_servers,
            enabled_tool_names=enabled if enabled is not None else self.enabled_names,
        )

    def test_disabled_builtin_tool_reports_disabled(self):
        result = self._execute("todo_write")
        self.assertTrue(result["exists"])
        self.assertTrue(result["disabled"])
        self.assertIn("未被当前轮次任务启用", result["message"])

    def test_disabled_mcp_tool_reports_disabled(self):
        result = self._execute("run_command")
        self.assertTrue(result["exists"])
        self.assertTrue(result["disabled"])

    def test_enabled_tool_reports_available(self):
        result = self._execute("fetch_url")
        self.assertTrue(result["exists"])
        self.assertFalse(result["disabled"])
        self.assertIn("可用", result["message"])

    def test_unknown_tool_reports_missing(self):
        result = self._execute("list_dir")
        self.assertFalse(result["exists"])
        self.assertFalse(result["disabled"])
        self.assertIn("不存在", result["message"])

    def test_legacy_call_without_enabled_set_keeps_old_behavior(self):
        # 不传 enabled_tool_names：视为全部启用（旧调用方兼容，不误报 disabled）
        result = builtin_tools.execute_builtin_tool(
            "check_tool_exists",
            {"tool_name": "todo_write"},
            self.configured_names,
            self.configured_servers,
        )
        self.assertTrue(result["exists"])
        self.assertFalse(result["disabled"])

    def test_definition_mentions_round_scope(self):
        description = builtin_tools.CHECK_TOOL_EXISTS_DEFINITION["function"]["description"]
        self.assertIn("当前轮次任务", description)


if __name__ == "__main__":
    unittest.main()
