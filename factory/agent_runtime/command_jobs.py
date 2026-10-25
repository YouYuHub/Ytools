"""Disk-backed background command registry shared by workers and HTTP server."""
import json
import os
import re
import signal
import subprocess
import time
import uuid
from pathlib import Path

from util.file_lock import cross_process_lock
from util.process_identity import process_identity


def _builtin():
    from factory.agent_runtime import builtin_tools
    return builtin_tools


def write_metadata(path, metadata):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
        # A concurrent Windows reader/AV can briefly hold the destination
        # without delete-sharing. Retry the commit, never the stdin delivery.
        from util.atomic_json import RETRY_DELAYS
        for attempt in range(len(RETRY_DELAYS) + 1):
            try:
                os.replace(temporary, path)
                return
            except OSError as error:
                occupied = isinstance(error, PermissionError) or getattr(error, 'winerror', None) in {5, 32, 33}
                if not occupied or attempt == len(RETRY_DELAYS):
                    raise
                time.sleep(RETRY_DELAYS[attempt])
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass  # Cleanup must not turn a committed receipt into a failure.


def read_job(job_id, session_id=None):
    if not re.fullmatch(r"bg_[A-Za-z0-9_]+", job_id):
        raise ValueError("job_id 无效；请使用 background=true 返回的 job_id")
    root = _builtin()._BG_LOG_DIR.resolve()
    path = root / f"{job_id}.meta.json"
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"后台任务 {job_id} 不存在或记录已过期") from exc
    if meta.get("job_id") != job_id:
        raise ValueError("后台任务记录无效")
    from security.authorization import visible_session
    if not visible_session(meta.get("session_id") or ""):
        raise ValueError("后台任务不存在或无权访问")
    if session_id is not None and (meta.get("session_id") or "") != session_id:
        raise ValueError("后台任务不属于当前会话")
    # Never let registry metadata turn HTTP output/download into arbitrary file reads.
    for key in ("output_path", "completion_path", "lease_path"):
        if not meta.get(key):
            continue
        candidate = Path(meta[key])
        if key == 'lease_path':
            # This ephemeral spec is deleted on completion. Resolve its stable
            # parent, not the disappearing leaf (Windows can resolve it during
            # deletion to an inconsistent parent). Only sibling state is read.
            valid = candidate.parent.resolve() == root and bool(re.fullmatch(r'bg_lease_[a-f0-9]{32}\.json',candidate.name))
        else:
            valid = candidate.resolve().parent == root
        if not valid:
            raise ValueError(f"后台任务日志路径无效（{key}）；请重新启动命令")
    return path, meta


