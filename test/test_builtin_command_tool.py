# coding: utf-8
"""内置终端命令工具（run_command）单元测试。

覆盖：
- 工具定义与注入（include_run_command）
- 名称注册（is_builtin_tool / SELECTABLE_BUILTIN_TOOL_NAMES / check_tool_exists）
- execute_run_command / try_execute_builtin_command_tool 基本执行（echo / 退出码 / 参数校验）
- cmd 家族补丁辅助：多行内联代码定位、简单管道过滤器解析、本地 tail/head 兜底
说明：真实命令执行依赖系统 shell（Windows 为 cmd）；非 Windows 环境自动跳过。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from factory.agent_runtime import builtin_tools as bt

IS_WINDOWS = os.name == "nt"


class BuiltinCommandToolTests(unittest.TestCase):
    # ---------- 注入与名称注册 ----------

    def test_inject_run_command(self):
        tools, servers = bt.inject_builtin_tools([], {}, include_run_command=True)
        names = {t["function"]["name"] for t in tools}
        self.assertIn("run_command", names)
        self.assertEqual(servers["run_command"], "__builtin__")
        # 不勾选时不注入
        tools2, _ = bt.inject_builtin_tools([], {})
        self.assertNotIn("run_command", {t["function"]["name"] for t in tools2})

    def test_selectable_names_include_run_command(self):
        self.assertIn("run_command", bt.SELECTABLE_BUILTIN_TOOL_NAMES)
        self.assertTrue(bt.is_builtin_tool("run_command"))
        self.assertFalse(bt.is_builtin_tool("run_command_extra"))

    def test_tool_definition_schema(self):
        definition = bt.RUN_COMMAND_TOOL_DEFINITION["function"]
        self.assertEqual(definition["name"], "run_command")
        params = definition["parameters"]
        self.assertEqual(params["type"], "object")
        self.assertEqual(params["required"], ["command"])
        for key in ("shell", "work_dir", "timeout_seconds", "background"):
            self.assertIn(key, params["properties"])

    def test_check_tool_exists_recognizes_run_command(self):
        result = bt.execute_builtin_tool(
            "check_tool_exists", {"tool_name": "run_command"}, set(), {}
        )
        self.assertTrue(result["exists"])
        self.assertEqual(result["server"], "__builtin__")

    # ---------- 执行入口 ----------

    @unittest.skipUnless(IS_WINDOWS, "真实命令执行用例仅 Windows 验证")
    def test_execute_echo(self):
        result = bt.execute_run_command({"command": "echo builtin_rc_ok"})
        self.assertIsInstance(result, str)
        self.assertIn("exit=0", result)
        self.assertIn("builtin_rc_ok", result)

    @unittest.skipUnless(IS_WINDOWS, "真实命令执行用例仅 Windows 验证")
    def test_execute_exit_code(self):
        result = bt.execute_run_command({"command": "cmd /c exit 7"})
        self.assertIn("exit=7", result)

    def test_execute_empty_command_rejected(self):
        with self.assertRaises(ValueError):
            bt.execute_run_command({"command": "   "})

    def test_try_execute_dispatch(self):
        self.assertIsNone(bt.try_execute_builtin_command_tool("read_file", {}))
        result = bt.try_execute_builtin_command_tool("run_command", {"command": ""})
        self.assertIsInstance(result, dict)
        self.assertIn("error", result)
        self.assertEqual(result["tool"], "run_command")

    # ---------- cmd 家族补丁辅助 ----------

    def test_multiline_inline_code_rewrite(self):
        command = 'python -c "\nimport sys\nprint(sys.version)\n"'
        found = bt._find_inline_code(command)
        self.assertIsNotNone(found)
        self.assertEqual(found["interp"], "python")
        self.assertEqual(found["flag"], "-c")
        # 单行内联代码不改写（交给 shell 原生处理）
        self.assertIsNone(bt._find_inline_code('python -c "print(1)"'))

    def test_simple_filter_parse_and_local_fallback(self):
        self.assertEqual(bt._parse_simple_filter("tail -20"), ("tail", 20))
        self.assertEqual(bt._parse_simple_filter("head -n 5"), ("head", 5))
        self.assertIsNone(bt._parse_simple_filter("tail -f"))
        text = "\n".join(f"line{i}" for i in range(1, 11)) + "\n"
        self.assertEqual(bt._apply_local_line_filter(text, "tail", 2), "line9\nline10\n")
        self.assertEqual(bt._apply_local_line_filter(text, "head", 2), "line1\nline2\n")


if __name__ == "__main__":
    unittest.main()
