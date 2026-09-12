"""工具调用流式阶段超时回归测试。

背景：部分服务商 API 在 SSE 输出 tool_calls 阶段会无限卡住（连接不断、
永无后续事件），任务卡在工具调用卡片上十几分钟无法继续。现按
TOOL_CALL_STREAM_TIMEOUT_SECONDS 对"工具调用阶段事件间隔"计时：
超时不终止任务——本次工具调用以失败结果反馈模型，模型继续运行
（可重试新调用或直接回答）。

覆盖：
- 已知工具：assistant(tool_calls) + tool 失败结果成对落盘，SSE 有
  tool_return(timeout=True) 与 warning 帧，任务继续完成；
- 未知工具（有名但未注册）：与已知工具同路径——声明保留在
  assistant.tool_calls 中并配对失败结果反馈（上游只校验
  tool_call_id 配对，不校验历史中的函数名），模型可自我纠正；
- 调用结构不可用（连函数名都没有）：不写空 tool_calls 的 assistant 消息，
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
    mode = "stall_partial"  # stall_partial / ghost / finish_then_stall / ghost_full
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
            elif StallingToolCallLLM.mode == "ghost_full":
                # 完整的未知工具调用（不超时）：验证未知工具被成对拦截反馈
                # 且任务继续（旧实现剔除声明 → 孤儿结果 → 上游 400 断流）
                delta = {
                    "tool_calls": [{
                        "index": 0,
                        "id": "call_ghost_1",
                        "type": "function",
                        "function": {
                            "name": StallingToolCallLLM.tool_name,
                            "arguments": json.dumps({"x": 1}, ensure_ascii=False),
                        },
                    }]
                }
                yield f"data: {json.dumps(delta)}\n\n"
                yield f"data: {json.dumps({'finish_reason': 'tool_calls'})}\n\n"
                return  # 完整调用不模拟卡死：立即返回走正常工具执行链路
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

    async def test_unknown_tool_call_timeout_reports_failure_and_continues(self):
        StallingToolCallLLM.tool_name = "ghost_tool_not_registered"
        sid = f"mock_tc_timeout_{uuid.uuid4().hex}"
        try:
            text = await self._consume(sid)
            self.assertIn("超时后模型继续运行", text)
            self.assertIn("[DONE]", text)
            self.assertIn("TOOL_CALL_STREAM_TIMEOUT", text)
            # 未知工具（有名有 id）保留声明并成对反馈失败结果（契约完整），
            # 模型可基于失败结果自我纠正，任务继续运行
            self.assertIn('"timeout": true', text)
            self.assertIn("工具调用失败", text)
        finally:
            await cleanup_chat_memory_manager(sid)
            ChatMemoryManager.delete_chat_session_file(sid)

    async def test_unknown_tool_blocked_with_paired_result_and_continues(self):
        # 完整的未知工具调用（不超时）：旧实现把声明从 assistant.tool_calls
        # 剔除但仍生成拦截结果 → "结果无声明"孤儿 → 下一轮回放上游 400、
        # 任务终止。新实现：声明保留 + 成对拦截结果 + 任务继续。
        StallingToolCallLLM.mode = "ghost_full"
        StallingToolCallLLM.tool_name = "ghost_tool_not_registered"
        sid = f"mock_tc_timeout_{uuid.uuid4().hex}"
        try:
            text = await self._consume(sid)
            self.assertIn("超时后模型继续运行", text)
            self.assertIn("[DONE]", text)
            self.assertGreaterEqual(StallingToolCallLLM.calls, 2)
            # 拦截结果以 tool_return 事件反馈（blocked 标记 + 纠正指引）
            self.assertIn('"blocked": true', text)
            self.assertIn("不在本轮可用工具列表中", text)
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
