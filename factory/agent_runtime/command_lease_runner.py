"""Independent supervisor for yielded foreground and interactive commands."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from util.process_identity import process_identity
from factory.agent_runtime.command_pty import Terminal, Pipe, resolve_stdin_codec, stdin_env_vars
from factory.agent_runtime import command_input, command_jobs


def run(spec_path):
    # 监督进程自身的 stdout/stderr 默认按系统 ANSI 编码：英文代码页服务器上
    # 打印中文错误信息会抛 UnicodeEncodeError（吞掉退出码与 outcome 上报）。
    # 输出仅作日志展示，统一 replace 兜底，绝不因日志编码中断监督流程。
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(errors='replace')
        except Exception:
            pass
    spec_path = Path(spec_path)
    spec = json.loads(spec_path.read_text(encoding='utf-8'))
    outcome_path = spec_path.with_suffix('.outcome.json')
    cancel_path = spec_path.with_suffix('.cancel')
    process = None
    writer = None
    cancelled = threading.Event()
    if os.name != 'nt':
        signal.signal(signal.SIGTERM, lambda *_: cancelled.set())
    outcome = {'timed_out': False, 'cancelled': False, 'exit_code': None}
    try:
        persistent = bool(spec.get('persistent'))
        if cancel_path.exists() or (not persistent and process_identity(spec['owner_pid']) != spec['owner_identity']):
            outcome.update(cancelled=True, exit_code=-1)
            return -1
        if not persistent and time.monotonic() >= spec['monotonic_deadline']:
            outcome.update(timed_out=True, exit_code=-1)
            return -1
        stdin_codec = 'utf-8'
        if os.name == 'nt' and spec.get('interactive') and not spec.get('tty'):
            # Pipe 子进程共享本监督进程的控制台，按控制台代码页解码 stdin 字节；
            # stdin 字节编码必须与子进程实际解码口径一致（写死 UTF-8 会乱码）。
            # cmd 与 PS 口径不同，需分开处理（实测结论）：
            # - cmd：控制台固定地区传统 OEM 页（输入输出都设：stdin/批处理/输出
            #   全跟输出口径；65001 下读管道 stdin 会逐字节变 U+FFFD），stdin
            #   同口径编码；输出 GBK 由解码回退处理；
            # - PS 按 InputEncoding 真实解码，65001+UTF-8 正常工作，保持强制。
            from util.text_encoding import traditional_oem_cp
            has_script = bool(spec.get('script'))
            shell_kind = spec.get('shell_kind') or ''
            if not shell_kind and not has_script:
                # 兼容升级前创建的常驻会话（spec 无 shell_kind）：从 argv 推断
                base = str((spec.get('argv') or [''])[0]).lower().replace('/', '\\').rsplit('\\', 1)[-1]
                if base in ('cmd.exe', 'cmd'):
                    shell_kind = 'cmd'
                elif base in ('powershell.exe', 'pwsh.exe', 'powershell', 'pwsh'):
                    shell_kind = 'powershell'
            forced_utf8 = False
            if shell_kind == 'cmd':
                # cmd 会话控制台固定地区传统 OEM 页（输入输出都设：stdin/批处理
                # 文件/输出全跟输出口径；cmd 在 65001 下读管道 stdin 会把非 ASCII
                # 字节逐个变成 U+FFFD，实测硬伤）。脚本与 /k 参数自带 chcp 自校正。
                try:
                    import ctypes
                    from util.text_encoding import traditional_oem_cp as _trad_cp
                    target = _trad_cp() or 65001
                    kernel32 = ctypes.windll.kernel32
                    kernel32.SetConsoleCP(target)
                    kernel32.SetConsoleOutputCP(target)
                except Exception:
                    pass
            else:
                try:
                    import ctypes
                    kernel32 = ctypes.windll.kernel32
                    kernel32.SetConsoleCP(65001)
                    kernel32.SetConsoleOutputCP(65001)
                    forced_utf8 = (int(kernel32.GetConsoleCP()) == 65001)
                except Exception:
                    pass
            # 仅限交互式：非交互式脚本按 OEM 落盘，若提前切控制台反而误读（回归）。
            child_self_forces = has_script or shell_kind == 'cmd'
            stdin_codec = resolve_stdin_codec(shell_kind=shell_kind, console_forced_utf8=forced_utf8,
                                              child_self_forces_utf8=child_self_forces)
            # python 读管道 stdin 用 locale/ANSI 而非控制台代码页：跟随 stdin 口径
            # 声明 PYTHONIOENCODING（setdefault，不覆盖用户显式配置；注意不能用
            # PYTHONUTF8，那会破坏 cmd/GBK 会话的一致性）。
            for key, value in stdin_env_vars(stdin_codec).items():
                os.environ.setdefault(key, value)
        process = Terminal(spec['argv'], spec['cwd'], rows=spec.get('tty_rows', 30), cols=spec.get('tty_cols', 120)) if spec.get('tty') else Pipe(
            spec['argv'], spec['cwd'], spec.get('interactive', False), stdin_codec=stdin_codec)
        child_identity = process_identity(process.pid)
        if spec.get('interactive'):
            writer = threading.Thread(target=command_input.serve,
                args=(spec_path, process.write, process.close_input, lambda: process.poll() is None), daemon=True)
            writer.start()
        while process.poll() is None:
            timed_out = not persistent and time.monotonic() >= spec['monotonic_deadline']
            owner_gone = not persistent and process_identity(spec['owner_pid']) != spec['owner_identity']
            if timed_out or owner_gone or cancel_path.exists() or cancelled.is_set():
                outcome.update(timed_out=timed_out, cancelled=not timed_out, exit_code=-1, terminating=True,
                               command_pid=process.pid)
                # On POSIX the launch supervisor, this runner, and descendants share a
                # dedicated group. Record the reason before terminating that group.
                command_jobs.write_metadata(outcome_path, outcome)
                if spec.get('tty'):
                    process.terminate()
                elif os.name == 'nt':
                    if child_identity and process_identity(process.pid) == child_identity:
                        stopped = subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                            capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
                else:
                    os.killpg(os.getpgrp(), signal.SIGKILL)
                until = time.monotonic() + 3
                while process.poll() is None and time.monotonic() < until:
                    time.sleep(0.02)
                outcome.update(termination_confirmed=process.poll() is not None, terminating=False)
                return -1
            time.sleep(0.05)
        outcome['exit_code'] = process.poll()
        return outcome['exit_code']
    except Exception as exc:
        outcome.update(exit_code=-1, error=str(exc))
        print('命令启动/执行失败: ' + str(exc), flush=True)
        return -1
    finally:
        if process:
            process.finish()
        if writer:
            # EOF can let the child exit before its accepted receipt commits.
            # Give bounded metadata retries time to finish; never resend input.
            from util.atomic_json import RETRY_DELAYS
            writer.join(sum(RETRY_DELAYS) + 1.0)
        from util.file_lock import cross_process_lock
        outcome.setdefault('finished_at', time.time())
        with cross_process_lock(spec_path.with_suffix('.input.lock'), timeout=3):
            command_jobs.write_metadata(outcome_path, outcome)
        command_input.finish_pending(spec_path)
        spec_path.unlink(missing_ok=True)
        cancel_path.unlink(missing_ok=True)
        script = Path(spec['script'])
        if script.parent == spec_path.parent:
            script.unlink(missing_ok=True)


if __name__ == '__main__':
    code = run(sys.argv[1])
    sys.stdout.flush()
    sys.stderr.flush()
    # Native PTY readers can stay blocked after child exit. Durable state/output
    # are flushed; OS teardown closes remaining native handles.
    os._exit((code or 0) & 255)
