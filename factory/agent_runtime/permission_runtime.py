"""Async approvals and durable execution ownership, shared by parent/children."""
import asyncio
import contextvars
import copy
import json
import os
import time
from pathlib import Path

from memory import permission_store as store
from factory.agent_runtime.permission_policy import inside, protected, plan_tool, fingerprint, server_fingerprint

CURRENT = contextvars.ContextVar("tool_permission_runtime", default=None)
WAITERS = {}


class PermissionSuspended(asyncio.CancelledError):
    pass


def notify(approval_id):
    waiter = WAITERS.get(approval_id)
    if waiter and not waiter.done():
        waiter.set_result(True)


def denied(message="用户拒绝本次调用", code="PERMISSION_DENIED"):
    return {"success": False, "code": code, "executed": False, "error": message}


def current_policy_mode(session):
    """读取当前生效权限模式，绕过任务启动时的偏好缓存。

    全局模式存在账号偏好里并缓存在 principal 上；进行中任务必须绕过
    缓存才能让用户刚改的权限立即生效。会话级覆盖走 DB，天然实时。
    """
    from security.principal import current
    principal = current()
    if principal is not None:
        principal.resource_cache.pop("account_preferences", None)
    return store.policy(session)["mode"]


def interrupted_tool_result(task_id, agent, call_id, reason):
    """Describe a durable result without re-executing a tool after a crash."""
    execution = store.execution(task_id, agent, call_id)
    if execution and execution["state"] == "completed":
        from factory.chat_factory import _format_tool_result
        result = execution["result"]
        return _format_tool_result(result), isinstance(result, dict) and bool(result.get("error"))
    return (reason + "；工具执行结果未知，请核对实际效果，不要直接重复执行" if execution and execution["state"] == "executing"
            else reason + "；子任务部分操作可能已完成，请先核对" if execution
            else reason + "；本次工具未执行"), True


def reconcile_interrupted_round(pending, task_id, reason):
    """Fill missing tool results from the ledger; retain every saved event."""
    result = copy.deepcopy(pending)
    events = result.get("events") or []
    paired = {event.get("tool_call_id") for event in events if isinstance(event, dict) and event.get("role") == "tool"}
    reconciled, outstanding = [], []
    def flush():
        for call in outstanding:
            call_id = call.get("id")
            if not call_id or call_id in paired:
                continue
            content, is_error = interrupted_tool_result(task_id, "main", call_id, reason)
            reconciled.append({"role":"tool", "tool_call_id":call_id,
                               "name":(call.get("function") or {}).get("name", ""),
                               "content":content, "tool_is_error":is_error})
            paired.add(call_id)
        outstanding.clear()
    for event in events:
        if isinstance(event, dict) and event.get("role") in {"assistant", "user"}:
            flush()
        reconciled.append(event)
        if isinstance(event, dict) and event.get("role") == "assistant":
            outstanding.extend(call for call in event.get("tool_calls") or [] if isinstance(call, dict))
    flush()
    reconciled.append({"role":"assistant", "content":reason + "。以下新消息将基于已保存的历史继续；已完成的操作保留，结果未知的操作应先核对。"})
    result["events"] = reconciled
    return result


def close_pending_native(task_id, session, reason="任务已停止"):
    """Retire provider tool slots without inventing execution of unfinished tools."""
    from chat.protocols import native_store
    from chat.protocols.responses_store import tool_result
    with store.database() as db:
        snapshots = [(row[0], json.loads(row[1])) for row in db.execute("SELECT agent,data FROM checkpoints WHERE task=?", (task_id,))]
    for agent, snapshot in snapshots:
        messages = snapshot.get("messages", [])
        assistant = snapshot.get("assistant") or (messages[-1] if messages else {})
        reference = assistant.get("provider_state")
        if not reference:
            continue
        record = native_store.load_record(session, reference) if reference.get("protocol") != "responses" else None
        for call in assistant.get("tool_calls") or []:
            if ((record and (record.get("tool_status", {}).get(call["id"], {}).get("status") == "completed"))
                    or (reference.get("protocol") == "responses" and tool_result(session, reference, call["id"]))):
                continue
            content, is_error = interrupted_tool_result(task_id, agent, call["id"], reason)
            native_store.tool_completed(session, reference, call["id"], content, is_error)


