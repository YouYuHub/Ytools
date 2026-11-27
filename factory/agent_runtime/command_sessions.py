"""跨调用常驻 shell 会话（run_command session_id）。

复用 lease 监督进程 + 磁盘注册表 + 输入请求队列：启动一个常驻交互式 shell，
后续 run_command(session_id=...) 把命令写入同一 shell 的 stdin，用结束标记
界定输出并取退出码，从而保住 cwd / 环境变量 / REPL import 等跨调用状态。

坑表落实（均为 Windows + cmd/PS 实测真 bug）：
- cmd 取退出码必须 `& call echo __END_<n>__^%ERRORLEVEL^%`（脱字符不能省，
  否则 %ERRORLEVEL% 在整行解析期展开恒得 0）；
- PS 取退出码先 `$global:LASTEXITCODE=$null` 哨兵，命令后非 null 用其值、仍
  null 按 $? 给 0/1；终止性错误用 try/catch/finally 保证标记不丢；
- `for … do` 形态禁用标记（& call 会被当循环体，首次迭代即命中标记）；
- 标记与输出同行时只删标记片段、不删整行（否则吞真实输出）；
- shell 退出检测查进程句柄（ConPTY 下管道不关闭），不靠管道关闭；
- 多行命令保持逐行 Enter、不加标记（拿不到单一退出码）。
"""
import os
import re
import time
import uuid

from . import command_jobs, command_input, command_output

_SESSION_IDLE_SECONDS = 1800  # 常驻会话空闲 30 分钟自动回收


def _registry_path():
    from . import builtin_tools as bt
    bt._BG_LOG_DIR.mkdir(parents=True, exist_ok=True)
    return bt._BG_LOG_DIR / "shell_sessions.json"


