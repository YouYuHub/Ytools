"""Local control policies, durable approval checkpoints and execution ledger."""
import copy
import json
import os
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "history_files" / "state"
DATABASE_NAME = "permission_runtime.sqlite3"
_migration_lock = threading.Lock()


def _snapshot_database(source, target):
    """SQLite backup includes committed WAL frames; the result is one file."""
    origin = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True, timeout=20)
    try:
        snapshot = sqlite3.connect(target, timeout=20)
        try:
            deadline = time.monotonic() + 20
            def check_progress(status, remaining, total):
                if time.monotonic() >= deadline:
                    raise TimeoutError("读取旧权限数据库超时")
            origin.backup(snapshot, pages=256, progress=check_progress, sleep=0.05)
            snapshot.execute("PRAGMA journal_mode=DELETE")
            snapshot.execute("VACUUM")
            if snapshot.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise sqlite3.DatabaseError("迁移快照完整性检查失败")
        finally:
            snapshot.close()
    finally:
        origin.close()


def prepare_database():
    """Migrate the legacy store once, before publishing/using the new path.

    Upgrade with the old backend stopped. Thread/process locks prevent new
    workers from racing each other; an existing new store always takes priority.
    ROOT remains overridable for isolated tests and worker fixtures.
    """
    root = Path(ROOT)
    path = root / DATABASE_NAME
    if path.exists():
        return path
    legacy = root.parent / "sidecars" / DATABASE_NAME
    root.mkdir(parents=True, exist_ok=True)
    if not legacy.exists():
        return path
    from util.file_lock import cross_process_lock
    with _migration_lock, cross_process_lock(root.parent / "lock" / (DATABASE_NAME + ".migration.lock")):
        if path.exists() or not legacy.exists():
            return path
        snapshot = root / ("." + DATABASE_NAME + "." + uuid.uuid4().hex + ".migrating")
        backup_temp = None
        try:
            _snapshot_database(legacy, snapshot)
            backup_dir = root / "backups"
            backup_dir.mkdir(exist_ok=True)
            backup = backup_dir / ("permission_runtime.legacy-" + time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8] + ".sqlite3")
            backup_temp = backup.with_suffix(".sqlite3.migrating")
            shutil.copyfile(snapshot, backup_temp)
            for saved in (snapshot, backup_temp):
                with saved.open("r+b") as file:
                    os.fsync(file.fileno())
            os.replace(backup_temp, backup)
            os.replace(snapshot, path)
        except (OSError, sqlite3.Error) as error:
            raise RuntimeError("权限数据库迁移失败，旧文件已保留；请检查磁盘与文件权限后重启") from error
        finally:
            for suffix in ("", "-wal", "-shm", "-journal"):
                scratch = Path(str(snapshot) + suffix)
                try:
                    scratch.unlink(missing_ok=True)
                except OSError:
                    pass
            if backup_temp is not None:
                try:
                    backup_temp.unlink(missing_ok=True)
                except OSError:
                    pass
        # Never copy/remove SQLite's WAL separately before making a verified
        # snapshot. If the legacy DB is still open, keep its whole file set.
        from util.logger import get_logger
        try:
            legacy.unlink()
            for suffix in ("-wal", "-shm", "-journal"):
                Path(str(legacy) + suffix).unlink(missing_ok=True)
        except OSError:
            get_logger(__name__).warning("权限库已迁移到 %s；旧文件仍被占用，请关闭旧版本后清理。备份：%s", path, backup)
        else:
            get_logger(__name__).info("权限数据库已迁移到 %s；迁移备份：%s", path, backup)
    return path


def encode(value):
    return json.dumps(value, ensure_ascii=False, default=lambda obj: obj.model_dump(exclude_none=True) if hasattr(obj, "model_dump") else str(obj))


@contextmanager
def database():
    path = prepare_database()
    db = sqlite3.connect(path, timeout=20)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    db.executescript("""
      CREATE TABLE IF NOT EXISTS policies(scope TEXT PRIMARY KEY, mode TEXT);
      CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, session TEXT, state TEXT, owner INTEGER, epoch TEXT, data TEXT, updated REAL);
      CREATE TABLE IF NOT EXISTS checkpoints(task TEXT, agent TEXT, data TEXT, PRIMARY KEY(task,agent));
      CREATE TABLE IF NOT EXISTS approvals(id TEXT PRIMARY KEY, task TEXT, agent TEXT, call_id TEXT, state TEXT, epoch TEXT, plan TEXT, updated REAL);
      CREATE TABLE IF NOT EXISTS executions(task TEXT, agent TEXT, call_id TEXT, state TEXT, result TEXT, PRIMARY KEY(task,agent,call_id));
      CREATE INDEX IF NOT EXISTS approval_task ON approvals(task);
    """)
    try:
        with db:
            yield db
    finally:
        db.close()


