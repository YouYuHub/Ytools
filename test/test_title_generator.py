"""会话标题自动生成（factory/agent_runtime/title_generator.py）单元测试。

覆盖：
- TitleSourceCollector：思考/正文采集、100 字符短路、快照取较长者、短流回退全文；
- build_title_source / _clean_title_text：资料组装与输出清洗；
- resolve_title_model_config：未配置/不存在/协议不符回退、有效配置解析；
- maybe_generate_session_title：已生成标记拦截、未配置静默回退、成功生成、调用失败回退；
- apply_generated_title：写盘 title + _title_state，recompute 首问兜底不再覆盖，
  手动重命名（update_session_title）不被后续 recompute 恢复为旧机制标题；
- schedule_title_generation：后台任务触发与异常兜底。
"""

import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from memory import chat_memory
from factory.agent_runtime import title_generator


class TitleSourceCollectorTests(unittest.TestCase):
    def test_snapshot_prefers_longer_source(self):
        collector = title_generator.TitleSourceCollector()
        collector.feed_reasoning("思考" * 25)          # 50 字
        collector.feed_content("正文内容" * 34)         # 136 字 → 超限
        snapshot = collector.snapshot()
        self.assertEqual(snapshot, ("正文内容" * 34)[:100])

    def test_short_stream_returns_full_content(self):
        collector = title_generator.TitleSourceCollector()
        collector.feed_content("  短回答  ")
        collector.feed_reasoning("短思考")
        # 正文与思考等长取正文；strip 后无首尾空白
        self.assertEqual(collector.snapshot(), "短回答")

    def test_short_circuits_after_limit(self):
        collector = title_generator.TitleSourceCollector()
        collector.feed_content("a" * 100)
        collector.feed_content("b" * 500)   # 已满：内部短路，不再累积
        self.assertEqual(collector.snapshot(), "a" * 100)

    def test_non_string_delta_ignored(self):
        collector = title_generator.TitleSourceCollector()
        collector.feed_content(None)
        collector.feed_reasoning(123)
        self.assertEqual(collector.snapshot(), "")

    def test_empty_snapshot(self):
        self.assertEqual(title_generator.TitleSourceCollector().snapshot(), "")


class BuildTitleContentTests(unittest.TestCase):
    def test_text_only_with_preview(self):
        content = title_generator.build_title_content("帮我写个爬虫", "好的，我来帮你" + "x" * 95)
        self.assertIsInstance(content, str)
        self.assertIn("【用户问题】", content)
        self.assertIn("帮我写个爬虫", content)
        self.assertIn("【模型输出内容前100字符】", content)
        self.assertIn("好的，我来帮你", content)

    def test_without_preview_only_question(self):
        content = title_generator.build_title_content("问题", "")
        self.assertIn("【用户问题】", content)
        self.assertNotIn("【模型输出内容前100字符】", content)

    def test_empty_question_placeholder(self):
        content = title_generator.build_title_content("", "")
        self.assertIn("（无文本，可能为多模态消息）", content)

    def test_long_question_truncated(self):
        content = title_generator.build_title_content("问" * 500, "")
        question_part = content.split("【用户问题】\n", 1)[1]
        self.assertEqual(len(question_part), 300)

    def test_media_without_session_falls_back_text(self):
        parts = [{"type": "image_url", "image_url": {"url": "media://a.png"}}]
        content = title_generator.build_title_content("看图", "", question_parts=parts)
        self.assertIsInstance(content, str)

    def test_vision_disabled_media_becomes_placeholder_parts(self):
        parts = [
            {"type": "text", "text": "根据截图优化"},
            {"type": "image_url", "image_url": {"url": "media://a.png"}},
        ]
        with mock.patch("memory.file_memory.resolve_media_content_parts",
                        return_value=([{"type": "text", "text": "[图片 media://a.png]"}], [])):
            content = title_generator.build_title_content(
                "根据截图优化", "", question_parts=parts,
                session_id="s1", vision_enabled=False,
            )
        self.assertIsInstance(content, list)
        texts = [p.get("text", "") for p in content if p.get("type") == "text"]
        self.assertTrue(any("【用户问题】" in t for t in texts))
        self.assertTrue(any("[图片 media://a.png]" in t for t in texts))

    def test_vision_enabled_resolves_media_to_parts(self):
        parts = [
            {"type": "text", "text": "看图"},
            {"type": "image_url", "image_url": {"url": "media://a.png"}},
        ]
        resolved_media = {"type": "image_url", "image_url": {"url": "data:image/png;base64,xxx"}}
        with mock.patch("memory.file_memory.resolve_media_content_parts",
                        return_value=([resolved_media], [])):
            content = title_generator.build_title_content(
                "看图", "", question_parts=parts,
                session_id="s1", vision_enabled=True,
            )
        self.assertIsInstance(content, list)
        # 文本说明部件在前，媒体部件（解析后的）在后
        self.assertEqual(content[0]["type"], "text")
        self.assertIn("【用户问题】", content[0]["text"])
        self.assertIn(resolved_media, content[1:])
        # 原始部件不被修改（media:// 形态保留）
        self.assertEqual(parts[1]["image_url"]["url"], "media://a.png")

    def test_media_resolve_error_falls_back_text(self):
        parts = [{"type": "image_url", "image_url": {"url": "media://a.png"}}]
        with mock.patch("memory.file_memory.resolve_media_content_parts",
                        side_effect=RuntimeError("解析崩了")):
            content = title_generator.build_title_content(
                "看图", "", question_parts=parts,
                session_id="s1", vision_enabled=True,
            )
        self.assertIsInstance(content, str)
        self.assertIn("【用户问题】", content)

    def test_text_only_parts_skip_media_resolution(self):
        parts = [{"type": "text", "text": "纯文本"}]
        with mock.patch("memory.file_memory.resolve_media_content_parts") as resolver:
            content = title_generator.build_title_content(
                "问题", "", question_parts=parts,
                session_id="s1", vision_enabled=True,
            )
        self.assertIsInstance(content, str)
        resolver.assert_not_called()


