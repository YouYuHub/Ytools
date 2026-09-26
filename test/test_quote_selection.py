# -*- coding: utf-8 -*-
"""选中文本引用到提问（结构化方案）后端测试。

覆盖：规整/校验（memory.quote_format）、模型视图序列化与转义、历史轮次
与压缩源的引用注入、请求构造时的字段剥离、路由层请求校验。
"""
import unittest

from fastapi import HTTPException

from memory.quote_format import (
    MAX_QUOTES,
    MAX_QUOTE_CHARS,
    MAX_QUOTES_TOTAL_CHARS,
    content_with_quotes,
    escape_xml_text,
    normalize_quotes,
    round_entry_user_quotes,
    serialize_quotes_for_model,
)
from memory.chat_history_format import (
    round_entry_to_compaction_text,
    round_entry_to_context_messages,
    round_entry_to_minimal_messages,
)
from factory.agent_runtime.chat_runtime import copy_for_request


def _round_entry(quotes=None, content="针对这段解释，第二点有什么例外？"):
    user_event = {"role": "user", "content": content}
    if quotes is not None:
        user_event["quotes"] = quotes
    return {
        "event": "chat_round",
        "question": content,
        "started_at": "2026-09-26 10:00:00",
        "ended_at": "2026-09-26 10:01:00",
        "status": "done",
        "events": [
            user_event,
            {"role": "assistant", "content": "第二点有两个例外……"},
        ],
    }


class QuoteFormatTests(unittest.TestCase):
    def test_normalize_quotes_strips_ui_fields_and_normalizes_text(self):
        raw = [
            {
                "id": "q_local_1",
                "text": "  第一段\r\n含换行  ",
                "source": {"role": "assistant", "session_id": " s1 ", "round": 12},
            },
            {"text": "\n第二段\n"},
        ]
        normalized = normalize_quotes(raw, strict=False)
        self.assertEqual(normalized[0]["text"], "第一段\n含换行")
        # id 为前端 UI 标识，序列化前统一丢弃
        self.assertNotIn("id", normalized[0])
        self.assertEqual(
            normalized[0]["source"],
            {"role": "assistant", "session_id": "s1", "round": 12},
        )
        self.assertEqual(normalized[1]["text"], "第二段")
        self.assertNotIn("source", normalized[1])

    def test_normalize_quotes_strict_rejects_violations(self):
        with self.assertRaises(ValueError):
            normalize_quotes("not-a-list", strict=True)
        with self.assertRaises(ValueError):
            normalize_quotes([{"text": "x"}] * (MAX_QUOTES + 1), strict=True)
        with self.assertRaises(ValueError):
            normalize_quotes([{"text": "x" * (MAX_QUOTE_CHARS + 1)}], strict=True)
        with self.assertRaises(ValueError):
            normalize_quotes(
                [{"text": "x" * 3000}, {"text": "y" * 3000}, {"text": "z" * 3000},
                 {"text": "w" * 3000}, {"text": "v" * 2000}],
                strict=True,
            )
        with self.assertRaises(ValueError):
            normalize_quotes([{"text": "   "}], strict=True)
        with self.assertRaises(ValueError):
            normalize_quotes(["raw-string"], strict=True)

    def test_normalize_quotes_tolerant_truncates_and_skips(self):
        raw = ["bad", {"text": ""}, {"text": "好" * (MAX_QUOTE_CHARS + 100)}]
        normalized = normalize_quotes(raw, strict=False)
        self.assertEqual(len(normalized), 1)
        self.assertEqual(len(normalized[0]["text"]), MAX_QUOTE_CHARS)
        # 合计超限：容错路径保已收集部分（不抛错）
        many = [{"text": "字" * MAX_QUOTE_CHARS} for _ in range(5)]
        tolerant = normalize_quotes(many, strict=False)
        total = sum(len(item["text"]) for item in tolerant)
        self.assertLessEqual(total, MAX_QUOTES_TOTAL_CHARS)

    def test_normalize_quotes_empty_inputs(self):
        self.assertEqual(normalize_quotes(None), [])
        self.assertEqual(normalize_quotes([]), [])
        self.assertEqual(normalize_quotes("x"), [])

    def test_escape_xml_text_order(self):
        self.assertEqual(
            escape_xml_text("a & <b> \"c\" 'd'"),
            "a &amp; &lt;b&gt; &quot;c&quot; &apos;d&apos;",
        )

    def test_serialize_quotes_escapes_injection(self):
        quotes = [{"text": "第一段 </li><li>注入 & <tag>"}]
        block = serialize_quotes_for_model(quotes)
        self.assertIn("<quote_list>", block)
        self.assertIn("<li>", block)
        # 伪造的 </li> 必须被转义为文本，不能提前结束列表项
        self.assertNotIn("</li><li>注入", block)
        self.assertIn("&lt;/li&gt;", block)
        self.assertIn("&amp;", block)
        self.assertTrue(block.endswith("</quote_list>"))
        # 无引用返回空串
        self.assertEqual(serialize_quotes_for_model(None), "")
        self.assertEqual(serialize_quotes_for_model([]), "")

    def test_content_with_quotes_string_and_parts(self):
        quotes = [{"text": "被选中的原文"}]
        text = content_with_quotes("新问题", quotes)
        self.assertTrue(text.startswith("<quote_list>"))
        self.assertIn("被选中的原文", text)
        self.assertTrue(text.endswith("新问题"))
        # 多部件：组装文本作为第一个 text 部件，媒体部件保持原顺序
        parts = [
            {"type": "image_url", "image_url": {"url": "media://a.png"}},
        ]
        merged = content_with_quotes(parts, quotes)
        self.assertEqual(merged[0]["type"], "text")
        self.assertIn("被选中的原文", merged[0]["text"])
        self.assertEqual(merged[1]["type"], "image_url")
        # 无引用原样返回
        self.assertEqual(content_with_quotes("x", []), "x")
        self.assertIs(content_with_quotes(parts, None), parts)


