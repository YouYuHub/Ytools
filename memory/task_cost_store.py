"""Durable task usage and configurable budgets. No prompts, keys or tool bodies."""
import json
import math
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager

from memory import permission_store

LIMITS = {"tokens": 0, "cost": 0, "model_calls": 0, "tool_calls": 0, "seconds": 0}
ROLES = ("main", "sub_agent", "compaction", "title")


@contextmanager
def database():
    root = permission_store.ROOT
    root.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(root / "task_costs.sqlite3", timeout=20)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    db.executescript("""
      CREATE TABLE IF NOT EXISTS settings(scope TEXT PRIMARY KEY, data TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS sessions(session TEXT PRIMARY KEY, baseline TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, session TEXT NOT NULL, data TEXT NOT NULL);
      CREATE INDEX IF NOT EXISTS task_session ON tasks(session);
      CREATE TABLE IF NOT EXISTS calls(id TEXT PRIMARY KEY, task TEXT NOT NULL, data TEXT NOT NULL);
      CREATE INDEX IF NOT EXISTS call_task ON calls(task);
      CREATE TABLE IF NOT EXISTS tools(task TEXT, id TEXT, PRIMARY KEY(task,id));
    """)
    try:
        with db:
            yield db
    finally:
        db.close()


def _dump(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _setting(db, scope, default):
    row = db.execute("SELECT data FROM settings WHERE scope=?", (scope,)).fetchone()
    return json.loads(row[0]) if row else default


def validate_limits(value):
    if not isinstance(value, dict) or set(value) - set(LIMITS):
        raise ValueError("预算字段无效")
    result = dict(LIMITS)
    for key, number in value.items():
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or number < 0:
            raise ValueError("预算必须为非负有限数值；0 表示无限制")
        if key in {"tokens", "model_calls", "tool_calls"} and int(number) != number:
            raise ValueError("tokens 和调用次数必须为整数")
        if number > 1e15:
            raise ValueError("预算数值过大")
        result[key] = number
    return result


def config(session=None):
    with database() as db:
        default = _setting(db, "global", dict(LIMITS))
        override = _setting(db, "session:" + session, None) if session else None
        pricing = _setting(db, "pricing", {"currency": "CNY", "models": {}})
    from security.principal import current
    personal = current() is not None
    if personal:
        from security.preferences import read
        default = read()["data"].get("task_limits", {}).get("limits", default)
    return {"global": default, "override": override, "limits": override if override is not None else default,
            "defaults": dict(LIMITS), "pricing": pricing, "scope": "account" if personal else "instance"}


def save_config(limits, session=None):
    scope = "session:" + session if session else "global"
    if limits is None and not session:
        raise ValueError("全局预算不能为 null")
    normalized = validate_limits(limits) if limits is not None else None
    from security.principal import current
    if current() is not None and not session:
        from security.preferences import update
        update("task_limits", {"limits": normalized})
        return config()
    with database() as db:
        if normalized is None:
            db.execute("DELETE FROM settings WHERE scope=?", (scope,))
        else:
            db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (scope, _dump(normalized)))
    return config(session)


def save_pricing(value):
    if not isinstance(value, dict) or value.get("currency") not in {"CNY", "USD", "EUR"}:
        raise ValueError("请选择 CNY、USD 或 EUR；所有单价使用同一币种")
    models = value.get("models")
    if not isinstance(models, dict) or len(models) > 2000:
        raise ValueError("模型价格列表无效")
    result = {}
    for key, rates in models.items():
        if not isinstance(key, str) or len(key) > 600 or not isinstance(rates, dict):
            raise ValueError("模型价格无效")
        normalized = {}
        for kind in ("input", "output", "cached_input"):
            number = rates.get(kind, rates.get("input") if kind == "cached_input" else None)
            if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or not 0 <= number <= 1e12:
                raise ValueError("模型单价须为非负有限数值；免费模型请明确填 0")
            normalized[kind] = number
        result[key] = normalized
    with database() as db:
        # Historical costs keep their original currency; do not silently add currencies.
        old = _setting(db, "pricing", {"currency": value["currency"]})
        if old["currency"] != value["currency"] and db.execute("SELECT 1 FROM calls LIMIT 1").fetchone():
            raise ValueError("已有用量记录后不能切换计价币种，请沿用原币种填写价格")
        db.execute("INSERT OR REPLACE INTO settings VALUES('pricing',?)", (_dump({"currency": value["currency"], "models": result}),))


