"""Stateful wire contract, journal recovery and compaction boundaries."""
import asyncio
import copy
import json
from unittest import mock

import pytest

from chat.chat_llm import ChatLLM
from chat import responses_client
from chat.protocols import responses_store as store
from chat.protocols.responses import endpoint, parameters, normalize_response, ResponsesStream
from chat.protocols.responses_content import convert_messages, content_parts, native_input_items
from chat.protocols.errors import ResponsesProtocolError
from config import ChatLLMRequest, Message, ToolDefinition
from factory.agent_runtime.responses_context import start_turn, prepare, committed, mark_baseline


CONFIG = {"apiType": "responses", "url": "https://example.invalid/v1",
          "apiKey": "test-key", "selected_provider_name": "P", "selected_model_id": "test-model",
          "maxInputTokens": 100000, "maxOutputTokens": 10000}


@pytest.fixture
def journal(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_root", lambda: tmp_path)
    monkeypatch.setattr(store, "_mirror", lambda *args: None)
    return tmp_path


def answer(response_id="resp_1", text="OK", *, calls=()):
    output = [{"type": "message", "id": "msg_" + response_id, "role": "assistant",
               "phase": "final_answer", "status": "completed",
               "content": [{"type": "output_text", "text": text, "annotations": []}]}] if text else []
    output.extend({"type": "function_call", "id": "fc_" + cid, "call_id": cid, "name": name,
                   "arguments": args, "status": "completed"} for cid, name, args in calls)
    return {"id": response_id, "object": "response", "status": "completed", "store": True,
            "created_at": 1791810000, "output": output,
            "usage": {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15,
                      "input_tokens_details": {"cached_tokens": 2},
                      "output_tokens_details": {"reasoning_tokens": 1}}}


def turn(messages, session="s", scope="chat_model", start_index=None):
    context = start_turn(messages, scope=scope, start_index=start_index)
    request = ChatLLMRequest(messages=messages, session_id=session)
    request.responses_context = context
    return request


def commit(request, response):
    payload, ids, state = prepare(request, CONFIG, False)
    record = store.begin_request(request.session_id, state["scope"], payload, CONFIG, ids)
    ref = store.commit_response(request.session_id, record, response)
    committed(request, ids)
    return ref


def test_endpoints_preserve_gateway_prefix_and_query():
    assert endpoint("https://example.invalid/go/v1?tenant=a") == "https://example.invalid/go/v1/responses?tenant=a"
    assert endpoint("https://api.openai.com") == "https://api.openai.com/v1/responses"
    with pytest.raises(ResponsesProtocolError):
        endpoint("https://example.invalid/v1/messages")


def test_parameters_only_explicit_native_fields():
    request = ChatLLMRequest(messages=[{"role": "user", "content": "hello"}],
                             extra_body={"store": False, "previous_response_id": "bad",
                                         "presence_penalty": 2, "enable_thinking": True})
    payload = parameters(request, CONFIG, True)
    assert payload["store"] is True
    assert "temperature" not in payload and "top_p" not in payload
    assert "presence_penalty" not in payload and "previous_response_id" not in payload
    assert payload["max_output_tokens"] == 8192


def test_tools_flatten_and_keep_optional_schema_non_strict():
    request = ChatLLMRequest(messages=[], tools=[ToolDefinition(function={
        "name": "search_files", "parameters": {"type": "object", "properties": {"limit": {"type": "integer"}}}})])
    tool = parameters(request, CONFIG, False)["tools"][0]
    assert tool["name"] == "search_files" and tool["strict"] is False
    assert "function" not in tool


@pytest.mark.parametrize("content", ["旧回复", [{"type": "text", "text": "旧回复"}],
                                    [{"type": "output_text", "text": "旧回复"}],
                                    123, [123]])
def test_assistant_history_always_uses_output_text(content):
    parts = content_parts(content, role="assistant")
    assert parts and all(part["type"] == "output_text" for part in parts)
    assert all(part["annotations"] == [] for part in parts)
    assert all(part["type"] == "input_text" for part in content_parts(content))


def test_legacy_staged_rebuild_is_repaired_without_changing_native_output(journal):
    items = [{"type": "message", "role": "assistant", "content": [
        {"type": "input_text", "text": "legacy string"},
        {"type": "output_text", "text": "native", "annotations": [{"type": "url_citation", "url": "https://example.com"}]},
        {"type": "refusal", "refusal": "native refusal"}], "phase": "final_answer"}]
    original = copy.deepcopy(items)
    store.stage_rebuild("s", items)
    payload, _, _ = prepare(turn([{"role": "user", "content": "continue"}]), CONFIG, False)
    projected = payload["input"][0]
    assert projected["content"][0] == {"type": "output_text", "text": "legacy string", "annotations": []}
    assert projected["content"][1:] == items[0]["content"][1:]
    assert projected["phase"] == "final_answer"
    assert items == original
    assert native_input_items([projected]) == [projected]


def test_new_user_sent_once_and_tool_call_id_used(journal):
    messages = [{"role": "system", "content": "instructions"}, {"role": "user", "content": "question"}]
    request = turn(messages)
    first = answer("resp_A", "", calls=[("call_A", "read_file", '{"path":"x"}')])
    ref = commit(request, first)
    messages.extend([
        {"role": "assistant", "provider_state": ref,
         "tool_calls": normalize_response(first)["tool_calls"]},
        {"role": "tool", "tool_call_id": "call_A", "content": "result"}])
    from factory.agent_runtime.chat_runtime import copy_for_request, build_request_messages
    request.messages = build_request_messages(copy_for_request(messages, api_type="responses"))
    payload, ids, _ = prepare(request, CONFIG, True)
    assert payload["previous_response_id"] == "resp_A"
    assert payload["input"] == [{"type": "function_call_output", "call_id": "call_A", "output": "result"}]
    second = answer("resp_B", "done")
    commit(request, second)
    messages.append({"role": "assistant", "content": "done"})
    messages.append({"role": "user", "content": "new question"})
    next_request = turn(messages)
    payload, _, _ = prepare(next_request, CONFIG, False)
    assert payload["previous_response_id"] == "resp_B"
    assert payload["input"] == [{"role": "user", "content": [{"type": "input_text", "text": "new question"}]}]
    assert payload["instructions"] == "instructions"
    assert len(store.current_window("s", store.load_state("s"))) == 4


def test_scopes_isolated(journal):
    commit(turn([{"role": "user", "content": "parent"}]), answer("resp_parent"))
    child = turn([{"role": "user", "content": "child"}], scope="sub_agent:a", start_index=0)
    payload, _, _ = prepare(child, CONFIG, False)
    assert "previous_response_id" not in payload
    commit(child, answer("resp_child"))
    assert store.load_state("s")["head_response_id"] == "resp_parent"
    assert store.load_state("s", "sub_agent:a")["head_response_id"] == "resp_child"


def test_compact_window_full_and_used_once(journal):
    request = turn([{"role": "user", "content": "old"}])
    commit(request, answer("resp_old"))
    output = [{"role": "user", "content": [{"type": "input_text", "text": "retained"}]},
              {"type": "compaction", "id": "cmp_1", "encrypted_content": "opaque", "future": {"a": 1}}]
    store.save_compaction("s", "chat_model",
                          {"id": "compact_resource", "object": "response.compaction", "output": output},
                          store.load_state("s"))
    next_request = turn([{"role": "user", "content": "next"}])
    payload, _, _ = prepare(next_request, CONFIG, False)
    assert "previous_response_id" not in payload
    assert payload["input"][0] == output[0]
    assert payload["input"][1] == {key: value for key, value in output[1].items() if key != "future"}
    assert store.current_window("s", store.load_state("s")) == output
    commit(next_request, answer("resp_new"))
    assert store.load_state("s")["pending_compaction_ref"] is None
    third = turn([{"role": "user", "content": "third"}])
    payload, _, _ = prepare(third, CONFIG, False)
    assert payload["previous_response_id"] == "resp_new"
    assert len(payload["input"]) == 1


def test_compaction_covers_unsent_tool_results_without_duplicate(journal):
    request = turn([{"role": "user", "content": "q"}])
    commit(request, answer("resp_tool", "", calls=[("call_1", "read_file", "{}")]))
    message = {"role": "tool", "tool_call_id": "call_1", "content": "actual"}
    from factory.agent_runtime.chat_runtime import copy_for_request, build_request_messages
    request.messages = build_request_messages(copy_for_request([message], api_type="responses"))
    covered_id = request.messages[0].context_item_id
    output = [{"type": "compaction", "id": "cmp", "encrypted_content": "opaque"}]
    store.save_compaction("s", "chat_model",
                          {"object": "response.compaction", "output": output}, store.load_state("s"),
                          boundary_ids=[covered_id])
    payload, _, _ = prepare(request, CONFIG, False)
    assert payload["input"] == output


def test_late_response_cannot_replace_changed_history(journal):
    request = turn([{"role": "user", "content": "q"}])
    payload, ids, state = prepare(request, CONFIG, False)
    record = store.begin_request("s", "chat_model", payload, CONFIG, ids)
    store.invalidate("s", "deleted")
    with pytest.raises(ResponsesProtocolError, match="迟到"):
        store.commit_response("s", record, answer())
    assert store.load_state("s")["head_response_id"] is None


def test_tools_cached_and_unknown_execution_never_restarted(journal):
    reference = commit(turn([{"role": "user", "content": "q"}]),
                       answer("resp_t", "", calls=[("call_1", "write_file", "{}")]))
    store.tool_started("s", reference, "call_1")
    with pytest.raises(ResponsesProtocolError, match="未知"):
        store.tool_started("s", reference, "call_1")
    store.tool_completed("s", reference, "call_1", "saved")
    store.tool_started("s", reference, "call_1")
    assert store.tool_result("s", reference, "call_1")["content"] == "saved"


def test_stream_deltas_not_duplicated_by_done_and_compaction():
    machine = ResponsesStream()
    machine.feed({"type": "response.created", "response": {"id": "resp_1"}, "sequence_number": 0})
    assert machine.feed({"type": "response.output_text.delta", "delta": "O", "sequence_number": 1})[0]["content"] == "O"
    assert machine.feed({"type": "response.output_text.delta", "delta": "O", "sequence_number": 1}) == []
    machine.feed({"type": "response.output_item.done", "item": {"type": "compaction", "encrypted_content": "opaque"}, "sequence_number": 2})
    final = machine.feed({"type": "response.completed", "response": answer(), "sequence_number": 3})
    assert [frame["content"] for frame in final if "content" in frame] == ["K"]
    assert final[-1]["response_complete"] is True


def test_malformed_tool_arguments_not_executable():
    with pytest.raises(ResponsesProtocolError):
        normalize_response(answer("resp_bad", "", calls=[("call_1", "write_file", '{"path":')]))


def test_tool_media_belongs_to_function_output():
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,YQ=="}}
    items = convert_messages([
        {"role": "tool", "tool_call_id": "call_img", "content": "image loaded"},
        {"role": "user", "tool_call_id": "call_img", "content": [image]},
    ], delta=True)
    assert len(items) == 1
    assert items[0]["output"][-1]["type"] == "input_image"


class AsyncStream:
    def __init__(self, events, failure=None):
        self.events = iter(events)
        self.failure = failure
    def __aiter__(self):
        return self
    async def __anext__(self):
        try:
            return next(self.events)
        except StopIteration:
            if self.failure:
                failure, self.failure = self.failure, None
                raise failure
            raise StopAsyncIteration
    async def close(self):
        pass


class FakeAsyncClient:
    def __init__(self, response, disconnect=False):
        self.responses = self
        self.response = response
        self.disconnect = disconnect
        self.creates = []
        self.retrieves = []
    async def create(self, **kwargs):
        self.creates.append(kwargs)
        events = [{"type": "response.created", "response": {"id": self.response["id"]}},
                  {"type": "response.output_text.delta", "delta": "O"}]
        if not self.disconnect:
            events.append({"type": "response.completed", "response": self.response})
        return AsyncStream(events, ConnectionError("disconnect") if self.disconnect else None)
    async def retrieve(self, response_id, **kwargs):
        self.retrieves.append(response_id)
        return self.response
    async def close(self):
        pass


def test_async_disconnect_retrieves_instead_of_recreating(journal):
    client = FakeAsyncClient(answer(), disconnect=True)
    request = turn([{"role": "user", "content": "q"}])
    async def run():
        with mock.patch.object(responses_client, "_client", return_value=(client, {})):
            frames = [frame async for frame in responses_client.async_stream(ChatLLM, request, CONFIG)]
        return frames
    frames = asyncio.run(run())
    data = [json.loads(frame[6:]) for frame in frames if frame.startswith("data: {")]
    assert "".join(frame.get("content", "") for frame in data) == "OK"
    assert len(client.creates) == 1
    assert client.retrieves == ["resp_1"]
    assert store.load_state("s")["head_response_id"] == "resp_1"
    assert data[-1]["provider_state"]["protocol"] == "responses"


def test_context_rebuild_baseline_does_not_resubmit_old_users(journal):
    messages = [{"role": "user", "content": "original"}]
    request = turn(messages)
    commit(request, answer())
    rebuilt = [{"role": "system", "content": "local summary"}, {"role": "user", "content": "original"}]
    mark_baseline(request.responses_context, rebuilt)
    rebuilt.append({"role": "user", "content": "new steering"})
    from factory.agent_runtime.chat_runtime import build_request_messages, copy_for_request
    request.messages = build_request_messages(copy_for_request(rebuilt, api_type="responses"))
    payload, _, _ = prepare(request, CONFIG, False)
    assert len(payload["input"]) == 1
    assert payload["input"][0]["content"][0]["text"] == "new steering"


def test_readable_and_native_compaction_run_in_parallel_and_keep_partial(journal):
    from factory.agent_runtime import responses_compaction as coordinator
    request = turn([{"role": "user", "content": "q"}])
    commit(request, answer())
    phases = []
    async def run():
        started = asyncio.Event()
        async def fake_compact(*args, **kwargs):
            started.set()
            raise RuntimeError("native unavailable")
        async def local(emit):
            await emit({"event": "context_compaction", "phase": "start"})
            await asyncio.wait_for(started.wait(), 1)
            return 3
        async def emit(event):
            phases.append(event)
        with mock.patch.object(coordinator, "compact", side_effect=fake_compact):
            return await coordinator.coordinate(local, request, CONFIG, emitter=emit)
    assert asyncio.run(run()) == 3
    assert any(event.get("warning") for event in phases)
    assert store.load_state("s")["last_compaction"]["local_phase"] == "done"


def test_journal_grows_linearly_and_reconstructs_full_window(journal):
    for i in range(12):
        commit(turn([{"role": "user", "content": "q" + str(i)}]), answer("resp_" + str(i)))
    window = store.current_window("s", store.load_state("s"))
    assert len(window) == 24
    for path in store.session_directory("s").glob("*.json"):
        row = json.loads(path.read_text(encoding="utf-8"))
        assert "parent_window" not in row and "window" not in row


def test_trigger_compaction_resets_window_but_retains_head(journal, monkeypatch):
    request = turn([{"role": "system", "content": "persona"}, {"role": "user", "content": "old"}])
    commit(request, answer("resp_old"))
    class Client(FakeAsyncClient):
        async def compact(self, **kwargs):
            exc = RuntimeError("unsupported compact endpoint")
            exc.status_code = 404
            raise exc
        async def create(self, **kwargs):
            self.creates.append(kwargs)
            response = answer("resp_compacted", "")
            response["output"] = [{"type": "compaction", "encrypted_content": "opaque", "id": "cmp_1"}]
            return response
    client = Client(None)
    monkeypatch.setattr(responses_client, "_client", lambda *args, **kwargs: (client, {}))
    config = dict(CONFIG, maxOutputTokens=30000)
    ref = asyncio.run(responses_client.compact(request, config))
    state = store.load_state("s")
    assert state["head_response_id"] == "resp_compacted" and state["compact_strategy"] == "trigger"
    assert state["pending_compaction_ref"] == ref
    assert state["last_compaction"]["activation"] == "pending_rebase"
    assert client.creates[0]["input"] == [{"type": "compaction_trigger"}]
    assert client.creates[0]["instructions"] == "persona"
    assert store.current_window("s", state) == [{"type": "compaction", "encrypted_content": "opaque", "id": "cmp_1"}]
    assert store.load_record("s", {"protocol": "responses", "record_id": ref})["response_id"] == "resp_compacted"


@pytest.mark.parametrize("scope", ["chat_model", "sub_agent:compact-test"])
@pytest.mark.parametrize("local_fails_first", [False, True])
def test_trigger_compaction_resumes_without_resending_tools_or_recompacting(journal, monkeypatch, scope, local_fails_first):
    from factory.agent_runtime import responses_compaction as coordinator
    from factory.agent_runtime.chat_runtime import build_request_messages, copy_for_request
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    request = turn([{"role": "user", "content": "finish task"}], scope=scope)
    ref = commit(request, answer("resp_tool", "", calls=[("call_1", "read_file", "{}")]))
    store.tool_started("s", ref, "call_1")
    store.tool_completed("s", ref, "call_1", "unique tool result")
    messages = [*request.messages, {"role": "tool", "tool_call_id": "call_1", "content": "unique tool result"}]
    request.messages = build_request_messages(copy_for_request(messages, api_type="responses"))
    covered_id = request.messages[-1].context_item_id

    class Client(FakeAsyncClient):
        async def compact(self, **kwargs):
            exc = RuntimeError("unsupported compact")
            exc.status_code = 404
            raise exc
        async def create(self, **kwargs):
            self.creates.append(kwargs)
            response = answer("resp_compacted", "")
            response["output"] = [{"type": "compaction", "id": "cmp_1", "encrypted_content": "opaque"}]
            return response

    client = Client(None)
    monkeypatch.setattr(responses_client, "_client", lambda *args, **kwargs: (client, {}))
    config = dict(CONFIG, maxOutputTokens=30000)
    manager = SimpleNamespace(add_history_compression_usage=AsyncMock())
    async def local(emit):
        await emit({"event": "context_compaction", "phase": "start"})
        return 1
    async def failing_local(emit):
        await emit({"event": "context_compaction", "phase": "start"})
        raise RuntimeError("local summary failed")

    if local_fails_first:
        with pytest.raises(RuntimeError, match="local summary failed"):
            asyncio.run(coordinator.coordinate(failing_local, request, config, manager=manager))
    asyncio.run(coordinator.coordinate(local, request, config, manager=manager))
    assert covered_id in request.responses_context["seen_ids"]
    rebuilt = [{"role": "system", "content": "readable summary"}, {"role": "user", "content": "finish task"}]
    mark_baseline(request.responses_context, rebuilt)
    request.messages = build_request_messages(copy_for_request(rebuilt, api_type="responses"))
    # A history pass and an active-round pass share the same remote boundary.
    asyncio.run(coordinator.coordinate(local, request, config, manager=manager))
    assert len(client.creates) == 1
    assert client.creates[0]["input"] == [
        {"type": "function_call_output", "call_id": "call_1", "output": "unique tool result"},
        {"type": "compaction_trigger"}]
    assert manager.add_history_compression_usage.await_count == 1
    payload, ids, _ = prepare(request, config, True)
    assert "previous_response_id" not in payload and payload["input"] == [
        {"type": "compaction", "id": "cmp_1", "encrypted_content": "opaque"}] and ids == []
    assert "unique tool result" not in json.dumps(payload)
    # The compacted head also works after reopening the private journal/context.
    reopened = turn([{"role": "system", "content": "readable summary"}], scope=scope, start_index=1)
    assert prepare(reopened, config, True)[0]["input"] == payload["input"]
    assert not store.load_state("s", scope).get("compaction_lease")
    commit(request, answer("resp_continued", "task complete"))
    assert store.load_state("s", scope)["last_compaction"]["activation"] == "active"
    with pytest.raises(ResponsesProtocolError, match="没有新的有效输入"):
        prepare(request, config, False)


def test_new_inputs_after_staged_compaction_survive_another_local_summary(journal, monkeypatch):
    from factory.agent_runtime import responses_compaction as coordinator
    from factory.agent_runtime.chat_runtime import build_request_messages, copy_for_request
    request = turn([{"role": "user", "content": "old question"}])
    commit(request, answer("resp_tool", "", calls=[("new_call", "read_file", "{}")]))
    output = [{"type": "compaction", "id": "cmp", "encrypted_content": "opaque"},
              {"type": "function_call", "call_id": "new_call", "name": "read_file", "arguments": "{}"}]
    store.save_compaction("s", "chat_model", {"object": "response.compaction", "output": output}, store.load_state("s"))
    request.messages = build_request_messages(copy_for_request([
        {"role": "tool", "tool_call_id": "new_call", "content": "arrived after compact boundary"},
        {"role": "user", "content": "new steering"}], api_type="responses"))
    new_ids = [message.context_item_id for message in request.messages]
    async def local(emit):
        await emit({"phase": "start"})
        return 1
    monkeypatch.setattr(responses_client, "_client", mock.Mock(side_effect=AssertionError("must reuse staged window")))
    asyncio.run(coordinator.coordinate(local, request, CONFIG))
    assert set(request.responses_context["deferred_ids"]) == set(new_ids)
    rebuilt = [{"role": "system", "content": "local summary after new result"}]
    mark_baseline(request.responses_context, rebuilt)
    request.messages = build_request_messages(copy_for_request(rebuilt, api_type="responses"))
    payload, _, _ = prepare(request, CONFIG, False)
    assert payload["input"] == [*output,
        {"type": "function_call_output", "call_id": "new_call", "output": "arrived after compact boundary"},
        {"role": "user", "content": [{"type": "input_text", "text": "new steering"}]}]
    commit(request, answer("resp_after_compact"))
    assert "deferred_items" not in request.responses_context


def test_legacy_trigger_rebases_later_messages_and_cached_tool_results(journal):
    request = turn([{"role": "user", "content": "huge original history"}])
    commit(request, answer("resp_before"))
    raw = answer("resp_compacted", "")
    raw["output"] = [{"type": "compaction", "id": "cmp", "encrypted_content": "opaque"}]
    control = store.begin_request("s", "chat_model", {"previous_response_id": "resp_before", "input": [{"type": "compaction_trigger"}]}, CONFIG, [])
    ref = store.commit_response("s", control, raw)
    store.note_trigger_compaction("s", "chat_model", ref, raw)
    # Simulate records made by the previous version, including one later turn.
    state = store.load_state("s")
    state["pending_compaction_ref"] = None
    state["last_compaction"].pop("activation")
    store._write(store._state_path("s", "chat_model"), state)
    after = answer("resp_after", "", calls=[("call_later", "read_file", "{}")])
    after["usage"]["input_tokens"] = 462904
    record = store.begin_request("s", "chat_model", {"previous_response_id": raw["id"],
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "intervening question"}]}]}, CONFIG, [])
    latest = store.commit_response("s", record, after)
    store.tool_started("s", latest, "call_later")
    store.tool_completed("s", latest, "call_later", "confirmed result after compact")
    resumed = turn([{"role": "system", "content": "small local summary"}, {"role": "user", "content": "continue"}])
    payload, _, _ = responses_client._prepare(resumed, CONFIG, True)
    assert "previous_response_id" not in payload
    assert payload["input"][0]["type"] == "compaction"
    assert "intervening question" in json.dumps(payload)
    assert "huge original history" not in json.dumps(payload)
    assert [item for item in payload["input"] if item.get("type") == "function_call_output"] == [
        {"type": "function_call_output", "call_id": "call_later", "output": "confirmed result after compact"}]
    commit(resumed, answer("resp_new_root"))
    state = store.load_state("s")
    assert state["last_compaction"]["activation"] == "active"
    assert state["last_compaction"]["input_tokens_after"] == 12
    next_payload, _, _ = responses_client._prepare(turn([{"role": "user", "content": "new question"}]), CONFIG, True)
    assert next_payload["previous_response_id"] == "resp_new_root" and len(next_payload["input"]) == 1


