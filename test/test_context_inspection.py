"""Context panel describes local history without mutating or executing it."""
import asyncio
import json
import threading

import pytest

import env_manager
from chat.protocols import responses_store
from factory.agent_runtime import context_compaction as compact
from memory import chat_memory as cm, file_memory
from memory.context_inspection import inspect_configuration
from routers import chat_router as router


@pytest.fixture
def context_env(tmp_path, monkeypatch):
    root = tmp_path / "history"
    root.mkdir()
    monkeypatch.setattr(cm, "HISTORY_ROOT", root)
    monkeypatch.setattr(cm, "LOCK_ROOT", root / "lock")
    monkeypatch.setattr(file_memory, "HISTORY_ROOT", root)
    monkeypatch.setattr(cm, "_session_managers", {})
    monkeypatch.setattr(cm, "_resolve_model_max_input_tokens", lambda **kw: 628000)
    config = {"selected_provider_name": "test", "selected_model_id": "model", "selected_model_name": "model",
        "apiType": "chat_completions", "apiKey": "SECRET_KEY", "_custom_headers": [{"name": "X-Key", "value": "SECRET_HEADER"}]}
    monkeypatch.setattr(env_manager, "require_default_chat_config", lambda: config)
    monkeypatch.setattr(router, "resolve_session_model_selection", lambda *args: ({}, False))
    monkeypatch.setattr(router, "resolve_session_work_dir", lambda *args: (str(root), False))
    monkeypatch.setattr(router, "build_runtime_system_text", lambda **kw: "runtime prompt")
    settings = compact.ContextCompactionSettings(.8, .15, 1.5, 3, target_tokens=60000)
    monkeypatch.setattr(compact, "load_context_compaction_settings", lambda: settings)
    monkeypatch.setattr(compact, "resolve_effective_window", lambda *args: 200000)
    monkeypatch.setattr(compact, "resolve_effective_summary_budget", lambda *args: 36000)
    return root, config


async def seed(count=5):
    manager = cm.ChatMemoryManager("inspect")
    for i in range(1, count + 1):
        for message in [{"role": "user", "content": f"Q{i}"},
            {"role": "assistant", "content": f"A{i}"}, {"role": "assistant", "done": "[DONE]"}]:
            await manager.add_chat_history(message)
    await manager.update_context_summary({"source_round_count": 3, "blocks": [
        {"summary": "Valid summary", "round_start": 1, "round_end": 3}]})
    return manager


async def inspect(offset=0, limit=40):
    response = await router.get_chat_context_token_stats(session_id="inspect", inspection=True,
        include_tools=True, tool_names=["read_file"], max_rounds=0, round_offset=offset, round_limit=limit)
    return json.loads(response.body)


def test_round_coverage_supplements_pagination_and_no_writes(context_env):
    async def run():
        manager = await seed()
        meta, entries = manager._load_for_read()
        meta["context_summary_raw_rounds"] = [2]
        cm._write_meta_and_entries(manager._file_path, meta, entries)
        before = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in context_env[0].rglob("*") if p.is_file()}
        paths = set(context_env[0].rglob("*"))
        data = await inspect(limit=3)
        rows = data["inspection"]["round_page"]
        assert [(r["round"], r["representation"]) for r in rows["items"]] == [(5, "raw"), (4, "raw"), (3, "summary")]
        assert rows["has_more"]
        older = await inspect(offset=3, limit=3)
        assert older["inspection"]["round_page"]["items"][0]["representation"] == "supplement"
        assert older["inspection"]["round_page"]["items"][1]["question_index"]
        assert data["inspection"]["policy"]["trigger_threshold"] == 160000
        assert data["tool_definition_tokens"] > 0
        assert data["round_tokens"] == []
        assert set(context_env[0].rglob("*")) == paths
        assert all((p.stat().st_mtime_ns, p.read_bytes()) == v for p, v in before.items())
        assert cm._session_managers == {}
        assert "SECRET_KEY" not in str(data) and "SECRET_HEADER" not in str(data)
    asyncio.run(run())


def test_pending_checkpoint_and_dirty_summary_are_visible_without_repair(context_env):
    async def run():
        manager = await seed()
        await manager.delete_rounds(2, mode="single")
        await manager.add_chat_history({"role": "user", "content": "live question"})
        data = await inspect()
        info = data["inspection"]
        assert info["repair_required"] and info["pending_round"]
        assert info["summary_blocks"][0]["repair_required"]
        assert info["round_page"]["items"][0]["representation"] == "active"
        assert any(r["representation"] == "repair_pending" for r in info["round_page"]["items"])
        assert "Valid summary" not in str(info["summary_blocks"])
        assert manager.get_summary_repair_snapshot() is not None
    asyncio.run(run())


