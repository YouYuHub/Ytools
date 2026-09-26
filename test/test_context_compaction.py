import json
import unittest
from dataclasses import replace as dataclasses_replace

from config import ChatLLMRequest
from factory.agent_runtime import context_compaction as compaction
from memory.chat_history_format import round_entry_to_minimal_messages


class _MemoryStub:
    def __init__(self, entries):
        self.entries = entries
        self.summary = None

    async def get_context_summary(self):
        return self.summary

    async def get_chat_history(self):
        return self.entries

    async def update_context_summary(self, summary):
        self.summary = summary


class ContextCompactionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._original_chat = compaction.ChatLLM.chat_completions
        self._original_resolver = compaction.resolve_context_compaction_model_config
        self._original_context_limit = compaction.resolve_model_max_input_tokens
        # 重试策略固定为 1 条降级链 + 零间隔：与旧行为语义一致，且测试不受
        # .env/默认重试配置与 1 秒 sleep 影响（显式验证重试语义的用例单独展开）
        self._original_retry_attempts = compaction.resolve_compaction_retry_max_attempts
        self._original_retry_interval = compaction._COMPACTION_RETRY_INTERVAL_SECONDS
        compaction.resolve_compaction_retry_max_attempts = lambda: 1
        compaction._COMPACTION_RETRY_INTERVAL_SECONDS = 0
        self.captured_requests = []

        def fake_chat_completions(*, request=None, stream=False, model_config=None, **_kwargs):
            self.captured_requests.append({
                "request": request,
                "stream": stream,
                "model_config": model_config,
            })
            if not stream:
                return {"content": "任务仍需继续；工具已返回关键结果。"}

            # 流式契约：提供 event_emitter 时压缩走流式接口，逐帧产出 SSE 行
            async def _sse_frames():
                yield 'data: {"reasoning_content": "正在梳理轨迹与结论"}\n\n'
                yield 'data: {"content": "任务仍需"}\n\n'
                yield 'data: {"content": "继续；工具已返回关键结果。"}\n\n'
                yield 'data: {"usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20}}\n\n'
                yield "data: [DONE]\n\n"

            return _sse_frames()

        compaction.ChatLLM.chat_completions = staticmethod(fake_chat_completions)
        compaction.resolve_context_compaction_model_config = lambda _settings: {
            "selected_provider_name": "Test Provider",
            "selected_model_name": "Test Model",
            "selected_model_id": "test-model",
            "apiType": "chat-completions",
            "maxInputTokens": 20_000,
            "maxOutputTokens": 512,
        }

    def tearDown(self):
        compaction.ChatLLM.chat_completions = staticmethod(self._original_chat)
        compaction.resolve_context_compaction_model_config = self._original_resolver
        compaction.resolve_model_max_input_tokens = self._original_context_limit
        compaction.resolve_compaction_retry_max_attempts = self._original_retry_attempts
        compaction._COMPACTION_RETRY_INTERVAL_SECONDS = self._original_retry_interval

    @staticmethod
    def _settings(oversized_factor=1.5, max_rejections=3):
        return compaction.ContextCompactionSettings(
            trigger_ratio=0.5,
            summary_budget_ratio=0.2,
            oversized_reject_factor=oversized_factor,
            max_oversized_rejections=max_rejections,
        )

    async def test_oversized_summary_source_is_fully_chunked_before_model_calls(self):
        """单轮/单轮次源超过模型输入预算时，中段也必须完整送入分段摘要。"""
        original_require = compaction.require_default_chat_config
        compaction.require_default_chat_config = lambda: {
            "selected_provider_name": "Test Provider",
            "selected_model_id": "test-model",
            "apiType": "chat-completions",
            "maxInputTokens": 20_000,
            "maxOutputTokens": 512,
        }
        source = "\n\n".join(
            f"BLOCK_{index:02d}_MIDDLE_EVIDENCE_" + ("x" * 2_000)
            for index in range(48)
        )
        try:
            result = await compaction.summarize_context_text(
                source,
                ChatLLMRequest(messages=[{"role": "user", "content": "q"}]),
                scope="跨轮会话历史",
                settings=self._settings(),
            )
        finally:
            compaction.require_default_chat_config = original_require

        self.assertTrue(result.source_was_chunked)
        self.assertGreater(result.source_chunk_count, 1)
        self.assertFalse(result.source_was_truncated)
        original_parts = [
            captured["request"].messages[1].content
            for captured in self.captured_requests[:result.source_chunk_count]
        ]
        self.assertEqual("".join(original_parts), source)
        self.assertTrue(all(f"BLOCK_{index:02d}_MIDDLE_EVIDENCE_" in "".join(original_parts)
                            for index in range(48)))

    def test_token_chunk_splitter_preserves_all_text(self):
        source = "\n\n".join(f"段落-{index}-" + ("详细内容" * 120) for index in range(30))
        chunks = compaction._split_text_to_token_chunks(source, 300)
        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(chunks), source)
        self.assertTrue(all(compaction.estimate_text_tokens(chunk) <= 300 for chunk in chunks))

    async def test_summarize_falls_back_to_chat_model_then_raises(self):
        """压缩模型失败 -> 聊天模型重试一次；重试仍失败 -> 抛 ContextCompactionError。"""
        calls = []

        def fake_chat(*, request=None, stream=False, model_config=None, **_kwargs):
            calls.append(str(model_config.get("selected_model_id")))
            if len(calls) == 1:
                raise RuntimeError("压缩模型连接超时")
            return {"content": "聊天模型生成的摘要"}

        original_require = compaction.require_default_chat_config
        compaction.ChatLLM.chat_completions = staticmethod(fake_chat)
        compaction.require_default_chat_config = lambda: {
            "selected_provider_name": "Chat Provider",
            "selected_model_id": "chat-model",
            "apiType": "chat-completions",
            "maxInputTokens": 20_000,
            "maxOutputTokens": 512,
        }
        try:
            result = await compaction.summarize_context_text(
                "源文本", ChatLLMRequest(messages=[{"role": "user", "content": "q"}]),
                scope="单轮工具执行上下文",
            )
            self.assertEqual(result.text, "聊天模型生成的摘要")
            # 降级链真实发生过（压缩模型失败 → 聊天模型产出）：used_fallback 置 True。
            # 此前该字段恒为 False（死字段），前端"降级=False"无信息量且掩盖了实际降级。
            self.assertTrue(result.used_fallback)
            self.assertEqual(calls, ["test-model", "chat-model"])

            # 聊天模型也失败 -> 终止任务
            def fake_chat_all_fail(*, request=None, stream=False, model_config=None, **_kwargs):
                raise RuntimeError(f"{model_config.get('selected_model_id')} 不可用")

            compaction.ChatLLM.chat_completions = staticmethod(fake_chat_all_fail)
            with self.assertRaises(compaction.ContextCompactionError):
                await compaction.summarize_context_text(
                    "源文本", ChatLLMRequest(messages=[{"role": "user", "content": "q"}]),
                    scope="单轮工具执行上下文",
                )
        finally:
            compaction.require_default_chat_config = original_require

    async def test_summarize_same_model_failure_does_not_retry(self):
        """跟随聊天模型的场景下压缩失败：同一模型不重复尝试，直接抛错。"""
        def fake_chat(*, request=None, stream=False, model_config=None, **_kwargs):
            raise RuntimeError("上游 500")

        original_require = compaction.require_default_chat_config
        compaction.ChatLLM.chat_completions = staticmethod(fake_chat)
        compaction.resolve_context_compaction_model_config = lambda _settings: {
            "selected_provider_name": "Chat Provider",
            "selected_model_id": "chat-model",
            "apiType": "chat-completions",
            "maxInputTokens": 20_000,
            "maxOutputTokens": 512,
        }
        compaction.require_default_chat_config = lambda: {
            "selected_provider_name": "Chat Provider",
            "selected_model_id": "chat-model",
            "apiType": "chat-completions",
            "maxInputTokens": 20_000,
            "maxOutputTokens": 512,
        }
        try:
            with self.assertRaises(compaction.ContextCompactionError):
                await compaction.summarize_context_text(
                    "源文本", ChatLLMRequest(messages=[{"role": "user", "content": "q"}]),
                    scope="跨轮会话历史",
                )
        finally:
            compaction.require_default_chat_config = original_require

    async def test_summarize_retries_full_chain_until_configured_limit(self):
        """重试次数=完整降级链次数：每次尝试都走 压缩模型 -> 聊天模型，耗尽后抛错。"""
        calls = []

        def fake_chat(*, request=None, stream=False, model_config=None, **_kwargs):
            calls.append(str(model_config.get("selected_model_id")))
            raise RuntimeError(f"{model_config.get('selected_model_id')} 不可用")

        original_require = compaction.require_default_chat_config
        original_attempts = compaction.resolve_compaction_retry_max_attempts
        original_interval = compaction._COMPACTION_RETRY_INTERVAL_SECONDS
        compaction.ChatLLM.chat_completions = staticmethod(fake_chat)
        compaction.resolve_context_compaction_model_config = lambda _settings: {
            "selected_provider_name": "Test Provider",
            "selected_model_id": "test-model",
            "apiType": "chat-completions",
            "maxInputTokens": 20_000,
            "maxOutputTokens": 512,
        }
        compaction.require_default_chat_config = lambda: {
            "selected_provider_name": "Chat Provider",
            "selected_model_id": "chat-model",
            "apiType": "chat-completions",
            "maxInputTokens": 20_000,
            "maxOutputTokens": 512,
        }
        compaction.resolve_compaction_retry_max_attempts = lambda: 2
        compaction._COMPACTION_RETRY_INTERVAL_SECONDS = 0
        try:
            with self.assertRaises(compaction.ContextCompactionError) as ctx:
                await compaction.summarize_context_text(
                    "源文本", ChatLLMRequest(messages=[{"role": "user", "content": "q"}]),
                    scope="跨轮会话历史",
                )
            # 2 条完整链：每条都是 压缩模型 -> 聊天模型 的顺序
            self.assertEqual(calls, ["test-model", "chat-model", "test-model", "chat-model"])
            self.assertIn("已尝试 2 条完整降级链", str(ctx.exception))
        finally:
            compaction.require_default_chat_config = original_require
            compaction.resolve_compaction_retry_max_attempts = original_attempts
            compaction._COMPACTION_RETRY_INTERVAL_SECONDS = original_interval

    async def test_summarize_zero_or_negative_retry_means_unlimited(self):
        """0 或负数=不限制重试：整条降级链一直重试直到成功。"""
        calls = []

        def fake_chat(*, request=None, stream=False, model_config=None, **_kwargs):
            calls.append(str(model_config.get("selected_model_id")))
            if len(calls) < 5:
                raise RuntimeError(f"{model_config.get('selected_model_id')} 不可用")
            return {"content": "第 3 条链的压缩模型调用成功"}

        original_require = compaction.require_default_chat_config
        original_attempts = compaction.resolve_compaction_retry_max_attempts
        original_interval = compaction._COMPACTION_RETRY_INTERVAL_SECONDS
        compaction.ChatLLM.chat_completions = staticmethod(fake_chat)
        compaction.resolve_context_compaction_model_config = lambda _settings: {
            "selected_provider_name": "Test Provider",
            "selected_model_id": "test-model",
            "apiType": "chat-completions",
            "maxInputTokens": 20_000,
            "maxOutputTokens": 512,
        }
        compaction.require_default_chat_config = lambda: {
            "selected_provider_name": "Chat Provider",
            "selected_model_id": "chat-model",
            "apiType": "chat-completions",
            "maxInputTokens": 20_000,
            "maxOutputTokens": 512,
        }
        compaction.resolve_compaction_retry_max_attempts = lambda: 0
        compaction._COMPACTION_RETRY_INTERVAL_SECONDS = 0
        try:
            result = await compaction.summarize_context_text(
                "源文本", ChatLLMRequest(messages=[{"role": "user", "content": "q"}]),
                scope="跨轮会话历史",
            )
            # 前 2 条完整链（4 次调用）失败，第 5 次调用（第 3 条链的压缩模型）成功
            self.assertEqual(calls, ["test-model", "chat-model", "test-model", "chat-model", "test-model"])
            self.assertEqual(result.text, "第 3 条链的压缩模型调用成功")
        finally:
            compaction.require_default_chat_config = original_require
            compaction.resolve_compaction_retry_max_attempts = original_attempts
            compaction._COMPACTION_RETRY_INTERVAL_SECONDS = original_interval

    async def test_large_single_tool_result_compacts_active_round_as_plain_text(self):
        large_result = "关键输出" * 4000
        messages = [
            {"role": "system", "content": "系统提示"},
            {"role": "user", "content": "请处理这个任务"},
            {
                "role": "assistant",
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read_data", "arguments": "{}"},
                }],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": large_result,
                "_tool_name": "read_data",
            },
        ]
        request = ChatLLMRequest(
            session_id="context_compaction_test",
            messages=[{"role": "user", "content": "请处理这个任务"}],
            max_tokens=512,
        )
        request.tools = []

        result = await compaction.compact_active_round_context_if_needed(
            messages,
            request,
            settings=self._settings(),
        )

        self.assertTrue(result.triggered)
        self.assertLess(result.after_tokens, result.before_tokens)
        self.assertEqual([item["role"] for item in result.messages], ["system", "system", "user"])
        self.assertEqual(result.messages[-1]["content"], "请处理这个任务")
        self.assertEqual(
            result.messages[-2]["_context_compaction_scope"],
            "active_round",
        )
        self.assertNotIn(large_result, result.messages[-2]["content"])
        self.assertEqual(len(self.captured_requests), 1)
        summary_prompt = self.captured_requests[0]["request"].messages[0].content
        self.assertIn("不要输出 JSON", summary_prompt)
        self.assertEqual(
            self.captured_requests[0]["model_config"]["selected_model_id"],
            "test-model",
        )
        self.assertEqual(result.summary_text, "任务仍需继续；工具已返回关键结果。")
        self.assertEqual(result.compress_index, 1)

    async def test_single_round_compaction_emits_progress_events(self):
        large_result = "关键输出" * 4000
        messages = [
            {"role": "system", "content": "系统提示"},
            {"role": "user", "content": "请处理这个任务"},
            {
                "role": "assistant",
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read_data", "arguments": "{}"},
                }],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": large_result,
                "_tool_name": "read_data",
            },
        ]
        request = ChatLLMRequest(
            session_id="round_event_test",
            messages=[{"role": "user", "content": "请处理这个任务"}],
            max_tokens=512,
        )
        request.tools = []
        emitted = []

        async def emitter(payload):
            emitted.append(payload)

        result = await compaction.compact_active_round_context_if_needed(
            messages,
            request,
            settings=self._settings(),
            event_emitter=emitter,
        )

        self.assertTrue(result.triggered)
        # 传入 event_emitter 后压缩应改走流式接口
        self.assertTrue(self.captured_requests)
        self.assertTrue(self.captured_requests[-1]["stream"])
        # 事件序列：start -> delta*（思考/正文增量）-> done
        phases = [p.get("phase") for p in emitted]
        self.assertEqual(phases[0], "start")
        self.assertIn("delta", phases)
        self.assertEqual(phases[-1], "done")
        delta_payloads = [p for p in emitted if p.get("phase") == "delta"]
        self.assertTrue(any("reasoning_content" in p for p in delta_payloads))
        joined_delta_content = "".join(
            p.get("content", "") for p in delta_payloads
        )
        self.assertIn("任务仍需", joined_delta_content)
        self.assertEqual(emitted[0]["event"], "context_compaction")
        self.assertEqual(emitted[0]["scope"], "round")
        self.assertEqual(emitted[0]["phase"], "start")
        self.assertEqual(emitted[0]["role"], "assistant")
        self.assertIn("compress_context", emitted[0])
        self.assertIn("【上下文摘要】", emitted[0]["compress_context"])
        done_payload = emitted[-1]
        self.assertEqual(done_payload["event"], "context_compaction")
        self.assertEqual(done_payload["scope"], "round")
        self.assertEqual(done_payload["phase"], "done")
        self.assertEqual(done_payload["role"], "assistant")
        self.assertIn("compress_usage", done_payload)
        self.assertIn("before_tokens", done_payload["compress_usage"])
        self.assertIn("after_tokens", done_payload["compress_usage"])
        # done 事件携带最终摘要全文，供前端刷新后回放展示
        self.assertIn("summary_text", done_payload)
        self.assertIn("任务仍需继续", done_payload["summary_text"])

    async def test_single_round_compaction_skipped_when_history_dominant(self):
        # 超窗主要来自历史（系统提示巨大），本轮轨迹很小：不应空转压缩模型
        huge_system = "历史背景" * 3000
        messages = [
            {"role": "system", "content": huge_system},
            {"role": "user", "content": "继续"},
            {
                "role": "assistant",
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "get_time", "arguments": "{}"},
                }],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "14:20", "_tool_name": "get_time"},
        ]
        request = ChatLLMRequest(
            session_id="round_skip_test",
            messages=[{"role": "user", "content": "继续"}],
            max_tokens=512,
        )
        request.tools = []

        result = await compaction.compact_active_round_context_if_needed(
            messages,
            request,
            settings=self._settings(),
        )

        self.assertFalse(result.triggered)
        self.assertEqual(len(self.captured_requests), 0)

    async def test_session_compaction_emits_progress_events(self):
        def make_round(index):
            return {
                "event": "chat_round",
                "question": f"问题 {index}",
                "events": [
                    {"role": "user", "content": f"问题 {index}"},
                    {"role": "assistant", "content": "回答内容" * 150},
                    {
                        "role": "tool",
                        "tool_name": "query",
                        "arguments": "{}",
                        "result": f"工具结果 {index}" * 80,
                    },
                    {"role": "assistant", "done": "[DONE]"},
                ],
            }

        memory = _MemoryStub([make_round(index) for index in range(1, 8)])
        compaction.resolve_model_max_input_tokens = lambda default=8192: 1_000
        request = ChatLLMRequest(
            session_id="session_event_test",
            messages=[{"role": "user", "content": "新问题"}],
            max_tokens=512,
        )
        emitted = []

        async def emitter(payload):
            emitted.append(payload)

        compacted = await compaction.compact_session_history_if_needed(
            memory,
            request,
            settings=self._settings(),
            event_emitter=emitter,
        )

        self.assertGreater(compacted, 0)
        self.assertTrue(any(
            p.get("event") == "context_compaction"
            and p.get("scope") == "session"
            and p.get("phase") == "start"
            and "context_summary" in p
            for p in emitted
        ))
        # 流式压缩：start -> delta*（思考/正文增量）-> done（含最终摘要全文）
        self.assertTrue(any(
            p.get("event") == "context_compaction"
            and p.get("scope") == "session"
            and p.get("phase") == "delta"
            for p in emitted
        ))
        # 每批压缩量由预算自适应决定：本次调用内放不进预算的轮次一批压完，
        # 推送一对 start/done 事件（中间穿插 delta 增量帧）
        done_payloads = [
            p for p in emitted
            if p.get("event") == "context_compaction"
            and p.get("scope") == "session"
            and p.get("phase") == "done"
        ]
        self.assertEqual(len(done_payloads), 1)
        self.assertGreaterEqual(done_payloads[0].get("summary_usage", {}).get("compressed_rounds", 0), 1)
        # done 携带摘要全文；压缩请求应走流式接口
        self.assertTrue(done_payloads[0].get("summary_text"))
        self.assertIn("任务仍需继续", done_payloads[0]["summary_text"])
        # 前后对比字段：before = 触发时的历史全量估算，after = 压缩后的历史估算；
        # token_limit = 本批预算（自动路径 = 阈值）——前端据此显示
        # "历史上下文 X → Y · 触发阈值 Z"，消除把压缩模型输入误读为触发点的歧义
        done = done_payloads[0]
        self.assertIsNotNone(done.get("before_tokens"))
        self.assertIsNotNone(done.get("after_tokens"))
        self.assertGreater(done["before_tokens"], done["after_tokens"])
        self.assertIsNotNone(done.get("token_limit"))
        self.assertGreater(done["token_limit"], 0)
        # 触发来源默认 auto（任务开始自动）；调用方可传 task/first_call/manual/post
        self.assertEqual(done.get("trigger_reason"), "auto")
        self.assertTrue(self.captured_requests)
        self.assertTrue(self.captured_requests[-1]["stream"])

    async def test_session_compaction_done_carries_task_trigger_reason(self):
        """task/first_call 路径：done 事件携带触发来源与真实触发规模（全量上下文）。"""

        def make_round(index):
            return {
                "event": "chat_round",
                "question": f"问题 {index}",
                "events": [
                    {"role": "user", "content": f"问题 {index}"},
                    {"role": "assistant", "content": "回答内容" * 150},
                    {"role": "assistant", "done": "[DONE]"},
                ],
            }

        memory = _MemoryStub([make_round(index) for index in range(1, 8)])
        compaction.resolve_model_max_input_tokens = lambda default=8192: 1_000
        request = ChatLLMRequest(
            session_id="session_trigger_reason_test",
            messages=[{"role": "user", "content": "新问题"}],
            max_tokens=512,
        )
        emitted = []

        async def emitter(payload):
            emitted.append(payload)

        # 模拟任务内检查点触发：预算按历史预算传入，trigger_context_tokens 传全量规模
        # （预算下限守卫 max(1024, ...)，用 2048 避免被抬升）
        compacted = await compaction.compact_session_history_if_needed(
            memory,
            request,
            settings=self._settings(),
            budget_tokens=2048,
            enforce=True,
            force_all=True,
            event_emitter=emitter,
            trigger_reason="task",
            trigger_context_tokens=123_456,
            trigger_threshold=9_999,
        )

        self.assertGreater(compacted, 0)
        done = [
            p for p in emitted
            if p.get("event") == "context_compaction" and p.get("phase") == "done"
        ][0]
        self.assertEqual(done.get("trigger_reason"), "task")
        self.assertEqual(done.get("trigger_context_tokens"), 123_456)
        # 触发阈值（触发比较基准）与本批预算（压缩批输入预算）分离
        self.assertEqual(done.get("trigger_threshold"), 9_999)
        self.assertEqual(done.get("token_limit"), 2048)

    async def test_small_history_does_not_trigger_session_compaction(self):
        memory = _MemoryStub([{
            "event": "chat_round",
            "question": "简单问题",
            "events": [
                {"role": "user", "content": "简单问题"},
                {"role": "assistant", "content": "简单回答"},
                {"role": "assistant", "done": "[DONE]"},
            ],
        }])
        compaction.resolve_model_max_input_tokens = lambda default=8192: 10_000
        request = ChatLLMRequest(
            session_id="small_history_test",
            messages=[{"role": "user", "content": "新问题"}],
            max_tokens=512,
        )

        compacted = await compaction.compact_session_history_if_needed(
            memory,
            request,
            settings=self._settings(),
        )

        self.assertEqual(compacted, 0)
        self.assertIsNone(memory.summary)
        self.assertEqual(self.captured_requests, [])

    async def test_history_compaction_accumulates_source_round_count(self):
        def make_round(index):
            return {
                "event": "chat_round",
                "question": f"问题 {index}",
                "events": [
                    {"role": "user", "content": f"问题 {index}"},
                    {"role": "assistant", "content": "回答内容" * 150},
                    {
                        "role": "tool",
                        "tool_name": "query",
                        "arguments": "{}",
                        "result": f"工具结果 {index}" * 80,
                    },
                    {"role": "assistant", "done": "[DONE]"},
                ],
            }

        memory = _MemoryStub([make_round(1), make_round(2), make_round(3)])
        compaction.resolve_model_max_input_tokens = lambda default=8192: 1_000
        request = ChatLLMRequest(
            session_id="history_compaction_test",
            messages=[{"role": "user", "content": "新问题"}],
            max_tokens=512,
        )

        compacted = await compaction.compact_session_history_if_needed(
            memory,
            request,
            settings=self._settings(),
        )

        self.assertEqual(compacted, 2)
        self.assertIsNotNone(memory.summary)
        self.assertEqual(memory.summary["source_round_count"], 2)
        self.assertIn("工具已返回关键结果", memory.summary["summary"])

    async def test_enforce_mode_compacts_more_with_smaller_budget(self):
        """预算越小压得越多；无轮次窗口保护后压缩量完全由预算决定。"""
        def make_round(index):
            return {
                "event": "chat_round",
                "question": f"问题 {index}",
                "events": [
                    {"role": "user", "content": f"问题 {index}"},
                    {"role": "assistant", "content": "回答内容" * 150},
                    {
                        "role": "tool",
                        "tool_name": "query",
                        "arguments": "{}",
                        "result": f"工具结果 {index}" * 80,
                    },
                    {"role": "assistant", "done": "[DONE]"},
                ],
            }

        memory = _MemoryStub([make_round(index) for index in range(1, 6)])
        compaction.resolve_model_max_input_tokens = lambda default=8192: 1_000
        request = ChatLLMRequest(
            session_id="enforce_compaction_test",
            messages=[{"role": "user", "content": "新问题"}],
            max_tokens=512,
        )

        # 默认预算（阈值 = 1000 × 0.5 = 500）：预算内的最近轮次保留原始对话
        compacted = await compaction.compact_session_history_if_needed(
            memory,
            request,
            settings=self._settings(),
        )
        self.assertGreater(compacted, 0)

        # 极小预算：超出预算的轮次更多（不再有"保留最近 N 轮"下限）
        memory = _MemoryStub([make_round(index) for index in range(1, 6)])
        compacted = await compaction.compact_session_history_if_needed(
            memory,
            request,
            settings=self._settings(),
            budget_tokens=512,
            enforce=True,
        )
        self.assertEqual(compacted, 4)
        self.assertEqual(memory.summary["source_round_count"], 4)

    async def test_minimal_round_messages_drop_tools_and_thinking(self):
        """极简渲染：仅保留用户问题与助手文本回答。"""
        entry = {
            "event": "chat_round",
            "question": "帮我查数据",
            "events": [
                {"role": "user", "content": "帮我查数据"},
                {"role": "assistant", "content": "<think>思考过程</think>"},
                {
                    "role": "assistant",
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "query", "arguments": "{}"},
                    }],
                },
                {"role": "tool", "tool_call_id": "call_1", "tool_name": "query", "result": "大量数据" * 500},
                {"role": "assistant", "content": "查询完成：结果是 42"},
                {"role": "assistant", "done": "[DONE]"},
            ],
        }
        messages = round_entry_to_minimal_messages(entry)
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0], {"role": "user", "content": "帮我查数据"})
        self.assertEqual(messages[1]["role"], "assistant")
        self.assertEqual(messages[1]["content"], "查询完成：结果是 42")
        self.assertNotIn("思考过程", messages[1]["content"])
        self.assertNotIn("query", json.dumps(messages, ensure_ascii=False))

    async def test_history_compaction_keeps_one_cumulative_summary(self):
        """跨轮分块最终归并为一个累计摘要，旧摘要继续参与后续合并。"""

        def make_round(index):
            return {
                "event": "chat_round",
                "question": f"问题 {index}",
                "events": [
                    {"role": "user", "content": f"问题 {index}"},
                    {"role": "assistant", "content": "回答内容" * 150},
                    {
                        "role": "tool",
                        "tool_name": "query",
                        "arguments": "{}",
                        "result": f"工具结果 {index}" * 80,
                    },
                    {"role": "assistant", "done": "[DONE]"},
                ],
            }

        calls = []

        def fake_chat_completions(*, request=None, stream=False, model_config=None, **_kwargs):
            user_text = request.messages[-1].content
            calls.append(user_text)
            return {"content": f"本轮摘要：{len(calls)}"}

        original_chat = compaction.ChatLLM.chat_completions
        compaction.ChatLLM.chat_completions = staticmethod(fake_chat_completions)
        compaction.resolve_model_max_input_tokens = lambda default=8192: 1_000
        # 第一次调用压出轮 1，新增轮次后第二次调用继续压缩轮 2-4。
        memory = _MemoryStub([make_round(1), make_round(2)])
        request = ChatLLMRequest(
            session_id="history_block_test",
            messages=[{"role": "user", "content": "新问题"}],
            max_tokens=512,
        )
        try:
            await compaction.compact_session_history_if_needed(
                memory,
                request,
                settings=self._settings(),
            )
            memory.entries = [make_round(index) for index in range(1, 6)]
            await compaction.compact_session_history_if_needed(
                memory,
                request,
                settings=self._settings(),
            )
        finally:
            compaction.ChatLLM.chat_completions = staticmethod(original_chat)

        self.assertIsNotNone(memory.summary)
        blocks = memory.summary["blocks"]
        self.assertEqual(len(blocks), 1)
        self.assertEqual(memory.summary["source_round_count"], 4)
        self.assertEqual(blocks[0]["round_start"], 1)
        self.assertEqual(blocks[0]["round_end"], 4)
        self.assertEqual(memory.summary["return_blocks"], 1)
        # 拼接优先：新批摘要为纯文本（无分段标题）时，作为自由文本段直接
        # 拼入旧累计摘要，预算内不再发起独立的模型合并调用——calls 只有
        # 两次批压缩，最后一块同时承载旧摘要正文与新批摘要正文。
        self.assertEqual(len(calls), 2)
        cumulative_text = memory.summary["summary"] or ""
        self.assertIn("本轮摘要：1", cumulative_text)
        self.assertIn("本轮摘要：2", cumulative_text)

    async def test_history_compaction_parses_section_fields_into_blocks(self):
        """压缩模型按标题分段输出时，段落内容回填到结构化字段。"""

        def make_round(index):
            return {
                "event": "chat_round",
                "question": f"问题 {index}",
                "events": [
                    {"role": "user", "content": f"问题 {index}"},
                    {"role": "assistant", "content": "回答内容" * 150},
                    {
                        "role": "tool",
                        "tool_name": "query",
                        "arguments": "{}",
                        "result": f"工具结果 {index}" * 80,
                    },
                    {"role": "assistant", "done": "[DONE]"},
                ],
            }

        sectioned_text = (
            "【任务目标】\n- 实现远程终端控制\n"
            "【已完成工作】\n- NamedPipeServer\n"
            "【未解决问题】\n- dispatcher未完成\n"
            "【重要文件】\n- src/session.cpp\n"
        )

        def fake_chat_completions(*, request=None, stream=False, model_config=None, **_kwargs):
            return {"content": sectioned_text}

        original_chat = compaction.ChatLLM.chat_completions
        compaction.ChatLLM.chat_completions = staticmethod(fake_chat_completions)
        compaction.resolve_model_max_input_tokens = lambda default=8192: 1_000
        memory = _MemoryStub([make_round(1), make_round(2), make_round(3)])
        request = ChatLLMRequest(
            session_id="history_section_test",
            messages=[{"role": "user", "content": "新问题"}],
            max_tokens=512,
        )
        try:
            await compaction.compact_session_history_if_needed(
                memory,
                request,
                settings=self._settings(),
            )
        finally:
            compaction.ChatLLM.chat_completions = staticmethod(original_chat)

        self.assertIsNotNone(memory.summary)
        block = memory.summary["blocks"][0]
        self.assertEqual(block["summary"], "")
        self.assertEqual(block["objective"], ["实现远程终端控制"])
        self.assertEqual(block["completed"], ["NamedPipeServer"])
        self.assertEqual(block["open_items"], ["dispatcher未完成"])
        self.assertEqual(block["files"], ["src/session.cpp"])
        self.assertIn("dispatcher未完成", memory.summary["open_items"])
        self.assertIn("src/session.cpp", memory.summary["files"])

    async def test_send_caliber_ignores_historical_reasoning_for_trigger(self):
        """发送口径回归：历史轮次思考不随请求回传，不应把全量估算推高到误触发。

        历史背景：运行时 messages 保留每轮思考全文（REASONING_RETURN_MAX_LENGTH=-1
        时为原始全文），而真实请求（copy_for_request）只回传最近一条思考。此前
        触发判断直接用运行时 messages 估算，长任务的大量历史思考会虚高到数倍，
        导致压缩在远低于真实规模的阈值时提前触发（用户实测 386k 触发 / 实际请求
        仅约 168k）。本用例保证：仅历史思考超阈值时不触发，且旧口径确实会误触发。
        """
        original_limit = compaction.resolve_model_max_input_tokens
        compaction.resolve_model_max_input_tokens = lambda default=8192: 20_000
        try:
            huge_reasoning = "思考过程" * 500  # 2000 字符 ≈ 2000 tokens/条
            messages = [
                {"role": "system", "content": "系统提示"},
                {"role": "user", "content": "请处理这个任务"},
            ]
            for index in range(10):
                messages.append({
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": huge_reasoning,
                    "tool_calls": [{
                        "id": f"call_{index}",
                        "type": "function",
                        "function": {"name": "read_data", "arguments": "{}"},
                    }],
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": f"call_{index}",
                    "content": "ok",
                    "_tool_name": "read_data",
                })
            # 当前轮只有小规模轨迹（真实请求规模远低于阈值）
            messages.append({"role": "user", "content": "当前轮提问"})
            messages.append({
                "role": "assistant",
                "content": "",
                "reasoning_content": "短思考",
                "tool_calls": [{
                    "id": "call_cur",
                    "type": "function",
                    "function": {"name": "get_time", "arguments": "{}"},
                }],
            })
            messages.append({
                "role": "tool",
                "tool_call_id": "call_cur",
                "content": "14:20",
                "_tool_name": "get_time",
            })

            token_limit = compaction.resolve_context_compaction_threshold(self._settings())
            # 旧口径（含全部历史思考）会超过阈值——正是被修复的误触发场景
            old_style = compaction.estimate_request_context_tokens(messages, None)
            self.assertGreater(old_style, token_limit)

            request = ChatLLMRequest(
                session_id="send_caliber_skip_test",
                messages=[{"role": "user", "content": "当前轮提问"}],
                max_tokens=512,
            )
            request.tools = []
            result = await compaction.compact_active_round_context_if_needed(
                messages,
                request,
                settings=self._settings(),
            )
            # 发送口径：真实请求规模未超阈值，不触发
            self.assertFalse(result.triggered)
            self.assertEqual(len(self.captured_requests), 0)
        finally:
            compaction.resolve_model_max_input_tokens = original_limit

    async def test_send_caliber_still_triggers_on_current_round_reasoning(self):
        """发送口径仍保留当前轮最新思考：当前轮轨迹真实超阈值时必须触发（防漏报）。"""
        original_limit = compaction.resolve_model_max_input_tokens
        compaction.resolve_model_max_input_tokens = lambda default=8192: 20_000
        try:
            messages = [
                {"role": "system", "content": "系统提示"},
                {"role": "user", "content": "请处理这个任务"},
                {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": "思考过程" * 1500,  # 6000 字符
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "read_data", "arguments": "{}"},
                    }],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_1",
                    "content": "关键输出" * 2000,  # 8000 字符
                    "_tool_name": "read_data",
                },
            ]
            request = ChatLLMRequest(
                session_id="send_caliber_trigger_test",
                messages=[{"role": "user", "content": "请处理这个任务"}],
                max_tokens=512,
            )
            request.tools = []
            result = await compaction.compact_active_round_context_if_needed(
                messages,
                request,
                settings=self._settings(),
            )
            self.assertTrue(result.triggered)
            self.assertEqual(len(self.captured_requests), 1)
        finally:
            compaction.resolve_model_max_input_tokens = original_limit


