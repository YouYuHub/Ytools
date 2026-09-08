"""多模态媒体上传与引用解析测试。

覆盖：
- save_session_media 存储与唯一命名
- resolve_media_content_parts：image_url → data URL、input_audio → 纯 base64、未解析引用保留
- resolve_media_path 路径穿越拒绝
- content_part_to_text 文本提取（历史回放/压缩口径）
- estimate_message_tokens 对 base64 媒体部件的固定占位计费
- /file/upload_session_media 与 /file/get_session_media 端点
- 多模态轮次的历史回放（round_entry_to_context_messages）
"""
import asyncio
import base64
import io
import json
import os
import shutil
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from starlette.datastructures import UploadFile

from config import Message
from factory.agent_runtime.chat_runtime import (
    MULTIMODAL_PART_PLACEHOLDER_TOKENS,
    estimate_message_tokens,
)
from memory import file_memory as fm
from memory.chat_history_format import round_entry_to_context_messages
from routers import file_router

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 64
WAV_BYTES = b"RIFF" + b"0" * 32

TEST_SESSION = "media_ut_session"


def _cleanup_test_session():
    upload_dir = fm.HISTORY_ROOT / TEST_SESSION
    if upload_dir.exists():
        shutil.rmtree(upload_dir, ignore_errors=True)
    chat_file = Path(__file__).resolve().parents[1] / "history_files" / f"{TEST_SESSION}_chat.jsonl"
    if chat_file.exists():
        chat_file.unlink()
    pending_file = chat_file.with_name(chat_file.name + ".pending")
    if pending_file.exists():
        pending_file.unlink()
    try:
        from memory.chat_memory import cleanup_chat_memory_manager
        asyncio.run(cleanup_chat_memory_manager(TEST_SESSION))
    except Exception:
        pass
    fm.cleanup_file_memory_manager(TEST_SESSION)


class MediaStorageTests(unittest.TestCase):
    def setUp(self):
        _cleanup_test_session()

    def tearDown(self):
        _cleanup_test_session()

    def test_save_session_media_roundtrip(self):
        saved = fm.save_session_media(TEST_SESSION, "截图.png", PNG_BYTES)
        self.assertEqual(saved["kind"], "image")
        self.assertEqual(saved["mime"], "image/png")
        self.assertTrue(saved["media_ref"].startswith("media://"))
        stored = fm.resolve_media_path(TEST_SESSION, saved["media_ref"])
        self.assertIsNotNone(stored)
        self.assertEqual(stored.read_bytes(), PNG_BYTES)

    def test_save_same_name_unique_storage(self):
        first = fm.save_session_media(TEST_SESSION, "a.png", PNG_BYTES)
        second = fm.save_session_media(TEST_SESSION, "a.png", PNG_BYTES)
        self.assertNotEqual(first["stored_name"], second["stored_name"])

    def test_save_rejects_non_media(self):
        with self.assertRaises(ValueError):
            fm.save_session_media(TEST_SESSION, "doc.pdf", b"%PDF-1.4")

    def test_resolve_media_path_blocks_traversal(self):
        self.assertIsNone(fm.resolve_media_path(TEST_SESSION, "media://../other.png"))
        self.assertIsNone(fm.resolve_media_path(TEST_SESSION, "media://a/b.png"))
        self.assertIsNone(fm.resolve_media_path(TEST_SESSION, "media://missing.png"))


