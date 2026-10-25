"""Messages protocol, replay integrity, transport failure and role regression tests."""
import asyncio
import copy
import io
import json
from unittest.mock import AsyncMock

import pytest

import env_manager
from chat.chat_llm import ChatLLM, _estimate_payload_input_tokens
from chat import messages_client
from chat.protocols import native_store
from chat.protocols.messages import (
    MessagesProtocolError, MessagesStream, SSEFrames, build_payload, endpoint,
    headers, normalize_response,
)
from config import ChatLLMRequest
from factory.agent_runtime.chat_runtime import copy_for_request, build_request_messages


CONFIG = {"url": "http://gateway.test/v1", "apiType": "messages",
          "apiKey": "secret", "selected_model_id": "claude-test",
          "selected_model_name": "Claude", "selected_provider_name": "P"}
TOOLS = [{"type": "function", "function": {"name": "work", "description": "工作",
          "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}]
SIGNED = [{"type": "thinking", "thinking": "思考", "signature": "opaque-original-signature"},
          {"type": "redacted_thinking", "data": "opaque-encrypted"},
          {"type": "text", "text": "开始"},
          {"type": "tool_use", "id": "tool_a", "name": "work", "input": {"path": "a"}},
          {"type": "tool_use", "id": "tool_b", "name": "work", "input": {"path": "b"}}]


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    from collections import OrderedDict
    from util import media_limits
    monkeypatch.setattr(media_limits, '_cache', OrderedDict())
    # Exercise the real managed layout. Overriding only _root() no longer
    # redirects account-aware protocol_path() and can escape this test's tree.
    from memory import chat_memory
    monkeypatch.setattr(chat_memory, "HISTORY_ROOT", tmp_path / "history")
    monkeypatch.setattr(chat_memory, "LOCK_ROOT", tmp_path / "locks")
    monkeypatch.setattr(chat_memory, "_session_managers", {})
    monkeypatch.setattr(messages_client, "_retry_limit", lambda: 1)
    monkeypatch.setattr(messages_client, "_retry_delay", lambda exc: 0)
    # Never allow a test with an incomplete mock to call an actual provider.
    monkeypatch.setattr(messages_client.socket, "create_connection", lambda *a, **k: (_ for _ in ()).throw(AssertionError("unmocked network")))
    monkeypatch.setattr(ChatLLM, "_precheck_hard_limit", staticmethod(lambda payload, **kwargs: None))


def request(**overrides):
    data = {"messages": [{"role": "system", "content": "规则"}, {"role": "user", "content": "你好"}],
            "session_id": "messages_test", "max_tokens": 4096}
    data.update(overrides)
    return ChatLLMRequest(**data)


def response(content=None, reason="end_turn"):
    return {"id": "msg_test", "type": "message", "role": "assistant",
            "content": content if content is not None else [{"type": "text", "text": "你好"}],
            "stop_reason": reason, "usage": {"input_tokens": 7, "output_tokens": 3}}


def frames(content=None, reason="end_turn", complete=True):
    blocks = content if content is not None else [{"type": "text", "text": "你好"}]
    result = [{"type": "message_start", "message": {"id": "msg_test", "content": [], "usage": {
        "input_tokens": 7, "output_tokens": 1, "cache_read_input_tokens": 10}}}]
    for index, block in enumerate(blocks):
        kind = block["type"]
        initial = copy.deepcopy(block)
        if kind == "tool_use":
            initial["input"] = {}
        elif kind == "text":
            initial["text"] = ""
        elif kind == "thinking":
            initial["thinking"] = ""
            initial.pop("signature", None)
        result.append({"type": "content_block_start", "index": index, "content_block": initial})
        if kind == "text":
            deltas = [{"type": "text_delta", "text": block["text"]}]
        elif kind == "thinking":
            deltas = [{"type": "thinking_delta", "thinking": block["thinking"]}]
            if "signature" in block:
                deltas.append({"type": "signature_delta", "signature": block["signature"]})
        elif kind == "tool_use":
            args = json.dumps(block["input"], ensure_ascii=False)
            deltas = [{"type": "input_json_delta", "partial_json": args[:2]},
                      {"type": "input_json_delta", "partial_json": args[2:]}]
        else:
            deltas = []
        result.extend({"type": "content_block_delta", "index": index, "delta": delta} for delta in deltas)
        result.append({"type": "content_block_stop", "index": index})
    if complete:
        result.extend([{"type": "message_delta", "delta": {"stop_reason": reason}, "usage": {"output_tokens": 3}},
                       {"type": "message_stop"}])
    return result


def sse(events):
    return "".join("event: " + event["type"] + "\r\ndata: " + json.dumps(event, ensure_ascii=False) + "\r\n\r\n" for event in events).encode("utf-8")


def http(body=b"", status=200, chunked=False):
    if chunked:
        # Chunk boundaries deliberately split UTF-8, SSE field names, and JSON.
        chunks = [body[i:i + 7] for i in range(0, len(body), 7)]
        body = b"".join(f"{len(chunk):x};ignored=1\r\n".encode() + chunk + b"\r\n" for chunk in chunks) + b"0\r\n\r\n"
    return (f"HTTP/1.1 {status} test\r\n".encode() + (b"Transfer-Encoding: chunked\r\n" if chunked else b"") + b"\r\n" + body)


class Socket:
    def __init__(self, data):
        self.file = io.BytesIO(data)
        self.sent = b""
        self.closed = False
    def settimeout(self, timeout):
        pass
    def sendall(self, data):
        self.sent += data
    def makefile(self, mode):
        return self.file
    def close(self):
        self.closed = True
    def payload(self):
        return json.loads(self.sent.split(b"\r\n\r\n", 1)[1])


def sockets(monkeypatch, responses):
    values = [Socket(data) for data in responses]
    iterator = iter(values)
    monkeypatch.setattr(messages_client.socket, "create_connection", lambda *a, **k: next(iterator))
    return values


def decode(chunks):
    return [json.loads(chunk[6:].strip()) for chunk in chunks if chunk.startswith("data: {")]


def test_payload_protocol_fields_and_defaults():
    payload, _, _ = build_payload(request(tools=TOOLS, extra_body={"model": "bad", "presence_penalty": 3,
                        "enable_thinking": True, "custom": "ok"}), CONFIG, True)
    assert payload["system"] == [{"type": "text", "text": "规则"}]
    assert payload["messages"][0]["role"] == "user"
    assert payload["model"] == "claude-test"
    assert payload["tools"][0]["input_schema"] == TOOLS[0]["function"]["parameters"]
    assert payload["custom"] == "ok"
    assert not {"temperature", "top_p", "reasoning_effort", "presence_penalty", "enable_thinking", "stream_options"} & payload.keys()


def test_explicit_thinking_sampling_and_output_cap():
    req = request(thinking={"type": "enabled", "budget_tokens": 1024}, output_config={"effort": "high"}, stop_sequences=["STOP"], temperature=1)
    payload, _, _ = build_payload(req, dict(CONFIG, maxOutputTokens=2048), False)
    assert payload["temperature"] == 1
    assert payload["thinking"]["budget_tokens"] == 1024
    assert payload["max_tokens"] == 2048
    with pytest.raises(MessagesProtocolError):
        build_payload(req, dict(CONFIG, maxOutputTokens=1024), False)


def test_model_capabilities_filter_deprecated_messages_parameters():
    req = request(temperature=0.7, top_p=0.8, thinking={"type": "enabled", "budget_tokens": 1024},
                  extra_body={"temperature": 0.9, "custom": True})
    payload, _, _ = build_payload(req, dict(CONFIG, parameterCapabilities={
        "temperature": False, "top_p": False, "thinking_budget": False}), False)
    assert "temperature" not in payload
    assert "top_p" not in payload
    assert "thinking" not in payload
    assert payload["custom"] is True


def test_model_list_exposes_parameter_capabilities_without_api_keys(monkeypatch):
    monkeypatch.setattr(env_manager, "_iter_effective_provider_records", lambda: [{
        "name": "P", "apiKey": "must-not-leak", "models": {"M": {"id": "m",
            "apiType": "messages", "parameterCapabilities": {"temperature": False}}}}])
    model = env_manager.list_available_models()[0]
    assert model["parameter_capabilities"] == {"temperature": False}
    assert "apiKey" not in model and "must-not-leak" not in repr(model)


def test_tool_results_grouped_in_declaration_order():
    req = request(messages=[{"role": "user", "content": "工作"}, {"role": "assistant", "tool_calls": [
        {"id": "a", "function": {"name": "work", "arguments": "{}"}},
        {"id": "b", "function": {"name": "work", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "b", "content": "失败", "tool_is_error": True},
        {"role": "tool", "tool_call_id": "a", "content": "成功"}])
    payload, _, _ = build_payload(req, CONFIG, False)
    results = payload["messages"][-1]["content"]
    assert [item["tool_use_id"] for item in results] == ["a", "b"]
    assert results[1]["is_error"] is True


@pytest.mark.parametrize("url, expected", [("https://api.anthropic.com/", "https://api.anthropic.com/v1/messages"),
    ("http://host/api/v1/?a=1", "http://host/api/v1/messages?a=1"),
    ("http://host/v1/messages", "http://host/v1/messages")])
def test_endpoints(url, expected):
    assert endpoint(url) == expected


def test_headers_single_auth_and_transport_ownership():
    default_headers = {key.lower(): value for key, value in headers(CONFIG).items()}
    assert default_headers["x-api-key"] == "secret"
    assert "authorization" not in default_headers

    config = dict(CONFIG, _custom_headers=[{"name": "X-Api-Key", "value": "override"},
        {"name": "ANTHROPIC-VERSION", "value": "custom"}, {"name": "Host", "value": "bad"}])
    values = {key.lower(): value for key, value in headers(config).items()}
    assert values["x-api-key"] == "override" and "authorization" not in values
    assert values["anthropic-version"] == "custom" and "host" not in values
    config["_custom_headers"].append({"name": "Authorization", "value": "token"})
    with pytest.raises(MessagesProtocolError):
        headers(config)


def test_bearer_override_replaces_default_messages_api_key():
    config = dict(CONFIG, _custom_headers=[{"name": "Authorization", "value": "Bearer custom"}])
    values = {key.lower(): value for key, value in headers(config).items()}
    assert values["authorization"] == "Bearer custom"
    assert "x-api-key" not in values


def test_opencode_go_wire_adds_stable_session_and_user_agent():
    config = dict(CONFIG, url="https://opencode.ai/zen/go/v1")
    payload = {"model": "qwen3.7-max", "messages": []}
    _, _, wire = messages_client._wire(config, payload, "conversation-1")
    wire = wire.decode("utf-8").lower()
    assert "x-api-key: secret\r\n" in wire
    assert "user-agent: agent-tool-sse/1.0\r\n" in wire
    assert "x-opencode-session: " in wire
    assert "x-opencode-session: conversation-1\r\n" not in wire


def test_stream_commits_tools_only_after_message_stop():
    state = MessagesStream()
    events = frames(SIGNED, "tool_use")
    for event in events[:-1]:
        emitted = state.feed(event["type"], event)
        assert not any(item.get("tool_calls") for item in emitted)
    final = state.feed(events[-1]["type"], events[-1])[0]
    assert final["response_complete"]
    assert [item["id"] for item in final["tool_calls"]] == ["tool_a", "tool_b"]
    assert state.response["content"] == SIGNED
    assert final["usage"]["prompt_tokens"] == 17
    assert final["usage"]["completion_tokens"] == 3
    assert "signature" not in json.dumps(final)


def test_unknown_events_ping_and_multiline_frames():
    state = MessagesStream()
    assert state.feed("future_event", {"type": "future_event"}) == []
    assert state.feed("ping", {"type": "ping"}) == []
    parser = SSEFrames()
    assert parser.feed(b": comment\n") is None
    assert parser.feed(b"event: ping\n") is None
    assert parser.feed(b'data: {"type":\n') is None
    assert parser.feed(b'data: "ping"}\n') is None
    assert parser.feed(b"\n") == ("ping", {"type": "ping"})


@pytest.mark.parametrize("stream", [False, True])
def test_unsigned_gateway_thinking_finishes_without_native_replay(monkeypatch, stream):
    content = [{"type": "thinking", "thinking": "兼容平台的思考"}, {"type": "text", "text": "答案"}]
    raw = sse(frames(content)) if stream else json.dumps(response(content), ensure_ascii=False).encode()
    sockets(monkeypatch, [http(raw)])
    result = decode(list(ChatLLM.std_completions_sse(request(), model_config=CONFIG)))[-1] if stream else ChatLLM.chat_completions(request(), stream=False, model_config=CONFIG)
    assert result["response_complete"] and result["finish_reason"] == "stop"
    assert result["replay_eligible"] is False
    assert "provider_state" not in result and "error" not in result
    assert not list(native_store._root().glob("**/*.json"))
    if not stream:
        assert result["content"] == "答案" and result["reasoning_content"] == "兼容平台的思考"


def test_unsigned_thinking_tools_commit_and_continue_without_thinking_blocks(monkeypatch):
    content = [{"type": "thinking", "thinking": "无签名思考"}, SIGNED[-1]]
    sockets(monkeypatch, [http(sse(frames(content, "tool_use")))])
    result = decode(list(ChatLLM.std_completions_sse(request(tools=TOOLS), model_config=CONFIG)))[-1]
    assert result["finish_reason"] == "tool_calls" and len(result["tool_calls"]) == 1
    assert not result.get("provider_state") and not result["replay_eligible"]
    payload, parent, _ = build_payload(request(messages=[
        {"role": "user", "content": "工作"},
        {"role": "assistant", "tool_calls": result["tool_calls"], "reasoning_content": "无签名思考"},
        {"role": "tool", "tool_call_id": "tool_b", "content": "结果"},
    ], tools=TOOLS), CONFIG, False)
    assert parent is None
    assert payload["messages"][-1]["content"][0]["type"] == "tool_result"
    assert all(block["type"] != "thinking" for message in payload["messages"] for block in message["content"])


@pytest.mark.parametrize("reason", ["max_tokens", "model_context_window_exceeded"])
def test_length_drops_incomplete_tools(reason):
    state = MessagesStream()
    events = frames([SIGNED[-1]], reason)
    for event in events:
        if event.get("delta", {}).get("type") == "input_json_delta":
            event["delta"]["partial_json"] = '{"broken":'
        emitted = state.feed(event["type"], event)
    assert emitted[0]["finish_reason"] == "length"
    assert emitted[0]["tool_calls"] == []


def test_invalid_complete_tool_json_never_executes():
    state = MessagesStream()
    events = frames([SIGNED[-1]], "tool_use")
    with pytest.raises(MessagesProtocolError):
        for event in events:
            if event.get("delta", {}).get("type") == "input_json_delta":
                event["delta"]["partial_json"] = "invalid"
            state.feed(event["type"], event)


def test_sync_stream_chunked_and_private_native_storage(monkeypatch):
    sock = sockets(monkeypatch, [http(sse(frames(SIGNED, "tool_use")), chunked=True)])[0]
    emitted = decode(list(ChatLLM.std_completions_sse(request(tools=TOOLS), model_config=CONFIG)))
    final = next(event for event in emitted if event.get("finish_reason"))
    record = native_store.load_record("messages_test", final["provider_state"])
    assert record["content"] == SIGNED and sock.closed
    assert "opaque-original-signature" not in json.dumps(emitted)
    assert sock.payload()["stream"] is True


@pytest.mark.parametrize("status, retry", [(401, False), (400, False), (429, True), (500, True), (529, True)])
def test_http_classification_and_retry_count(monkeypatch, status, retry):
    values = sockets(monkeypatch, [http(b"failure", status), http(sse(frames()))])
    emitted = decode(list(ChatLLM.std_completions_sse(request(), model_config=CONFIG)))
    failure = emitted[0]
    assert failure["retrying"] is retry
    assert failure["retry"] == 1 and failure["discard_tool_calls"] is True
    assert bool(values[1].sent) is retry
    if retry:
        assert emitted[-1]["finish_reason"] == "stop"


def test_empty_response_retries_and_limit_is_bounded(monkeypatch):
    values = sockets(monkeypatch, [http(), http()])
    emitted = decode(list(ChatLLM.std_completions_sse(request(), model_config=CONFIG)))
    assert [event["retrying"] for event in emitted] == [True, False]
    assert emitted[-1]["error_type"] == "empty_response"
    assert all(sock.closed for sock in values)


def test_connection_failure_retries(monkeypatch):
    sock = Socket(http(sse(frames())))
    calls = []
    def connect(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise OSError("connection reset")
        return sock
    monkeypatch.setattr(messages_client.socket, "create_connection", connect)
    emitted = decode(list(ChatLLM.std_completions_sse(request(), model_config=CONFIG)))
    assert len(calls) == 2 and emitted[0]["retrying"]
    assert emitted[-1]["finish_reason"] == "stop"


def test_midstream_error_resumes_text_without_partial_signature(monkeypatch):
    first = frames(complete=False)
    first.append({"type": "error", "error": {"type": "overloaded_error", "message": "overload"}})
    values = sockets(monkeypatch, [http(sse(first)), http(sse(frames()))])
    emitted = decode(list(ChatLLM.std_completions_sse(request(), model_config=CONFIG)))
    failure = next(event for event in emitted if event.get("error"))
    assert failure["resume_from_partial"] and failure["retrying"]
    payload = values[1].payload()
    assert payload["messages"][-2]["content"] == [{"type": "text", "text": "你好"}]
    assert "未完成的工具调用没有执行" in payload["messages"][-1]["content"][0]["text"]


def test_eof_discards_even_closed_tool_blocks(monkeypatch):
    sockets(monkeypatch, [http(sse(frames([SIGNED[-1]], "tool_use", complete=False)))])
    emitted = decode(list(ChatLLM.std_completions_sse(request(tools=TOOLS), model_config=CONFIG)))
    assert emitted[-1]["stream_truncated"] and emitted[-1]["discard_tool_calls"]
    assert not any(event.get("tool_calls") or event.get("provider_state") for event in emitted)


def test_nonstream_chunked_dispatch(monkeypatch):
    sock = sockets(monkeypatch, [http(json.dumps(response(), ensure_ascii=False).encode(), chunked=True)])[0]
    result = ChatLLM.chat_completions(request(), stream=False, model_config=CONFIG)
    assert result["content"] == "你好" and result["finish_reason"] == "stop"
    assert sock.payload()["stream"] is False


def test_native_tool_replay_freezes_prefix_and_recovers_completed_results():
    req = request(tools=TOOLS, thinking={"type": "enabled", "budget_tokens": 1024})
    payload, _, _ = build_payload(req, CONFIG, True)
    ref = native_store.save_record(req.session_id, CONFIG, payload, response(SIGNED, "tool_use"))
    native_store.tool_started(req.session_id, ref, "tool_a")
    native_store.tool_completed(req.session_id, ref, "tool_a", "saved")
    source = [{"role": "system", "content": "changed"}, {"role": "user", "content": "changed"},
              {"role": "assistant", "tool_calls": normalize_response(response(SIGNED, "tool_use"))["tool_calls"], "provider_state": ref},
              {"role": "tool", "tool_call_id": "tool_b", "content": "failed", "tool_is_error": True}]
    req2 = request(messages=build_request_messages(copy_for_request(source, api_type="messages")), tools=[])
    next_payload, parent, appended = build_payload(req2, CONFIG, True)
    assert parent["record_id"] == ref["record_id"]
    assert next_payload["messages"][:-2] == payload["messages"]
    assert next_payload["messages"][-2]["content"] == SIGNED
    assert next_payload["system"] == payload["system"]
    assert next_payload["tools"] == payload["tools"]
    assert next_payload["thinking"] == payload["thinking"]
    results = appended[0]["content"]
    assert results[0]["content"] == "saved" and results[1]["is_error"]
    child = native_store.save_record(req.session_id, CONFIG, next_payload, response(SIGNED, "tool_use"), parent=parent, appended=appended)
    replay = native_store.replay_messages(req.session_id, native_store.load_record(req.session_id, child))
    assert replay == next_payload["messages"] + [{"role": "assistant", "content": SIGNED}]


def test_uncertain_native_tool_execution_is_not_repeated_and_reset_is_safe():
    req = request(tools=TOOLS)
    payload, _, _ = build_payload(req, CONFIG, True)
    ref = native_store.save_record(req.session_id, CONFIG, payload, response(SIGNED, "tool_use"))
    native_store.tool_started(req.session_id, ref, "tool_a")
    source = [{"role": "user", "content": "工作"}, {"role": "assistant", "content": "开始", "provider_state": ref,
              "tool_calls": normalize_response(response(SIGNED, "tool_use"))["tool_calls"]}]
    with pytest.raises(MessagesProtocolError, match="状态未知"):
        build_payload(request(messages=source), CONFIG, True)
    native_store.reset_replay(source)
    rebuilt, _, _ = build_payload(request(messages=source), CONFIG, True)
    assert "signature" not in json.dumps(rebuilt)
    assert "事实记录" in json.dumps(rebuilt, ensure_ascii=False)


def test_estimator_counts_system_not_opaque_signature():
    base = {"messages": [{"role": "assistant", "content": SIGNED}]}
    enlarged = copy.deepcopy(base)
    enlarged["messages"][0]["content"][0]["signature"] *= 10000
    assert _estimate_payload_input_tokens(base) == _estimate_payload_input_tokens(enlarged)
    assert _estimate_payload_input_tokens(dict(base, system="中文" * 100)) > _estimate_payload_input_tokens(base)


def test_all_roles_accept_messages_and_derive_current_protocol(monkeypatch):
    monkeypatch.setattr(env_manager, "get_model_config", lambda *args: CONFIG)
    selection = {"ownership_name": "P", "model_name": "Claude", "api_type": "chat_completions",
                 "parameter": {"chat_completions": {"temperature": .2, "reasoning_effort": "low", "max_tokens": 2048}}}
    monkeypatch.setattr(env_manager, "get_role_selection", lambda role: selection)
    for role in env_manager.MODEL_SELECTION_ROLES:
        assert env_manager.resolve_selection_target("P", "Claude", role)[1] == "messages"
        assert env_manager.get_role_parameter(role) == {"max_tokens": 2048}


class Writer:
    def __init__(self):
        self.sent = b""
        self.closed = False
    def write(self, data):
        self.sent += data
    async def drain(self):
        pass
    def close(self):
        self.closed = True
    async def wait_closed(self):
        pass


def test_async_stream_dispatch_and_cancellation(monkeypatch):
    async def run():
        reader = asyncio.StreamReader()
        reader.feed_data(http(sse(frames())))
        reader.feed_eof()
        writer = Writer()
        monkeypatch.setattr(ChatLLM, "_open_connection", AsyncMock(return_value=(reader, writer)))
        emitted = decode([chunk async for chunk in ChatLLM.chat_completions(request(), stream=True, model_config=CONFIG)])
        assert emitted[-1]["finish_reason"] == "stop" and writer.closed
        stop = [False]
        reader2 = asyncio.StreamReader()  # no incoming status: exercise cancellable read
        writer2 = Writer()
        monkeypatch.setattr(ChatLLM, "_open_connection", AsyncMock(return_value=(reader2, writer2)))
        asyncio.get_running_loop().call_later(.01, lambda: stop.__setitem__(0, True))
        result = [chunk async for chunk in ChatLLM.async_std_completions_sse(request(), stop_checker=lambda: stop[0], model_config=CONFIG)]
        assert result == ["data: [DONE]\n\n"] and writer2.closed
    asyncio.run(run())


def test_async_retry_sleep_can_be_stopped(monkeypatch):
    async def run():
        stop = [False]
        async def connect(*args):
            stop[0] = True
            raise OSError("offline")
        monkeypatch.setattr(ChatLLM, "_open_connection", connect)
        monkeypatch.setattr(messages_client, "_retry_delay", lambda exc: 60)
        chunks = [chunk async for chunk in ChatLLM.async_std_completions_sse(request(), stop_checker=lambda: stop[0], model_config=CONFIG)]
        assert chunks[-1] == "data: [DONE]\n\n"
        assert decode(chunks)[0]["retrying"]
    asyncio.run(run())


def test_async_read_timeout_is_bounded(monkeypatch):
    async def run():
        writers = []
        async def connect(*args):
            writers.append(Writer())
            return asyncio.StreamReader(), writers[-1]
        monkeypatch.setattr(ChatLLM, "_open_connection", connect)
        emitted = decode([chunk async for chunk in ChatLLM.async_std_completions_sse(request().model_copy(update={"timeout_read": .01}), model_config=CONFIG)])
        assert [event["retrying"] for event in emitted] == [True, False]
        assert "超时" in emitted[-1]["error"]
        assert all(writer.closed for writer in writers)
    asyncio.run(run())


def test_history_completion_prevents_replaying_old_tool_turn():
    from memory.chat_history_format import round_entry_to_context_messages
    req = request(tools=TOOLS)
    payload, _, _ = build_payload(req, CONFIG, True)
    pending = native_store.save_record(req.session_id, CONFIG, payload, response(SIGNED, "tool_use"))
    complete = native_store.save_record(req.session_id, CONFIG, payload, response())
    calls = normalize_response(response(SIGNED, "tool_use"))["tool_calls"]
    entry = {"type": "chat_round", "round": 1, "question": "工作", "events": [
        {"role": "assistant", "tool_calls": calls, "provider_state": pending},
        {"role": "tool", "tool_call_id": "tool_a", "result": "a"},
        {"role": "tool", "tool_call_id": "tool_b", "result": "b"},
        {"role": "assistant", "content": "完成", "provider_state": complete}]}
    projection = round_entry_to_context_messages(entry, 1000)
    assert projection[-1]["provider_state"] == complete
    next_payload, parent, _ = build_payload(request(messages=projection + [{"role": "user", "content": "新任务"}]), CONFIG, True)
    assert parent is None
    assert "signature" not in json.dumps(next_payload)


def test_disk_failure_does_not_retry_or_commit_tool_calls(monkeypatch):
    sockets(monkeypatch, [http(sse(frames(SIGNED, "tool_use")))])
    def fail(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(native_store, "save_record", fail)
    emitted = decode(list(ChatLLM.std_completions_sse(request(tools=TOOLS), model_config=CONFIG)))
    assert emitted[-1]["non_retryable"] and "未提交" in emitted[-1]["error"]
    assert not any(event.get("tool_calls") for event in emitted)


@pytest.mark.parametrize("known", [False, True])
def test_main_loop_native_tool_batch_and_frontend_privacy(monkeypatch, known):
    import test_todo_no_tools_regression as harness
    monkeypatch.setattr(harness._MemoryStub, "update_current_round_usage", AsyncMock(), raising=False)
    async def run():
        native = copy.deepcopy(SIGNED)
        if known:
            for index, block in enumerate(native[-2:]):
                block["name"] = "todo_write"
                block["input"] = {"todos": [{"id": "1", "content": "步骤" + str(index), "status": "done"}]}
        writers = []
        events = iter([frames(native, "tool_use"), frames()])
        async def connect(*args):
            reader = asyncio.StreamReader()
            reader.feed_data(http(sse(next(events)), chunked=True))
            reader.feed_eof()
            writers.append(Writer())
            return reader, writers[-1]
        monkeypatch.setattr(ChatLLM, "_open_connection", connect)
        stream = await harness._run_case(["todo_write"] if known else [], llm=ChatLLM, resolve_config=lambda: dict(CONFIG, maxInputTokens=128000))
        assert len(writers) == 2
        payloads = [json.loads(writer.sent.split(b"\r\n\r\n", 1)[1]) for writer in writers]
        assert payloads[1]["messages"][:len(payloads[0]["messages"])] == payloads[0]["messages"]
        assert payloads[1]["messages"][-2]["content"] == native
        assert [block["tool_use_id"] for block in payloads[1]["messages"][-1]["content"]] == ["tool_a", "tool_b"]
        assert all(block["is_error"] is (not known) for block in payloads[1]["messages"][-1]["content"])
        if known:
            assert harness._last_memory.todos[-1]["content"] == "步骤1"
        visible = "".join(stream.chunks)
        assert "opaque-original-signature" not in visible and "provider_state" not in visible
        assert "tool_call_preview" not in visible and "upstream_activity" not in visible
        assert any(isinstance(record, dict) and record.get("provider_state") for record in harness._last_memory.records)
    asyncio.run(run())


def test_sub_agent_native_tool_batch_and_journal(monkeypatch):
    from test_sub_agent import _make_context
    from factory.agent_runtime.sub_agent import SubAgentRunner
    async def run():
        context, emitted, _ = _make_context()
        runner = SubAgentRunner(context)
        monkeypatch.setattr(runner, "_resolve_request_model_config", lambda: (CONFIG, {}))
        writers = []
        scripts = iter([frames(SIGNED, "tool_use"), frames()])
        async def connect(*args):
            reader = asyncio.StreamReader()
            reader.feed_data(http(sse(next(scripts))))
            reader.feed_eof()
            writers.append(Writer())
            return reader, writers[-1]
        monkeypatch.setattr(ChatLLM, "_open_connection", connect)
        result = await runner.run()
        assert result.status == "done"
        assert len(writers) == 2
        payload = json.loads(writers[1].sent.split(b"\r\n\r\n", 1)[1])
        assert payload["messages"][-2]["content"] == SIGNED
        assert all(block["is_error"] for block in payload["messages"][-1]["content"])
        ref = next(message["provider_state"] for message in runner.messages if message.get("tool_calls"))
        record = native_store.load_record(context.session_id, ref)
        assert all(item["status"] == "completed" for item in record["tool_status"].values())
        assert "opaque-original-signature" not in json.dumps(emitted)
    asyncio.run(run())


def test_title_request_omits_injected_chat_defaults(monkeypatch):
    from factory.agent_runtime import title_generator as title
    monkeypatch.setattr(title, "resolve_title_model_config", lambda: (CONFIG, ""))
    monkeypatch.setattr(title, "get_role_parameter", lambda role: {})
    payload, _, _ = build_payload(title._build_title_request("标题", "s"), CONFIG, False)
    assert payload["max_tokens"] == 64
    assert "temperature" not in payload and "top_p" not in payload


def test_unknown_execution_reference_survives_history_pair_cleanup():
    from factory.agent_runtime.chat_runtime import sanitize_tool_call_pairing
    ref = {"protocol": "messages", "record_id": "a" * 32}
    messages, _ = sanitize_tool_call_pairing([{"role": "assistant", "provider_state": ref,
        "tool_calls": [{"id": "a", "function": {"name": "work", "arguments": "{}"}}]}])
    assert messages == [{"role": "assistant", "provider_state": ref}]


def test_external_tool_watchdog_timeout_is_not_swallowed(monkeypatch):
    async def run():
        reader, writer = asyncio.StreamReader(), Writer()
        # Tool block has started, but no JSON or completion will arrive.
        reader.feed_data(http(sse(frames([SIGNED[-1]], "tool_use")[:2])))
        monkeypatch.setattr(ChatLLM, "_open_connection", AsyncMock(return_value=(reader, writer)))
        stream = ChatLLM.async_std_completions_sse(request(tools=TOOLS), model_config=CONFIG)
        assert decode([await stream.__anext__()])[0]["tool_call_preview"]
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(stream.__anext__(), timeout=.02)
        assert writer.closed
    asyncio.run(run())


def test_compaction_messages_uses_native_parameters(monkeypatch):
    from factory.agent_runtime import context_compaction as compaction
    config = dict(CONFIG, maxInputTokens=128000)
    monkeypatch.setattr(compaction, "resolve_context_compaction_model_config", lambda settings: config)
    monkeypatch.setattr(compaction, "get_role_selection", lambda role: {"ownership_name": "P", "model_name": "Claude"})
    monkeypatch.setattr(compaction, "get_role_parameter", lambda role: {"output_config": {"effort": "low"}})
    sock = sockets(monkeypatch, [http(json.dumps(response()).encode())])[0]
    result = asyncio.run(compaction.summarize_context_text("待压缩正文", request(), scope="history"))
    assert result.text == "你好"
    assert sock.payload()["output_config"] == {"effort": "low"}
    assert "temperature" not in sock.payload() and "top_p" not in sock.payload()


def test_native_session_delete_cleans_only_its_directory(monkeypatch, tmp_path):
    from memory import chat_memory
    from memory.chat_memory import ChatMemoryManager
    monkeypatch.setattr(chat_memory, "HISTORY_ROOT", tmp_path / "history")
    monkeypatch.setattr(chat_memory, "UPLOAD_HISTORY_ROOT", tmp_path / "uploads")
    req = request()
    payload, _, _ = build_payload(req, CONFIG, False)
    first = native_store.save_record(req.session_id, CONFIG, payload, response())
    second = native_store.save_record("other_session", CONFIG, payload, response())
    ChatMemoryManager.delete_chat_session_file(req.session_id)
    assert native_store.load_record(req.session_id, first) is None
    assert native_store.load_record("other_session", second) is not None


@pytest.mark.parametrize("kind", ["truncated", "terminal_error", "refusal"])
def test_compaction_never_commits_partial_or_failed_summary(monkeypatch, kind):
    from factory.agent_runtime import context_compaction as compaction
    config = dict(CONFIG, maxInputTokens=128000)
    monkeypatch.setattr(compaction, "resolve_context_compaction_model_config", lambda settings: config)
    monkeypatch.setattr(compaction, "require_default_chat_config", lambda: config)
    monkeypatch.setattr(compaction, "resolve_compaction_retry_max_attempts", lambda: 1)
    async def run():
        reader, writer = asyncio.StreamReader(), Writer()
        events = frames(complete=False)
        if kind == "terminal_error":
            events.append({"type": "error", "error": {"type": "invalid_request_error", "message": "invalid"}})
        elif kind == "refusal":
            events = frames(reason="refusal")
        reader.feed_data(http(sse(events)))
        reader.feed_eof()
        monkeypatch.setattr(ChatLLM, "_open_connection", AsyncMock(return_value=(reader, writer)))
        with pytest.raises(compaction.ContextCompactionError):
            await compaction.summarize_context_text("完整源历史", request(), scope="history", delta_emitter=AsyncMock())
        assert writer.closed
    asyncio.run(run())


def test_old_history_with_thinking_uses_facts_without_fabricated_blocks():
    req = request(thinking={"type": "enabled", "budget_tokens": 1024}, messages=[
        {"role": "user", "content": "旧任务"},
        {"role": "assistant", "tool_calls": [{"id": "a", "function": {"name": "work", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "a", "content": "已完成"},
        {"role": "user", "content": "继续"}])
    payload, _, _ = build_payload(req, CONFIG, True)
    assert all(block["type"] == "text" for msg in payload["messages"] for block in msg["content"])
    assert "signature" not in json.dumps(payload)


def test_text_history_preserves_pending_native_recovery_guard():
    from memory.chat_history_format import round_entry_to_context_messages
    req = request(tools=TOOLS)
    payload, _, _ = build_payload(req, CONFIG, True)
    ref = native_store.save_record(req.session_id, CONFIG, payload, response(SIGNED, "tool_use"))
    entry = {"type": "chat_round", "round": 1, "question": "工作", "events": [
        {"role": "assistant", "tool_calls": normalize_response(response(SIGNED, "tool_use"))["tool_calls"], "provider_state": ref}]}
    history = round_entry_to_context_messages(entry, 0)
    assert history[-1]["provider_state"] == ref
    with pytest.raises(MessagesProtocolError, match="状态未知"):
        build_payload(request(messages=history), CONFIG, True)


def test_later_plain_answer_cannot_revive_old_native_turn():
    req = request(tools=TOOLS)
    payload, _, _ = build_payload(req, CONFIG, True)
    ref = native_store.save_record(req.session_id, CONFIG, payload, response(SIGNED, "tool_use"))
    messages = [{"role": "user", "content": "工作"}, {"role": "assistant", "provider_state": ref, "content": "开始"},
                {"role": "assistant", "content": "已由其他模型完成"}, {"role": "user", "content": "新任务"}]
    payload, parent, _ = build_payload(request(messages=messages), CONFIG, True)
    assert parent is None and "signature" not in json.dumps(payload)
