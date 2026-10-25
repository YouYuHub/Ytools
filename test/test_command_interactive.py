"""Real interactive process tests; no model calls or network services."""
import os
import json
from pathlib import Path
import sys
import subprocess
import time
from types import SimpleNamespace

import pytest

from factory.agent_runtime import builtin_tools as bt, command_jobs, command_leases
from factory.agent_runtime.permission_runtime import CURRENT
from factory.agent_runtime.permission_policy import plan_tool
from factory.agent_runtime.tool_validation import argument_error


@pytest.fixture
def logs(tmp_path, monkeypatch):
    monkeypatch.setattr(bt, '_BG_LOG_DIR', tmp_path)
    yield tmp_path
    for path in tmp_path.glob('bg_*.meta.json'):
        try:
            command_jobs.action(path.name.removesuffix('.meta.json'), 'stop', 'interactive-test')
        except (OSError, ValueError):
            pass


def launch(logs, code, *, shell=None, **options):
    shell = shell or ('cmd' if os.name == 'nt' else 'sh')
    script = logs / ('program_' + str(time.monotonic_ns()) + '.py')
    script.write_text(code, encoding='utf-8')
    if shell == 'powershell':
        command = f'& "{sys.executable}" -X utf8 -u "{script}"'
    elif shell == 'cmd':
        command = f'"{sys.executable}" -X utf8 -u "{script}"'
    else:
        import shlex
        command = shlex.join([sys.executable, '-X', 'utf8', '-u', str(script)])
    result = bt.try_execute_builtin_command_tool('run_command', {
        'command': command, 'shell': shell, 'work_dir': str(logs), 'interactive': True,
        'timeout_seconds': 20, '_session_id': 'interactive-test', **options})
    assert 'job_id' in result, result
    return result


def send(result, chars='', **options):
    value = bt.try_execute_builtin_command_tool('write_stdin', {
        'job_id': result['job_id'], 'chars': chars, 'cursor': result.get('next_cursor', ''),
        'wait_seconds': 0.2, '_session_id': 'interactive-test', **options})
    return value


def finish(result, seconds=15):
    output = [result.get('output', '')]
    deadline = time.monotonic() + seconds
    while result['status'] == 'running' or result.get('has_more'):
        assert time.monotonic() < deadline, result
        result = bt.try_execute_builtin_command_tool('poll_command', {
            'job_id': result['job_id'], 'cursor': result.get('next_cursor', ''),
            'wait_seconds': 0.2, '_session_id': 'interactive-test'})
        assert not result.get('error'), result
        output.append(result['output'])
    return result, ''.join(output)


@pytest.mark.parametrize('shell', ['cmd', 'powershell'] if os.name == 'nt' else ['sh'])
def test_pipe_input_unicode_and_eof(logs, shell):
    result = launch(logs, "import sys\nprint('ready',flush=True)\nprint('got:'+sys.stdin.read(),flush=True)", shell=shell)
    assert result['interactive'] and not result['tty']
    value = send(result, '中文 input\n', eof=True, request_id='input-1')
    assert value['input_status'] == 'accepted', value
    final, output = finish(value)
    assert final['status'] == 'completed', (final, output)
    assert 'got:中文 input' in output
    assert not [p for p in logs.glob('*.input.*.json') if not p.name.endswith('.ack.json')]


@pytest.mark.parametrize('shell', ['cmd', 'powershell'] if os.name == 'nt' else ['sh'])
def test_real_tty_and_multiple_inputs(logs, shell):
    result = launch(logs, "import sys\nprint('tty='+str(sys.stdin.isatty()),flush=True)\na=input('first>')\nprint('one:'+a,flush=True)\nb=input('second>')\nprint('two:'+b,flush=True)",
                    shell=shell, tty=True)
    pieces = [result.get('output', '')]
    value = send(result, 'alpha\r', request_id='tty-1')
    assert value['input_status'] == 'accepted', value
    pieces.append(value.get('output', ''))
    value = send(value, 'beta\r', request_id='tty-2')
    assert value['input_status'] == 'accepted', value
    final, output = finish(value)
    output = ''.join(pieces) + output
    assert final['status'] == 'completed', (final, output)
    assert 'tty=True' in output and 'one:alpha' in output and 'two:beta' in output, output


def test_input_retry_is_not_sent_twice_and_conflicts_are_rejected(logs):
    result = launch(logs, "import sys\nfor line in sys.stdin: print('echo:'+line,flush=True)")
    one = send(result, 'once\n', request_id='repeatable')
    two = send(one, 'once\n', request_id='repeatable')
    assert one['input_status'] == two['input_status'] == 'accepted'
    bad = send(two, 'different\n', request_id='repeatable')
    assert bad['success'] is False and 'request_id' in bad['error']
    final, output = finish(send(two, eof=True))
    assert (one['output'] + two['output'] + output).count('echo:once') == 1
    assert final['status'] == 'completed'