def _load_registry():
    import json
    try:
        return json.loads(_registry_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_registry(reg):
    command_jobs.write_metadata(_registry_path(), reg)


def _key(session_id, name):
    return f"{session_id}::{name}"


def _shell_argv(shell_kind, shell_exe):
    if shell_kind == "cmd":
        # /k 先切传统 OEM 代码页再驻留（cmd 在 65001 下读管道 stdin 会把非
        # ASCII 字节逐个变成 U+FFFD，实测硬伤；stdin/批处理/输出全跟输出口径）。
        # 监督进程也会预先设置，这里是自校正兜底（无控制台监督进程的场景）。
        from util.text_encoding import traditional_oem_cp
        return [shell_exe, "/q", "/k", "chcp %d >nul" % (traditional_oem_cp() or 65001)]
    if shell_kind in {"powershell", "pwsh"}:
        return [shell_exe, "-NoLogo", "-NoProfile", "-NoExit", "-Command", "-"]
    return [shell_exe, "-i"]


def _is_for_loop(command, shell_kind):
    if shell_kind == "cmd":
        return bool(re.search(r"\bfor\b[^&|]*\bdo\b", command, re.IGNORECASE))
    return False


def _build_payload(command, shell_kind, nonce):
    """返回 (payload, marker, has_exit)。多行/for 循环不加标记。"""
    # 常驻会话是真实交互式 cmd 控制台（命令经 stdin 喂入，非批处理文件）：
    # FOR 循环变量用单 %d 才正确；若模型写了批处理双写 %%d，降为单写。
    # 与批处理执行层（_write_runner_script 双写）方向相反，各自匹配自身语境。
    if shell_kind == "cmd":
        from . import builtin_tools as bt
        command, _ = bt._normalize_cmd_for_variables(command, batch=False)
    multiline = "\n" in command.strip()
    if multiline or _is_for_loop(command, shell_kind):
        return command + "\n", None, False
    marker = f"__END_{nonce}__"
    if shell_kind == "cmd":
        payload = f"{command} & call echo {marker}^%ERRORLEVEL^%\n"
    elif shell_kind in {"powershell", "pwsh"}:
        payload = (
            "$global:LASTEXITCODE=$null; "
            f"try {{ {command} }} catch {{ throw }} finally {{ "
            f"if ($null -ne $global:LASTEXITCODE) {{ $c=$global:LASTEXITCODE }} "
            f"elseif ($?) {{ $c=0 }} else {{ $c=1 }}; "
            f"Write-Output ('{marker}'+$c) }}\n"
        )
    else:
        payload = f"{command}\nprintf '{marker}%s\\n' \"$?\"\n"
    return payload, marker, True


def _extract(output, marker):
    """从输出中剥离标记片段，返回 (clean, exit_code)。只删片段不删整行。"""
    if not marker:
        return output, None
    pat = re.compile(re.escape(marker) + r"(-?\d+)?")
    code = None
    def sub(m):
        nonlocal code
        if m.group(1) is not None:
            code = int(m.group(1))
        return ""
    clean = pat.sub(sub, output)
    return clean, code


def _alive(meta):
    snap = command_jobs.snapshot(meta)
    return snap["status"] == "running"


def get_or_create(session_id, name, shell, work_dir, agent):
    from . import builtin_tools as bt
    reg = _load_registry()
    key = _key(session_id, name)
    entry = reg.get(key)
    if entry:
        try:
            _, meta = command_jobs.read_job(entry["job_id"], session_id)
            if _alive(meta):
                entry["last_used"] = time.time()
                reg[key] = entry
                _save_registry(reg)
                return entry["job_id"], meta, False
        except (OSError, ValueError):
            pass
        reg.pop(key, None)
    is_nt = os.name == "nt"
    shell_exe, shell_kind, shell_prefix = bt._resolve_shell(shell, is_nt)
    cwd = str(bt._resolve_dir_path(work_dir)) if work_dir else os.getcwd()
    argv = _shell_argv(shell_kind, shell_exe)
    from . import command_leases
    job_id = command_leases.start_resident(argv, cwd, session_id, agent, shell_kind)
    reg[key] = {"job_id": job_id, "shell": shell_kind, "cwd": cwd,
                "created": time.time(), "last_used": time.time()}
    _save_registry(reg)
    _, meta = command_jobs.read_job(job_id, session_id)
    return job_id, meta, True


def run(session_id, name, command, shell, work_dir, agent, wait_ms, max_chars):
    job_id, meta, created = get_or_create(session_id, name, shell, work_dir, agent)
    shell_kind = meta.get("shell") or "cmd"
    nonce = uuid.uuid4().hex[:8]
    payload, marker, has_exit = _build_payload(command, shell_kind, nonce)
    out_path = meta["output_path"]
    try:
        before_offset = os.path.getsize(out_path)
    except OSError:
        before_offset = 0
    command_input.send(job_id, session_id, payload, False, None, "", 0, agent,
                       max_chars, require_owned=False)
    deadline = time.monotonic() + max(0.2, wait_ms / 1000)
    collected = ""
    while time.monotonic() < deadline:
        _, meta = command_jobs.read_job(job_id, session_id)
        collected = _read_delta(out_path, before_offset, max_chars)
        if marker and marker in collected:
            break
        if not _alive(meta):
            break
        time.sleep(0.05)
    clean, exit_code = _extract(collected, marker)
    reg = _load_registry()
    entry = reg.get(_key(session_id, name))
    if entry:
        entry["last_used"] = time.time()
        reg[_key(session_id, name)] = entry
        _save_registry(reg)
    return {"job_id": job_id, "shell": shell_kind, "cwd": meta.get("cwd"),
            "output": clean, "exit_code": exit_code, "created": created,
            "status": "running" if _alive(meta) else "exited"}


def _read_delta(out_path, offset, max_chars):
    try:
        with open(out_path, "rb") as handle:
            handle.seek(offset)
            data = handle.read(max_chars)
    except OSError:
        return ""
    text, _, _ = command_output._auto_decode(data, True, None)
    return text


def close(session_id, name, agent):
    reg = _load_registry()
    key = _key(session_id, name)
    entry = reg.pop(key, None)
    _save_registry(reg)
    if not entry:
        return {"closed": False, "message": f"会话 {name} 不存在"}
    try:
        command_jobs.action(entry["job_id"], "stop", session_id)
    except (OSError, ValueError):
        pass
    return {"closed": True, "message": f"会话 {name} 已关闭"}


def list_sessions(session_id):
    reg = _load_registry()
    out = []
    for key, entry in reg.items():
        if not key.startswith(f"{session_id}::"):
            continue
        try:
            _, meta = command_jobs.read_job(entry["job_id"], session_id)
            alive = _alive(meta)
        except (OSError, ValueError):
            alive = False
        out.append({"name": key.split("::", 1)[1], "shell": entry.get("shell"),
                    "cwd": entry.get("cwd"), "alive": alive,
                    "idle_seconds": int(time.time() - entry.get("last_used", 0))})
    return out
