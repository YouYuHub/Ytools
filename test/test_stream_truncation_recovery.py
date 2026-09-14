"""上游流式截断（提前断开）恢复回归测试。

背景：上游网关在响应生成时间过长时主动断开连接（EOF 且未收到
finish_reason 与 [DONE]）。此前该场景与正常收尾走完全相同的代码路径——
空正文静默落盘并结束任务，思考内容看似保留实则回答丢失。实证案例：
history_files/yt-2026-09-13_22.03.42_062_chat.jsonl 两轮均在流开始约
301 秒处被切断（22:31:19→22:36:20、22:42:22→22:47:23），收到的都是
"思考全文 + 空正文 + [DONE]"。

修复：
- chat_llm.py：EOF 且未收到 finish_reason 时在合成 [DONE] 前补发
  stream_truncated 标记帧；
- chat_factory.py / sub_agent.py：拦截标记帧走"截断续写重试"——已生成的
  部分内容进 messages（不落盘）+ 内部消息提示模型继续；重试超限先落盘
  部分内容再写带统计的错误记录，任务不再静默假成功。

测试 mock 直接模拟 ChatLLM 的完整行为（含 stream_truncated 标记帧），
验证工厂层与子智能体的拦截/重试/超限收尾逻辑。
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


class TruncatingLLM:
    """按 fail_calls 截断前 N 次调用（模拟上游 EOF + 标记帧），之后正常回答。"""

    calls = 0
    fail_calls = 1
    last_messages = None

    @staticmethod
    async def chat_completions(request=None, stream=False, stop_checker=None, **kw):
        TruncatingLLM.calls += 1
        if TruncatingLLM.last_messages is None:
            TruncatingLLM.last_messages = {}
        TruncatingLLM.last_messages[TruncatingLLM.calls] = (
            list(request.messages) if request is not None and request.messages else []
        )
        if TruncatingLLM.calls <= TruncatingLLM.fail_calls:
            # 模拟 ChatLLM 对上游 EOF 的处理：部分思考增量 →
            # stream_truncated 标记帧 → 合成 [DONE]（无 finish_reason）
            yield f"data: {json.dumps({'reasoning_content': '部分生成的思考内容'})}\n\n"
            yield f"data: {json.dumps({'stream_truncated': True})}\n\n"
            yield "data: [DONE]\n\n"
            return
        yield f"data: {json.dumps({'content': '恢复后的完整回答'})}\n\n"
        yield f"data: {json.dumps({'finish_reason': 'stop'})}\n\n"
        yield "data: [DONE]\n\n"


class SubAgentTruncatingLLM:
    """子智能体专用 mock：第一次截断，第二次正常（content 而非工具调用）。"""

    calls = 0

    @staticmethod
    async def chat_completions(request=None, stream=False, stop_checker=None, **kw):
        SubAgentTruncatingLLM.calls += 1
        if SubAgentTruncatingLLM.calls == 1:
            yield f"data: {json.dumps({'reasoning_content': '子任务部分思考'})}\n\n"
            yield f"data: {json.dumps({'stream_truncated': True})}\n\n"
            yield "data: [DONE]\n\n"
            return
        yield f"data: {json.dumps({'content': '子任务恢复后的结论'})}\n\n"
        yield f"data: {json.dumps({'finish_reason': 'stop'})}\n\n"
        yield "data: [DONE]\n\n"


class StreamTruncationRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        cf._CHAT_WORKER_MODE = "inline"
        self._orig_chat = cf.ChatLLM.chat_completions
        self._orig_load_var = cf.load_var
        cf.ChatLLM.chat_completions = TruncatingLLM.chat_completions
        cf.load_var = self._orig_load_var  # 保持真实配置（截断重试默认 2 次）
        cf._SSE_HEARTBEAT_SECONDS = 0.05
        cf.load_all_tools = lambda: asyncio.sleep(0)
        cf.tool_registry.ALL_TOOLS = []
        cf.tool_registry.TOOL_MCP_SERVERS = {}
        cf.require_default_chat_config = lambda: None
        cf.compact_session_history_if_needed = lambda *a, **k: asyncio.sleep(0)
        TruncatingLLM.calls = 0
        TruncatingLLM.fail_calls = 1
        TruncatingLLM.last_messages = {}

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

    def _history_path(self, sid):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(root, "history_files", f"{sid}_chat.jsonl")

    async def test_truncated_stream_retries_and_completes(self):
        # 第一次调用被截断 → 自动重试 → 第二次正常回答收尾，任务不假结束
        sid = f"mock_trunc_{uuid.uuid4().hex}"
        try:
            text = await self._consume(sid)
            self.assertIn("恢复后的完整回答", text)
            self.assertIn("[DONE]", text)
            self.assertGreaterEqual(TruncatingLLM.calls, 2)
            # 重试过程对用户可见（warning 帧），但任务不被终止
            self.assertIn("STREAM_TRUNCATED_RETRY", text)
            self.assertIn("自动续写重试", text)
            # 落盘历史不应有"思考全文 + 空正文"的假收尾记录：
            # 截断轮只进 messages 续写上下文，完整回答统一落盘
            with open(self._history_path(sid), "r", encoding="utf-8") as f:
                raw = f.read()
            self.assertNotIn('"role": "assistant", "content": ""', raw)
            self.assertIn("恢复后的完整回答", raw)
        finally:
            await cleanup_chat_memory_manager(sid)
            ChatMemoryManager.delete_chat_session_file(sid)

    async def test_truncation_retry_includes_partial_context(self):
        # 重试请求应携带已生成的部分内容（截断续写上下文）与内部提示消息
        sid = f"mock_trunc_ctx_{uuid.uuid4().hex}"
        try:
            text = await self._consume(sid)
            self.assertIn("恢复后的完整回答", text)
            second = TruncatingLLM.last_messages.get(2) or []
            flattened = json.dumps(second, ensure_ascii=False, default=str)
            # 部分思考以占位形式进入续写上下文（与 finish_reason=length 同语义）
            self.assertIn("...部分生成的思考内容", flattened)
            self.assertIn("连接中断被截断", flattened)
        finally:
            await cleanup_chat_memory_manager(sid)
            ChatMemoryManager.delete_chat_session_file(sid)

    async def test_truncation_beyond_max_retries_records_error(self):
        # 重试上限内仍未恢复：落盘部分内容 + 错误记录，任务结束不无限重试
        TruncatingLLM.fail_calls = 3  # 默认上限 2：重试 2 次后第三次仍截断 → 终止
        sid = f"mock_trunc_max_{uuid.uuid4().hex}"
        try:
            text = await self._consume(sid)
            self.assertIn("[DONE]", text)
            self.assertEqual(TruncatingLLM.calls, 3)
            with open(self._history_path(sid), "r", encoding="utf-8") as f:
                raw = f.read()
            # 已生成的部分思考被保留 + 错误记录带重试统计
            self.assertIn("部分生成的思考内容", raw)
            self.assertIn("响应完成前断开", raw)
            self.assertIn('"retry": 2', raw)
            self.assertIn('"max_attempts": 2', raw)
        finally:
            await cleanup_chat_memory_manager(sid)
            ChatMemoryManager.delete_chat_session_file(sid)

    async def test_normal_stream_unaffected(self):
        # 正常完成（有 finish_reason）不触发重试，行为与原先完全一致
        TruncatingLLM.fail_calls = 0
        sid = f"mock_trunc_ok_{uuid.uuid4().hex}"
        try:
            text = await self._consume(sid)
            self.assertIn("恢复后的完整回答", text)
            self.assertIn("[DONE]", text)
            self.assertEqual(TruncatingLLM.calls, 1)
            self.assertNotIn("STREAM_TRUNCATED_RETRY", text)
        finally:
            await cleanup_chat_memory_manager(sid)
            ChatMemoryManager.delete_chat_session_file(sid)


class SubAgentTruncationRecoveryTests(unittest.TestCase):
    """子智能体截断续写：第一次调用被截断 → 重试后正常给出结论（done）。"""

    def test_sub_agent_truncated_stream_retries_and_completes(self):
        from chat.chat_llm import ChatLLM
        from factory.agent_runtime.sub_agent import SubAgentContext, SubAgentRunner, new_agent_id

        original = ChatLLM.chat_completions
        ChatLLM.chat_completions = SubAgentTruncatingLLM.chat_completions
        SubAgentTruncatingLLM.calls = 0
        emitted = []

        async def emit(payload):
            emitted.append((payload.get("phase"), payload))

        ctx = SubAgentContext(
            agent_id=new_agent_id(),
            parent_agent_id="main",
            parent_tool_call_id="call_p1",
            parent_tool_index=0,
            agent_index=0,
            session_id=f"mock_sub_trunc_{uuid.uuid4().hex}",
            task="测试任务",
            initial_todo=None,
            tools=[],
            tool_servers={},
            configured_tool_names=set(),
            configured_tool_servers={},
            max_rounds=5,
            timeout_seconds=0,
            reply_max_chars=1000,
            emit_event=emit,
            stop_checker=lambda: False,
        )
        try:
            runner = SubAgentRunner(ctx)
            result = asyncio.run(runner.run())
            self.assertEqual(result.status, "done")
            self.assertIn("子任务恢复后的结论", result.final_reply)
            self.assertGreaterEqual(SubAgentTruncatingLLM.calls, 2)
            # 首轮截断后 messages 中应有截断续写内部提示（受 max_rounds 约束）
            internal = [
                m for m in runner.messages
                if m.get("_internal") and "连接中断被截断" in str(m.get("content"))
            ]
            self.assertGreaterEqual(len(internal), 1)
        finally:
            ChatLLM.chat_completions = original


if __name__ == "__main__":
    unittest.main()
