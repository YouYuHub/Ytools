"""Bounded, acknowledged stdin requests for a live command supervisor."""
import hashlib
import json
from pathlib import Path
import time
import uuid

from util.file_lock import cross_process_lock
from . import command_jobs

MAX_INPUT_BYTES = 65536
MAX_REQUESTS = 1024


def identity(meta):
    return {key: meta.get(key) for key in
            ('job_id', 'pid', 'process_identity', 'lease_path', 'owner_task', 'owner_agent')}


def _owned(meta, agent):
    from .permission_runtime import CURRENT
    current = CURRENT.get()
    if meta.get('owner_task') != (current.id if current else None) or meta.get('owner_agent') != agent:
        raise PermissionError('交互终端不属于当前任务或代理；请在当前任务新建交互终端')
    plan = current.active_plan.get() if current and hasattr(current, 'active_plan') else None
    if plan and plan.get('terminal_identity') != identity(meta):
        raise PermissionError('交互终端身份已变化，请重新调用并确认')


def send(job_id, session_id, chars, eof, request_id, cursor, wait_seconds, agent, max_chars=12000, encoding=''):
    from .tool_control import check_tool_control
    _, meta = command_jobs.read_job(job_id, session_id)
    if not meta.get('interactive') or not meta.get('lease_path'):
        raise ValueError('该命令未启用 interactive；请用 run_command(interactive=true) 启动')
    _owned(meta, agent)
    # Validate a supplied cursor before sending input: a decoding/contract error
    # must not hide the fact that a command already received a side effect.
    from .command_output import read
    read(meta, cursor, 512, finished=command_jobs.snapshot(meta)['status'] != 'running', encoding=encoding)
    if eof and meta.get('tty'):
        raise ValueError('eof 仅用于管道 stdin；PTY 请发送程序支持的控制字符或使用 stop_command')
    receipt_only = bool(request_id) and not chars and not eof
    request_id = request_id or uuid.uuid4().hex
    fingerprint = hashlib.sha256(json.dumps([chars, eof], ensure_ascii=False).encode('utf-8')).hexdigest()
    spec = Path(meta['lease_path'])
    prefix = spec.with_suffix('.input.' + hashlib.sha256(request_id.encode()).hexdigest())
    request = Path(str(prefix) + '.json')
    ack = Path(str(prefix) + '.ack.json')
    response = None
    if chars or eof:
        with cross_process_lock(spec.with_suffix('.input.lock'), timeout=3):
            check_tool_control()
            _, latest = command_jobs.read_job(job_id, session_id)
            _owned(latest, agent)
            if identity(meta) != identity(latest):
                raise PermissionError('交互终端身份已变化')
            if ack.exists():
                response = json.loads(ack.read_text(encoding='utf-8'))
            elif request.exists():
                response = json.loads(request.read_text(encoding='utf-8'))
            else:
                if spec.with_suffix('.outcome.json').exists():
                    raise ValueError('交互终端已结束，不能发送输入')
                live = command_jobs.snapshot(latest)
                if live['status'] != 'running' or not live['can_stop']:
                    raise ValueError('交互终端已结束或进程身份无效，不能发送输入')
                if len(list(spec.parent.glob(spec.stem + '.input.*.json'))) >= MAX_REQUESTS:
                    raise ValueError('本终端输入请求数已达 1024；请新建终端')
                response = {'fingerprint': fingerprint, 'chars': chars, 'eof': eof, 'status': 'queued'}
                command_jobs.write_metadata(request, response)
            if response.get('fingerprint') != fingerprint:
                raise ValueError('request_id 已用于不同输入；重试须保持输入原文一致')
    elif receipt_only:
        if ack.exists():
            response = json.loads(ack.read_text(encoding='utf-8'))
        elif request.exists():
            response = json.loads(request.read_text(encoding='utf-8'))
        else:
            raise ValueError('request_id 不存在；查询回执须使用此前返回的标识')
    if chars or eof or receipt_only:
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            check_tool_control()
            if ack.exists():
                response = json.loads(ack.read_text(encoding='utf-8'))
                if response.get('status') != 'writing':
                    break
            time.sleep(0.02)
    try:
        result = command_jobs.incremental(job_id, session_id, cursor, max_chars, wait_seconds, encoding=encoding)
    except ValueError as exc:
        if 'OUTPUT_RESET' not in str(exc):
            raise
        # Input has already been acknowledged. Recover output without sending it again.
        result = command_jobs.incremental(job_id, session_id, '', max_chars, 0, encoding=encoding)
        result['output_reset'] = True
    result.update(request_id=request_id, input_status=(response or {}).get('status', 'not_sent'),
                  input_bytes=(response or {}).get('accepted_bytes', 0))
    if response and response.get('error'):
        result['input_error'] = response['error']
    return result


def serve(spec_path, write, close, alive):
    """Single daemon writer; blocking child stdin never blocks the lifetime guard."""
    spec = Path(spec_path)
    pattern = spec.stem + '.input.*.json'
    while alive():
        requests = sorted((p for p in spec.parent.glob(pattern) if not p.name.endswith('.ack.json')),
                          key=lambda p: (p.stat().st_mtime_ns, p.name))
        for request in requests:
            ack = request.with_name(request.stem + '.ack.json')
            payload = {}
            try:
                if request.stat().st_size > MAX_INPUT_BYTES * 7:
                    raise ValueError('input request too large')
                payload = json.loads(request.read_text(encoding='utf-8'))
                if ack.exists():
                    request.unlink(missing_ok=True)
                    continue
                value = {'fingerprint': payload['fingerprint'], 'status': 'writing'}
                command_jobs.write_metadata(ack, value)
                request.unlink(missing_ok=True)  # Never replay an uncertain partial write.
                chars = payload['chars']
                if not isinstance(chars, str) or len(chars.encode('utf-8')) > MAX_INPUT_BYTES:
                    raise ValueError('invalid input size')
                if not alive():
                    raise BrokenPipeError('terminal exited')
                if chars:
                    write(chars)
                if payload.get('eof'):
                    close()
                value.update(status='accepted', accepted_bytes=len(chars.encode('utf-8')))
                command_jobs.write_metadata(ack, value)
            except (OSError, EOFError, ValueError, KeyError, TypeError) as exc:
                value = {'fingerprint': payload.get('fingerprint'),
                         'status': 'failed', 'error': str(exc)}
                command_jobs.write_metadata(ack, value)
                request.unlink(missing_ok=True)
        time.sleep(0.02)


def finish_pending(spec_path):
    """Erase queued input bodies; acknowledgments contain no stdin text."""
    spec = Path(spec_path)
    with cross_process_lock(spec.with_suffix('.input.lock'), timeout=3):
        for request in spec.parent.glob(spec.stem + '.input.*.json'):
            if request.name.endswith('.ack.json'):
                continue
            try:
                payload = json.loads(request.read_text(encoding='utf-8'))
                command_jobs.write_metadata(request.with_name(request.stem + '.ack.json'),
                    {'fingerprint': payload.get('fingerprint'), 'status': 'failed',
                     'error': 'terminal exited before input'})
            except (OSError, ValueError):
                pass
            finally:
                request.unlink(missing_ok=True)
