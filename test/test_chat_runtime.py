import asyncio
import unittest
import uuid
import tempfile
import json
from pathlib import Path

from factory.chat_runtime import (
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

        self.assertEqual(meta["session_id"], "meta_test_session")
        self.assertIn("created_at", meta)
        self.assertIn("usage", meta)

    def test_context_messages_keep_user_content_and_tool_commands(self):
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
            return await manager.get_context_messages(max_rounds=6)

        try:
            context_messages = asyncio.run(_run_case())
            self.assertEqual(len(context_messages), 2)
            self.assertEqual(context_messages[0]["role"], "user")
            self.assertEqual(context_messages[0]["content"], "能告诉我现在系统时间是几点吗？")
            self.assertEqual(context_messages[1]["role"], "assistant")
            self.assertEqual(
                context_messages[1]["content"],
                "我先查一下当前系统时间。\n[调用工具] run_pipe_command({\"command\": \"echo %date% %time%\"})\n现在系统时间是 14:20:00。\n距离上次查询大约 32 分钟。",
            )
        finally:
            ChatMemoryManager.delete_chat_session_file(session_id)

    def test_context_messages_include_summary_and_skip_compacted_rounds(self):
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
            self.assertEqual(context_messages[0]["role"], "system")
            self.assertIn("第一轮已完成", context_messages[0]["content"])
            self.assertEqual(len(context_messages), 3)
            self.assertEqual(context_messages[1]["role"], "user")
            self.assertEqual(context_messages[1]["content"], "第二轮问题")
            self.assertEqual(context_messages[2]["role"], "assistant")
            self.assertEqual(context_messages[2]["content"], "第二轮回答")
        finally:
            ChatMemoryManager.delete_chat_session_file(session_id)

    def test_models_json_drives_default_chat_config_and_context_limit(self):
        repo_root = Path(__file__).resolve().parents[1]
        original_cwd = Path.cwd()

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            (temp_path / "setting").mkdir(parents=True, exist_ok=True)
            (temp_path / ".env").write_text(
                "CHAT_OWNERSHIP_NANE=OpenCode Completions\nCHAT_MODEL_NAME=deepseek-v4-pro\n",
                encoding="utf-8",
            )
            (temp_path / "setting" / "models.json").write_text(
                json.dumps({
                    "OpenCode Completions": {
                        "vendor": "custom_endpoint",
                        "apiKey": "test-key",
                        "apiType": "chat-completions",
                        "models": {
                            "deepseek-v4-pro": {
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
                self.assertEqual(default_chat_config.get("selected_model_name"), "deepseek-v4-pro")
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
