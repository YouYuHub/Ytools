"""系统提示词构建模块（factory/system_prompt.py）的单元测试。

覆盖：
- build_runtime_system_text：工作路径行 + 系统提示正文拼接；
- MCP 工具执行超时的可变配置说明：正数秒数分支与 0（不限制）分支；
- 回传长度条件提示（tool_result / reasoning 截断时才出现）；
- chat_factory 的 re-export 兼容（既有导入路径不变）。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from env_manager import init_path, set_env_vars

init_path(".")

from config import DEFAULT_MCP_TOOL_CALL_TIMEOUT_SECONDS  # noqa: E402
from factory import chat_factory as cf  # noqa: E402
from factory import system_prompt as sp  # noqa: E402


def _clear_tool_timeout_env():
    # 把测试写进 .env 的超时清掉（set_env_vars 写 0 即"不限制"，也是合法值；
    # 测试结束统一恢复默认值，避免污染其它用例）
    set_env_vars({"MCP_TOOL_CALL_TIMEOUT_SECONDS": DEFAULT_MCP_TOOL_CALL_TIMEOUT_SECONDS})


class RuntimeSystemTextTests(unittest.TestCase):
    """build_runtime_system_text 的结构。"""

    def test_contains_work_dir_and_prompt(self):
        text = sp.build_runtime_system_text("C:/tmp/demo")
        self.assertIn("当前工作路径为<C:/tmp/demo>", text)
        self.assertIn("当前系统已安装基础 py 环境", text)
        self.assertIn("## 媒体展示（伪标签）", text)

    def test_chat_factory_reexports_runtime_text_builder(self):
        # routers/chat_router 等仍从 chat_factory 导入，re-export 必须同源
        self.assertIs(cf.build_runtime_system_text, sp.build_runtime_system_text)

    def test_text_changes_with_work_dir(self):
        a = sp.build_runtime_system_text("C:/tmp/a")
        b = sp.build_runtime_system_text("C:/tmp/b")
        self.assertIn("<C:/tmp/a>", a)
        self.assertIn("<C:/tmp/b>", b)


class ToolCallTimeoutNoteTests(unittest.TestCase):
    """MCP 工具超时的可变配置说明。"""

    @classmethod
    def tearDownClass(cls):
        _clear_tool_timeout_env()

    def _prompt(self, timeout_seconds):
        set_env_vars({"MCP_TOOL_CALL_TIMEOUT_SECONDS": timeout_seconds})
        return sp.build_sys_prompt()

    def test_positive_timeout_mentioned_in_prompt(self):
        prompt = self._prompt(120)
        self.assertIn("MCP 工具单次执行超时为 120 秒", prompt)
        # 当前措辞：说明超时后果与拆分建议（用户改配置后下一轮生效）
        self.assertIn("超时会被中止并返回错误", prompt)
        self.assertIn("拆分", prompt)

    def test_zero_timeout_unlimited_branch(self):
        prompt = self._prompt(0)
        self.assertIn("MCP 工具执行不限制超时", prompt)
        self.assertNotIn("单次执行超时为", prompt)

    def test_fractional_timeout_formatting(self):
        prompt = self._prompt(7.5)
        self.assertIn("MCP 工具单次执行超时为 7.5 秒", prompt)

    def test_timeout_note_is_numbered(self):
        # 超时说明应作为编号注意事项出现（模型可按序读取）
        prompt = self._prompt(120)
        self.assertRegex(prompt, r"3、MCP 工具单次执行超时为 120 秒")


class ConditionalNoteTests(unittest.TestCase):
    """回传长度类条件提示。"""

    def test_media_prompt_rules(self):
        prompt = sp.build_media_tag_prompt()
        self.assertIn("<image", prompt)
        self.assertIn("media://", prompt)
        self.assertIn("src 使用双引号", prompt)

    def test_sys_prompt_base_lines(self):
        prompt = sp.build_sys_prompt()
        self.assertIn("工具返回 [] 表示空值而不是失败", prompt)
        self.assertIn("思考过程只保留最近一次", prompt)


if __name__ == "__main__":
    unittest.main()
