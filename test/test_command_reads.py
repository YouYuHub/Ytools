"""Common model calls keep a durable, isolated output position."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from factory.agent_runtime import builtin_tools as bt, command_jobs, command_reads
from factory.agent_runtime.permission_runtime import CURRENT


@pytest.fixture
def job(tmp_path, monkeypatch):
    monkeypatch.setattr(bt, '_BG_LOG_DIR', tmp_path)
    output, done = tmp_path / 'bg_auto.out.txt', tmp_path / 'bg_auto.done.txt'
    output.write_bytes(b'first\n')
    done.write_text('0', encoding='utf-8')
    identifier = bt._save_command_job(os.getpid(), output, done, 'auto', str(tmp_path), 's')
    return identifier, output


def poll(job, **extra):
    return bt.try_execute_builtin_command_tool('poll_command', {
        'job_id': job[0], '_session_id': 's', 'wait_seconds': 0, **extra})


def test_minimal_poll_continues_without_cursor(job):
    assert poll(job)['output'] == 'first\n'
    assert poll(job)['output'] == ''
    with job[1].open('ab') as handle:
        handle.write('新内容\n'.encode())
    assert poll(job)['output'] == '新内容\n'
    assert poll(job)['output'] == ''


def test_finished_job_drains_all_output_and_gives_next_call(job):
    source = '中文🙂\n' * 700
    job[1].write_bytes(source.encode())
    pieces = []
    for _ in range(100):
        result = poll(job, _max_chars=512)
        assert result['status'] == 'completed'
        pieces.append(result['output'])
        if not result['has_more']:
            assert 'next_call' not in result
            break
        assert result['next_call'] == {'tool': 'poll_command', 'arguments': {'job_id': job[0]}}
    else:
        pytest.fail('did not drain output')
    assert ''.join(pieces) == source


def test_initial_output_seeds_next_read(job):
    first = command_jobs.incremental(job[0], 's', '', 12000)
    command_reads.seed(first, 's', 'main')
    assert poll(job)['output'] == ''


def test_explicit_replay_sets_next_automatic_position(job):
    assert poll(job)['output'] == 'first\n'
    assert poll(job, cursor='')['output'] == 'first\n'
    assert poll(job)['output'] == ''
    assert poll(job, cursor='invalid')['success'] is False


def test_http_and_explicit_tail_do_not_consume_model_output(job):
    assert command_jobs.action(job[0], 'poll', 's')['output_tail'] == 'first\n'
    assert bt.try_execute_builtin_command_tool('poll_command', {
        'job_id': job[0], '_session_id': 's', 'tail_lines': 1})['output_tail'] == 'first\n'
    assert poll(job)['output'] == 'first\n'
    assert command_jobs.action(job[0], 'poll', 's')['output_tail'] == 'first\n'
    assert poll(job)['output'] == ''


def test_task_and_agent_cursors_are_isolated(job):
    for task, agent in [('one', 'main'), ('one', 'child'), ('two', 'main')]:
        token = CURRENT.set(SimpleNamespace(id=task))
        try:
            assert poll(job, _command_owner_agent=agent)['output'] == 'first\n'
            assert poll(job, _command_owner_agent=agent)['output'] == ''
        finally:
            CURRENT.reset(token)
    assert poll(job, _session_id='other')['success'] is False


def test_read_position_survives_worker_change(job):
    assert poll(job)['output'] == 'first\n'
    script = '''import json,sys
from pathlib import Path
from factory.agent_runtime import builtin_tools as bt
bt._BG_LOG_DIR = Path(sys.argv[1])
print(json.dumps(bt.try_execute_builtin_command_tool('poll_command', {'job_id':sys.argv[2], '_session_id':'s', 'wait_seconds':0})))
'''
    result = subprocess.run([sys.executable, '-B', '-c', script, str(job[1].parent), job[0]],
                            check=True, capture_output=True, text=True, encoding='utf-8', timeout=20)
    assert json.loads(result.stdout)['output'] == ''


def test_parallel_reads_do_not_duplicate_output(job):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: poll(job), range(2)))
    assert sorted(result['output'] for result in results) == ['', 'first\n']


def test_automatic_reset_is_reported_but_explicit_stale_cursor_fails(job):
    old = poll(job)['next_cursor']
    job[1].write_bytes(b'new\n')
    assert poll(job, cursor=old)['success'] is False
    result = poll(job)
    assert result['output'] == 'new\n'
    assert result['output_reset'] is True
    assert poll(job)['output'] == ''


def test_checkpoint_failure_keeps_output_and_manual_recovery(job, monkeypatch):
    def fail(*args):
        raise OSError('disk full')
    monkeypatch.setattr(command_reads, '_save', fail)
    result = poll(job)
    assert result['output'] == 'first\n'
    assert result['cursor_saved'] is False
    assert 'next_cursor' in result['cursor_warning']
    assert poll(job, cursor=result['next_cursor'])['output'] == ''


def test_reader_map_bounded_and_registry_fields_preserved(job):
    path, meta = command_jobs.read_job(job[0], 's')
    meta['closed'] = True
    meta['output_readers'] = {str(i): {'cursor': '', 'updated_at': i} for i in range(64)}
    command_jobs.write_metadata(path, meta)
    assert poll(job)['output'] == 'first\n'
    _, latest = command_jobs.read_job(job[0], 's')
    assert latest['closed'] is True
    assert len(latest['output_readers']) == 64


def test_explicit_encoding_persists_and_can_be_reset(job):
    job[1].write_bytes('你好\n'.encode('gbk'))
    assert poll(job, encoding='gbk')['output'] == '你好\n'
    with job[1].open('ab') as handle:
        handle.write('世界\n'.encode('gbk'))
    assert poll(job)['output'] == '世界\n'
    with job[1].open('ab') as handle:
        handle.write('恢复UTF8\n'.encode())
    assert poll(job, encoding='')['output'] == '恢复UTF8\n'
    assert poll(job, encoding='no-such-codec')['success'] is False


def test_empty_stdin_is_read_alias_and_shares_cursor(job):
    result = bt.try_execute_builtin_command_tool('write_stdin', {'job_id': job[0], '_session_id': 's'})
    assert result['output'] == 'first\n'
    assert poll(job)['output'] == ''


def test_input_validation_happens_before_side_effect(job):
    sent = []
    def send(*args):
        sent.append(args)
        return {}
    with pytest.raises(ValueError, match='cursor'):
        command_reads.read(job[0], 's', 'main', cursor='invalid', send=send)
    with pytest.raises(LookupError):
        command_reads.read(job[0], 's', 'main', encoding='no-such-codec', send=send)
    assert sent == []


def test_model_defaults_are_applied_but_internal_legacy_calls_unchanged(monkeypatch):
    captured = []
    def run(*args):
        captured.append(args)
        return {'status': 'completed', 'exit_code': 0}
    monkeypatch.setattr(bt, '_run_command_impl', run)
    bt.try_execute_builtin_command_tool('run_command', {'command': 'echo ok'})
    assert captured[-1][-1] is True
    bt.execute_run_command({'command': 'echo ok'})
    assert captured[-1][-1] is False


def test_wait_does_not_block_job_registry_or_stop_status(job):
    from util.file_lock import cross_process_lock
    path, meta = command_jobs.read_job(job[0], 's')
    Path(meta['completion_path']).unlink()
    assert poll(job)['output'] == 'first\n'
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(poll, job, wait_seconds=2)
        time.sleep(0.1)
        started = time.monotonic()
        with cross_process_lock(path.with_suffix('.lock'), timeout=0.5):
            _, latest = command_jobs.read_job(job[0], 's')
            latest['stopped'] = True
            command_jobs.write_metadata(path, latest)
        result = pending.result(timeout=2)
    assert result['status'] == 'stopped'
    assert time.monotonic() - started < 1
    assert command_jobs.read_job(job[0], 's')[1]['stopped'] is True


def test_input_log_reset_recovers_without_replaying_input(job, monkeypatch):
    from factory.agent_runtime import command_input
    path, meta = command_jobs.read_job(job[0], 's')
    spec = job[1].parent / ('bg_lease_' + 'a' * 32 + '.json')
    meta.update(interactive=True, lease_path=str(spec), owner_task=None, owner_agent='main')
    command_jobs.write_metadata(path, meta)
    import hashlib
    prefix = spec.with_suffix('.input.' + hashlib.sha256(b'receipt').hexdigest())
    ack = Path(str(prefix) + '.ack.json')
    command_jobs.write_metadata(ack, {'status': 'accepted', 'accepted_bytes': 5})
    original = command_jobs.incremental
    calls = []
    def reset_once(*args, **kwargs):
        calls.append(args[2])
        if len(calls) == 1:
            raise ValueError('OUTPUT_RESET')
        return original(*args, **kwargs)
    monkeypatch.setattr(command_jobs, 'incremental', reset_once)
    result = command_input.send(job[0], 's', '', False, 'receipt', '', 0, 'main')
    assert result['input_status'] == 'accepted'
    assert result['input_bytes'] == 5
    assert result['output_reset'] is True
    assert len(calls) == 2
    assert not [p for p in spec.parent.glob(spec.stem + '.input.*.json') if not p.name.endswith('.ack.json')]


def test_seed_metadata_failure_does_not_discard_command_output(job, monkeypatch):
    result = {'job_id': job[0], 'next_cursor': '', 'output': 'completed work'}
    def fail(*args):
        raise OSError('registry temporarily unavailable')
    monkeypatch.setattr(command_jobs, 'read_job', fail)
    assert command_reads.seed(result, 's', 'main')['output'] == 'completed work'
    assert result['cursor_saved'] is False


def test_default_short_wait_preserves_cmd_syntax_hints(job, monkeypatch):
    from factory.agent_runtime import command_leases
    monkeypatch.setattr(bt, '_resolve_shell', lambda *_: ('cmd', 'cmd', ['/c']))
    monkeypatch.setattr(bt, '_guard_cmd_pipeline', lambda command: (command, None, []))
    seen = []
    def launch(*args):
        seen.append(args)
        return {'status': 'completed', 'message': 'done'}
    monkeypatch.setattr(command_leases, 'start', launch)
    result = bt.try_execute_builtin_command_tool('run_command', {'command': 'echo one; echo two'})
    assert seen[0][7] == 1000
    assert 'cmd 不支持' in result['message']


def test_implicit_short_wait_keeps_local_pipeline_fallback(job, monkeypatch):
    monkeypatch.setattr(bt, '_resolve_shell', lambda *_: ('cmd', 'cmd', ['/c']))
    monkeypatch.setattr(bt, '_guard_cmd_pipeline', lambda command: (command, ('tail', 1), []))
    monkeypatch.setattr(bt, '_run_command_foreground', lambda *_: (0, 'first\nlast\n', '', False, 0.1, None))
    result = bt.try_execute_builtin_command_tool('run_command', {'command': 'echo one | tail -1'})
    assert result['status'] == 'completed'
    assert result['stdout'] == 'last\n'
