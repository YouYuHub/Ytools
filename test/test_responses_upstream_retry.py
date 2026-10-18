"""Terminally failed Responses share the network retry budget (server_error etc)."""
import asyncio
import json

import pytest

from chat import responses_client as client
from chat.chat_llm import ChatLLM
from chat.protocols import responses_store as store
from chat.protocols.errors import ResponsesProtocolError, ResponsesUpstreamError
from chat.protocols.responses import normalize_response
from test_responses_api import CONFIG, AsyncStream, answer, commit, turn


STATELESS = dict(CONFIG, responsesMode="stateless")


@pytest.fixture
def journal(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_root", lambda: tmp_path)
    monkeypatch.setattr(store, "_mirror", lambda *args: None)
    monkeypatch.setattr(client, "_retry_delay", lambda *args: 0)
    monkeypatch.setattr(client, "_maximum", lambda: 3)


def failed(response_id, code="server_error", message="The model failed to generate a response."):
    return {"id": response_id, "object": "response", "status": "failed", "store": True,
            "created_at": 1791810000, "output": [],
            "error": {"code": code, "message": message}}


def incomplete(response_id, reason):
    raw = answer(response_id, "")
    raw["status"] = "incomplete"
    raw["incomplete_details"] = {"reason": reason}
    return raw


class ScriptAsyncClient:
    """Replays one raw response per create() as its terminal stream event."""

    def __init__(self, script):
        self.responses = self
        self.script = list(script)
        self.creates = []
        self.retrieves = []

    async def create(self, **kwargs):
        self.creates.append(kwargs)
        raw = self.script[min(len(self.creates) - 1, len(self.script) - 1)]
        kind = {"completed": "response.completed", "failed": "response.failed"}.get(
            raw["status"], "response.incomplete")
        return AsyncStream([{"type": "response.created", "response": {"id": raw["id"]}},
                            {"type": kind, "response": raw}])

    async def retrieve(self, response_id, **kwargs):
        self.retrieves.append(response_id)
        raise AssertionError("terminally failed responses must be recreated, not retrieved")

    async def close(self):
        pass


def run_async(request, config, fake, monkeypatch):
    monkeypatch.setattr(client, "_client", lambda *args, **kwargs: (fake, {}))

    async def collect():
        return [frame async for frame in client.async_stream(ChatLLM, request, config)]

    return asyncio.run(collect())


def decode(frames):
    return [json.loads(frame[6:]) for frame in frames if frame.startswith("data: {")]


def request_rows(session="s"):
    rows = [json.loads(path.read_text(encoding="utf-8"))
            for path in store.session_directory(session).glob("*.json")]
    return [row for row in rows if row.get("kind") == "request"]


def test_server_error_retries_with_fresh_create(journal, monkeypatch):
    fake = ScriptAsyncClient([failed("resp_bad"), answer("resp_good", "recovered")])
    frames = decode(run_async(turn([{"role": "user", "content": "q"}]), CONFIG, fake, monkeypatch))
    assert len(fake.creates) == 2
    assert fake.retrieves == []
    assert fake.creates[0] == fake.creates[1]
    errors = [frame for frame in frames if frame.get("error")]
    assert len(errors) == 1
    assert errors[0]["retrying"] is True
    assert errors[0]["error_type"] == "upstream_error_frame"
    assert errors[0]["retry"] == 1 and errors[0]["max_attempts"] == 3
    assert "server_error" in errors[0]["error"]
    assert "".join(frame.get("content", "") for frame in frames) == "recovered"
    assert frames[-1]["finish_reason"] == "stop"
    assert store.load_state("s")["head_response_id"] == "resp_good"
    assert store.pending_request("s", "chat_model") is None
    abandoned = [row for row in request_rows() if row.get("status") == "failed"]
    assert len(abandoned) == 1
    assert abandoned[0]["terminal_response"]["error"]["code"] == "server_error"
    assert abandoned[0]["failures"] and abandoned[0]["failures"][0]["operation"] == "stream"


def test_server_error_retry_preserves_chain_parent(journal, monkeypatch):
    commit(turn([{"role": "user", "content": "old"}]), answer("resp_old"))
    fake = ScriptAsyncClient([failed("resp_bad"), answer("resp_new", "recovered")])
    decode(run_async(turn([{"role": "user", "content": "new"}]), CONFIG, fake, monkeypatch))
    assert len(fake.creates) == 2
    assert fake.creates[0]["previous_response_id"] == "resp_old"
    assert fake.creates[1]["previous_response_id"] == "resp_old"
    assert store.load_state("s")["head_response_id"] == "resp_new"


def test_server_error_exhausts_budget_with_single_terminal(journal, monkeypatch):
    fake = ScriptAsyncClient([failed("resp_bad")])
    frames = decode(run_async(turn([{"role": "user", "content": "q"}]), CONFIG, fake, monkeypatch))
    assert len(fake.creates) == 3
    assert fake.retrieves == []
    errors = [frame for frame in frames if frame.get("error")]
    assert [frame["retry"] for frame in errors] == [1, 2, 3]
    assert [frame["retrying"] for frame in errors] == [True, True, False]
    assert errors[-1]["non_retryable"] is True
    assert store.load_state("s")["head_response_id"] is None
    assert store.pending_request("s", "chat_model") is None


def test_stateless_server_error_retries_without_query(journal, monkeypatch):
    bad, good = failed("resp_bad"), answer("resp_good", "recovered")
    bad["store"] = good["store"] = False
    fake = ScriptAsyncClient([bad, good])
    frames = decode(run_async(turn([{"role": "user", "content": "q"}]), STATELESS, fake, monkeypatch))
    assert len(fake.creates) == 2
    assert fake.retrieves == []
    assert any(frame.get("retrying") for frame in frames if frame.get("error"))
    assert "".join(frame.get("content", "") for frame in frames) == "recovered"


def test_bare_error_event_frame_retries(journal, monkeypatch):
    class ErrorFrameClient(ScriptAsyncClient):
        async def create(self, **kwargs):
            self.creates.append(kwargs)
            if len(self.creates) == 1:
                return AsyncStream([
                    {"type": "response.created", "response": {"id": "resp_err"}},
                    {"type": "error", "message": "upstream blew up"}])
            return AsyncStream([
                {"type": "response.created", "response": {"id": "resp_good"}},
                {"type": "response.completed", "response": answer("resp_good", "recovered")}])

    fake = ErrorFrameClient([])
    frames = decode(run_async(turn([{"role": "user", "content": "q"}]), CONFIG, fake, monkeypatch))
    assert len(fake.creates) == 2
    assert fake.retrieves == []
    assert "".join(frame.get("content", "") for frame in frames) == "recovered"


def test_sync_nonstream_server_error_retries(journal, monkeypatch):
    class SyncClient:
        def __init__(self):
            self.responses = self
            self.creates = []

        def create(self, **kwargs):
            self.creates.append(kwargs)
            if len(self.creates) == 1:
                return failed("resp_bad")
            return answer("resp_good", "recovered")

        def close(self):
            pass

    fake = SyncClient()
    monkeypatch.setattr(client, "_client", lambda *args, **kwargs: (fake, {}))
    result = client.nonstream(ChatLLM, turn([{"role": "user", "content": "q"}]), CONFIG)
    assert result["content"] == "recovered"
    assert len(fake.creates) == 2
    assert store.load_state("s")["head_response_id"] == "resp_good"


@pytest.mark.parametrize("raw", [
    failed("resp_x", code="invalid_request_error", message="bad input"),
    failed("resp_x", code="authentication_error", message="bad key"),
    failed("resp_x", code="permission_error", message="denied"),
    failed("resp_x", code="not_found_error", message="model gone"),
    incomplete("resp_x", "max_output_tokens"),
    incomplete("resp_x", "content_filter"),
    {**answer("resp_x", ""), "status": "cancelled"},
])
def test_deterministic_terminations_never_retry(journal, monkeypatch, raw):
    fake = ScriptAsyncClient([raw])
    frames = decode(run_async(turn([{"role": "user", "content": "q"}]), CONFIG, fake, monkeypatch))
    assert len(fake.creates) == 1
    assert fake.retrieves == []
    errors = [frame for frame in frames if frame.get("error")]
    assert len(errors) == 1
    assert errors[0]["retrying"] is False and errors[0]["non_retryable"] is True
    assert store.load_state("s")["head_response_id"] is None
    assert store.pending_request("s", "chat_model") is None


@pytest.mark.parametrize("code,expected", [
    ("server_error", True), ("rate_limit_exceeded", True), ("rate_limit_error", True),
    ("api_error", True), ("overloaded_error", True), ("", True),
    ("invalid_request_error", False), ("invalid_request", False),
    ("authentication_error", False), ("permission_error", False), ("not_found_error", False),
])
def test_upstream_retryable_by_failed_code(code, expected):
    response = {"status": "failed"}
    if code:
        response["error"] = {"code": code, "message": "detail"}
    assert client._retryable(ResponsesUpstreamError("failed", response)) is expected


@pytest.mark.parametrize("status,expected", [
    ("failed", True), ("completed", True), ("incomplete", False), ("cancelled", False),
])
def test_upstream_retryable_by_status(status, expected):
    response = {"status": status, "error": {"code": "server_error", "message": "Detail"}}
    assert client._retryable(ResponsesUpstreamError("terminated", response)) is expected


@pytest.mark.parametrize("message,expected", [
    ("upstream blew up", True), ("Error 503 overloaded", True),
    ("authentication_error: bad key", False), ("invalid request parameters", False),
    ("HTTP 401 unauthorized", False), ("model 404 not found", False),
])
def test_bare_error_frame_retryable_by_text(message, expected):
    assert client._retryable(ResponsesUpstreamError(message)) is expected


def test_protocol_and_transport_retry_rules_unchanged():
    assert client._retryable(ResponsesProtocolError("bad window")) is False
    http = RuntimeError("busy")
    http.status_code = 500
    assert client._retryable(http) is True
    denied = RuntimeError("denied")
    denied.status_code = 401
    assert client._retryable(denied) is False
    assert client._retryable(OSError("disconnect")) is True


def malformed_call(response_id, **overrides):
    raw = answer(response_id, "", calls=[("call_1", "write_file", "{}")])
    raw["output"][0].update(overrides)
    return raw


@pytest.mark.parametrize("overrides,expected", [
    ({"call_id": ""}, "缺少 call_id"),
    ({"call_id": None}, "缺少 call_id"),
    ({"name": ""}, "缺少名称"),
    ({"status": "in_progress"}, "完成状态无效"),
    ({"status": "incomplete"}, "完成状态无效"),
])
def test_malformed_call_reports_precise_reason(overrides, expected):
    raw = malformed_call("resp_bad", **overrides)
    with pytest.raises(ResponsesUpstreamError) as caught:
        normalize_response(raw)
    assert expected in str(caught.value)
    assert "第 1 个 function_call" in str(caught.value)
    assert caught.value.response is raw


def test_duplicate_call_id_reports_offending_id():
    raw = answer("resp_bad", "", calls=[("call_1", "write_file", "{}"), ("call_1", "read_file", "{}")])
    with pytest.raises(ResponsesUpstreamError) as caught:
        normalize_response(raw)
    assert "call_id 重复" in str(caught.value) and "call_1" in str(caught.value)


@pytest.mark.parametrize("bad", [
    malformed_call("resp_bad", status="in_progress"),
    answer("resp_bad", "", calls=[("call_1", "write_file", "{}"), ("call_1", "read_file", "{}")]),
    malformed_call("resp_bad", name=""),
], ids=["bad-status", "duplicate-id", "missing-name"])
def test_malformed_completed_call_retries_with_fresh_create(journal, monkeypatch, bad):
    fake = ScriptAsyncClient([bad, answer("resp_good", "recovered")])
    frames = decode(run_async(turn([{"role": "user", "content": "q"}]), CONFIG, fake, monkeypatch))
    assert len(fake.creates) == 2
    assert fake.retrieves == []
    errors = [frame for frame in frames if frame.get("error")]
    assert len(errors) == 1 and errors[0]["retrying"] is True
    assert errors[0]["error_type"] == "upstream_error_frame"
    assert "".join(frame.get("content", "") for frame in frames) == "recovered"
    assert store.load_state("s")["head_response_id"] == "resp_good"
    abandoned = [row for row in request_rows() if row.get("status") == "failed"]
    assert len(abandoned) == 1
    assert abandoned[0]["terminal_response"]["id"] == "resp_bad"


def test_malformed_call_exhausts_budget_with_precise_terminal(journal, monkeypatch):
    fake = ScriptAsyncClient([malformed_call("resp_bad", status="in_progress")])
    frames = decode(run_async(turn([{"role": "user", "content": "q"}]), CONFIG, fake, monkeypatch))
    assert len(fake.creates) == 3
    errors = [frame for frame in frames if frame.get("error")]
    assert [frame["retrying"] for frame in errors] == [True, True, False]
    assert "完成状态无效" in errors[-1]["error"] and "in_progress" in errors[-1]["error"]
    assert store.load_state("s")["head_response_id"] is None