class MediaResolveTests(unittest.TestCase):
    def setUp(self):
        _cleanup_test_session()
        self.png = fm.save_session_media(TEST_SESSION, "p.png", PNG_BYTES)
        self.wav = fm.save_session_media(TEST_SESSION, "a.wav", WAV_BYTES)

    def tearDown(self):
        _cleanup_test_session()

    def test_image_url_resolved_to_data_url(self):
        content = [
            {"type": "text", "text": "看这张图"},
            {"type": "image_url", "image_url": {"url": self.png["media_ref"]}},
        ]
        resolved, unresolved = fm.resolve_media_content_parts(TEST_SESSION, content)
        self.assertEqual(unresolved, [])
        url = resolved[1]["image_url"]["url"]
        self.assertTrue(url.startswith("data:image/png;base64,"))
        self.assertEqual(
            base64.b64decode(url.split(",", 1)[1]),
            PNG_BYTES,
        )
        # 原部件不被修改（返回新结构）
        self.assertEqual(content[1]["image_url"]["url"], self.png["media_ref"])

    def test_input_audio_resolved_to_raw_base64(self):
        content = [{"type": "input_audio", "input_audio": {"data": self.wav["media_ref"], "format": "wav"}}]
        resolved, unresolved = fm.resolve_media_content_parts(TEST_SESSION, content)
        self.assertEqual(unresolved, [])
        self.assertEqual(base64.b64decode(resolved[0]["input_audio"]["data"]), WAV_BYTES)

    def test_unresolved_reference_kept_and_reported(self):
        content = [{"type": "image_url", "image_url": {"url": "media://ghost.png"}}]
        resolved, unresolved = fm.resolve_media_content_parts(TEST_SESSION, content)
        self.assertEqual(unresolved, ["media://ghost.png"])
        self.assertEqual(resolved[0]["image_url"]["url"], "media://ghost.png")

    def test_http_and_data_urls_passthrough(self):
        content = [
            {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]
        resolved, unresolved = fm.resolve_media_content_parts(TEST_SESSION, content)
        self.assertEqual(unresolved, [])
        self.assertEqual(resolved, content)

    def test_resolve_message_media_refs_mutates_in_place(self):
        message = {
            "role": "user",
            "content": [
                {"type": "text", "text": "hi"},
                {"type": "image_url", "image_url": {"url": self.png["media_ref"]}},
            ],
        }
        unresolved = fm.resolve_message_media_refs(TEST_SESSION, [message])
        self.assertEqual(unresolved, [])
        self.assertTrue(message["content"][1]["image_url"]["url"].startswith("data:"))

    def test_content_part_to_text(self):
        self.assertEqual(fm.content_part_to_text("hello"), "hello")
        self.assertEqual(fm.content_part_to_text(None), "")
        parts = [
            {"type": "text", "text": "第一行"},
            {"type": "image_url", "image_url": {"url": "media://x.png"}},
            {"type": "input_audio", "input_audio": {"data": "media://y.wav"}},
        ]
        self.assertEqual(fm.content_part_to_text(parts), "第一行\n[图片]\n[音频]")


class MediaEstimatorTests(unittest.TestCase):
    def test_base64_part_uses_placeholder_cost(self):
        big_base64 = "A" * (2 * 1024 * 1024)  # 2MB base64
        message = {
            "role": "user",
            "content": [
                {"type": "text", "text": "看图"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{big_base64}"}},
            ],
        }
        tokens = estimate_message_tokens(message)
        # 媒体部件按固定占位计费，不随 base64 长度线性膨胀
        self.assertLess(tokens, MULTIMODAL_PART_PLACEHOLDER_TOKENS * 4)

    def test_media_ref_part_uses_placeholder_cost(self):
        message = {
            "role": "user",
            "content": [{"type": "image_url", "image_url": {"url": "media://a.png"}}],
        }
        tokens = estimate_message_tokens(message)
        self.assertLess(tokens, MULTIMODAL_PART_PLACEHOLDER_TOKENS * 2)

    def test_text_content_unchanged(self):
        message = {"role": "user", "content": "普通文本" * 100}
        self.assertGreater(estimate_message_tokens(message), 100)


class MediaEndpointTests(unittest.TestCase):
    def setUp(self):
        _cleanup_test_session()

    def tearDown(self):
        _cleanup_test_session()

    @staticmethod
    def _upload_file(name: str, data: bytes) -> UploadFile:
        return UploadFile(file=io.BytesIO(data), filename=name)

    def test_upload_and_get_session_media(self):
        response = asyncio.run(file_router.upload_session_media(
            files=[self._upload_file("图.png", PNG_BYTES), self._upload_file("曲.wav", WAV_BYTES)],
            session_id=TEST_SESSION,
        ))
        body = json.loads(response.body.decode("utf-8"))
        self.assertEqual(body["success"], 2)
        self.assertEqual(body["failed"], 0)
        kinds = sorted(result["kind"] for result in body["results"])
        self.assertEqual(kinds, ["audio", "image"])
        self.assertTrue(body["upload_id"])

        stored = body["results"][0]["stored_name"]
        from starlette.requests import Request

        media_response = asyncio.run(file_router.get_session_media(
            request=Request(scope={"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""}),
            name=stored, session_id=TEST_SESSION,
        ))
        self.assertEqual(media_response.status_code, 200)
        self.assertIn(bytes(media_response.body), (PNG_BYTES, WAV_BYTES))

    def test_upload_rejects_non_media_and_reports_per_file(self):
        response = asyncio.run(file_router.upload_session_media(
            files=[self._upload_file("doc.pdf", b"%PDF-1.4"), self._upload_file("ok.png", PNG_BYTES)],
            session_id=TEST_SESSION,
        ))
        body = json.loads(response.body.decode("utf-8"))
        self.assertEqual(body["success"], 1)
        self.assertEqual(body["failed"], 1)
        failed = next(result for result in body["results"] if result["status"] != "success")
        self.assertIn("不支持的媒体类型", failed["message"])

    def test_get_missing_media_404(self):
        from starlette.requests import Request

        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(file_router.get_session_media(
                request=Request(scope={"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""}),
                name="ghost.png", session_id=TEST_SESSION,
            ))
        self.assertEqual(ctx.exception.status_code, 404)


class MultimodalHistoryReplayTests(unittest.TestCase):
    def test_round_entry_with_list_content_keeps_text(self):
        round_entry = {
            "event": "chat_round",
            "question": "这是什么图",
            "status": "done",
            "events": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "这是什么图"},
                        {"type": "image_url", "image_url": {"url": "media://abc_def123456.png"}},
                    ],
                },
                {"role": "assistant", "content": "这是一张测试图"},
                {"role": "assistant", "done": "[DONE]"},
            ],
        }
        messages = round_entry_to_context_messages(round_entry, 0)
        self.assertEqual(messages[0]["role"], "user")
        # 历史轮次用户消息为纯文本口径：媒体部件（media:// 引用）替换为
        # [图片] 占位引用，不回传图片数据；仅当前轮消息保留多部件并解析
        self.assertEqual(messages[0]["content"], "这是什么图\n[图片]")
        self.assertEqual(messages[1]["role"], "assistant")

    def test_message_model_accepts_multimodal_content(self):
        message = Message(role="user", content=[
            {"type": "text", "text": "hi"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ])
        dumped = message.model_dump()
        self.assertIsInstance(dumped["content"], list)
        self.assertEqual(dumped["content"][1]["type"], "image_url")


class RoundQuestionTests(unittest.TestCase):
    """多模态消息的轮次问题提取（会话标题/user_questions 依赖）。"""

    def test_record_message_extracts_question_from_multimodal(self):
        from memory.chat_round_store import ChatRoundStore

        store = ChatRoundStore(session_id=TEST_SESSION)
        store.record_message({
            "role": "user",
            "content": [
                {"type": "text", "text": "这是什么图"},
                {"type": "image_url", "image_url": {"url": "media://x.png"}},
            ],
        })
        # 问题为纯文本（不带 [图片] 占位），供标题与 user_questions 使用
        self.assertEqual(store.pending_round["question"], "这是什么图")

    def test_record_message_image_only_uses_placeholder(self):
        from memory.chat_round_store import ChatRoundStore

        store = ChatRoundStore(session_id=TEST_SESSION)
        store.record_message({
            "role": "user",
            "content": [{"type": "image_url", "image_url": {"url": "media://x.png"}}],
        })
        # 纯图无文字：问题留空（标题回退 session_id），轮次仍正常建立
        self.assertEqual(store.pending_round["question"], "")

    def test_recompute_meta_extracts_question_from_events(self):
        """存量兼容：修复前落盘的空 question 轮次，重建 user_questions 时从事件回提取。"""
        from memory.chat_memory import _recompute_meta_from_entries

        entries = [{
            "event": "chat_round",
            "question": "",
            "status": "done",
            "events": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "帮我看看报错"},
                        {"type": "image_url", "image_url": {"url": "media://err.png"}},
                    ],
                },
                {"role": "assistant", "content": "好的"},
            ],
        }]
        meta = _recompute_meta_from_entries(TEST_SESSION, None, entries)
        self.assertEqual(meta["user_questions"], ["帮我看看报错"])