def test_trigger_recovery_stages_activation_in_the_same_commit(journal, monkeypatch):
    commit(turn([{"role": "user", "content": "old"}]), answer("resp_before"))
    control = store.begin_request("s", "chat_model", {"previous_response_id": "resp_before", "input": [{"type": "compaction_trigger"}]}, CONFIG, [])
    store.note_response_id("s", control, "resp_control")
    store.abandon_request("s", control, "interrupted after response id", unknown=True)
    compacted = answer("resp_control", "")
    compacted["output"] = [{"type": "compaction", "encrypted_content": "opaque"}]
    class Client(FakeAsyncClient):
        async def retrieve(self, response_id, **kwargs):
            self.retrieves.append(response_id)
            return compacted
    client = Client(answer("resp_activated"))
    monkeypatch.setattr(responses_client, "_client", lambda *args, **kwargs: (client, {}))
    async def run():
        return [frame async for frame in responses_client.async_stream(ChatLLM, turn([{"role": "user", "content": "continue"}]), CONFIG)]
    frames = asyncio.run(run())
    assert not [frame for frame in frames if '"error"' in frame]
    assert client.retrieves == ["resp_control"] and len(client.creates) == 1
    assert "previous_response_id" not in client.creates[0] and client.creates[0]["input"][0]["type"] == "compaction"
    assert store.load_state("s")["last_compaction"]["activation"] == "active"


