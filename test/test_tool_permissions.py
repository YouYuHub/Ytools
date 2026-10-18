import asyncio
import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from memory import permission_store as store
from factory.agent_runtime.permission_policy import plan_tool, inside
from factory.agent_runtime.permission_runtime import PermissionRuntime, notify, guard_path, run_with_active_timeout
from routers.tool_approval_router import api_permission_router
from routers import permissions_auth


@pytest.fixture(autouse=True)
def isolated_permissions(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "ROOT", tmp_path / "control")
    monkeypatch.setattr(permissions_auth, "MEDIA_TICKET_KEY", "test-media-key")


def context(root, mode="ask"):
    return {"session": "test", "root": str(root.resolve()), "mode": mode}


def call(name, args=None, call_id="one"):
    return (1, {"id": call_id, "function": {"name": name, "arguments": json.dumps(args or {})}}, name, args or {})


def test_global_and_independent_session_policy_persist():
    assert store.policy("a")["mode"] == "ask"
    store.set_policy("full_control")
    store.set_policy("ask", "a")
    assert store.policy("a")["mode"] == "ask"
    assert store.policy("b")["mode"] == "full_control"
    store.set_policy(None, "a")
    assert store.policy("a")["mode"] == "full_control"
    store.set_policy("ask")
    store.set_policy("full_control", "b")
    assert store.policy("b")["mode"] == "full_control"


@pytest.mark.parametrize("tool,args,server,required", [
    ("run_command", {"command": "echo hi"}, "__builtin__", True),
    ("run_command", {"action": "poll", "job_id": "j"}, "__builtin__", False),
    ("poll_command", {"job_id": "j"}, "__builtin__", False),
    ("stop_command", {"job_id": "j"}, "__builtin__", False),
    ("read_file", {"path": "a.txt"}, "__builtin__", False),
    ("read_file", {"path": "../outside.txt"}, "__builtin__", True),
    ("search_files", {"dir_path": "../"}, "__builtin__", True),
    ("read_media", {"references": ["https://example.com/a.png"]}, "__builtin__", True),
    ("read_media", {"references": ["media://upload.png"]}, "__builtin__", False),
    ("todo_write", {}, "__builtin__", False),
    ("ask_user", {}, "__builtin__", False),
    ("sub_agent", {"task": "work"}, "__builtin__", False),
    ("read_file", {"path": "a.txt"}, "mcp-server", True),
])
def test_permission_matrix(tmp_path, tool, args, server, required):
    assert plan_tool(tool, args, context(tmp_path), server=server)["needs_approval"] is required
    assert not plan_tool(tool, args, context(tmp_path, "full_control"), server=server)["needs_approval"]


def test_prefix_and_link_escape(tmp_path):
    root = tmp_path / "work"; root.mkdir()
    other = tmp_path / "work-other"; other.mkdir()
    assert not inside(other, root)
    try:
        (root / "link").symlink_to(other, target_is_directory=True)
    except OSError:
        pytest.skip("Windows symlink privilege unavailable")
    assert plan_tool("write_file", {"path": "link/new.txt"}, context(root))["needs_approval"]


def test_approve_once_and_cache_result(tmp_path):
    async def run():
        events = []
        async def emit(event):
            events.append(event)
            approval = event["approval"]
            if event["phase"] == "requested":
                assert store.execution(approval["task_id"], "main", "one") is None
                store.decide(approval["id"], "s", approval["epoch"], "approve")
                notify(approval["id"])
        runtime = PermissionRuntime("s", {}, emit, lambda: False)
        runtime.save("main", {"messages": []})
        calls = [call("run_command", {"command": "echo hi"})]
        allowed, denied = await runtime.authorize(calls, {"run_command": "__builtin__"})
        assert len(allowed) == 1 and not denied
        runtime.begin_call("main", "one")
        with pytest.raises(ValueError):
            store.claim_execution(runtime.id, runtime.epoch, "main", "one")
        runtime.finish_call("main", "one", {"stdout": "hi"})
        again, cached = await runtime.authorize(calls, {"run_command": "__builtin__"})
        assert not again and cached[0]["result"] == {"stdout": "hi"}
        assert len([event for event in events if event["phase"] == "requested"]) == 1
        runtime.close("completed")
    asyncio.run(run())


