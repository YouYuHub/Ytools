"""Bounded, prompt-free API timings. Context never spans a generator yield."""
import contextvars
import json
import time
from collections import deque

from util.logger import get_logger

logger = get_logger("api_timing")
CURRENT = contextvars.ContextVar("api_send_timing", default=None)
_RECENT = deque(maxlen=100)


class SendTiming:
    def __init__(self, request):
        self.started = getattr(request, "_api_send_origin", None) or time.perf_counter()
        request._api_send_origin = None
        self.stages, self.attempts, self.finished = {}, [], False
        self.session = str(getattr(request, "session_id", "") or "")[:160]

    def mark(self, name, **values):
        target = self.attempts[-1] if self.attempts else self.stages
        if name + "_ms" not in target:
            target[name + "_ms"] = round((time.perf_counter() - self.started) * 1000, 3)
        target.update(values)

    def attempt(self, protocol):
        if len(self.attempts) >= 10:
            self.attempts.pop(0)
        self.attempts.append({"protocol": protocol})
        self.mark("attempt_start")

    def finish(self):
        if self.finished:
            return
        self.finished = True
        record = {"session_id": self.session, "elapsed_ms": round((time.perf_counter() - self.started) * 1000, 3),
                  "stages": self.stages, "attempts": self.attempts}
        _RECENT.append(record)
        try:
            logger.info("API latency %s", json.dumps(record, ensure_ascii=False))
        except Exception:
            pass  # Diagnostics must never replace an API result or exception.


def mark(name, **values):
    timing = CURRENT.get()
    if timing is not None:
        timing.mark(name, **values)


def attempt(protocol):
    timing = CURRENT.get()
    if timing is not None:
        timing.attempt(protocol)


def tool_result_emitted(request):
    request._api_send_origin = time.perf_counter()


def _observe(frame):
    timing = CURRENT.get()
    if timing is None or (timing.attempts and "first_visible_delta_ms" in timing.attempts[-1]):
        return
    if not isinstance(frame, str):
        return
    for line in frame.splitlines():
        if not line.startswith("data:") or line[5:].strip() == "[DONE]":
            continue
        try:
            data = json.loads(line[5:].strip())
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict) and any(data.get(key) for key in (
                "content", "reasoning_content", "tool_calls", "tool_call_preview")):
            mark("first_visible_delta")
            return


def traced_call(invoke, request, stream):
    timing = SendTiming(request)
    if not stream:
        token = CURRENT.set(timing)
        try:
            return invoke()
        finally:
            timing.finish()
            CURRENT.reset(token)

    async def generate():
        upstream = None
        try:
            token = CURRENT.set(timing)
            try:
                upstream = invoke()
                timing.mark("meter_ready")
            finally:
                CURRENT.reset(token)
            iterator = upstream.__aiter__()
            while True:
                token = CURRENT.set(timing)
                try:
                    frame = await iterator.__anext__()
                    _observe(frame)
                except StopAsyncIteration:
                    break
                finally:
                    CURRENT.reset(token)
                yield frame
        finally:
            try:
                if upstream is not None:
                    await upstream.aclose()
            finally:
                timing.finish()
    return generate()