def test_local_rebuild_does_not_inherit_legacy_activation(journal):
    request = turn([{"role": "user", "content": "old"}])
    commit(request, answer("resp_old"))
    raw = answer("resp_compacted", "")
    raw["output"] = [{"type": "compaction", "encrypted_content": "opaque"}]
    control = store.begin_request("s", "chat_model", {"previous_response_id": "resp_old", "input": [{"type": "compaction_trigger"}]}, CONFIG, [])
    store.commit_response("s", control, raw)
    assert store.load_state("s")["last_compaction"]["activation"] == "pending_rebase"
    store.stage_rebuild("s", [{"role": "user", "content": [{"type": "input_text", "text": "local facts"}]}], source="local")
    assert not store.needs_compaction_activation(store.load_state("s"))
    new = turn([{"role": "user", "content": "next"}])
    commit(new, answer("resp_local_root"))
    payload, _, _ = prepare(turn([{"role": "user", "content": "following"}]), CONFIG, False)
    assert payload["previous_response_id"] == "resp_local_root"


@pytest.mark.parametrize("scope", ["chat_model", "sub_agent:pressure"])
def test_remote_pressure_compacts_even_when_local_summary_is_small(journal, monkeypatch, scope):
    from factory.agent_runtime import responses_compaction as coordinator
    from factory.agent_runtime import context_compaction
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    request = turn([{"role": "user", "content": "small local question"}], scope=scope)
    raw = answer("resp_large")
    raw["usage"]["input_tokens"] = 90000
    commit(request, raw)
    monkeypatch.setattr(context_compaction, "load_context_compaction_settings", lambda: SimpleNamespace(trigger_ratio=0.8))
    class Client(FakeAsyncClient):
        async def compact(self, **kwargs):
            exc = RuntimeError("unsupported compact")
            exc.status_code = 404
            raise exc
        async def create(self, **kwargs):
            self.creates.append(kwargs)
            response = answer("resp_compacted", "")
            response["output"] = [{"type": "compaction", "encrypted_content": "opaque"}]
            return response
    client = Client(None)
    monkeypatch.setattr(responses_client, "_client", lambda *args, **kwargs: (client, {}))
    manager = SimpleNamespace(add_history_compression_usage=AsyncMock())
    async def local(emit):
        return "no local compression needed"
    assert asyncio.run(coordinator.coordinate(local, request, dict(CONFIG, maxOutputTokens=30000), manager=manager)) == "no local compression needed"
    assert len(client.creates) == 1
    assert manager.add_history_compression_usage.await_count == 1
    payload, _, _ = responses_client._prepare(request, CONFIG, False)
    assert "previous_response_id" not in payload and payload["input"][0]["type"] == "compaction"


