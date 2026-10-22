import asyncio
import copy
import json
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from factory import task_templates as service
from memory import chat_memory as memory, task_template_store as store, task_cost_store as costs
from routers.task_template_router import TaskTemplate, api_task_template_router


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "ROOT", tmp_path / "templates")
    monkeypatch.setattr(memory, "HISTORY_ROOT", tmp_path / "history")
    monkeypatch.setattr(memory, "LOCK_ROOT", tmp_path / "locks")
    models = {"chat_model": {"ownership_name": "P", "model_name": "Chat", "parameter": {"chat_completions": {"temperature": .4}}, "api_type": "chat_completions", "headers": [{"name": "Authorization", "value": "secret-header"}]}}
    monkeypatch.setattr(service.env_manager, "get_global_model_selection", lambda: copy.deepcopy(models))
    monkeypatch.setattr(memory, "_get_global_model_selection", lambda: copy.deepcopy(models))
    available = {"Chat": "chat-completions", "Messages": "messages", "Responses": "responses"}
    monkeypatch.setattr(service.env_manager, "get_model_config", lambda p, m: {"selected_model_name": m, "apiType": available[m]} if p == "P" and m in available else None)
    monkeypatch.setattr(service.env_manager, "list_available_models", lambda: [{"provider_name": "P", "model_name": m, "api_type": api, "apiKey": "DO_NOT_CAPTURE"} for m, api in available.items()])
    selection = {"S": ["qualified_echo"], "__builtin__": ["run_command"]}
    get_inputs = lambda: (copy.deepcopy(selection), ["S", "Other"], None)
    monkeypatch.setattr(service, "get_global_tool_inputs", get_inputs)
    monkeypatch.setattr(memory, "get_global_tool_inputs", get_inputs)
    monkeypatch.setattr(service, "get_current_dir", lambda: str(tmp_path))
    monkeypatch.setattr(service, "get_persisted_work_dir", lambda: str(tmp_path))
    monkeypatch.setattr(memory, "get_persisted_work_dir", lambda: str(tmp_path))
    monkeypatch.setattr(service.tool_registry, "get_tool_registry_snapshot", lambda: (
        [{"function": {"name": "qualified_echo", "description": "Echo"}}, {"function": {"name": "other_echo"}}],
        {"qualified_echo": "S", "other_echo": "Other"}, {"qualified_echo": "echo", "other_echo": "echo"}, {}))
    app = FastAPI(); app.include_router(api_task_template_router)
    with TestClient(app) as api:
        yield api, tmp_path, available, models, selection


def definition(**changes):
    return TaskTemplate(name="代码审查", prompt="检查当前项目", output_format="按文件列出问题", acceptance="给出验证结果", **changes).model_dump()


def test_crud_revisions_duplicate_names_and_restart(setup):
    api, *_ = setup
    created = api.post("/task_templates", json=definition())
    assert created.status_code == 200
    record = created.json(); identifier = record["id"]
    assert api.get("/task_templates").json()["templates"][0]["name"] == "代码审查"
    assert api.get("/task_templates/" + identifier).json() == record
    assert store.read(identifier) == record
    assert api.post("/task_templates", json=definition()).status_code == 409
    edited = {**definition(), "name": "Review"}
    assert api.put("/task_templates/" + identifier, json={"template": edited, "version": 1}).json()["version"] == 2
    assert api.put("/task_templates/" + identifier, json={"template": edited, "version": 1}).status_code == 409
    assert api.delete("/task_templates/" + identifier + "?version=1").status_code == 409
    assert api.delete("/task_templates/" + identifier + "?version=2").status_code == 200
    assert api.get("/task_templates/" + identifier).status_code == 404


@pytest.mark.parametrize("changes", [{"name": " "}, {"headers": {}}, {"api_key": "secret"},
    {"models": {"wrong_role": {"provider": "P", "model": "Chat"}}},
    {"models": {"chat_model": {"provider": "P", "model": "Chat", "api_key": "secret"}}},
    {"budget": {"cost": -1}}, {"budget": {"tokens": 1.5}}, {"budget": {"tokens": True}},
    {"budget": {"foo": 1}}, {"tools": {"S": "echo"}}, {"work_dir": {"mode": "arbitrary"}}, {"prompt": "x" * 32769}])
def test_schema_rejects_credentials_bad_types_and_limits(setup, changes):
    api, *_ = setup
    payload = {**definition(), **changes}
    assert api.post("/task_templates/validate", json=payload).status_code == 422
    assert api.get("/task_templates").json()["templates"] == []


