import codecs
import os
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from factory.agent_runtime import builtin_tools as bt, command_jobs, command_output


@pytest.fixture
def job(tmp_path, monkeypatch):
    monkeypatch.setattr(bt, '_BG_LOG_DIR', tmp_path)
    output = tmp_path / 'bg_cursor.out.txt'
    output.write_bytes(b'')
    done = tmp_path / 'bg_cursor.done.txt'
    identifier = bt._save_command_job(os.getpid(), output, done, 'auto', str(tmp_path), 's')
    _, meta = command_jobs.read_job(identifier, 's')
    return identifier, meta, output, done


def test_incremental_has_no_duplicate_or_missing_output(job):
    identifier, meta, output, done = job
    output.write_bytes(('hello\n' * 300 + '中文\n').encode())
    cursor, parts = '', []
    for _ in range(10):
        page = command_jobs.action(identifier, 'poll', 's', cursor=cursor, max_chars=512)
        parts.append(page['output'])
        cursor = page['next_cursor']
        if not page['has_more']:
            break
    assert ''.join(parts) == output.read_text(encoding='utf-8')
    assert command_jobs.action(identifier, 'poll', 's', cursor=cursor)['output'] == ''
    with output.open('ab') as handle:
        handle.write('新增\n'.encode())
    assert command_jobs.action(identifier, 'poll', 's', cursor=cursor)['output'] == '新增\n'
    assert 'output_tail' not in page


def test_partial_utf8_and_bom_boundaries(job):
    _, meta, output, done = job
    output.write_bytes(b'a' * 511 + '中文'.encode())
    first = command_output.read(meta, '', 512)
    assert first['output'] == 'a' * 511
    second = command_output.read(meta, first['next_cursor'], 512)
    assert second['output'] == '中文'
    output.write_bytes(codecs.BOM_UTF16_LE + '甲乙中文\n'.encode('utf-16-le'))
    first = command_output.read(meta, '', 5)
    second = command_output.read(meta, first['next_cursor'], 512, finished=True)
    assert first['output'] + second['output'] == '甲乙中文\n'


def test_mixed_lines_and_explicit_encoding(job):
    _, meta, output, _ = job
    output.write_bytes('中文\n'.encode() + '你好\n'.encode('gbk'))
    assert command_output.read(meta, '', 512, finished=True)['output'] == '中文\n你好\n'
    output.write_bytes('你好'.encode('gbk'))
    assert command_output.read(meta, '', 512, finished=True, encoding='gbk')['output'] == '你好'


def test_reset_invalid_cursor_and_session_rejected(job):
    identifier, meta, output, done = job
    output.write_bytes(b'old content')
    first = command_output.read(meta, '', 512)
    output.write_bytes(b'new')
    with pytest.raises(ValueError, match='OUTPUT_RESET'):
        command_output.read(meta, first['next_cursor'], 512)
    with pytest.raises(ValueError, match='cursor'):
        command_output.read(meta, 'not-a-cursor', 512)
    with pytest.raises(ValueError, match='不属于'):
        command_jobs.action(identifier, 'poll', 'other', cursor='')


def test_wait_returns_new_output_without_holding_stop_lock(job):
    identifier, meta, output, done = job
    def append():
        time.sleep(0.08)
        with output.open('ab') as handle:
            handle.write(b'ready\n')
    thread = threading.Thread(target=append)
    thread.start()
    started = time.monotonic()
    result = command_jobs.action(identifier, 'poll', 's', cursor='', wait_seconds=1)
    thread.join()
    assert result['output'] == 'ready\n'
    assert time.monotonic() - started < 1


def test_poll_validation_and_legacy_tail(job):
    identifier, meta, output, done = job
    output.write_bytes(b'tail\n')
    result = bt.try_execute_builtin_command_tool('poll_command', {'job_id': identifier, '_session_id': 's', 'cursor': ''})
    assert result['output'] == 'tail\n'
    assert command_jobs.action(identifier, 'poll', 's')['output_tail'] == 'tail\n'
    assert bt.try_execute_builtin_command_tool('poll_command', {'job_id': identifier, '_session_id': 's', 'wait_seconds': 0})['output'] == ''
    for extra in ({'cursor': '', 'tail_lines': 5}, {'tail_lines': 5, 'wait_seconds': 1}, {'cursor': '', 'wait_seconds': 31}):
        result = bt.try_execute_builtin_command_tool('poll_command', {'job_id': identifier, '_session_id': 's', **extra})
        assert result['success'] is False
