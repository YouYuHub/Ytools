"""cmd 百分号规范化回归：FOR 循环变量双写/单写 + 字面量孤立 % 转义。

背景（实测真 bug）：
- cmd 命令经 .run.cmd 批处理文件执行，FOR 循环变量须 %%d，模型按交互式写 %d
  导致 `d was unexpected at this time.`、`%~nd` 报 invalid path operator；
- 批处理层吞掉孤立 %（echo 100% 输出 100），静默丢数据。
修复：_write_runner_script 写批处理前自动规范化 FOR 变量（双写）并转义字面量 %；
常驻交互式控制台会话（command_sessions）方向相反（单写）。
纯函数单元跨平台跑；真实执行仅 Windows。
"""
import os
import sys
import tempfile
import uuid

import pytest

from factory.agent_runtime import builtin_tools as bt

norm = bt._normalize_cmd_for_variables
esc = bt._escape_cmd_literal_percent


# ---------------- FOR 变量规范化（批处理双写） ----------------

@pytest.mark.parametrize("cmd,want", [
    ("for /d %d in (*) do @echo %d", "for /d %%d in (*) do @echo %%d"),
    ('for /d %d in (*) do @type "%d\\%~nd_file.md"', 'for /d %%d in (*) do @type "%%d\\%%~nd_file.md"'),
    ("for %f in (*.txt) do @echo %f", "for %%f in (*.txt) do @echo %%f"),
    ("for /l %i in (1,1,3) do @echo %i", "for /l %%i in (1,1,3) do @echo %%i"),
    ("for /r %f in (*.md) do @echo %~dpf", "for /r %%f in (*.md) do @echo %%~dpf"),
    ("for %a in (1) do @for %b in (2) do @echo %a%b", "for %%a in (1) do @for %%b in (2) do @echo %%a%%b"),
    ("for %a in (1) do @echo %a & echo %PATH%", "for %%a in (1) do @echo %%a & echo %PATH%"),
])
def test_for_batch_doubling(cmd, want):
    got, changed = norm(cmd, batch=True)
    assert got == want
    assert changed is True


@pytest.mark.parametrize("cmd", [
    "for /d %%d in (*) do @echo %%d",   # 已双写幂等
    "echo %PATH%",                       # 环境变量
    "echo %1 %2",                        # 批参数
    "echo 100%",                         # 无 FOR 声明
    "echo %USERNAME% %temp%",
])
def test_for_batch_no_change(cmd):
    got, changed = norm(cmd, batch=True)
    assert got == cmd
    assert changed is False


def test_for_batch_idempotent():
    once, _ = norm("for /d %d in (*) do @echo %d", batch=True)
    twice, changed = norm(once, batch=True)
    assert twice == once
    assert changed is False


# ---------------- FOR 变量规范化（交互式单写） ----------------

@pytest.mark.parametrize("cmd,want", [
    ("for /d %%d in (*) do @echo %%d", "for /d %d in (*) do @echo %d"),
    ("for %%f in (*.txt) do @echo %%~nf", "for %f in (*.txt) do @echo %~nf"),
])
def test_for_interactive_singling(cmd, want):
    got, changed = norm(cmd, batch=False)
    assert got == want
    assert changed is True


def test_for_interactive_already_single():
    got, changed = norm("for %d in (*) do @echo %d", batch=False)
    assert got == "for %d in (*) do @echo %d"
    assert changed is False


# ---------------- 字面量孤立 % 转义 ----------------

@pytest.mark.parametrize("cmd,want", [
    ("echo 100%", "echo 100%%"),
    ("echo 50% off", "echo 50%% off"),
    ("echo (50%)", "echo (50%%)"),
    ("for %%d in (*) do @echo %%d 100%", "for %%d in (*) do @echo %%d 100%%"),
])
def test_literal_percent_escaped(cmd, want):
    got, changed = esc(cmd)
    assert got == want
    assert changed is True


@pytest.mark.parametrize("cmd", [
    "echo %PATH%",            # 环境变量对
    "echo %USERNAME% %temp%",
    "echo %1 %2",             # 批参数
    "echo %%",                # 已双写
    "echo %foo",              # 无闭合 %letter 保守不动
    "echo a%b%c",             # 形似变量对保守不动
    "for %%d in (*) do @echo %%d",  # FOR 已双写
])
def test_literal_percent_protected(cmd):
    got, changed = esc(cmd)
    assert got == cmd
    assert changed is False


