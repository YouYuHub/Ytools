"""Shared parent/child metering and cooperative budget boundaries for all APIs."""
import asyncio
import contextvars
import json
import math
import time
import uuid
from contextlib import asynccontextmanager

from memory import task_cost_store as store

CURRENT = contextvars.ContextVar("task_cost_tracker", default=None)
MODEL_METER = contextvars.ContextVar("task_model_meter", default=None)


async def network_attempt():
    meter = MODEL_METER.get()
    if meter:
        if meter.network_started:
            meter.settle("unknown")
            await meter.start()
        meter.network_started = True


def network_attempt_sync():
    meter = MODEL_METER.get()
    if meter:
        if meter.network_started:
            meter.settle("unknown")
            meter.start_sync()
        meter.network_started = True


class BudgetTracker:
    def __init__(self, task_id, session, stop=None, emit=None, permission=None):
        self.id, self.session = task_id, session
        self.stop = stop or (lambda: False)
        self.emit, self.permission = emit, permission
        self.last_version = None
        self.agent_pause_baselines = {"main": 0}

    def _reserve(self, **kwargs):
        if self.stop():
            raise asyncio.CancelledError()
        call = kwargs.get("call")
        if call:
            pricing = store.config()["pricing"]
            call["price"] = pricing["models"].get(call["model"])
            call["currency"] = pricing["currency"]
        waiting = self.permission.paused_time("main", include_budget=False) if self.permission else 0
        return store.reserve(self.id, waiting_seconds=waiting, **kwargs)

    async def notify(self):
        if self.emit:
            task = store.status(self.id)
            if task:
                result = self.emit({"event": "task_usage", "task": task})
                if hasattr(result, "__await__"):
                    await result

    async def gate(self, **kwargs):
        while True:
            result = self._reserve(**kwargs)
            if result["allowed"]:
                return
            if result.get("state") not in {"paused"}:
                raise asyncio.CancelledError()
            status = store.status(self.id)
            if status["version"] != self.last_version:
                self.last_version = status["version"]
                await self.notify()
            await asyncio.sleep(0.25)

    def gate_sync(self, **kwargs):
        # Used by title/local compaction's to_thread calls; HTTP reads the same DB.
        while True:
            result = self._reserve(**kwargs)
            if result["allowed"]:
                return
            if result.get("state") != "paused":
                if result.get("state") == "completed" or (kwargs.get("call") or {}).get("role") == "title":
                    raise RuntimeError("任务预算不足，跳过辅助模型调用")
                raise asyncio.CancelledError()
            time.sleep(0.25)

    def register_agent(self, agent):
        self.agent_pause_baselines.setdefault(agent, self.paused_seconds())

    def paused_seconds(self, agent=None):
        data = store.status(self.id)
        if not data:
            return 0
        total = data["paused_seconds"] + (max(0, time.time() - data["pause_started"]) if data.get("pause_started") else 0)
        return max(0, total - self.agent_pause_baselines.get(agent, total)) if agent else total


def current(request=None):
    tracker = CURRENT.get()
    if tracker:
        return tracker
    from .permission_runtime import CURRENT as PERMISSION
    runtime = PERMISSION.get()
    if runtime:
        return getattr(runtime, "costs", None)
    # Title requests run in the web process, outside the worker's ContextVars.
    if request is not None and getattr(request, "usage_role", "main") == "title":
        task = store.latest(request.session_id, kind="chat")
        if task and task["kind"] == "chat":
            # A title finishing after the chat still belongs to that chat task.
            return BudgetTracker(task["id"], request.session_id)
    return None


@asynccontextmanager
async def auxiliary_task(session, manager):
    baseline = None
    if not store.has_session(session):
        baseline = store.legacy_baseline(await manager.get_file_text(), await manager.get_session_meta())
    task_id = "manual_compaction_" + uuid.uuid4().hex
    store.create_task(task_id, session, baseline=baseline, kind="manual_compaction")
    stopped = False
    token = CURRENT.set(BudgetTracker(task_id, session, lambda: stopped))
    final = "completed"
    try:
        yield
    except BaseException as exc:
        final = "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
        raise
    finally:
        stopped = True
        existing = store.status(task_id)
        store.finish(task_id, "cancelled" if existing and existing["state"] == "cancelled" else final)
        CURRENT.reset(token)


