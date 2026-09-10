# -*- coding: utf-8 -*-
"""restart_service 工具（mcp_server/restart_tools_server.py）单元测试。

范围：入参校验 / 快照缺失与 pid 探活 / 冷却与 already_scheduled / 调度落盘内容 /
管道命令内容 / cancel 与 status。所有调度只验证"写文件 + 派发被模拟"，不真杀进程。
"""
import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

_MODULE_PATH = PROJECT_ROOT / "mcp_server" / "restart_tools_server.py"
_SPEC = importlib.util.spec_from_file_location("restart_tools_server_under_test", _MODULE_PATH)


@pytest.fixture()
def restart_module(tmp_path, monkeypatch):
    """按文件路径加载模块，并把 _LOCK_DIR 指到临时目录（避免污染真实服务）。"""
    module = importlib.util.module_from_spec(_SPEC)
    _SPEC.loader.exec_module(module)
    lock_dir = tmp_path / "lock"
    lock_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(module, "_LOCK_DIR", lock_dir)
    return module


def _write_state(module, tmp_path: Path, pid: int | None = None, **overrides) -> Path:
    """写入伪造 service_state.json（pid 默认取当前进程，保证探活通过）。"""
    state_path = tmp_path / "lock" / "service_state.json"
    payload = {
        "pid": os.getpid() if pid is None else pid,
        "port": 48621,
        "project_root": str(PROJECT_ROOT),
        "python_exe": sys.executable,
    }
    payload.update(overrides)
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    return state_path


# ----------------------------------------------------------------------
# _validate_inputs
# ----------------------------------------------------------------------

def test_validate_inputs_rejects_empty_session_id(restart_module):
    delay, error = restart_module._validate_inputs("", "继续任务", None)
    assert delay is None
    assert "session_id" in error


def test_validate_inputs_rejects_blank_follow_up(restart_module):
    delay, error = restart_module._validate_inputs("default", "   ", None)
    assert delay is None
    assert "follow_up" in error


def test_validate_inputs_rejects_long_follow_up(restart_module):
    delay, error = restart_module._validate_inputs("default", "x" * 501, None)
    assert delay is None
    assert "follow_up 过长" in error


def test_validate_inputs_rejects_bad_delay(restart_module):
    delay, error = restart_module._validate_inputs("default", "继续", "abc")
    assert delay is None
    assert "delay_seconds" in error


def test_validate_inputs_rejects_out_of_range_delay(restart_module):
    delay, error = restart_module._validate_inputs("default", "继续", 3.0)
    assert delay is None
    assert "超出范围" in error


def test_validate_inputs_accepts_defaults(restart_module):
    delay, error = restart_module._validate_inputs("default", "继续任务", None)
    assert error is None
    assert delay == restart_module._DELAY_DEFAULT_SECONDS


# ----------------------------------------------------------------------
# _build_pending（service_state 读取 + pid 探活）
# ----------------------------------------------------------------------

def test_build_pending_errors_without_state(restart_module, tmp_path):
    pending, error = restart_module._build_pending("default", "继续", 25.0)
    assert pending is None
    assert "service_state.json" in error


def test_build_pending_errors_on_dead_pid(restart_module, tmp_path):
    dead_pid = _find_dead_pid()
    _write_state(restart_module, tmp_path, pid=dead_pid)
    pending, error = restart_module._build_pending("default", "继续", 25.0)
    assert pending is None
    assert "已不存活" in error


def test_build_pending_ok(restart_module, tmp_path):
    _write_state(restart_module, tmp_path)
    pending, error = restart_module._build_pending("default", "继续任务", 30.0)
    assert error is None
    assert pending["session_id"] == "default"
    assert pending["follow_up"] == "继续任务"
    assert pending["delay_seconds"] == 30.0
    assert pending["old_pid"] == os.getpid()
    assert pending["port"] == 48621
    assert Path(pending["project_root"]).exists()
    assert 0 < pending["scheduled_epoch"] <= time.time() + 1


def _find_dead_pid() -> int:
    """找一个几乎必然不存在的 pid（托底自进程 + 大偏移，最多试到 2**22）。"""
    candidates = [os.getpid() + offset for offset in (40000, 80000, 160000)]
    for pid in candidates:
        if 0 < pid < 2 ** 22 and not restart_module_pid_alive(pid):
            return pid
    return 4194303  # 大 pid 撞活进程概率极低


def restart_module_pid_alive(pid: int) -> bool:
    import ctypes
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if handle:
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    return ctypes.windll.kernel32.GetLastError() == 5


# ----------------------------------------------------------------------
# 冷却与已调度
# ----------------------------------------------------------------------

