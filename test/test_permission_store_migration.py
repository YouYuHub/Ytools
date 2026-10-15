"""Persistent permission storage relocates without losing approvals or WAL data."""
import multiprocessing
import sqlite3
from pathlib import Path

import pytest
from memory import permission_store as store
from factory.agent_runtime import permission_policy


def seed_legacy(root):
    old_root = root / "sidecars"
    previous = store.ROOT
    store.ROOT = old_root
    try:
        store.set_policy("full_control")
        store.set_policy("ask", "one")
        task = store.create_task("one", {"root": str(root), "mode": "ask", "request": {"messages": [{"role": "user", "content": "继续任务"}]}})
        store.checkpoint(task["id"], "main", {"round": 1, "messages": [{"role": "user", "content": "继续任务"}]})
        a = store.request_approval(task["id"], "main", "pending-call", task["epoch"], {"tool": "run_command", "digest": "frozen", "args": {"command": "echo safe"}})
        store.claim_execution(task["id"], task["epoch"], "main", "done-call")
        store.completed(task["id"], "main", "done-call", {"stdout": "cached"})
        store.set_task_state(task["id"], "suspended")
        return old_root / store.DATABASE_NAME, task["id"], a["id"]
    finally:
        store.ROOT = previous


def test_moves_all_permission_data_and_keeps_verified_backup(tmp_path, monkeypatch):
    legacy, task_id, approval_id = seed_legacy(tmp_path)
    monkeypatch.setattr(store, "ROOT", tmp_path / "state")
    assert store.policy("one") == {"global_mode": "full_control", "session_mode": "ask", "mode": "ask"}
    assert store.task(task_id)["state"] == "suspended"
    assert store.load_checkpoint(task_id)["messages"][0]["content"] == "继续任务"
    assert store.approval(approval_id)["state"] == "pending"
    assert store.execution(task_id, "main", "done-call")["result"] == {"stdout": "cached"}
    assert not legacy.exists()
    backups = list((tmp_path / "state" / "backups").glob("*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as db:
        assert db.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert db.execute("SELECT mode FROM policies WHERE scope='global'").fetchone() == ("full_control",)
    # Do not overwrite a user's new setting or take another backup on restart.
    store.set_policy("ask")
    assert store.prepare_database() == tmp_path / "state" / store.DATABASE_NAME
    assert store.policy()["mode"] == "ask"
    assert list((tmp_path / "state" / "backups").glob("*.sqlite3")) == backups


def test_migration_includes_committed_wal_frames(tmp_path, monkeypatch):
    legacy, _, _ = seed_legacy(tmp_path)
    writer = sqlite3.connect(legacy)
    try:
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("INSERT OR REPLACE INTO policies VALUES ('session:wal-only','full_control')")
        writer.commit()
        assert Path(str(legacy) + "-wal").stat().st_size > 0
        monkeypatch.setattr(store, "ROOT", tmp_path / "state")
        assert store.policy("wal-only")["session_mode"] == "full_control"
    finally:
        writer.close()


def test_failed_copy_preserves_old_database_and_can_retry(tmp_path, monkeypatch):
    legacy, _, _ = seed_legacy(tmp_path)
    monkeypatch.setattr(store, "ROOT", tmp_path / "state")
    real_snapshot = store._snapshot_database
    def fail(source, target):
        target.write_bytes(b"incomplete snapshot")
        raise sqlite3.OperationalError("simulated disk failure")
    monkeypatch.setattr(store, "_snapshot_database", fail)
    with pytest.raises(RuntimeError, match="旧文件已保留"):
        store.policy()
    assert legacy.is_file()
    assert not (tmp_path / "state" / store.DATABASE_NAME).exists()
    assert not list((tmp_path / "state").glob("*.migrating*"))
    monkeypatch.setattr(store, "_snapshot_database", real_snapshot)
    assert store.policy()["global_mode"] == "full_control"


def test_existing_new_store_is_authoritative(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "ROOT", tmp_path / "state")
    store.set_policy("ask")
    legacy, _, _ = seed_legacy(tmp_path)
    assert store.policy()["mode"] == "ask"
    assert legacy.exists()  # No automatic merge/replacement of an established new DB.


def test_backup_failure_never_publishes_new_store_or_partial_backup(tmp_path, monkeypatch):
    legacy, _, _ = seed_legacy(tmp_path)
    monkeypatch.setattr(store, "ROOT", tmp_path / "state")
    def fail(source, target):
        target.write_bytes(b"partial backup")
        raise OSError("simulated disk full")
    monkeypatch.setattr(store.shutil, "copyfile", fail)
    with pytest.raises(RuntimeError, match="旧文件已保留"):
        store.prepare_database()
    assert legacy.is_file()
    assert not (tmp_path / "state" / store.DATABASE_NAME).exists()
    assert not list((tmp_path / "state" / "backups").iterdir())


def test_corrupt_legacy_store_is_not_silently_replaced_by_empty_settings(tmp_path, monkeypatch):
    legacy = tmp_path / "sidecars" / store.DATABASE_NAME
    legacy.parent.mkdir()
    legacy.write_bytes(b"corrupt database")
    monkeypatch.setattr(store, "ROOT", tmp_path / "state")
    with pytest.raises(RuntimeError, match="旧文件已保留"):
        store.policy()
    assert legacy.read_bytes() == b"corrupt database"
    assert not (tmp_path / "state" / store.DATABASE_NAME).exists()


def test_new_install_creates_state_store_without_using_real_legacy_file(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "ROOT", tmp_path / "state")
    assert store.policy("new")["mode"] == "ask"
    assert (tmp_path / "state" / store.DATABASE_NAME).is_file()
    assert not (tmp_path / "sidecars").exists()


def test_state_and_backup_paths_are_protected_inside_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(permission_policy, "APP_ROOT", tmp_path)
    monkeypatch.setattr(store, "ROOT", tmp_path / "history_files" / "state")
    for relative in [store.DATABASE_NAME, "backups/permission_runtime.legacy.sqlite3"]:
        path = store.ROOT / relative
        assert permission_policy.protected(path)
        plan = permission_policy.plan_tool("read_file", {"full_file_name": str(path)}, {"root": str(tmp_path), "mode": "ask", "session": "one"})
        assert plan["needs_approval"] and plan["reason"] == "protected_control_resource"


def migrated_worker(root, queue):
    store.ROOT = Path(root) / "state"
    try:
        queue.put(store.policy("one"))
    except Exception as error:
        queue.put({"error": str(error)})


def test_two_windows_workers_migrate_once(tmp_path):
    seed_legacy(tmp_path)
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    workers = [ctx.Process(target=migrated_worker, args=(str(tmp_path), queue)) for _ in range(2)]
    try:
        for process in workers: process.start()
        expected = {"global_mode": "full_control", "session_mode": "ask", "mode": "ask"}
        assert [queue.get(timeout=15) for _ in workers] == [expected, expected]
        for process in workers:
            process.join(5)
            assert process.exitcode == 0
        assert len(list((tmp_path / "state" / "backups").glob("*.sqlite3"))) == 1
    finally:
        for process in workers:
            if process.is_alive(): process.terminate()
            process.join(3)
        queue.close(); queue.join_thread()