class RoundCheckpointTests(unittest.TestCase):
    """pending 轮次逐事件检查点：重启/崩溃后工具调用与思考过程不丢。"""

    def setUp(self):
        _cleanup_test_session()

    def tearDown(self):
        _cleanup_test_session()

    def _read_meta(self, manager):
        import memory.chat_memory as cm
        with manager._lock:
            meta, entries = cm._load_meta_and_entries(manager._file_path, manager.session_id)
        # _load 不消费检查点（恢复只在管理器新建时做），此处手动读取原始 meta
        rows = cm._read_jsonlines(manager._file_path)
        raw_meta = rows[0].get("_meta", {}) if rows and rows[0].get("_meta") else {}
        return raw_meta, meta, entries

    def test_checkpoint_written_per_event_before_finalize(self):
        from memory.chat_memory import ChatMemoryManager

        manager = ChatMemoryManager(TEST_SESSION)
        asyncio.run(manager.add_chat_history({"role": "user", "content": "帮我看下时间"}))
        asyncio.run(manager.add_chat_history({
            "role": "assistant",
            "content": "我先查一下",
            "reasoning_content": "需要调用时间工具",
            "tool_calls": [{"id": "call_1", "type": "function",
                            "function": {"name": "format_current_time", "arguments": "{}"}}],
        }))
        # 检查点为侧车文件（仅当前轮事件，不重写整个历史 JSONL）
        sidecar = manager._pending_checkpoint_path
        self.assertTrue(sidecar.exists())
        checkpoint = json.loads(sidecar.read_text(encoding="utf-8"))
        self.assertEqual(len(checkpoint["events"]), 2)
        self.assertTrue(checkpoint["events"][1]["tool_calls"])
        self.assertTrue(checkpoint["events"][1]["reasoning_content"])

    def test_crash_recovery_restores_interrupted_round(self):
        from memory.chat_memory import ChatMemoryManager

        manager = ChatMemoryManager(TEST_SESSION)
        asyncio.run(manager.add_chat_history({"role": "user", "content": "帮我看下时间"}))
        asyncio.run(manager.add_chat_history({
            "role": "assistant",
            "content": "我先查一下",
            "reasoning_content": "需要调用时间工具",
            "tool_calls": [{"id": "call_1", "type": "function",
                            "function": {"name": "format_current_time", "arguments": "{}"}}],
        }))
        asyncio.run(manager.add_chat_history({
            "role": "tool", "tool_call_id": "call_1", "content": "16:00", "_tool_name": "format_current_time",
        }))
        # 模拟崩溃重启：把检查点写者 PID 改成已不存在的进程，再新建管理器
        # 实例触发检查点恢复（写者存活校验要求写进程已死亡才恢复，
        # 同进程内直接新建管理器会被判定为"轮次仍在生成中"而跳过恢复）
        sidecar = manager._pending_checkpoint_path
        checkpoint = json.loads(sidecar.read_text(encoding="utf-8"))
        checkpoint["writer_pid"] = -2147483000
        sidecar.write_text(json.dumps(checkpoint, ensure_ascii=False), encoding="utf-8")
        recovered_manager = ChatMemoryManager(TEST_SESSION)
        raw_meta, _, entries = self._read_meta(recovered_manager)
        self.assertIsNone(raw_meta.get("_pending_round_checkpoint"))
        rounds = [e for e in entries if e.get("event") == "chat_round"]
        self.assertEqual(len(rounds), 1)
        self.assertEqual(rounds[0]["status"], "interrupted")
        roles = [e.get("role") for e in rounds[0]["events"]]
        self.assertEqual(roles, ["user", "assistant", "tool"])
        self.assertEqual(rounds[0]["question"], "帮我看下时间")

    def test_finalize_clears_checkpoint_without_duplicates(self):
        from memory.chat_memory import ChatMemoryManager

        manager = ChatMemoryManager(TEST_SESSION)
        asyncio.run(manager.add_chat_history({"role": "user", "content": "你好"}))
        asyncio.run(manager.add_chat_history({"role": "assistant", "content": "你好！"}))
        asyncio.run(manager.add_chat_history({"role": "assistant", "done": "[DONE]"}))
        raw_meta, _, entries = self._read_meta(manager)
        self.assertIsNone(raw_meta.get("_pending_round_checkpoint"))
        rounds = [e for e in entries if e.get("event") == "chat_round"]
        self.assertEqual(len(rounds), 1)
        self.assertEqual(rounds[0]["status"], "done")
        self.assertEqual(len(rounds[0]["events"]), 3)