class ModelMeter:
    def __init__(self, tracker, request, config):
        self.tracker, self.request = tracker, request
        self.role = getattr(request, "usage_role", "main")
        self.agent = getattr(request, "usage_agent", "main")
        self.model = json.dumps([config.get("selected_provider_name", ""),
                                 config.get("selected_model_name") or config.get("selected_model_id") or ""], ensure_ascii=False)
        prices = store.config()["pricing"]
        self.price = prices["models"].get(self.model)
        self.currency = prices["currency"]
        from .chat_runtime import estimate_request_context_tokens
        messages = [m.model_dump(exclude_none=True) if hasattr(m, "model_dump") else m for m in (request.messages or [])]
        self.input_estimate = estimate_request_context_tokens(messages, request.tools)
        self.id = None
        self.events, self.output_ascii, self.output_non_ascii = {}, 0, 0
        self.completed = False
        self.network_started = False

    def call(self):
        self.id = uuid.uuid4().hex
        self.network_started = False
        self.events, self.output_ascii, self.output_non_ascii, self.completed = {}, 0, 0, False
        return {"id": self.id, "role": self.role, "agent": self.agent, "model": self.model,
                "price": self.price, "currency": self.currency, "usage": store.normalize_usage({"prompt_tokens": self.input_estimate}),
                "cost": None, "source": "unknown", "state": "running"}

    async def start(self):
        call = self.call()
        from security import quotas
        output = getattr(self.request, "max_output_tokens", None) or getattr(self.request, "max_tokens", 0) or 0
        await asyncio.to_thread(quotas.reserve, self.id, self.input_estimate + output)
        try:
            await self.tracker.gate(call=call)
            from security.resource_limits import acquire
            await acquire(self.id, self.tracker.stop)
        except BaseException:
            await asyncio.to_thread(self.cancel_start)
            raise

    def start_sync(self):
        call = self.call()
        from security import quotas
        output = getattr(self.request, "max_output_tokens", None) or getattr(self.request, "max_tokens", 0) or 0
        quotas.reserve(self.id, self.input_estimate + output)
        try:
            self.tracker.gate_sync(call=call)
            from security.resource_limits import acquire_sync
            acquire_sync(self.id, self.tracker.stop)
        except BaseException:
            self.cancel_start()
            raise

    def cancel_start(self):
        """Waiting for a slot never consumes a provider or task token budget."""
        from security import quotas
        from security.resource_limits import release
        try:
            store.settle(self.id, {}, source="not_sent", state="cancelled")
        finally:
            try:
                quotas.settle(self.id, 0, sent=False, complete=False)
            finally:
                release(self.id)
                self.id = None

    def feed(self, frame):
        if not isinstance(frame, dict):
            return False
        self.completed = self.completed or bool(frame.get("finish_reason") or frame.get("response_complete"))
        usage = frame.get("usage")
        reported = usage.get("messages_usage", usage) if isinstance(usage, dict) else None
        valid_usage = isinstance(reported, dict) and any(
            isinstance(reported.get(key), (int, float)) and not isinstance(reported.get(key), bool)
            and math.isfinite(reported[key]) and reported[key] >= 0
            for key in ("total_tokens", "prompt_tokens", "input_tokens", "completion_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        if valid_usage:
            # Snapshots replace previous usage for the same completion id.
            key = str(frame.get("id") or "anonymous")
            self.events[key] = store.normalize_usage(usage)
        for key in ("content", "reasoning_content", "tool_calls"):
            value = frame.get(key)
            if value:
                text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
                ascii_count = sum(ord(c) < 128 for c in text)
                self.output_ascii += ascii_count
                self.output_non_ascii += len(text) - ascii_count
        return bool(frame.get("error") and frame.get("retrying")) or (isinstance(frame.get("warning"), dict)
                    and frame["warning"].get("code") == "MEDIA_LIMIT_DOWNGRADE")

    def settle(self, state="completed"):
        if not self.id:
            return
        total = store.normalize_usage({})
        for usage in self.events.values():
            for key, number in usage.items():
                total[key] += number
        source = "reported" if self.events else "estimated"
        if state == "unknown" and self.events and not self.completed:
            source = "partial"
        if not self.events:
            total = store.normalize_usage({"prompt_tokens": self.input_estimate,
                                          "completion_tokens": (self.output_ascii + 3) // 4 + self.output_non_ascii})
        from security import quotas
        from security.resource_limits import release
        try:
            store.settle(self.id, total, source=source, state=state)
            quotas.settle(self.id, total["total_tokens"], sent=self.network_started,
                          complete=bool(self.events and (self.completed or state == "completed")))
        finally:
            release(self.id)
            self.id = None


def tracked_call(invoke, request, stream, stop_checker, config):
    tracker = current(request)
    if tracker is None:
        from security.principal import current as identity
        if identity() is None:
            return invoke()
        # Auxiliary calls outside a task still count toward the account limit.
        class AccountOnly:
            id = session = ""
            emit = permission = None
            stop = staticmethod(lambda: bool(stop_checker and stop_checker()))
            async def gate(self, **kwargs): pass
            def gate_sync(self, **kwargs): pass
            async def notify(self): pass
        tracker = AccountOnly()
    meter = ModelMeter(tracker, request, config)
    # Preserve child-specific stop checks during a budget pause.
    effective = (BudgetTracker(tracker.id, tracker.session,
        lambda: tracker.stop() or bool(stop_checker and stop_checker()), tracker.emit, tracker.permission)
        if tracker.id else tracker)
    meter.tracker = effective
    if not stream:
        meter.start_sync()
        token = MODEL_METER.set(meter)
        try:
            result = invoke()
            meter.feed(result)
            return result
        except BaseException:
            meter.settle("unknown")
            raise
        finally:
            meter.settle()
            MODEL_METER.reset(token)

    async def generate():
        await meter.start()
        upstream = None
        try:
            upstream = invoke()
            iterator = upstream.__aiter__()
            while True:
                # Each __anext__ may run in a separate wait_for Task. A ContextVar
                # token must never survive a yield into a different Context.
                token = MODEL_METER.set(meter)
                try:
                    chunk = await iterator.__anext__()
                except StopAsyncIteration:
                    break
                finally:
                    MODEL_METER.reset(token)
                retry = False
                for line in chunk.splitlines():
                    if line.startswith("data:") and line[5:].strip() == "[DONE]":
                        meter.completed = True
                    if line.startswith("data:") and line[5:].strip() != "[DONE]":
                        try:
                            retry = meter.feed(json.loads(line[5:].strip())) or retry
                        except (ValueError, TypeError):
                            pass
                yield chunk
                if retry:
                    meter.settle("unknown")
                    await effective.notify()
        except BaseException:
            meter.settle("completed" if meter.completed else "unknown")
            raise
        finally:
            if upstream is not None:
                token = MODEL_METER.set(meter)
                try:
                    await upstream.aclose()
                finally:
                    MODEL_METER.reset(token)
            meter.settle()
            await effective.notify()
    return generate()
