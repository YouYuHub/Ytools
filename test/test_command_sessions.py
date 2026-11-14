"""常驻 shell 会话（run_command session_id）真实进程端到端测试。

验证坑表：cmd/PS 退出码、标记剥离、for 循环禁标记、cwd/env 跨调用保持。
仅在 Windows 跑（依赖 cmd/powershell）；POSIX 跳过。
"""
import sys
import time
import uuid

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="resident shell tests target Windows cmd/PS")

from factory.agent_runtime import command_sessions


@pytest.fixture
def sess():
    sid = "sess-" + uuid.uuid4().hex[:8]
    yield sid
    for entry in command_sessions.list_sessions(sid):
        command_sessions.close(sid, entry["name"], "main")


def _run(sid, name, cmd, shell, wait=2500):
    return command_sessions.run(sid, name, cmd, shell, "", "main", wait, 12000)


def test_cmd_env_and_cwd_persist(sess):
    a = _run(sess, "dev", "set X=1", "cmd")
    assert a["exit_code"] == 0
    b = _run(sess, "dev", "echo %X%", "cmd")
    assert "1" in b["output"]
    assert "__END_" not in b["output"]
    c = _run(sess, "dev", "cd C:\\Windows", "cmd")
    d = _run(sess, "dev", "cd", "cmd")
    assert "Windows" in d["output"]


def test_cmd_exit_code_captured(sess):
    r = _run(sess, "dev", "cmd /c exit 5", "cmd")
    assert r["exit_code"] == 5
    assert "__END_" not in r["output"]
    ok = _run(sess, "dev", "echo hello", "cmd")
    assert ok["exit_code"] == 0
    assert "hello" in ok["output"]


def test_cmd_for_loop_no_marker(sess):
    r = _run(sess, "dev", "for /l %i in (1,1,3) do @echo %i", "cmd")
    assert "__END_" not in r["output"]
    for n in ("1", "2", "3"):
        assert n in r["output"]


def test_ps_env_and_exit_code(sess):
    _run(sess, "ps", "$env:X='v'", "powershell")
    b = _run(sess, "ps", "echo $env:X", "powershell")
    assert "v" in b["output"]
    assert "__END_" not in b["output"]
    r = _run(sess, "ps", "cmd /c exit 7", "powershell")
    assert r["exit_code"] == 7


def test_session_reuse_and_list(sess):
    _run(sess, "dev", "echo first", "cmd")
    jobs = command_sessions.list_sessions(sess)
    assert any(j["name"] == "dev" and j["alive"] for j in jobs)
    # 复用同一 job（不新建）
    j1, _, c1 = command_sessions.get_or_create(sess, "dev", "cmd", "", "main")
    j2, _, c2 = command_sessions.get_or_create(sess, "dev", "cmd", "", "main")
    assert j1 == j2 and c2 is False