def test_minimal_input_and_receipt_query_share_automatic_read_position(logs):
    initial = launch(logs, "import sys\nprint('ready',flush=True)\nfor line in sys.stdin: print('echo:'+line.strip(),flush=True)")
    args = {'job_id': initial['job_id'], '_session_id': 'interactive-test'}
    one = bt.try_execute_builtin_command_tool('write_stdin', {**args, 'chars': 'once\n', 'request_id': 'one'})
    receipt = bt.try_execute_builtin_command_tool('write_stdin', {**args, 'request_id': 'one'})
    assert receipt['input_status'] == one['input_status'] == 'accepted'
    assert bt.try_execute_builtin_command_tool('write_stdin', {**args, 'request_id': 'missing'})['success'] is False
    value = bt.try_execute_builtin_command_tool('write_stdin', {**args, 'eof': True})
    pieces = [initial['output'], one['output'], receipt['output'], value['output']]
    deadline = time.monotonic() + 12
    while value['status'] == 'running' or value.get('has_more'):
        assert time.monotonic() < deadline, value
        value = bt.try_execute_builtin_command_tool('poll_command', args)
        pieces.append(value['output'])
    text = ''.join(pieces)
    assert text.count('ready') == text.count('echo:once') == 1, text
    assert value['status'] == 'completed'


def test_input_owner_agent_session_and_noninteractive_are_checked(logs):
    owner = SimpleNamespace(id='interactive-owner')
    token = CURRENT.set(owner)
    try:
        result = launch(logs, 'import time;time.sleep(30)', _command_owner_agent='child')
        assert send(result, 'bad')['success'] is False
        assert send(result, 'bad', _command_owner_agent='child', _session_id='wrong')['success'] is False
    finally:
        CURRENT.reset(token)
    assert send(result, 'bad', _command_owner_agent='child')['success'] is False
    command_leases.cancel_owned(owner.id, 'child')
    final, _ = finish(result)
    assert final['status'] == 'stopped'


def test_interactive_deadline_and_explicit_stop(logs):
    result = launch(logs, 'input()', tty=True, timeout_seconds=3, yield_time_ms=0)
    final, _ = finish(result)
    assert final['status'] == 'timeout' and final['timed_out']
    other = launch(logs, 'input()', tty=True)
    stopped = bt.try_execute_builtin_command_tool('stop_command', {
        'job_id': other['job_id'], '_session_id': 'interactive-test'})
    assert stopped['status'] == 'stopped'
    assert send(other, 'late')['success'] is False


@pytest.mark.parametrize('args', [
    {'interactive': 'yes'}, {'tty': 1}, {'interactive': True, 'background': True},
    {'tty': True, 'background': True},
])
def test_invalid_interactive_options_never_launch(logs, args):
    result = bt.try_execute_builtin_command_tool('run_command', {'command': 'echo bad', **args})
    assert result['success'] is False
    assert not list(logs.glob('*.meta.json'))


def test_input_schema_and_approval_contract(logs):
    assert argument_error('write_stdin', {'job_id': 'bg_test', 'eof': 'yes'})
    assert argument_error('write_stdin', {'job_id': 'bg_test', 'chars': 'a'*16385})
    result = launch(logs, 'input()', tty=True)
    context = {'root': str(logs), 'session': 'interactive-test', 'mode': 'ask'}
    read_plan = plan_tool('write_stdin', {'job_id': result['job_id']}, context)
    write_plan = plan_tool('write_stdin', {'job_id': result['job_id'], 'chars': 'y\r'}, context)
    assert not read_plan['needs_approval']
    assert write_plan['needs_approval'] and write_plan['reason'] == 'terminal_input'
    assert write_plan['terminal_identity']['job_id'] == result['job_id']
    assert send(result, eof=True)['success'] is False


def test_invalid_cursor_has_no_input_side_effect(logs):
    result = launch(logs, "import sys\nfor line in sys.stdin: print('received:'+line,flush=True)")
    bad = send(result, 'must-not-send\n', cursor='invalid-cursor')
    assert bad['success'] is False
    final, output = finish(send(result, eof=True))
    assert final['status'] == 'completed' and 'must-not-send' not in output