def normalize_usage(value):
    value = value if isinstance(value, dict) else {}
    def count(*keys):
        for key in keys:
            n = value.get(key)
            if isinstance(n, (int, float)) and not isinstance(n, bool) and math.isfinite(n):
                return max(0, int(n))
        return 0
    prompt, output = count("prompt_tokens", "input_tokens"), count("completion_tokens", "output_tokens")
    total = count("total_tokens") or prompt + output
    details = value.get("prompt_tokens_details") or value.get("input_tokens_details") or {}
    cached = count("cached_tokens")
    n = details.get("cached_tokens") if isinstance(details, dict) else None
    if isinstance(n, (int, float)) and not isinstance(n, bool) and math.isfinite(n):
        cached = max(cached, max(0, int(n)))
    native = value.get("messages_usage") or {}
    if isinstance(native, dict):
        n = native.get("cache_read_input_tokens")
        if isinstance(n, (int, float)) and not isinstance(n, bool) and math.isfinite(n):
            cached = max(cached, max(0, int(n)))
    # Messages adapters expose cache reads separately when prompt excludes them.
    return {"prompt_tokens": prompt, "completion_tokens": output, "total_tokens": max(total, prompt + output),
            "cached_tokens": min(prompt, cached)}


def _add(total, usage):
    for key, n in normalize_usage(usage).items():
        total[key] = total.get(key, 0) + n


def legacy_baseline(text, meta=None):
    """Old main totals already include compression. Add child totals only once."""
    total = normalize_usage((meta or {}).get("usage"))
    # 只按 \n 切分：记录内的 U+0085 等字符会让 splitlines() 把合法记录劈开，
    # 导致该轮的 sub_agent 用量被漏算（与 memory.chat_memory._split_jsonl_lines 同理）。
    for line in (text or "").split("\n"):
        try:
            entry = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(entry, dict):
            continue
        if isinstance(entry.get("_meta"), dict) and meta is None:
            total = normalize_usage(entry["_meta"].get("usage"))
        if entry.get("event") == "chat_round":
            seen = set()
            events = entry.get("events")
            for event in events if isinstance(events, list) else []:
                if (isinstance(event, dict) and event.get("event") == "sub_agent" and event.get("phase") == "done"
                        and event.get("agent_id") not in seen):
                    _add(total, event.get("usage_total"))
                    seen.add(event.get("agent_id"))
    return total