class CleanTitleTextTests(unittest.TestCase):
    def test_strip_quotes_and_prefix(self):
        self.assertEqual(title_generator._clean_title_text('"标题：测试标题"'), "测试标题")
        self.assertEqual(title_generator._clean_title_text("“标题: 另一个”"), "另一个")
        self.assertEqual(title_generator._clean_title_text("Title: Hello"), "Hello")

    def test_first_nonempty_line(self):
        self.assertEqual(
            title_generator._clean_title_text("\n\n  主标题  \n解释文字\n"),
            "主标题",
        )

    def test_hard_truncate(self):
        result = title_generator._clean_title_text("字" * 100)
        self.assertEqual(result, "字" * 40)

    def test_empty_input(self):
        self.assertEqual(title_generator._clean_title_text(""), "")
        self.assertEqual(title_generator._clean_title_text(None), "")


class ResolveTitleModelConfigTests(unittest.TestCase):
    def test_not_configured(self):
        with mock.patch.object(title_generator, "get_role_selection", return_value={
            "ownership_name": None, "model_name": None, "parameter": {}, "api_type": None,
        }):
            config, reason = title_generator.resolve_title_model_config()
        self.assertIsNone(config)
        self.assertIn("未配置", reason)

    def test_valid_config(self):
        model_config = {"apiType": "chat-completions", "selected_model_id": "m"}
        with mock.patch.object(title_generator, "get_role_selection", return_value={
            "ownership_name": "P", "model_name": "M", "parameter": {}, "api_type": "chat_completions",
        }), mock.patch.object(title_generator, "get_model_config", return_value=model_config), \
            mock.patch.object(title_generator, "get_role_headers", return_value=[]):
            config, reason = title_generator.resolve_title_model_config()
        # 空 headers 时返回配置浅拷贝（副本注入语义），等值即可
        self.assertEqual(config, model_config)
        self.assertEqual(reason, "")

    def test_missing_config(self):
        with mock.patch.object(title_generator, "get_role_selection", return_value={
            "ownership_name": "P", "model_name": "M", "parameter": {}, "api_type": None,
        }), mock.patch.object(title_generator, "get_model_config", return_value=None):
            config, _ = title_generator.resolve_title_model_config()
        self.assertIsNone(config)

    def test_wrong_protocol(self):
        with mock.patch.object(title_generator, "get_role_selection", return_value={
            "ownership_name": "P", "model_name": "M", "parameter": {}, "api_type": "messages",
        }), mock.patch.object(title_generator, "get_model_config", return_value={"apiType": "messages"}):
            config, reason = title_generator.resolve_title_model_config()
        self.assertIsNone(config)
        self.assertIn("chat-completions", reason)


