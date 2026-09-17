"""按轮次号删除历史（/chat_history/delete_rounds 持久层）与 target_round 原地重跑测试：

- delete_rounds：truncate（该轮及之后全删）/ single（仅删该轮整轮）模式；
- dry_run 预演：只返回明细，不写盘、不删文件；
- 媒体清理：被删轮次引用 − 保留轮次引用 的差集精确删除 media/ 文件；
  keep_media_refs 排除编辑重发复用的附件；
- 文档清理：files/ 目录按上传时间窗近似删除（truncate=被删首轮起）；
- .bak 备份：破坏性写盘前生成；
- target_round：轮次收尾替换第 N 轮而非追加、get_current_round_number 覆盖、
  上下文历史截断到第 N-1 轮、越界降级为追加。

运行：python -m pytest test/test_delete_rounds.py -q
"""
import asyncio
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from memory import chat_memory, file_memory


class DeleteRoundsTests(unittest.TestCase):
    """delete_rounds 持久层行为（truncate/single/dry_run/文件清理/备份）。"""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._orig_chat_root = chat_memory.HISTORY_ROOT
        self._orig_file_root = file_memory.HISTORY_ROOT
        chat_memory.HISTORY_ROOT = Path(self._tmp)
        file_memory.HISTORY_ROOT = Path(self._tmp)
        self.session_id = "sess_delete_rounds"

    def tearDown(self):
        chat_memory.HISTORY_ROOT = self._orig_chat_root
        file_memory.HISTORY_ROOT = self._orig_file_root
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _round(self, question: str, answer: str, media_ref: str | None = None) -> list[dict]:
        """构造一轮对话的落盘事件（user + assistant 正文 + assistant done 标记）。

        与真实落盘结构一致：助手正文与 done 收尾标记是两条独立事件
        （done 标记事件内容为空，进上下文时被 chat_history_format 跳过）。
        """
        content = [{"type": "text", "text": question}]
        if media_ref:
            content.append({"type": "image_url", "image_url": {"url": media_ref}})
        return [
            {"role": "user", "content": content if media_ref else question},
            {"role": "assistant", "content": answer},
            {"role": "assistant", "done": "[DONE]"},
        ]

    def _seed_session(self, rounds: list[tuple[str, str, str | None]]) -> chat_memory.ChatMemoryManager:
        manager = chat_memory.ChatMemoryManager(self.session_id)
        for question, answer, media_ref in rounds:
            for record in self._round(question, answer, media_ref):
                asyncio.run(manager.add_chat_history(record))
        return manager

    def _entries(self, manager):
        meta, entries = chat_memory._load_meta_and_entries(manager._file_path, self.session_id)
        return meta, entries

    def _round_questions(self, manager):
        _, entries = self._entries(manager)
        return [
            entry.get("question")
            for entry in entries
            if isinstance(entry, dict) and entry.get("event") == "chat_round"
        ]

    def _create_media_file(self, stored_name: str) -> Path:
        media_dir = file_memory._media_dir(self.session_id)
        path = media_dir / stored_name
        path.write_bytes(b"fake-image-bytes")
        return path

    # ---------- 基础删除模式 ----------

    def test_truncate_removes_round_and_after(self):
        manager = self._seed_session([
            ("问题一", "回答一", None),
            ("问题二", "回答二", None),
            ("问题三", "回答三", None),
        ])
        result = asyncio.run(manager.delete_rounds(2, mode="truncate"))
        self.assertEqual(result["state"], "succeed")
        self.assertEqual(result["removed_rounds"], 2)
        self.assertEqual(self._round_questions(manager), ["问题一"])
        # usage/问题索引重算：只剩第一轮的问题
        meta, _ = self._entries(manager)
        self.assertEqual(meta["user_questions"], ["问题一"])
        # 破坏性写盘前应生成 .bak 备份（sidecars/ 子目录）
        bak_path = manager._file_path.parent / "sidecars" / (manager._file_path.name + ".bak")
        self.assertTrue(bak_path.exists())

    def test_single_keeps_later_rounds(self):
        manager = self._seed_session([
            ("问题一", "回答一", None),
            ("问题二", "回答二", None),
            ("问题三", "回答三", None),
        ])
        result = asyncio.run(manager.delete_rounds(2, mode="single"))
        self.assertEqual(result["state"], "succeed")
        self.assertEqual(result["removed_rounds"], 1)
        self.assertEqual(self._round_questions(manager), ["问题一", "问题三"])

    def test_delete_rounds_validation(self):
        manager = self._seed_session([("问题一", "回答一", None)])
        with self.assertRaises(ValueError):
            asyncio.run(manager.delete_rounds(5, mode="truncate"))
        with self.assertRaises(ValueError):
            asyncio.run(manager.delete_rounds(1, mode="bad-mode"))

    def test_dry_run_touches_nothing(self):
        manager = self._seed_session([
            ("问题一", "回答一", "media://keep_a.png"),
            ("问题二", "回答二", "media://del_b.png"),
        ])
        keep = self._create_media_file("keep_a.png")
        deleted = self._create_media_file("del_b.png")
        before_text = manager._file_path.read_text(encoding="utf-8")

        result = asyncio.run(manager.delete_rounds(2, mode="truncate", dry_run=True))

        self.assertEqual(result["state"], "planned")
        self.assertEqual([r["round"] for r in result["planned_rounds"]], [2])
        self.assertEqual(result["planned_files"]["media_files"], ["del_b.png"])
        # 历史与文件均未变动
        self.assertEqual(manager._file_path.read_text(encoding="utf-8"), before_text)
        self.assertTrue(keep.exists())
        self.assertTrue(deleted.exists())

    # ---------- 附件清理 ----------

    def test_media_cleanup_by_reference_diff(self):
        # 轮1 引用 keep_a（保留），轮2 引用 del_b，轮3 引用 del_c
        manager = self._seed_session([
            ("问题一", "回答一", "media://keep_a.png"),
            ("问题二", "回答二", "media://del_b.png"),
            ("问题三", "回答三", "media://del_c.png"),
        ])
        keep = self._create_media_file("keep_a.png")
        del_b = self._create_media_file("del_b.png")
        del_c = self._create_media_file("del_c.png")

        asyncio.run(manager.delete_rounds(2, mode="truncate", delete_files=True))

        self.assertTrue(keep.exists(), "保留轮次引用的文件不应删除")
        self.assertFalse(del_b.exists())
        self.assertFalse(del_c.exists())

    def test_single_media_cleanup_keeps_referenced_by_later_round(self):
        manager = self._seed_session([
            ("问题一", "回答一", None),
            ("问题二", "回答二", "media://del_b.png"),
            ("问题三", "回答三", "media://del_c.png"),
        ])
        del_b = self._create_media_file("del_b.png")
        del_c = self._create_media_file("del_c.png")

        # 仅删第 2 轮：轮3 仍引用 del_c，必须保留
        asyncio.run(manager.delete_rounds(2, mode="single", delete_files=True))

        self.assertFalse(del_b.exists())
        self.assertTrue(del_c.exists())

    def test_keep_media_refs_excludes_reused_attachment(self):
        manager = self._seed_session([
            ("问题一", "回答一", "media://del_b.png"),
            ("问题二", "回答二", None),
        ])
        del_b = self._create_media_file("del_b.png")

        # 编辑重发：旧消息里的 del_b.png 仍被新消息复用，清理时排除
        asyncio.run(manager.delete_rounds(
            1, mode="truncate", delete_files=True, keep_media_refs=["del_b.png"]
        ))

        self.assertTrue(del_b.exists(), "keep_media_refs 中的文件不应删除")

    def test_doc_cleanup_within_window(self):
        # 先建 3 轮，再按真实链路上传文档（save_session_document 存原始字节 +
        # add_file_memory 写记录 JSON；时间戳晚于被删首轮 started_at，落在删除窗内）
        manager = self._seed_session([
            ("问题一", "回答一", None),
            ("问题二", "回答二", None),
            ("问题三", "回答三", None),
        ])
        doc = file_memory.save_session_document(self.session_id, "报告.pdf", b"pdf-bytes")
        session_files = await_noop = None  # noqa: F841（语义占位，真实链路下一步补记录）
        file_memory.FileMemoryManager(self.session_id).add_file_memory({
            "filename": "报告.pdf",
            "type": "pdf",
            "content": "占位解析文本",
            "size": 8,
            "stored_name": doc["stored_name"],
        })
        doc_path = file_memory.resolve_document_path(self.session_id, doc["stored_name"])
        self.assertTrue(doc_path.exists())

        asyncio.run(manager.delete_rounds(2, mode="truncate", delete_files=True))

        self.assertFalse(doc_path.exists(), "删除窗内上传的文档应一并清理")

    def test_doc_before_window_kept_in_single_mode(self):
        # 上传文档发生在第 1 轮对话之前（记录 JSON 时间戳回拨 10 秒，早于第 1 轮
        # started_at，排除秒级时间戳碰撞）——single 删第 2 轮的窗口从第 1 轮
        # ended_at 起，文档早于窗口时不删
        doc = file_memory.save_session_document(self.session_id, "early.pdf", b"old-bytes")
        file_memory.FileMemoryManager(self.session_id).add_file_memory({
            "filename": "early.pdf",
            "type": "pdf",
            "content": "占位解析文本",
            "size": 8,
            "stored_name": doc["stored_name"],
        })
        # 时间戳回拨（测试确定性：避免与会话轮次同秒）
        record_path = next(
            p for p in file_memory._list_session_files(self.session_id) if p.name.endswith(".json")
        )
        record = file_memory._read_json_file(record_path)
        from util.timestamp_utils import DEFAULT_TIMESTAMP_FORMAT
        from datetime import datetime, timedelta
        record["timestamp"] = (datetime.strptime(record["timestamp"], DEFAULT_TIMESTAMP_FORMAT)
                               - timedelta(seconds=10)).strftime(DEFAULT_TIMESTAMP_FORMAT)
        file_memory._write_json_file(record_path, record)
        doc_path = file_memory.resolve_document_path(self.session_id, doc["stored_name"])
        manager = self._seed_session([("问题一", "回答一", None), ("问题二", "回答二", None)])

        asyncio.run(manager.delete_rounds(2, mode="single", delete_files=True))

        self.assertTrue(doc_path.exists(), "窗口外上传的文档不应误删")


