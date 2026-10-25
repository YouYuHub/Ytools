"""User-owned template definitions, with atomic revision checks across processes."""
import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

from util.timestamp_utils import now_str

ROOT = Path(__file__).resolve().parents[1] / "history_files" / "task_templates"


class Conflict(ValueError):
    pass


@contextmanager
def database():
    from memory.storage_layout import require_identity
    require_identity()
    from security.principal import current
    principal = current()
    directory = ROOT / principal.user_id if principal else ROOT
    directory.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(directory / "templates.sqlite3", timeout=20)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    db.execute("""CREATE TABLE IF NOT EXISTS templates(
        id TEXT PRIMARY KEY, name TEXT NOT NULL, name_key TEXT UNIQUE NOT NULL,
        description TEXT NOT NULL, body TEXT NOT NULL, version INTEGER NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
    try:
        with db:
            yield db
    finally:
        db.close()


def _record(row):
    if row is None:
        raise FileNotFoundError("任务模板不存在或已删除")
    return {"id": row["id"], "version": row["version"], "created_at": row["created_at"],
            "updated_at": row["updated_at"], "template": json.loads(row["body"])}


def listing():
    with database() as db:
        return [dict(row) for row in db.execute("SELECT id,name,description,version,updated_at FROM templates ORDER BY updated_at DESC,id")]


def read(identifier):
    with database() as db:
        return _record(db.execute("SELECT * FROM templates WHERE id=?", (identifier,)).fetchone())


def save(definition, identifier=None, version=None):
    body = json.dumps(definition, ensure_ascii=False, allow_nan=False)
    if len(body.encode("utf-8")) > 262144:
        raise ValueError("单个模板不能超过 256KB")
    now = now_str()
    from security.resource_limits import storage_write
    try:
        with storage_write(len(body.encode("utf-8")) + 8192), database() as db:
            if identifier:
                changed = db.execute("UPDATE templates SET name=?,name_key=?,description=?,body=?,version=version+1,updated_at=? WHERE id=? AND version=?",
                                     (definition["name"], definition["name"].casefold(), definition["description"], body, now, identifier, version)).rowcount
                if not changed:
                    if db.execute("SELECT 1 FROM templates WHERE id=?", (identifier,)).fetchone() is None:
                        raise FileNotFoundError("任务模板不存在或已删除")
                    raise Conflict("模板已被其他页面修改，请重新加载后再保存")
            else:
                # Reserve a write transaction before checking the collection limit.
                db.execute("BEGIN IMMEDIATE")
                if db.execute("SELECT count(*) FROM templates").fetchone()[0] >= 200:
                    raise ValueError("任务模板最多保存 200 个，请先删除不再需要的模板")
                identifier = uuid.uuid4().hex
                db.execute("INSERT INTO templates VALUES(?,?,?,?,?,1,?,?)",
                           (identifier, definition["name"], definition["name"].casefold(), definition["description"], body, now, now))
            record = _record(db.execute("SELECT * FROM templates WHERE id=?", (identifier,)).fetchone())
        return record
    except sqlite3.IntegrityError as exc:
        raise Conflict("已有同名任务模板，请更换名称") from exc


def delete(identifier, version):
    with database() as db:
        if not db.execute("DELETE FROM templates WHERE id=? AND version=?", (identifier, version)).rowcount:
            if db.execute("SELECT 1 FROM templates WHERE id=?", (identifier,)).fetchone() is None:
                raise FileNotFoundError("任务模板不存在或已删除")
            raise Conflict("模板已变化，请重新加载后再删除")
