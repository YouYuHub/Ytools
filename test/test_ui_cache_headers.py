"""静态资源版本参数的缓存策略。"""
import asyncio

from fastapi import Request
from fastapi.responses import Response

from util.ui_cache import ui_cache_headers


async def _response(_request):
    return Response("ok")


def _request(path: str, query: bytes = b"") -> Request:
    return Request({
        "type": "http", "method": "GET", "path": path,
        "query_string": query, "headers": [], "scheme": "http",
        "server": ("test", 80),
    })


def test_ui_asset_cache_headers():
    hashed = asyncio.run(ui_cache_headers(_request("/ui/js/app.js", b"v=012345abcdef"), _response))
    assert "immutable" in hashed.headers["Cache-Control"]
    page = asyncio.run(ui_cache_headers(_request("/ui/"), _response))
    assert page.headers["Cache-Control"] == "no-cache"
    worker = asyncio.run(ui_cache_headers(_request("/ui/js/history_parse_worker.js"), _response))
    assert worker.headers["Cache-Control"] == "no-cache"
