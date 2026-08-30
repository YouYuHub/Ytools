import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from memory.chat_memory import ChatMemoryManager, normalize_session_id
from memory.chat_round_store import parse_round_entry


# 完整轮次：与 history_files/web-msd5kqad_chat.jsonl 中的格式一致
_VALID_ROUND = {
    "event": "chat_round",
    "question": "你有什么用",
    "started_at": "2026-08-03 19:36:29",
    "events": [
        {"timestamp": "2026-08-03 19:36:29", "role": "user", "content": "你有什么用"},
        {"timestamp": "2026-08-03 19:36:33", "role": "assistant", "content": "我是一个智能AI助手"},
        {"timestamp": "2026-08-03 19:36:33", "role": "assistant", "done": "[DONE]"},
    ],
    "completion_count": 0,
    "usage_total": {},
    "status": "done",
    "ended_at": "2026-08-03 19:36:33",
}


def _build_jsonl(meta: dict | None, rounds: list[dict], extra_garbage: list[str] | None = None) -> bytes:
    lines: list[str] = []
    if meta is not None:
        lines.append(json.dumps({"_meta": meta}, ensure_ascii=False))
    for round_entry in rounds:
        lines.append(json.dumps(round_entry, ensure_ascii=False))
    if extra_garbage:
        lines.extend(extra_garbage)
    return ("\n".join(lines) + "\n").encode("utf-8")


class ParseRoundEntryTests(unittest.TestCase):
    def test_valid_round_passes(self):
        parsed = parse_round_entry(_VALID_ROUND)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["event"], "chat_round")
        self.assertEqual(parsed["question"], "你有什么用")
        self.assertEqual(parsed["status"], "done")
        # 内部事件应被保留
        self.assertEqual(len(parsed["events"]), 3)

    def test_compression_fields_are_normalized_and_preserved(self):
        round_entry = json.loads(json.dumps(_VALID_ROUND, ensure_ascii=False))
        round_entry["events"].insert(2, {
            "role": "assistant",
            "tool_calls": [{
                "id": "call_1",
                "function": {"name": "list_dir_item", "arguments": "{}"},
            }],
        })
        round_entry["events"].insert(3, {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": "目录结果",
        })
        round_entry["compress_content"] = "目录扫描已完成。"
        round_entry["compress_index"] = "3"  # 超过实际工具结果数时应安全收紧

        parsed = parse_round_entry(round_entry)
        self.assertEqual(parsed["compress_content"], "目录扫描已完成。")
        self.assertEqual(parsed["compress_index"], 1)

    def test_missing_question_is_rejected(self):
        round_entry = dict(_VALID_ROUND)
        round_entry["question"] = "   "
        self.assertIsNone(parse_round_entry(round_entry))

    def test_missing_user_event_is_rejected(self):
        round_entry = {
            "event": "chat_round",
            "question": "hi",
            "started_at": "2026-08-03 19:36:29",
            "events": [
                {"role": "assistant", "content": "没有用户消息的轮次"},
                {"role": "assistant", "done": "[DONE]"},
            ],
            "status": "done",
            "ended_at": "2026-08-03 19:36:29",
        }
        self.assertIsNone(parse_round_entry(round_entry))

    def test_invalid_status_is_rejected(self):
        round_entry = dict(_VALID_ROUND)
        round_entry["status"] = "unknown"
        self.assertIsNone(parse_round_entry(round_entry))

    def test_wrong_event_name_is_rejected(self):
        round_entry = dict(_VALID_ROUND)
        round_entry["event"] = "other_event"
        self.assertIsNone(parse_round_entry(round_entry))


class NormalizeSessionIdTests(unittest.TestCase):
    def test_filename_suffix_is_stripped(self):
        self.assertEqual(normalize_session_id("web-msd5kqad_chat.jsonl"), "web-msd5kqad")
        self.assertEqual(normalize_session_id("web-msd5kqad.jsonl"), "web-msd5kqad")
        self.assertEqual(normalize_session_id("default_chat.jsonl"), "default")

    def test_unicode_filenames_are_preserved_losslessly(self):
        # 中文文件名必须无损映射回原文件名（与 history_files 手动重命名场景一致）
        self.assertEqual(normalize_session_id("测试的文件_chat.jsonl"), "测试的文件")
        self.assertEqual(normalize_session_id("测试的文件"), "测试的文件")
        self.assertEqual(normalize_session_id("我的 文件_chat.jsonl"), "我的 文件")
        # 混合中英文
        self.assertEqual(normalize_session_id("web-中文会话"), "web-中文会话")

    def test_path_separators_replaced_and_empty_fallback(self):
        # 空格是合法文件名字符，应保留；路径分隔符替换为下划线
        self.assertEqual(normalize_session_id("a b/c\\d"), "a b_c_d")
        self.assertEqual(normalize_session_id("   "), "default")
        self.assertEqual(normalize_session_id(None), "default")
        self.assertEqual(normalize_session_id(""), "default")

    def test_path_traversal_blocked(self):
        self.assertNotIn("..", normalize_session_id("../evil"))
        self.assertNotEqual(normalize_session_id(".."), "..")
        self.assertNotEqual(normalize_session_id("...."), "....")

    def test_normal_id_passes_through(self):
        self.assertEqual(normalize_session_id("web-msd5kqad"), "web-msd5kqad")
        self.assertEqual(normalize_session_id("default"), "default")


class ImportJsonlChatHistoryTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        # 把 HISTORY_ROOT 临时指向临时目录，避免污染 history_files/
        from memory import chat_memory

        self._original_history_root = chat_memory.HISTORY_ROOT
        self._history_root_backup = self._original_history_root
        chat_memory.HISTORY_ROOT = Path(self._tmpdir.name)

    def tearDown(self):
        from memory import chat_memory

        chat_memory.HISTORY_ROOT = self._history_root_backup
        self._tmpdir.cleanup()

    def test_overwrite_writes_filtered_rounds_and_recomputed_meta(self):
        meta = {
            "session_id": "imported-session",
            "title": "导入的会话",
            "user_questions": [],
            "usage": {},
            "created_at": "2026-08-01 10:00:00",
            "updated_at": "2026-08-01 10:00:00",
            "record_count": 0,
            "completion_count": 0,
            "context_summary": None,
        }
        # 一条合法轮次 + 一条无 question 的非法轮次 + 一条 JSON 解析失败行
        invalid_round = dict(_VALID_ROUND)
        invalid_round["question"] = ""
        payload = _build_jsonl(
            meta=meta,
            rounds=[_VALID_ROUND, invalid_round],
            extra_garbage=["not a json line", "{broken json"],
        )

        result = ChatMemoryManager.import_jsonl_chat_history(
            "imported-session", payload, overwrite=True
        )

        self.assertEqual(result["state"], "succeed")
        # total_lines 包含 _meta + 2 round + 2 garbage
        self.assertEqual(result["total_lines"], 5)
        # 仅 1 条合法 round
        self.assertEqual(result["imported_rounds"], 1)
        # 1 条非法 round + 2 条垃圾行
        self.assertEqual(result["skipped_lines"], 3)
        self.assertEqual(result["record_count"], 1)
        self.assertEqual(result["title"], "导入的会话")
        self.assertEqual(result["collision"], False)
        # 文件应能直接被 _load_meta_and_entries 重新加载
        meta_after, entries = self._read_session("imported-session")
        self.assertEqual(meta_after["title"], "导入的会话")
        self.assertEqual(meta_after["record_count"], 1)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["question"], "你有什么用")
        # 会话标识以文件名为准，_meta 不应再包含 session_id
        self.assertNotIn("session_id", meta_after)

    def test_same_session_creates_timestamped_file_when_not_overwrite(self):
        # 第一次上传（默认 overwrite=False，目标不存在）→ 直接写入 merge-session_chat.jsonl
        first = ChatMemoryManager.import_jsonl_chat_history(
            "merge-session",
            _build_jsonl(meta=None, rounds=[_VALID_ROUND]),
        )
        self.assertEqual(first["session_id"], "merge-session")
        self.assertEqual(first["collision"], False)
        self.assertTrue((Path(self._tmpdir.name) / "merge-session_chat.jsonl").exists())

        # 第二次上传同名文件（overwrite=False）→ 同名冲突，自动追加时间戳另存
        new_round = {
            "event": "chat_round",
            "question": "另一个问题",
            "started_at": "2026-08-05 12:00:00",
            "events": [
                {"role": "user", "content": "另一个问题"},
                {"role": "assistant", "content": "好的", "done": "[DONE]"},
            ],
            "status": "done",
            "ended_at": "2026-08-05 12:00:05",
            "usage_total": {},
            "completion_count": 0,
        }
        payload = _build_jsonl(meta=None, rounds=[new_round])

        result = ChatMemoryManager.import_jsonl_chat_history(
            "merge-session", payload, overwrite=False
        )

        self.assertEqual(result["state"], "succeed")
        self.assertTrue(result["collision"])
        self.assertNotEqual(result["session_id"], "merge-session")
        self.assertIn("merge-session_", result["session_id"])
        self.assertTrue(result["filename"].endswith("_chat.jsonl"))
        self.assertEqual(result["imported_rounds"], 1)
        # 新文件包含新轮次
        _, entries = self._read_session(result["session_id"])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["question"], "另一个问题")
        # 原文件未被覆盖，仍保留第一轮
        _, original_entries = self._read_session("merge-session")
        self.assertEqual(len(original_entries), 1)
        self.assertEqual(original_entries[0]["question"], "你有什么用")

    def test_overwrite_true_replaces_existing_file(self):
        first = ChatMemoryManager.import_jsonl_chat_history(
            "replace-session",
            _build_jsonl(meta=None, rounds=[_VALID_ROUND]),
            overwrite=True,
        )
        self.assertEqual(first["imported_rounds"], 1)

        new_round = {
            "event": "chat_round",
            "question": "新问题",
            "started_at": "2026-08-05 09:00:00",
            "events": [
                {"role": "user", "content": "新问题"},
                {"role": "assistant", "content": "回答", "done": "[DONE]"},
            ],
            "status": "done",
            "ended_at": "2026-08-05 09:00:01",
            "usage_total": {},
            "completion_count": 0,
        }
        result = ChatMemoryManager.import_jsonl_chat_history(
            "replace-session",
            _build_jsonl(meta=None, rounds=[new_round]),
            overwrite=True,
        )
        self.assertEqual(result["session_id"], "replace-session")
        self.assertEqual(result["collision"], False)
        _, entries = self._read_session("replace-session")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["question"], "新问题")

    def test_empty_payload_returns_zero_rounds(self):
        result = ChatMemoryManager.import_jsonl_chat_history(
            "empty-session", b"", overwrite=True
        )
        self.assertEqual(result["total_lines"], 0)
        self.assertEqual(result["imported_rounds"], 0)
        self.assertEqual(result["skipped_lines"], 0)
        self.assertEqual(result["record_count"], 0)

    def test_filename_metadata_is_preserved_when_present(self):
        meta = {
            "session_id": "preserved-session",
            "title": "保留的标题",
            "context_summary": {
                "summary": "关键背景",
                "key_facts": ["a"],
                "open_items": ["b"],
                "tool_state": ["c"],
                "source_round_count": 0,
            },
            "user_questions": [],
            "usage": {},
            "created_at": "2026-08-01 10:00:00",
            "updated_at": "2026-08-01 10:00:00",
            "record_count": 0,
            "completion_count": 0,
        }
        payload = _build_jsonl(meta=meta, rounds=[_VALID_ROUND])
        ChatMemoryManager.import_jsonl_chat_history(
            "preserved-session", payload, overwrite=True
        )
        meta_after, _ = self._read_session("preserved-session")
        self.assertEqual(meta_after["title"], "保留的标题")
        self.assertEqual(meta_after["context_summary"]["summary"], "关键背景")
        self.assertEqual(meta_after["context_summary"]["key_facts"], ["a"])
        # 即使上传元数据带 session_id，也会被移除
        self.assertNotIn("session_id", meta_after)

    def test_mismatched_summary_cursor_is_cleared_on_import(self):
        meta = {
            "context_summary": {
                "summary": "旧摘要",
                "source_round_count": 8,
            },
        }
        ChatMemoryManager.import_jsonl_chat_history(
            "mismatched-summary",
            _build_jsonl(meta=meta, rounds=[_VALID_ROUND]),
            overwrite=True,
        )
        meta_after, _ = self._read_session("mismatched-summary")
        self.assertIsNone(meta_after["context_summary"])

    def test_deleting_history_invalidates_summary_cursor(self):
        manager = ChatMemoryManager("delete-summary")

        async def prepare():
            await manager.add_chat_history({"role": "user", "content": "问题"})
            await manager.add_chat_history({"role": "assistant", "content": "回答"})
            await manager.add_chat_history({"role": "assistant", "done": "[DONE]"})
            await manager.update_context_summary({
                "summary": "旧摘要",
                "source_round_count": 1,
            })

        asyncio.run(prepare())
        ChatMemoryManager.delete_chat_session_file_line("delete-summary", 1, 1)
        meta_after, _ = self._read_session("delete-summary")
        self.assertIsNone(meta_after["context_summary"])

    def test_unicode_session_id_round_trips_to_real_file(self):
        # 模拟 history_files 下手动重命名出的中文文件：必须能按文件名加载回
        session_id = "测试的文件"
        result = ChatMemoryManager.import_jsonl_chat_history(
            session_id,
            _build_jsonl(meta={"title": "中文会话"}, rounds=[_VALID_ROUND]),
            overwrite=True,
        )
        self.assertEqual(result["session_id"], "测试的文件")
        self.assertEqual(result["filename"], "测试的文件_chat.jsonl")
        self.assertEqual(result["collision"], False)
        # 实际文件确实以中文名落盘
        real_file = Path(self._tmpdir.name) / "测试的文件_chat.jsonl"
        self.assertTrue(real_file.exists())
        # 用 normalize_session_id 从文件名能无损找回
        self.assertEqual(normalize_session_id("测试的文件_chat.jsonl"), "测试的文件")
        # 加载回文件，内容一致
        meta_after, entries = self._read_session("测试的文件")
        self.assertEqual(meta_after["title"], "中文会话")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["question"], "你有什么用")

    @staticmethod
    def _read_session(session_id: str):
        from memory import chat_memory

        file_path = chat_memory._get_chat_history_file(session_id)
        return chat_memory._load_meta_and_entries(file_path, session_id)


if __name__ == "__main__":
    unittest.main()