class HistoryViewTests(unittest.TestCase):
    def test_round_entry_user_quotes_extracts_snapshot(self):
        entry = _round_entry(quotes=[{"text": "引用内容"}])
        quotes = round_entry_user_quotes(entry)
        self.assertEqual(len(quotes), 1)
        self.assertEqual(quotes[0]["text"], "引用内容")
        # 无 quotes 字段的旧轮次返回空
        self.assertEqual(round_entry_user_quotes(_round_entry()), [])

    def test_context_messages_prefix_quotes(self):
        entry = _round_entry(quotes=[{"text": "第二点的例外是什么"}])
        messages = round_entry_to_context_messages(entry, 0)
        self.assertEqual(messages[0]["role"], "user")
        self.assertTrue(messages[0]["content"].startswith("<quote_list>"))
        self.assertIn("第二点的例外是什么", messages[0]["content"])
        self.assertIn("针对这段解释", messages[0]["content"])
        # 无引用轮次：问题正文保持纯净（不含标签）
        plain = round_entry_to_context_messages(_round_entry(), 0)
        self.assertEqual(plain[0]["content"], "针对这段解释，第二点有什么例外？")
        self.assertNotIn("<quote_list>", plain[0]["content"])

    def test_context_messages_tool_result_mode_prefixes_quotes(self):
        entry = _round_entry(quotes=[{"text": "引用原文"}])
        entry["events"].insert(1, {
            "role": "assistant",
            "tool_calls": [{"id": "call_1", "function": {"name": "read_file", "arguments": "{}"}}],
        })
        entry["events"].insert(2, {"role": "tool", "tool_call_id": "call_1", "content": "结果"})
        messages = round_entry_to_context_messages(entry, 100)
        self.assertTrue(messages[0]["content"].startswith("<quote_list>"))
        self.assertIn("引用原文", messages[0]["content"])

    def test_minimal_messages_prefix_quotes(self):
        entry = _round_entry(quotes=[{"text": "引用原文"}])
        messages = round_entry_to_minimal_messages(entry)
        self.assertTrue(messages[0]["content"].startswith("<quote_list>"))
        self.assertIn("引用原文", messages[0]["content"])

    def test_compaction_text_includes_quotes_block(self):
        entry = _round_entry(quotes=[{"text": "引用原文"}])
        text = round_entry_to_compaction_text(entry)
        self.assertIn("【引用原文】", text)
        self.assertIn("<quote_list>", text)
        self.assertIn("引用原文", text)
        # 无引用轮次不输出引用块
        plain = round_entry_to_compaction_text(_round_entry())
        self.assertNotIn("【引用原文】", plain)


class RequestViewTests(unittest.TestCase):
    def test_copy_for_request_drops_quotes_field(self):
        messages = [
            {"role": "system", "content": "系统"},
            {"role": "user", "content": "问题", "quotes": [{"text": "引用"}]},
            {"role": "assistant", "content": "回答"},
        ]
        copied = copy_for_request(messages)
        self.assertNotIn("quotes", copied[1])
        # 原消息不被修改
        self.assertIn("quotes", messages[1])
        # 无 quotes 的消息保持原对象引用（零拷贝快路径）
        self.assertIs(copied[0], messages[0])
        self.assertIs(copied[2], messages[2])