class MaybeGenerateSessionTitleTests(unittest.IsolatedAsyncioTestCase):
    async def test_skips_when_already_generated(self):
        with mock.patch("memory.chat_memory.read_session_meta_value", return_value={
            "title_generated": True,
        }) as fake_read, mock.patch.object(title_generator, "resolve_title_model_config") as resolve:
            result = await title_generator.maybe_generate_session_title("s1", "q", "p")
        self.assertIsNone(result)
        resolve.assert_not_called()
        # 标记检查发生在配置解析之前（未配置模型也不产生额外读取）
        self.assertEqual(
            mock.call("s1", "_title_state"),
            fake_read.call_args,
        )

    async def test_not_configured_keeps_legacy(self):
        with mock.patch("memory.chat_memory.read_session_meta_value", return_value=None), \
            mock.patch.object(
                title_generator, "resolve_title_model_config", return_value=(None, "未配置标题模型")
        ), mock.patch.object(title_generator.ChatLLM, "chat_completions") as llm:
            result = await title_generator.maybe_generate_session_title("s1", "q", "p")
        self.assertIsNone(result)
        llm.assert_not_called()

    async def test_generates_and_cleans_title(self):
        fake_config = {"apiType": "chat-completions", "selected_model_name": "测试模型"}
        captured = {}

        def fake_chat_completions(*, request=None, stream=False, model_config=None, **_kwargs):
            captured["request"] = request
            captured["model_config"] = model_config
            return {"content": '"标题：测试会话标题"', "usage": {}}

        with mock.patch("memory.chat_memory.read_session_meta_value", return_value=None), \
            mock.patch.object(
                title_generator, "resolve_title_model_config", return_value=(fake_config, "")
        ), mock.patch.object(title_generator.ChatLLM, "chat_completions",
                             side_effect=fake_chat_completions):
            result = await title_generator.maybe_generate_session_title("s1", "问题", "回答前100字")
        self.assertEqual(result, "测试会话标题")
        self.assertEqual(captured["model_config"], fake_config)
        self.assertFalse(captured["request"].stream)
        self.assertIsNone(captured["request"].tool_choice)
        # 标题请求资料包含用户问题与输出预览（messages 为 pydantic Message 模型）
        user_text = captured["request"].messages[-1].content
        self.assertIn("问题", user_text)
        self.assertIn("回答前100字", user_text)

    async def test_model_error_returns_none(self):
        def broken_chat_completions(**_kwargs):
            raise RuntimeError("网络故障")

        with mock.patch("memory.chat_memory.read_session_meta_value", return_value=None), \
            mock.patch.object(
                title_generator, "resolve_title_model_config", return_value=({"apiType": "chat-completions"}, "")
        ), mock.patch.object(title_generator.ChatLLM, "chat_completions",
                             side_effect=broken_chat_completions):
            result = await title_generator.maybe_generate_session_title("s1", "q", "p")
        self.assertIsNone(result)

    async def test_empty_model_output_returns_none(self):
        with mock.patch("memory.chat_memory.read_session_meta_value", return_value=None), \
            mock.patch.object(
                title_generator, "resolve_title_model_config", return_value=({"apiType": "chat-completions"}, "")
        ), mock.patch.object(title_generator.ChatLLM, "chat_completions",
                             return_value={"content": "   "}):
            result = await title_generator.maybe_generate_session_title("s1", "q", "p")
        self.assertIsNone(result)

    async def test_vision_title_model_receives_media_parts(self):
        """标题模型支持视觉且首问含媒体：user content 为部件列表（媒体解析）。"""
        fake_config = {"apiType": "chat-completions", "vision": True}
        captured = {}
        media_part = {"type": "image_url", "image_url": {"url": "media://a.png"}}
        resolved_media = {"type": "image_url", "image_url": {"url": "data:image/png;base64,xxx"}}

        def fake_chat_completions(*, request=None, **_kwargs):
            captured["request"] = request
            return {"content": "带图标题"}

        with mock.patch("memory.chat_memory.read_session_meta_value", return_value=None), \
            mock.patch.object(
                title_generator, "resolve_title_model_config", return_value=(fake_config, "")
        ), mock.patch("memory.file_memory.resolve_media_content_parts",
                      return_value=([resolved_media], [])) as resolver, \
            mock.patch.object(title_generator.ChatLLM, "chat_completions",
                              side_effect=fake_chat_completions):
            result = await title_generator.maybe_generate_session_title(
                "s1", "看图", "好的", question_parts=[media_part],
            )
        self.assertEqual(result, "带图标题")
        resolver.assert_called_once_with("s1", [media_part], vision_enabled=True)
        user_content = captured["request"].messages[-1].content
        self.assertIsInstance(user_content, list)
        self.assertIn(resolved_media, user_content)

    async def test_non_vision_title_model_sends_placeholder_parts(self):
        """标题模型不支持视觉：媒体按 vision=False 转文本占位（不发 base64）。"""
        fake_config = {"apiType": "chat-completions", "vision": False}
        captured = {}
        media_part = {"type": "image_url", "image_url": {"url": "media://a.png"}}

        def fake_chat_completions(*, request=None, **_kwargs):
            captured["request"] = request
            return {"content": "占位标题"}

        with mock.patch("memory.chat_memory.read_session_meta_value", return_value=None), \
            mock.patch.object(
                title_generator, "resolve_title_model_config", return_value=(fake_config, "")
        ), mock.patch("memory.file_memory.resolve_media_content_parts",
                      return_value=([{"type": "text", "text": "[图片 media://a.png]"}], [])) as resolver, \
            mock.patch.object(title_generator.ChatLLM, "chat_completions",
                              side_effect=fake_chat_completions):
            result = await title_generator.maybe_generate_session_title(
                "s1", "看图", "好的", question_parts=[media_part],
            )
        self.assertEqual(result, "占位标题")
        resolver.assert_called_once_with("s1", [media_part], vision_enabled=False)
        user_content = captured["request"].messages[-1].content
        self.assertIsInstance(user_content, list)
        self.assertTrue(
            any("[图片 media://a.png]" in str(p.get("text", "")) for p in user_content)
        )


class ApplyGeneratedTitleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._orig_root = chat_memory.HISTORY_ROOT
        chat_memory.HISTORY_ROOT = Path(self._tmp)
        self.session_id = "title_apply_test"

    def tearDown(self):
        chat_memory.HISTORY_ROOT = self._orig_root
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _manager(self):
        return chat_memory.ChatMemoryManager(self.session_id)

    async def test_apply_writes_title_and_state(self):
        manager = self._manager()
        await manager.add_chat_history({"role": "user", "content": "第一个问题"})
        await manager.add_chat_history({"role": "assistant", "done": "[DONE]"})
        meta = await manager.apply_generated_title(
            "生成的标题", {"source": "title_model", "model": "m1", "applied_at": "t"}
        )
        self.assertEqual(meta["title"], "生成的标题")
        self.assertTrue(meta["_title_state"]["title_generated"])
        self.assertEqual(meta["_title_state"]["model"], "m1")

    async def test_generated_title_survives_recompute(self):
        manager = self._manager()
        await manager.add_chat_history({"role": "user", "content": "第一个问题"})
        await manager.apply_generated_title("生成的标题", None)
        # 下一轮任务收尾 recompute（模拟后续轮次追加）：旧机制首问 40 字兜底不得覆盖
        await manager.add_chat_history({"role": "user", "content": "第二个问题"})
        await manager.add_chat_history({"role": "assistant", "done": "[DONE]"})
        meta = await manager.get_session_meta()
        self.assertEqual(meta["title"], "生成的标题")

    async def test_manual_rename_not_restored_by_recompute(self):
        manager = self._manager()
        await manager.add_chat_history({"role": "user", "content": "第一个问题"})
        await manager.update_session_title("手动重命名")
        # 后续轮次收尾 recompute：title 已非默认值，首问兜底不触发
        await manager.add_chat_history({"role": "user", "content": "第二个问题"})
        await manager.add_chat_history({"role": "assistant", "done": "[DONE]"})
        meta = await manager.get_session_meta()
        self.assertEqual(meta["title"], "手动重命名")

    async def test_empty_title_rejected(self):
        manager = self._manager()
        with self.assertRaises(ValueError):
            await manager.apply_generated_title("   ", None)

    async def test_second_generation_blocked_by_marker(self):
        with mock.patch("memory.chat_memory.read_session_meta_value", return_value={
            "title_generated": True, "source": "title_model",
        }), mock.patch.object(title_generator, "resolve_title_model_config") as resolve:
            result = await title_generator.maybe_generate_session_title(self.session_id, "q", "p")
        self.assertIsNone(result)
        resolve.assert_not_called()


