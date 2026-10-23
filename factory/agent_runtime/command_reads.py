"""Durable model read positions; HTTP/tail previews never advance these cursors."""
from contextlib import ExitStack
import codecs
import hashlib
import time

from util.file_lock import cross_process_lock
from . import command_jobs, command_output

MAX_READERS = 64


def _reader(session_id, agent):
    from .permission_runtime import CURRENT
    current = CURRENT.get()
    value = repr((session_id, current.id if current else None, agent))
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _save(path, session_id, reader, cursor, encoding):
    # Re-read under the registry lock so stop/close and other readers are preserved.
    with cross_process_lock(path.with_suffix('.lock'), timeout=2):
        _, meta = command_jobs.read_job(path.name.removesuffix('.meta.json'), session_id)
        readers = meta.setdefault('output_readers', {})
        readers[reader] = {'cursor': cursor, 'encoding': encoding, 'updated_at': time.time()}
        while len(readers) > MAX_READERS:
            oldest = min((key for key in readers if key != reader),
                         key=lambda key: readers[key].get('updated_at', 0))
            del readers[oldest]
        command_jobs.write_metadata(path, meta)


def seed(result, session_id, agent):
    """Remember output already included in run_command's initial response."""
    if not result.get('job_id') or 'next_cursor' not in result:
        return result
    try:
        path, _ = command_jobs.read_job(result['job_id'], session_id)
        _save(path, session_id, _reader(session_id, agent), result['next_cursor'], '')
    except (OSError, ValueError, TimeoutError) as exc:
        result.update(cursor_saved=False, cursor_warning=f'输出位置未保存；后续请传 next_cursor：{exc}')
    return result


def read(job_id, session_id, agent, *, cursor=None, wait_seconds=1, encoding=None,
         max_chars=12000, send=None):
    """Serialize one consumer's read/advance, including optional acknowledged input.

    Explicit cursor replays also establish the next automatic position. Automatic
    recovery from a replaced log is reported; invalid explicit cursors still fail.
    """
    path, _ = command_jobs.read_job(job_id, session_id)  # Validate before creating locks.
    reader = _reader(session_id, agent)
    with ExitStack() as stack:
        try:
            stack.enter_context(cross_process_lock(path.with_suffix('.reader.' + reader + '.lock'), timeout=2))
        except TimeoutError as exc:
            raise ValueError('同一任务正在读取此命令输出；请等待该调用返回后再 poll_command') from exc
        _, meta = command_jobs.read_job(job_id, session_id)
        saved = meta.get('output_readers', {}).get(reader, {})
        automatic = cursor is None
        position = saved.get('cursor', '') if automatic else cursor
        codec = saved.get('encoding', '') if encoding is None else encoding
        if codec:
            codecs.lookup(codec)
        reset = False
        try:
            command_output.read(meta, position, 512,
                                finished=command_jobs.snapshot(meta)['status'] != 'running', encoding=codec)
        except ValueError as exc:
            if not automatic or 'OUTPUT_RESET' not in str(exc):
                raise
            position, reset = '', True
        try:
            result = (send(position, wait_seconds, codec) if send else
                      command_jobs.incremental(job_id, session_id, position, max_chars, wait_seconds, encoding=codec))
        except ValueError as exc:
            if send or not automatic or 'OUTPUT_RESET' not in str(exc):
                raise
            result = command_jobs.incremental(job_id, session_id, '', max_chars, 0, encoding=codec)
            reset = True
        if reset:
            result['output_reset'] = True
        try:
            _save(path, session_id, reader, result['next_cursor'], codec)
        except (OSError, ValueError, TimeoutError) as exc:
            # Never hide successful input/output just because checkpoint persistence failed.
            result.update(cursor_saved=False, cursor_warning=f'输出位置未保存；后续请传 next_cursor：{exc}')
        return result
