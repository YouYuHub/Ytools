"""Cooperative cancellation and deadlines for synchronous built-in tools."""
import asyncio
import contextvars
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field


class ToolCancelledError(RuntimeError):
    pass


@dataclass
class ToolControl:
    cancelled: threading.Event = field(default_factory=threading.Event)
    deadline: float | None = None


CURRENT_CONTROL = contextvars.ContextVar("builtin_tool_control", default=None)


def check_tool_control(*, check_deadline=True):
    control = CURRENT_CONTROL.get()
    if control and control.cancelled.is_set():
        raise ToolCancelledError("任务已停止，工具执行已取消")
    if check_deadline and control and control.deadline is not None and time.monotonic() >= control.deadline:
        raise TimeoutError("工具执行超时")


def remaining_seconds(default):
    check_tool_control()
    control = CURRENT_CONTROL.get()
    if control and control.deadline is not None:
        return max(0.001, min(default, control.deadline - time.monotonic()))
    return default


@contextmanager
def tool_deadline(seconds):
    previous = CURRENT_CONTROL.get()
    deadline = time.monotonic() + seconds
    if previous and previous.deadline is not None:
        deadline = min(deadline, previous.deadline)
    control = ToolControl(previous.cancelled if previous else threading.Event(), deadline)
    token = CURRENT_CONTROL.set(control)
    try:
        check_tool_control()
        yield control
    finally:
        CURRENT_CONTROL.reset(token)


async def run_sync_tool(function, *args, **kwargs):
    """Keep the loop responsive; a cancelled command gets time to reap its tree."""
    control = ToolControl()
    token = CURRENT_CONTROL.set(control)
    try:
        running = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    finally:
        CURRENT_CONTROL.reset(token)
    try:
        return await asyncio.shield(running)
    except asyncio.CancelledError:
        control.cancelled.set()
        try:
            await asyncio.wait_for(asyncio.shield(running), timeout=20)
        except (Exception, asyncio.CancelledError):
            # Observe a late failure even if the caller has already finished.
            def consume(task):
                if not task.cancelled():
                    task.exception()
            running.add_done_callback(consume)
        raise