def policy(session=None):
    with database() as db:
        rows = {row[0]: row[1] for row in db.execute("SELECT scope,mode FROM policies")}
    default = rows.get("global", "ask")
    override = rows.get("session:" + session) if session else None
    if default not in {"ask", "full_control"}:
        default = "ask"
    if override is not None and override not in {"ask", "full_control"}:
        override = "ask"
    return {"global_mode": default, "session_mode": override, "mode": override or default}


def set_policy(mode, session=None):
    if mode not in {None, "ask", "full_control"} or (session is None and mode is None):
        raise ValueError("权限模式必须为 ask 或 full_control；会话可用 null 跟随全局")
    scope = "session:" + session if session else "global"
    with database() as db:
        if mode is None:
            db.execute("DELETE FROM policies WHERE scope=?", (scope,))
        else:
            db.execute("INSERT OR REPLACE INTO policies VALUES(?,?)", (scope, mode))
    return policy(session)


def task(task_id):
    with database() as db:
        row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not row:
        return None
    value = dict(row)
    value["data"] = json.loads(value["data"])
    return value


def create_task(session, data):
    task_id, epoch = uuid.uuid4().hex, uuid.uuid4().hex
    with database() as db:
        db.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?,?)", (task_id, session, "running", os.getpid(), epoch, encode(data), time.time()))
    return task(task_id)


def checkpoint(task_id, agent, data):
    with database() as db:
        db.execute("INSERT OR REPLACE INTO checkpoints VALUES(?,?,?)", (task_id, agent, encode(data)))
        db.execute("UPDATE tasks SET updated=? WHERE id=?", (time.time(), task_id))


def load_checkpoint(task_id, agent="main"):
    with database() as db:
        row = db.execute("SELECT data FROM checkpoints WHERE task=? AND agent=?", (task_id, agent)).fetchone()
    return json.loads(row[0]) if row else None


