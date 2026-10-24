"""Offline regressions for send preparation: no provider requests."""
import copy
import asyncio
import json
import random

import pytest

from chat import chat_llm, messages_client
from chat.chat_llm import ChatLLM
from chat.protocols.messages import MessagesProtocolError
from config import ChatLLMRequest
from factory.agent_runtime import chat_runtime
from util.token_estimate import text_token_estimate


def test_fast_character_estimate_preserves_formula():
    samples = ["", "a", "abc", "a" * 400, "汉字🙂\ud800\x00", "a汉" * 20000]
    rng = random.Random(7)
    samples += ["".join(chr(rng.randrange(0x110000)) for _ in range(1000)) for _ in range(20)]
    for text in samples:
        ascii_count = sum(ord(char) < 128 for char in text)
        old = max(1, ascii_count // 4 + len(text) - ascii_count) if text else 0
        assert text_token_estimate(text) == old
        assert chat_runtime.estimate_text_tokens(text) == old
        assert chat_llm._estimate_payload_input_tokens({"messages": [{"content": text}]}) == old


def test_messages_one_estimate_and_one_validation_per_attempt(monkeypatch):
    config = {"url": "http://offline.invalid/v1", "apiType": "messages", "selected_model_id": "m",
              "maxInputTokens": 200000}
    request = ChatLLMRequest(messages=[{"role": "user", "content": "Hello中文"}], max_tokens=4096)
    counts = {"estimate": 0, "validate": 0}
    original_estimate = chat_llm._estimate_payload_input_tokens
    original_validate = messages_client.validate_payload

    def estimate(payload):
        counts["estimate"] += 1
        return original_estimate(payload)

    def validate(payload, config, **kwargs):
        counts["validate"] += 1
        assert json.loads(kwargs["encoded_body"]) == payload
        return original_validate(payload, config, **kwargs)

    monkeypatch.setattr(chat_llm, "_estimate_payload_input_tokens", estimate)
    monkeypatch.setattr(messages_client, "validate_payload", validate)
    monkeypatch.setattr(chat_llm, "_resolve_request_hard_limit_tokens", lambda: 1048576)
    payload, _, _ = messages_client._prepare(ChatLLM, request, config, True)
    for expected in (1, 2):
        messages_client._precheck(ChatLLM, payload, config)
        _, _, wire = messages_client._wire(config, payload)
        assert json.loads(wire.split(b"\r\n\r\n", 1)[1]) == payload
        assert counts == {"estimate": expected, "validate": expected}
        payload = messages_client._continue_payload(payload, ["partial"])
    bad = copy.deepcopy(payload)
    bad["messages"].append({"role": "user", "content": [{"type": "document", "source": {
        "type": "text", "data": "x"}, "citations": {"enabled": "invalid"}}]})
    with pytest.raises(MessagesProtocolError, match="citations"):
        messages_client._wire(config, bad)


def test_disabled_debug_does_not_serialize_messages(monkeypatch):
    monkeypatch.setattr(chat_runtime, "is_debug_enabled", lambda: False)
    monkeypatch.setattr(chat_runtime.json, "dumps", lambda *a, **k: pytest.fail("unused debug serialization"))
    chat_runtime.log_upstream_assistant_messages([{"role": "assistant", "content": "x"}], source="test")


def test_chunk_first_event_does_not_wait_for_rest_of_http_chunk():
    async def run():
        reader = asyncio.StreamReader()
        first, last = b'data: {"text":"first"}\n\n', b'data: [DONE]\n\n'
        reader.feed_data(f"{len(first + last):x}\r\n".encode() + first)
        lines = ChatLLM._aiter_sse_body_lines(reader, True, 1, None)
        assert await asyncio.wait_for(anext(lines), 0.1) == first.splitlines()[0]
        reader.feed_data(last + b"\r\n0\r\n\r\n")
        reader.feed_eof()
        assert b"data: [DONE]" in [line async for line in lines]
    asyncio.run(run())


@pytest.mark.parametrize("chunked", [True, False])
def test_sse_utf8_arbitrary_byte_boundaries(chunked):
    async def run():
        raw = 'data: {"text":"中文🙂"}\n\ndata: [DONE]\n\n'.encode()
        wire = b"".join(b"1\r\n" + bytes([b]) + b"\r\n" for b in raw) + b"0\r\n\r\n" if chunked else raw
        reader = asyncio.StreamReader()
        reader.feed_data(wire)
        reader.feed_eof()
        lines = [line async for line in ChatLLM._aiter_sse_body_lines(reader, chunked, 1, None)]
        assert lines == raw.split(b"\n")[:-1]
    asyncio.run(run())


def test_truncated_chunk_still_raises():
    async def run():
        reader = asyncio.StreamReader()
        reader.feed_data(b"10\r\nx")
        reader.feed_eof()
        with pytest.raises(asyncio.IncompleteReadError):
            _ = [line async for line in ChatLLM._aiter_sse_body_lines(reader, True, 1, None)]
    asyncio.run(run())


def test_timings_distinguish_activity_from_content_across_wait_for_tasks(monkeypatch):
    from util import api_timing
    request = ChatLLMRequest(messages=[], session_id="timing-test")
    recorded, closed = [], []
    monkeypatch.setattr(api_timing.logger, "info", lambda template, data: recorded.append(json.loads(data)))
    async def source():
        try:
            api_timing.attempt("test")
            api_timing.mark("request_ready", payload_bytes=40)
            api_timing.mark("request_sent")
            api_timing.mark("first_upstream_event")
            yield 'data: {"upstream_activity": true}\n\n'
            yield 'data: {"content": "hello"}\n\n'
        finally:
            closed.append(True)
    async def run():
        stream = api_timing.traced_call(source, request, True)
        assert "upstream_activity" in await asyncio.wait_for(anext(stream), 1)
        assert api_timing.CURRENT.get() is None
        assert "hello" in await asyncio.wait_for(anext(stream), 1)
        await stream.aclose()
        assert api_timing.CURRENT.get() is None
    asyncio.run(run())
    assert closed == [True]
    attempt = recorded[0]["attempts"][0]
    assert attempt["first_visible_delta_ms"] >= attempt["first_upstream_event_ms"]
    assert "hello" not in json.dumps(recorded)


def test_unchanged_round_does_not_write_task(monkeypatch):
    from memory import task_cost_store
    from contextlib import contextmanager
    writes = []
    class Database:
        def execute(self, *args):
            pass
    @contextmanager
    def database():
        yield Database()
    monkeypatch.setattr(task_cost_store, "database", database)
    monkeypatch.setattr(task_cost_store, "_task", lambda *args: {"round": 3})
    monkeypatch.setattr(task_cost_store, "_put_task", lambda *args: writes.append(args[-1]))
    task_cost_store.update_round("t", 3)
    assert not writes
    task_cost_store.update_round("t", 4)
    assert writes == [{"round": 4}]


def test_timing_log_failure_cannot_change_result_or_leak_context(monkeypatch):
    from util import api_timing
    monkeypatch.setattr(api_timing.logger, "info", lambda *args: (_ for _ in ()).throw(OSError("log unavailable")))
    request = ChatLLMRequest(messages=[])
    assert api_timing.traced_call(lambda: {"content": "OK"}, request, False) == {"content": "OK"}
    assert api_timing.CURRENT.get() is None
