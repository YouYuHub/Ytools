import asyncio
import json
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from factory import chat_factory
from routers import chat_router


class Request:
    def __init__(self, body):
        self.body = body

    async def json(self):
        return self.body


def test_route_accepts_image_only_and_quotes():
    content = [{"type": "image_url", "image_url": {"url": "media://image.png"}}]
    enqueue = AsyncMock(return_value={"ok": True})
    with patch.object(chat_factory, "inject_user_message", enqueue):
        response = asyncio.run(chat_router.inject_message(Request({
            "session_id": "inject-ut", "content": content, "message_id": "picture",
            "quotes": [{"text": "引用"}],
        })))
    assert json.loads(response.body)["ok"]
    assert enqueue.call_args.args[1] == {"role": "user", "content": content, "message_id": "picture",
                                        "quotes": [{"text": "引用"}]}


def test_route_validates_quotes_before_enqueue():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(chat_router.inject_message(Request({"content": "正文", "quotes": [{"text": "a" * 4001}]})))
    assert exc.value.status_code == 400


def test_inline_cancel_id_handles_picture_only_and_duplicate_text():
    stream = SimpleNamespace(injected_messages=deque([
        {"message_id": "a", "content": "同样文本"}, {"message_id": "b", "content": "同样文本"},
        {"message_id": "image", "content": [{"type": "image_url", "image_url": {"url": "media://x.png"}}]},
    ]))
    with patch.object(chat_factory, "_generation_use_worker", return_value=False), \
            patch.object(chat_factory, "_get_session_stream", return_value=stream):
        assert asyncio.run(chat_factory.cancel_injected_message("ut", "", message_id="image"))["ok"]
        assert asyncio.run(chat_factory.cancel_injected_message("ut", "同样文本", message_id="b"))["ok"]
        assert [m["message_id"] for m in stream.injected_messages] == ["a"]
        assert not asyncio.run(chat_factory.cancel_injected_message("ut", "", message_id="b"))["ok"]


@pytest.mark.parametrize("removed", [True, False])
def test_worker_cancel_waits_for_actual_ack(removed):
    class Proxy:
        def add_handler(self, handler): self.handler = handler
        def remove_handler(self, handler): self.handler = None
        def cancel_injected_message(self, text, **kwargs):
            self.handler({"type": "injected_cancelled", "request_id": kwargs["request_id"], "ok": removed})
            return True
    proxy = Proxy()
    with patch.object(chat_factory, "_generation_use_worker", return_value=True), \
            patch.object(chat_factory, "peek_worker_proxy", return_value=proxy):
        result = asyncio.run(chat_factory.cancel_injected_message("ut", "", message_id="image"))
    assert result["ok"] is removed
    assert proxy.handler is None


@pytest.mark.parametrize("accepted", [True, False])
def test_worker_inject_waits_for_actual_acceptance(accepted):
    class Proxy:
        def add_handler(self, handler): self.handler = handler
        def remove_handler(self, handler): self.handler = None
        def inject_message(self, message, **kwargs):
            self.handler({"type": "injected", "request_id": "unrelated", "ok": not accepted})
            self.handler({"type": "injected", "request_id": kwargs["request_id"], "ok": accepted})
            return True
    proxy = Proxy()
    with patch.object(chat_factory, "_generation_use_worker", return_value=True), \
            patch.object(chat_factory, "peek_worker_proxy", return_value=proxy):
        result = asyncio.run(chat_factory.inject_user_message("ut", {"content": "图片引导"}))
    assert result["ok"] is accepted
    assert proxy.handler is None