def test_capture_only_references_and_canonical_original_tool_names(setup):
    api, tmp_path, *_ = setup
    payload = api.get("/task_templates/options").json()
    assert {m["api_type"] for m in payload["models"]} == {"chat_completions", "messages", "responses"}
    capture = payload["capture"]
    assert capture["models"]["chat_model"] == {"provider": "P", "model": "Chat"}
    assert capture["tools"]["S"] == ["echo"]
    assert "secret-header" not in json.dumps(payload) and "DO_NOT_CAPTURE" not in json.dumps(payload)
    assert capture["work_dir"] == {"mode": "fixed", "path": str(tmp_path.resolve())}
    assert not list((tmp_path / "history").glob("*_chat.jsonl"))


def test_preview_never_writes_a_session_and_resolves_all_fields(setup):
    api, tmp_path, *_ = setup
    reply = api.post("/task_templates/preview", json={"template": definition(tools={"Other": ["echo"]}, budget={"tokens": 5000, "model_calls": 4})})
    assert reply.status_code == 200, reply.text
    plan = reply.json()["summary"]
    assert plan["tools"] == [{"server": "Other", "name": "echo"}]
    assert plan["budget"]["tokens"] == 5000 and plan["budget"]["cost"] == 0
    assert plan["prompt"] == "检查当前项目\n\n## 输出格式\n按文件列出问题\n\n## 验收要求\n给出验证结果"
    assert not list((tmp_path / "history").glob("*_chat.jsonl"))


@pytest.mark.parametrize("model", ["Chat", "Messages", "Responses"])
def test_new_task_uses_normal_session_configuration_with_any_api(setup, model):
    api, tmp_path, *_ = setup
    data = definition(models={"chat_model": {"provider": "P", "model": model}}, tools={}, budget={"tokens": 1000})
    preview = api.post("/task_templates/preview", json={"template": data}).json()
    request = {"template": data, "request_id": str(uuid.uuid4()), "expected_fingerprint": preview["fingerprint"]}
    result = api.post("/task_templates/start", json=request)
    assert result.status_code == 200, result.text
    sid = result.json()["session_id"]
    path = tmp_path / "history" / (sid + "_chat.jsonl")
    rows = path.read_text(encoding="utf-8").splitlines()
    assert len(rows) == 1, "新建不产生聊天轮次或模型调用"
    meta = json.loads(rows[0])["_meta"]
    assert meta["model_selection"]["chat_model"]["model_name"] == model
    assert meta["tool_selection"] == {"__empty__": []}
    assert meta["config_snapshot_at"] and meta["task_template"]["initial_prompt"] == result.json()["summary"]["prompt"]
    assert costs.config(sid)["limits"]["tokens"] == 1000
    manager = memory.ChatMemoryManager(sid)
    assert asyncio.run(manager.ensure_session_config_snapshot())[0] is False
    assert memory.resolve_session_tool_selection(sid) == ({}, None)
    assert service.env_manager.get_global_model_selection()["chat_model"]["model_name"] == "Chat"


def test_idempotency_does_not_reset_a_subsequently_edited_budget(setup):
    api, tmp_path, *_ = setup
    data = definition(budget={"tokens": 1000})
    plan, _ = service.prepare(data)
    request = uuid.uuid4()
    first = service.start(data, None, request, plan["fingerprint"])
    costs.save_config(None, first["session_id"])
    second = service.start(data, None, request, plan["fingerprint"])
    assert second["reused"] and second["session_id"] == first["session_id"]
    assert costs.config(first["session_id"])["override"] is None
    assert len(list((tmp_path / "history").glob("*_chat.jsonl"))) == 1
    with pytest.raises(ValueError, match="其他模板"):
        service.start({**data, "prompt": "different"}, None, request, plan["fingerprint"])


def test_concurrent_start_creates_only_one_session(setup):
    _api, tmp_path, *_ = setup
    data = definition(); plan, _ = service.prepare(data); request = uuid.uuid4()
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: service.start(data, None, request, plan["fingerprint"]), range(2)))
    assert results[0]["session_id"] == results[1]["session_id"]
    assert sum(not result["reused"] for result in results) == 1


def test_early_or_stale_dependencies_block_without_writing(setup):
    api, tmp_path, available, models, _selection = setup
    data = definition()
    plan, _ = service.prepare(data)
    models["chat_model"]["parameter"] = {"chat_completions": {"temperature": .7}}
    with pytest.raises(ValueError, match="预览后已变化"):
        service.start(data, None, uuid.uuid4(), plan["fingerprint"])
    for template in (definition(models={"chat_model": {"provider": "P", "model": "Removed"}}),
                     definition(tools={"Removed": ["echo"]}), definition(tools={"S": ["removed"]})):
        assert api.post("/task_templates/preview", json={"template": template}).status_code == 400
    assert not list((tmp_path / "history").glob("*_chat.jsonl"))


