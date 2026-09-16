# coding: utf-8
"""多会话分享/导入（zip + jsonl 两阶段）单元测试。

覆盖：
- export_sessions_to_zip：单会话/多会话打包、manifest、session_files 数据收纳、跳过不存在会话
- list_zip_sessions：预检清单与本地冲突标记（不落盘）
- import_sessions_from_zip：create / rename / overwrite / skip 四种决策路径
  （含上传数据目录归属改名与 _meta.upload_id 回写）
"""
import asyncio
import io
import json
import shutil
import unittest
import zipfile
from pathlib import Path

from memory import chat_memory as cm
from memory import file_memory as fm
_TEST_SESSION = "share_roundtrip_ut"
_TEST_SESSION_B = "share_roundtrip_ut_b"


def _make_round(question: str, reply: str) -> dict:
    return {
        "event": "chat_round",
        "question": question,
        "started_at": "2026-09-15 12:00:00",
        "events": [
            {"timestamp": "2026-09-15 12:00:00", "role": "user", "content": question},
            {"timestamp": "2026-09-15 12:00:01", "role": "assistant", "content": reply},
            {"timestamp": "2026-09-15 12:00:02", "role": "assistant", "done": "[DONE]"},
        ],
        "completion_count": 1,
        "usage_total": {},
        "status": "done",
        "ended_at": "2026-09-15 12:00:02",
    }


def _build_zip_bytes(payloads: dict[str, tuple[str, dict[str, bytes]]]) -> bytes:
    """按分享包布局构造 zip 字节流：{jsonl_name: (jsonl_text, {rel_path: data})}。"""
    buffer = io.BytesIO()
    manifest_sessions = []
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for jsonl_name, (text, files) in payloads.items():
            zf.writestr(jsonl_name, text)
            session_id = cm.normalize_session_id(jsonl_name)
            upload_id = session_id
            for rel, data in files.items():
                zf.writestr(f"session_files/{upload_id}/{rel}", data)
            manifest_sessions.append({
                "session_id": session_id,
                "filename": jsonl_name,
                "title": f"标题-{session_id}",
                "upload_id": upload_id,
            })
        zf.writestr("manifest.json", json.dumps(
            {"version": 1, "sessions": manifest_sessions}, ensure_ascii=False))
    return buffer.getvalue()


