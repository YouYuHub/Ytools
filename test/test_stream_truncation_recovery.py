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


class TextToolCallLLM:
    """First reply is a text envelope; the next reply is a normal answer."""

    calls = 0
    last_messages = None
    always_markup = False

    @staticmethod
    async def chat_completions(request=None, stream=False, stop_checker=None, **kw):
        TextToolCallLLM.calls += 1
        TextToolCallLLM.last_messages = list(request.messages or [])
        if TextToolCallLLM.calls == 1 or TextToolCallLLM.always_markup:
            chunks = [
                "@@<tool_",
                "call>\n<tool_call>\n",
                '{"name":"todo_write","arguments":{"todos":[]}}',
                "\n</tool_call>",
            ]
        else:
            chunks = ["任务已完成，以下是最终结果。"]
        for content in chunks:
            yield f"data: {json.dumps({'content': content}, ensure_ascii=False)}\n\n"
        yield 'data: {"finish_reason":"stop"}\n\n'
        yield "data: [DONE]\n\n"


class TruncationTestBase(unittest.IsolatedAsyncioTestCase):
    """公共基座：只放环境搭建与消费工具，不定义测试方法（避免被子类重复继承）。"""

    def setUp(self):
        cf._CHAT_WORKER_MODE = "inline"
        self._orig_chat = cf.ChatLLM.chat_completions
        self._orig_load_var = cf.load_var
        cf.ChatLLM.chat_completions = TruncatingLLM.chat_completions

        def _patched_load_var(key, default=None):
            # 截断重试次数与 .env 解耦（真实环境可能调成 3/5）：
            # 本测试固定 2 次验证"默认上限"语义，其余配置走真实读取
            if key == "STREAM_TRUNCATION_MAX_RETRIES":
                return 2
            return self._orig_load_var(key, default)

        cf.load_var = _patched_load_var
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


class TextToolCallRecoveryTests(TruncationTestBase):
    async def test_text_tool_call_is_hidden_and_model_retries(self):
        sid = f"mock_text_tool_{uuid.uuid4().hex}"
        TextToolCallLLM.calls = 0
        TextToolCallLLM.always_markup = False
        cf.ChatLLM.chat_completions = TextToolCallLLM.chat_completions
        try:
            text = await self._consume(sid)
            self.assertEqual(TextToolCallLLM.calls, 2)
            self.assertIn("TEXT_TOOL_CALL_RETRY", text)
            self.assertIn("任务已完成，以下是最终结果", text)
            self.assertNotIn("<tool_call>", text)
            self.assertIn("请勿输出 <tool_call>", json.dumps(
                TextToolCallLLM.last_messages, ensure_ascii=False, default=str,
            ))
            with open(self._history_path(sid), "r", encoding="utf-8") as f:
                self.assertNotIn("<tool_call>", f.read())
        finally:
            await cleanup_chat_memory_manager(sid)
            ChatMemoryManager.delete_chat_session_file(sid)

    async def test_repeated_text_tool_calls_stop_after_two_retries(self):
        sid = f"mock_text_tool_stuck_{uuid.uuid4().hex}"
        TextToolCallLLM.calls = 0
        TextToolCallLLM.always_markup = True
        cf.ChatLLM.chat_completions = TextToolCallLLM.chat_completions
        try:
            text = await self._consume(sid)
            self.assertEqual(TextToolCallLLM.calls, 3)
            self.assertIn("模型连续输出文本形式的工具调用", text)
            self.assertNotIn("<tool_call>", text)
        finally:
            TextToolCallLLM.always_markup = False
            await cleanup_chat_memory_manager(sid)
            ChatMemoryManager.delete_chat_session_file(sid)


class StreamTruncationRecoveryTests(TruncationTestBase):

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


