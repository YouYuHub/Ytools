"""Permission snapshots merge per imported session, never replace local control."""
import asyncio
import io
import json
import sqlite3
import zipfile

import pytest

from memory import chat_memory as cm, file_memory as fm, permission_store as store


@pytest.fixture
def history(tmp_path, monkeypatch):
    root = tmp_path / "history"
    monkeypatch.setattr(cm, "HISTORY_ROOT", root)
    monkeypatch.setattr(cm, "LOCK_ROOT", root / "lock")
    monkeypatch.setattr(cm, "_session_managers", {})
    monkeypatch.setattr(fm, "HISTORY_ROOT", root / "session_files")
    monkeypatch.setattr(store, "ROOT", tmp_path / "sender-state")
    return root


def write_history(sid, *, title="shared", upload_id="", extra_meta=None, extra_events=()):
    meta = {"title": title, "upload_id": upload_id, **(extra_meta or {})}
    entry = {"event": "chat_round", "question": "question", "status": "done", "events": [
        {"role": "user", "content": "question"}, *extra_events,
        {"role": "assistant", "content": "answer"}, {"role": "assistant", "done": "[DONE]"}],
        "completion_count": 1, "usage_total": {}}
    path = cm._get_chat_history_file(sid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"_meta": meta}) + "\n" + json.dumps(entry) + "\n", encoding="utf-8")
    return path


def receiver(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "ROOT", tmp_path / "receiver-state")
    store.set_policy("ask")
    store.set_policy("full_control", "unrelated")


def package(sid="one", mode="full_control"):
    write_history(sid)
    store.set_policy(mode, sid)
    return cm.export_sessions_to_zip([sid])[0]


def replace_members(raw, updates):
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as source, zipfile.ZipFile(output, "w") as target:
        for name in source.namelist():
            data = updates.get(name, source.read(name))
            if data is not None:
                target.writestr(name, data)
        for name, data in updates.items():
            if name not in source.namelist() and data is not None:
                # ZipInfo(name) normalizes backslashes on Windows. Assign the
                # raw name explicitly to exercise foreign/malformed archives.
                info = zipfile.ZipInfo()
                info.filename = name
                target.writestr(info, data)
    return output.getvalue()