class SessionShareTests(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self._cleanup()
        # 造两个带数据的测试会话
        for sid in (_TEST_SESSION, _TEST_SESSION_B):
            manager = self.loop.run_until_complete(cm.get_chat_memory_manager(sid))
            with manager._write_guard():
                rows = [_meta_line(f"{sid} 标题"), _make_round(f"{sid} 问1", "答1"), _make_round(f"{sid} 问2", "答2")]
                text = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n"
                Path(manager._file_path).write_text(text, encoding="utf-8")
            data_dir = fm.HISTORY_ROOT / sid
            (data_dir / "media").mkdir(parents=True, exist_ok=True)
            (data_dir / "media" / "clip.png").write_bytes(b"\x89PNG-fake-bytes")

    def tearDown(self):
        self._cleanup()
        self.loop.close()
        asyncio.set_event_loop(None)

    def _cleanup(self):
        # sid_new 也在清理列表内（test_05 断言失败时残留会导致后续 run 冲突跳过）
        for sid in (_TEST_SESSION, _TEST_SESSION_B, _TEST_SESSION + "_fresh"):
            chat_file = _repo_history(sid)
            if chat_file.exists():
                try:
                    chat_file.unlink()
                except OSError:
                    pass
            pending = chat_file.with_name(chat_file.name + ".pending")
            if pending.exists():
                try:
                    pending.unlink()
                except OSError:
                    pass
            for base in (fm.HISTORY_ROOT,):
                data_dir = base / cm._safe_session_id(sid)
                if data_dir.is_dir():
                    shutil.rmtree(data_dir, ignore_errors=True)

    # ---------- 导出 ----------
    def test_01_export_zip_contains_jsonl_and_data(self):
        zip_bytes, name, skipped = cm.export_sessions_to_zip([_TEST_SESSION])
        self.assertEqual(name, f"{_TEST_SESSION}_chat.zip")
        self.assertEqual(skipped, [])
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
        names = zf.namelist()
        self.assertIn(f"{_TEST_SESSION}_chat.jsonl", names)
        self.assertIn("manifest.json", names)
        self.assertIn(f"session_files/{_TEST_SESSION}/media/clip.png", names)
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        self.assertEqual(manifest["sessions"][0]["session_id"], _TEST_SESSION)

    def test_02_export_multi_and_skip_missing(self):
        zip_bytes, name, skipped = cm.export_sessions_to_zip(
            [_TEST_SESSION, _TEST_SESSION_B, "no_such_session_x"])
        self.assertTrue(name.startswith("ytools_sessions_"))
        self.assertEqual(skipped, ["no_such_session_x"])
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
        self.assertIn(f"{_TEST_SESSION}_chat.jsonl", zf.namelist())
        self.assertIn(f"{_TEST_SESSION_B}_chat.jsonl", zf.namelist())

    def test_03_export_empty_raises(self):
        with self.assertRaises(ValueError):
            cm.export_sessions_to_zip([])
        with self.assertRaises(ValueError):
            cm.export_sessions_to_zip(["no_such_session_x"])

    # ---------- 预检 ----------
    def test_04_preview_lists_conflicts_without_writing(self):
        zip_bytes, _name, _skipped = cm.export_sessions_to_zip(
            [_TEST_SESSION, _TEST_SESSION_B])
        preview = cm.list_zip_sessions(zip_bytes)
        ids = {s["session_id"]: s for s in preview["sessions"]}
        self.assertEqual(preview["total"], 2)
        # 本地已存在 → 冲突标记
        self.assertTrue(ids[_TEST_SESSION]["exists"])
        self.assertTrue(ids[_TEST_SESSION]["imported_rounds"] == 2)
        self.assertEqual(ids[_TEST_SESSION]["media_count"], 1)

    # ---------- 导入：各决策路径 ----------
    def test_05_import_create_and_data_dir(self):
        # 用另一个 ID 构造无冲突的包
        sid_new = _TEST_SESSION + "_fresh"
        payloads = {
            f"{sid_new}_chat.jsonl": (
                _meta_line(sid_new) + "\n" +
                json.dumps(_make_round(f"{sid_new} 问", "答"), ensure_ascii=False),
                {"media/m.png": b"PNGDATA"}),
        }
        result = asyncio.run(cm.import_sessions_from_zip(
            _build_zip_bytes(payloads)))
        self.assertEqual(len(result["imported"]), 1)
        imported = result["imported"][0]
        self.assertEqual(imported["session_id"], sid_new)
        self.assertFalse(imported["collision"])
        # 上传数据目录已落盘
        data_dir = fm.HISTORY_ROOT / sid_new
        self.assertTrue((data_dir / "media" / "m.png").exists())
        manager = self.loop.run_until_complete(cm.get_chat_memory_manager(sid_new))
        meta = self.loop.run_until_complete(manager.get_session_meta())
        self.assertEqual(meta.get("upload_id"), sid_new)
        self._cleanup_extra(sid_new)

    def test_06_import_rename_on_conflict(self):
        payload_zip = _build_zip_bytes({
            f"{_TEST_SESSION}_chat.jsonl": (_meta_line(_TEST_SESSION), {}),
        })
        # 本地原会话数据目录基线（重命名导入不得破坏）
        orig_dir = fm.HISTORY_ROOT / _TEST_SESSION
        self.assertTrue((orig_dir / "media" / "clip.png").exists())
        result = asyncio.run(cm.import_sessions_from_zip(
            payload_zip, decisions={_TEST_SESSION: "rename"}))
        self.assertEqual(len(result["imported"]), 1)
        renamed = result["imported"][0]
        self.assertTrue(renamed["collision"])
        self.assertNotEqual(renamed["session_id"], _TEST_SESSION)
        # 另存文件真实存在
        self.assertTrue(_repo_history(renamed["session_id"]).exists())
        # _meta.upload_id 已回写为新目录名
        manager = self.loop.run_until_complete(
            cm.get_chat_memory_manager(renamed["session_id"]))
        meta = self.loop.run_until_complete(manager.get_session_meta())
        self.assertEqual(meta.get("upload_id"), renamed["session_id"])
        # 关键回归：重命名导入不得移动/清空原会话的数据目录
        self.assertTrue((orig_dir / "media" / "clip.png").exists(),
                        "重命名导入后原会话数据目录被破坏")
        # 新会话不应占用原会话目录名
        self.assertFalse(result["imported"][0]["upload_dir_name"] == _TEST_SESSION)
        self._cleanup_extra(renamed["session_id"])

    def test_10_import_rename_with_data_keeps_original_dir(self):
        # 用户踩中场景：包内带数据 + 重命名 → 新会话拿到包内数据，原会话目录完好无损
        payloads = {
            f"{_TEST_SESSION}_chat.jsonl": (
                _meta_line(_TEST_SESSION) + "\n" +
                json.dumps(_make_round("另存问", "另存答"), ensure_ascii=False),
                {"media/imported.png": b"IMPORTED", "file_diffs/x.json": b"{}"}),
        }
        orig_dir = fm.HISTORY_ROOT / _TEST_SESSION
        self.assertTrue((orig_dir / "media" / "clip.png").exists())
        result = asyncio.run(cm.import_sessions_from_zip(
            _build_zip_bytes(payloads), decisions={_TEST_SESSION: "rename"}))
        self.assertEqual(len(result["imported"]), 1)
        renamed = result["imported"][0]
        self.assertTrue(renamed["collision"])
        new_dir = fm.HISTORY_ROOT / renamed["upload_dir_name"]
        # 包内数据落在新目录
        self.assertTrue((new_dir / "media" / "imported.png").exists())
        self.assertTrue((new_dir / "file_diffs" / "x.json").exists())
        # 原会话目录未被搬走、未被混写
        self.assertTrue((orig_dir / "media" / "clip.png").exists())
        self.assertFalse((orig_dir / "media" / "imported.png").exists())
        self._cleanup_extra(renamed["session_id"])

    def test_07_import_overwrite_replaces(self):
        # 覆盖：导入只有 1 轮 + 新媒体文件的包，覆盖后应为 1 轮、目录内容被替换
        payloads = {
            f"{_TEST_SESSION}_chat.jsonl": (
                _meta_line(_TEST_SESSION) + "\n" +
                json.dumps(_make_round("覆盖问", "覆盖答"), ensure_ascii=False),
                {"media/replaced.png": b"NEW"}),
        }
        result = asyncio.run(cm.import_sessions_from_zip(
            _build_zip_bytes(payloads), decisions={_TEST_SESSION: "overwrite"}))
        self.assertEqual(len(result["imported"]), 1)
        self.assertFalse(result["imported"][0]["collision"])
        self.assertEqual(result["imported"][0]["imported_rounds"], 1)
        # 上传目录被清空后重写
        data_dir = fm.HISTORY_ROOT / _TEST_SESSION
        self.assertTrue((data_dir / "media" / "replaced.png").exists())
        self.assertFalse((data_dir / "media" / "clip.png").exists())

    def test_08_import_skip_on_conflict(self):
        payload_zip = _build_zip_bytes({
            f"{_TEST_SESSION}_chat.jsonl": (_meta_line(_TEST_SESSION), {}),
        })
        result = asyncio.run(cm.import_sessions_from_zip(
            payload_zip, decisions={_TEST_SESSION: "skip"}))
        self.assertEqual(len(result["imported"]), 0)
        self.assertEqual(result["skipped"][0]["session_id"], _TEST_SESSION)

    def test_09_import_ask_without_decision_skips(self):
        payload_zip = _build_zip_bytes({
            f"{_TEST_SESSION}_chat.jsonl": (_meta_line(_TEST_SESSION), {}),
        })
        result = asyncio.run(cm.import_sessions_from_zip(
            payload_zip, conflict_strategy="ask"))
        # ask 且无逐项决策 → 不静默改名/覆盖，跳过
        self.assertEqual(len(result["imported"]), 0)
        self.assertEqual(len(result["skipped"]), 1)

    def _cleanup_extra(self, sid: str):
        chat_file = _repo_history(sid)
        if chat_file.exists():
            try:
                chat_file.unlink()
            except OSError:
                pass
        data_dir = fm.HISTORY_ROOT / sid
        if data_dir.is_dir():
            shutil.rmtree(data_dir, ignore_errors=True)


def _repo_history(session_id: str) -> Path:
    return cm._get_chat_history_file(session_id)


def _meta_line(title: str) -> str:
    meta = {
        "title": title,
        "user_questions": [],
        "usage": {},
        "record_count": 0,
        "completion_count": 0,
        "upload_id": "",
        "created_at": "2026-09-15 12:00:00",
        "updated_at": "2026-09-15 12:00:00",
    }
    return json.dumps({"_meta": meta}, ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()