def test_unsent_results_survive_local_success_native_failure(journal, monkeypatch):
    from factory.agent_runtime import responses_compaction as coordinator
    from factory.agent_runtime.chat_runtime import build_request_messages, copy_for_request
    messages = [{"role": "user", "content": "q"}]
    request = turn(messages)
    commit(request, answer("resp_tools", "", calls=[("call_1", "read_file", "{}")]))
    messages.append({"role": "tool", "tool_call_id": "call_1", "content": "important result"})
    request.messages = build_request_messages(copy_for_request(messages, api_type="responses"))
    async def unavailable(*args, **kwargs):
        raise RuntimeError("native unavailable")
    async def local(emit):
        await emit({"phase": "start"})
        return 1
    monkeypatch.setattr(coordinator, "compact", unavailable)
    asyncio.run(coordinator.coordinate(local, request, CONFIG))
    rebuilt = [{"role": "system", "content": "local summary"}, {"role": "user", "content": "q"}]
    mark_baseline(request.responses_context, rebuilt)
    request.messages = build_request_messages(copy_for_request(rebuilt, api_type="responses"))
    payload, _, _ = prepare(request, CONFIG, False)
    assert payload["input"] == [{"type": "function_call_output", "call_id": "call_1", "output": "important result"}]
    commit(request, answer("resp_resumed"))
    assert "deferred_items" not in request.responses_context