def read_snapshot(raw, tmp_path):
    path = tmp_path / "read.sqlite3"
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        path.write_bytes(zf.read(store.SHARE_DATABASE_PATH))
    with sqlite3.connect(path) as db:
        return dict(db.execute("SELECT scope,mode FROM policies")), {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_export_only_selected_overrides_includes_inherit_and_no_runtime(history, tmp_path):
    write_history("one"); write_history("inherited"); write_history("other")
    store.set_policy("full_control")
    store.set_policy("ask", "one"); store.set_policy("full_control", "other")
    task = store.create_task("one", {"request": {"private": "private-checkpoint"}})
    store.checkpoint(task["id"], "main", {"private": "private-checkpoint"})
    raw, _, skipped = cm.export_sessions_to_zip(["one", "inherited", "missing"])
    rows, tables = read_snapshot(raw, tmp_path)
    assert skipped == ["missing"]
    assert rows == {"session:one": "ask", "session:inherited": None}
    assert tables == {"policies", "share_metadata"}
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        assert "private-checkpoint".encode() not in b"".join(zf.read(n) for n in zf.namelist())
        assert not any(n.endswith(("-wal", "-shm")) for n in zf.namelist())


def test_preview_is_readonly_and_surfaces_config(history, tmp_path, monkeypatch):
    raw = package()
    receiver(tmp_path, monkeypatch)
    store.set_policy("ask", "one")
    before = (store.ROOT / store.DATABASE_NAME).read_bytes()
    preview = cm.list_zip_sessions(raw)["sessions"][0]
    assert preview["permission_included"] and preview["permission_mode"] == "full_control"
    assert (store.ROOT / store.DATABASE_NAME).read_bytes() == before
    assert store.policy("one")["session_mode"] == "ask"


def test_export_reads_committed_configuration_from_wal(history, tmp_path):
    write_history("one"); store.set_policy("ask", "one")
    writer = sqlite3.connect(store.ROOT / store.DATABASE_NAME)
    try:
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("UPDATE policies SET mode='full_control' WHERE scope='session:one'")
        writer.commit()
        assert (store.ROOT / (store.DATABASE_NAME + "-wal")).exists()
        rows, _ = read_snapshot(cm.export_sessions_to_zip(["one"])[0], tmp_path)
        assert rows == {"session:one": "full_control"}
    finally:
        writer.close()


def test_http_export_preview_import_and_load_permissions(history, tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers.chat_router import api_chat_router
    from routers.tool_approval_router import api_permission_router
    write_history("one"); store.set_policy("full_control", "one")
    app = FastAPI()
    app.include_router(api_chat_router); app.include_router(api_permission_router)
    with TestClient(app) as client:
        exported = client.get("/chat_history/export_zip", params={"session_ids": "one"})
        assert exported.status_code == 200 and exported.headers["content-type"].startswith("application/zip")
        receiver(tmp_path, monkeypatch); store.set_policy("ask", "one")
        params = {"filename": "share.zip"}
        headers = {"content-type": "application/octet-stream"}
        preview = client.post("/chat_history/import_preview", params=params, headers=headers, content=exported.content)
        assert preview.status_code == 200 and preview.json()["sessions"][0]["permission_mode"] == "full_control"
        assert client.get("/chat_config/permissions", params={"session_id": "one"}).json()["mode"] == "ask"
        imported = client.post("/chat_history/import_package", params={**params,
            "decisions": json.dumps({"one": "rename"})}, headers=headers, content=exported.content)
        assert imported.status_code == 200 and not imported.json()["failed"]
        sid = imported.json()["imported"][0]["session_id"]
        assert sid != "one"
        current = client.get("/chat_config/permissions", params={"session_id": sid}).json()
        assert current["mode"] == "full_control" and current["global_mode"] == "ask"
        assert client.get("/chat_config/permissions", params={"session_id": "one"}).json()["mode"] == "ask"
        assert client.get("/chat_config/permissions", params={"session_id": "unrelated"}).json()["mode"] == "full_control"
        corrupted = replace_members(exported.content, {store.SHARE_DATABASE_PATH: b"damaged"})
        assert client.post("/chat_history/import_preview", params=params, headers=headers, content=corrupted).status_code == 400


def test_portable_cleanup_handles_malformed_control_values():
    # Invalid events should be left for the normal history validator, not crash
    # the sanitizer while checking for private-control event names.
    value = [{"event": {"invalid": True}, "response_record": {"protocol": []}},
             {"role": "assistant", "content": "kept"}]
    assert cm._portable_history_value(value)[1]["content"] == "kept"


def test_merge_rename_preserves_source_other_sessions_and_local_task(history, tmp_path, monkeypatch):
    raw = package()
    receiver(tmp_path, monkeypatch)
    store.set_policy("ask", "one")
    task = store.create_task("unrelated", {"request": {"local": True}})
    approval = store.request_approval(task["id"], "main", "local-call", task["epoch"], {"tool": "run_command"})
    result = asyncio.run(cm.import_sessions_from_zip(raw, decisions={"one": "rename"}))
    imported = result["imported"][0]
    assert not result["failed"] and imported["collision"] and imported["session_id"] != "one"
    assert store.policy(imported["session_id"])["session_mode"] == "full_control"
    assert store.policy("one")["session_mode"] == "ask"
    assert store.policy("unrelated")["session_mode"] == "full_control"
    assert store.policy()["global_mode"] == "ask"
    assert store.task(task["id"])["state"] == "running"
    assert store.approval(approval["id"])["state"] == "pending"
    with store.database() as db:
        assert db.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1


@pytest.mark.parametrize("mode", ["ask", "full_control", None])
def test_overwrite_merges_only_target_including_inherit(history, tmp_path, monkeypatch, mode):
    raw = package(mode=mode)
    receiver(tmp_path, monkeypatch)
    store.set_policy("full_control" if mode != "full_control" else "ask", "one")
    result = asyncio.run(cm.import_sessions_from_zip(raw, decisions={"one": "overwrite"}))
    assert not result["failed"] and result["imported"][0]["permission_included"]
    assert store.policy("one")["session_mode"] == mode
    assert store.policy("one")["mode"] == (mode or "ask")
    assert store.policy("unrelated")["session_mode"] == "full_control"
    assert store.policy()["global_mode"] == "ask"


def test_skip_and_mixed_import_apply_only_successful_sessions(history, tmp_path, monkeypatch):
    raw = package()
    write_history("new"); store.set_policy("ask", "new")
    raw = cm.export_sessions_to_zip(["one", "new"])[0]
    cm._get_chat_history_file("new").unlink()
    receiver(tmp_path, monkeypatch); store.set_policy("ask", "one")
    result = asyncio.run(cm.import_sessions_from_zip(raw, decisions={"one": "skip"}))
    assert [r["session_id"] for r in result["imported"]] == ["new"]
    assert [r["session_id"] for r in result["skipped"]] == ["one"]
    assert store.policy("one")["session_mode"] == "ask"
    assert store.policy("new")["session_mode"] == "ask"


@pytest.mark.parametrize("damage", ["missing", "garbage", "extra-session", "invalid-mode", "runtime-table", "missing-row"])
def test_bad_snapshot_rejected_before_any_import(history, tmp_path, monkeypatch, damage):
    raw = package()
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        snapshot = zf.read(store.SHARE_DATABASE_PATH)
    if damage in {"extra-session", "invalid-mode", "runtime-table", "missing-row"}:
        path = tmp_path / "tampered.sqlite3"; path.write_bytes(snapshot)
        with sqlite3.connect(path) as db:
            if damage == "extra-session": db.execute("INSERT INTO policies VALUES('session:unrelated','ask')")
            if damage == "invalid-mode": db.execute("UPDATE policies SET mode='unexpected'")
            if damage == "runtime-table": db.execute("CREATE TABLE tasks(id TEXT)")
            if damage == "missing-row": db.execute("DELETE FROM policies")
        snapshot = path.read_bytes()
    elif damage == "garbage": snapshot = b"not a sqlite database"
    else: snapshot = None
    raw = replace_members(raw, {store.SHARE_DATABASE_PATH: snapshot})
    receiver(tmp_path, monkeypatch); store.set_policy("ask", "one")
    before = cm._get_chat_history_file("one").read_bytes()
    with pytest.raises(ValueError): cm.list_zip_sessions(raw)
    with pytest.raises(ValueError): asyncio.run(cm.import_sessions_from_zip(raw, conflict_strategy="overwrite"))
    assert cm._get_chat_history_file("one").read_bytes() == before
    assert store.policy("one")["session_mode"] == "ask"
    assert store.policy("unrelated")["session_mode"] == "full_control"


@pytest.mark.parametrize("version", [1, 2, "bare"])
def test_legacy_packages_do_not_change_local_permissions(history, tmp_path, monkeypatch, version):
    raw = package()
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        manifest = json.loads(zf.read("manifest.json"))
    manifest.pop("permissions"); manifest["version"] = version
    raw = replace_members(raw, {store.SHARE_DATABASE_PATH: None,
        "manifest.json": None if version == "bare" else json.dumps(manifest)})
    receiver(tmp_path, monkeypatch); store.set_policy("ask", "one")
    result = asyncio.run(cm.import_sessions_from_zip(raw, conflict_strategy="overwrite"))
    assert not result["failed"] and not result["imported"][0]["permission_included"]
    assert store.policy("one")["session_mode"] == "ask"
    assert store.policy("unrelated")["session_mode"] == "full_control"


@pytest.mark.parametrize("exists", [True, False])
def test_failed_policy_transaction_restores_history(history, tmp_path, monkeypatch, exists):
    raw = package()
    receiver(tmp_path, monkeypatch); store.set_policy("ask", "one")
    path = cm._get_chat_history_file("one")
    if exists: write_history("one", title="local-original")
    else: path.unlink()
    before = path.read_bytes() if exists else None
    def fail(*args): raise RuntimeError("simulated permission disk failure")
    monkeypatch.setattr(store, "merge_shared_policy", fail)
    result = asyncio.run(cm.import_sessions_from_zip(raw, conflict_strategy="overwrite"))
    assert not result["imported"] and result["failed"]
    assert (path.read_bytes() if path.exists() else None) == before
    assert store.policy("one")["session_mode"] == "ask"


@pytest.mark.parametrize("state", ["running", "suspended"])
def test_cannot_replace_local_active_or_suspended_task(history, tmp_path, monkeypatch, state):
    raw = package()
    receiver(tmp_path, monkeypatch); store.set_policy("ask", "one")
    task = store.create_task("one", {})
    store.set_task_state(task["id"], state)
    before = cm._get_chat_history_file("one").read_bytes()
    result = asyncio.run(cm.import_sessions_from_zip(raw, conflict_strategy="overwrite"))
    assert not result["imported"] and result["failed"]
    assert "任务" in result["failed"][0]["error"]
    assert cm._get_chat_history_file("one").read_bytes() == before
    assert store.task(task["id"])["state"] == state
    assert store.policy("one")["session_mode"] == "ask"


def test_upload_directory_alias_survives_rename_and_second_share(history):
    write_history("one", upload_id="attachment-alias")
    data = fm.HISTORY_ROOT / "attachment-alias" / "media" / "image.png"
    data.parent.mkdir(parents=True); data.write_bytes(b"image bytes")
    raw = cm.export_sessions_to_zip(["one"])[0]
    result = asyncio.run(cm.import_sessions_from_zip(raw, conflict_strategy="rename"))
    imported = result["imported"][0]
    assert not result["failed"] and imported["data_files"] == 1
    assert (fm.HISTORY_ROOT / imported["upload_dir_name"] / "media/image.png").read_bytes() == b"image bytes"
    assert data.read_bytes() == b"image bytes"
    second = cm.export_sessions_to_zip([imported["session_id"]])[0]
    assert cm.list_zip_sessions(second)["sessions"][0]["media_count"] == 1


def test_attachment_write_failure_does_not_replace_history_or_policy(history, tmp_path, monkeypatch):
    raw = package()
    raw = replace_members(raw, {"session_files/one/media/fail.png": b"image"})
    # The original no-attachment manifest uses an empty upload_id; fallback is one.
    receiver(tmp_path, monkeypatch); store.set_policy("ask", "one")
    write_history("one", title="original")
    before = cm._get_chat_history_file("one").read_bytes()
    original_write = type(history).write_bytes
    def fail(path, data):
        if path.name == "fail.png": raise OSError("simulated attachment failure")
        return original_write(path, data)
    monkeypatch.setattr(type(history), "write_bytes", fail)
    result = asyncio.run(cm.import_sessions_from_zip(raw, conflict_strategy="overwrite"))
    assert not result["imported"] and result["failed"]
    assert cm._get_chat_history_file("one").read_bytes() == before
    assert store.policy("one")["session_mode"] == "ask"
    assert not any(fm.HISTORY_ROOT.glob("*/media/fail.png"))


def test_overwrite_does_not_remove_another_sessions_shared_attachments(history):
    raw = package()
    write_history("one", upload_id="shared-folder")
    write_history("other", upload_id="shared-folder")
    data = fm.HISTORY_ROOT / "shared-folder" / "media/keep.png"
    data.parent.mkdir(parents=True); data.write_bytes(b"keep")
    result = asyncio.run(cm.import_sessions_from_zip(raw, conflict_strategy="overwrite"))
    assert result["imported"] and not result["failed"]
    assert data.read_bytes() == b"keep"
    assert cm._session_upload_directory("other") == "shared-folder"


@pytest.mark.parametrize("fail", [False, True])
def test_overwrite_retires_old_pending_and_todo_only_on_success(history, monkeypatch, fail):
    raw = package()
    pending = history / "sidecars/one_chat.jsonl.pending"
    todo = history / "sidecars/one_chat.jsonl.todo"
    pending.parent.mkdir(parents=True, exist_ok=True)
    pending.write_text(json.dumps({"event": "chat_round", "writer_pid": -1}), encoding="utf-8")
    todo.write_text('[{"content":"old plan","status":"pending"}]', encoding="utf-8")
    old_pending, old_todo = pending.read_bytes(), todo.read_bytes()
    before = cm._get_chat_history_file("one").read_bytes()
    if fail:
        def broken_merge(*args): raise RuntimeError("simulated permission failure")
        monkeypatch.setattr(store, "merge_shared_policy", broken_merge)
    result = asyncio.run(cm.import_sessions_from_zip(raw, conflict_strategy="overwrite"))
    if fail:
        assert result["failed"] and not result["imported"]
        assert pending.read_bytes() == old_pending and todo.read_bytes() == old_todo
        assert cm._get_chat_history_file("one").read_bytes() == before
    else:
        assert result["imported"] and not result["failed"]
        assert not pending.exists() and not todo.exists()
    assert not list((history / "sidecars").glob("*.import-old-*"))


def test_live_legacy_pending_round_blocks_overwrite(history):
    import os
    raw = package()
    pending = history / "sidecars/one_chat.jsonl.pending"
    pending.parent.mkdir(parents=True, exist_ok=True)
    pending.write_text(json.dumps({"writer_pid": os.getpid()}), encoding="utf-8")
    before = cm._get_chat_history_file("one").read_bytes()
    result = asyncio.run(cm.import_sessions_from_zip(raw, conflict_strategy="overwrite"))
    assert not result["imported"] and result["failed"]
    assert cm._get_chat_history_file("one").read_bytes() == before and pending.exists()


def test_history_strips_live_approvals_both_protocol_refs_and_custom_headers(history):
    source = write_history("one", extra_meta={"responses_state": {"head_response_id": "remote-private"},
        "model_selection": {"chat_model": {"ownership_name": "P", "model_name": "M", "api_type": "messages",
            "headers": {"Authorization": "private-header"}, "parameter": {"max_tokens": 128}}}}, extra_events=[
        {"role": "assistant", "content": "fact", "provider_state": {"protocol": "messages", "record_id": "private-native"}},
        {"role": "assistant", "content": "fact2", "provider_state": {"protocol": "responses", "record_id": "private-native2"}},
        {"event": "tool_approval", "phase": "requested", "approval": {"id": "private-approval"}},
        {"event": "model_response", "protocol": "responses", "response_id": "remote-private", "record_ref": "private-record"},
    ])
    original = source.read_bytes()
    raw = cm.export_sessions_to_zip(["one"])[0]
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        exported = zf.read("one_chat.jsonl").decode()
    meta, entries, _, _ = cm._parse_jsonl_payload(original)
    imported = json.dumps({"meta": meta, "entries": entries})
    for text in (exported, imported):
        for private in ("provider_state", "responses_state", "private-header", "private-approval", "private-native", "private-record"):
            assert private not in text
        assert "fact" in text and "max_tokens" in text
    assert source.read_bytes() == original


@pytest.mark.parametrize("path", ["../escaped.txt", "session_files/one/C:/escape", "session_files/one/media/../../escape", "session_files/one/back\\slash"])
def test_zip_paths_rejected_before_writing(history, path):
    raw = replace_members(package(), {path: b"untrusted"})
    with pytest.raises(ValueError, match="路径"): cm.list_zip_sessions(raw)


def test_expanded_zip_limit_and_duplicate_manifest_ids(history, monkeypatch):
    raw = package()
    monkeypatch.setattr(cm, "IMPORT_PACKAGE_MAX_BYTES", len(raw) + 100)
    # The compressed package fits, its SQLite/history expansion does not.
    with pytest.raises(ValueError, match="解压后"): cm.list_zip_sessions(raw)
    monkeypatch.setattr(cm, "IMPORT_PACKAGE_MAX_BYTES", 512 * 1024 * 1024)
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        manifest = json.loads(zf.read("manifest.json"))
    manifest["sessions"] *= 2
    with pytest.raises(ValueError, match="重复"):
        cm.list_zip_sessions(replace_members(raw, {"manifest.json": json.dumps(manifest)}))
