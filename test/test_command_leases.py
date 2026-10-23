import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import pytest

from factory.agent_runtime import builtin_tools as bt, command_jobs, command_leases
from factory.agent_runtime.permission_runtime import CURRENT


@pytest.fixture
def logs(tmp_path, monkeypatch):
    monkeypatch.setattr(bt, '_BG_LOG_DIR', tmp_path)
    yield tmp_path
    for entry in tmp_path.glob('bg_*.meta.json'):
        try:
            command_jobs.action(entry.name.removesuffix('.meta.json'), 'stop', 'lease-test')
        except (OSError, ValueError):
            pass


def python_command(code, shell):
    if shell == 'powershell':
        return f'& "{sys.executable}" -u -c "{code}"'
    if shell == 'cmd':
        return f'"{sys.executable}" -u -c "{code}"'
    import shlex
    return shlex.join([sys.executable, '-u', '-c', code])


def poll_to_end(result, *, seconds=12):
    pieces = [result.get('output', '')]
    deadline = time.monotonic() + seconds
    while result['status'] == 'running' or result.get('has_more'):
        assert time.monotonic() < deadline, result
        result = bt.try_execute_builtin_command_tool('poll_command', {
            'job_id': result['job_id'], 'cursor': result.get('next_cursor', ''),
            'wait_seconds': 0.2, '_session_id': 'lease-test'})
        assert not result.get('error'), result
        pieces.append(result['output'])
    return result, ''.join(pieces)


@pytest.mark.parametrize('shell', ['cmd', 'powershell'] if os.name == 'nt' else ['sh'])
def test_real_yield_and_resume_preserve_output(logs, shell):
    args = {'command': python_command("import time; print('begin', flush=True); time.sleep(0.4); print('done')", shell),
            'shell': shell, 'work_dir': str(logs), 'yield_time_ms': 0, 'timeout_seconds': 15,
            '_session_id': 'lease-test'}
    result = bt.try_execute_builtin_command_tool('run_command', args)
    assert 'job_id' in result, result
    assert result['background'] is False
    final, output = poll_to_end(result)
    assert final['status'] == 'completed', (final, output)
    assert final['exit_code'] == 0
    assert output.count('begin') == output.count('done') == 1, output


@pytest.mark.parametrize('shell', ['cmd', 'powershell'] if os.name == 'nt' else ['sh'])
def test_model_common_flow_needs_no_wait_or_cursor_arguments(logs, shell):
    result = bt.try_execute_builtin_command_tool('run_command', {
        'command': python_command("import time; print('begin',flush=True); time.sleep(2); print('end')", shell),
        'shell': shell, 'work_dir': str(logs), '_session_id': 'lease-test'})
    assert result['status'] == 'running', result
    assert result['background'] is False
    pieces = [result['output']]
    deadline = time.monotonic() + 12
    while result['status'] == 'running' or result.get('has_more'):
        assert time.monotonic() < deadline, result
        result = bt.try_execute_builtin_command_tool('poll_command', {
            'job_id': result['job_id'], '_session_id': 'lease-test'})
        pieces.append(result['output'])
    assert result['exit_code'] == 0 and result['status'] == 'completed'
    assert ''.join(pieces).count('begin') == ''.join(pieces).count('end') == 1


@pytest.mark.parametrize('exit_code', [0, 7])
def test_short_command_finishes_in_initial_call_with_actual_exit(logs, exit_code):
    shell = 'cmd' if os.name == 'nt' else 'sh'
    result = bt.try_execute_builtin_command_tool('run_command', {
        'command': python_command(f"import sys; print('short'); sys.exit({exit_code})", shell),
        'shell': shell, 'work_dir': str(logs), '_session_id': 'lease-test'})
    assert result['exit_code'] == exit_code, result
    assert result['status'] == ('failed' if exit_code else 'completed')
    assert result['output'].strip() == 'short'
    assert bt.try_execute_builtin_command_tool('poll_command', {
        'job_id': result['job_id'], '_session_id': 'lease-test'})['output'] == ''