class ImageConversionTests(unittest.TestCase):
    """ICO/TIFF 等视觉模型不原生接受的格式：上传时自动转 PNG。"""

    def setUp(self):
        _cleanup_test_session()

    def tearDown(self):
        _cleanup_test_session()

    @staticmethod
    def _ico_bytes():
        from PIL import Image
        import io as _io
        img = Image.new("RGBA", (32, 32), (255, 0, 0, 128))
        buf = _io.BytesIO()
        img.save(buf, format="ICO")
        return buf.getvalue()

    @staticmethod
    def _tiff_bytes():
        from PIL import Image
        import io as _io
        img = Image.new("RGB", (24, 24), (10, 200, 10))
        buf = _io.BytesIO()
        img.save(buf, format="TIFF")
        return buf.getvalue()

    def test_ico_converted_to_png(self):
        saved = fm.save_session_media(TEST_SESSION, "图标.ico", self._ico_bytes())
        self.assertTrue(saved["stored_name"].endswith(".png"))
        self.assertEqual(saved["mime"], "image/png")
        stored = fm.resolve_media_path(TEST_SESSION, saved["media_ref"])
        self.assertIsNotNone(stored)
        from PIL import Image
        import io as _io
        img = Image.open(_io.BytesIO(stored.read_bytes()))
        self.assertEqual(img.format, "PNG")

    def test_tiff_converted_to_png(self):
        saved = fm.save_session_media(TEST_SESSION, "page.tiff", self._tiff_bytes())
        self.assertTrue(saved["stored_name"].endswith(".png"))
        self.assertEqual(saved["mime"], "image/png")

    def test_corrupt_ico_rejected_with_clear_message(self):
        with self.assertRaises(ValueError) as ctx:
            fm.save_session_media(TEST_SESSION, "broken.ico", b"not-an-ico")
        self.assertIn("自动转换为 PNG 失败", str(ctx.exception))

    def test_converted_ico_resolves_to_png_data_url(self):
        saved = fm.save_session_media(TEST_SESSION, "图标.ico", self._ico_bytes())
        content = [{"type": "image_url", "image_url": {"url": saved["media_ref"]}}]
        resolved, unresolved = fm.resolve_media_content_parts(TEST_SESSION, content)
        self.assertEqual(unresolved, [])
        self.assertTrue(resolved[0]["image_url"]["url"].startswith("data:image/png;base64,"))