def test_literal_percent_idempotent():
    once, _ = esc("echo 100%")
    twice, changed = esc(once)
    assert twice == once
    assert changed is False


# ---------------- 提示函数（不误导） ----------------

def test_for_hint_fires_only_for_single_percent():
    assert bt._cmd_for_variable_hint("for /d %d in (*) do @echo %d")
    assert not bt._cmd_for_variable_hint("for /d %%d in (*) do @echo %%d")
    assert not bt._cmd_for_variable_hint("echo %PATH%")


def test_literal_hint_not_misled_by_for_variable():
    # 纯 FOR 命令不应触发"字面量 %"提示（FOR 单 % 不是字面量）
    assert not bt._cmd_literal_percent_hint("for /d %d in (*) do @echo %d")
    assert not bt._cmd_literal_percent_hint("echo %PATH%")
    # 真正的字面量 % 才触发
    assert bt._cmd_literal_percent_hint("echo 100%")
    # FOR + 字面量混合：仍触发（确有字面量）
    assert bt._cmd_literal_percent_hint("for %d in (*) do @echo %d 100%")


# ---------------- 真实执行（Windows） ----------------

win = pytest.mark.skipif(sys.platform != "win32", reason="cmd execution targets Windows")


@pytest.fixture
def workdir():
    d = tempfile.mkdtemp(prefix="cmdpct_")
    for name in ("alpha", "beta"):
        sub = os.path.join(d, name)
        os.makedirs(sub, exist_ok=True)
        with open(os.path.join(sub, name + "_file.md"), "w", encoding="utf-8") as f:
            f.write("hi " + name)
    yield d


def _exec(cmd, cwd):
    r = bt._execute_command_impl(cmd, "cmd", cwd, 25, False, 8000, True, "", yield_time_ms=None)
    return r


@win
def test_exec_for_d_single_percent(workdir):
    r = _exec("for /d %d in (*) do @echo %d", workdir)
    assert r["exit_code"] == 0
    assert r["stdout"] == "alpha\nbeta\n"


@win
def test_exec_for_d_modifier(workdir):
    r = _exec('for /d %d in (*) do @type "%d\\%~nd_file.md"', workdir)
    assert r["exit_code"] == 0
    assert "hi alpha" in r["stdout"] and "hi beta" in r["stdout"]


@win
def test_exec_for_l(workdir):
    r = _exec("for /l %i in (1,1,3) do @echo %i", workdir)
    assert r["exit_code"] == 0
    assert r["stdout"] == "1\n2\n3\n"


@win
def test_exec_multiline_for_block(workdir):
    r = _exec("for /d %d in (*) do (\n  @echo %d\n)", workdir)
    assert r["exit_code"] == 0
    assert r["stdout"] == "alpha\nbeta\n"


@win
@pytest.mark.parametrize("cmd,want", [
    ("echo 100%", "100%\n"),
    ("echo 50% off", "50% off\n"),
    ("echo (50%)", "(50%)\n"),
])
def test_exec_literal_percent_not_lost(workdir, cmd, want):
    r = _exec(cmd, workdir)
    assert r["exit_code"] == 0
    assert r["stdout"] == want


@win
def test_exec_env_var_still_expands(workdir):
    r = _exec("echo %OS%", workdir)
    assert r["exit_code"] == 0
    assert "Windows" in r["stdout"]


@win
def test_exec_for_double_percent_also_works(workdir):
    # 模型若已写双写 %%d，批处理路径同样正确（幂等）
    r = _exec("for /d %%d in (*) do @echo %%d", workdir)
    assert r["exit_code"] == 0
    assert r["stdout"] == "alpha\nbeta\n"


@win
def test_resident_session_for_single_percent(workdir):
    from factory.agent_runtime import command_sessions
    sid = "sess-" + uuid.uuid4().hex[:8]
    name = "fordev"
    try:
        r = command_sessions.run(sid, name, "for /d %d in (*) do @echo %d", "cmd", workdir, "main", 4000, 12000)
        assert "alpha" in r["output"] and "beta" in r["output"]
        # 双写也应降为单写后正确
        r2 = command_sessions.run(sid, name, "for /d %%d in (*) do @echo %%d", "cmd", workdir, "main", 4000, 12000)
        assert "alpha" in r2["output"] and "beta" in r2["output"]
    finally:
        command_sessions.close(sid, name, "main")