def test_total_timeout_still_applies_after_yield(logs):
    shell = 'powershell' if os.name == 'nt' else 'sh'
    result = bt.try_execute_builtin_command_tool('run_command', {
        'command': python_command("import time; print('begin', flush=True); time.sleep(20)", shell),
        'shell': shell, 'work_dir': str(logs), 'yield_time_ms': 0, 'timeout_seconds': 2,
        '_session_id': 'lease-test'})
    assert 'job_id' in result, result
    final, _ = poll_to_end(result)
    assert final['status'] == 'timeout'
    assert final['timed_out'] is True


def test_owner_cancellation_does_not_cancel_independent_background(logs):
    shell = 'powershell' if os.name == 'nt' else 'sh'
    owner = SimpleNamespace(id='owner-lease', session='lease-test', context={'mode': 'full_control'})
    token = CURRENT.set(owner)
    try:
        result = bt.try_execute_builtin_command_tool('run_command', {
            'command': python_command("import time; time.sleep(20)", shell),
            'shell': shell, 'work_dir': str(logs), 'yield_time_ms': 0, 'timeout_seconds': 30,
            '_session_id': 'lease-test', '_command_owner_agent': 'child'})
        assert 'job_id' in result, result
        background = bt.try_execute_builtin_command_tool('run_command', {
            'command': python_command('import time; time.sleep(20)', shell),
            'shell': shell, 'work_dir': str(logs), 'background': True, '_session_id': 'lease-test'})
        assert background['background'] is True
    finally:
        CURRENT.reset(token)
    command_leases.cancel_owned(owner.id, 'other-child')
    assert command_jobs.action(result['job_id'], 'poll', 'lease-test')['status'] == 'running'
    command_leases.cancel_owned(owner.id, 'child')
    final, _ = poll_to_end(result)
    assert final['status'] == 'stopped'
    # Explicit background jobs do not carry a foreground owner.
    _, meta = command_jobs.read_job(result['job_id'], 'lease-test')
    assert meta['owner_task'] == owner.id
    assert command_jobs.action(background['job_id'], 'poll', 'lease-test')['status'] == 'running'


def test_invalid_yield_options_do_not_start_command(logs):
    for args in ({'yield_time_ms': -1}, {'yield_time_ms': True}, {'yield_time_ms': 30001},
                 {'yield_time_ms': 0, 'background': True}):
        result = bt.try_execute_builtin_command_tool('run_command', {'command': 'echo bad', **args})
        assert result['success'] is False
    assert not list(logs.glob('*.meta.json'))


def test_initial_wait_exceeding_total_budget_keeps_pollable_id(logs):
    shell = 'powershell' if os.name == 'nt' else 'sh'
    result = bt.try_execute_builtin_command_tool('run_command', {
        'command': python_command('import time; time.sleep(20)', shell), 'shell': shell,
        'work_dir': str(logs), 'yield_time_ms': 30000, 'timeout_seconds': 3,
        '_session_id': 'lease-test'})
    assert 'job_id' in result, result
    final, _ = poll_to_end(result)
    assert final['status'] == 'timeout'


def test_runtime_completion_cancels_owned_foreground(logs, monkeypatch):
    from memory import permission_store
    from factory.agent_runtime.permission_runtime import PermissionRuntime
    monkeypatch.setattr(permission_store, 'ROOT', logs / 'control')
    runtime = PermissionRuntime('lease-test', {}, lambda event: None, lambda: False)
    runtime.context['mode'] = 'full_control'
    shell = 'powershell' if os.name == 'nt' else 'sh'
    try:
        result = bt.try_execute_builtin_command_tool('run_command', {
            'command': python_command('import time; time.sleep(20)', shell), 'shell': shell,
            'work_dir': str(logs), 'yield_time_ms': 0, 'timeout_seconds': 30,
            '_session_id': 'lease-test'})
        assert 'job_id' in result, result
    finally:
        runtime.close('completed')
    final, _ = poll_to_end(result)
    assert final['status'] == 'stopped'


def test_corrupt_registry_entry_does_not_break_task_cleanup(logs):
    command_jobs.write_metadata(logs / 'bg_broken.meta.json', {
        'job_id': 'bg_broken', 'owner_task': 'owner', 'background': False,
        'pid': None, 'output_path': str(logs / 'bg_broken.out.txt')})
    command_leases.cancel_owned('owner')