class RangeRequestTests(unittest.TestCase):
    """媒体/文档读取接口的 Range 支持（音频/视频进度条即时跳转依赖）。"""

    def test_parse_range_header(self):
        parse = file_router._parse_range_header
        self.assertEqual(parse("bytes=0-4", 100), (0, 4))
        self.assertEqual(parse("bytes=10-", 100), (10, 99))
        self.assertEqual(parse("bytes=95-", 100), (95, 99))
        self.assertEqual(parse("bytes=-4", 100), (96, 99))
        self.assertEqual(parse("bytes=99-200", 100), (99, 99))
        self.assertIsNone(parse("bytes=100-", 100))       # 完全越界
        self.assertIsNone(parse("bytes=5-2", 100))        # 起止倒挂
        self.assertIsNone(parse("bytes=0-1,5-9", 100))    # 多区间不支持
        self.assertIsNone(parse("wibble", 100))
        self.assertIsNone(parse(None, 100))

    def test_get_session_media_206_and_full(self):
        from starlette.requests import Request

        response = asyncio.run(file_router.upload_session_media(
            files=[MediaEndpointTests._upload_file("音频.wav", WAV_BYTES)],
            session_id=TEST_SESSION,
        ))
        body = json.loads(response.body.decode("utf-8"))
        stored = body["results"][0]["stored_name"]

        def make_request(headers):
            return Request(scope={
                "type": "http", "method": "GET", "path": "/",
                "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
                "query_string": b"",
            })

        partial = asyncio.run(file_router.get_session_media(
            request=make_request({"Range": "bytes=2-5"}),
            name=stored, session_id=TEST_SESSION,
        ))
        self.assertEqual(partial.status_code, 206)
        self.assertEqual(partial.headers["content-range"], f"bytes 2-5/{len(WAV_BYTES)}")
        self.assertEqual(bytes(partial.body), WAV_BYTES[2:6])

        full = asyncio.run(file_router.get_session_media(
            request=make_request({}),
            name=stored, session_id=TEST_SESSION,
        ))
        self.assertEqual(full.status_code, 200)
        self.assertEqual(full.headers["accept-ranges"], "bytes")
        self.assertEqual(bytes(full.body), WAV_BYTES)


