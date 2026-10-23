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
from factory.agent_runtime.command_pty import Terminal, Pipe
from factory.agent_runtime import command_input, command_jobs


def run(spec_path):
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
        if cancel_path.exists() or process_identity(spec['owner_pid']) != spec['owner_identity']:
            outcome.update(cancelled=True, exit_code=-1)
            return -1
        if time.monotonic() >= spec['monotonic_deadline']:
            outcome.update(timed_out=True, exit_code=-1)
            return -1
        process = Terminal(spec['argv'], spec['cwd']) if spec.get('tty') else Pipe(
            spec['argv'], spec['cwd'], spec.get('interactive', False))
        child_identity = process_identity(process.pid)
        if spec.get('interactive'):
            writer = threading.Thread(target=command_input.serve,
                args=(spec_path, process.write, process.close_input, lambda: process.poll() is None), daemon=True)
            writer.start()
        while process.poll() is None:
            timed_out = time.monotonic() >= spec['monotonic_deadline']
            owner_gone = process_identity(spec['owner_pid']) != spec['owner_identity']
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
            writer.join(0.25)
        from util.file_lock import cross_process_lock
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
