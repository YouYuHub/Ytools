"""配置文件热重载轮询线程（全局，主进程内）。

不依赖 uvicorn/FastAPI 的 --reload 机制：后台守护线程按固定间隔读取
setting/mcp_servers.json 与 setting/models.json，先比对文件内容 hash
（sha256），确认变化且 JSON 解析成功后才触发重载回调；解析失败保留内存
现状（hash 也不更新，下一轮自动重试），并在终端打印失败/更新调试信息。

- mcp_servers.json 变更：同步工具选择内存快照；仅当 `servers` 键的内容
  变化时才重新探测 MCP 工具（inputs 单独保存不会触发昂贵的工具发现）。
- models.json 变更：重载 models_config / model_selection / setting_vars
  （不动 .env）。模型配置本身是"先构建后整体替换"的原子赋值，请求协程
  读到的要么是旧配置要么是新配置。

worker 子进程不需要该线程：每个生成任务开始时 worker 会自行 init_path()
刷新 .env/models.json，并按磁盘上的 mcp_servers.json 重新发现工具。

间隔由 .env 的 CONFIG_HOT_RELOAD_INTERVAL_SECONDS 控制（默认 5 秒，
<=0 表示禁用轮询）。测试可通过 watch_targets 指向临时目录。
"""
import hashlib
import json
import threading
from pathlib import Path
from typing import Any, Callable

from env_manager import load_var

# 每个被监视文件的内存状态：内容 hash + 快路径（size/mtime_ns，未变化时跳过读取）
# {"hash": str|None, "size": int|None, "mtime_ns": int|None}
_watch_state: dict[str, dict[str, Any]] = {}
_state_lock = threading.Lock()

# 重载回调注册表：target -> [(名称, 回调)]；回调签名 (data: dict, meta: dict) -> None
# meta 固定含 "path"（文件路径），mcp_servers 目标额外含 "servers_changed"（bool）
_reload_callbacks: dict[str, list[tuple[str, Callable[[dict, dict], Any]]]] = {}
_callbacks_lock = threading.Lock()

_watcher_thread: threading.Thread | None = None
_watcher_stop = threading.Event()
_watcher_started = False
_watcher_start_lock = threading.Lock()

WATCH_INTERVAL_ENV_NAME = "CONFIG_HOT_RELOAD_INTERVAL_SECONDS"
DEFAULT_WATCH_INTERVAL_SECONDS = 5.0


def default_watch_targets() -> dict[str, Path]:
    """默认监视目标：项目 setting 目录下的两个配置文件。"""
    from config import PROJECT_ROOT
    setting_dir = Path(PROJECT_ROOT) / "setting"
    return {
        "mcp_servers": setting_dir / "mcp_servers.json",
        "models": setting_dir / "models.json",
    }


def register_reload_callback(target: str, name: str, callback: Callable[[dict, dict], Any]) -> None:
    """注册某个监视目标的重载回调（重复同名注册会覆盖旧回调）。"""
    if not callable(callback):
        raise TypeError("callback 必须可调用")
    with _callbacks_lock:
        entries = _reload_callbacks.setdefault(target, [])
        for index, (existing_name, _) in enumerate(entries):
            if existing_name == name:
                entries[index] = (name, callback)
                return
        entries.append((name, callback))


def unregister_reload_callback(target: str, name: str) -> None:
    with _callbacks_lock:
        entries = _reload_callbacks.get(target)
        if entries:
            _reload_callbacks[target] = [entry for entry in entries if entry[0] != name]