def snapshot(meta, *, include_output=False, tail_lines=2000, max_chars=131072):
    bt = _builtin()
    done_path = Path(meta["completion_path"]) if meta.get("completion_path") else None
    done = bool(done_path and done_path.exists())
    pid = int(meta["pid"])
    alive = bt._pid_alive_windows(pid) if os.name == "nt" else bt._pid_alive_posix(pid)
    identity = process_identity(pid) if alive and meta.get("process_identity") else None
    verified = bool(identity and identity == meta.get("process_identity"))
    if meta.get("process_identity") and not verified:
        alive = False
    exit_code = None
    if done:
        try:
            exit_code = int(done_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            pass
    status = ("stopped" if meta.get("stopped") else "failed" if done and exit_code not in (0, None)
              else "completed" if done else "running" if alive else "unknown")
    lease = {}
    if meta.get('lease_path'):
        try:
            lease = json.loads(Path(meta['lease_path']).with_suffix('.outcome.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            pass
        if lease.get('timed_out') and not (lease.get('terminating') and alive):
            status = 'timeout'
            exit_code = lease.get('exit_code')
        elif lease.get('cancelled') and not (lease.get('terminating') and alive):
            status = 'stopped'
            exit_code = lease.get('exit_code')
    result = {key: meta.get(key) for key in ("job_id", "pid", "command", "shell", "cwd", "created_at", "session_id")}
    if meta.get('interactive'):
        result.update(interactive=True, tty=bool(meta.get('tty')), input_encoding='utf-8')
    result.update(status=status, exit_code=exit_code, can_stop=status == "running" and verified,
                  output_path=bt._display_path(Path(meta["output_path"])),
                  completion_path=bt._display_path(done_path) if done_path else None,
                  background=meta.get('background', True), timed_out=bool(lease.get('timed_out')))
    if 'termination_confirmed' in lease:
        result['termination_confirmed'] = lease['termination_confirmed']
    if include_output:
        try:
            output, truncated = bt._command_output_tail(Path(meta["output_path"]), tail_lines, max_chars)
        except OSError:
            output, truncated = "", False
        result.update(output_tail=output, output_truncated=truncated, tail_lines=tail_lines)
    return result


def list_jobs(session_id=None, *, active_only=False):
    """共享磁盘注册表按会话筛选；页面仅展示仍运行的终端，日志另行保留。"""
    result = []
    for path in _builtin()._BG_LOG_DIR.glob("bg_*.meta.json"):
        try:
            _, meta = read_job(path.name.removesuffix(".meta.json"))
            if meta.get("closed") or (session_id is not None and (meta.get("session_id") or "") != session_id):
                continue
            current = snapshot(meta)
            if not active_only or current["status"] == "running":
                result.append(current)
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return sorted(result, key=lambda item: item.get("created_at") or 0, reverse=True)


def incremental(job_id, session_id, cursor, max_chars, wait_seconds=0, encoding='', *, check_deadline=True):
    from .command_output import read
    from .tool_control import check_tool_control
    deadline = time.monotonic() + wait_seconds
    while True:
        check_tool_control(check_deadline=check_deadline)
        _, meta = read_job(job_id, session_id)
        result = snapshot(meta)
        result.update(read(meta, cursor, max_chars, finished=result['status'] != 'running', encoding=encoding))
        result['message'] = f"[run_command] job_id={job_id} | status={result['status']} | exit={result['exit_code']}"
        if result['output'] or result['status'] != 'running' or time.monotonic() >= deadline:
            return result
        time.sleep(min(0.05, max(0, deadline - time.monotonic())))


def action(job_id, operation, session_id=None, *, tail_lines=30, max_chars=12000,
           cursor=None, wait_seconds=0, encoding=''):
    if operation == 'poll' and cursor is not None:
        return incremental(job_id, session_id, cursor, max_chars, wait_seconds, encoding)
    path, _ = read_job(job_id, session_id)
    with cross_process_lock(path.with_suffix(".lock")):
        path, meta = read_job(job_id, session_id)
        current = snapshot(meta)
        if operation in ("stop", "close") and current["status"] == "running":
            if not current["can_stop"]:
                if operation == "stop":
                    raise ValueError("旧任务缺少进程身份记录，无法安全停止；请在系统终端处理")
                # Legacy record: close only removes its UI entry, never signal an unverified PID.
            else:
                pid = int(meta["pid"])
                if process_identity(pid) != meta["process_identity"]:
                    raise ValueError("进程身份已改变，请刷新列表后重试")
                if os.name == "nt":
                    stopped = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                        capture_output=True, timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    if stopped.returncode and process_identity(pid) == meta["process_identity"]:
                        raise ValueError("停止后台任务失败，请刷新后重试")
                else:
                    # The supervisor and shell share a dedicated process group.
                    try:
                        os.killpg(pid, signal.SIGTERM)
                        deadline = time.monotonic() + 2
                        while time.monotonic() < deadline:
                            try:
                                os.killpg(pid, 0)
                            except ProcessLookupError:
                                break
                            time.sleep(0.05)
                        else:
                            os.killpg(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                meta["stopped"] = True
        if operation == "close":
            meta["closed"] = True
            meta.setdefault("closed_at", time.time())
        if operation in ("stop", "close"):
            write_metadata(path, meta)
            if meta.get('stopped') and meta.get('interactive') and meta.get('lease_path'):
                from .command_input import finish_pending
                finish_pending(meta['lease_path'])
        result = snapshot(meta, include_output=True, tail_lines=tail_lines, max_chars=max_chars)
        result["message"] = f"[run_command] job_id={job_id} | status={result['status']} | exit={result['exit_code']}"
        return result
