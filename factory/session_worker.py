# coding: utf-8
"""每会话独立 worker 进程：生成任务在此进程内运行，并 os.chdir 到会话工作目录。

设计（对应每会话独立工作目录改造）：
- 主进程（FastAPI）只负责路由、SSE 消费、元数据管理；生成循环迁入
  每会话一个的 worker 进程，worker 收到 generate 命令后：
    1. init_path 刷新 .env / models.json（主进程可能已改全局配置）；
    2. 解析会话生效工作目录（_meta.work_dir 覆盖 → DEFAULT_CHAT_WORK_DIR
       兜底 → 保持当前 cwd），目录失效发 warning 并回退；
    3. os.chdir(生效目录)——此后相对路径、MCP 子进程继承全部天然正确，
       无需在 MCP 调用链上显式传 cwd；
    4. 复用 factory.chat_factory._run_chat_generation 完整生成循环。
- worker 与主进程之间：cmd Pipe（generate/stop/ping/shutdown）+
  evt Pipe（sse/round_start/question_text/task_started/task_done/...）。
- worker 空闲自动退出（进程随用随建），崩溃由主进程 reader 线程感知并
  以合成 task_done 收尾 SSE；JSONL 写入靠跨进程文件锁互斥。

注意：本模块禁止在顶部导入 chat_factory 等重依赖——chat_factory 顶部会
导入本模块（SessionWorkerProxy），worker 内的重导入必须延迟到函数体内，
否则构成循环导入，也会拖慢主进程启动。
"""
# 标准库
import asyncio
import json
import multiprocessing
import os
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# worker 空闲自退出秒数：进程随用随建，避免长期驻留；>0 生效
DEFAULT_WORKER_IDLE_TIMEOUT_SECONDS = 900.0


def _safe_send(conn, payload: dict) -> bool:
    try:
        conn.send(payload)
        return True
    except (OSError, ValueError, RuntimeError):
        return False


# ======================================================================
# worker 进程侧
# ======================================================================

def worker_main(session_id: str, cmd_conn, evt_conn) -> None:
    """spawn 子进程入口：重建项目运行环境后进入命令循环。

    Windows spawn 会重新导入主模块并重建运行时，这里不能依赖父进程内存
    状态（env_vars、会话注册表等），全部在子进程内重新初始化。
    """
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    try:
        from env_manager import init_path
        init_path(PROJECT_ROOT)
    except Exception as exc:
        _safe_send(evt_conn, {"type": "worker_error", "error": f"worker 初始化失败: {exc}"})
        return
    try:
        asyncio.run(_worker_loop(session_id, cmd_conn, evt_conn))
    except KeyboardInterrupt:
        pass
    finally:
        _safe_send(evt_conn, {"type": "worker_exit"})


def _worker_idle_timeout() -> float:
    try:
        from env_manager import load_var
        value = float(
            load_var("CHAT_WORKER_IDLE_TIMEOUT_SECONDS", DEFAULT_WORKER_IDLE_TIMEOUT_SECONDS)
        )
    except Exception:
        return DEFAULT_WORKER_IDLE_TIMEOUT_SECONDS
    return value if value > 0 else DEFAULT_WORKER_IDLE_TIMEOUT_SECONDS


class _WorkerStreamAdapter:
    """worker 内替代 _SessionStream 的事件发射器。

    与生成循环实际用到的 stream 接口鸭子类型兼容：
    emit(chunk) / notify() / set_round_start() / question_text（可写属性）/
    user_stop_requested / finish()。所有事件经 evt 队列由专职写线程送回
    主进程，emit 非阻塞，事件顺序与产生顺序一致。
    """

    def __init__(self, session_id: str, evt_queue: "queue.Queue"):
        self.session_id = session_id
        self._evt_queue = evt_queue
        self._question_text = ""
        self.user_stop_requested = False
        self.done = False
        self._finish_sent = False
        # 运行中注入的用户消息队列（消息引导）：生成循环在每轮检查点取出，
        # 作为新一轮用户消息追加进 messages 并落盘（inject 命令写入）
        self.injected_messages: "queue.Queue" = queue.Queue()

    def pop_injected_message(self) -> dict | None:
        """非阻塞取一条运行中注入的用户消息；队列为空返回 None。"""
        try:
            return self.injected_messages.get_nowait()
        except queue.Empty:
            return None

    @property
    def question_text(self) -> str:
        return self._question_text

    @question_text.setter
    def question_text(self, value: str) -> None:
        value = str(value or "")
        self._question_text = value
        self._evt_queue.put({"type": "question_text", "text": value})

    def emit(self, chunk: str) -> None:
        if isinstance(chunk, str) and chunk:
            self._evt_queue.put({"type": "sse", "chunk": chunk})

    def emit_event(self, payload: dict) -> None:
        self.emit(f"data: {json.dumps(payload, ensure_ascii=False)}\n\n")

    def notify(self) -> None:
        # 唤醒 SSE 消费者是主进程侧 stream 的职责
        return None

    def set_round_start(self) -> None:
        self._evt_queue.put({"type": "round_start"})

    async def finish(self) -> None:
        self.done = True
        if not self._finish_sent:
            self._finish_sent = True
            self._evt_queue.put({"type": "task_done"})


