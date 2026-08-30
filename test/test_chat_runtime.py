import asyncio
import unittest
import uuid
import tempfile
import json
from pathlib import Path

from factory.agent_runtime.chat_runtime import (
    parse_bool_like,
    parse_int_like,
    frontend_provides_full_history,
    UsageAccumulator,
    resolve_model_max_input_tokens,
)
from env_manager import init_path, get_default_chat_config
from memory.chat_memory import ChatMemoryManager


class ChatRuntimeTests(unittest.TestCase):
    def test_parse_bool_like(self):
        self.assertTrue(parse_bool_like("true", False))
        self.assertFalse(parse_bool_like("0", True))
        self.assertTrue(parse_bool_like(None, True))

    def test_parse_int_like(self):
        self.assertEqual(parse_int_like("8", 6), 8)
        self.assertEqual(parse_int_like("0", 6), 6)
        self.assertEqual(parse_int_like("bad", 6), 6)

    def test_frontend_provides_full_history(self):
        self.assertFalse(frontend_provides_full_history([{"role": "user", "content": "q"}]))
        self.assertTrue(frontend_provides_full_history([
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
        ]))

    def test_usage_accumulator_deduplicate_same_id(self):
        acc = UsageAccumulator()
        event = {
            "id": "cmpl-1",
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
            },
        }
        acc.collect(event)
        acc.collect(event)

        target = {}
        acc.merge_to(target, lambda total, delta: total.update({k: total.get(k, 0) + v for k, v in delta.items()}))
        self.assertEqual(acc.count, 1)
        self.assertEqual(target["total_tokens"], 15)

    def test_usage_accumulator_deduplicate_same_usage_across_different_ids(self):
        acc = UsageAccumulator()
        first = {
            "id": "cmpl-1",
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
            },
        }
        second = {
            "id": "cmpl-2",
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
            },
        }
        acc.collect(first)
        acc.collect(second)

        self.assertEqual(acc.count, 1)

    def test_memory_manager_exposes_session_meta_interface(self):
        manager = ChatMemoryManager("meta_test_session")
        meta = asyncio.run(manager.get_session_meta())

        # 会话标识以文件名为准，_meta 不包含 session_id
        self.assertNotIn("session_id", meta)
        self.assertIn("created_at", meta)
        self.assertIn("usage", meta)

    def test_update_session_title_persists_and_keeps_other_fields(self):
        session_id = f"meta_title_{uuid.uuid4().hex}"
        manager = ChatMemoryManager(session_id)

        async def _run_case():
            await manager.clear_chat_history()
            before = await manager.get_session_meta()
            created_before = before.get("created_at")
            await manager.update_session_title("我的自定义会话标题")
            after = await manager.get_session_meta()
            # 空标题应报错
            try:
                await manager.update_session_title("   ")
            except ValueError:
                pass
            else:
                raise AssertionError("空标题应当抛出 ValueError")
            return before, after, created_before

        try:
            before, after, created_before = asyncio.run(_run_case())
            self.assertEqual(after["title"], "我的自定义会话标题")
            # 其他字段保持不变（created_at 不被覆盖）
            self.assertEqual(after["created_at"], created_before)
            # 会话标识以文件名为准，_meta 不包含 session_id
            self.assertNotIn("session_id", after)
            # 再次更新，验证可重复修改
            self.assertEqual(asyncio.run(manager.update_session_title("标题2"))["title"], "标题2")
        finally:
            ChatMemoryManager.delete_chat_session_file(session_id)

    def test_context_messages_use_question_index_without_raw_history(self):
        """无摘要时按窗口回传最近 N 轮完整对话（最旧超出窗口舍弃）。"""
        session_id = f"ctx_compact_{uuid.uuid4().hex}"
        manager = ChatMemoryManager(session_id)

        async def _run_case():
            await manager.clear_chat_history()
            await manager.add_chat_history({"role": "user", "content": "能告诉我现在系统时间是几点吗？"})
            await manager.add_chat_history({
                "role": "assistant",
                "content": "我先查一下当前系统时间。",
                "reasoning_content": "先调用命令获取时间",
                "tool_calls": [
                    {
                        "id": "call_time_1",
                        "type": "function",
                        "function": {
                            "name": "run_pipe_command",
                            "arguments": "{\"command\": \"echo %date% %time%\"}",
                        },
                    }
                ],
            })
            await manager.add_chat_history({
                "role": "tool",
                "tool_call_id": "call_time_1",
                "content": "周二 2026/07/28 14:20:00.68",
            })
            await manager.add_chat_history({"role": "assistant", "content": "现在系统时间是 14:20:00。"})
            await manager.add_chat_history({"role": "assistant", "content": "距离上次查询大约 32 分钟。"})
            await manager.add_chat_history({"role": "assistant", "done": "[DONE]"})
            return await manager.get_context_messages(max_rounds=6, max_tool_result_length=0)

        try:
            context_messages = asyncio.run(_run_case())
            # 无摘要：1 条 user + 1 条 assistant（工具调用参数行并入 assistant 文本）
            self.assertEqual(len(context_messages), 2)
            self.assertEqual(context_messages[0]["role"], "user")
            self.assertIn("能告诉我现在系统时间是几点吗？", context_messages[0]["content"])
            self.assertEqual(context_messages[1]["role"], "assistant")
            self.assertIn("现在系统时间是 14:20:00", context_messages[1]["content"])
            # 工具结果不回传（max_tool_result_length=0），仅保留调用参数行
            self.assertIn("[调用工具] run_pipe_command", context_messages[1]["content"])
            self.assertNotIn("14:20:00.68", context_messages[1]["content"])
        finally:
            ChatMemoryManager.delete_chat_session_file(session_id)

    def test_context_messages_include_summary_and_skip_compacted_rounds(self):
        """摘要模式：摘要 + 已压缩轮次问题保真 + 未压缩轮次完整对话。"""
        session_id = f"ctx_summary_{uuid.uuid4().hex}"
        manager = ChatMemoryManager(session_id)

        async def _run_case():
            await manager.clear_chat_history()
            await manager.add_chat_history({"role": "user", "content": "第一轮问题"})
            await manager.add_chat_history({"role": "assistant", "content": "第一轮回答"})
            await manager.add_chat_history({"role": "assistant", "done": "[DONE]"})
            await manager.add_chat_history({"role": "user", "content": "第二轮问题"})
            await manager.add_chat_history({"role": "assistant", "content": "第二轮回答"})
            await manager.add_chat_history({"role": "assistant", "done": "[DONE]"})
            await manager.update_context_summary({
                "summary": "第一轮已完成，核心结论已保留。",
                "key_facts": ["第一轮问题 -> 第一轮回答"],
                "open_items": [],
                "tool_state": [],
                "source_round_count": 1,
            })
            return await manager.get_context_messages(max_rounds=6)

        try:
            context_messages = asyncio.run(_run_case())
            # 消息结构：摘要 system + 问题索引 system + 第2轮完整对话（user+assistant）
            self.assertEqual(context_messages[0]["role"], "system")
            self.assertIn("第一轮已完成", context_messages[0]["content"])
            self.assertEqual(context_messages[1]["role"], "system")
            # 问题索引只含已压缩的第 1 轮（第 2 轮以完整对话回传，不重复进索引）
            self.assertIn("第 1 轮：第一轮问题", context_messages[1]["content"])
            self.assertNotIn("第二轮问题", context_messages[1]["content"])
            # 第 2 轮完整对话回传
            self.assertEqual(context_messages[2]["role"], "user")
            self.assertIn("第二轮问题", context_messages[2]["content"])
            self.assertEqual(context_messages[3]["role"], "assistant")
            self.assertIn("第二轮回答", context_messages[3]["content"])
            self.assertEqual(len(context_messages), 4)
        finally:
            ChatMemoryManager.delete_chat_session_file(session_id)

    def test_single_round_compaction_checkpoint_and_events_persist(self):
        session_id = f"ctx_round_compression_{uuid.uuid4().hex}"
        manager = ChatMemoryManager(session_id)

        async def _run_case():
            await manager.clear_chat_history()
            await manager.add_chat_history({"role": "user", "content": "分步读取文件"})
            await manager.add_chat_history({
                "role": "assistant",
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "list_dir_item", "arguments": "{}"},
                }],
            })
            await manager.add_chat_history({
                "role": "tool",
                "tool_call_id": "call_1",
                "tool_name": "list_dir_item",
                "result": "目录结果",
            })
            await manager.update_current_round_compaction("已完成目录扫描。", 1)
            checkpoint_rows = [
                json.loads(line)
                for line in (await manager.get_file_text()).splitlines()
                if line.strip()
            ]
            await manager.add_chat_history({"role": "assistant", "done": "[DONE]"})
            final_entries = await manager.get_chat_history()
            return checkpoint_rows, final_entries

        try:
            checkpoint_rows, final_entries = asyncio.run(_run_case())
            self.assertIn("_active_round_compaction", checkpoint_rows[0]["_meta"])
            checkpoint_events = checkpoint_rows[0]["_meta"]["_active_round_compaction"]["events"]
            self.assertEqual(checkpoint_events[-1]["summary_text"], "已完成目录扫描。")
            self.assertEqual(checkpoint_events[-1]["compress_index"], 1)
            self.assertEqual(len(final_entries), 1)
            self.assertNotIn("compress_content", final_entries[0])
            self.assertNotIn("compress_index", final_entries[0])
            final_compaction = next(
                event for event in final_entries[0]["events"]
                if event.get("event") == "context_compaction"
            )
            self.assertEqual(final_compaction["summary_text"], "已完成目录扫描。")
            self.assertEqual(final_compaction["compress_index"], 1)
            self.assertNotIn("_active_round_compaction", (asyncio.run(manager.get_meta())))
        finally:
            ChatMemoryManager.delete_chat_session_file(session_id)

    def test_models_json_drives_default_chat_config_and_context_limit(self):
        repo_root = Path(__file__).resolve().parents[1]
        original_cwd = Path.cwd()

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            (temp_path / "setting").mkdir(parents=True, exist_ok=True)
            (temp_path / ".env").write_text(
                "CHAT_OWNERSHIP_NANE=OpenCode Completions\nCHAT_MODEL_NAME=Deepseek V4 Pro\n",
                encoding="utf-8",
            )
            (temp_path / "setting" / "models.json").write_text(
                json.dumps({
                    "OpenCode Completions": {
                        "vendor": "custom_endpoint",
                        "apiKey": "test-key",
                        "apiType": "chat-completions",
                        "models": {
                            "Deepseek V4 Pro": {
                                "id": "deepseek-v4-pro",
                                "url": "https://example.test/v1",
                                "toolCalling": True,
                                "vision": True,
                                "maxInputTokens": 123456,
                                "maxOutputTokens": 4096,
                            }
                        },
                    }
                }),
                encoding="utf-8",
            )

            try:
                init_path(str(temp_path))
                default_chat_config = get_default_chat_config()
                self.assertIsInstance(default_chat_config, dict)
                self.assertEqual(default_chat_config.get("selected_model_name"), "Deepseek V4 Pro")
                self.assertEqual(default_chat_config.get("selected_model_id"), "deepseek-v4-pro")
                self.assertEqual(default_chat_config.get("url"), "https://example.test/v1")
                self.assertEqual(default_chat_config.get("apiKey"), "test-key")
                self.assertEqual(resolve_model_max_input_tokens(default=8192), 123456)
            finally:
                init_path(str(repo_root))
                try:
                    from config import set_current_dir
                    set_current_dir(str(original_cwd))
                except Exception:
                    pass


if __name__ == "__main__":
    unittest.main()