class IntermittentLLM:
    """模拟"长任务中多次间歇中断但从未连续达上限"的场景。

    调用序列（上限 2）：1 截断 → 2 正常工具调用轮(todo_write) → 3 截断 →
    4 截断 → 5 正常回答。
    旧的任务级累计计数：call 1 计 1、call 3 计 2、call 4 达上限 2 → 第 4 次
    调用后直接终止任务（共 4 次调用，无最终回答）。
    连续中断计数语义（修复后）：call 2 正常完成把计数清零，call 3、4 重新
    从 1 开始累计，第 5 次调用仍可正常完成任务。
    """

    calls = 0
    last_messages = None

    @staticmethod
    async def chat_completions(request=None, stream=False, stop_checker=None, **kw):
        IntermittentLLM.calls += 1
        n = IntermittentLLM.calls
        if IntermittentLLM.last_messages is None:
            IntermittentLLM.last_messages = {}
        IntermittentLLM.last_messages[n] = (
            list(request.messages) if request is not None and request.messages else []
        )
        if n in (1, 3, 4):
            # 模拟上游 EOF + stream_truncated 标记帧（部分思考增量）
            yield f"data: {json.dumps({'reasoning_content': f'间歇中断片段{n}'})}\n\n"
            yield f"data: {json.dumps({'stream_truncated': True})}\n\n"
            yield "data: [DONE]\n\n"
            return
        if n == 2:
            # 正常完成的工具调用轮：finish_reason=tool_calls（触发计数复位）
            yield f"data: {json.dumps({'content': '工具轮前的说明'})}\n\n"
            yield f"data: {json.dumps({'tool_calls': [{
                'index': 0,
                'id': 'call_reset_1',
                'type': 'function',
                'function': {
                    'name': 'todo_write',
                    'arguments': json.dumps(
                        {'todos': [{'id': 't1', 'content': '验证连续中断计数复位', 'status': 'pending'}]},
                        ensure_ascii=False,
                    ),
                },
            }]})}\n\n"
            yield f"data: {json.dumps({'finish_reason': 'tool_calls'})}\n\n"
            yield "data: [DONE]\n\n"
            return
        # n == 5：恢复后的正常收尾
        yield f"data: {json.dumps({'content': '恢复后的完整回答'})}\n\n"
        yield f"data: {json.dumps({'finish_reason': 'stop'})}\n\n"
        yield "data: [DONE]\n\n"


class ModelRetriesConsecutiveResetTests(TruncationTestBase):
    """连续中断计数复位回归：正常完成的轮次清零计数，任务不被历史累计误杀。"""

    def setUp(self):
        super().setUp()
        cf.ChatLLM.chat_completions = IntermittentLLM.chat_completions
        IntermittentLLM.calls = 0
        IntermittentLLM.last_messages = {}

    async def test_consecutive_counter_resets_after_normal_round(self):
        # 中断(1) → 正常工具轮(2) → 中断(3) → 中断(4) → 正常收尾(5)：
        # 上限 2 是"单次请求内连续中断"——call 2 正常完成后计数清零，
        # call 3/4 各自重新累计（第 1 次、第 2 次），call 5 正常完成任务。
        # 旧的任务级累计计数会在 call 4 就终止任务（仅 4 次调用 + 错误记录）
        sid = f"mock_trunc_reset_{uuid.uuid4().hex}"
        try:
            text = await self._consume(sid)
            self.assertIn("恢复后的完整回答", text)
            self.assertIn("[DONE]", text)
            self.assertEqual(IntermittentLLM.calls, 5)
            # 连续计数语义：每次中断都从"第 1 次"重新计数
            self.assertEqual(text.count("（第 1 次）"), 2)  # 调用 1、3（各为新一轮的连续第 1 次）
            self.assertIn("（第 2 次）", text)              # 调用 4（同一请求周期内连续第 2 次）
            self.assertNotIn("（第 3 次）", text)           # 未发生连续 3 次中断
            # 落盘不应有"连续重试超限"的错误记录，最终回答完整保留
            with open(self._history_path(sid), "r", encoding="utf-8") as f:
                raw = f.read()
            self.assertNotIn("单次请求内连续自动重试", raw)
            self.assertIn("恢复后的完整回答", raw)
        finally:
            await cleanup_chat_memory_manager(sid)
            ChatMemoryManager.delete_chat_session_file(sid)


if __name__ == "__main__":
    unittest.main()