class OversizedResultHelpersTests(unittest.TestCase):
    def setUp(self):
        # 类内测试直接替换模块属性，必须保存/恢复，否则泄漏到同进程后续
        # 测试模块（如 test_chat_config_router 会重载 router 重新导入本属性）
        self._original_settings = compaction.load_context_compaction_settings
        self._original_window = compaction.resolve_model_max_input_tokens
        self._original_config = compaction.resolve_context_compaction_model_config

    def tearDown(self):
        compaction.load_context_compaction_settings = self._original_settings
        compaction.resolve_model_max_input_tokens = self._original_window
        compaction.resolve_context_compaction_model_config = self._original_config

    def test_oversized_threshold_disabled_when_factor_zero(self):
        compaction.load_context_compaction_settings = lambda: compaction.ContextCompactionSettings(
            trigger_ratio=0.8,
            summary_budget_ratio=0.2,
            oversized_reject_factor=0.0,
            max_oversized_rejections=3,
        )
        self.assertEqual(compaction.resolve_oversized_result_token_threshold(), 0)

    def test_oversized_threshold_uses_min_window_times_factor(self):
        compaction.resolve_model_max_input_tokens = lambda default=8192: 500_000
        compaction.resolve_context_compaction_model_config = lambda _settings: {
            "maxInputTokens": 200_000,
            "apiType": "chat-completions",
        }
        compaction.load_context_compaction_settings = lambda: compaction.ContextCompactionSettings(
            trigger_ratio=0.8,
            summary_budget_ratio=0.2,
            oversized_reject_factor=1.5,
            max_oversized_rejections=3,
        )
        threshold = compaction.resolve_oversized_result_token_threshold()
        self.assertEqual(threshold, 300_000)

    def test_oversized_threshold_floor_at_1024(self):
        compaction.resolve_model_max_input_tokens = lambda default=8192: 1000
        compaction.resolve_context_compaction_model_config = lambda _settings: {
            "maxInputTokens": 2000,
            "apiType": "chat-completions",
        }
        compaction.load_context_compaction_settings = lambda: compaction.ContextCompactionSettings(
            trigger_ratio=0.8,
            summary_budget_ratio=0.2,
            oversized_reject_factor=0.5,
            max_oversized_rejections=3,
        )
        self.assertEqual(compaction.resolve_oversized_result_token_threshold(), 1024)

    def test_oversized_preview_is_truncated_and_bounded(self):
        preview = compaction.make_oversized_result_preview("数据内容" * 10_000, token_budget=300)
        self.assertIn("【中间内容因上下文预算已省略】", preview)
        self.assertLessEqual(compaction.estimate_text_tokens(preview), 400)

    def test_oversized_feedback_mentions_budget_and_guidance(self):
        feedback = compaction.build_oversized_tool_feedback("list_dir_item", 600_000, 300_000)
        self.assertIn("list_dir_item", feedback)
        self.assertIn("600.0k", feedback)
        self.assertIn("300.0k", feedback)
        self.assertIn("重新考虑工具使用", feedback)
        self.assertNotIn("头尾节选", feedback)

    def test_oversized_feedback_includes_head_tail_excerpt(self):
        huge_text = "开头标记 " + "中间填充内容" * 20_000 + " 结尾标记"
        feedback = compaction.build_oversized_tool_feedback(
            "list_dir_item", 600_000, 300_000, result_text=huge_text
        )
        self.assertIn("头尾节选", feedback)
        self.assertIn("开头标记", feedback)
        self.assertIn("结尾标记", feedback)
        self.assertIn("【中间内容因上下文预算已省略】", feedback)
        self.assertLessEqual(compaction.estimate_text_tokens(feedback), 900)


