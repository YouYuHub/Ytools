"""Isolated lifecycle, paths, backup/restore, migration and resource governance."""
import asyncio
import contextlib
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import zipfile

import pytest
from fastapi import HTTPException

from memory.identity_store import IdentityStore, Denied, Throttled
from memory import session_catalog as catalog
from memory import storage_layout as layout
from security.principal import CURRENT, Principal
from security.settings import SecuritySettings

PASSWORD = "correct horse battery staple 25"


@pytest.fixture
def storage(tmp_path, monkeypatch):
    from memory import chat_memory, file_memory, file_history
    from prompt import prompt_manager
    from memory import task_template_store
    root = tmp_path / "history_files"
    root.mkdir()
    monkeypatch.setattr(chat_memory, "HISTORY_ROOT", root)
    monkeypatch.setattr(chat_memory, "UPLOAD_HISTORY_ROOT", root / "session_files")
    monkeypatch.setattr(chat_memory, "LOCK_ROOT", root / "lock")
    monkeypatch.setattr(chat_memory, "_session_managers", {})
    monkeypatch.setattr(file_memory, "HISTORY_ROOT", root / "session_files")
    monkeypatch.setattr(file_history, "HISTORY_DIFF_ROOT", root / "session_files")
    monkeypatch.setattr(prompt_manager, "MD_FILES_DIR", tmp_path / "prompt/md_files")
    monkeypatch.setattr(task_template_store, "ROOT", root / "task_templates")
    store = IdentityStore(SecuritySettings(root / "state/security", require_mfa=False))
    store.prepare()
    admin = store.create_user("admin", PASSWORD, "admin", bootstrap=True)
    member = store.create_user("member", PASSWORD, actor=admin["id"])
    principal = store.authenticate(store.login("member", PASSWORD))
    token = CURRENT.set(principal)
    try:
        yield tmp_path, root, store, admin, member, principal
    finally:
        CURRENT.reset(token)


def seeded(storage, sid="old"):
    _, root, store, _, member, _ = storage
    store.register(sid, member["id"])
    directory = root / "users" / member["id"] / "sessions" / sid
    directory.mkdir(parents=True)
    (directory / (sid + "_chat.jsonl")).write_text(json.dumps({"_meta": {"title": "legacy", "upload_id": sid}}) + "\n", encoding="utf-8")
    folder = directory / "files" / "media"
    folder.mkdir(parents=True)
    (folder / "picture.png").write_bytes(b"original-media")
    folder = directory / "sidecars"
    folder.mkdir(exist_ok=True)
    (folder / (sid + "_chat.jsonl.todo")).write_text("[]", encoding="utf-8")
    return sid


def test_fresh_session_paths_and_owner_tombstone(storage):
    from security.authorization import session
    from memory.chat_memory import ChatMemoryManager
    from memory.session_listing import list_session_summaries
    _, root, store, admin, member, _ = storage
    session("fresh", create=True)
    assert layout.chat_path(root, "fresh").is_relative_to(root / "users" / member["id"])
    manager = ChatMemoryManager("fresh")
    assert catalog.get(store, "fresh")["state"] == "active"
    assert [r["id"] for r in list_session_summaries()] == ["fresh"]
    manager.delete_chat_session_file("fresh")
    assert catalog.get(store, "fresh")["state"] == "deleted"
    assert store.owner("fresh") == member["id"]
    with pytest.raises(HTTPException):
        session("fresh", create=True)
    with pytest.raises(Denied):
        store.register("fresh", admin["id"])


