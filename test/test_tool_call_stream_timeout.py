"""工具调用流式阶段超时回归测试。

背景：部分服务商 API 在 SSE 输出 tool_calls 阶段会无限卡住（连接不断、
永无后续事件），任务卡在工具调用卡片上十几分钟无法继续。现按
TOOL_CALL_STREAM_TIMEOUT_SECONDS 对"工具调用阶段事件间隔"计时：
超时不终止任务——本次工具调用以失败结果反馈模型，模型继续运行
（可重试新调用或直接回答）。

覆盖：
- 已知工具：assistant(tool_calls) + tool 失败结果成对落盘，SSE 有
  tool_return(timeout=True) 与 warning 帧，任务继续完成；
- 调用结构不可用（工具名未注册）：不写空 tool_calls 的 assistant 消息，
  以内部提示消息让模型重试，任务继续完成；
- finish_reason 已到达后的尾帧停顿（usage/[DONE] 迟到）：不算工具失败，
  按正常收尾执行已完整的工具调用。
"""
import asyncio
import json
import os
import sys
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import factory.chat_factory as cf
from config import ChatLLMRequest
from memory.chat_memory import (
    ChatMemoryManager,
    cleanup_chat_memory_manager,
)


class StallingToolCallLLM:
    """第一次调用按 mode 制造不同的工具调用阶段卡死；之后正常回答。"""

    calls = 0
    mode = "stall_partial"  # stall_partial / ghost / finish_then_stall
    tool_name = "todo_write"

    @staticmethod
    async def chat_completions(request=None, stream=False, stop_checker=None, **kw):
        StallingToolCallLLM.calls += 1
        if StallingToolCallLLM.calls == 1:
            if StallingToolCallLLM.mode == "finish_then_stall":
                delta = {
                    "tool_calls": [{
                        "index": 0,
                        "id": "call_done_1",
                        "type": "function",
                        "function": {
                            "name": "todo_write",
                            "arguments": json.dumps({
                                "todos": [{"content": "验证尾帧停顿", "status": "pending"}],
                            }, ensure_ascii=False),
                        },
                    }]
                }
                yield f"data: {json.dumps(delta)}\n\n"
                yield f"data: {json.dumps({'finish_reason': 'tool_calls'})}\n\n"
            else:
                delta = {
                    "tool_calls": [{
                        "index": 0,
                        "id": "call_stall_1",
                        "type": "function",
                        "function": {
                            "name": StallingToolCallLLM.tool_name,
                            "arguments": "{\"todos\": [{\"status\": \"pending\"",
                        },
                    }]
                }
                yield f"data: {json.dumps(delta)}\n\n"
            await asyncio.sleep(30)  # 模拟上游卡死（超时后应被 aclose 打断）
            return
        yield f"data: {json.dumps({'content': '超时后模型继续运行'})}\n\n"
        yield f"data: {json.dumps({'finish_reason': 'stop', 'usage': {'total_tokens': 3}})}\n\n"
        yield "data: [DONE]\n\n"


class ToolCallStreamTimeoutTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        cf._CHAT_WORKER_MODE = "inline"
        self._orig_chat = cf.ChatLLM.chat_completions
        self._orig_load_var = cf.load_var
        cf.ChatLLM.chat_completions = StallingToolCallLLM.chat_completions
        orig_load_var = self._orig_load_var

        def fake_load_var(name, default=None):
            if name == "TOOL_CALL_STREAM_TIMEOUT_SECONDS":
                return 0.2
            return orig_load_var(name, default)

        cf.load_var = fake_load_var
        cf._SSE_HEARTBEAT_SECONDS = 0.05
        cf.load_all_tools = lambda: asyncio.sleep(0)
        cf.tool_registry.ALL_TOOLS = []
        cf.tool_registry.TOOL_MCP_SERVERS = {}
        cf.require_default_chat_config = lambda: None
        cf.compact_session_history_if_needed = lambda *a, **k: asyncio.sleep(0)
        StallingToolCallLLM.calls = 0
        StallingToolCallLLM.mode = "stall_partial"
        StallingToolCallLLM.tool_name = "todo_write"

    def tearDown(self):
        cf.ChatLLM.chat_completions = self._orig_chat
        cf.load_var = self._orig_load_var

    async def _consume(self, sid):
        req = ChatLLMRequest(
            session_id=sid,
            messages=[{"role": "user", "content": "hi"}],
            tool_names=["todo_write"],
        )
        chunks = []
        async for chunk in cf.tool_chat_server(req):
            chunks.append(chunk)
        return "".join(chunks)

    async def test_known_tool_timeout_reports_failure_and_continues(self):
        sid = f"mock_tc_timeout_{uuid.uuid4().hex}"
        try:
            text = await self._consume(sid)
            # 不终止任务：模型被再次调用并正常收尾
            self.assertIn("超时后模型继续运行", text)
            self.assertIn("[DONE]", text)
            self.assertGreaterEqual(StallingToolCallLLM.calls, 2)
            # 超时失败以工具结果形式反馈（tool_return + warning 帧）
            self.assertIn('"timeout": true', text)
            self.assertIn("TOOL_CALL_STREAM_TIMEOUT", text)
            self.assertIn("工具调用失败", text)
        finally:
            await cleanup_chat_memory_manager(sid)
            ChatMemoryManager.delete_chat_session_file(sid)

    async def test_unusable_tool_call_timeout_keeps_task_running(self):
        StallingToolCallLLM.tool_name = "ghost_tool_not_registered"
        sid = f"mock_tc_timeout_{uuid.uuid4().hex}"
        try:
            text = await self._consume(sid)
            self.assertIn("超时后模型继续运行", text)
            self.assertIn("[DONE]", text)
            self.assertIn("TOOL_CALL_STREAM_TIMEOUT", text)
            # 调用结构不可用：不产生 tool_return（没有可落盘的工具调用契约）
            self.assertNotIn('"timeout": true', text)
        finally:
            await cleanup_chat_memory_manager(sid)
            ChatMemoryManager.delete_chat_session_file(sid)

    async def test_tail_stall_after_finish_is_not_tool_failure(self):
        # finish_reason 已到达：尾部（usage/[DONE]）停顿按正常收尾，
        # 已完整的工具调用应正常执行而不是按超时失败处理
        StallingToolCallLLM.mode = "finish_then_stall"
        sid = f"mock_tc_timeout_{uuid.uuid4().hex}"
        try:
            text = await self._consume(sid)
            self.assertNotIn("TOOL_CALL_STREAM_TIMEOUT", text)
            self.assertNotIn('"timeout": true', text)
            self.assertIn("超时后模型继续运行", text)
            self.assertIn("[DONE]", text)
            self.assertGreaterEqual(StallingToolCallLLM.calls, 2)
        finally:
            await cleanup_chat_memory_manager(sid)
            ChatMemoryManager.delete_chat_session_file(sid)


if __name__ == "__main__":
    unittest.main()