def reset_unexecuted_native(task_id):
    """Rewind natively started Responses slots the ledger proves never executed.

    Suspended tasks keep native running slots so a resume can continue them,
    but the replay re-runs tool_started, which refuses running/unknown slots.
    The execution ledger is authoritative: a slot without an execution record
    (or with a completed cache / replayable dispatch) rewinds to planned so
    the replay re-requests approval and runs once. Slots with an 'executing'
    record keep their unknown state and the replay still refuses them.
    Returns the number of rewound slots.
    """
    from chat.protocols.responses_store import reset_tool
    saved = store.task(task_id)
    if not saved:
        return 0
    session = saved["session"]
    with store.database() as db:
        snapshots = [(row[0], json.loads(row[1])) for row in db.execute("SELECT agent,data FROM checkpoints WHERE task=?", (task_id,))]
    rewound = 0
    for agent, snapshot in snapshots:
        messages = snapshot.get("messages", [])
        assistant = snapshot.get("assistant") or (messages[-1] if messages else {})
        reference = assistant.get("provider_state")
        if not reference or reference.get("protocol") != "responses":
            continue
        for call in assistant.get("tool_calls") or []:
            call_id = call.get("id") if isinstance(call, dict) else None
            if not call_id:
                continue
            execution = store.execution(task_id, agent, call_id)
            if execution and execution["state"] == "executing":
                continue
            if reset_tool(session, reference, call_id):
                rewound += 1
    return rewound


def guard_path(path, *, search=False):
    current = CURRENT.get()
    if current is not None:
        # 进行中任务权限变更立即生效：路径守卫每次都刷新模式，
        # 切到完全控制后长任务内的后续文件访问不再被旧快照拦截。
        try:
            current.refresh_mode()
        except Exception:
            pass
    if not current or current.context["mode"] == "full_control":
        return True
    target = str(Path(path).resolve())
    if str(Path(current.context["root"]).resolve()) != current.context["root"]:
        raise PermissionError("工作目录真实位置已变化，请重新配置任务")
    allowed = inside(target, current.context["root"]) and not protected(target)
    plan = current.active_plan.get()
    if not allowed and plan:
        allowed = plan["needs_approval"] and (target in plan["paths"] or (
            plan["tool"] in {"search_files", "list_files"} and not protected(target)
            and any(inside(target, root) for root in plan["paths"])))
    if target in current.media_grants:
        allowed = True
    if not allowed:
        if search:
            return False
        raise PermissionError("路径未获本次工具授权: " + target)
    return True


def permission_urlopen(request, *, timeout):
    """Only the approved URL; cross-origin redirects require a new invocation."""
    import urllib.request
    from urllib.parse import urlsplit
    current = CURRENT.get()
    from security.principal import current as account_principal
    if account_principal() is not None:
        source = request.full_url if isinstance(request, urllib.request.Request) else request
        if current:
            try:
                current.refresh_mode()
            except Exception:
                pass
        if current and current.context["mode"] != "full_control":
            plan = current.active_plan.get()
            if source not in current.network_grants and (not plan or source not in plan["sources"]):
                raise PermissionError("网络媒体来源未获本次授权")
        from security.network import account_urlopen
        return account_urlopen(request, timeout=timeout)
    if current:
        try:
            current.refresh_mode()
        except Exception:
            pass
    if not current or current.context["mode"] == "full_control":
        return urllib.request.urlopen(request, timeout=timeout)
    source = request.full_url if isinstance(request, urllib.request.Request) else request
    plan = current.active_plan.get()
    if source not in current.network_grants and (not plan or source not in plan["sources"]):
        raise PermissionError("网络媒体来源未获本次授权")
    origin = urlsplit(source)
    class ApprovedRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            target = urlsplit(newurl)
            if (target.scheme, target.hostname, target.port) != (origin.scheme, origin.hostname, origin.port):
                raise PermissionError("媒体跳转到其他来源，需要对新 URL 重新确认")
            return super().redirect_request(req, fp, code, msg, headers, newurl)
    return urllib.request.build_opener(ApprovedRedirect).open(request, timeout=timeout)


