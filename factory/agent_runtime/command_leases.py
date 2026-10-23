"""Yielded foreground jobs use existing durable job IDs, with an independent lifetime guard."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
import uuid


def start(command, shell_exe, shell_kind, shell_prefix, cwd, session_id, timeout, wait_ms, max_chars, agent,
          interactive=False, tty=False):
    from . import builtin_tools as bt, command_jobs
    from .permission_runtime import CURRENT
    from .tool_control import check_tool_control
    from util.process_identity import process_identity
    bt._BG_LOG_DIR.mkdir(parents=True, exist_ok=True)
    spec_path = bt._BG_LOG_DIR / ('bg_lease_' + uuid.uuid4().hex + '.json')
    script = spec_path.with_suffix('.run.cmd' if shell_kind == 'cmd' else '.run.ps1' if shell_kind in {'powershell', 'pwsh'} else '.run.sh')
    bt._write_runner_script(script, shell_kind, command)
    argv = ([shell_exe, '/c', str(script)] if shell_kind == 'cmd' else
            [shell_exe, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', str(script)]
            if shell_kind in {'powershell', 'pwsh'} else [shell_exe, str(script)])
    if interactive and '-NonInteractive' in argv:
        argv.remove('-NonInteractive')
    spec = {'argv': argv, 'script': str(script), 'cwd': cwd,
            'deadline': time.time() + timeout, 'monotonic_deadline': time.monotonic() + timeout, 'owner_pid': os.getpid(),
            'owner_identity': process_identity(os.getpid()), 'interactive': interactive, 'tty': tty}
    spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding='utf-8')
    runner = [sys.executable, str(Path(__file__).with_name('command_lease_runner.py')), str(spec_path)]
    outer_exe, outer_kind, outer_prefix = bt._resolve_shell('cmd' if os.name == 'nt' else 'sh', os.name == 'nt')
    invocation = subprocess.list2cmdline(runner) if os.name == 'nt' else shlex.join(runner)
    current = CURRENT.get()
    job_id = None
    try:
        launched = bt._run_command_background(invocation, outer_exe, outer_kind, outer_prefix,
                                                cwd, os.name == 'nt', session_id, structured=True)
        job_id = launched['job_id']
        path, meta = command_jobs.read_job(job_id, session_id)
        meta.update(background=False, command=command, shell=shell_kind,
                    owner_task=current.id if current else None, owner_agent=agent,
                    lease_path=str(spec_path), deadline=spec['deadline'], interactive=interactive, tty=tty)
        command_jobs.write_metadata(path, meta)
        # Ordinary short commands should finish in one call, even if they emit output
        # before exiting. Interactive prompts must return promptly for input.
        deadline = min(time.monotonic() + wait_ms / 1000, spec['monotonic_deadline'])
        result = command_jobs.incremental(job_id, session_id, '', max_chars,
                                          max(0, deadline - time.monotonic()), check_deadline=False)
        output = result['output']
        decoding_errors = result.get('decoding_errors', False)
        encodings = {result['encoding']} if result.get('encoding') else set()
        while (not interactive and result['status'] == 'running' and not result['has_more']
               and len(output) < max_chars and time.monotonic() < deadline):
            result = command_jobs.incremental(job_id, session_id, result['next_cursor'],
                                              max_chars - len(output),
                                              max(0, deadline - time.monotonic()), check_deadline=False)
            output += result['output']
            decoding_errors |= result.get('decoding_errors', False)
            if result.get('encoding'):
                encodings.add(result['encoding'])
        result['output'] = output
        result['decoding_errors'] = decoding_errors
        result['encoding'] = next(iter(encodings)) if len(encodings) == 1 else 'mixed' if encodings else None
        check_tool_control(check_deadline=False)
        result['yielded'] = result['status'] == 'running'
        result['message'] += ' | 使用 poll_command 继续查询' if result['yielded'] else ''
        return result
    except BaseException:
        if job_id:
            try:
                command_jobs.action(job_id, 'stop', session_id)
            except Exception:
                spec_path.with_suffix('.cancel').touch()
        else:
            spec_path.with_suffix('.cancel').touch()
        raise


def cancel_owned(task_id, agent=None):
    """Request cancellation without blocking the event loop on taskkill."""
    from . import builtin_tools as bt, command_jobs
    for path in bt._BG_LOG_DIR.glob('bg_*.meta.json'):
        try:
            _, meta = command_jobs.read_job(path.name.removesuffix('.meta.json'))
            if meta.get('owner_task') != task_id or meta.get('background', True):
                continue
            if agent is not None and meta.get('owner_agent') != agent:
                continue
            if command_jobs.snapshot(meta)['status'] == 'running' and meta.get('lease_path'):
                Path(meta['lease_path']).with_suffix('.cancel').touch()
        except (OSError, ValueError, TypeError, KeyError):
            continue
