"""原位重生成成功落盘后，只清理旧轮不再引用的媒体文件。"""
import shutil
import tempfile
import unittest
import asyncio
from pathlib import Path
from unittest import mock

from memory import chat_memory, file_memory


class ReplacedRoundMediaCleanupTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._orig_history_root = chat_memory.HISTORY_ROOT
        self._orig_file_history_root = file_memory.HISTORY_ROOT
        root = Path(self._tmp)
        chat_memory.HISTORY_ROOT = root
        file_memory.HISTORY_ROOT = root

    def tearDown(self):
        chat_memory.HISTORY_ROOT = self._orig_history_root
        file_memory.HISTORY_ROOT = self._orig_file_history_root
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_cleanup_keeps_reused_media_and_removes_only_unreferenced_old_media(self):
        manager = chat_memory.ChatMemoryManager("sess_replace")
        media_dir = file_memory._media_dir("sess_replace")
        thumb_dir = file_memory._thumb_dir("sess_replace")
        old_media = media_dir / "old.png"
        shared_media = media_dir / "shared.png"
        old_thumb = thumb_dir / "old.png.thumb.jpg"
        for path in (old_media, shared_media, old_thumb):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"test")

        replaced = {
            "event": "chat_round",
            "events": [{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "media://old.png"}},
                    {"type": "image_url", "image_url": {"url": "media://shared.png"}},
                ],
            }],
        }
        remaining = [{
            "event": "chat_round",
            "events": [{
                "role": "user",
                "content": [{"type": "image_url", "image_url": {"url": "media://shared.png"}}],
            }],
        }]

        manager._cleanup_replaced_round_media(replaced, remaining)

        self.assertFalse(old_media.exists())
        self.assertFalse(old_thumb.exists())
        self.assertTrue(shared_media.exists())

    def test_cleanup_failure_does_not_raise_after_round_persistence(self):
        manager = chat_memory.ChatMemoryManager("sess_replace_fail")
        replaced = {"event": "chat_round", "events": [{"content": "media://old.png"}]}
        with mock.patch.object(
            manager, "_execute_deleted_files_cleanup",
            return_value={"removed": [], "failed": ["media/old.png: locked"]},
        ) as cleanup:
            manager._cleanup_replaced_round_media(replaced, [])
        cleanup.assert_called_once()

    def test_editing_a_summarized_round_preserves_snapshot_and_sends_edited_raw_round(self):
        manager = chat_memory.ChatMemoryManager("sess_replace_summary")
        async def seed_rounds():
            for index in range(1, 4):
                await manager.add_chat_history({"role": "user", "content": f"问题{index}"})
                await manager.add_chat_history({"role": "assistant", "content": f"回答{index}"})
                await manager.add_chat_history({"role": "assistant", "done": "[DONE]"})

        asyncio.run(seed_rounds())
        meta, entries = manager._load_for_read()
        meta["context_summary"] = {
            "summary": "旧图片 media://old.png",
            "blocks": [{"summary": "旧图片 media://old.png", "round_start": 1, "round_end": 3}],
            "source_round_count": 3,
        }
        entries.extend([
            {
                "event": "context_compaction",
                "scope": "session",
                "phase": "start",
                "context_summary": "media://old.png",
            },
            {
                "event": "context_compaction",
                "scope": "session",
                "phase": "done",
                "summary_text": "media://old.png",
            },
            {
                "event": "context_compaction",
                "scope": "round",
                "phase": "done",
                "summary_text": "media://keep-round-snapshot.png",
            },
        ])
        chat_memory._write_meta_and_entries(manager._file_path, meta, entries)

        manager.set_target_round(2)

        # 重发请求只使用目标轮前原文，摘要不会被持久化清空。
        prefix = asyncio.run(manager.get_context_messages())
        self.assertNotIn("旧图片", "\n".join(str(item.get("content")) for item in prefix))
        
        async def replace_round():
            await manager.add_chat_history({"role": "user", "content": "编辑后问题2"})
            await manager.add_chat_history({"role": "assistant", "content": "替换后回复"})
            await manager.add_chat_history({"role": "assistant", "done": "[DONE]"})

        asyncio.run(replace_round())
        updated_meta, updated_entries = manager._load_for_read()
        self.assertEqual(updated_meta["context_summary"]["summary"], "旧图片 media://old.png")
        self.assertEqual(updated_meta["context_summary_raw_rounds"], [2])
        historical_snapshots = [
            entry for entry in updated_entries
            if isinstance(entry, dict)
            and entry.get("event") == "context_compaction"
            and entry.get("scope") == "session"
        ]
        self.assertEqual(len(historical_snapshots), 2)
        self.assertTrue(all(not entry.get("invalidated") for entry in historical_snapshots))
        self.assertTrue(any(
            entry.get("event") == "context_compaction"
            and entry.get("scope") == "round"
            for entry in updated_entries
            if isinstance(entry, dict)
        ))
        context = asyncio.run(manager.get_context_messages())
        context_text = "\n".join(str(item.get("content")) for item in context)
        self.assertIn("旧图片", context_text)
        self.assertIn("编辑后问题2", context_text)
        self.assertIn("替换后回复", context_text)
        self.assertIn("第 2 轮在摘要生成后被编辑或插入", context_text)

    def test_invalidated_snapshot_does_not_keep_replaced_media(self):
        manager = chat_memory.ChatMemoryManager("sess_invalidated_media")
        media_dir = file_memory._media_dir("sess_invalidated_media")
        media_dir.mkdir(parents=True, exist_ok=True)
        old_media = media_dir / "old.png"
        old_media.write_bytes(b"test")
        replaced = {"event": "chat_round", "events": [{"content": "media://old.png"}]}
        remaining = [{
            "event": "context_compaction", "scope": "session", "invalidated": True,
            "summary_text": "历史图片 media://old.png",
        }]
        manager._cleanup_replaced_round_media(replaced, remaining)
        self.assertFalse(old_media.exists())

    def test_editing_round_after_summary_coverage_keeps_summary(self):
        manager = chat_memory.ChatMemoryManager("sess_replace_summary_tail")
        meta, entries = manager._load_for_read()
        meta["context_summary"] = {
            "summary": "前两轮摘要",
            "source_round_count": 2,
        }
        entries.append({
            "event": "context_compaction",
            "scope": "session",
            "phase": "done",
            "summary_text": "前两轮摘要",
        })
        chat_memory._write_meta_and_entries(manager._file_path, meta, entries)

        manager.set_target_round(3)

        updated_meta, updated_entries = manager._load_for_read()
        self.assertEqual(
            updated_meta["context_summary"]["source_round_count"], 2
        )
        self.assertTrue(any(
            entry.get("event") == "context_compaction"
            and entry.get("scope") == "session"
            for entry in updated_entries
            if isinstance(entry, dict)
        ))


if __name__ == "__main__":
    unittest.main()