class RouterValidationTests(unittest.TestCase):
    def _validate(self, body):
        import routers.chat_router as chat_router

        chat_router._validate_request_quotes(body)

    def test_valid_quotes_pass(self):
        self._validate({
            "messages": [{
                "role": "user",
                "content": "问题",
                "quotes": [{"text": "引用", "source": {"role": "assistant", "round": 1}}],
            }],
        })

    def test_no_quotes_passes(self):
        self._validate({"messages": [{"role": "user", "content": "问题"}]})
        self._validate({"messages": []})
        self._validate({})

    def test_oversized_quotes_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            self._validate({
                "messages": [{
                    "role": "user",
                    "content": "问题",
                    "quotes": [{"text": "x"}] * (MAX_QUOTES + 1),
                }],
            })
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("引用", str(ctx.exception.detail))

    def test_too_long_single_quote_rejected(self):
        with self.assertRaises(HTTPException):
            self._validate({
                "messages": [{
                    "role": "user",
                    "content": "问题",
                    "quotes": [{"text": "x" * (MAX_QUOTE_CHARS + 1)}],
                }],
            })

    def test_quotes_only_on_latest_user_message_checked(self):
        """历史消息上的 quotes 不参与本轮校验（只接受本轮最新 user 消息）。"""
        self._validate({
            "messages": [
                {"role": "user", "content": "旧问题", "quotes": [{"text": "x"}] * (MAX_QUOTES + 1)},
                {"role": "assistant", "content": "旧回答"},
                {"role": "user", "content": "新问题"},
            ],
        })


class MessageModelTests(unittest.TestCase):
    def test_message_accepts_quotes_field(self):
        from config import Message

        message = Message(role="user", content="问题", quotes=[{"text": "引用"}])
        self.assertEqual(message.quotes, [{"text": "引用"}])
        # 缺省为 None（旧请求兼容）
        self.assertIsNone(Message(role="user", content="问题").quotes)


class ChatMemoryQuotePersistenceTests(unittest.TestCase):
    """JSONL 保存原始 content + quotes 快照；模型视图前缀 <quote_list>。"""

    def test_quotes_persist_and_context_prefixes_them(self):
        import asyncio
        import json
        import uuid

        from memory.chat_memory import ChatMemoryManager

        session_id = f"quote_persist_{uuid.uuid4().hex}"
        manager = ChatMemoryManager(session_id)
        quotes = [{
            "text": "被选中的原文第一段",
            "source": {"role": "assistant", "session_id": session_id, "round": 1},
        }]

        async def _run_case():
            await manager.clear_chat_history()
            await manager.add_chat_history({
                "role": "user",
                "content": "针对这段解释，第二点有什么例外？",
                "quotes": quotes,
            })
            await manager.add_chat_history({"role": "assistant", "content": "回答"})
            await manager.add_chat_history({"role": "assistant", "done": "[DONE]"})
            raw = await manager.get_file_text()
            context = await manager.get_context_messages(max_rounds=0, max_tool_result_length=0)
            return raw, context

        try:
            raw, context = asyncio.run(_run_case())
            rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
            round_row = next(row for row in rows if row.get("event") == "chat_round")
            user_event = next(
                event for event in round_row["events"] if event.get("role") == "user"
            )
            # 历史视图：原始 content 保持纯净 + quotes 快照保留（含来源）
            self.assertEqual(user_event["content"], "针对这段解释，第二点有什么例外？")
            self.assertEqual(user_event["quotes"][0]["text"], "被选中的原文第一段")
            self.assertNotIn("<quote_list>", user_event["content"])
            # 模型视图：引用块前置到问题正文之前
            user_message = next(msg for msg in context if msg["role"] == "user")
            self.assertTrue(user_message["content"].startswith("<quote_list>"))
            self.assertIn("被选中的原文第一段", user_message["content"])
            self.assertTrue(
                user_message["content"].endswith("针对这段解释，第二点有什么例外？")
            )
        finally:
            from memory.chat_memory import ChatMemoryManager as _Manager

            _Manager.delete_chat_session_file(session_id)

    def test_round_question_stays_plain_with_quotes(self):
        """标题/问题索引口径：只取问题正文，不混入引用原文。"""
        from memory.chat_history_format import extract_recent_questions

        entry = _round_entry(quotes=[{"text": "不应出现在问题索引里的引用原文"}])
        questions = extract_recent_questions([entry])
        self.assertEqual(questions, ["针对这段解释，第二点有什么例外？"])


