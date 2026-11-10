"""压缩目标语义：target_tokens 是「历史部分预算」，不得被固定开销扣成 2048。

回归背景（实测会话事件）：
  用户设 HISTORY_COMPACT_TARGET_TOKENS=60000，但旧实现把它当「完整请求目标」，
  减去 system 提示词 + 34 个工具定义 + 当前轮上下文 + 摘要预留 + 问题索引后
  变成负数，max(2048, …) 兜底成 2048 → 历史被压到只剩 1~2k tokens，
  而用户设的 60k 完全没生效（事件里反复出现 limit=2048）。

修正后：history_target 直接作为历史预算，只受「动态可用空间」上限约束。
"""
import inspect
import unittest

from factory import chat_factory


class HistoryTargetSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.src = inspect.getsource(chat_factory._compact_task_context_if_needed)

    def test_target_is_not_reduced_by_fixed_overheads(self):
        """目标不能再被 system/tools/当前轮等固定开销减掉。"""
        self.assertNotRegex(
            self.src,
            r"history_target\s*-\s*system_tokens",
            "旧实现用 history_target 减固定开销，会把 60k 扣成负数→2048",
        )
        self.assertNotRegex(
            self.src,
            r"history_target\s*-\s*tools_tokens",
            "同上：不能再从目标里扣工具定义",
        )

    def test_target_used_directly_as_history_budget(self):
        self.assertRegex(
            self.src,
            r"target_history_budget\s*=\s*history_target",
            "目标应当直接作为历史预算",
        )
        self.assertRegex(
            self.src,
            r"history_budget\s*=\s*min\(\s*history_budget\s*,\s*target_history_budget\s*\)",
            "目标只作上限约束（与动态可用空间取小），不能反过来被扣减",
        )

    def test_dynamic_budget_still_capped_by_threshold(self):
        """0（不设目标）时仍走动态可用空间，且不超窗。"""
        self.assertRegex(
            self.src,
            r"threshold\s*-\s*system_tokens\s*-\s*tools_tokens",
            "动态预算必须保持「阈值 − 固定开销」公式",
        )


class RoundCompactionFullRequestTargetTests(unittest.TestCase):
    def test_round_compaction_accepts_full_request_target(self):
        from factory.agent_runtime import context_compaction as cc
        sig = inspect.signature(cc._compact_active_round_local)
        self.assertIn(
            "full_request_target", sig.parameters,
            "单轮压缩需要接收换算好的完整请求目标，避免拿历史预算直接比全量估算",
        )

    def test_full_request_target_overrides_bare_history_target(self):
        from factory.agent_runtime import context_compaction as cc
        src = inspect.getsource(cc._compact_active_round_local)
        self.assertRegex(
            src,
            r"enforce_target and full_request_target is not None and full_request_target > 0",
            "有换算值时应当优先使用它",
        )

    def test_slot_reset_each_task_compaction(self):
        src = inspect.getsource(chat_factory._compact_task_context_if_needed)
        self.assertRegex(
            src,
            r"global _last_task_compaction_target\s*\n\s*_last_task_compaction_target = 0",
            "每次进入任务内压缩都要先重置共享目标槽，避免上一轮残留",
        )

    def test_slot_converted_with_fixed_overheads(self):
        src = inspect.getsource(chat_factory._compact_task_context_if_needed)
        self.assertIn("_last_task_compaction_target = (", src)
        self.assertIn("target_history_budget + system_tokens + tools_tokens", src)


if __name__ == "__main__":
    unittest.main()
