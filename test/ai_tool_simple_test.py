# coding: utf-8
"""内置工具定义格式校验（对齐主程序 factory/agent_runtime/builtin_tools.py）。

背景：本文件原为早期 `tool_decorator`（ai_tool 装饰器）实验的演示脚本；该模块
未进入主程序（工具的 OpenAI function-calling schema 现由内置工具定义与 MCP
服务提供），且会导致 pytest 收集失败。按「以主程序为准」原则改写为对内置工具
定义的统一校验：

- 全部 *_DEFINITION 与动态构建的 read_media 定义均为合法 function-calling 格式
  （type=function、name 非空、description 非空、parameters 为 object、
  required ⊆ properties、每个属性均带 description）；
- 工具名唯一（ASK_USER_PLACEHOLDER_DEFINITION 为子智能体占位定义，同名不计重复）；
- SELECTABLE_BUILTIN_TOOL_NAMES 中的可勾选工具都有对应定义；
- is_builtin_tool 覆盖全部定义。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from factory.agent_runtime import builtin_tools as bt


def _collect_tool_definitions() -> dict:
    """收集模块内全部工具定义：*_DEFINITION 常量 + read_media 动态构建。"""
    collected = {}
    for const_name, value in vars(bt).items():
        if const_name.endswith("_DEFINITION") and isinstance(value, dict) and "function" in value:
            collected[const_name] = value
    collected["build_read_media_tool_definition()"] = bt.build_read_media_tool_definition()
    return collected


class BuiltinToolDefinitionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.definitions = _collect_tool_definitions()

    def test_definitions_are_openai_function_format(self):
        self.assertTrue(self.definitions, "未收集到任何工具定义")
        for const_name, definition in self.definitions.items():
            with self.subTest(definition=const_name):
                self.assertEqual(definition.get("type"), "function")
                function = definition.get("function") or {}
                name = function.get("name")
                self.assertIsInstance(name, str)
                self.assertTrue(name.strip(), f"{const_name} 工具名为空")
                self.assertTrue(
                    str(function.get("description") or "").strip(),
                    f"{const_name} 缺少 description",
                )
                parameters = function.get("parameters") or {}
                self.assertEqual(parameters.get("type"), "object")
                properties = parameters.get("properties") or {}
                self.assertIsInstance(properties, dict)
                for key in parameters.get("required") or []:
                    self.assertIn(key, properties, msg=f"{const_name}: required 含未声明属性 {key}")
                for prop_name, prop in properties.items():
                    self.assertTrue(
                        str((prop or {}).get("description") or "").strip(),
                        msg=f"{const_name}.{prop_name} 缺少 description",
                    )

    def test_tool_names_unique(self):
        names = [
            definition["function"]["name"]
            for const_name, definition in self.definitions.items()
            if "PLACEHOLDER" not in const_name
        ]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        self.assertEqual(duplicates, [], msg=f"工具名重复: {duplicates}")

    def test_selectable_names_have_definitions(self):
        defined = {definition["function"]["name"] for definition in self.definitions.values()}
        missing = [name for name in bt.SELECTABLE_BUILTIN_TOOL_NAMES if name not in defined]
        self.assertEqual(missing, [], msg=f"可勾选工具缺少定义: {missing}")

    def test_is_builtin_tool_covers_definitions(self):
        for const_name, definition in self.definitions.items():
            name = definition["function"]["name"]
            self.assertTrue(bt.is_builtin_tool(name), msg=f"is_builtin_tool 未覆盖 {name}（{const_name}）")


if __name__ == "__main__":
    unittest.main()