class PermissionRuntime:
    def __init__(self, session, request, emit, stop, *, resume_id=None):
        self.emit, self.stop, self.session = emit, stop, session
        self.waiting = 0
        self.wait_seconds = 0.0
        self.agent_waits = {}
        self.agent_wait_totals = {}
        self.active_plan = contextvars.ContextVar("active_tool_plan", default=None)
        if resume_id:
            self.task = store.adopt(resume_id)
            if not self.task or self.task["session"] != session or self.task["state"] != "running":
                raise ValueError("恢复任务身份无效")
            # Rewind natively started slots the ledger proves never executed;
            # otherwise the replay trips the unknown-execution guard and the
            # resumed Responses task fails permanently (suspend/resume deadlock).
            reset_unexecuted_native(resume_id)
        else:
            self.task = store.create_task(session, {"request": request, "mode": store.policy(session)["mode"], "root": str(Path.cwd().resolve())})
        self.context = {"session": session, "root": self.task["data"]["root"], "mode": self.task["data"]["mode"]}
        if not Path(self.context["root"]).is_dir():
            raise ValueError("任务原工作目录已失效，请重新选择目录")
        self.id, self.epoch = self.task["id"], self.task["epoch"]
        self.plans = {}
        self.ui_locations = {}
        self.media_grants = set()
        self.network_grants = set()
        self.token = CURRENT.set(self)

    def save(self, agent, snapshot):
        store.checkpoint(self.id, agent, snapshot)
        self.ui_locations[agent] = {key: snapshot[key] for key in ("round", "seq", "parent_round_number", "parent_tool_call_id") if key in snapshot}

    def bind_config(self, agent, config):
        key = "config:" + agent
        value = fingerprint(config)
        saved = store.load_checkpoint(self.id, key)
        if saved and saved["fingerprint"] != value:
            raise ValueError("模型或工具配置已变化，不能继续旧任务，请放弃后重新发送")
        self.save(key, {"fingerprint": value})

    def child_id(self, call_id, proposed):
        key = "dispatch:" + call_id
        cached = store.load_checkpoint(self.id, key)
        if cached:
            return cached["agent_id"]
        self.save(key, {"agent_id": proposed})
        return proposed

    async def event(self, approval, phase):
        await self.emit({"event": "tool_approval", "phase": phase, "approval": {
            "id": approval["id"], "task_id": self.id, "epoch": self.epoch, "session_id": self.session,
            "agent_id": approval["agent"], "tool_call_id": approval["call_id"], "state": approval["state"], **approval["plan"],
        }})

    async def wait(self, request):
        started = time.monotonic()
        agent = request["agent"]
        self.agent_waits[agent] = started
        future = asyncio.get_running_loop().create_future()
        WAITERS[request["id"]] = future
        self.waiting += 1
        await self.event(request, "requested")
        try:
            # Register before checking the committed decision (early IPC is safe).
            current = store.approval(request["id"])
            def _auto_approve_on_full_control():
                # 用户在等待期间切换为完全控制：自动放行本次审批，立即生效。
                # 任务已停止时不放行；读库失败则保持等待。
                if self.stop():
                    return False
                try:
                    if current_policy_mode(self.session) != "full_control":
                        return False
                except Exception:
                    return False
                try:
                    with store.database() as db:
                        db.execute("UPDATE approvals SET state='approved',updated=? WHERE id=? AND state='pending'",
                                   (time.time(), request["id"]))
                except Exception:
                    return False
                try:
                    self.refresh_mode()
                except Exception:
                    pass
                return True
            while current["state"] == "pending" and current["task_state"] == "running" and not self.stop():
                # 先检查一次：authorize 与 wait 之间切换权限也能立即放行，不用等 1 秒。
                if _auto_approve_on_full_control():
                    break
                # IPC wakes promptly; the durable decision also wakes a worker
                # when a notification was lost during process startup/reconnect.
                await asyncio.wait({future}, timeout=1.0)
                if _auto_approve_on_full_control():
                    break
                current = store.approval(request["id"])
                if future.done():
                    break
            current = store.approval(request["id"])
            if self.stop() or current["task_state"] != "running" or current["epoch"] != self.epoch or current["state"] not in {"approved", "denied"}:
                return denied("任务已停止或审批已失效", "APPROVAL_CANCELLED")
            await self.event(current, "resolved")
            return None if current["state"] == "approved" else denied()
        finally:
            WAITERS.pop(request["id"], None)
            self.waiting -= 1
            self.wait_seconds += time.monotonic() - started
            self.agent_wait_totals[agent] = self.agent_wait_totals.get(agent, 0) + time.monotonic() - started
            self.agent_waits.pop(agent, None)

    def paused_time(self, agent, *, include_budget=True):
        budget = getattr(self, "costs", None)
        return (self.agent_wait_totals.get(agent, 0)
                + (time.monotonic() - self.agent_waits[agent] if agent in self.agent_waits else 0)
                + (budget.paused_seconds(agent) if budget and include_budget else 0))

    def refresh_mode(self):
        """进行中任务刷新权限模式：每批工具调用前调用，变更立即生效。"""
        try:
            mode = current_policy_mode(self.session)
        except Exception:
            return
        if mode == self.context.get("mode"):
            return
        self.context["mode"] = mode
        try:
            with store.database() as db:
                row = db.execute("SELECT data FROM tasks WHERE id=?", (self.id,)).fetchone()
                if row:
                    data = json.loads(row[0])
                    data["mode"] = mode
                    db.execute("UPDATE tasks SET data=?,updated=? WHERE id=?",
                               (store.encode(data), time.time(), self.id))
        except Exception:
            pass

    async def authorize(self, calls, servers, originals=None, agent="main"):
        self.refresh_mode()
        allowed, rejected, requests = [], [], []
        pending = []
        for item in calls:
            idx, call, name, args = item
            previous = store.execution(self.id, agent, call.get("id") or "")
            if previous and previous["state"] == "completed":
                rejected.append({"index": idx, "tool_call": call, "tool_name": name, "tool_args": args,
                                 "result": copy.deepcopy(previous["result"]), "error": None, "_permission_cached": True})
            elif previous and previous["state"] == "executing":
                raise RuntimeError("工具执行结果未知，不能自动重跑，请核对后重新规划")
            else:
                pending.append(item)
        from .tool_validation import prepare_builtin_calls
        calls, invalid = prepare_builtin_calls(pending)
        for item in invalid:
            store.completed(self.id, agent, item["tool_call"]["id"], item["result"])
        rejected.extend(invalid)
        for idx, call, name, args in calls:
            call_id = call.get("id") or ""
            previous = store.execution(self.id, agent, call_id)
            if previous and previous["state"] == "completed":
                rejected.append({"index": idx, "tool_call": call, "tool_name": name, "tool_args": args,
                                 "result": copy.deepcopy(previous["result"]), "error": None, "_permission_cached": True})
                continue
            if previous and previous["state"] == "executing":
                raise RuntimeError("工具执行结果未知，不能自动重跑，请核对后重新规划")
            try:
                plan = plan_tool(name, args, self.context, server=servers.get(name), original_name=(originals or {}).get(name))
            except (ValueError, OSError, RuntimeError) as error:
                rejected.append({"index": idx, "tool_call": call, "tool_name": name, "tool_args": args,
                                 "result": denied(str(error), "INVALID_TOOL_PLAN"), "error": None})
                continue
            checkpoint = self.ui_locations.get(agent, {})
            main_checkpoint = self.ui_locations.get("main", {})
            plan["round"] = checkpoint.get("parent_round_number") or main_checkpoint.get("round")
            plan["tool_index"] = idx
            if agent != "main":
                plan["seq"] = checkpoint.get("seq")
                plan["parent_tool_call_id"] = checkpoint.get("parent_tool_call_id", "")
            self.plans[(agent, call_id)] = plan
            item = (idx, call, name, plan["args"])
            if plan["needs_approval"]:
                request = store.request_approval(self.id, agent, call_id, self.epoch, plan)
                requests.append((item, request))
            else:
                allowed.append(item)
        # All requests exist durably before waiting for the first one.
        for item, request in requests:
            result = await self.wait(request)
            if result is None:
                allowed.append(item)
            else:
                idx, call, name, args = item
                store.completed(self.id, agent, call["id"], result)
                rejected.append({"index": idx, "tool_call": call, "tool_name": name, "tool_args": args, "result": result, "error": None})
        budget = getattr(self, "costs", None)
        if budget and allowed:
            await budget.gate(tools=[agent + ":" + call["id"] for _, call, _, _ in allowed])
        for _, call, name, _ in allowed:
            if name == "read_media":
                self.media_grants.update(self.plans[(agent, call["id"])]["paths"])
                self.network_grants.update(self.plans[(agent, call["id"])]["sources"])
        return sorted(allowed, key=lambda item: item[0]), rejected

    def begin_call(self, agent, call_id):
        self.refresh_mode()
        plan = self.plans[(agent, call_id)]
        if plan["server_fingerprint"] != server_fingerprint(plan["server"]):
            raise PermissionError("MCP 服务配置已变化，请重新发起调用并确认")
        for path in plan["paths"]:
            if str(Path(path).resolve()) != path:
                raise PermissionError("目标真实路径已变化，请重新确认")
        self.active_plan.set(plan)
        previous = store.execution(self.id, agent, call_id)
        if not previous:
            store.claim_execution(self.id, self.epoch, agent, call_id)
        if plan["tool"] == "sub_agent":
            # Internal dispatch is replayable; child side effects have their own ledger.
            with store.database() as db:
                db.execute("UPDATE executions SET state='delegating' WHERE task=? AND agent=? AND call_id=?", (self.id, agent, call_id))

    def finish_call(self, agent, call_id, result):
        store.completed(self.id, agent, call_id, result)
        with store.database() as db:
            db.execute("UPDATE approvals SET state='completed' WHERE task=? AND agent=? AND call_id=? AND epoch=?", (self.id, agent, call_id, self.epoch))

    def close(self, state):
        if state != 'suspended':
            from .command_leases import cancel_owned
            cancel_owned(self.id)
        if state in {"failed", "cancelled"}:
            try:
                close_pending_native(self.id, self.session, "任务异常结束" if state == "failed" else "用户停止任务")
            except (OSError, ValueError):
                from util.logger import get_logger
                get_logger(__name__).warning("关闭原生工具槽失败；旧工具不会自动重跑")
        store.set_task_state(self.id, state)
        CURRENT.reset(self.token)

    async def retire_agent(self, agent):
        """A finished/stopped child cannot retain actionable tool approvals."""
        from .command_leases import cancel_owned
        await asyncio.to_thread(cancel_owned, self.id, agent)
        with store.database() as db:
            ids = [row[0] for row in db.execute(
                "SELECT id FROM approvals WHERE task=? AND agent=? AND epoch=? AND state IN ('pending','approved','denied')",
                (self.id, agent, self.epoch))]
            db.execute("UPDATE approvals SET state='cancelled' WHERE task=? AND agent=? AND epoch=? AND state IN ('pending','approved','denied')",
                       (self.id, agent, self.epoch))
        for identifier in ids:
            await self.event(store.approval(identifier), "cancelled")


