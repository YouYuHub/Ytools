# coding: utf-8
"""媒体伪标签删除链路测试：

- ChatMemoryManager.replace_content_in_chat_session（JSONL 全记录字符串替换）
- POST /chat_history/remove_media_tag（路由参数校验）
- GET /file/get_local_file（绝对路径/相对路径/不存在 404）
"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException
from starlette.requests import Request

from memory.chat_memory import ChatMemoryManager
from routers.chat_router import remove_media_tag_from_history, MediaTagRemoveRequest
from routers.file_router import get_local_file


_ROUND_WITH_TAG = {
    "event": "chat_round",
    "question": "展示图片",
    "started_at": "2026-09-08 10:00:00",
    "events": [
        {"timestamp": "2026-09-08 10:00:00", "role": "user", "content": "展示图片"},
        {
            "timestamp": "2026-09-08 10:00:05",
            "role": "assistant",
            "content": '好的：<image src="./out/a.png" alt="图表"></image>\n\n<audio src="C:/media/b.mp3" title="录音"></audio>',
        },
        {"timestamp": "2026-09-08 10:00:06", "role": "assistant", "done": "[DONE]"},
    ],
    "status": "done",
}


class ReplaceMediaTagTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        from memory import chat_memory

        self._backup = chat_memory.HISTORY_ROOT
        chat_memory.HISTORY_ROOT = Path(self._tmpdir.name)

    def tearDown(self):
        from memory import chat_memory

        chat_memory.HISTORY_ROOT = self._backup
        self._tmpdir.cleanup()

    def _prepare(self, session_id: str):
        manager = ChatMemoryManager(session_id)

        async def prepare():
            await manager.add_chat_history({"role": "user", "content": "展示图片"})
            await manager.add_chat_history({
                "role": "assistant",
                "content": '好的：<image src="./out/a.png" alt="图表"></image>',
            })
            await manager.add_chat_history({
                "role": "assistant",
                "content": '再看：<image src="./out/a.png" alt="图表"></image>',
            })
            await manager.add_chat_history({"role": "assistant", "done": "[DONE]"})

        asyncio.run(prepare())

    def _read_session(self, session_id: str):
        from memory import chat_memory

        file_path = chat_memory._get_chat_history_file(session_id)
        return chat_memory._load_meta_and_entries(file_path, session_id)

    def test_replace_replaces_all_occurrences(self):
        self._prepare("replace-media")
        result = ChatMemoryManager.replace_content_in_chat_session(
            "replace-media",
            '<image src="./out/a.png" alt="图表"></image>',
            "用户已删除/文件不存在",
        )
        self.assertEqual(result["state"], "succeed")
        self.assertEqual(result["replaced"], 2)
        _meta, entries = self._read_session("replace-media")
        joined = json.dumps(entries, ensure_ascii=False)
        self.assertNotIn("<image", joined)
        self.assertEqual(joined.count("用户已删除/文件不存在"), 2)

    def test_replace_miss_returns_failed_without_rewrite(self):
        self._prepare("replace-media-miss")
        result = ChatMemoryManager.replace_content_in_chat_session(
            "replace-media-miss", "<pdf src=\"ghost.pdf\"></pdf>", "用户已删除/文件不存在"
        )
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["replaced"], 0)

    def test_replace_empty_find_raises(self):
        with self.assertRaises(ValueError):
            ChatMemoryManager.replace_content_in_chat_session("replace-media-empty", "  ", "x")

    def test_router_endpoint_replaces_in_history(self):
        self._prepare("replace-media-router")
        request = MediaTagRemoveRequest(
            session_id="replace-media-router",
            tag='<image src="./out/a.png" alt="图表"></image>',
        )
        result = asyncio.run(remove_media_tag_from_history(request))
        self.assertEqual(json.loads(result.body.decode("utf-8"))["state"], "succeed")
        _meta, entries = self._read_session("replace-media-router")
        self.assertNotIn("<image", json.dumps(entries, ensure_ascii=False))

    def test_router_endpoint_rejects_empty_tag(self):
        request = MediaTagRemoveRequest(session_id="replace-media-router", tag="   ")
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(remove_media_tag_from_history(request))
        self.assertEqual(ctx.exception.status_code, 400)


class LocalFileEndpointTests(unittest.TestCase):
    def _request(self):
        return Request(scope={
            "type": "http", "method": "GET", "path": "/",
            "headers": [], "query_string": b"",
        })

    def test_absolute_path_served_with_mime(self):
        target = Path(__file__).resolve()
        response = asyncio.run(get_local_file(self._request(), path=str(target), session_id="default"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.media_type, "text/x-python")

    def test_relative_path_resolves_against_cwd_fallback(self):
        # 无会话覆盖/全局默认时按进程 cwd 解析（显式 patch 掉会话工作目录
        # 解析，避免 .env 持久化的 DEFAULT_CHAT_WORK_DIR 干扰）
        from unittest.mock import patch

        with patch("routers.file_router.resolve_session_work_dir", return_value=(None, None)):
            response = asyncio.run(get_local_file(
                self._request(), path=str(Path(__file__).relative_to(Path.cwd())), session_id="default"
            ))
        self.assertEqual(response.status_code, 200)

    def test_missing_file_returns_404(self):
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(get_local_file(
                self._request(), path="Z:/definitely/not/exists/ghost.png", session_id="default"
            ))
        self.assertEqual(ctx.exception.status_code, 404)

    def test_empty_path_returns_400(self):
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(get_local_file(self._request(), path="  ", session_id="default"))
        self.assertEqual(ctx.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