async def _worker_generate(session_id: str, request_data: dict, adapter: _WorkerStreamAdapter) -> None:
    """worker 内执行一次完整生成任务：刷新环境 → 解析会话目录 → chdir → 生成循环。"""
    from config import PROJECT_ROOT as CONFIG_PROJECT_ROOT, ChatLLMRequest
    from env_manager import init_path
    from memory.chat_memory import resolve_session_work_dir

    # 每个任务前刷新 .env / models.json：主进程可能已修改模型/全局配置，
    # 与「每次请求重新解析模型配置」的既有语义保持一致
    try:
        init_path(CONFIG_PROJECT_ROOT)
    except Exception as exc:
        print(f"[WARN] worker 刷新环境配置失败（沿用启动时配置）: {exc}")

    # 会话工作目录解析：_meta.work_dir 覆盖 → DEFAULT_CHAT_WORK_DIR 兜底；
    # 目录失效时发 warning 并回退，不终止任务
    effective_dir, warning = resolve_session_work_dir(session_id)
    if warning:
        adapter.emit_event({
            "warning": {"code": "WORK_DIR_FALLBACK", "message": warning}
        })
    if effective_dir:
        try:
            os.chdir(effective_dir)
            print(f"[INFO] 会话 [{session_id}] 生成任务工作目录: {effective_dir}")
        except OSError as exc:
            adapter.emit_event({
                "warning": {
                    "code": "WORK_DIR_CHDIR_FAILED",
                    "message": f"切换工作目录失败（{effective_dir}）: {exc}",
                }
            })
    tool_request = ChatLLMRequest(**(request_data or {}))
    from factory.chat_factory import _run_chat_generation
    await _run_chat_generation(tool_request, adapter)


async def _interrupt_worker_task(session_id: str, current_task: "asyncio.Task | None", user_stop: bool) -> None:
    """打断 worker 内正在运行的生成任务（新消息打断/手动停止/关停共用）。"""
    if current_task is None or current_task.done():
        return
    try:
        from memory.chat_memory import get_chat_memory_manager
        (await get_chat_memory_manager(session_id)).run_task = False
    except Exception:
        pass
    current_task.cancel()
    try:
        await current_task
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        print(f"[WARN] worker 旧生成任务退出异常（已忽略）: {exc}")