class StreamMediaTests(unittest.TestCase):
    """大文件（视频）流式落盘与按类别大小上限。"""

    def setUp(self):
        _cleanup_test_session()

    def tearDown(self):
        _cleanup_test_session()

    def test_media_size_limit_by_kind(self):
        self.assertEqual(fm.media_size_limit("image"), 20 * 1024 * 1024)
        self.assertEqual(fm.media_size_limit("audio"), 20 * 1024 * 1024)
        self.assertEqual(fm.media_size_limit("video"), 500 * 1024 * 1024)
        self.assertEqual(fm.media_size_limit(None), 20 * 1024 * 1024)

    def test_save_session_media_stream_happy_path(self):
        import io as _io

        saved = fm.save_session_media_stream(TEST_SESSION, "clip.mp4", _io.BytesIO(b"fake-video" * 10))
        self.assertEqual(saved["kind"], "video")
        self.assertTrue(saved["stored_name"].endswith(".mp4"))
        stored = fm.resolve_media_path(TEST_SESSION, saved["media_ref"])
        self.assertEqual(stored.read_bytes(), b"fake-video" * 10)

    def test_save_session_media_stream_aborts_over_limit(self):
        import io as _io

        class LimitedStream:
            """模拟超过上限的流：每次吐 1MB，共 21MB（图片/音频上限 20MB）"""

            def __init__(self):
                self.remaining = 21 * 1024 * 1024

            def read(self, n):
                chunk = min(n, self.remaining)
                self.remaining -= chunk
                return b"0" * chunk

        # 用 audio 类别（20MB 上限）验证超限中止
        with self.assertRaises(ValueError) as ctx:
            fm.save_session_media_stream(TEST_SESSION, "big.wav", LimitedStream())
        self.assertIn("超过限制", str(ctx.exception))
        # 半成品已清理
        media_dir = fm.HISTORY_ROOT / TEST_SESSION / "media"
        self.assertEqual(list(media_dir.glob("*")), [])

    def test_save_session_media_image_over_limit(self):
        with self.assertRaises(ValueError) as ctx:
            fm.save_session_media(TEST_SESSION, "big.png", b"0" * (21 * 1024 * 1024))
        self.assertIn("超过限制", str(ctx.exception))


def _make_png_bytes(width: int, height: int) -> bytes:
    """生成真实 PNG 字节（逐像素伪随机噪声，压缩率低），供缩略图降采样测试。"""
    import random

    from PIL import Image as _Image

    rng = random.Random(42)
    img = _Image.new("RGB", (width, height))
    img.putdata([(rng.randrange(256), rng.randrange(256), rng.randrange(256))
                 for _ in range(width * height)])
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


