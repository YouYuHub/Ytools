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
        # 超时说明应作为编号注意事项出现（模型可按序读取）；
        # 序号不写死：notes 列表新增条目时序号会后移，只断言"N、MCP 工具单次执行超时"形态
        prompt = self._prompt(120)
        self.assertRegex(prompt, r"\d+、MCP 工具单次执行超时为 120 秒")


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


class ReadMediaNoteTests(unittest.TestCase):
    """read_media 使用指引的条件注入：提示词提及的工具必须与请求 tools 一致。

    核心回归：用户未选择（或后端未注入）read_media 时，系统提示词不得
    提及该工具——"如果可用"式模糊措辞会诱导模型凭空编造工具调用。
    """

    def _sys_prompt(self, *, vision: bool, note: bool) -> str:
        # 显式钉住视觉判定，避免受本机 .env 会话模型配置影响
        original = sp._load_chat_vision_enabled
        sp._load_chat_vision_enabled = lambda: vision
        try:
            return sp.build_sys_prompt(include_read_media_note=note)
        finally:
            sp._load_chat_vision_enabled = original

    def test_note_present_when_tool_injected(self):
        prompt = self._sys_prompt(vision=True, note=True)
        self.assertIn("用 read_media 传入对应 media:// 引用", prompt)

    def test_note_absent_by_default(self):
        # 默认 False：调用方未确认注入时绝不提及（本 bug 的核心回归点）
        prompt = self._sys_prompt(vision=True, note=False)
        self.assertNotIn("read_media", prompt)

    def test_note_absent_without_vision_even_if_selected(self):
        # 视觉能力分叉优先于勾选：不支持视觉的模型永远看不到该指引
        prompt = self._sys_prompt(vision=False, note=True)
        self.assertNotIn("read_media", prompt)
        self.assertIn("不支持视觉", prompt)

    def test_runtime_text_propagates_flag(self):
        original = sp._load_chat_vision_enabled
        sp._load_chat_vision_enabled = lambda: True
        try:
            with_note = sp.build_runtime_system_text("C:/tmp/demo", include_read_media_note=True)
            without_note = sp.build_runtime_system_text("C:/tmp/demo")
        finally:
            sp._load_chat_vision_enabled = original
        self.assertIn("用 read_media 传入对应 media:// 引用", with_note)
        self.assertNotIn("read_media", without_note)


if __name__ == "__main__":
    unittest.main()
