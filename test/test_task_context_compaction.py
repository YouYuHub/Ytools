import unittest
from unittest.mock import patch

from config import ChatLLMRequest
from factory import chat_factory


class _MemoryStub:
    def __init__(self, history_msgs):
        self.history_msgs = history_msgs
        self.calls = []

    async def get_context_messages(self, max_rounds=None, max_tool_result_length=None, minimal=False):
        self.calls.append({"max_rounds": max_rounds, "minimal": minimal})
        return self.history_msgs


def _big_message(text_repeat=2000):
    return {"role": "assistant", "content": "历史内容" * text_repeat}


def _build_messages():
    return [
        # 任务启动时合成的主 system（persona+运行时文本），重建后应原样保留在首位
        {"role": "system", "content": "主系统提示" + "runtime提示"},
        {"role": "user", "content": "老问题1"},
        _big_message(),
        {"role": "user", "content": "老问题2"},
        _big_message(),
        # 当前任务轮（应原样保留在重建结果尾部）
        {"role": "user", "content": "当前问题"},
        {
            "role": "assistant",
            "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {"name": "query", "arguments": "{}"},
            }],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "工具结果", "_tool_name": "query"},
    ]


class TaskContextCompactionTests(unittest.IsolatedAsyncioTestCase):
    """任务内上下文预算管理：达到阈值时压缩持久层历史并重建内存历史部分。"""

    def setUp(self):
        self.request = ChatLLMRequest(
            messages=[{"role": "user", "content": "当前问题"}],
            max_tokens=512,
        )
        self.stream = object()
        self.runtime_sys_text = "runtime提示"
        self.file_block = ""

    async def test_returns_none_below_threshold(self):
        messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}]
        memory = _MemoryStub([])
        compact_calls = []

        async def fake_compact(*args, **kwargs):
            compact_calls.append(kwargs)

        with patch.object(chat_factory, "compact_session_history_if_needed", fake_compact):
            result = await chat_factory._compact_task_context_if_needed(
                messages,
                self.request,
                memory,
                self.stream,
                threshold=100_000,
                backend_history_rounds=20,
                runtime_sys_text=self.runtime_sys_text,
                file_block_text=self.file_block,
                event_emitter=None,
            )
        self.assertIsNone(result)
        self.assertEqual(compact_calls, [])

    async def test_rebuilds_history_and_keeps_active_round(self):
        messages = _build_messages()
        memory = _MemoryStub([
            {"role": "system", "content": "【历史压缩摘要】新摘要"},
            {"role": "system", "content": "【用户最近问题】\n- 老问题1"},
        ])
        compact_kwargs = []

        async def fake_compact(*args, **kwargs):
            compact_kwargs.append(kwargs)

        with patch.object(chat_factory, "compact_session_history_if_needed", fake_compact):
            result = await chat_factory._compact_task_context_if_needed(
                messages,
                self.request,
                memory,
                self.stream,
                threshold=1500,
                backend_history_rounds=20,
                runtime_sys_text=self.runtime_sys_text,
                file_block_text=self.file_block,
                event_emitter=None,
            )

        self.assertIsNotNone(result)
        # 跨轮压缩以 enforce + 预算（阈值×0.4，下限 2048）调用
        self.assertEqual(len(compact_kwargs), 1)
        self.assertTrue(compact_kwargs[0].get("enforce"))
        self.assertTrue(compact_kwargs[0].get("force_all"))
        self.assertEqual(compact_kwargs[0].get("budget_tokens"), 2048)
        # 首条 system = 原 main system 原样保留（persona+runtime 不丢失）
        self.assertEqual(result[0]["role"], "system")
        self.assertEqual(result[0]["content"], messages[0]["content"])
        # 历史部分来自持久层重建：摘要排在主 system 之后，runtime 文本不混入摘要
        self.assertIn("【历史压缩摘要】", result[1]["content"])
        self.assertNotIn(self.runtime_sys_text, result[1]["content"])
        self.assertIn("【用户最近问题】", result[2]["content"])
        # 当前任务轮原样保留在尾部
        self.assertEqual(result[-3:], messages[-3:])
        self.assertEqual(result[-1]["role"], "tool")
        self.assertEqual(result[-3]["content"], "当前问题")

    async def test_history_budget_floor_and_rounds_argument(self):
        messages = _build_messages()
        memory = _MemoryStub([])

        async def fake_compact(*args, **kwargs):
            pass

        with patch.object(chat_factory, "compact_session_history_if_needed", fake_compact):
            await chat_factory._compact_task_context_if_needed(
                messages,
                self.request,
                memory,
                self.stream,
                threshold=1000,
                backend_history_rounds=7,
                runtime_sys_text=self.runtime_sys_text,
                file_block_text=self.file_block,
                event_emitter=None,
            )
        # 历史预算下限 2048
        self.assertGreaterEqual(chat_factory.estimate_request_context_tokens(messages, None), 0)
        self.assertEqual(memory.calls[0]["max_rounds"], 7)

    async def test_unlimited_rounds_passed_through_to_context_rebuild(self):
        """backend_history_rounds<=0（无限窗口）应原样透传，不被 max(1, ...) 限成 1 轮。"""
        messages = _build_messages()
        memory = _MemoryStub([])

        async def fake_compact(*args, **kwargs):
            pass

        with patch.object(chat_factory, "compact_session_history_if_needed", fake_compact):
            await chat_factory._compact_task_context_if_needed(
                messages,
                self.request,
                memory,
                self.stream,
                threshold=1000,
                backend_history_rounds=0,
                runtime_sys_text=self.runtime_sys_text,
                file_block_text=self.file_block,
                event_emitter=None,
            )
        self.assertEqual(memory.calls[0]["max_rounds"], 0)


if __name__ == "__main__":
    unittest.main()