def test_detail_work_runs_off_event_loop_thread(context_env, monkeypatch):
    main = threading.get_ident()
    seen = []
    original = cm.ChatMemoryManager.get_context_token_stats
    async def traced(self, *args, **kwargs):
        seen.append(threading.get_ident())
        return await original(self, *args, **kwargs)
    monkeypatch.setattr(cm.ChatMemoryManager, "get_context_token_stats", traced)
    async def run():
        await seed()
        await inspect()
        assert seen and all(t != main for t in seen)
    asyncio.run(run())


def test_unknown_session_does_not_create_files(context_env):
    before = set(context_env[0].rglob("*"))
    response = asyncio.run(router.get_chat_context_token_stats(session_id="missing", inspection=True))
    assert json.loads(response.body)["exists"] is False
    assert set(context_env[0].rglob("*")) == before


@pytest.mark.parametrize("protocol,mode", [("chat_completions", "stateless"), ("messages", "stateless"),
    ("responses", "stateless"), ("responses", "stateful")])
def test_protocol_mode_and_private_response_state(context_env, protocol, mode):
    cfg = context_env[1]
    cfg.update(apiType=protocol, responsesMode=mode)
    state = responses_store.load_state("inspect")
    record_id = "a" * 32
    state.update(head_response_id="PRIVATE_RESPONSE_ID", head_record_id=record_id, status="ready",
        source_key=responses_store.source_key(cfg))
    responses_store._write(responses_store._state_path("inspect", "chat_model"), state)
    responses_store._write(responses_store._record_path("inspect", record_id), {
        "session_id": "inspect", "response": {"usage": {"input_tokens": 1000, "output_tokens": 50},
            "output": [{"type": "message", "content": "PRIVATE_PROVIDER_PAYLOAD"}]}})
    info = inspect_configuration("inspect", cfg, {})
    assert info["model"]["protocol"] == protocol and info["model"]["mode"] == mode
    assert info["remote"]["applicable"] == (protocol == "responses" and mode == "stateful")
    if info["remote"]["applicable"]:
        assert info["remote"]["chain_tokens_estimate"] == 1050
        assert info["remote"]["last_usage"]["input_tokens"] == 1000
    text = json.dumps(info)
    for private in ["PRIVATE_RESPONSE_ID", "PRIVATE_PROVIDER_PAYLOAD", "SECRET_KEY", "SECRET_HEADER"]:
        assert private not in text


def test_model_change_and_compaction_ciphertext_do_not_get_fake_token_count(context_env):
    cfg = context_env[1]
    cfg.update(apiType="responses")
    state = responses_store.load_state("inspect")
    state.update(head_record_id="b" * 32, head_response_id="r", source_key="different-model")
    responses_store._write(responses_store._state_path("inspect", "chat_model"), state)
    responses_store._write(responses_store._record_path("inspect", "b" * 32), {
        "session_id": "inspect", "response": {"usage": {"input_tokens": 100},
            "output": [{"type": "compaction", "encrypted_content": "x" * 10000}]}})
    info = inspect_configuration("inspect", cfg, {})
    assert info["remote"]["compatible"] is False
    assert "chain_tokens_estimate" not in info["remote"]
    assert "encrypted_content" not in str(info)


def test_compaction_records_and_summary_previews_are_bounded(context_env):
    async def run():
        manager = await seed()
        await manager.update_context_summary({"source_round_count": 3, "summary": "z" * 20000})
        for i in range(18):
            await manager.add_context_compaction_event({"event": "context_compaction", "scope": "session",
                "phase": "done", "trigger_reason": "history_mutation", "before_tokens": 10000,
                "after_tokens": 100, "context_summary": "TOOL_OUTPUT_SHOULD_NOT_BE_SENT", "summary_text": "summary"})
        data = await inspect()
        info = data["inspection"]
        assert len(info["recent_compactions"]) == 12
        assert info["recent_compactions"][0]["trigger_reason"] == "history_mutation"
        assert "TOOL_OUTPUT_SHOULD_NOT_BE_SENT" not in str(info)
        assert info["summary_blocks"][0]["preview"]["truncated"]
        assert len(info["summary_blocks"][0]["preview"]["text"]) == 6000
    asyncio.run(run())


def test_http_inspection_contract_and_paging_validation(context_env):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    asyncio.run(seed())
    app = FastAPI()
    app.include_router(router.api_chat_router)
    with TestClient(app) as client:
        data = client.get("/chat_context/token_stats", params={"session_id": "inspect", "inspection": "true",
            "round_offset": 1, "round_limit": 2}).json()
        assert len(data["inspection"]["round_page"]["items"]) == 2
        assert data["inspection"]["round_page"]["items"][0]["round"] == 4
        assert client.get("/chat_context/token_stats", params={"inspection": "true", "round_limit": 101}).status_code == 422
        assert client.get("/chat_context/token_stats", params={"inspection": "true", "round_offset": -1}).status_code == 422