def test_path_rules_existing_relative_fixed_and_traversal(setup):
    api, tmp_path, *_ = setup
    (tmp_path / "project").mkdir()
    good = [{"mode": "inherit", "path": ""}, {"mode": "fixed", "path": str(tmp_path / "project")}, {"mode": "relative", "path": "project"}]
    bad = [{"mode": "fixed", "path": "project"}, {"mode": "relative", "path": "../outside"},
           {"mode": "relative", "path": "C:\\outside"}, {"mode": "relative", "path": "missing"}]
    for rule in good:
        assert api.post("/task_templates/preview", json={"template": definition(work_dir=rule)}).status_code == 200
    for rule in bad:
        assert api.post("/task_templates/preview", json={"template": definition(work_dir=rule)}).status_code == 400
    assert not (tmp_path / "missing").exists()


def test_inherit_source_session_without_modifying_it(setup):
    api, tmp_path, *_ = setup
    directory = tmp_path / "source-dir"; directory.mkdir()
    manager = memory.ChatMemoryManager("source")
    asyncio.run(manager.update_session_work_dir(str(directory)))
    asyncio.run(manager.update_session_tool_selection({"Other": ["other_echo"]}))
    costs.save_config({"model_calls": 2}, "source")
    before = (tmp_path / "history" / "source_chat.jsonl").read_bytes()
    preview = api.post("/task_templates/preview", json={"template": definition(), "source_session_id": "source"}).json()
    assert preview["summary"]["tools"] == [{"server": "Other", "name": "echo"}]
    assert preview["summary"]["budget"]["model_calls"] == 2
    assert preview["summary"]["work_dir"] == str(directory)
    assert (tmp_path / "history" / "source_chat.jsonl").read_bytes() == before


def test_currency_mismatch_and_validation_import_no_side_effects(setup):
    api, tmp_path, *_ = setup
    data = definition(budget={"cost": 1}, budget_currency="USD")
    assert api.post("/task_templates/validate", json=data).status_code == 200
    assert api.post("/task_templates/preview", json={"template": data}).status_code == 400
    assert api.get("/task_templates").json()["templates"] == []


def test_incompatible_vision_tool_is_reported_before_creating_session(setup):
    api, tmp_path, *_ = setup
    data = definition(tools={"__builtin__": ["read_media"]})
    result = api.post("/task_templates/preview", json={"template": data})
    assert result.status_code == 400 and "不支持视觉" in result.text
    assert not list((tmp_path / "history").glob("*_chat.jsonl"))


def test_history_write_failure_rolls_back_budget(setup, monkeypatch):
    _api, tmp_path, *_ = setup
    data = definition(budget={"tokens": 1000}); plan, _ = service.prepare(data); request = uuid.uuid4()
    def failure(*args):
        raise OSError("disk failure")
    monkeypatch.setattr(memory, "_write_meta_and_entries", failure)
    with pytest.raises(OSError):
        service.start(data, None, request, plan["fingerprint"])
    assert not list((tmp_path / "history").glob("*_chat.jsonl"))
    assert costs.config("task-" + request.hex)["override"] is None


def test_crash_between_file_and_budget_commit_recovers(setup):
    _api, _tmp_path, *_ = setup
    data = definition(budget={"tokens": 1000}); plan, _ = service.prepare(data); request = uuid.uuid4()
    first = service.start(data, None, request, plan["fingerprint"])
    with costs.database() as db:
        db.execute("DELETE FROM settings WHERE scope IN (?,?)", ("session:" + first["session_id"], "template:" + first["session_id"]))
    assert service.start(data, None, request, plan["fingerprint"])["reused"]
    assert costs.config(first["session_id"])["limits"]["tokens"] == 1000


def test_template_draft_is_consumed_once_even_when_all_rounds_are_deleted(setup):
    _api, *_ = setup
    data = definition(); plan, _ = service.prepare(data)
    result = service.start(data, None, uuid.uuid4(), plan["fingerprint"])
    meta = memory.read_session_meta_value(result["session_id"], "task_template")
    assert not meta.get("draft_consumed")
    base_meta = {"task_template": meta}
    entry = {"event": "chat_round", "question": "已发送", "events": [{"role": "user", "content": "已发送"}], "usage": {}}
    after_send = memory._recompute_meta_from_entries(result["session_id"], base_meta, [entry])
    after_delete = memory._recompute_meta_from_entries(result["session_id"], after_send, [])
    assert after_delete["task_template"]["draft_consumed"] is True


def test_retry_does_not_recreate_a_deleted_task(setup):
    _api, tmp_path, *_ = setup
    data = definition(); plan, _ = service.prepare(data); request = uuid.uuid4()
    result = service.start(data, None, request, plan["fingerprint"])
    (tmp_path / "history" / (result["session_id"] + "_chat.jsonl")).unlink()
    with pytest.raises(ValueError, match="已删除"):
        service.start(data, None, request, plan["fingerprint"])
    assert not list((tmp_path / "history").glob("*_chat.jsonl"))