def test_dead_compaction_process_cannot_lock_session_forever(journal, monkeypatch):
    commit(turn([{"role": "user", "content": "q"}]), answer())
    store.mark_compacting("s", "chat_model", True)
    assert store.load_state("s").get("compaction_lease")
    monkeypatch.setattr(store, "process_identity", lambda pid: None)
    assert not store.load_state("s").get("compaction_lease")
    assert store.load_state("s")["head_response_id"] == "resp_1"
    assert store.load_state("s")["source_key"] == store.source_key(CONFIG)


def test_root_history_repairs_dangling_calls_without_running_them():
    items = convert_messages([
        {"role": "assistant", "tool_calls": [{"id": "unknown", "function": {"name": "write_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "orphan", "content": "existing fact"}])
    assert not any(item.get("type") in {"function_call", "function_call_output"} for item in items)
    assert "没有确认执行结果" in items[0]["content"][0]["text"]
    assert "existing fact" in items[1]["content"][1]["text"]


def test_share_history_strips_private_pointers_preserves_facts():
    from memory.chat_memory import _portable_responses_history
    rows = [{"_meta": {"title": "title", "responses_state": {"head_response_id": "resp_secret"}}},
            {"event": "chat_round", "events": [
                {"role": "assistant", "content": "fact", "provider_state": {"protocol": "responses", "record_id": "secret"}},
                {"event": "model_response", "protocol": "responses", "response_id": "resp_secret", "record_ref": "secret"}]}]
    exported = _portable_responses_history("\n".join(json.dumps(row) for row in rows))
    assert "secret" not in exported and "responses_state" not in exported and "fact" in exported


class ScriptClient(FakeAsyncClient):
    def __init__(self, responses):
        super().__init__(None)
        self.script = iter(responses)
    async def create(self, **kwargs):
        self.creates.append(kwargs)
        raw = next(self.script)
        return AsyncStream([{"type": "response.created", "response": {"id": raw["id"]}},
                            {"type": "response.completed", "response": raw}])


def test_main_agent_tool_loop_and_private_frontend_contract(journal, monkeypatch):
    import test_todo_no_tools_regression as harness
    from unittest.mock import AsyncMock
    monkeypatch.setattr(harness._MemoryStub, "update_current_round_usage", AsyncMock(), raising=False)
    client = ScriptClient([answer("resp_tool", "", calls=[("call_1", "todo_write",
        '{"todos":[{"id":"1","content":"verified","status":"done"}]}')]), answer("resp_done")])
    monkeypatch.setattr(responses_client, "_client", lambda *args, **kwargs: (client, {}))
    async def run():
        return await harness._run_case(["todo_write"], llm=ChatLLM, resolve_config=lambda: CONFIG)
    stream = asyncio.run(run())
    assert len(client.creates) == 2
    assert client.creates[1]["previous_response_id"] == "resp_tool"
    assert client.creates[1]["input"][0]["type"] == "function_call_output"
    assert all(item.get("role") != "assistant" for item in client.creates[1]["input"])
    assert harness._last_memory.todos[0]["content"] == "verified"
    visible = "".join(stream.chunks)
    assert "provider_state" not in visible and "response_record" not in visible and "record_ref" not in visible
    assert len([record for record in harness._last_memory.records if isinstance(record, dict) and record.get("event") == "model_response"]) == 2


def test_child_agent_uses_own_chain_and_does_not_repeat_write(journal, monkeypatch):
    from test_sub_agent import _make_context
    from factory.agent_runtime.sub_agent import SubAgentRunner
    context, events, _ = _make_context()
    context.initial_todo = []
    runner = SubAgentRunner(context)
    monkeypatch.setattr(runner, "_resolve_request_model_config", lambda: (CONFIG, {}))
    client = ScriptClient([answer("resp_child_call", "", calls=[("call_1", "write_file",
                          json.dumps({"path": "child.txt", "content": "saved", "mode": "create"}))]), answer("resp_child_done")])
    monkeypatch.setattr(responses_client, "_client", lambda *args, **kwargs: (client, {}))
    writes = []
    def write(name, args):
        writes.append((name, args))
        return "saved"
    monkeypatch.setattr("factory.agent_runtime.sub_agent.try_execute_builtin_file_tool", write)
    result = asyncio.run(runner.run())
    assert result.status == "done" and len(writes) == 1
    assert store.load_state(context.session_id)["head_response_id"] is None
    assert store.load_state(context.session_id, "sub_agent:" + context.agent_id)["head_response_id"] == "resp_child_done"
    assert client.creates[1]["input"] == [{"type": "function_call_output", "call_id": "call_1", "output": "saved"}]
    assert len([payload for _, payload in events if payload.get("event") == "model_response"]) == 2


@pytest.mark.parametrize("message,status", [
    ("previous_response_id not found", 404),
    ("referenced response not found or expired", 400),
])
def test_sync_nonstream_expired_parent_rebuilt_once(journal, monkeypatch, message, status):
    commit(turn([{"role": "user", "content": "old fact"}]), answer("resp_old"))
    class Client:
        def __init__(self):
            self.responses = self
            self.creates = []
        def create(self, **kwargs):
            self.creates.append(kwargs)
            if len(self.creates) == 1:
                exc = RuntimeError(message)
                exc.status_code = status
                raise exc
            return answer("resp_rebuilt")
        def close(self):
            pass
    client = Client()
    monkeypatch.setattr(responses_client, "_client", lambda *args, **kwargs: (client, {}))
    result = responses_client.nonstream(ChatLLM, turn([{"role": "user", "content": "new"}]), CONFIG)
    assert result["content"] == "OK" and len(client.creates) == 2
    assert "previous_response_id" not in client.creates[1]
    assert client.creates[1]["input"][0]["content"][0]["text"] == "old fact"
    assert store.load_state("s")["head_response_id"] == "resp_rebuilt"


def test_incomplete_response_never_commits_tools(journal, monkeypatch):
    raw = answer("resp_bad", "", calls=[("call_1", "write_file", "{}")])
    raw["status"] = "incomplete"
    raw["incomplete_details"] = {"reason": "max_output_tokens"}
    client = ScriptClient([raw])
    monkeypatch.setattr(responses_client, "_client", lambda *args, **kwargs: (client, {}))
    async def run():
        return [frame async for frame in responses_client.async_stream(ChatLLM, turn([{"role": "user", "content": "q"}]), CONFIG)]
    frames = asyncio.run(run())
    assert not any('"tool_calls"' in frame for frame in frames)
    assert store.load_state("s")["head_response_id"] is None
    assert store.load_state("s")["pending_request_ref"] is None


def test_worker_dispatch_preserves_omitted_sampling_parameters(journal, monkeypatch):
    from factory import chat_factory
    from unittest.mock import AsyncMock
    from types import SimpleNamespace
    manager = SimpleNamespace(ensure_session_config_snapshot=AsyncMock())
    monkeypatch.setattr(chat_factory, "get_chat_memory_manager", AsyncMock(return_value=manager))
    monkeypatch.setattr(chat_factory, "_wait_worker_generation", AsyncMock())
    sent = []
    proxy = SimpleNamespace(bind_stream=lambda stream: None,
                            start_generation=lambda payload: sent.append(payload) or True)
    async def run():
        request = ChatLLMRequest(messages=[{"role": "user", "content": "q"}], max_output_tokens=1024,
                                 reasoning={"effort": "low"})
        stream = SimpleNamespace(task=None, finish=AsyncMock())
        assert await chat_factory._start_session_generation(proxy, stream, request)
        await stream.task
        stream.finish.assert_awaited_once()
    asyncio.run(run())
    reconstructed = ChatLLMRequest(**sent[0])
    payload = parameters(reconstructed, CONFIG, True)
    assert "temperature" not in payload and "top_p" not in payload
    assert payload["reasoning"] == {"effort": "low"}
    assert payload["max_output_tokens"] == 1024


def test_http_auth_failure_not_retried_or_committed(journal, monkeypatch):
    class Client(FakeAsyncClient):
        async def create(self, **kwargs):
            self.creates.append(kwargs)
            exc = RuntimeError("invalid API key")
            exc.status_code = 401
            raise exc
    client = Client(None)
    monkeypatch.setattr(responses_client, "_client", lambda *args, **kwargs: (client, {}))
    async def run():
        return [frame async for frame in responses_client.async_stream(ChatLLM, turn([{"role": "user", "content": "q"}]), CONFIG)]
    frames = asyncio.run(run())
    assert len(client.creates) == 1
    assert store.load_state("s")["head_response_id"] is None
    assert '"non_retryable": true' in "".join(frames)


def test_stop_after_created_id_keeps_uncertain_request(journal, monkeypatch):
    stopped = False
    class Stream(AsyncStream):
        async def __anext__(self):
            nonlocal stopped
            event = await super().__anext__()
            stopped = True
            return event
    class Client(FakeAsyncClient):
        async def create(self, **kwargs):
            return Stream([{"type": "response.created", "response": {"id": "resp_pending"}}])
    client = Client(None)
    monkeypatch.setattr(responses_client, "_client", lambda *args, **kwargs: (client, {}))
    async def run():
        return [frame async for frame in responses_client.async_stream(ChatLLM, turn([{"role": "user", "content": "q"}]), CONFIG, lambda: stopped)]
    asyncio.run(run())
    pending = store.pending_request("s", "chat_model")
    assert pending["response_id"] == "resp_pending" and pending["status"] == "unknown"
    assert store.load_state("s")["head_response_id"] is None


def test_native_document_parts_fold_into_own_tool_result():
    parts = [{"type": "file", "file": {"filename": "a.txt", "file_data": "data:text/plain;base64,YQ=="}, "_responses_call_id": "a"},
             {"type": "file", "file": {"filename": "b.pdf", "file_data": "data:application/pdf;base64,Yg=="}, "_responses_call_id": "b"}]
    output = convert_messages([{"role": "tool", "tool_call_id": "a", "content": "loaded a"},
                               {"role": "tool", "tool_call_id": "b", "content": "loaded b"},
                               {"role": "user", "content": parts}], delta=True)
    assert len(output) == 2
    assert output[0]["output"][1]["filename"] == "a.txt"
    assert output[1]["output"][1]["filename"] == "b.pdf"


def test_protocol_switch_requires_new_root(journal):
    request = turn([{"role": "user", "content": "old account fact"}])
    commit(request, answer("resp_old"))
    new = turn([{"role": "user", "content": "current local facts"}])
    payload, _, state = prepare(new, dict(CONFIG, apiKey="different-account"), False)
    assert "previous_response_id" not in payload
    assert state["generation"] == 1 and state["head_response_id"] is None
    assert "old account fact" not in json.dumps(payload)


def test_sync_recovery_validates_prefix_before_commit(journal, monkeypatch):
    class Client:
        def __init__(self):
            self.responses = self
            self.creates = 0
        def create(self, **kwargs):
            self.creates += 1
            def stream():
                yield {"type": "response.created", "response": {"id": "resp_uncertain"}}
                yield {"type": "response.output_text.delta", "delta": "incorrect prefix"}
                raise ConnectionError("disconnect")
            return stream()
        def retrieve(self, response_id, **kwargs):
            return answer("resp_uncertain", "different content")
        def close(self):
            pass
    client = Client()
    monkeypatch.setattr(responses_client, "_client", lambda *args, **kwargs: (client, {}))
    events = list(responses_client.sync_stream(ChatLLM, turn([{"role": "user", "content": "q"}]), CONFIG))
    assert client.creates == 1
    assert store.load_state("s")["head_response_id"] is None
    assert any("不一致" in event for event in events)


def test_meta_and_chat_round_keep_response_audit_then_delete_invalidates(journal, monkeypatch):
    from memory import chat_memory
    monkeypatch.setattr(chat_memory, "HISTORY_ROOT", journal)
    monkeypatch.setattr(store, "_mirror", lambda session, state:
        chat_memory.update_responses_session_meta(session, store.public_state(state, include_ids=True)))
    manager = chat_memory.ChatMemoryManager("s")
    async def run():
        await manager.add_chat_history({"role": "user", "content": "q"})
        request = turn([{"role": "user", "content": "q"}])
        payload, ids, state = prepare(request, CONFIG, False)
        record = store.begin_request("s", "chat_model", payload, CONFIG, ids)
        normalized = responses_client._commit(request, record, answer("resp_persisted"))
        await manager.add_chat_history(normalized["response_record"])
        await manager.add_chat_history({"role": "assistant", "content": "OK"})
        await manager.add_chat_history({"role": "assistant", "done": "[DONE]"})
        meta = await manager.get_session_meta()
        assert meta["responses_state"]["head_response_id"] == "resp_persisted"
        _, rows = manager._load_for_read()
        assert any(event.get("response_id") == "resp_persisted" for row in rows
                   for event in row.get("events") or [])
        await manager.delete_rounds(1)
        assert store.load_state("s")["head_response_id"] is None
    asyncio.run(run())


@pytest.mark.parametrize("protocol", ["responses", "messages"])
def test_private_journal_retries_windows_file_occupation(journal, monkeypatch, protocol):
    from util import atomic_json
    from chat.protocols import native_store
    real_replace = atomic_json.os.replace
    calls = []
    def occupied_then_available(source, target):
        calls.append(target)
        if len(calls) < 3:
            raise PermissionError(5, "file occupied")
        real_replace(source, target)
    monkeypatch.setattr(atomic_json.os, "replace", occupied_then_available)
    monkeypatch.setattr(atomic_json.time, "sleep", lambda seconds: None)
    path = journal / "durable.json"
    (store if protocol == "responses" else native_store)._write(path, {"committed": True})
    assert json.loads(path.read_text(encoding="utf-8")) == {"committed": True}
    assert len(calls) == 3
    assert not list(journal.glob("*.tmp"))


def test_native_input_projection_retains_cipher_and_preserves_raw_metadata():
    from chat.protocols.responses_content import native_input_items
    raw = [{"type": "compaction", "id": "cmp", "encrypted_content": "opaque",
            "created_by": "provider-output-metadata"}]
    assert native_input_items(raw) == [{"type": "compaction", "id": "cmp", "encrypted_content": "opaque"}]
    assert raw[0]["created_by"] == "provider-output-metadata"


def test_staged_native_window_cannot_follow_account_switch(journal):
    commit(turn([{"role": "user", "content": "account A"}]), answer("resp_old"))
    window = store.current_window("s", store.load_state("s"))
    store.stage_rebuild("s", window, source="native")
    payload, _, state = prepare(turn([{"role": "user", "content": "local facts B"}]),
                                dict(CONFIG, apiKey="account-B"), False)
    assert "account A" not in json.dumps(payload)
    assert state["pending_compaction_ref"] is None


def test_stopped_before_tool_execution_requires_explicit_rebuild(journal):
    commit(turn([{"role": "user", "content": "q"}]),
           answer("resp_tools", "", calls=[("call_1", "write_file", "{}")]))
    with pytest.raises(ResponsesProtocolError, match="旧工具不会自动执行"):
        prepare(turn([{"role": "user", "content": "new question"}]), CONFIG, False)
    assert store.load_state("s")["status"] == "recovery_required"


def test_cached_results_missing_from_local_round_are_sent_without_execution(journal):
    ref = commit(turn([{"role": "user", "content": "q"}]),
                 answer("resp_tools", "", calls=[("call_1", "write_file", "{}")]))
    store.tool_started("s", ref, "call_1")
    store.tool_completed("s", ref, "call_1", "already saved")
    payload, _, _ = prepare(turn([{"role": "user", "content": "new question"}]), CONFIG, False)
    assert payload["input"][0] == {"type": "function_call_output", "call_id": "call_1", "output": "already saved"}
    assert payload["input"][1]["role"] == "user"


def test_rebuild_api_blocks_uncertain_native_tools_and_restores_cached_result(journal, monkeypatch):
    from routers import chat_router
    from fastapi import HTTPException
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    import env_manager
    ref = commit(turn([{"role": "user", "content": "q"}]),
                 answer("resp_tools", "", calls=[("call_1", "write_file", "{}")]))
    monkeypatch.setattr(chat_router, "_session_generation_busy", lambda session: False)
    monkeypatch.setattr(chat_router, "get_chat_memory_manager", AsyncMock(return_value=SimpleNamespace()))
    monkeypatch.setattr(chat_router, "resolve_session_model_selection", lambda session: ({}, None))
    monkeypatch.setattr(env_manager, "require_default_chat_config", lambda: CONFIG)
    with pytest.raises(HTTPException, match="未确认"):
        asyncio.run(chat_router.rebuild_responses_context("s", "native"))
    store.tool_started("s", ref, "call_1")
    store.tool_completed("s", ref, "call_1", "saved")
    result = asyncio.run(chat_router.rebuild_responses_context("s", "native"))
    assert result["state"] == "succeed"
    payload, _, _ = prepare(turn([{"role": "user", "content": "next"}]), CONFIG, False)
    assert "previous_response_id" not in payload
    assert any(item.get("type") == "function_call_output" and item["output"] == "saved" for item in payload["input"])