class ScheduleTitleGenerationTests(unittest.IsolatedAsyncioTestCase):
    async def test_schedules_background_task(self):
        calls = []

        async def fake_generate(session_id, question, preview, question_parts=None, retitle_each_message=False):
            calls.append((session_id, question, preview, question_parts, retitle_each_message))

        collector = title_generator.TitleSourceCollector()
        collector.feed_content("回答内容" * 30)
        with mock.patch("memory.chat_memory.read_session_meta_value", return_value=None), \
            mock.patch.object(title_generator, "_generate_and_apply_title",
                               side_effect=fake_generate):
            title_generator.schedule_title_generation("sess_sched", "问题", collector)
            # 让事件循环调度后台任务执行
            for _ in range(5):
                await asyncio.sleep(0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "sess_sched")
        self.assertEqual(calls[0][1], "问题")
        self.assertEqual(calls[0][2], ("回答内容" * 30)[:100])
        self.assertIsNone(calls[0][3])
        self.assertFalse(calls[0][4])

    async def test_schedule_passes_question_parts(self):
        calls = []
        parts = [{"type": "image_url", "image_url": {"url": "media://a.png"}}]

        async def fake_generate(session_id, question, preview, question_parts=None, retitle_each_message=False):
            calls.append((session_id, question, preview, question_parts, retitle_each_message))

        with mock.patch("memory.chat_memory.read_session_meta_value", return_value=None), \
            mock.patch.object(title_generator, "_generate_and_apply_title",
                               side_effect=fake_generate):
            title_generator.schedule_title_generation("sess_sched3", "问题", None, question_parts=parts)
            for _ in range(5):
                await asyncio.sleep(0)
        self.assertEqual(calls, [("sess_sched3", "问题", "", parts, False)])

    async def test_schedule_reads_retitle_flag(self):
        calls = []

        async def fake_generate(session_id, question, preview, question_parts=None, retitle_each_message=False):
            calls.append((session_id, question, preview, question_parts, retitle_each_message))

        with mock.patch("memory.chat_memory.read_session_meta_value", return_value=True), \
            mock.patch.object(title_generator, "_generate_and_apply_title",
                               side_effect=fake_generate):
            title_generator.schedule_title_generation("sess_retitle", "问题", None)
            for _ in range(5):
                await asyncio.sleep(0)
        self.assertTrue(calls[0][4])

    async def test_schedule_survives_inner_exception(self):
        async def broken_generate(*_args, **_kwargs):
            raise RuntimeError("boom")

        with mock.patch("memory.chat_memory.read_session_meta_value", return_value=None), \
            mock.patch.object(title_generator, "_generate_and_apply_title",
                               side_effect=broken_generate):
            # 内部异常由 done_callback 记录日志，不向外抛出
            title_generator.schedule_title_generation("sess_boom", "q", None)
            for _ in range(5):
                await asyncio.sleep(0)


class RetitleModeTests(unittest.IsolatedAsyncioTestCase):
    """「每条消息重新标题」开关与失败 attempted 标记语义。"""

    async def test_attempted_flag_blocks_retry(self):
        with mock.patch("memory.chat_memory.read_session_meta_value", return_value={
            "attempted": True, "title_attempted_at": "t",
        }) as fake_read, mock.patch.object(title_generator, "resolve_title_model_config") as resolve:
            result = await title_generator.maybe_generate_session_title("s1", "q", "p")
        self.assertIsNone(result)
        resolve.assert_not_called()
        self.assertEqual(mock.call("s1", "_title_state"), fake_read.call_args)

    async def test_retitle_mode_ignores_generated_flag(self):
        fake_config = {"apiType": "chat-completions", "selected_model_name": "测试模型"}
        with mock.patch("memory.chat_memory.read_session_meta_value", return_value={
            "title_generated": True,
        }), mock.patch.object(
            title_generator, "resolve_title_model_config", return_value=(fake_config, "")
        ), mock.patch.object(title_generator.ChatLLM, "chat_completions",
                             return_value={"content": "新标题"}):
            result = await title_generator.maybe_generate_session_title(
                "s1", "q", "p", retitle_each_message=True
            )
        self.assertEqual(result, "新标题")

    async def test_retitle_mode_ignores_attempted_flag(self):
        fake_config = {"apiType": "chat-completions", "selected_model_name": "测试模型"}
        with mock.patch("memory.chat_memory.read_session_meta_value", return_value={
            "attempted": True,
        }), mock.patch.object(
            title_generator, "resolve_title_model_config", return_value=(fake_config, "")
        ), mock.patch.object(title_generator.ChatLLM, "chat_completions",
                             return_value={"content": "重试标题"}):
            result = await title_generator.maybe_generate_session_title(
                "s1", "q", "p", retitle_each_message=True
            )
        self.assertEqual(result, "重试标题")


