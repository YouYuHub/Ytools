"""终端工具增强（P0/P1/P2）回归测试：控制键/ANSI/footer/completion/env/list_jobs/graceful。"""
import sys
import time
import uuid

import pytest

from factory.agent_runtime import command_input, command_jobs, builtin_tools as bt


def test_key_to_chars_mapping():
    assert command_input.key_to_chars("ctrl+c") == "\x03"
    assert command_input.key_to_chars("up") == "\x1b[A"
    assert command_input.key_to_chars("enter", tty=True) == "\r"
    assert command_input.key_to_chars("enter", tty=False) == "\n"
    with pytest.raises(ValueError):
        command_input.key_to_chars("nope")


def test_strip_ansi():
    raw = "\x1b[1t\x1b[c\x1b[?1004hhello\x1b[31m world\x1b[0m\r\nline2\r"
    out = command_jobs.strip_ansi(raw)
    assert out == "hello world\nline2\n"
    assert "\x1b" not in out


def test_footer_includes_elapsed_cwd_completion():
    result = {"status": "running", "exit_code": None, "completion": "awaiting_input",
              "elapsed_ms": 1234, "cwd": "C:/x"}
    foot = command_jobs._footer(result)
    assert "completion=awaiting_input" in foot
    assert "elapsed_ms=1234" in foot
    assert "cwd=C:/x" in foot


def test_awaiting_input_heuristic(tmp_path):
    out = tmp_path / "o.txt"
    out.write_text("Password: ", encoding="utf-8")
    import os
    old = time.time() - 5
    os.utime(out, (old, old))
    meta = {"output_path": str(out)}
    assert command_jobs._awaiting_input(meta) is True
    out.write_text("just normal output\n", encoding="utf-8")
    os.utime(out, (old, old))
    assert command_jobs._awaiting_input(meta) is False


@pytest.mark.skipif(sys.platform != "win32", reason="env echo uses cmd")
def test_env_override_applied_to_child():
    res = bt.try_execute_builtin_command_tool("run_command", {
        "command": "echo %YTOOLS_TEST_ENV%", "shell": "cmd",
        "env": {"YTOOLS_TEST_ENV": "hello-env"}, "_session_id": "s-env",
        "yield_time_ms": 3000,
    })
    text = res.get("output") or res.get("stdout") or res.get("message") or ""
    assert "hello-env" in text, res


@pytest.mark.skipif(sys.platform != "win32", reason="list_jobs real process")
def test_list_jobs_and_graceful_stop():
    sid = "s-list-" + uuid.uuid4().hex[:6]
    started = bt.try_execute_builtin_command_tool("run_command", {
        "command": "ping -n 6 127.0.0.1", "shell": "cmd", "background": True,
        "_session_id": sid,
    })
    job_id = started["job_id"]
    listed = bt.try_execute_builtin_command_tool("list_jobs", {"_session_id": sid, "active_only": True})
    assert any(j["job_id"] == job_id for j in listed["jobs"])
    stopped = bt.try_execute_builtin_command_tool("stop_command", {"job_id": job_id, "_session_id": sid, "graceful": True})
    assert stopped["status"] in ("stopped", "completed", "failed")


def test_write_stdin_key_sends_bytes():
    # 交互 python 逐行回显；key=tab 应作为 1 字节控制字符送达
    sid = "s-key-" + uuid.uuid4().hex[:6]
    started = bt.try_execute_builtin_command_tool("run_command", {
        "command": f'"{sys.executable}" -u -c "import sys;[sys.stdout.write(repr(l)+chr(10)) for l in sys.stdin]"',
        "shell": "cmd", "interactive": True, "_session_id": sid, "yield_time_ms": 800,
    })
    job_id = started["job_id"]
    res = bt.try_execute_builtin_command_tool("write_stdin", {
        "job_id": job_id, "chars": "a", "key": "tab", "_session_id": sid, "wait_seconds": 1,
    })
    assert res.get("input_bytes") == 2  # 'a' + '\t'
    bt.try_execute_builtin_command_tool("stop_command", {"job_id": job_id, "_session_id": sid})