class SummaryRequestParameterTests(unittest.TestCase):
    def setUp(self):
        self._original_parameter = compaction.get_role_parameter

    def tearDown(self):
        compaction.get_role_parameter = self._original_parameter

    def _build(self):
        return compaction._build_summary_request("压缩提示", "压缩源文本", 2048, "sess-1")

    def test_uses_compaction_parameter_when_configured(self):
        compaction.get_role_parameter = lambda role: {
            "temperature": 0.7, "max_tokens": 4096, "top_p": 0.9,
            "presence_penalty": 0.5, "reasoning_effort": "high",
            "extra_body": {"guided_json": True},
        }
        request = self._build()
        self.assertEqual(request.temperature, 0.7)
        self.assertEqual(request.max_tokens, 4096)
        self.assertEqual(request.top_p, 0.9)
        self.assertEqual(request.presence_penalty, 0.5)
        self.assertEqual(request.reasoning_effort, "high")
        self.assertEqual(request.extra_body, {"guided_json": True})

    def test_falls_back_to_compaction_defaults_without_parameter(self):
        compaction.get_role_parameter = lambda role: {}
        request = self._build()
        self.assertEqual(request.temperature, 0.2)
        self.assertEqual(request.max_tokens, 2048)
        self.assertEqual(request.top_p, 1.0)
        self.assertEqual(request.presence_penalty, 0.0)
        self.assertEqual(request.reasoning_effort, "low")
        self.assertIsNone(request.extra_body)

    def test_invalid_parameter_values_fall_back_to_defaults(self):
        compaction.get_role_parameter = lambda role: {
            "temperature": "not-a-number", "max_tokens": None, "reasoning_effort": "",
        }
        request = self._build()
        self.assertEqual(request.temperature, 0.2)
        self.assertEqual(request.max_tokens, 2048)
        self.assertEqual(request.reasoning_effort, "low")


