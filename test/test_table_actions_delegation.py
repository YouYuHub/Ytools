"""md 表格复制/更多按钮：事件委托必须挂在 document，覆盖非聊天区渲染。

回归背景：表格按钮由 Markdown.render 无条件输出，Skills 提示词预览、
任务模板预览等模态框同样使用该渲染器。事件委托原先只挂在 chatInner，
导致这些区域「hover 能看到复制/更多按钮，点击毫无反应」。
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = (ROOT / "H5" / "src" / "app" / "messages_tables.ts").read_text(encoding="utf-8")


class TableActionsDelegationTests(unittest.TestCase):
    def test_delegation_bound_to_document_not_chat_inner(self):
        self.assertRegex(
            SRC, r'document\.addEventListener\(\s*"click",\s*async',
            "表格操作按钮必须委托到 document，否则模态框内的表格按钮点不动",
        )
        self.assertNotRegex(
            SRC, r"chatInner\.addEventListener\(\s*\"click\"",
            "不要再把表格按钮委托挂在 chatInner 上",
        )

    def test_all_table_actions_still_handled(self):
        for action in ("copy-md", "download-xlsx", "copy-image"):
            self.assertIn(action, SRC, f"菜单项 {action} 的处理分支不能丢")
        self.assertRegex(SRC, r'tableAction\s*===\s*["\']more["\']', "「更多」展开/收起分支不能丢")

    def test_guards_against_non_element_target(self):
        # document 级监听会遇到 Text/document 等非 Element target
        self.assertIn("closest ?", SRC, "需要对 e.target 做 Element 归一，避免 closest 抛错")


class MarkdownEmitsActionsTests(unittest.TestCase):
    def test_renderer_always_emits_actions(self):
        md = (ROOT / "H5" / "src" / "markdown.ts").read_text(encoding="utf-8")
        self.assertIn("buildTableActions(lines.join", md, "表格渲染应始终带操作按钮")
        # 反向确认：没有「只在聊天区才输出按钮」的开关（那正是历史 bug 的形态）
        self.assertNotRegex(
            md, r"buildTableActions\([^)]*\)\s*[+?]*\s*\(",
            "操作按钮不应按上下文条件输出",
        )


if __name__ == "__main__":
    unittest.main()