class GenerateAndApplyTitleMarkTests(unittest.IsolatedAsyncioTestCase):
    async def test_failure_marks_attempted_in_one_shot_mode(self):
        with mock.patch.object(title_generator, "maybe_generate_session_title", return_value=None), \
            mock.patch("memory.chat_memory.read_session_meta_value", return_value=None), \
            mock.patch("memory.chat_memory.mark_title_attempted", return_value=None) as mark:
            await title_generator._generate_and_apply_title("s1", "q", "p")
        mark.assert_called_once_with("s1")

    async def test_failure_skips_mark_in_retitle_mode(self):
        with mock.patch.object(title_generator, "maybe_generate_session_title", return_value=None), \
            mock.patch("memory.chat_memory.mark_title_attempted", return_value=None) as mark:
            await title_generator._generate_and_apply_title("s1", "q", "p", retitle_each_message=True)
        mark.assert_not_called()

    async def test_failure_mark_skipped_when_already_generated(self):
        with mock.patch.object(title_generator, "maybe_generate_session_title", return_value=None), \
            mock.patch("memory.chat_memory.read_session_meta_value", return_value={
                "title_generated": True,
            }), mock.patch("memory.chat_memory.mark_title_attempted", return_value=None) as mark:
            await title_generator._generate_and_apply_title("s1", "q", "p")
        mark.assert_not_called()


class TitleStateFileTests(unittest.IsolatedAsyncioTestCase):
    """mark_title_attempted / update_session_retitle_setting 真实落盘行为。"""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._orig_root = chat_memory.HISTORY_ROOT
        chat_memory.HISTORY_ROOT = Path(self._tmp)
        self.session_id = "title_state_file_test"

    def tearDown(self):
        chat_memory.HISTORY_ROOT = self._orig_root
        shutil.rmtree(self._tmp, ignore_errors=True)

    async def test_mark_attempted_writes_flag(self):
        manager = chat_memory.ChatMemoryManager(self.session_id)
        await manager.add_chat_history({"role": "user", "content": "q"})
        result = await asyncio.to_thread(chat_memory.mark_title_attempted, self.session_id)
        self.assertIsNotNone(result)
        self.assertTrue(result["_title_state"]["attempted"])
        self.assertIn("title_attempted_at", result["_title_state"])
        # 幂等：已有 title_generated 时不再写 attempted 标记（返回 None）
        await manager.apply_generated_title("T", None)
        result2 = await asyncio.to_thread(chat_memory.mark_title_attempted, self.session_id)
        self.assertIsNone(result2)
        final_meta = await manager.get_session_meta()
        self.assertNotIn("attempted", final_meta.get("_title_state") or {})

    async def test_retitle_setting_roundtrip(self):
        manager = chat_memory.ChatMemoryManager(self.session_id)
        await manager.add_chat_history({"role": "user", "content": "q"})
        meta = await manager.update_session_retitle_setting(True)
        self.assertTrue(meta["retitle_each_message"])
        read_back = await asyncio.to_thread(
            chat_memory.read_session_meta_value, self.session_id, "retitle_each_message"
        )
        self.assertTrue(bool(read_back))
        meta2 = await manager.update_session_retitle_setting(False)
        self.assertFalse(meta2["retitle_each_message"])

    async def test_retitle_on_clears_attempted(self):
        manager = chat_memory.ChatMemoryManager(self.session_id)
        await manager.add_chat_history({"role": "user", "content": "q"})
        await asyncio.to_thread(chat_memory.mark_title_attempted, self.session_id)
        meta = await manager.update_session_retitle_setting(True)
        self.assertNotIn("attempted", meta.get("_title_state") or {})


def read_back_value(value):
    return value


def read_back_value(value):
    return value


if __name__ == "__main__":
    unittest.main()
