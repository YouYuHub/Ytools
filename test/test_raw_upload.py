"""Raw upload protocol: no multipart parser and bounded request streaming."""

import asyncio
import uuid

import httpx
from fastapi import FastAPI

from memory.chat_memory import ChatMemoryManager
from routers.file_router import api_file_router


def test_document_upload_uses_octet_stream_and_filename():
    session_id = "raw-upload-" + uuid.uuid4().hex[:12]
    app = FastAPI()
    app.include_router(api_file_router)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            path = "/file/upload_session_files"
            params = {"session_id": session_id, "filename": "notes.txt"}
            bad = await client.post(path, params=params, content=b"hello", headers={"Content-Type": "text/plain"})
            assert bad.status_code == 415
            ok = await client.post(path, params=params, content=b"hello", headers={"Content-Type": "application/octet-stream"})
            assert ok.status_code == 200
            assert ok.json()["success"] == 1
            assert ok.json()["results"][0]["filename"] == "notes.txt"
            empty = await client.post(path, params=params, content=b"", headers={"Content-Type": "application/octet-stream"})
            assert empty.status_code == 400
            oversized = await client.post(path, params=params, content=b"x" * (10 * 1024 * 1024 + 1),
                                          headers={"Content-Type": "application/octet-stream"})
            assert oversized.status_code == 413

    try:
        asyncio.run(exercise())
    finally:
        ChatMemoryManager.delete_chat_session_file(session_id)