async def _worker_loop(session_id: str, cmd_conn, evt_conn) -> None:
    """worker 命令循环：处理 generate/stop/ping/shutdown，空闲自动退出。"""
    loop = asyncio.get_running_loop()
    cmd_queue: asyncio.Queue = asyncio.Queue()

    def _cmd_reader() -> None:
        while True:
            try:
                cmd = cmd_conn.recv()
            except (EOFError, OSError, ValueError):
                break
            loop.call_soon_threadsafe(cmd_queue.put_nowait, {"__raw__": cmd})
        loop.call_soon_threadsafe(cmd_queue.put_nowait, {"__eof__": True})

    threading.Thread(
        target=_cmd_reader, name=f"chat-worker-cmd-{session_id}", daemon=True
    ).start()

    evt_queue: "queue.Queue[Optional[dict]]" = queue.Queue()

    def _evt_writer() -> None:
        while True:
            item = evt_queue.get()
            if item is None:
                break
            _safe_send(evt_conn, item)

    threading.Thread(
        target=_evt_writer, name=f"chat-worker-evt-{session_id}", daemon=True
    ).start()

    idle_timeout = _worker_idle_timeout()
    current_task: asyncio.Task | None = None
    adapter: _WorkerStreamAdapter | None = None

    try:
        while True:
            try:
                task_active = current_task is not None and not current_task.done()
                timeout = None if task_active else idle_timeout
                cmd = await asyncio.wait_for(cmd_queue.get(), timeout=timeout)
            except asyncio.TimeoutError:
                print(f"[INFO] 会话 [{session_id}] worker 空闲退出")
                break
            if cmd.get("__eof__"):
                break
            raw = cmd.get("__raw__")
            if not isinstance(raw, dict):
                continue
            ctype = raw.get("type")
            try:
                if ctype == "ping":
                    evt_queue.put({"type": "pong"})
                elif ctype == "generate":
                    if current_task is not None and not current_task.done():
                        # 携带新用户消息：平滑打断旧任务并等待其完全收尾，
                        # 避免新旧两个生成循环交错写同一会话
                        if adapter is not None:
                            adapter.user_stop_requested = False
                        await _interrupt_worker_task(session_id, current_task, user_stop=False)
                    adapter = _WorkerStreamAdapter(session_id, evt_queue)
                    evt_queue.put({"type": "task_started"})
                    current_task = asyncio.create_task(
                        _worker_generate(session_id, raw.get("request") or {}, adapter)
                    )
                elif ctype == "inject":
                    # 运行中注入用户消息（消息引导）：投递到当前适配器队列，
                    # 生成循环在下一轮检查点取出并作为新一轮继续
                    message = raw.get("message")
                    if adapter is not None and isinstance(message, dict):
                        adapter.injected_messages.put(message)
                        evt_queue.put({"type": "injected", "ok": True})
                    else:
                        evt_queue.put({"type": "injected", "ok": False})
                elif ctype == "cancel_inject":
                    # 撤回尚未消费的注入消息（前端提示行 ×）：按文本匹配移除
                    # 一条；已被生成循环取走（已消费）时回报 ok=False
                    target = str(raw.get("text") or "").strip()
                    removed = False
                    if adapter is not None and target:
                        kept = []
                        while True:
                            try:
                                item = adapter.injected_messages.get_nowait()
                            except queue.Empty:
                                break
                            if not removed and _injected_content_text(item) == target:
                                removed = True
                                continue
                            kept.append(item)
                        for item in kept:
                            adapter.injected_messages.put(item)
                    evt_queue.put({"type": "injected_cancelled", "ok": removed})
                elif ctype == "stop":
                    reason = "user" if raw.get("reason") == "user" else "interrupt"
                    if current_task is not None and not current_task.done():
                        if adapter is not None:
                            # 手动停止按 stopped 语义落盘；打断按 interrupted 语义
                            adapter.user_stop_requested = (reason == "user")
                        await _interrupt_worker_task(session_id, current_task, user_stop=(reason == "user"))
                    else:
                        evt_queue.put({"type": "task_done", "no_task": True})
                elif ctype == "shutdown":
                    if current_task is not None and not current_task.done():
                        if adapter is not None:
                            adapter.user_stop_requested = False
                        await _interrupt_worker_task(session_id, current_task, user_stop=False)
                    break
                else:
                    print(f"[WARN] worker 收到未知命令: {ctype!r}")
            except Exception as exc:
                print(f"[WARN] worker 处理命令 {ctype!r} 失败: {exc}")
    finally:
        evt_queue.put(None)  # 让 evt 写线程退出


# ======================================================================
# 主进程侧
# ======================================================================