def test_restart_cooldown_blocks_second_schedule(restart_module, tmp_path, monkeypatch):
    _write_state(restart_module, tmp_path)
    dispatched = []
    monkeypatch.setattr(
        restart_module, "_dispatch_pipeline", lambda cmd_path, log_path: (12345, None))
    first = json.loads(restart_module._restart_service_impl("default", "第一轮", 15.0))
    assert first["ok"] is True
    second = json.loads(restart_module._restart_service_impl("default", "第二轮", 15.0))
    assert second["ok"] is False
    assert "冷却期" in second["reason"]
    assert dispatched == []


def test_restart_returns_already_scheduled_when_pending_recent(restart_module, tmp_path, monkeypatch):
    _write_state(restart_module, tmp_path)
    pending_path = tmp_path / "lock" / "restart_pending.json"
    pending_path.write_text(json.dumps({"session_id": "x"}), encoding="utf-8")
    result = json.loads(restart_module._restart_service_impl("default", "继续", 15.0))
    assert result["ok"] is False
    assert "already_scheduled" in result["reason"]


def test_restart_overrides_stale_pending(restart_module, tmp_path, monkeypatch):
    _write_state(restart_module, tmp_path)
    pending_path = tmp_path / "lock" / "restart_pending.json"
    pending_path.write_text(json.dumps({"session_id": "stale"}), encoding="utf-8")
    old_ts = time.time() - (restart_module._PENDING_STALE_SECONDS + 30)
    os.utime(pending_path, (old_ts, old_ts))
    dispatched = []
    monkeypatch.setattr(
        restart_module, "_dispatch_pipeline", lambda cmd_path, log_path: (12345, None))
    result = json.loads(restart_module._restart_service_impl("default", "覆盖旧调度", 15.0))
    assert result["ok"] is True
    stored = json.loads(pending_path.read_text(encoding="utf-8"))
    assert stored["follow_up"] == "覆盖旧调度"


# ----------------------------------------------------------------------
# 调度落盘内容
# ----------------------------------------------------------------------

def test_restart_schedule_writes_pending_and_cmd(restart_module, tmp_path, monkeypatch):
    _write_state(restart_module, tmp_path)
    dispatched = []
    monkeypatch.setattr(
        restart_module, "_dispatch_pipeline", lambda cmd_path, log_path: (666, None))
    result = json.loads(restart_module._restart_service_impl("default", "重启后继续", 18.0))
    assert result["ok"] is True
    assert result["status"] == "scheduled"
    pending_path = Path(result["pending"])
    assert pending_path.exists()
    pending = json.loads(pending_path.read_text(encoding="utf-8"))
    assert pending["follow_up"] == "重启后继续"
    assert pending["delay_seconds"] == 18.0
    # 冷却已标记
    assert restart_module._cooldown_remaining() > 0
    # 旧结果文件已被清理
    assert not (tmp_path / "lock" / "restart_done.json").exists()

    cmd_path = tmp_path / "lock" / "restart_pipeline.cmd"
    assert cmd_path.exists()
    cmd_text = cmd_path.read_bytes().decode("utf-8", errors="replace")
    assert "taskkill /F /T /PID" in cmd_text
    assert f"--port {pending['port']}" in cmd_text
    assert str(pending_path) in cmd_text
    assert "restart_helper.py" in cmd_text


def test_restart_dispatch_failure_cleanup_pending(restart_module, tmp_path, monkeypatch):
    _write_state(restart_module, tmp_path)
    monkeypatch.setattr(
        restart_module, "_dispatch_pipeline", lambda cmd_path, log_path: (None, "boom"))
    result = json.loads(restart_module._restart_service_impl("default", "继续", 15.0))
    assert result["ok"] is False
    assert "boom" in result["reason"]
    assert not (tmp_path / "lock" / "restart_pending.json").exists()


# ----------------------------------------------------------------------
# _pid_alive
# ----------------------------------------------------------------------

def test_pid_alive_current_process(restart_module):
    assert restart_module._pid_alive(os.getpid()) is True


def test_pid_alive_rejects_invalid(restart_module):
    assert restart_module._pid_alive(None) is False
    assert restart_module._pid_alive(0) is False
    assert restart_module._pid_alive(-5) is False


# ----------------------------------------------------------------------
# cancel / status
# ----------------------------------------------------------------------

def test_restart_cancel_removes_files(restart_module, tmp_path):
    lock_dir = tmp_path / "lock"
    (lock_dir / "restart_pending.json").write_text("{}", encoding="utf-8")
    (lock_dir / "restart_pipeline.cmd").write_bytes(b"foo\r\n")
    result = json.loads(restart_module._restart_cancel_impl())
    assert result["ok"] is True
    assert any("已删除" in item and "pending" in item for item in result["details"])
    assert not (lock_dir / "restart_pending.json").exists()
    assert not (lock_dir / "restart_pipeline.cmd").exists()


def test_restart_status_reports(restart_module, tmp_path):
    _write_state(restart_module, tmp_path)
    info = json.loads(restart_module._restart_status_impl())
    assert info["state"]["pid"] == os.getpid()
    assert info["pending"] is None
    assert info["done"] is None
