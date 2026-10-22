"""Real loopback sockets exercise HTTP framing, cancellation and state recovery."""
import asyncio
from collections import deque
import gzip
import json
import socketserver
import sys
import threading
import time

import pytest

from chat import responses_client
from chat.chat_llm import ChatLLM
from chat.protocols.errors import ResponsesProtocolError
from chat.protocols import responses_store as store
from chat.protocols.responses_content import convert_messages
from chat.responses_transport import AsyncClient, SyncClient, _SSEDecoder, _wire
from config import ChatLLMRequest
from test_responses_api import answer, CONFIG, turn


@pytest.fixture
def journal(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_root", lambda: tmp_path)
    monkeypatch.setattr(store, "_mirror", lambda *args: None)
    monkeypatch.setattr(responses_client, "_retry_delay", lambda *args: 0)
    # This suite supplies exactly three retry responses, independently of .env.
    monkeypatch.setattr(responses_client, "_maximum", lambda: 3)


@pytest.fixture
def server():
    scripts, requests, failures = deque(), [], []
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            try:
                start = self.rfile.readline().decode().strip()
                headers = {}
                while True:
                    line = self.rfile.readline()
                    if line in {b"\r\n", b"\n", b""}:
                        break
                    key, value = line.decode().split(":", 1)
                    headers[key.lower()] = value.strip()
                body = self.rfile.read(int(headers.get("content-length", 0)))
                requests.append((start, headers, json.loads(body) if body else None))
                script = scripts.popleft()
                if callable(script):
                    script(self)
                else:
                    for packet in script:
                        self.connection.sendall(packet)
            except (ConnectionError, BrokenPipeError):
                pass
            except Exception as exc:
                failures.append(exc)
    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True
        allow_reuse_address = True
    service = Server(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=service.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{service.server_address[1]}/gateway/v1", scripts, requests
    finally:
        service.shutdown()
        service.server_close()
        worker.join(1)
        assert not failures, failures


def http(body, *, status=200, chunked=False, compressed=False, content_type="application/json"):
    headers = [f"HTTP/1.1 {status} Status", "Content-Type: " + content_type]
    if compressed:
        body = gzip.compress(body)
        headers.append("Content-Encoding: gzip")
    if chunked:
        headers.append("Transfer-Encoding: chunked")
        chunks = [body[i:i + 7] for i in range(0, len(body), 7)]
        body = b"".join(f"{len(chunk):x};test=yes\r\n".encode() + chunk + b"\r\n" for chunk in chunks)
        body += b"0\r\nX-Trailer: done\r\n\r\n"
    else:
        headers.append("Content-Length: " + str(len(body)))
    return ("\r\n".join(headers) + "\r\n\r\n").encode() + body


def sse(raw=None, *, complete=True):
    raw = raw or answer(text="你好")
    events = [{"type": "response.created", "response": {"id": raw["id"]}},
              {"type": "response.output_text.delta", "delta": "你"}]
    if complete:
        events.append({"type": "response.completed", "response": raw})
    return b": heartbeat\r\n\r\n" + b"".join(
        ("event: " + event["type"] + "\r\ndata: " + json.dumps(event, ensure_ascii=False) + "\r\n\r\n").encode()
        for event in events)


def config(server):
    return dict(CONFIG, url=server[0])


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("retry", [False, True])
def test_real_responses_transport_meters_each_generation_once(server, journal, stream, retry):
    from factory.agent_runtime.task_costs import CURRENT, BudgetTracker
    from memory import task_cost_store as costs
    cfg = config(server)
    if retry:
        server[1].append([http(b'{"error":"temporary unavailable"}', status=503)])
    raw = answer("resp_meter", "你好")
    server[1].append([http(sse(raw), content_type="text/event-stream") if stream else http(json.dumps(raw).encode())])
    costs.create_task("responses-meter", "meter-session")
    token = CURRENT.set(BudgetTracker("responses-meter", "meter-session"))
    request = turn([{"role": "user", "content": "check"}], session="meter-session")
    try:
        if stream:
            async def run():
                return [frame async for frame in ChatLLM.chat_completions(request=request, stream=True, model_config=cfg)]
            asyncio.run(run())
        else:
            ChatLLM.chat_completions(request=request, stream=False, model_config=cfg)
    finally:
        CURRENT.reset(token)
    result = costs.status("responses-meter")
    assert len(server[2]) == (2 if retry else 1)
    assert result["model_calls"] == len(server[2])
    if not retry:
        assert result["usage"]["total_tokens"] == 15
        assert result["estimated_calls"] == 0


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("chunked,compressed", [(False, False), (True, False), (True, True)])
def test_raw_http_json_create_query_and_extensions(server, asynchronous, chunked, compressed, monkeypatch):
    monkeypatch.setitem(sys.modules, "openai", None)
    monkeypatch.setitem(sys.modules, "httpx", None)
    cfg = dict(config(server), url=server[0] + "?tenant=a&tenant=b",
               _custom_headers=[{"name": "X-Test", "value": "retained"},
                                {"name": "Content-Length", "value": "wrong"}])
    raw = answer(text="你好")
    server[1].append([b"HTTP/1.1 100 Continue\r\n\r\n" + http(json.dumps(raw, ensure_ascii=False).encode(), chunked=chunked, compressed=compressed)])
    client, query = responses_client._client(cfg, ChatLLMRequest(messages=[]), asynchronous=asynchronous)
    async def run():
        try:
            return await client.responses.create(model="test", store=True, stream=False, input=[], extension={"future": True}, **({"extra_query": query} if query else {}))
        finally:
            await client.close()
    try:
        result = asyncio.run(run()) if asynchronous else client.responses.create(model="test", store=True, stream=False, input=[], extension={"future": True})
        assert result == raw
        start, headers, payload = server[2][0]
        assert start == "POST /gateway/v1/responses?tenant=a&tenant=b HTTP/1.1"
        assert headers["authorization"] == "Bearer test-key" and headers["x-test"] == "retained"
        assert payload["extension"] == {"future": True} and "extra_body" not in payload
        assert payload["store"] is True
    finally:
        if not asynchronous:
            client.close()


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_chunked_gzip_sse_unicode_and_single_commit(server, journal, mode):
    packet = http(sse(), chunked=True, compressed=True, content_type="text/event-stream")
    server[1].append([packet[i:i + 3] for i in range(0, len(packet), 3)])
    request = turn([{"role": "user", "content": "q"}])
    async def run():
        return [frame async for frame in responses_client.async_stream(ChatLLM, request, config(server))]
    frames = asyncio.run(run()) if mode == "async" else list(responses_client.sync_stream(ChatLLM, request, config(server)))
    values = [json.loads(frame[6:]) for frame in frames if frame.startswith("data: {")]
    assert not [value for value in values if value.get("error")]
    assert "".join(value.get("content", "") for value in values) == "你好"
    assert store.load_state("s")["head_response_id"] == "resp_1"
    assert len(server[2]) == 1


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_disconnect_after_id_queries_same_response_before_commit(server, journal, mode):
    server[1].append([http(sse(complete=False), content_type="text/event-stream")])
    server[1].append([http(json.dumps(answer(text="你好")).encode())])
    request = turn([{"role": "user", "content": "q"}])
    async def run():
        return [frame async for frame in responses_client.async_stream(ChatLLM, request, config(server))]
    frames = asyncio.run(run()) if mode == "async" else list(responses_client.sync_stream(ChatLLM, request, config(server)))
    values = [json.loads(frame[6:]) for frame in frames if frame.startswith("data: {")]
    assert "".join(value.get("content", "") for value in values) == "你好"
    assert [item[0].split()[0] for item in server[2]] == ["POST", "GET"]
    assert server[2][1][0] == "GET /gateway/v1/responses/resp_1 HTTP/1.1"
    assert store.load_state("s")["head_response_id"] == "resp_1"


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("status", [404, 405])
def test_disconnect_unavailable_retrieve_then_local_rebuild_and_increment(server, journal, mode, status):
    server[1].append([http(sse(complete=False), content_type="text/event-stream")])
    server[1].append([http(b'{"error":"retrieve unavailable"}', status=status)])

    def call(messages):
        request = turn(messages)
        async def run():
            return [frame async for frame in responses_client.async_stream(ChatLLM, request, config(server))]
        frames = asyncio.run(run()) if mode == "async" else list(responses_client.sync_stream(ChatLLM, request, config(server)))
        return [json.loads(frame[6:]) for frame in frames if frame.startswith("data: {")]

    values = call([{"role": "user", "content": "old question"}])
    assert len(server[2]) == 2  # An uncertain response is never recreated automatically.
    assert not any(value.get("tool_calls") for value in values)
    pending = store.pending_request("s", "chat_model")
    assert pending["status"] == "unknown" and pending["response_id"] == "resp_1"
    assert [failure["operation"] for failure in pending["failures"]] == ["stream", "retrieve"]
    assert pending["failures"][-1]["http_status"] == status
    assert any(f"HTTP {status}" in value.get("error", "") for value in values)

    history = [{"role": "user", "content": "old question"},
               {"role": "assistant", "content": "old visible answer"},
               {"role": "assistant", "tool_calls": [{"id": "call_done", "function": {"name": "read_file", "arguments": "{}"}}]},
               {"role": "tool", "tool_call_id": "call_done", "content": "already read"}]
    store.stage_rebuild("s", convert_messages(history), source="local")
    server[1].append([http(sse(answer("resp_rebuilt", "你好")), content_type="text/event-stream")])
    values = call([*history, {"role": "user", "content": "continue"}])
    assert any(value.get("response_complete") for value in values)
    root = server[2][2][2]
    assert "previous_response_id" not in root
    assert root["input"][1]["content"][0]["type"] == "output_text"
    assert [item["type"] for item in root["input"] if item.get("call_id") == "call_done"] == ["function_call", "function_call_output"]

    server[1].append([http(sse(answer("resp_next", "你好")), content_type="text/event-stream")])
    values = call([*history, {"role": "user", "content": "new question"}])
    assert not [value for value in values if value.get("error")]
    assert server[2][-1][2]["previous_response_id"] == "resp_rebuilt"
    assert server[2][-1][2]["input"] == [{"role": "user", "content": [{"type": "input_text", "text": "new question"}]}]
    assert store.load_state("s")["head_response_id"] == "resp_next"


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("failure", ["auth", "disconnect"])
def test_activation_failure_keeps_compacted_window_and_known_head(server, journal, mode, failure):
    cfg = config(server)
    control = store.begin_request("s", "chat_model", {"input": [{"type": "compaction_trigger"}]}, cfg, [])
    compacted = answer("resp_compacted", "")
    compacted["output"] = [{"type": "compaction", "encrypted_content": "opaque"}]
    reference = store.commit_response("s", control, compacted)
    if failure == "auth":
        server[1].append([http(b'{"error":"invalid key"}', status=401)])
    else:
        server[1].append([http(sse(complete=False), content_type="text/event-stream")])
        server[1].append([http(b'{"error":"retrieve unavailable"}', status=404)])
    request = turn([{"role": "user", "content": "continue"}])
    async def run():
        return [frame async for frame in responses_client.async_stream(ChatLLM, request, cfg)]
    frames = asyncio.run(run()) if mode == "async" else list(responses_client.sync_stream(ChatLLM, request, cfg))
    values = [json.loads(frame[6:]) for frame in frames if frame.startswith("data: {")]
    assert any(value.get("error") and not value.get("retrying") for value in values)
    assert not any(value.get("tool_calls") for value in values)
    first = server[2][0][2]
    assert "previous_response_id" not in first and first["input"][0]["type"] == "compaction"
    state = store.load_state("s")
    assert state["head_response_id"] == "resp_compacted"
    assert state["pending_compaction_ref"] == reference["record_id"]
    assert state["last_compaction"]["activation"] == "pending_rebase"
    if failure == "disconnect":
        assert store.pending_request("s", "chat_model")["status"] == "unknown"
    else:
        assert state["pending_request_ref"] is None


@pytest.mark.parametrize("status,attempts", [(400, 1), (401, 1), (403, 1), (429, 3), (503, 3)])
def test_http_failures_preserve_retry_policy_and_no_tools(server, journal, status, attempts):
    for _ in range(attempts):
        server[1].append([http(b'{"error":{"message":"test rejection"}}', status=status)])
    result = responses_client.nonstream(ChatLLM, turn([{"role": "user", "content": "q"}]), config(server))
    assert result["error_type"] == "http" and result["retrying"] is False
    assert len(server[2]) == attempts
    assert store.load_state("s")["head_response_id"] is None
    assert store.load_state("s")["pending_request_ref"] is None


@pytest.mark.parametrize("body,header", [(b"abc", b"Content-Length: 99"), (b"7\r\nabc", b"Transfer-Encoding: chunked"),
                                         (b"wat\r\n", b"Transfer-Encoding: chunked")])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_invalid_and_truncated_http_body_is_not_success(server, body, header, asynchronous):
    server[1].append([b"HTTP/1.1 200 OK\r\n" + header + b"\r\n\r\n" + body])
    client, _ = responses_client._client(config(server), ChatLLMRequest(messages=[]), asynchronous=asynchronous)
    async def run():
        try:
            return await client.create(stream=False)
        finally:
            await client.close()
    with pytest.raises((ConnectionError, ResponsesProtocolError)):
        if asynchronous:
            asyncio.run(run())
        else:
            try:
                client.create(stream=False)
            finally:
                client.close()
    assert not client.active


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_content_length_does_not_wait_for_socket_eof(server, mode):
    def reply_and_hold(handler):
        handler.connection.sendall(http(json.dumps(answer()).encode()))
        handler.connection.settimeout(1)
        assert handler.connection.recv(1) == b""
    server[1].append(reply_and_hold)
    request = ChatLLMRequest(messages=[])
    request.timeout_read = 0.2
    client, _ = responses_client._client(config(server), request, asynchronous=mode == "async")
    async def run():
        try:
            return await client.create(stream=False)
        finally:
            await client.close()
    try:
        raw = asyncio.run(run()) if mode == "async" else client.create(stream=False)
        assert raw["id"] == "resp_1"
    finally:
        if mode == "sync":
            client.close()


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_stop_during_idle_stream_closes_socket_and_keeps_recovery_id(server, journal, mode):
    ready, stopped = threading.Event(), threading.Event()
    def stream_and_wait(handler):
        body = sse(complete=False)
        handler.connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n" + body)
        ready.set()
        handler.connection.settimeout(2)
        assert handler.connection.recv(1) == b""
    server[1].append(stream_and_wait)
    request = turn([{"role": "user", "content": "q"}])
    def stop_later():
        ready.wait(1)
        time.sleep(0.15)
        stopped.set()
    stopper = threading.Thread(target=stop_later, daemon=True)
    stopper.start()
    async def run():
        return [frame async for frame in responses_client.async_stream(ChatLLM, request, config(server), stopped.is_set)]
    frames = asyncio.run(run()) if mode == "async" else list(responses_client.sync_stream(ChatLLM, request, config(server), stopped.is_set))
    stopper.join(1)
    assert not any('"tool_calls"' in frame for frame in frames)
    pending = store.pending_request("s", "chat_model")
    assert pending["response_id"] == "resp_1" and pending["status"] == "unknown"
    assert store.load_state("s")["head_response_id"] is None


def test_async_timeout_recovery_retrieves_before_resending(server, journal):
    def stream_then_stall(handler):
        handler.connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n" + sse(complete=False))
        handler.connection.settimeout(1)
        assert handler.connection.recv(1) == b""
    server[1].append(stream_then_stall)
    server[1].append([http(json.dumps(answer(text="你好")).encode())])
    request = turn([{"role": "user", "content": "q"}])
    request.timeout_read = 0.1
    async def run():
        return [frame async for frame in responses_client.async_stream(ChatLLM, request, config(server))]
    frames = asyncio.run(run())
    assert len(server[2]) == 2 and server[2][1][0].startswith("GET ")
    assert store.load_state("s")["head_response_id"] == "resp_1"
    assert any('"retrying": true' in frame for frame in frames)


def test_sse_cr_lf_multiline_data_bom_and_utf8_fragmentation():
    decoder = _SSEDecoder()
    packet = '\ufeff: comment\r\revent: response.output_text.delta\rdata: {"delta":\rdata: "你好"}\r\r'.encode()
    events = []
    for byte in packet:
        events.extend(decoder.feed(bytes([byte])))
    events.extend(decoder.feed(final=True))
    assert events == [{"type": "response.output_text.delta", "delta": "你好"}]


def test_sse_incomplete_tail_is_a_recoverable_disconnect():
    decoder = _SSEDecoder()
    decoder.feed(b'data: {"type":"response.output_text.delta","delta":"')
    with pytest.raises(ConnectionError):
        decoder.feed(final=True)


def test_request_auth_header_injection_is_rejected():
    with pytest.raises(ResponsesProtocolError, match="换行"):
        _wire("https://example.invalid/responses", {"Authorization": "Bearer key\r\nX-Evil: yes"}, "POST", {})


def test_retry_after_seconds_and_http_date():
    from email.utils import formatdate
    from chat.responses_transport import HTTPFailure
    assert responses_client._retry_delay(HTTPFailure(429, "limited", {"retry-after": "4"}), 1) == 4
    delay = responses_client._retry_delay(HTTPFailure(503, "unavailable", {"retry-after": formatdate(time.time() + 8, usegmt=True)}), 1)
    assert 6 <= delay <= 8
