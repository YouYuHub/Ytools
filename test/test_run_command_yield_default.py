"""run_command 默认短等回归：非交互 5 秒一次返回、长命令仍提前 yield、交互式保持 1 秒。

背景：旧默认 1000ms 扣除 Windows 中转链约 300ms 启动开销后，只给命令本身留
约 700ms——凡真实工作超 ~0.7s 的命令 100% 需要第二次 poll 调用，token 接近翻倍。
现非交互默认 5000ms（5 秒内命令一次返回），交互式保持 1000ms（输入提示快速返回）。
"""
import os
import time

import json

import pytest

from factory.agent_runtime import builtin_tools as bt, command_jobs
from factory.agent_runtime.builtin_tool_catalog import RUN_COMMAND_TOOL_DEFINITION

SID = "yield-test"
SHELLS = ["cmd", "powershell"] if os.name == "nt" else ["sh"]


@pytest.fixture
def logs(tmp_path, monkeypatch):
    monkeypatch.setattr(bt, "_BG_LOG_DIR", tmp_path)
    yield tmp_path
    for entry in tmp_path.glob("bg_*.meta.json"):
        try:
            command_jobs.action(entry.name.removesuffix(".meta.json"), "stop", SID)
        except (OSError, ValueError):
            pass


def _short_command(shell):
    # 约 2 秒真实工作：旧默认 1000ms 下必 yield，新默认下一次返回
    if shell in ("cmd", "powershell"):
        return "ping -n 3 127.0.0.1"
    return "sleep 2; echo done"


def _long_command(shell):
    # 约 9~12 秒：新默认下仍应提前 yield（不等结束）
    if shell in ("cmd", "powershell"):
        return "ping -n 10 127.0.0.1"
    return "sleep 12"


def _waiting_command(shell):
    # 等待 stdin 输入、永不自行结束：测交互式默认等待时长
    if shell == "cmd":
        return "set /p answer=Type_something:"
    if shell == "powershell":
        return "Read-Host -Prompt Type_something"
    return "read answer"


def test_yield_defaults_contract():
    assert bt._RUN_COMMAND_YIELD_MS == 5000
    assert bt._RUN_COMMAND_INTERACTIVE_YIELD_MS == 1000
    desc = RUN_COMMAND_TOOL_DEFINITION["function"]["description"]
    assert "5000ms" in desc
    # PS 5.1 收发 JSON 默认 latin-1 编解码（中文必乱码）：描述中必须保留该警告
    assert "charset=utf-8" in desc
    props = RUN_COMMAND_TOOL_DEFINITION["function"]["parameters"]["properties"]
    assert "5000ms" in props["yield_time_ms"]["description"]


def test_yield_maximum_contract():
    # 上限 120000 与默认 timeout_seconds=120 对齐：默认配置下一次调用可覆盖
    # 任何不超时的命令；实际等待仍受 timeout 约束，超限拒绝而非钳制
    props = RUN_COMMAND_TOOL_DEFINITION["function"]["parameters"]["properties"]
    assert props["yield_time_ms"]["maximum"] == 120000


@pytest.mark.parametrize("shell", SHELLS)
def test_yield_boundary_accepts_120s_rejects_above(logs, shell):
    ok = bt.try_execute_builtin_command_tool("run_command", {
        "command": "echo ok", "shell": shell,
        "work_dir": str(logs), "yield_time_ms": 120000, "_session_id": SID})
    assert ok.get("success", True) is not False, ok
    assert ok["status"] == "completed", ok
    bad = bt.try_execute_builtin_command_tool("run_command", {
        "command": "echo ok", "shell": shell,
        "work_dir": str(logs), "yield_time_ms": 120001, "_session_id": SID})
    assert bad["success"] is False
    assert "0-120000" in json.dumps(bad, ensure_ascii=False)


def test_yield_maximum_session_path(logs):
    shell = SHELLS[0]
    ok = bt.try_execute_builtin_command_tool("run_command", {
        "command": "echo ok", "shell": shell, "work_dir": str(logs),
        "session_id": "yield-max-test", "yield_time_ms": 120000, "_session_id": SID})
    assert ok.get("success", True) is not False, ok
    bad = bt.try_execute_builtin_command_tool("run_command", {
        "command": "echo ok", "shell": shell, "work_dir": str(logs),
        "session_id": "yield-max-test", "yield_time_ms": 120001, "_session_id": SID})
    assert bad["success"] is False


@pytest.mark.parametrize("shell", SHELLS)
def test_short_command_single_call(logs, shell):
    t0 = time.monotonic()
    result = bt.try_execute_builtin_command_tool("run_command", {
        "command": _short_command(shell), "shell": shell,
        "work_dir": str(logs), "_session_id": SID})
    elapsed = time.monotonic() - t0
    assert result["status"] == "completed", result
    assert "next_call" not in result, result
    assert elapsed < 4.5, f"应提前返回而非等满默认: {elapsed:.2f}s"


@pytest.mark.parametrize("shell", SHELLS)
def test_long_command_still_yields(logs, shell):
    t0 = time.monotonic()
    result = bt.try_execute_builtin_command_tool("run_command", {
        "command": _long_command(shell), "shell": shell,
        "work_dir": str(logs), "timeout_seconds": 30, "_session_id": SID})
    elapsed = time.monotonic() - t0
    try:
        assert result["status"] == "running", result
        assert result["yielded"] is True
        assert "next_call" in result, result
        assert elapsed < 8.0, f"应提前 yield 而非等命令结束: {elapsed:.2f}s"
        assert elapsed > 4.0, f"应等满默认 5 秒而非立即返回: {elapsed:.2f}s"
    finally:
        try:
            command_jobs.action(result["job_id"], "stop", SID)
        except (OSError, ValueError, KeyError):
            pass


@pytest.mark.parametrize("shell", SHELLS)
def test_interactive_default_stays_1s(logs, shell):
    t0 = time.monotonic()
    result = bt.try_execute_builtin_command_tool("run_command", {
        "command": _waiting_command(shell), "shell": shell,
        "work_dir": str(logs), "timeout_seconds": 30,
        "interactive": True, "_session_id": SID})
    elapsed = time.monotonic() - t0
    try:
        assert result["status"] == "running", result
        assert elapsed < 4.0, f"交互式默认应为 1 秒而非 5 秒: {elapsed:.2f}s"
    finally:
        try:
            command_jobs.action(result["job_id"], "stop", SID)
        except (OSError, ValueError, KeyError):
            pass
