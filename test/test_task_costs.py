import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from config import ChatLLMRequest
from memory import permission_store, task_cost_store as store
from factory.agent_runtime.task_costs import BudgetTracker, ModelMeter, CURRENT, tracked_call
from routers.task_cost_router import api_task_cost_router


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(permission_store, "ROOT", tmp_path / "state")


def meter(role="main", task="t", model="model"):
    store.create_task(task, "s")
    return ModelMeter(BudgetTracker(task, "s"), ChatLLMRequest(messages=[{"role": "user", "content": "hello"}], usage_role=role),
                      {"selected_provider_name": "provider", "selected_model_name": model})


def test_limits_inherit_validation_and_restart():
    assert all(value == 0 for value in store.config()["limits"].values())
    store.save_config({"tokens": 100})
    store.save_config({"model_calls": 2}, "s")
    assert store.config("s")["limits"]["tokens"] == 0
    assert store.config("other")["limits"]["tokens"] == 100
    store.save_config(None, "s")
    assert store.config("s")["limits"]["tokens"] == 100
    for invalid in ({"tokens": -1}, {"tokens": True}, {"tokens": 1.2}, {"cost": float("nan")}, {"cost": float("inf")}, {"typo": 1}):
        with pytest.raises(ValueError): store.save_config(invalid)


def test_roles_snapshots_prices_and_idempotent_settlement():
    key = json.dumps(["provider", "model"], ensure_ascii=False)
    store.save_pricing({"currency": "CNY", "models": {key: {"input": 1, "output": 4, "cached_input": .1}}})
    for role in store.ROLES:
        m = meter(role)
        m.start_sync()
        m.feed({"id": "same-upstream-id", "usage": {"prompt_tokens": 80, "completion_tokens": 2}})
        final = {"id": "same-upstream-id", "usage": {"prompt_tokens": 100, "completion_tokens": 10, "prompt_tokens_details": {"cached_tokens": 40}}}
        m.feed(final); m.feed(final); m.settle(); m.settle()
    result = store.session_summary("s")
    assert result["usage"]["total_tokens"] == 440
    assert set(result["tasks"][0]["roles"]) == set(store.ROLES)
    assert result["cost"] == pytest.approx(4 * (60 + 4 + 40) / 1_000_000)
    assert result["unknown_price_calls"] == 0
    assert result["estimated_calls"] == 0
    again = store.session_summary("s")
    assert again["usage"] == result["usage"] and again["cost"] == result["cost"]


def test_concurrent_model_budget_pause_extend_continue():
    store.save_config({"model_calls": 1})
    async def run():
        first, second = meter(), meter("sub_agent")
        await first.start(); first.feed({"usage": {"prompt_tokens": 5, "completion_tokens": 5}}); first.settle()
        pending = asyncio.create_task(second.start())
        await asyncio.sleep(.04)
        data = store.status("t")
        assert data["state"] == "paused" and data["model_calls"] == 1
        assert not pending.done()
        with pytest.raises(ValueError): store.continue_task("t", "other", data["version"], {})
        with pytest.raises(ValueError): store.continue_task("t", "s", data["version"] + 1, {})
        store.continue_task("t", "s", data["version"], {"model_calls": 2})
        await asyncio.wait_for(pending, 2)
        second.settle()
        assert store.status("t")["model_calls"] == 2
    asyncio.run(run())


def test_atomic_parallel_reservations_and_tools_replay():
    store.save_config({"model_calls": 1})
    meter()
    calls = [meter().call() for _ in range(8)]
    with ThreadPoolExecutor(8) as pool:
        allowed = list(pool.map(lambda c: store.reserve("t", call=c)["allowed"], calls))
    assert sum(allowed) == 1
    task = store.status("t")
    store.continue_task("t", "s", task["version"], {"tool_calls": 1})
    assert store.reserve("t", tools=["main:a", "main:b"])["allowed"]
    assert store.reserve("t", tools=["main:a", "main:b"])["allowed"]
    assert store.status("t")["tool_calls"] == 2
    assert not store.reserve("t", tools=["child:c"])["allowed"]
    store.finish("t", "cancelled")
    assert not store.reserve("t", tools=["main:a"])["allowed"]


def test_missing_price_can_be_supplied_while_paused():
    store.save_config({"cost": .5})
    async def run():
        m = meter()
        pending = asyncio.create_task(m.start())
        await asyncio.sleep(.03)
        data = store.status("t")
        assert data["reason"] == "missing_price"
        store.save_pricing({"currency": "CNY", "models": {m.model: {"input": 1, "output": 2}}})
        store.continue_task("t", "s", data["version"], {"cost": .5})
        await asyncio.wait_for(pending, 2)
        m.feed({"usage": {"prompt_tokens": 1000, "completion_tokens": 1000}}); m.settle()
        assert store.status("t")["cost"] == pytest.approx(.003)
    asyncio.run(run())