def test_reconcile_keeps_reserved_ids_and_reports_missing_and_unowned(storage):
    _, root, store, _, member, _ = storage
    seeded(storage)
    store.register("empty", member["id"])
    stray = root / "users" / member["id"] / "sessions" / "unowned"
    stray.mkdir(parents=True)
    (stray / "unowned_chat.jsonl").write_text("{}\n")
    result = catalog.reconcile(store, root)
    assert result["unowned"] == ["unowned"]
    assert catalog.get(store, "old")["state"] == "reserved"
    catalog.reconcile(store, root, apply=True)
    (root / "users" / member["id"] / "sessions" / "old" / "old_chat.jsonl").unlink()
    catalog.reconcile(store, root, apply=True)
    assert catalog.get(store, "old")["state"] == "missing"
    assert catalog.get(store, "empty")["state"] == "reserved"


def test_delete_failure_is_retryable_and_cached_writer_cannot_resurrect(storage, monkeypatch):
    from memory import chat_memory
    from security.authorization import session
    _, root, store, *_ = storage
    session("fresh", create=True)
    manager = chat_memory.ChatMemoryManager("fresh")
    original = chat_memory._delete_path_with_retry
    monkeypatch.setattr(chat_memory, "_delete_path_with_retry", lambda *a, **k: (_ for _ in ()).throw(OSError("injected")))
    with pytest.raises(OSError):
        manager.delete_chat_session_file("fresh")
    assert catalog.get(store, "fresh")["state"] == "cleanup_pending"
    with pytest.raises(HTTPException):
        with manager._write_guard():
            pytest.fail("cached writer must not write")
    monkeypatch.setattr(chat_memory, "_delete_path_with_retry", original)
    manager.delete_chat_session_file("fresh")
    assert catalog.get(store, "fresh")["state"] == "deleted"


def test_parallel_tool_pool_keeps_authenticated_storage_identity(storage, monkeypatch):
    from factory.agent_runtime import tool_executor
    _, _, _, _, _, principal = storage
    monkeypatch.setattr(tool_executor, "_invoke_tool_function", lambda *a: CURRENT.get().user_id)
    result = tool_executor.execute_tool_round([(0, {}, "test", {})], {}, 2)
    assert result[0]["result"] == principal.user_id



def test_account_layout_import_overwrite_uses_published_attachment_generation(storage):
    from memory import chat_memory, file_memory
    _, root, store, *_ = storage
    async def run(text):
        raw = json.dumps({"_meta": {"title": text}}).encode() + b"\n"
        return await chat_memory._import_session_with_data("imported", raw, {}, [("files/source.txt", text.encode())], True)
    asyncio.run(run("first"))
    old = file_memory.resolve_document_path("imported", "source.txt")
    assert old.read_text() == "first"
    asyncio.run(run("second"))
    new = file_memory.resolve_document_path("imported", "source.txt")
    assert new != old and new.read_text() == "second"
    assert new.is_relative_to(root / "users")
    assert not old.exists()


def test_backup_restore_includes_wal_and_excludes_credentials_history(storage):
    from memory import storage_backup as backup
    project, root, store, *_ = storage
    seeded(storage)
    (store.root / "mfa.key").write_bytes(b"test-key-not-real")
    (store.root / "old-initial-password.txt").write_text("must-not-copy")
    (project / ".env").write_text("TEST_ONLY=true\n")
    with contextlib.closing(sqlite3.connect(root / "state/extra.sqlite3")) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE item(value TEXT)")
        db.execute("INSERT INTO item VALUES('committed-in-wal')")
        db.commit()
        archive = project / "backup.zip"
        backup.backup(project, store.root, archive)
    with zipfile.ZipFile(archive) as zipped:
        assert not any("initial-password" in name or name.endswith("-wal") for name in zipped.namelist())
    target = project / "restored"
    assert backup.restore(archive, target)["verified"]
    with sqlite3.connect(target / "history_files/state/extra.sqlite3") as db:
        assert db.execute("SELECT value FROM item").fetchone()[0] == "committed-in-wal"
    with pytest.raises(ValueError):
        backup.restore(archive, target)
    if os.name == "nt":
        import base64
        import subprocess
        script = "$sid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value; $bad=0; Get-ChildItem -LiteralPath $env:YTOOLS_TEST_RESTORE_DIR -Recurse -Force | ForEach-Object { foreach($rule in (Get-Acl -LiteralPath $_.FullName).Access) { if($rule.AccessControlType -eq 'Allow' -and $rule.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value -ne $sid) {$bad++} } }; [Console]::Write($bad)"
        result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand",
                                 base64.b64encode(script.encode("utf-16-le")).decode()],
                                env={**os.environ, "YTOOLS_TEST_RESTORE_DIR": str(target)},
                                capture_output=True, text=True, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
        assert result.stdout.strip() == "0", "Restore children must only grant access to the current user"