def test_worker_death_reaps_real_interactive_pty(logs):
    code = f'''import json,time,sys
from pathlib import Path
from factory.agent_runtime import builtin_tools as bt
bt._BG_LOG_DIR=Path({str(logs)!r})
result=bt.try_execute_builtin_command_tool('run_command',{{
'command': '"'+sys.executable+'" -X utf8 -u -c "input()"',
'shell': {'cmd' if os.name == 'nt' else 'sh'!r},'work_dir':{str(logs)!r},
'tty':True,'timeout_seconds':30,'_session_id':'interactive-test'}})
Path({str(logs/'worker-result.json')!r}).write_text(json.dumps(result),encoding='utf-8')
time.sleep(30)
'''
    worker = subprocess.Popen([sys.executable, '-c', code], creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    try:
        deadline = time.monotonic()+12
        saved = logs/'worker-result.json'
        while not saved.exists():
            assert time.monotonic()<deadline and worker.poll() is None
            time.sleep(0.05)
        result=json.loads(saved.read_text(encoding='utf-8'))
        assert 'job_id' in result, result
        worker.terminate(); worker.wait(timeout=5)
        final,_=finish(result)
        assert final['status']=='stopped'
        assert final['termination_confirmed']
    finally:
        if worker.poll() is None:
            worker.terminate();worker.wait(timeout=5)


def test_exited_process_with_open_handle_has_no_live_identity():
    from util.process_identity import process_identity
    worker=subprocess.Popen([sys.executable,'-c','pass'],creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    worker.wait(timeout=5)
    assert process_identity(worker.pid) is None


def test_job_validation_does_not_resolve_disappearing_lease_leaf(logs,monkeypatch):
    lease=logs/('bg_lease_'+'a'*32+'.json')
    done=logs/'bg_paths.done.txt'
    done.write_text('0')
    metadata={'job_id':'bg_paths','pid':99999999,'session_id':'interactive-test',
              'output_path':str(logs/'bg_paths.out.txt'),'completion_path':str(done),'lease_path':str(lease)}
    path=logs/'bg_paths.meta.json'
    command_jobs.write_metadata(path,metadata)
    original=type(lease).resolve
    def stable(self,*args,**kwargs):
        assert self!=lease,'must resolve stable parent instead of ephemeral leaf'
        return original(self,*args,**kwargs)
    monkeypatch.setattr(type(lease),'resolve',stable)
    command_jobs.read_job('bg_paths','interactive-test')
    metadata['lease_path']=str(logs.parent/lease.name)
    command_jobs.write_metadata(path,metadata)
    with pytest.raises(ValueError,match='lease_path'):
        command_jobs.read_job('bg_paths','interactive-test')
    metadata.pop('lease_path')
    command_jobs.write_metadata(path,metadata)


def test_input_to_closed_pipe_fails_without_replay(logs):
    from factory.agent_runtime import command_input
    spec=logs/'bg_lease_closed.json'
    request=logs/'bg_lease_closed.input.abc.json'
    command_jobs.write_metadata(request,{'fingerprint':'hash','chars':'data','eof':False})
    alive=[True]
    def broken(_):
        alive[0]=False
        raise BrokenPipeError('pipe closed')
    command_input.serve(spec,broken,lambda:None,lambda:alive[0])
    ack=request.with_name(request.stem+'.ack.json')
    result=json.loads(ack.read_text(encoding='utf-8'))
    assert result['status']=='failed' and 'closed' in result['error']
    assert not request.exists()


def test_receipt_commit_retries_windows_occupation_without_replaying_input(logs,monkeypatch):
    from factory.agent_runtime import command_input
    spec=logs/'bg_lease_receipt.json'
    request=logs/'bg_lease_receipt.input.abc.json'
    command_jobs.write_metadata(request,{'fingerprint':'hash','chars':'exactly once','eof':False})
    original=os.replace;delivered=[];failures=[];alive=[True]
    def replace(source,target):
        # The initial 'writing' marker is durable. Only its final accepted
        # replacement collides, after stdin has already been delivered.
        if str(target).endswith('.ack.json') and delivered and len(failures)<2:
            failures.append(True)
            raise PermissionError('simulated Windows reader holds receipt')
        return original(source,target)
    monkeypatch.setattr(command_jobs.os,'replace',replace)
    def write(chars):
        delivered.append(chars);alive[0]=False
    command_input.serve(spec,write,lambda:None,lambda:alive[0])
    receipt=json.loads(request.with_name(request.stem+'.ack.json').read_text(encoding='utf8'))
    assert failures==[True,True] and delivered==['exactly once']
    assert receipt['status']=='accepted' and receipt['accepted_bytes']==12
    assert not request.exists() and not list(logs.glob('*.tmp'))
