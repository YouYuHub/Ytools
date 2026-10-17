"""Local context replay, tool pairs and failure handling for stateless gateways."""
import asyncio
import copy
import json
from unittest import mock

import pytest

from chat import responses_client as client
from chat.chat_llm import ChatLLM
from chat.protocols import responses_store as store
from chat.protocols.errors import ResponsesProtocolError
from chat.protocols.responses import parameters, normalize_response, response_mode
from config import ChatLLMRequest
from factory.agent_runtime.responses_context import prepare
from factory.agent_runtime.chat_runtime import build_request_messages, copy_for_request
from test_responses_api import CONFIG, answer, turn, AsyncStream

STATELESS = dict(CONFIG, responsesMode="stateless")


@pytest.fixture
def journal(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_root", lambda: tmp_path)
    monkeypatch.setattr(store, "_mirror", lambda *args: None)
    monkeypatch.setattr(client, "_retry_delay", lambda *args: 0)
    monkeypatch.setattr(client, "_maximum", lambda: 3)


def request(messages, **kwargs):
    value = turn(messages, **kwargs)
    value.messages = build_request_messages(copy_for_request(messages, api_type="responses"))
    return value


def save(req, response, config=STATELESS):
    payload, ids, state = prepare(req, config, False)
    record = store.begin_request(req.session_id, state["scope"], payload, config, ids, snapshot=state)
    return client._commit(req, record, response)


def native_answer(response_id="stateless_one", text="A", *, calls=()):
    value = answer(response_id, text, calls=calls)
    value["store"] = False
    value["output"].insert(0, {"type": "reasoning", "id": "rs_" + response_id,
        "summary": [{"type": "summary_text", "text": "summary"}], "encrypted_content": "cipher_" + response_id})
    return value


def test_mode_is_explicit_and_store_cannot_be_overridden():
    req = ChatLLMRequest(messages=[], extra_body={"store": True, "previous_response_id": "bad"})
    assert parameters(req, STATELESS, False)["store"] is False
    req.responses_mode = "stateful"
    assert parameters(req, STATELESS, False)["store"] is True
    req.responses_mode = "stateless"
    assert parameters(req, CONFIG, False)["store"] is False
    assert "responses_mode" not in parameters(req, CONFIG, False)
    with pytest.raises(ResponsesProtocolError): response_mode(req.model_copy(update={"responses_mode": "invalid"}), CONFIG)
    assert normalize_response(native_answer(), require_stored=False)["content"] == "A"
    with pytest.raises(ResponsesProtocolError): normalize_response(native_answer())


def test_every_turn_sends_context_and_complete_native_output(journal):
    messages = [{"role": "system", "content": "always obey"}, {"role": "user", "content": "remember code 4137"}]
    req = request(messages)
    first = save(req, native_answer())
    messages += [{"role": "assistant", "content": "A", "provider_state": first["provider_state"]},
                 {"role": "user", "content": "what code?"}]
    second = request(messages)
    payload, _, _ = prepare(second, STATELESS, True)
    assert payload["store"] is False and "previous_response_id" not in payload
    assert payload["instructions"] == "always obey"
    assert payload["input"][0]["content"][0]["text"] == "remember code 4137"
    assert [i["type"] for i in payload["input"] if "type" in i] == ["reasoning", "message"]
    assert payload["input"][1]["encrypted_content"] == "cipher_stateless_one"
    assert payload["input"][2]["phase"] == "final_answer"
    assert payload["input"][-1]["content"][0]["text"] == "what code?"
    assert not store.public_state(store.load_state("s"))["has_remote_head"]


def test_tool_loop_replays_function_call_result_and_encrypted_reasoning(journal):
    messages = [{"role": "user", "content": "read image"}]
    req = request(messages)
    first = save(req, native_answer(text="", calls=[("call_image", "read_media", "{}")]))
    messages += [{"role": "assistant", "tool_calls": first["tool_calls"], "provider_state": first["provider_state"]},
        {"role": "tool", "tool_call_id": "call_image", "content": "image loaded"},
        {"role": "user", "tool_call_id": "call_image", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,YQ=="}}]}]
    req.messages = build_request_messages(copy_for_request(messages, api_type="responses"))
    payload, _, _ = prepare(req, STATELESS, True)
    assert "previous_response_id" not in payload and payload["store"] is False
    calls = [i for i in payload["input"] if i.get("type") == "function_call"]
    results = [i for i in payload["input"] if i.get("type") == "function_call_output"]
    assert len(calls) == len(results) == 1 and calls[0]["call_id"] == results[0]["call_id"] == "call_image"
    assert results[0]["output"][-1]["type"] == "input_image"
    assert any(i.get("encrypted_content") for i in payload["input"])


def test_new_account_never_reuses_old_encrypted_output(journal):
    req = request([{"role": "user", "content": "Q"}])
    first = save(req, native_answer())
    later = request([{"role": "user", "content": "Q"}, {"role": "assistant", "content": "A", "provider_state": first["provider_state"]},
                     {"role": "user", "content": "continue"}])
    payload, _, _ = prepare(later, dict(STATELESS, apiKey="another-fixture-account"), False)
    assert "cipher_" not in json.dumps(payload)
    assert any(i.get("role") == "assistant" for i in payload["input"])


def test_mode_change_rebuilds_once_and_local_compaction_is_authoritative(journal):
    req = request([{"role": "user", "content": "old large context"}])
    save(req, answer(), CONFIG)
    req.responses_mode = "stateless"
    req.messages = build_request_messages([{"role": "system", "content": "compressed local facts"}, {"role": "user", "content": "continue"}])
    payload, _, _ = prepare(req, CONFIG, False)
    assert "previous_response_id" not in payload and payload["store"] is False
    assert "old large context" not in json.dumps(payload)
    assert payload["instructions"] == "compressed local facts"
    from factory.agent_runtime import responses_compaction as coordinator
    async def local(emit): return "local compacted"
    with mock.patch.object(coordinator, "compact", side_effect=AssertionError("must not compact remotely")):
        assert asyncio.run(coordinator.coordinate(local, req, CONFIG, manual=True)) == "local compacted"


class FakeAsync:
    def __init__(self, response=None, *, disconnect=False, include_conflict=False, connect_failure=False):
        self.responses = self
        self.response = response or native_answer()
        self.disconnect = disconnect
        self.include_conflict = include_conflict
        self.connect_failure = connect_failure
        self.creates, self.retrieves = [], []
    async def create(self, **kwargs):
        self.creates.append(copy.deepcopy(kwargs))
        if self.connect_failure and len(self.creates) == 1: raise ConnectionError("connect")
        if self.include_conflict and "reasoning.encrypted_content" in kwargs.get("include", []):
            error = RuntimeError("`reasoning.encrypted_content` cannot be used with `previous_response_id`")
            error.status_code = 400
            raise error
        events = [{"type": "response.created", "response": {"id": self.response["id"]}}]
        if not self.disconnect: events.append({"type": "response.completed", "response": self.response})
        return AsyncStream(events, ConnectionError("disconnect") if self.disconnect else None)
    async def retrieve(self, response_id, **kwargs):
        self.retrieves.append(response_id)
        raise AssertionError("stateless must never query an ID")
    async def close(self): pass


def run_stream(req, config, fake):
    async def run():
        with mock.patch.object(client, "_client", return_value=(fake, {})):
            return [frame async for frame in client.async_stream(ChatLLM, req, config)]
    return [json.loads(frame[6:]) for frame in asyncio.run(run()) if frame.startswith("data: {")]


def test_stateless_disconnect_releases_pending_without_query_or_tools(journal):
    fake = FakeAsync(disconnect=True)
    frames = run_stream(request([{"role": "user", "content": "Q"}]), STATELESS, fake)
    assert fake.retrieves == [] and len(fake.creates) == 1
    assert any("无状态" in frame.get("error", "") and frame["non_retryable"] for frame in frames)
    assert not any(frame.get("provider_state") or frame.get("tool_calls") for frame in frames)
    assert store.pending_request("s", "chat_model") is None


def test_stateless_retry_before_acceptance_freezes_context(journal):
    fake = FakeAsync(connect_failure=True)
    frames = run_stream(request([{"role": "user", "content": "Q"}]), STATELESS, fake)
    assert fake.creates[0] == fake.creates[1] and len(fake.creates) == 2
    assert fake.retrieves == [] and any(frame.get("provider_state") for frame in frames)


def test_old_stateless_pending_is_not_queried_after_restart(journal):
    req = request([{"role": "user", "content": "Q"}])
    payload, ids, state = prepare(req, STATELESS, True)
    record = store.begin_request("s", "chat_model", payload, STATELESS, ids, snapshot=state)
    store.note_response_id("s", record, "unqueryable")
    fake = FakeAsync()
    frames = run_stream(request([{"role": "user", "content": "continue"}]), STATELESS, fake)
    assert fake.retrieves == [] and any(frame.get("provider_state") for frame in frames)
    assert store.load_record("s", {"protocol": "responses", "record_id": record["record_id"]})["status"] == "failed"


def test_stateful_gateway_include_conflict_retries_same_parent_once(journal):
    save(request([{"role": "user", "content": "Q"}]), answer("parent"), CONFIG)
    fake = FakeAsync(answer("next"), include_conflict=True)
    frames = run_stream(request([{"role": "user", "content": "new question"}]), CONFIG, fake)
    assert len(fake.creates) == 2 and all(p["previous_response_id"] == "parent" for p in fake.creates)
    assert fake.creates[0]["input"] == fake.creates[1]["input"]
    assert "include" not in fake.creates[1]
    assert not any(frame.get("error") for frame in frames)
    payload, _, _ = prepare(request([{"role": "user", "content": "third"}]), CONFIG, False)
    assert payload["previous_response_id"] == "next" and "include" not in payload


def test_role_parameters_preserve_mode_without_leaking_into_other_protocols(monkeypatch):
    import env_manager
    monkeypatch.setattr(env_manager, "get_role_parameter", lambda role: {"responses_mode": "stateless"})
    assert env_manager.apply_role_parameter_defaults({}, "chat_model")["responses_mode"] == "stateless"
    req = ChatLLMRequest(messages=[{"role": "user", "content": "Q"}], responses_mode="stateless")
    assert "responses_mode" not in ChatLLM._build_request_payload(req, False, "test")


class FakeSync:
    def __init__(self, *, stream=False, disconnect=False, conflict=False):
        self.responses = self
        self.stream, self.disconnect, self.conflict = stream, disconnect, conflict
        self.creates, self.retrieves = [], []
    def create(self, **kwargs):
        self.creates.append(copy.deepcopy(kwargs))
        if self.conflict and "reasoning.encrypted_content" in kwargs.get("include", []):
            error = RuntimeError("`reasoning.encrypted_content` cannot be used with `previous_response_id`")
            error.status_code = 400
            raise error
        raw = answer("sync") if self.conflict else native_answer("sync")
        if not self.stream: return raw
        def events():
            yield {"type": "response.created", "response": {"id": raw["id"]}}
            if self.disconnect: raise ConnectionError("sync disconnected")
            yield {"type": "response.completed", "response": raw}
        return events()
    def retrieve(self, response_id, **kwargs):
        self.retrieves.append(response_id)
        raise AssertionError("stateless must not retrieve")
    def close(self): pass


@pytest.mark.parametrize("stream", [False, True])
def test_sync_stateless_accepts_unstored_response(journal, stream):
    fake = FakeSync(stream=stream)
    with mock.patch.object(client, "_client", return_value=(fake, {})):
        frames = list(client._sync_call(request([{"role": "user", "content": "Q"}]), STATELESS, stream=stream))
    assert any(frame.get("provider_state") for frame in frames)
    assert fake.creates[0]["store"] is False and "previous_response_id" not in fake.creates[0]
    assert fake.retrieves == []


def test_sync_stateless_stream_failure_does_not_get_or_retry_partial(journal):
    fake = FakeSync(stream=True, disconnect=True)
    with mock.patch.object(client, "_client", return_value=(fake, {})):
        frames = list(client._sync_call(request([{"role": "user", "content": "Q"}]), STATELESS, stream=True))
    assert len(fake.creates) == 1 and fake.retrieves == []
    assert frames[-1]["discard_tool_calls"] and "无状态" in frames[-1]["error"]
    assert store.pending_request("s", "chat_model") is None


def test_sync_stateful_include_conflict_keeps_parent(journal):
    save(request([{"role": "user", "content": "Q"}]), answer("parent"), CONFIG)
    fake = FakeSync(conflict=True)
    with mock.patch.object(client, "_client", return_value=(fake, {})):
        frames = list(client._sync_call(request([{"role": "user", "content": "new"}]), CONFIG, stream=False))
    assert len(fake.creates) == 2 and all(item["previous_response_id"] == "parent" for item in fake.creates)
    assert frames[-1]["provider_state"] and "include" not in fake.creates[-1]