@pytest.mark.parametrize("name", ["../outside", "C:/outside", "folder\\outside", "/absolute"])
def test_restore_rejects_unsafe_archive_paths(tmp_path, name):
    from memory.storage_backup import restore
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr(name, b"bad")
        zipped.writestr("manifest.json", json.dumps({"version": 1, "files": [
            {"path": name, "size": 3, "sha256": hashlib.sha256(b"bad").hexdigest(), "sqlite": False}]}))
    with pytest.raises(ValueError):
        restore(archive, tmp_path / "target")
    assert not (tmp_path / "outside").exists()


def test_storage_identity_fails_closed_in_request_and_allows_explicit_system(storage):
    from security.authorization import session
    _, _, store, *_ = storage
    token = CURRENT.set(None)
    required = layout._REQUIRED.set(True)
    try:
        with pytest.raises(PermissionError):
            session("x")
        with layout.system_access(store, reason="offline audit"):
            assert session("x") == "x"
    finally:
        layout._REQUIRED.reset(required)
        CURRENT.reset(token)


def test_account_quota_counts_account_data_and_preserves_old_limits(storage):
    from security import quotas
    from security.resource_limits import storage_usage, storage_write
    _, root, store, admin, member, _ = storage
    seeded(storage)
    used = storage_usage(store, member["id"])
    assert used > 14
    quotas.update(store, admin["id"], member["id"], {"storage_bytes": used + 5})
    quotas.update(store, admin["id"], member["id"], {"daily_calls": 40, "daily_tokens": 100})
    assert quotas.status(store, member["id"])["limits"]["storage_bytes"] == used + 5
    with pytest.raises(Throttled):
        with storage_write(6):
            pytest.fail("must reject before writing")


def test_model_slots_obey_user_and_instance_caps_and_other_account_can_run(storage, monkeypatch):
    from security import quotas
    from security.resource_limits import attempt, release
    _, _, store, admin, member, principal = storage
    monkeypatch.setenv("YTOOLS_MODEL_CONCURRENCY", "2")
    quotas.update(store, admin["id"], member["id"], {"concurrent_models": 1})
    assert attempt("a-running")
    assert not attempt("a-queued")
    token = CURRENT.set(store.authenticate(store.login("admin", PASSWORD)))
    try:
        assert attempt("b-running")
        assert not attempt("b-queued")
        release("b-running")
        release("b-queued")
    finally:
        CURRENT.reset(token)
    release("a-running")
    assert attempt("a-queued")
    release("a-queued")