def set_task_state(task_id, state):
    with database() as db:
        db.execute("UPDATE tasks SET state=?,updated=? WHERE id=?", (state, time.time(), task_id))
        if state in {"cancelled", "completed", "failed"}:
            db.execute("UPDATE approvals SET state='cancelled' WHERE task=? AND state IN ('pending','approved','denied')", (task_id,))
            db.execute("DELETE FROM checkpoints WHERE task=?", (task_id,))
            db.execute("UPDATE executions SET result=NULL WHERE task=?", (task_id,))
            row = db.execute("SELECT data FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row:
                data = json.loads(row[0]); data.pop("request", None)
                db.execute("UPDATE tasks SET data=? WHERE id=?", (encode(data), task_id))


def execution(task_id, agent, call_id):
    with database() as db:
        row = db.execute("SELECT state,result FROM executions WHERE task=? AND agent=? AND call_id=?", (task_id, agent, call_id)).fetchone()
    return {"state": row[0], "result": json.loads(row[1]) if row[1] else None} if row else None


def claim_execution(task_id, epoch, agent, call_id):
    with database() as db:
        row = db.execute("SELECT state,epoch FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row or row[0] != "running" or row[1] != epoch:
            raise ValueError("任务已停止或执行代次已失效")
        if db.execute("SELECT 1 FROM executions WHERE task=? AND agent=? AND call_id=?", (task_id, agent, call_id)).fetchone():
            raise ValueError("本次工具调用已有执行记录，禁止重复执行")
        db.execute("INSERT INTO executions VALUES(?,?,?,'executing',NULL)", (task_id, agent, call_id))


def completed(task_id, agent, call_id, result):
    with database() as db:
        db.execute("INSERT OR REPLACE INTO executions VALUES(?,?,?,'completed',?)", (task_id, agent, call_id, encode(result)))


def request_approval(task_id, agent, call_id, epoch, plan):
    with database() as db:
        existing = db.execute("SELECT id FROM approvals WHERE task=? AND agent=? AND call_id=? AND epoch=?", (task_id, agent, call_id, epoch)).fetchone()
        if existing:
            return approval(existing[0])
        approval_id = uuid.uuid4().hex
        db.execute("INSERT INTO approvals VALUES(?,?,?,?,'pending',?,?,?)", (approval_id, task_id, agent, call_id, epoch, encode(plan), time.time()))
    return approval(approval_id)


def approval(approval_id):
    with database() as db:
        row = db.execute("SELECT a.*,t.session,t.state AS task_state,t.owner FROM approvals a JOIN tasks t ON t.id=a.task WHERE a.id=?", (approval_id,)).fetchone()
    if not row:
        return None
    value = dict(row)
    value["plan"] = json.loads(value["plan"])
    return value


def decide(approval_id, session, epoch, decision):
    if decision not in {"approve", "deny"}:
        raise ValueError("请选择 approve 或 deny")
    new_state = "approved" if decision == "approve" else "denied"
    with database() as db:
        row = db.execute("SELECT a.state,a.epoch,t.session,t.state FROM approvals a JOIN tasks t ON t.id=a.task WHERE a.id=?", (approval_id,)).fetchone()
        if not row:
            raise LookupError("审批不存在")
        if row[1] != epoch or row[2] != session or row[3] != "running":
            raise ValueError("任务或审批代次已变化，请刷新后重新确认")
        if row[0] not in {"pending", new_state}:
            raise ValueError("此审批已被处理，请刷新")
        db.execute("UPDATE approvals SET state=?,updated=? WHERE id=?", (new_state, time.time(), approval_id))
    return approval(approval_id)


def finish_approval(approval_id, state):
    with database() as db:
        db.execute("UPDATE approvals SET state=?,updated=? WHERE id=?", (state, time.time(), approval_id))


def pid_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        # pid_exists/OpenProcess can still find an exited Windows process while
        # the parent holds its handle. Check its exit code before blocking resume.
        import ctypes
        from ctypes import wintypes
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = api.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5
        try:
            code = wintypes.DWORD()
            return not api.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
        finally:
            api.CloseHandle(handle)
    from memory.chat_memory import _pid_alive
    return _pid_alive(pid)


def pending(session):
    with database() as db:
        rows = db.execute("SELECT id FROM tasks WHERE session=? AND state IN ('running','suspended')", (session,)).fetchall()
    for row in rows:
        current = task(row[0])
        if current["state"] == "running" and not pid_alive(current["owner"]):
            set_task_state(current["id"], "suspended")
    with database() as db:
        ids = db.execute("SELECT a.id FROM approvals a JOIN tasks t ON t.id=a.task WHERE t.session=? AND t.state IN ('running','suspended') AND a.state IN ('pending','approved','denied') ORDER BY a.updated", (session,)).fetchall()
    return [approval(row[0]) for row in ids]


def resumable(session):
    pending(session)
    with database() as db:
        ids = db.execute("SELECT id FROM tasks WHERE session=? AND state='suspended' ORDER BY updated DESC", (session,)).fetchall()
    return [task(row[0]) for row in ids if load_checkpoint(row[0]) and not task(row[0])["data"].get("preview")]


def resume(task_id):
    current = task(task_id)
    if not current or current["state"] != "suspended":
        raise ValueError("任务不是可恢复的挂起状态")
    if load_checkpoint(task_id) is None:
        raise ValueError("任务检查点不存在，不能恢复执行；请重新发送任务")
    if pid_alive(current["owner"]):
        raise ValueError("旧执行进程仍存在，请停止旧进程后再恢复")
    with database() as db:
        if db.execute("SELECT 1 FROM executions WHERE task=? AND state='executing'", (task_id,)).fetchone():
            raise ValueError("存在执行结果未知的工具，请核对实际效果后重新规划；不会自动重跑")
        epoch = uuid.uuid4().hex
        updated = db.execute("UPDATE tasks SET state='running',owner=?,epoch=?,updated=? WHERE id=? AND state='suspended'", (os.getpid(), epoch, time.time(), task_id))
        if updated.rowcount != 1:
            raise ValueError("任务已由另一个恢复请求接管")
        db.execute("UPDATE approvals SET state='invalidated' WHERE task=? AND state IN ('pending','approved','denied')", (task_id,))
    return task(task_id)


def release_resume(task_id, epoch):
    """Release a dispatch that failed before another process adopted it."""
    with database() as db:
        db.execute("UPDATE tasks SET state='suspended',owner=-1,updated=? "
                   "WHERE id=? AND epoch=? AND owner=? AND state='running'",
                   (time.time(), task_id, epoch, os.getpid()))


def adopt(task_id):
    with database() as db:
        db.execute("UPDATE tasks SET owner=? WHERE id=? AND state='running'", (os.getpid(), task_id))
    return task(task_id)


def has_restorable_round(session):
    with database() as db:
        return bool(db.execute("SELECT 1 FROM tasks t JOIN checkpoints c ON c.task=t.id WHERE session=? AND state IN ('running','suspended') AND c.agent='main' AND COALESCE(json_extract(t.data,'$.preview'),0)=0 LIMIT 1", (session,)).fetchone())


def suspend_session(session):
    with database() as db:
        db.execute("UPDATE tasks SET state='suspended' WHERE session=? AND state='running' AND id IN (SELECT task FROM checkpoints)", (session,))


def delete_session(session):
    with database() as db:
        for table in ("approvals", "executions", "checkpoints"):
            db.execute(f"DELETE FROM {table} WHERE task IN (SELECT id FROM tasks WHERE session=?)", (session,))
        db.execute("DELETE FROM tasks WHERE session=?", (session,))
        db.execute("DELETE FROM policies WHERE scope=?", ("session:" + session,))