def _file_hash(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_hash(value: Any) -> str:
    """对 JSON 可序列化结构计算稳定 hash（键排序），用于比较 servers 子结构。"""
    return _file_hash(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def _resolve_watch_targets(watch_targets: dict[str, Path] | None) -> dict[str, Path]:
    return {str(key): Path(value) for key, value in (watch_targets or default_watch_targets()).items()}


def _watch_interval_seconds() -> float:
    raw = load_var(WATCH_INTERVAL_ENV_NAME, DEFAULT_WATCH_INTERVAL_SECONDS)
    try:
        interval = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_WATCH_INTERVAL_SECONDS
    return interval if interval > 0 else 0.0


def poll_once(watch_targets: dict[str, Path] | None = None) -> dict[str, str]:
    """执行一轮检查：hash 变化且解析成功才触发回调并更新 hash。

    Returns:
        {target: 动作}，动作取值 "reloaded" / "failed" / "unchanged" / "missing"
    """
    actions: dict[str, str] = {}
    for target, path in _resolve_watch_targets(watch_targets).items():
        actions[target] = _check_target(target, path)
    return actions


def _check_target(target: str, path: Path) -> str:
    with _state_lock:
        state = _watch_state.setdefault(target, {"hash": None, "size": None, "mtime_ns": None})
        stored_hash = state["hash"]
        stored_size = state["size"]
        stored_mtime_ns = state["mtime_ns"]
    try:
        stat = path.stat()
    except OSError:
        # 文件被暂时移走/重命名：不触发重载也不清 hash，等文件回来按 hash 判断
        return "missing"
    # 快路径：大小 + mtime 纳秒都没变就跳过读取与 hash
    if stored_size == stat.st_size and stored_mtime_ns == stat.st_mtime_ns and stored_hash is not None:
        return "unchanged"
    try:
        payload = path.read_bytes()
    except OSError as exc:
        print(f"[config-watch] 读取 {path} 失败，保留内存配置: {exc}")
        return "failed"
    payload_hash = _file_hash(payload)
    with _state_lock:
        state = _watch_state.setdefault(target, {"hash": None, "size": None, "mtime_ns": None})
        if payload_hash == state["hash"]:
            state["size"] = stat.st_size
            state["mtime_ns"] = stat.st_mtime_ns
            return "unchanged"
    try:
        data = json.loads(payload.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("顶层结构必须是 JSON 对象")
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        # 解析失败：不更新 hash（下一轮文件修好后自动重试），内存配置保持现状
        print(f"[config-watch] {path} 解析失败，保留内存配置: {exc}")
        return "failed"
    meta: dict[str, Any] = {"path": str(path)}
    if target == "mcp_servers":
        with _state_lock:
            previous = _watch_state.setdefault(f"{target}__servers", {"hash": None})["hash"]
        servers_changed = _canonical_hash(data.get("servers")) != previous
        meta["servers_changed"] = servers_changed
        if servers_changed:
            with _state_lock:
                _watch_state.setdefault(f"{target}__servers", {"hash": None})["hash"] = _canonical_hash(data.get("servers"))
    dispatched: list[str] = []
    with _callbacks_lock:
        callbacks = list(_reload_callbacks.get(target, []))
    for name, callback in callbacks:
        try:
            callback(data, meta)
            dispatched.append(name)
        except Exception as exc:
            print(f"[config-watch] {target} 重载回调 [{name}] 执行失败: {exc}")
    with _state_lock:
        state = _watch_state.setdefault(target, {"hash": None, "size": None, "mtime_ns": None})
        state["hash"] = payload_hash
        state["size"] = stat.st_size
        state["mtime_ns"] = stat.st_mtime_ns
    print(
        f"[config-watch] {path} 已重新加载（回调: {'、'.join(dispatched) or '无'}"
        + (f"，servers 变更={meta['servers_changed']}" if target == "mcp_servers" else "")
        + "）"
    )
    return "reloaded"


def _watcher_loop(watch_targets: dict[str, Path], interval: float) -> None:
    while not _watcher_stop.wait(interval):
        try:
            poll_once(watch_targets)
        except Exception as exc:  # 轮询线程绝不能静默死亡
            print(f"[config-watch] 轮询异常（已忽略，继续下一轮）: {exc}")


def prime_watch_state(watch_targets: dict[str, Path] | None = None) -> None:
    """记录当前文件 hash 作为基线，避免启动后第一轮就把“未变化”当变更。"""
    for target, path in _resolve_watch_targets(watch_targets).items():
        try:
            payload = path.read_bytes()
            payload_hash = _file_hash(payload)
            stat = path.stat()
        except OSError:
            continue
        with _state_lock:
            _watch_state[target] = {"hash": payload_hash, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
            if target == "mcp_servers":
                try:
                    data = json.loads(payload.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                servers_value = data.get("servers") if isinstance(data, dict) else None
                _watch_state[f"{target}__servers"] = {"hash": _canonical_hash(servers_value)}


def start_config_watcher(watch_targets: dict[str, Path] | None = None) -> threading.Thread | None:
    """启动全局配置热重载守护线程（幂等）；间隔 <=0 时不启动。

    Returns:
        已启动（或此前已启动）的线程；禁用时返回 None。
    """
    global _watcher_thread, _watcher_started
    with _watcher_start_lock:
        if _watcher_started and _watcher_thread and _watcher_thread.is_alive():
            return _watcher_thread
        interval = _watch_interval_seconds()
        resolved_targets = _resolve_watch_targets(watch_targets)
        if interval <= 0:
            print(
                f"[config-watch] 配置热重载已禁用（{WATCH_INTERVAL_ENV_NAME}<=0）："
                + "、".join(str(path) for path in resolved_targets.values())
            )
            _watcher_started = True
            return None
        prime_watch_state(resolved_targets)
        _watcher_stop.clear()
        _watcher_thread = threading.Thread(
            target=_watcher_loop,
            args=(resolved_targets, interval),
            name="config-hot-reload",
            daemon=True,
        )
        _watcher_thread.start()
        _watcher_started = True
        print(
            f"[config-watch] 配置热重载线程已启动（间隔 {interval:g}s）："
            + "、".join(str(path) for path in resolved_targets.values())
        )
        return _watcher_thread


def stop_config_watcher(timeout: float = 2.0) -> None:
    """停止轮询线程并清空状态（主要供测试使用）。"""
    global _watcher_thread, _watcher_started
    with _watcher_start_lock:
        _watcher_stop.set()
        thread = _watcher_thread
        _watcher_thread = None
        _watcher_started = False
    if thread and thread.is_alive():
        thread.join(timeout=timeout)
    with _state_lock:
        _watch_state.clear()
    with _callbacks_lock:
        _reload_callbacks.clear()