def test_cancelled_wait_releases_queue_and_dead_process_slot(storage):
    from security.resource_limits import acquire, attempt, release
    from security import quotas
    _, _, store, admin, member, _ = storage
    quotas.update(store, admin["id"], member["id"], {"concurrent_models": 1})
    assert attempt("running")
    async def run():
        task = asyncio.create_task(acquire("waiting"))
        await asyncio.sleep(.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(run())
    with store.database(True) as db:
        assert db.execute("SELECT count(*) FROM model_slots WHERE id='waiting'").fetchone()[0] == 0
        db.execute("UPDATE model_slots SET process_identity='dead-process' WHERE id='running'")
    assert attempt("next")
    release("next")


def test_waiting_accounts_rotate_instead_of_one_account_reentering(storage, monkeypatch):
    from security.resource_limits import attempt, release
    _, _, store, _, _, member = storage
    monkeypatch.setenv("YTOOLS_MODEL_CONCURRENCY", "1")
    assert attempt("member-first")
    assert not attempt("member-next")
    admin = store.authenticate(store.login("admin", PASSWORD))
    token = CURRENT.set(admin)
    try:
        assert not attempt("admin-waiting")
    finally:
        CURRENT.reset(token)
    release("member-first")
    assert not attempt("member-next")
    token = CURRENT.set(admin)
    try:
        assert attempt("admin-waiting")
        release("admin-waiting")
    finally:
        CURRENT.reset(token)
    assert attempt("member-next")
    release("member-next")


def test_recursive_cleanup_refuses_external_targets(storage):
    from memory.chat_memory import _delete_path_with_retry
    project, root, *_ = storage
    outside = project / "unmanaged"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    with pytest.raises(ValueError):
        _delete_path_with_retry(outside, recursive=True)
    assert (outside / "keep.txt").read_text() == "keep"


def test_prompt_quota_rejection_preserves_existing_content(storage):
    from security import quotas
    from security.resource_limits import storage_usage
    from prompt import prompt_manager
    _, _, store, admin, member, _ = storage
    prompt_manager.create_prompt("protected", "original")
    used = storage_usage(store, member["id"])
    quotas.update(store, admin["id"], member["id"], {"storage_bytes": used})
    with pytest.raises(Throttled):
        prompt_manager.save_prompt("protected", "new content is longer")
    assert prompt_manager.read_prompt("protected")["content"] == "original"


def test_prompt_order_uses_subsecond_mtime(storage):
    from prompt import prompt_manager
    import time
    prompt_manager.create_prompt("older", "a")
    prompt_manager.create_prompt("newer", "b")
    base = (time.time_ns() // 1_000_000_000 + 2) * 1_000_000_000
    for name, offset in [("older", 10_000_000), ("newer", 20_000_000)]:
        os.utime(prompt_manager._directory() / (name + ".md"), ns=(base + offset, base + offset))
    assert [item["name"] for item in prompt_manager.list_prompts()][:2] == ["newer.md", "older.md"]


def test_restored_fixture_account_can_login_and_read_history_without_source(storage, monkeypatch):
    from app_factory import create_app
    from fastapi.testclient import TestClient
    from memory import storage_backup as backup, chat_memory, file_memory, file_history
    project, root, store, _, member, _ = storage
    seeded(storage)
    # This fixture represents an account that has completed first-login setup;
    # restore must preserve that flag rather than bypass the HTTP password gate.
    with store.database(True) as db:
        db.execute("UPDATE users SET must_change=0 WHERE username='member'")
    archive = project / "http-restore.zip"
    backup.backup(project, store.root, archive)
    target = project / "http-restored"
    backup.restore(archive, target)
    (root / "users" / member["id"] / "sessions" / "old" / "old_chat.jsonl").unlink()
    restored = target / "history_files"
    monkeypatch.setattr(chat_memory, "HISTORY_ROOT", restored)
    monkeypatch.setattr(chat_memory, "LOCK_ROOT", restored / "lock")
    monkeypatch.setattr(file_memory, "HISTORY_ROOT", restored / "session_files")
    monkeypatch.setattr(file_history, "HISTORY_DIFF_ROOT", restored / "session_files")
    settings = SecuritySettings(restored / "state/security", require_mfa=False)
    app = create_app(security_settings=settings)
    with TestClient(app, base_url=settings.origin) as client:
        response = client.post("/auth/login", json={"username": "member", "password": PASSWORD},
                               headers={"Origin": settings.origin})
        assert response.status_code == 200
        summaries = client.get("/chat_history/summaries").json()
        assert "old" in json.dumps(summaries)
        history = client.get("/chat_history/file", params={"session_id": "old"})
        assert history.status_code == 200 and '"title": "legacy"' in history.text