class TargetRoundTests(unittest.TestCase):
    """target_round 原地重跑：替换式收尾、版本链轮次覆盖、历史截断。"""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._orig_chat_root = chat_memory.HISTORY_ROOT
        self._orig_file_root = file_memory.HISTORY_ROOT
        chat_memory.HISTORY_ROOT = Path(self._tmp)
        file_memory.HISTORY_ROOT = Path(self._tmp)
        self.session_id = "sess_target_round"
        self.manager = chat_memory.ChatMemoryManager(self.session_id)

    def tearDown(self):
        chat_memory.HISTORY_ROOT = self._orig_chat_root
        file_memory.HISTORY_ROOT = self._orig_file_root
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _seed_three_rounds(self):
        for question, answer in [("问题一", "回答一"), ("问题二", "回答二"), ("问题三", "回答三")]:
            asyncio.run(self.manager.add_chat_history({"role": "user", "content": question}))
            asyncio.run(self.manager.add_chat_history({"role": "assistant", "content": answer}))
            asyncio.run(self.manager.add_chat_history({"role": "assistant", "done": "[DONE]"}))

    def _questions(self):
        _, entries = chat_memory._load_meta_and_entries(self.manager._file_path, self.session_id)
        return [
            entry.get("question")
            for entry in entries
            if isinstance(entry, dict) and entry.get("event") == "chat_round"
        ]

    def _answers(self):
        _, entries = chat_memory._load_meta_and_entries(self.manager._file_path, self.session_id)
        answers = []
        for entry in entries:
            if not (isinstance(entry, dict) and entry.get("event") == "chat_round"):
                continue
            for event in entry.get("events") or []:
                # 只取正文事件（done 标记事件 content 为空，跳过）
                if (
                    isinstance(event, dict)
                    and event.get("role") == "assistant"
                    and event.get("done") != "[DONE]"
                    and event.get("content")
                ):
                    answers.append(event.get("content"))
        return answers

    def test_finalize_replaces_target_round(self):
        self._seed_three_rounds()
        self.assertEqual(self._questions(), ["问题一", "问题二", "问题三"])
        self.manager.set_target_round(2)

        # 编辑重发：同位置用户消息 + 新回复（正文 + done 标记）
        asyncio.run(self.manager.add_chat_history({"role": "user", "content": "问题二（已编辑）"}))
        asyncio.run(self.manager.add_chat_history({"role": "assistant", "content": "新回答二"}))
        asyncio.run(self.manager.add_chat_history({"role": "assistant", "done": "[DONE]"}))

        self.assertEqual(self._questions(), ["问题一", "问题二（已编辑）", "问题三"])
        self.assertEqual(self._answers(), ["回答一", "新回答二", "回答三"])
        # 收尾后 target 状态自动复位
        self.assertIsNone(asyncio.run(self.manager.get_target_round()))

    def test_same_question_regenerates_keeps_started_at(self):
        self._seed_three_rounds()
        _, entries_before = chat_memory._load_meta_and_entries(self.manager._file_path, self.session_id)
        old_started = [
            entry.get("started_at") for entry in entries_before
            if isinstance(entry, dict) and entry.get("event") == "chat_round"
        ]
        self.manager.set_target_round(2)

        # 同一用户消息：原样重跑（重新生成），started_at 保持轮次身份
        asyncio.run(self.manager.add_chat_history({"role": "user", "content": "问题二"}))
        asyncio.run(self.manager.add_chat_history({"role": "assistant", "content": "重新生成的回答二"}))
        asyncio.run(self.manager.add_chat_history({"role": "assistant", "done": "[DONE]"}))

        _, entries_after = chat_memory._load_meta_and_entries(self.manager._file_path, self.session_id)
        new_started = [
            entry.get("started_at") for entry in entries_after
            if isinstance(entry, dict) and entry.get("event") == "chat_round"
        ]
        self.assertEqual(old_started, new_started)
        self.assertIn("重新生成的回答二", self._answers())

    def test_out_of_range_target_falls_back_to_append(self):
        self._seed_three_rounds()
        self.manager.set_target_round(99)
        asyncio.run(self.manager.add_chat_history({"role": "user", "content": "问题四"}))
        asyncio.run(self.manager.add_chat_history({"role": "assistant", "content": "回答四"}))
        asyncio.run(self.manager.add_chat_history({"role": "assistant", "done": "[DONE]"}))
        self.assertEqual(len(self._questions()), 4)

    def test_stop_current_round_replaces_target(self):
        self._seed_three_rounds()
        self.manager.set_target_round(3)
        asyncio.run(self.manager.add_chat_history({"role": "user", "content": "问题三"}))
        asyncio.run(self.manager.stop_current_round())
        self.assertEqual(self._questions(), ["问题一", "问题二", "问题三"])
        _, entries = chat_memory._load_meta_and_entries(self.manager._file_path, self.session_id)
        stopped = [e for e in entries if isinstance(e, dict) and e.get("event") == "chat_round"][2]
        self.assertEqual(stopped.get("status"), "stopped")

    def test_current_round_number_overridden(self):
        self._seed_three_rounds()
        self.assertEqual(asyncio.run(self.manager.get_current_round_number()), 4)
        self.manager.set_target_round(2)
        # 原地重跑第 2 轮：版本链 round 标注应仍为 2（而非 4）
        self.assertEqual(asyncio.run(self.manager.get_current_round_number()), 2)

    def test_context_messages_truncated_to_target_minus_one(self):
        self._seed_three_rounds()
        self.manager.set_target_round(2)
        messages = asyncio.run(self.manager.get_context_messages(max_rounds=0))
        rendered = json.dumps(messages, ensure_ascii=False)
        self.assertNotIn("回答二", rendered, "被编辑轮次的旧回复不应进入上下文")
        self.assertNotIn("回答三", rendered, "第 N 轮之后的旧轮次也不应进入上下文")
        self.assertIn("回答一", rendered)
        # 复位后恢复完整历史
        self.manager.set_target_round(None)
        messages = asyncio.run(self.manager.get_context_messages(max_rounds=0))
        self.assertIn("回答三", json.dumps(messages, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