def test_denial_never_claims_execution_and_preserves_call_id():
    async def run():
        async def emit(event):
            if event["phase"] == "requested":
                a = event["approval"]; store.decide(a["id"], "s", a["epoch"], "deny"); notify(a["id"])
        runtime = PermissionRuntime("s", {}, emit, lambda: False)
        allowed, refused = await runtime.authorize([call("mcp", {"path": "elsewhere"})], {"mcp": "server"})
        assert not allowed and refused[0]["tool_call"]["id"] == "one"
        assert refused[0]["result"]["executed"] is False
        assert store.execution(runtime.id, "main", "one")["state"] == "completed"
        runtime.close("completed")
    asyncio.run(run())


def test_checkpoint_and_epoch_recover_but_old_approval_cannot_execute():
    original = store.create_task("s", {"mode": "ask", "root": str(Path.cwd()), "request": {}})
    store.checkpoint(original["id"], "main", {"messages": [{"role": "user", "content": "original"}]})
    a = store.request_approval(original["id"], "main", "one", original["epoch"], {"args": {"command": "echo hi"}})
    with store.database() as db:
        db.execute("UPDATE tasks SET owner=-1 WHERE id=?", (original["id"],))
    assert store.pending("s")[0]["task_state"] == "suspended"
    resumed = store.resume(original["id"])
    assert resumed["epoch"] != original["epoch"]
    assert store.load_checkpoint(original["id"])["messages"][0]["content"] == "original"
    with pytest.raises(ValueError):
        store.decide(a["id"], "s", a["epoch"], "approve")


def test_unknown_execution_is_not_replayed():
    original = store.create_task("s", {})
    store.checkpoint(original["id"], "main", {})
    store.claim_execution(original["id"], original["epoch"], "main", "one")
    store.set_task_state(original["id"], "suspended")
    with store.database() as db:
        db.execute("UPDATE tasks SET owner=-1 WHERE id=?", (original["id"],))
    with pytest.raises(ValueError, match="未知"):
        store.resume(original["id"])


def test_permission_api_needs_no_pairing_and_keeps_decision_validation():
    app = FastAPI(); app.include_router(api_permission_router)
    with TestClient(app) as client:
        assert client.post("/chat_config/permissions", json={"mode": "full_control"}).status_code == 200
        assert client.post("/chat_config/permissions", json={"mode": "bad"}).status_code == 400
        assert client.get("/tool_approvals/pending?session_id=s").json()["approvals"] == []
        params = {"session_id": "s", "source": "https://example.invalid/demo.png"}
        response = client.get("/permissions/media", params=params, follow_redirects=False)
        assert response.status_code == 302 and response.headers["location"] == params["source"]
        client.post("/chat_config/permissions", json={"mode": "ask"})
        assert client.get("/permissions/media", params=params, follow_redirects=False).status_code == 403
        assert client.post("/tool_approvals/no-such-id/decision", json={"session_id": "s", "epoch": "x", "decision": "approve"}).status_code == 404


def test_resource_ticket_bound_to_source_session_and_boot():
    grant = permissions_auth.ticket("s", "C:/data/a.png")
    assert permissions_auth.verify_ticket(grant, "s", "C:/data/a.png")
    assert not permissions_auth.verify_ticket(grant, "other", "C:/data/a.png")
    assert not permissions_auth.verify_ticket(grant, "s", "C:/data/b.png")
    with patch.object(permissions_auth, "MEDIA_TICKET_KEY", "new-boot"):
        assert not permissions_auth.verify_ticket(grant, "s", "C:/data/a.png")


def test_mcp_domain_permission_arguments_do_not_set_application_policy(tmp_path):
    plan = plan_tool("remote_acl", {"permission_mode": "owner", "_permission_scope": "a"}, context(tmp_path), server="remote")
    assert plan["needs_approval"]
    assert plan["args"]["permission_mode"] == "owner"
    assert store.policy("test")["mode"] == "ask"