def create_task(task_id, session, *, baseline=None, round_no=0, kind="chat"):
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("INSERT OR IGNORE INTO sessions VALUES(?,?)", (session, _dump(normalize_usage(baseline))))
        row = db.execute("SELECT data FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row:
            data = json.loads(row[0])
            if data["state"] == "suspended":
                data["owner"] = os.getpid()
                data["state"] = "paused" if data.get("reason") else "running"
                data["started"] = time.time() - data.get("elapsed", 0) - data.get("paused_seconds", 0)
                data["pause_started"] = time.time() if data.get("reason") else None
                _put_task(db, data)
            return data
        limits = _setting(db, "session:" + session, None)
        if limits is None:
            limits = _setting(db, "global", dict(LIMITS))
        data = {"id": task_id, "session_id": session, "round": round_no, "kind": kind, "state": "running",
                "owner": os.getpid(),
                "limits": limits, "started": time.time(), "elapsed": 0, "paused_seconds": 0,
                "pause_started": None, "version": 0, "reason": None, "model_calls": 0, "tool_calls": 0}
        _put_task(db, data)
        return data


def has_session(session):
    with database() as db:
        return db.execute("SELECT 1 FROM sessions WHERE session=?", (session,)).fetchone() is not None


def _put_task(db, data):
    db.execute("INSERT OR REPLACE INTO tasks VALUES(?,?,?)", (data["id"], data["session_id"], _dump(data)))


def _task(db, task_id):
    row = db.execute("SELECT data FROM tasks WHERE id=?", (task_id,)).fetchone()
    return json.loads(row[0]) if row else None


def _elapsed(data):
    if data["state"] not in {"running", "paused"}:
        return data.get("elapsed", 0)
    end = data.get("pause_started") or time.time()
    return max(0, end - data["started"] - data.get("paused_seconds", 0))


def _summary(db, task_id):
    result = {"usage": normalize_usage({}), "roles": {}, "cost": 0, "unknown_price_calls": 0,
              "estimated_calls": 0, "pending_calls": 0}
    for row in db.execute("SELECT data FROM calls WHERE task=?", (task_id,)):
        call = json.loads(row[0])
        usage = call["usage"]
        _add(result["usage"], usage)
        role = result["roles"].setdefault(call["role"], {"usage": normalize_usage({}), "cost": 0, "calls": 0})
        _add(role["usage"], usage)
        role["calls"] += 1
        if call.get("cost") is None:
            result["unknown_price_calls"] += 1
        else:
            role["cost"] += call["cost"]
            result["cost"] += call["cost"]
        result["estimated_calls"] += call["source"] != "reported"
        result["pending_calls"] += call["state"] == "running"
    return result


def status(task_id):
    with database() as db:
        data = _task(db, task_id)
        if data:
            data = {**data, **_summary(db, task_id), "elapsed": _elapsed(data)}
        return data


def latest(session, kind=None):
    with database() as db:
        rows = [json.loads(r[0]) for r in db.execute("SELECT data FROM tasks WHERE session=?", (session,))]
    return max((row for row in rows if kind is None or row["kind"] == kind), key=lambda t: t["started"], default=None)


def update_round(task_id, number):
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        data = _task(db, task_id)
        if data:
            number = max(0, int(number or 0))
            if data.get("round") != number:
                data["round"] = number
                _put_task(db, data)


def _pause(db, data, reason):
    if data["state"] != "paused":
        data.update(state="paused", reason=reason, pause_started=time.time(), version=data["version"] + 1)
        _put_task(db, data)
    return {"allowed": False, "state": data["state"], "reason": data.get("reason")}


def reserve(task_id, *, call=None, tools=None, waiting_seconds=0):
    """One transaction serializes concurrent parent/child dispatch decisions."""
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        data = _task(db, task_id)
        if not data:
            raise ValueError("任务用量记录不存在")
        late_title = bool(call and call.get("role") == "title" and data["state"] == "completed")
        if data["state"] not in {"running", "paused"} and not late_title:
            return {"allowed": False, "state": data["state"]}
        if data["state"] == "paused":
            return {"allowed": False, "state": "paused", "reason": data["reason"]}
        if tools and all(db.execute("SELECT 1 FROM tools WHERE task=? AND id=?", (task_id, key)).fetchone() for key in tools):
            return {"allowed": True}
        totals = _summary(db, task_id)
        used = {"tokens": totals["usage"]["total_tokens"], "cost": totals["cost"],
                "model_calls": data["model_calls"], "tool_calls": data["tool_calls"],
                "seconds": max(0, _elapsed(data) - waiting_seconds)}
        for key, limit in data["limits"].items():
            if limit > 0 and used[key] >= limit:
                if late_title:
                    return {"allowed": False, "state": "completed"}
                return _pause(db, data, key)
        if data["limits"]["cost"] > 0 and (totals["unknown_price_calls"] or (call and call.get("price") is None)):
            if late_title:
                return {"allowed": False, "state": "completed"}
            return _pause(db, data, "missing_price")
        if call:
            data["model_calls"] += 1
            if call.get("price"):
                call["cost"] = call["usage"]["prompt_tokens"] * call["price"]["input"] / 1_000_000
            db.execute("INSERT INTO calls VALUES(?,?,?)", (call["id"], task_id, _dump(call)))
        for key in tools or []:
            added = db.execute("INSERT OR IGNORE INTO tools VALUES(?,?)", (task_id, key)).rowcount
            data["tool_calls"] += added
        _put_task(db, data)
        return {"allowed": True}


def settle(call_id, usage, *, source="reported", state="completed"):
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT data FROM calls WHERE id=?", (call_id,)).fetchone()
        if not row:
            return
        data = json.loads(row[0])
        data.update(usage=normalize_usage(usage), source=source, state=state)
        price = data.get("price")
        if price:
            u = data["usage"]
            data["cost"] = ((u["prompt_tokens"] - u["cached_tokens"]) * price["input"]
                            + u["cached_tokens"] * price["cached_input"] + u["completion_tokens"] * price["output"]) / 1_000_000
        else:
            data["cost"] = None
        db.execute("UPDATE calls SET data=? WHERE id=?", (_dump(data), call_id))


def continue_task(task_id, session, version, limits):
    limits = validate_limits(limits)
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        data = _task(db, task_id)
        if not data or data["session_id"] != session:
            raise ValueError("任务不属于当前会话")
        if data["state"] != "paused" or data["version"] != version:
            raise ValueError("任务状态已变化，请刷新后重试")
        elapsed = _elapsed(data)
        totals = _summary(db, task_id)
        used = {"tokens": totals["usage"]["total_tokens"], "cost": totals["cost"],
                "model_calls": data["model_calls"], "tool_calls": data["tool_calls"], "seconds": elapsed}
        if any(limit > 0 and limit <= used[key] for key, limit in limits.items()):
            raise ValueError("新额度须大于已消耗量，或设为 0（无限制）")
        if limits["cost"] > 0 and totals["unknown_price_calls"]:
            raise ValueError("已有调用缺少价格，无法按费用继续限制；请将本任务费用限额设为无限制")
        data["paused_seconds"] += max(0, time.time() - data["pause_started"])
        data.update(state="running", reason=None, pause_started=None, limits=limits, version=version + 1)
        _put_task(db, data)
    return status(task_id)


def finish(task_id, state):
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        data = _task(db, task_id)
        if data:
            elapsed = _elapsed(data)
            data.update(elapsed=elapsed, state=state, version=data["version"] + 1)
            _put_task(db, data)
            if state in {"cancelled", "failed", "suspended"}:
                for row in db.execute("SELECT id,data FROM calls WHERE task=?", (task_id,)).fetchall():
                    call = json.loads(row[1])
                    if call["state"] == "running":
                        call["state"] = "unknown"
                        db.execute("UPDATE calls SET data=? WHERE id=?", (_dump(call), row[0]))


def session_summary(session):
    with database() as db:
        baseline = db.execute("SELECT baseline FROM sessions WHERE session=?", (session,)).fetchone()
        if not baseline:
            return {"tracked": False, "tasks": [], "usage": normalize_usage({})}
        usage = json.loads(baseline[0])
        tasks = []
        total_cost, unknown, estimated = 0, 0, 0
        for row in db.execute("SELECT data FROM tasks WHERE session=?", (session,)):
            data = json.loads(row[0])
            summary = _summary(db, data["id"])
            _add(usage, summary["usage"])
            total_cost += summary["cost"]
            unknown += summary["unknown_price_calls"]
            estimated += summary["estimated_calls"]
            tasks.append({**data, **summary, "elapsed": _elapsed(data)})
        currency = _setting(db, "pricing", {"currency": "CNY"})["currency"]
    return {"tracked": True, "usage": usage, "cost": total_cost, "currency": currency,
            "unknown_price_calls": unknown, "estimated_calls": estimated, "legacy_usage": json.loads(baseline[0]),
            "tasks": sorted(tasks, key=lambda t: t["started"])}


def delete_session(session):
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        for row in db.execute("SELECT id FROM tasks WHERE session=?", (session,)).fetchall():
            db.execute("DELETE FROM calls WHERE task=?", (row[0],))
            db.execute("DELETE FROM tools WHERE task=?", (row[0],))
        db.execute("DELETE FROM tasks WHERE session=?", (session,))
        db.execute("DELETE FROM sessions WHERE session=?", (session,))
        db.execute("DELETE FROM settings WHERE scope=?", ("session:" + session,))