class CompactionSettingsParsingTests(unittest.TestCase):
    """load_context_compaction_settings 的取值域规整。"""

    def setUp(self):
        self._original_load_var = compaction.load_var

    def tearDown(self):
        compaction.load_var = self._original_load_var

    def _with_env(self, values):
        def fake_load_var(name, default=None):
            return values.get(name, default)
        compaction.load_var = fake_load_var

    def test_keep_rounds_no_longer_part_of_settings(self):
        """历史轮次窗口已废弃：settings 不再包含该字段，环境变量被忽略。"""
        self._with_env({"HISTORY_COMPACT_KEEP_ROUNDS": "0"})
        settings = compaction.load_context_compaction_settings()
        self.assertFalse(hasattr(settings, "keep_rounds"))

    def test_keep_rounds_env_value_does_not_affect_compaction(self):
        """设置 HISTORY_COMPACT_KEEP_ROUNDS 不再影响压缩（仅按阈值/目标控制）。"""
        self._with_env({"HISTORY_COMPACT_KEEP_ROUNDS": "3"})
        settings = compaction.load_context_compaction_settings()
        self.assertEqual(compaction.resolve_history_target_tokens(settings), 0)
        self.assertFalse(hasattr(settings, "keep_rounds"))


class ContextCompactionThresholdTests(unittest.TestCase):
    def setUp(self):
        self._original_window = compaction.resolve_model_max_input_tokens
        self._original_config = compaction.resolve_context_compaction_model_config
        self._original_settings = compaction.load_context_compaction_settings

    def tearDown(self):
        compaction.resolve_model_max_input_tokens = self._original_window
        compaction.resolve_context_compaction_model_config = self._original_config
        compaction.load_context_compaction_settings = self._original_settings

    @staticmethod
    def _settings(trigger_ratio=0.8):
        return compaction.ContextCompactionSettings(
            trigger_ratio=trigger_ratio,
            summary_budget_ratio=0.2,
            oversized_reject_factor=1.5,
            max_oversized_rejections=3,
        )

    def test_threshold_uses_min_window_times_ratio(self):
        compaction.resolve_model_max_input_tokens = lambda default=8192: 1_000_000
        compaction.resolve_context_compaction_model_config = lambda _settings: {
            "maxInputTokens": 200_000,
            "apiType": "chat-completions",
        }
        compaction.load_context_compaction_settings = lambda: self._settings(trigger_ratio=0.5)
        # min(1M, 200k) × 0.5 = 100k
        self.assertEqual(compaction.resolve_context_compaction_threshold(), 100_000)

    def test_summary_budget_is_fraction_of_window(self):
        compaction.resolve_model_max_input_tokens = lambda default=8192: 1_000_000
        compaction.resolve_context_compaction_model_config = lambda _settings: {
            "maxInputTokens": 200_000,
            "apiType": "chat-completions",
        }
        compaction.load_context_compaction_settings = self._settings
        # 总预算 = 窗口 × 0.2 = 200k
        self.assertEqual(compaction.resolve_summary_total_budget(), 200_000)


class RoundMultiBlockCompactionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._original_chat = compaction.ChatLLM.chat_completions
        self._original_resolver = compaction.resolve_context_compaction_model_config
        self._original_context_limit = compaction.resolve_model_max_input_tokens
        self.captured_requests = []

        def fake_chat_completions(*, request=None, stream=False, model_config=None, **_kwargs):
            self.captured_requests.append(request)
            user_text = request.messages[-1].content
            if "【已有累计摘要】" in user_text:
                return {"content": "累计摘要：" + user_text}
            return {"content": f"段摘要：{len(self.captured_requests)}"}

        compaction.ChatLLM.chat_completions = staticmethod(fake_chat_completions)
        compaction.resolve_context_compaction_model_config = lambda _settings: {
            "selected_provider_name": "Test Provider",
            "selected_model_name": "Test Model",
            "selected_model_id": "test-model",
            "apiType": "chat-completions",
            "maxInputTokens": 20_000,
            "maxOutputTokens": 512,
        }
        compaction.resolve_model_max_input_tokens = lambda default=8192: 10_000

    def tearDown(self):
        compaction.ChatLLM.chat_completions = staticmethod(self._original_chat)
        compaction.resolve_context_compaction_model_config = self._original_resolver
        compaction.resolve_model_max_input_tokens = self._original_context_limit

    @staticmethod
    def _settings():
        return compaction.ContextCompactionSettings(
            trigger_ratio=0.5,
            summary_budget_ratio=0.2,
            oversized_reject_factor=1.5,
            max_oversized_rejections=3,
        )

    def _round_message(
        self,
        user_text: str,
        tool_results: list[str],
        block_marker: dict | None = None,
    ) -> list[dict]:
        messages = [{"role": "system", "content": "系统提示"}]
        if block_marker:
            messages.append(block_marker)
        messages.append({"role": "user", "content": user_text})
        for index, result in enumerate(tool_results, start=1):
            messages.append({
                "role": "assistant",
                "tool_calls": [{
                    "id": f"call_{index}",
                    "type": "function",
                    "function": {"name": "read_data", "arguments": "{}"},
                }],
            })
            messages.append({
                "role": "tool",
                "tool_call_id": f"call_{index}",
                "content": result,
                "_tool_name": "read_data",
            })
        return messages

    def _request(self, user_text: str) -> ChatLLMRequest:
        request = ChatLLMRequest(
            session_id="multi_block_test",
            messages=[{"role": "user", "content": user_text}],
            max_tokens=512,
        )
        request.tools = []
        return request

    async def test_second_compression_merges_into_cumulative_block(self):
        first_result = await compaction.compact_active_round_context_if_needed(
            self._round_message("请处理任务", ["关键输出" * 4000]),
            self._request("请处理任务"),
            settings=self._settings(),
            compression_index=1,
        )
        self.assertTrue(first_result.triggered)
        self.assertEqual(len(first_result.summary_blocks), 1)
        marker = first_result.messages[1]
        first_call_count = len(self.captured_requests)

        second_result = await compaction.compact_active_round_context_if_needed(
            self._round_message("请处理任务", ["第二批结果" * 4000], block_marker=marker),
            self._request("请处理任务"),
            settings=self._settings(),
            compression_index=2,
        )
        self.assertTrue(second_result.triggered)
        self.assertEqual(len(second_result.summary_blocks), 1)
        self.assertEqual(second_result.summary_blocks[0]["index"], 2)
        # 第二次压缩本身只读取新轨迹（不含旧摘要与此前轨迹渲染标记）。
        second_sources = [
            request.messages[-1].content
            for request in self.captured_requests[first_call_count:]
        ]
        self.assertTrue(any("第二批结果" in source for source in second_sources))
        self.assertTrue(all("段摘要：1" not in source for source in second_sources))
        self.assertTrue(all("【此前本轮摘要】" not in source for source in second_sources))
        # 拼接优先：新旧摘要同为标准分段结构时在本地直接合并，预算内不再
        # 消耗模型调用——虽然长来源可能被拆成多段，但不应额外向模型请求累计合并。
        self.assertTrue(all("【已有累计摘要】" not in source for source in second_sources))
        # 累计摘要只保留一个块，游标覆盖到最新工具结果
        markers = [
            msg for msg in second_result.messages
            if msg.get("_context_compaction_scope") == "active_round"
        ]
        self.assertEqual(len(markers), 1)
        self.assertEqual(markers[0]["_context_compaction_index"], 2)
        self.assertIn(first_result.summary_text, second_result.summary_text)
        self.assertNotEqual(first_result.summary_text, second_result.summary_text)
        self.assertEqual(second_result.compress_index, 2)

    async def test_single_round_cumulative_summary_keeps_old_segments(self):
        """单轮分块归并为一个累计摘要块。"""
        result = await compaction.compact_active_round_context_if_needed(
            self._round_message("请处理任务", ["关键输出" * 4000]),
            self._request("请处理任务"),
            settings=self._settings(),
            compression_index=1,
        )
        self.assertTrue(result.triggered)
        marker = result.messages[1]

        # 模拟上下文重建：第二段触发时同时出现第 1 段与第 2 段两个旧块
        second_messages = self._round_message("请处理任务", ["第二批结果" * 4000], block_marker=marker)
        second_messages.insert(2, {
            "role": "system",
            "content": "【本轮已执行工具摘要】\n段摘要：2",
            "_context_compaction_scope": "active_round",
            "_context_compaction_index": 2,
        })
        third_result = await compaction.compact_active_round_context_if_needed(
            second_messages,
            self._request("请处理任务"),
            settings=self._settings(),
            compression_index=3,
        )
        self.assertTrue(third_result.triggered)
        # 多次压缩归并为一个累计块，游标单调
        self.assertEqual(len(third_result.summary_blocks), 1)
        self.assertEqual(third_result.summary_blocks[0]["index"], 3)
        # 回传消息只含一个累计块，但其内容覆盖旧摘要和新轨迹
        markers = [
            msg for msg in third_result.messages
            if msg.get("_context_compaction_scope") == "active_round"
        ]
        self.assertEqual(len(markers), 1)
        self.assertEqual(markers[0]["_context_compaction_index"], 3)
        self.assertIn("段摘要：1", third_result.summary_text)
        self.assertIn("段摘要：2", third_result.summary_text)


class RecentQuestionsRetentionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._original_chat = compaction.ChatLLM.chat_completions
        self._original_resolver = compaction.resolve_context_compaction_model_config
        self._original_context_limit = compaction.resolve_model_max_input_tokens

        def fake_chat_completions(*, request=None, stream=False, model_config=None, **_kwargs):
            return {"content": "任务仍需继续；工具已返回关键结果。"}

        compaction.ChatLLM.chat_completions = staticmethod(fake_chat_completions)
        compaction.resolve_context_compaction_model_config = lambda _settings: {
            "maxInputTokens": 20_000,
            "apiType": "chat-completions",
        }
        compaction.resolve_model_max_input_tokens = lambda default=8192: 1_000

    def tearDown(self):
        compaction.ChatLLM.chat_completions = staticmethod(self._original_chat)
        compaction.resolve_context_compaction_model_config = self._original_resolver
        compaction.resolve_model_max_input_tokens = self._original_context_limit

    @staticmethod
    def _settings():
        return compaction.ContextCompactionSettings(
            trigger_ratio=0.5,
            summary_budget_ratio=0.2,
            oversized_reject_factor=1.5,
            max_oversized_rejections=3,
        )

    @staticmethod
    def _make_round(index):
        return {
            "event": "chat_round",
            "question": f"问题 {index}",
            "events": [
                {"role": "user", "content": f"问题 {index}"},
                {"role": "assistant", "content": "回答内容" * 150},
                {
                    "role": "tool",
                    "tool_name": "query",
                    "arguments": "{}",
                    "result": f"工具结果 {index}" * 80,
                },
                {"role": "assistant", "done": "[DONE]"},
            ],
        }

    async def test_compressed_rounds_questions_kept_in_summary_state(self):
        """保真索引只覆盖已压缩轮次，且受 keep_rounds 总窗口约束。"""
        memory = _MemoryStub([self._make_round(1), self._make_round(2), self._make_round(3)])
        request = ChatLLMRequest(
            session_id="questions_retention_test",
            messages=[{"role": "user", "content": "新问题"}],
            max_tokens=512,
        )
        compacted = await compaction.compact_session_history_if_needed(
            memory,
            request,
            settings=self._settings(),
        )
        self.assertGreater(compacted, 0)
        self.assertIsNotNone(memory.summary)
        # 未压缩的第 3 轮以完整对话回传（不再有轮次窗口限制）：已压缩轮次的
        # 问题进入保真索引（第 1、2 轮）
        self.assertEqual(memory.summary["recent_questions"], ["问题 1", "问题 2"])
        self.assertEqual(memory.summary["recent_question_numbers"], [1, 2])
        # 再次压缩后索引按新游标重算：已压缩轮次增加到第 3 轮
        memory.entries = [self._make_round(1), self._make_round(2), self._make_round(3), self._make_round(4)]
        await compaction.compact_session_history_if_needed(
            memory,
            request,
            settings=self._settings(),
        )
        self.assertIn("问题 3", memory.summary["recent_questions"])

    async def test_force_all_compacts_history_even_when_under_target_budget(self):
        memory = _MemoryStub([self._make_round(1), self._make_round(2), self._make_round(3)])
        request = ChatLLMRequest(
            session_id="force_all_test",
            messages=[{"role": "user", "content": "新问题"}],
            max_tokens=512,
        )
        compacted = await compaction.compact_session_history_if_needed(
            memory,
            request,
            settings=self._settings(),
            budget_tokens=100_000,
            enforce=True,
            force_all=True,
        )
        self.assertEqual(compacted, 3)
        self.assertEqual(memory.summary["source_round_count"], 3)
        self.assertEqual(len(memory.summary["blocks"]), 1)
        # 全部轮次已压缩：保真索引收录全部轮次问题（不再受轮次窗口限制）
        self.assertEqual(
            memory.summary["recent_questions"],
            ["问题 1", "问题 2", "问题 3"],
        )
        self.assertEqual(memory.summary["recent_question_numbers"], [1, 2, 3])

    def test_target_tokens_clamped_to_window_derived_range(self):
        """目标夹取：窗口 <500k → 下限 = 窗口×8%；窗口 ≥500k → 下限 40k；上限 = 窗口×40%。"""
        original_chat = compaction.resolve_model_max_input_tokens
        original_model = compaction.resolve_context_compaction_model_config
        try:
            # 小窗口（300k）：下限 = 300k × 8% = 24k，上限 = 300k × 40% = 120k
            compaction.resolve_model_max_input_tokens = lambda default=8192: 300_000
            compaction.resolve_context_compaction_model_config = lambda _s: {
                "maxInputTokens": 300_000, "apiType": "chat-completions"}
            settings = compaction.ContextCompactionSettings(
                trigger_ratio=0.5, summary_budget_ratio=0.2,
                oversized_reject_factor=1.5, max_oversized_rejections=3,
                target_tokens=80_000,
            )
            self.assertEqual(
                compaction.resolve_history_target_limits(settings), (24_000, 120_000))
            # 区间内 → 原值
            self.assertEqual(compaction.resolve_history_target_tokens(settings), 80_000)
            # 低于下限 → 按下限
            self.assertEqual(
                compaction.resolve_history_target_tokens(
                    dataclasses_replace(settings, target_tokens=1)),
                24_000,
            )
            # 高于上限 → 按上限
            self.assertEqual(
                compaction.resolve_history_target_tokens(
                    dataclasses_replace(settings, target_tokens=999_999)),
                120_000,
            )
            # 0 → 不设目标
            self.assertEqual(
                compaction.resolve_history_target_tokens(
                    dataclasses_replace(settings, target_tokens=0)),
                0,
            )

            # 大窗口（528k ≥ 500k）：下限 = 40k（固定），上限 = 528k × 40% = 211.2k
            compaction.resolve_model_max_input_tokens = lambda default=8192: 528_000
            compaction.resolve_context_compaction_model_config = lambda _s: {
                "maxInputTokens": 500_000, "apiType": "chat-completions"}
            lower, upper = compaction.resolve_history_target_limits(settings)
            self.assertEqual(lower, 40_000)
            self.assertEqual(upper, int(500_000 * 0.4))
            self.assertEqual(
                compaction.resolve_history_target_tokens(
                    dataclasses_replace(settings, target_tokens=1)),
                40_000,
            )
        finally:
            compaction.resolve_model_max_input_tokens = original_chat
            compaction.resolve_context_compaction_model_config = original_model

    def test_effective_summary_budget_shrinks_under_target(self):
        """设了目标后摘要预算按目标收紧（给保留的原始轮次留空间）。"""
        base = compaction.ContextCompactionSettings(
            trigger_ratio=0.8, summary_budget_ratio=0.2,
            oversized_reject_factor=1.5, max_oversized_rejections=3,
        )
        # 无目标：保持"窗口 × 比例"
        self.assertEqual(
            compaction.resolve_effective_summary_budget(base),
            compaction.resolve_summary_total_budget(base),
        )
        # 有目标：min(原预算, 目标 × 0.6)
        with_target = compaction.ContextCompactionSettings(
            trigger_ratio=0.8, summary_budget_ratio=0.2,
            oversized_reject_factor=1.5, max_oversized_rejections=3,
            target_tokens=80_000,
        )
        self.assertEqual(
            compaction.resolve_effective_summary_budget(with_target),
            min(compaction.resolve_summary_total_budget(with_target), 48_000),
        )

    @staticmethod
    def _make_big_round(index):
        """构造一个较大的轮次：上下文视图（user + assistant 正文）约 3k tokens。

        注意：已完成轮次的工具结果不回传给模型（摘要-only 模式），因此衡量
        "历史是否超目标"要看上下文视图，而不是原始工具输出。
        """
        return {
            "event": "chat_round",
            "question": f"问题 {index}",
            "events": [
                {"role": "user", "content": f"问题 {index}"},
                {"role": "assistant", "content": "回答内容片段" * 700},
                {
                    "role": "tool",
                    "tool_name": "query",
                    "arguments": "{}",
                    "result": "工具输出片段" * 500,
                },
                {"role": "assistant", "done": "[DONE]"},
            ],
        }



    async def test_target_tokens_pulls_history_below_target(self):
        """目标模式下历史被压到目标以内（不再有轮次窗口保护阻止压缩）。"""
        original_chat = compaction.resolve_model_max_input_tokens
        original_model = compaction.resolve_context_compaction_model_config
        try:
            # 大窗口（528k/500k）：下限 40k，上限 211.2k
            compaction.resolve_model_max_input_tokens = lambda default=8192: 528_000
            compaction.resolve_context_compaction_model_config = lambda _s: {
                "maxInputTokens": 500_000, "apiType": "chat-completions"}
            rounds = [self._make_big_round(index) for index in range(1, 41)]
            history_tokens = compaction.estimate_messages_tokens(
                [msg for entry in rounds
                 for msg in compaction.round_entry_to_context_messages(entry, -1)]
            )
            # 前置条件：上下文视图明显超过目标，压缩才有意义
            self.assertGreater(history_tokens, 40_000)
            memory = _MemoryStub(rounds)
            request = ChatLLMRequest(
                session_id="target_tokens_test",
                messages=[{"role": "user", "content": "新问题"}],
                max_tokens=512,
            )
            settings = compaction.ContextCompactionSettings(
                trigger_ratio=0.5, summary_budget_ratio=0.2,
                oversized_reject_factor=1.5, max_oversized_rejections=3,
                target_tokens=40_000,
            )
            compacted = await compaction.compact_session_history_if_needed(
                memory,
                request,
                settings=settings,
                budget_tokens=500_000,
                enforce=True,
            )
            self.assertGreater(compacted, 0)
            self.assertEqual(memory.summary["source_round_count"], compacted)
            # 压缩后历史上下文（摘要 + 保留轮次的上下文视图）应小于原始历史
            summary_text = compaction.render_context_summary(memory.summary) or ""
            retained = rounds[compacted:]
            retained_tokens = compaction.estimate_messages_tokens(
                [msg for entry in retained
                 for msg in compaction.round_entry_to_context_messages(entry, -1)]
            )
            self.assertLess(
                compaction.estimate_text_tokens(summary_text) + retained_tokens,
                history_tokens,
            )
        finally:
            compaction.resolve_model_max_input_tokens = original_chat
            compaction.resolve_context_compaction_model_config = original_model

    async def test_target_does_not_lower_trigger_threshold(self):
        """60k 目标不能让 0.85 × 500k 的自动压缩在 180k 历史时触发。"""
        original_chat = compaction.resolve_model_max_input_tokens
        original_model = compaction.resolve_context_compaction_model_config
        try:
            compaction.resolve_model_max_input_tokens = lambda default=8192: 528_000
            compaction.resolve_context_compaction_model_config = lambda _s: {
                "maxInputTokens": 500_000, "apiType": "chat-completions"}

            def fake_chat(*, stream=False, **_kwargs):
                if not stream:
                    return {"content": "任务仍需继续。"}

                async def frames():
                    yield 'data: {"content": "任务仍需继续。"}\n\n'
                    yield 'data: [DONE]\n\n'

                return frames()

            compaction.ChatLLM.chat_completions = staticmethod(fake_chat)
            settings = compaction.ContextCompactionSettings(
                trigger_ratio=0.85, summary_budget_ratio=0.15,
                oversized_reject_factor=1.5, max_oversized_rejections=3,
                target_tokens=60_000,
            )
            request = ChatLLMRequest(
                session_id="target_trigger_test",
                messages=[{"role": "user", "content": "新问题"}],
                max_tokens=512,
            )
            round_tokens = compaction.estimate_messages_tokens(
                compaction.round_entry_to_context_messages(self._make_big_round(1), -1)
            )
            below_count = max(1, 180_000 // round_tokens)
            memory = _MemoryStub([
                self._make_big_round(index) for index in range(1, below_count + 1)
            ])
            self.assertGreater(below_count * round_tokens, 60_000)
            self.assertLess(below_count * round_tokens, 425_000)
            events = []

            async def emit(payload):
                events.append(payload)

            self.assertEqual(
                await compaction.compact_session_history_if_needed(
                    memory, request, settings=settings, event_emitter=emit,
                ),
                0,
            )
            self.assertIsNone(memory.summary)
            self.assertFalse(events)
            self.assertEqual(
                await compaction.compact_session_history_if_needed(
                    memory, request, settings=settings, budget_tokens=60_000,
                    trigger_reason="post",
                ),
                0,
            )

            above_count = 450_000 // round_tokens + 1
            memory.entries = [
                self._make_big_round(index) for index in range(1, above_count + 1)
            ]
            self.assertGreater(
                await compaction.compact_session_history_if_needed(
                    memory, request, settings=settings, event_emitter=emit,
                    trigger_reason="post",
                ),
                0,
            )
            done = next(event for event in events if event.get("phase") == "done")
            self.assertGreater(done["before_tokens"], 425_000)
            self.assertEqual(done["trigger_threshold"], 425_000)
            self.assertEqual(done["token_limit"], 60_000)
            self.assertEqual(done["budget_scope"], "history")
            self.assertEqual(done["target_tokens"], 60_000)
        finally:
            compaction.resolve_model_max_input_tokens = original_chat
            compaction.resolve_context_compaction_model_config = original_model

    async def test_target_tokens_skips_when_history_already_below_target(self):
        """历史本身已低于目标时不触发压缩（目标只是上限，不是"必须压"）。"""
        original_chat = compaction.resolve_model_max_input_tokens
        original_model = compaction.resolve_context_compaction_model_config
        try:
            compaction.resolve_model_max_input_tokens = lambda default=8192: 528_000
            compaction.resolve_context_compaction_model_config = lambda _s: {
                "maxInputTokens": 500_000, "apiType": "chat-completions"}
            rounds = [self._make_round(index) for index in range(1, 9)]
            memory = _MemoryStub(rounds)
            request = ChatLLMRequest(
                session_id="target_noop_test",
                messages=[{"role": "user", "content": "新问题"}],
                max_tokens=512,
            )
            # 目标 = 上限（211.2k），远大于当前历史 → 不压缩
            settings = compaction.ContextCompactionSettings(
                trigger_ratio=0.5, summary_budget_ratio=0.2,
                oversized_reject_factor=1.5, max_oversized_rejections=3,
                target_tokens=200_000,
            )
            compacted = await compaction.compact_session_history_if_needed(
                memory,
                request,
                settings=settings,
                budget_tokens=500_000,
                enforce=True,
            )
            self.assertEqual(compacted, 0)
            self.assertIsNone(memory.summary)
        finally:
            compaction.resolve_model_max_input_tokens = original_chat
            compaction.resolve_context_compaction_model_config = original_model


if __name__ == "__main__":
    unittest.main()