class SessionWorkerProxy:
    """主进程侧的会话 worker 进程代理：进程生命周期 + 命令发送 + 事件分发。

    - 懒启动：首次 start_generation 时 spawn；
    - reader 线程把 evt Pipe 事件分发给注册的 handler（转发进 _SessionStream）；
    - worker 崩溃/退出由 reader 感知，置 alive=False 并合成 task_done，
      保证 SSE 消费端能收到收尾帧；
    - 事件处理在 reader 线程执行，handler 内通过 loop.call_soon_threadsafe /
      run_coroutine_threadsafe 回到主事件循环。
    """

    def __init__(self, session_id: str):
        self.session_id = session_id
        self._process = None
        self._cmd_conn = None
        self._evt_conn = None
        self._state_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._alive = False
        self._generation_running = False
        self._handlers: list[Callable[[dict], None]] = []
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # 是否已真实 spawn 过进程：从未启动的代理不是"死亡代理"，
        # 清理时不能弹出（否则每个请求都会重复 spawn 新 worker）
        self._started = False
        # 当前生成任务对应的主进程侧 _SessionStream：
        # 事件按绑定对象转发而不是按注册表查找——打断旧任务并启动新任务的
        # 过渡窗口里，迟到的旧 task_done 不能误把新 stream 置为完成
        self._bound_stream = None

    def bind_stream(self, stream) -> None:
        self._bound_stream = stream

    # ---- 事件循环绑定 ----
    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    @property
    def loop(self) -> Optional[asyncio.AbstractEventLoop]:
        loop = self._loop
        if loop is not None and loop.is_closed():
            return None
        return loop

    # ---- 进程生命周期 ----
    def _ensure_process(self) -> None:
        with self._state_lock:
            if self._alive and self._process is not None and self._process.is_alive():
                return
            # 清理残留旧进程/旧连接（reader 线程会随旧连接 EOF 自行退出）
            self._terminate_locked()
            ctx = multiprocessing.get_context("spawn")
            parent_cmd, child_cmd = ctx.Pipe(True)
            parent_evt, child_evt = ctx.Pipe(False)
            process = ctx.Process(
                target=worker_main,
                args=(self.session_id, child_cmd, child_evt),
                name=f"chat-worker-{self.session_id}",
                daemon=True,
            )
            process.start()
            self._process = process
            self._cmd_conn = parent_cmd
            self._evt_conn = parent_evt
            self._alive = True
            self._started = True
            self._generation_running = False
            threading.Thread(
                target=self._read_events,
                args=(parent_evt,),
                name=f"chat-worker-reader-{self.session_id}",
                daemon=True,
            ).start()

    def _terminate_locked(self) -> None:
        process = self._process
        self._process = None
        if process is not None and process.is_alive():
            try:
                process.terminate()
                process.join(timeout=3)
            except Exception:
                pass
        for conn in (self._cmd_conn, self._evt_conn):
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        self._cmd_conn = None
        self._evt_conn = None
        self._alive = False
        self._generation_running = False

    def _read_events(self, conn) -> None:
        while True:
            try:
                evt = conn.recv()
            except (EOFError, OSError, ValueError):
                break
            self._dispatch(evt)
        try:
            conn.close()
        except Exception:
            pass
        # worker 进程退出/崩溃：感知并合成收尾事件，避免 SSE 消费端悬挂
        with self._state_lock:
            was_running = self._generation_running
            self._alive = False
            self._generation_running = False
        if was_running:
            self._dispatch({"type": "task_done", "reason": "worker_exit_or_crash"})
        self._dispatch({"type": "worker_exit"})

    def _dispatch(self, evt: dict) -> None:
        if not isinstance(evt, dict):
            return
        evt_type = evt.get("type")
        if evt_type == "task_started":
            self._generation_running = True
        elif evt_type in ("task_done", "worker_exit"):
            self._generation_running = False
        with self._state_lock:
            handlers = list(self._handlers)
        for handler in handlers:
            try:
                handler(evt)
            except Exception as exc:
                print(f"[WARN] 会话 [{self.session_id}] worker 事件处理失败: {exc}")

    # ---- 状态查询 ----
    def is_alive(self) -> bool:
        return self._alive and self._process is not None and self._process.is_alive()

    def is_generation_running(self) -> bool:
        return self._generation_running

    def has_started(self) -> bool:
        """是否真实 spawn 过进程（供清理逻辑区分"未启动"与"已死亡"）。"""
        return self._started

    # ---- handler 管理 ----
    def add_handler(self, handler: Callable[[dict], None]) -> None:
        with self._state_lock:
            if handler not in self._handlers:
                self._handlers.append(handler)

    def remove_handler(self, handler: Callable[[dict], None]) -> None:
        with self._state_lock:
            if handler in self._handlers:
                self._handlers.remove(handler)

    # ---- 命令 ----
    def _send(self, payload: dict) -> bool:
        with self._send_lock:
            conn = self._cmd_conn
            if conn is None or not self._alive:
                return False
            return _safe_send(conn, payload)

    def start_generation(self, request_data: dict) -> bool:
        """确保 worker 存活并发送 generate 命令。"""
        self._ensure_process()
        return self._send({"type": "generate", "request": request_data or {}})

    def request_stop(self, reason: str = "user") -> bool:
        if not self.is_alive():
            return False
        return self._send({"type": "stop", "reason": reason})

    def inject_message(self, message: dict) -> bool:
        """向运行中的生成任务注入一条用户消息（消息引导）。

        worker 未运行或无活动任务时返回 False（调用方回退为普通发送）。
        """
        if not self.is_alive() or not self.is_generation_running():
            return False
        return self._send({"type": "inject", "message": message or {}})

    def cancel_injected_message(self, text: str) -> bool:
        """撤回一条尚未消费的注入消息（按文本匹配）。

        发送即视为已受理（worker 内部按匹配结果移除）；worker 未运行或
        无活动任务时返回 False。
        """
        if not self.is_alive() or not self.is_generation_running():
            return False
        return self._send({"type": "cancel_inject", "text": text})

    def shutdown(self, timeout: float = 8.0) -> None:
        """优雅关停：通知 worker 收尾后回收进程。"""
        if self.is_alive():
            self._send({"type": "shutdown"})
            try:
                self._process.join(timeout=timeout)
            except Exception:
                pass
        with self._state_lock:
            self._terminate_locked()

    def terminate(self, reason: str = "") -> None:
        """强制终止（等待优雅退出超时后的兜底，JSONL 原子写保证不损坏）。"""
        print(f"[WARN] 强制终止会话 [{self.session_id}] worker 进程: {reason}")
        with self._state_lock:
            self._terminate_locked()