class ImageThumbnailTests(unittest.TestCase):
    """大图发送上游前自动降采样：>2MB 生成 JPEG 缩略图，原图落盘不变。"""

    def setUp(self):
        _cleanup_test_session()

    def tearDown(self):
        _cleanup_test_session()

    def test_oversized_image_resolves_to_jpeg_thumbnail(self):
        # 2400×800 随机噪声 PNG：真实超过 2MB 且长边需要降采样
        big_png = _make_png_bytes(2400, 800)
        self.assertGreater(len(big_png), fm.IMAGE_THUMBNAIL_THRESHOLD_BYTES)
        saved = fm.save_session_media(TEST_SESSION, "大截图.png", big_png)
        content = [{"type": "image_url", "image_url": {"url": saved["media_ref"]}}]
        resolved, unresolved = fm.resolve_media_content_parts(TEST_SESSION, content)
        self.assertEqual(unresolved, [])
        url = resolved[0]["image_url"]["url"]
        self.assertTrue(url.startswith("data:image/jpeg;base64,"), url[:40])
        raw = base64.b64decode(url.split(",", 1)[1])
        # 缩略图应显著小于原图（长边压到 1568 + JPEG 压缩）
        self.assertLess(len(raw), fm.IMAGE_THUMBNAIL_THRESHOLD_BYTES)
        from PIL import Image as _Image
        thumb = _Image.open(io.BytesIO(raw))
        self.assertLessEqual(max(thumb.size), fm.IMAGE_THUMBNAIL_MAX_EDGE)
        self.assertEqual(max(thumb.size), fm.IMAGE_THUMBNAIL_MAX_EDGE)
        # 原图完整保留
        stored = fm.resolve_media_path(TEST_SESSION, saved["media_ref"])
        self.assertTrue(stored.name.endswith(".png"))

    def test_small_image_keeps_original_format(self):
        saved = fm.save_session_media(TEST_SESSION, "小图.png", PNG_BYTES)
        content = [{"type": "image_url", "image_url": {"url": saved["media_ref"]}}]
        resolved, unresolved = fm.resolve_media_content_parts(TEST_SESSION, content)
        self.assertEqual(unresolved, [])
        url = resolved[0]["image_url"]["url"]
        self.assertTrue(url.startswith("data:image/png;base64,"))

    def test_thumbnail_cache_reused_and_invalidated(self):
        # 用小阈值 + 小尺寸噪声图验证缓存逻辑（避免测试构造超大文件）
        saved = fm.save_session_media(
            TEST_SESSION, "缓存图.png", _make_png_bytes(300, 300)
        )
        ref = saved["media_ref"]
        content = [{"type": "image_url", "image_url": {"url": ref}}]
        media_path = fm.resolve_media_path(TEST_SESSION, ref)
        with patch.object(fm, "IMAGE_THUMBNAIL_THRESHOLD_BYTES", 8192):
            first, _ = fm.resolve_media_content_parts(TEST_SESSION, content)
            second, _ = fm.resolve_media_content_parts(TEST_SESSION, content)
            self.assertEqual(
                first[0]["image_url"]["url"], second[0]["image_url"]["url"]
            )
            thumbs_dir = fm.HISTORY_ROOT / TEST_SESSION / "thumbs"
            thumbs = list(thumbs_dir.glob("*.thumb.jpg"))
            self.assertEqual(len(thumbs), 1)
            cached_before = thumbs[0].read_text(encoding="ascii")
            # 原图被替换为不同尺寸内容（字节指纹变化）后缓存失效重建
            media_path.write_bytes(_make_png_bytes(360, 260))
            os.utime(media_path, (time.time() + 5, time.time() + 5))
            third, _ = fm.resolve_media_content_parts(TEST_SESSION, content)
            self.assertTrue(third[0]["image_url"]["url"].startswith("data:image/jpeg;base64,"))
            self.assertEqual(len(list(thumbs_dir.glob("*.thumb.jpg"))), 1)
            self.assertNotEqual(
                cached_before, thumbs[0].read_text(encoding="ascii")
            )

    def test_gif_skips_thumbnail(self):
        # 小阈值下的小 GIF：动图跳过缩略图，保持原格式发送
        from PIL import Image as _Image

        out = io.BytesIO()
        _Image.new("P", (64, 48), color=3).save(out, format="GIF")
        gif_bytes = out.getvalue()
        self.assertGreater(len(gif_bytes), 32)
        saved = fm.save_session_media(TEST_SESSION, "动图.gif", gif_bytes)
        content = [{"type": "image_url", "image_url": {"url": saved["media_ref"]}}]
        with patch.object(fm, "IMAGE_THUMBNAIL_THRESHOLD_BYTES", 16):
            resolved, unresolved = fm.resolve_media_content_parts(TEST_SESSION, content)
        self.assertEqual(unresolved, [])
        url = resolved[0]["image_url"]["url"]
        self.assertTrue(url.startswith("data:image/gif;base64,"))

    def test_corrupt_large_image_falls_back_to_original(self):
        # 超过阈值但不是合法图片：缩略图失败应回退原样发送
        junk = b"\x89PNG\r\n\x1a\n" + b"corrupt" * (fm.IMAGE_THUMBNAIL_THRESHOLD_BYTES // 7 + 1)
        saved = fm.save_session_media(TEST_SESSION, "损坏.png", junk)
        content = [{"type": "image_url", "image_url": {"url": saved["media_ref"]}}]
        resolved, unresolved = fm.resolve_media_content_parts(TEST_SESSION, content)
        self.assertEqual(unresolved, [])
        url = resolved[0]["image_url"]["url"]
        self.assertTrue(url.startswith("data:image/png;base64,"))
        self.assertEqual(base64.b64decode(url.split(",", 1)[1]), junk)


if __name__ == "__main__":
    unittest.main()