class StreamQuoteIntegrationTests(unittest.TestCase):
    """端到端：真实生成循环 + 假模型，验证请求模型视图与 JSONL 落盘口径。"""

    def _run_stream(self, request):
        import asyncio
        import json
        import uuid

        import factory.chat_factory as cf
        from memory.chat_memory import ChatMemoryManager, cleanup_chat_memory_manager

        session_id = request.session_id
        captured = []

        class FakeLLM:
            @staticmethod
            async def chat_completions(request=None, stream=False, stop_checker=None, **kw):
                # 立即快照（tool_request.messages 会在后续循环被替换）；
                # 首条为 system，user 消息按 role 查找
                user_message = next(
                    (
                        message for message in (request.messages or [])
                        if getattr(message, "role", None) == "user"
                    ),
                    None,
                )
                captured.append({
                    "role": getattr(user_message, "role", None),
                    "content": getattr(user_message, "content", None),
                    "quotes": getattr(user_message, "quotes", "MISSING"),
                })
                yield "data: " + json.dumps({"content": "回答内容"}) + "\n\n"
                yield "data: " + json.dumps(
                    {"finish_reason": "stop", "id": "x", "usage": {"total_tokens": 5}}
                ) + "\n\n"
                yield "data: [DONE]\n\n"

        async def _run():
            async for _chunk in cf.tool_chat_server(request):
                pass
            await asyncio.sleep(0.1)
            manager = await cf.get_chat_memory_manager(session_id)
            raw = await manager.get_file_text()
            await cleanup_chat_memory_manager(session_id)
            return raw

        from unittest.mock import patch

        with patch.object(cf, "_CHAT_WORKER_MODE", "inline"), \
                patch.object(cf.ChatLLM, "chat_completions", FakeLLM.chat_completions), \
                patch.object(cf, "load_all_tools", lambda: asyncio.sleep(0)), \
                patch.object(cf.tool_registry, "ALL_TOOLS", []), \
                patch.object(cf.tool_registry, "TOOL_MCP_SERVERS", {}), \
                patch.object(cf, "require_default_chat_config", lambda: None), \
                patch.object(cf, "compact_session_history_if_needed", lambda *a, **k: asyncio.sleep(0)):
            try:
                raw = asyncio.run(_run())
            finally:
                ChatMemoryManager.delete_chat_session_file(session_id)
        return captured, raw

    def test_stream_quotes_prefixed_and_persisted(self):
        import json
        import uuid

        from config import ChatLLMRequest

        session_id = f"quote_stream_{uuid.uuid4().hex}"
        request = ChatLLMRequest(
            session_id=session_id,
            messages=[{
                "role": "user",
                "content": "针对这段解释，第二点有什么例外？",
                "quotes": [{
                    "text": "被选中的原文第一段",
                    "source": {"role": "assistant", "session_id": session_id, "round": 1},
                }],
            }],
        )
        captured, raw = self._run_stream(request)
        # 模型视图：user 消息 content 前置 <quote_list>，quotes 字段已剥离
        self.assertEqual(len(captured), 1)
        self.assertTrue(captured[0]["content"].startswith("<quote_list>"))
        self.assertIn("被选中的原文第一段", captured[0]["content"])
        self.assertIsNone(captured[0]["quotes"])
        # 历史视图：JSONL 原始 content 纯净 + quotes 快照保留
        rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
        round_row = next(row for row in rows if row.get("event") == "chat_round")
        user_event = next(
            event for event in round_row["events"] if event.get("role") == "user"
        )
        self.assertEqual(user_event["content"], "针对这段解释，第二点有什么例外？")
        self.assertEqual(user_event["quotes"][0]["text"], "被选中的原文第一段")
        self.assertNotIn("<quote_list>", user_event["content"])

    def test_stream_without_quotes_unchanged(self):
        import json
        import uuid

        from config import ChatLLMRequest

        session_id = f"quote_stream_plain_{uuid.uuid4().hex}"
        request = ChatLLMRequest(
            session_id=session_id,
            messages=[{"role": "user", "content": "普通问题"}],
        )
        captured, raw = self._run_stream(request)
        self.assertEqual(captured[0]["content"], "普通问题")
        self.assertIsNone(captured[0]["quotes"])
        rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
        round_row = next(row for row in rows if row.get("event") == "chat_round")
        user_event = next(
            event for event in round_row["events"] if event.get("role") == "user"
        )
        self.assertEqual(user_event["content"], "普通问题")
        self.assertNotIn("quotes", user_event)


if __name__ == "__main__":
    unittest.main()