def test_token_boundary_late_title_and_unknown_usage():
    store.save_config({"tokens": 10})
    first = meter(); first.start_sync(); first.feed({"usage": {"prompt_tokens": 8, "completion_tokens": 4}}); first.settle()
    assert not store.reserve("t", call=meter("sub_agent").call())["allowed"]
    data = store.status("t")
    store.continue_task("t", "s", data["version"], {})
    store.finish("t", "completed")
    title = meter("title"); title.start_sync(); title.feed({"content": "title"}); title.settle()
    assert store.status("t")["state"] == "completed"
    assert store.status("t")["estimated_calls"] == 1


def test_old_history_baseline_adds_children_once():
    event = {"event": "sub_agent", "phase": "done", "agent_id": "a", "usage_total": {"prompt_tokens": 10, "completion_tokens": 2}}
    text = json.dumps({"event": "chat_round", "events": [event, event]})
    baseline = store.legacy_baseline(text, {"usage": {"total_tokens": 100, "prompt_tokens": 90, "completion_tokens": 10}})
    store.create_task("t", "s", baseline=baseline)
    store.create_task("next", "s", baseline={"total_tokens": 9999})
    assert store.session_summary("s")["usage"]["total_tokens"] == 112
    store.delete_session("s")
    assert not store.session_summary("s")["tracked"]


@pytest.mark.parametrize("api", ["chat-completions", "messages", "responses"])
def test_shared_stream_wrapper_counts_once_for_each_protocol(api):
    async def run():
        m = meter()
        token = CURRENT.set(m.tracker)
        async def upstream():
            yield 'data: {"id":"x","content":"ok"}\n\n'
            yield 'data: {"id":"x","usage":{"input_tokens":10,"output_tokens":3}}\n\n'
            yield 'data: {"id":"x","usage":{"input_tokens":10,"output_tokens":3},"finish_reason":"stop"}\n\n'
            yield 'data: [DONE]\n\n'
        try:
            request = ChatLLMRequest(messages=[{"role": "user", "content": "hello"}])
            output = [c async for c in tracked_call(upstream, request, True, None, {"apiType": api})]
            assert len(output) == 4
            assert store.status("t")["usage"]["total_tokens"] == 13
            assert store.status("t")["model_calls"] == 1
        finally: CURRENT.reset(token)
    asyncio.run(run())


def test_api_cross_session_and_stale_decisions(monkeypatch):
    import env_manager
    monkeypatch.setattr(env_manager, "list_available_models", lambda: [{"provider_name": "p", "model_name": "m", "apiKey": "NEVER_RETURN"}])
    app = FastAPI(); app.include_router(api_task_cost_router)
    client = TestClient(app)
    assert "NEVER_RETURN" not in client.get("/task_costs/config").text
    assert client.post("/task_costs/config", json={"limits": {"tokens": -1}}).status_code == 400
    store.save_config({"model_calls": 1}); first = meter(); first.start_sync(); first.settle()
    store.reserve("t", call=meter().call()); data = store.status("t")
    body = {"session_id": "s", "version": data["version"], "limits": {}}
    assert client.post("/task_costs/t/continue", json={**body, "session_id": "other"}).status_code == 409
    assert client.post("/task_costs/t/continue", json=body).status_code == 200
    assert client.post("/task_costs/t/continue", json=body).status_code == 409


def test_later_child_does_not_inherit_previous_budget_pause_time(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(store.time, "time", lambda: clock[0])
    store.save_config({"model_calls": 1})
    first = meter(); first.start_sync()
    budget = first.tracker
    budget.register_agent("old-child")
    store.reserve("t", call=meter().call())
    clock[0] = 110
    paused = store.status("t")
    store.continue_task("t", "s", paused["version"], {"model_calls": 2})
    budget.register_agent("new-child")
    assert budget.paused_seconds("old-child") == 10
    assert budget.paused_seconds("new-child") == 0
    store.reserve("t", call=meter().call())
    store.reserve("t", call=meter().call())
    clock[0] = 120
    assert budget.paused_seconds("old-child") == 20
    assert budget.paused_seconds("new-child") == 10


@pytest.mark.parametrize("usage", [
    {"total_tokens": None}, {"total_tokens": True}, {"total_tokens": float("nan")},
    {"messages_usage": {}, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
])
def test_missing_or_invalid_usage_is_not_treated_as_free(usage):
    store.save_config({"tokens": 1})
    m = meter(); m.start_sync(); m.feed({"usage": usage}); m.settle()
    task = store.status("t")
    assert task["estimated_calls"] == 1
    assert task["usage"]["total_tokens"] > 0
    assert not store.reserve("t", tools=["main:next"])["allowed"]