# 主进程的 worker 代理注册表：session_id -> proxy
_WORKER_PROXIES: dict[str, SessionWorkerProxy] = {}
_worker_proxies_lock = threading.Lock()


def get_worker_proxy(session_id: str) -> SessionWorkerProxy:
    """获取或创建会话 worker 代理（不立即 spawn，首次 generate 时启动）。"""
    with _worker_proxies_lock:
        proxy = _WORKER_PROXIES.get(session_id)
        if proxy is None:
            proxy = SessionWorkerProxy(session_id)
            proxy.add_handler(_make_worker_event_handler(proxy))
            _WORKER_PROXIES[session_id] = proxy
        return proxy


def drop_worker_proxy(session_id: str, shutdown: bool = True) -> None:
    """移除并（可选）关停会话 worker 代理。"""
    with _worker_proxies_lock:
        proxy = _WORKER_PROXIES.pop(session_id, None)
    if proxy is not None and shutdown:
        try:
            proxy.shutdown()
        except Exception as exc:
            print(f"[WARN] 关停会话 [{session_id}] worker 失败: {exc}")


def sweep_dead_worker_proxies() -> None:
    """清理"曾启动过、进程已死且无生成任务"的 worker 代理，防止注册表膨胀。

    从未 spawn 过的代理（刚创建、等待首次 generate）不在此列——弹出它们
    会导致每次请求都重复创建代理并 spawn 新进程。
    """
    with _worker_proxies_lock:
        dead = [
            session_id
            for session_id, proxy in _WORKER_PROXIES.items()
            if proxy.has_started()
            and not proxy.is_alive()
            and not proxy.is_generation_running()
        ]
        for session_id in dead:
            proxy = _WORKER_PROXIES.pop(session_id, None)
            if proxy is not None:
                try:
                    proxy.shutdown()
                except Exception:
                    pass


def _injected_content_text(message: Any) -> str:
    """提取注入消息的纯文本（撤回匹配用；与主进程 content_part_to_text 口径一致）。"""
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "\n".join(
            part.get("text", "") for part in content
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
        ).strip()
    return ""


def peek_worker_proxy(session_id: str) -> Optional[SessionWorkerProxy]:
    """只读查找会话 worker 代理（不创建、不 spawn）。"""
    with _worker_proxies_lock:
        return _WORKER_PROXIES.get(session_id)


def _make_worker_event_handler(proxy: SessionWorkerProxy) -> Callable[[dict], None]:
    """把 worker 事件转发进绑定的 _SessionStream（reader 线程执行）。"""

    def _handle(evt: dict) -> None:
        evt_type = evt.get("type")
        stream = proxy._bound_stream
        if stream is None:
            return
        loop = proxy.loop
        if evt_type == "sse":
            stream.emit(str(evt.get("chunk") or ""))
            if loop is not None:
                try:
                    loop.call_soon_threadsafe(stream.notify)
                except RuntimeError:
                    pass
        elif evt_type == "round_start":
            stream.set_round_start()
        elif evt_type == "question_text":
            stream.question_text = str(evt.get("text") or "")
        elif evt_type == "task_done":
            if not stream.done and loop is not None:
                try:
                    asyncio.run_coroutine_threadsafe(stream.finish(), loop)
                except RuntimeError:
                    pass

    return _handle