class DurableResults(list):
    """Commit each completed builtin immediately, before a later child can wait."""
    def __init__(self, runtime, agent, values=(), *, tuples=False):
        super().__init__(values)
        self.runtime, self.agent, self.tuples = runtime, agent, tuples

    def append(self, value):
        if self.runtime:
            call, result = (value[1], value[4]) if self.tuples else (value["tool_call"], value["result"])
            self.runtime.finish_call(self.agent, call["id"], result)
        super().append(value)


async def replay_stream(snapshot):
    """Replay a saved model completion locally; no provider request or usage charge."""
    assistant = snapshot["assistant"]
    value = {key: assistant[key] for key in ("content", "reasoning_content", "tool_calls", "provider_state") if assistant.get(key)}
    if value.get("tool_calls"):
        value["tool_calls"] = [dict(call, index=index) for index, call in enumerate(value["tool_calls"])]
    value["finish_reason"] = "tool_calls"
    yield "data: " + json.dumps(value, ensure_ascii=False) + "\n\n"


async def run_with_active_timeout(runner, timeout):
    started = getattr(runner, "_started_monotonic", time.monotonic())
    running = asyncio.create_task(runner.run())
    try:
        while not running.done():
            await asyncio.wait({running}, timeout=0.1)
            if callable(getattr(runner, "active_seconds", None)):
                elapsed = runner.active_seconds()
            else:
                permission = getattr(runner, "permission_runtime", None)
                paused = permission.paused_time(runner.context.agent_id) if permission else 0
                elapsed = time.monotonic() - started - paused
            if elapsed >= timeout and not running.done():
                runner.context.timeout_requested = True
                runner.context.cancel_token.set()
                raise asyncio.TimeoutError()
        return running.result()
    finally:
        if not running.done():
            running.cancel()
            try:
                await running
            except asyncio.CancelledError:
                pass
