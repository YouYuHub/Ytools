"""Strict context patches. Validate all files first; report any incomplete rollback."""
from contextlib import ExitStack
import hashlib
import os
from pathlib import Path
import re
import stat
import tempfile

from .permission_runtime import guard_path
from .tool_control import check_tool_control, remaining_seconds, tool_deadline
from util.file_lock import cross_process_lock

_PATCH_DIFF_MAX_CHARS = 60000  # 全批展示文本上限；完整逐文件统计仍保留。


def _display_diff(bt, item, budget):
    diff = bt._build_file_diff(item['old_text'], item['new_text'], item['path'])
    diff.update(path=str(item['path']), display_path=bt._display_path(item['path']), action=item['action'],
                old_exists=item['old_bytes'] is not None, new_exists=item['action'] != 'delete')
    text = diff['diff']
    if len(text) > budget:
        # 不截断半行/HTML；展示被省略不等于修改行数为 0。
        diff['diff'] = text[:budget].rsplit('\n', 1)[0] if '\n' in text[:budget] else ''
        diff['diff_truncated'] = True
    return diff


def _batch_diff(diffs):
    available = sum(d['lines_added'] is not None and d['lines_removed'] is not None for d in diffs)
    return {'files': diffs, 'file_count': len(diffs),
            'lines_added': sum(d['lines_added'] or 0 for d in diffs),
            'lines_removed': sum(d['lines_removed'] or 0 for d in diffs),
            'stats_complete': available == len(diffs), 'stats_available_files': available,
            'diff_truncated': any(d['diff_truncated'] for d in diffs)}


def parse(patch):
    if not isinstance(patch, str) or len(patch) > 2 * 1024 * 1024:
        raise ValueError('patch 必须是最多 2 Mi 字符的字符串')
    lines = patch.replace('\r\n', '\n').strip('\n').split('\n')
    if len(lines) < 3 or lines[0] != '*** Begin Patch' or lines[-1] != '*** End Patch':
        raise ValueError('补丁必须以 *** Begin Patch 开始、*** End Patch 结束')
    operations, current = [], None
    for line in lines[1:-1]:
        match = re.fullmatch(r'\*\*\* (Add|Update|Delete) File: (.+)', line)
        if match:
            if len(match[2]) > 4096:
                raise ValueError('补丁路径过长')
            current = {'action': match[1].lower(), 'path': match[2], 'lines': []}
            operations.append(current)
        elif current is None:
            raise ValueError('缺少文件操作头')
        else:
            current['lines'].append(line)
    if not 1 <= len(operations) <= 20:
        raise ValueError('补丁每次允许修改 1–20 个文件')
    paths = [os.path.normcase(str(Path(op['path']).expanduser().resolve())) for op in operations]
    if len(set(paths)) != len(paths):
        raise ValueError('同一文件只能出现一次，请合并修改区块')
    return operations


def _positions(lines, needle, start, eof=False):
    """KMP keeps repetitive source/context linear; two matches suffice to reject ambiguity."""
    prefix = [0] * len(needle)
    matched = 0
    for index in range(1, len(needle)):
        if index % 1024 == 0:
            check_tool_control()
        while matched and needle[index] != needle[matched]:
            matched = prefix[matched - 1]
        if needle[index] == needle[matched]:
            matched += 1
        prefix[index] = matched
    positions, matched = [], 0
    for index in range(start, len(lines)):
        if index % 1024 == 0:
            check_tool_control()
        while matched and lines[index] != needle[matched]:
            matched = prefix[matched - 1]
        if lines[index] == needle[matched]:
            matched += 1
        if matched == len(needle):
            if not eof or index + 1 == len(lines):
                positions.append(index - matched + 1)
                if len(positions) == 2:
                    break
            matched = prefix[matched - 1]
    return positions


def _update(original, patch_lines):
    if original.count('\n') + original.count('\r') - original.count('\r\n') > 200000:
        raise ValueError('补丁单文件最多 200000 行；请改用 edit_file 定位修改')
    eol = '\r\n' if '\r\n' in original else '\r' if '\r' in original else '\n'
    raw = re.findall(r'[^\r\n]*(?:\r\n|\r|\n|$)', original)
    if raw and raw[-1] == '':
        raw.pop()
    logical = [line.rstrip('\r\n') for line in raw]
    output, cursor, i = [], 0, 0
    if not patch_lines:
        raise ValueError('Update File 缺少修改区块')
    while i < len(patch_lines):
        if not patch_lines[i].startswith('@@'):
            raise ValueError('修改区块须以 @@ 或 @@ 上下文定位行 开始')
        anchor = patch_lines[i][2:]
        if anchor.startswith(' '):
            anchor = anchor[1:]
        search_from = cursor
        if anchor:
            anchors = _positions(logical, [anchor], cursor)
            if len(anchors) != 1:
                raise ValueError('@@ 定位行不存在或不唯一，请提供更多上下文')
            search_from = anchors[0]
        i += 1
        hunk, eof = [], False
        while i < len(patch_lines) and not patch_lines[i].startswith('@@'):
            line = patch_lines[i]
            if line == '*** End of File':
                eof = True
            elif line[:1] in {' ', '+', '-'}:
                hunk.append((line[0], line[1:]))
            else:
                raise ValueError('区块每行须以空格、+ 或 - 开始；暂不支持 Move 操作')
            i += 1
        before = [value for marker, value in hunk if marker != '+']
        if not before:
            raise ValueError('插入区块至少提供一行现有上下文')
        positions = _positions(logical, before, search_from, eof)
        if len(positions) != 1:
            raise ValueError('PATCH_CONTEXT_MISMATCH：原文不存在或匹配多处，请重新读取并增加上下文')
        start = positions[0]
        output.extend(raw[cursor:start])
        pos = start
        for marker, value in hunk:
            if marker == '+':
                output.append(value + eol)
            else:
                if marker == ' ':
                    output.append(raw[pos])
                pos += 1
        cursor = pos
    output.extend(raw[cursor:])
    for n in range(len(output) - 1):
        if not output[n].endswith(('\n', '\r')):
            output[n] += eol
    if output and not original.endswith(('\n', '\r')):
        output[-1] = output[-1].rstrip('\r\n')
    return ''.join(output)


def _replace(path, data, mode, *, create=False, expected_version):
    from .builtin_tools import _assert_file_version
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '.', suffix='.patch', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temporary, mode)
        _assert_file_version(path, expected_version)
        if create:
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def execute(args):
    from . import builtin_tools as bt
    operations = parse(args.get('patch'))
    for op in operations:
        op['path'] = Path(op['path']).expanduser().resolve()
        guard_path(op['path'])
    staged, committed, reports = [], [], []
    with tool_deadline(30), ExitStack() as locks:
        for path in sorted((op['path'] for op in operations), key=lambda p: os.path.normcase(str(p))):
            identity = hashlib.sha256(os.path.normcase(str(path)).encode('utf-8')).hexdigest()
            locks.enter_context(cross_process_lock(Path(tempfile.gettempdir()) / 'ytools-file-locks' / (identity + '.lock'),
                                                    timeout=remaining_seconds(10)))
        total = 0
        diff_budget = _PATCH_DIFF_MAX_CHARS
        for op in operations:
            check_tool_control()
            path, action = op['path'], op['action']
            version = bt._file_version(path)
            if action == 'add':
                if version is not None:
                    raise ValueError(f'FILE_ALREADY_EXISTS：{path}')
                if any(not line.startswith('+') for line in op['lines']):
                    raise ValueError('Add File 的每行须以 + 开始')
                old_bytes, original, encoding, mode = None, '', 'utf-8', None
                updated = '\n'.join(line[1:] for line in op['lines']) + '\n' if op['lines'] else ''
            else:
                old_bytes = bt._read_bytes_with_limit(path, 8 * 1024 * 1024)
                if bt._is_probably_binary(old_bytes):
                    raise ValueError('补丁仅支持文本文件')
                original, encoding = bt._decode_for_edit(old_bytes, '')
                expected = (args.get('_expected_hashes') or {}).get(os.path.normcase(str(path)))
                if expected and expected != bt._content_hash(original):
                    raise ValueError(f'FILE_MODIFIED_EXTERNALLY：{path}')
                mode = stat.S_IMODE(path.stat().st_mode)
                if action == 'delete':
                    if op['lines']:
                        raise ValueError('Delete File 后不接受内容')
                    updated = ''
                else:
                    updated = _update(original, op['lines'])
            new_bytes = updated.encode(encoding)
            if old_bytes and encoding in {'utf-16', 'utf-32'}:
                import codecs
                if old_bytes.startswith(codecs.BOM_UTF16_BE) and encoding == 'utf-16':
                    new_bytes = codecs.BOM_UTF16_BE + updated.encode('utf-16-be')
                elif old_bytes.startswith(codecs.BOM_UTF32_BE) and encoding == 'utf-32':
                    new_bytes = codecs.BOM_UTF32_BE + updated.encode('utf-32-be')
            if len(new_bytes) > 8 * 1024 * 1024:
                raise ValueError('补丁单文件结果超过 8 MiB；请改用 edit_file')
            total += len(old_bytes or b'') + len(new_bytes)
            if total > 64 * 1024 * 1024:
                raise ValueError('补丁修改前后内容合计超过 64 MiB，请分批处理')
            bt._assert_file_version(path, version)
            item = {**op, 'version': version, 'old_bytes': old_bytes, 'new_bytes': new_bytes,
                    'old_text': original, 'new_text': updated, 'encoding': encoding, 'mode': mode}
            item['diff'] = _display_diff(bt, item, diff_budget)
            diff_budget -= len(item['diff']['diff'])
            check_tool_control()
            staged.append(item)
            reports.append({'path': str(path), 'display_path': bt._display_path(path), 'action': action,
                            'status': 'not_applied', 'needs_attention': False})
        committing = None
        try:
            for item, report in zip(staged, reports):
                committing = (item, report)
                check_tool_control()
                path = item['path']
                guard_path(path)
                bt._assert_file_version(path, item['version'])
                if item['action'] == 'delete':
                    path.unlink()
                else:
                    _replace(path, item['new_bytes'], item['mode'], create=item['action'] == 'add', expected_version=item['version'])
                committed.append((item, report))
                report['status'] = 'applied'
                item['after_version'] = bt._file_version(path)
                committing = None
        except BaseException as exc:
            if committing and committing[1]['status'] == 'not_applied':
                item, report = committing
                report['status'] = 'commit_failed'
                try:
                    report['needs_attention'] = bt._file_version(item['path']) != item['version']
                except OSError:
                    report['needs_attention'] = True
            for item, report in reversed(committed):
                try:
                    if 'after_version' not in item:
                        raise OSError('写后文件状态无法确认')
                    bt._assert_file_version(item['path'], item['after_version'])
                    if item['old_bytes'] is None:
                        item['path'].unlink()
                    else:
                        _replace(item['path'], item['old_bytes'], item['mode'], create=item['action'] == 'delete',
                                 expected_version=item['after_version'])
                    report['status'] = 'rolled_back'
                except Exception:
                    report['status'] = 'rollback_failed'
                    report['needs_attention'] = True
            for report in reports:
                report.update(history_recorded=False,
                              history_reason='rollback_failed' if report['needs_attention'] else 'patch_not_applied')
            if not isinstance(exc, Exception):
                raise
            rollback_complete = not any(r['needs_attention'] for r in reports)
            return {'success': False, 'error': 'PATCH_COMMIT_FAILED：' + str(exc), 'files': reports,
                    'rollback_complete': rollback_complete,
                    'needs_attention': not rollback_complete,
                    'message': '提交失败，已回滚本次已提交文件。' if rollback_complete else
                               '提交失败，部分文件状态需核对；请先重新读取 needs_attention 文件，本次未记录回退版本。'}
    diffs = []
    for item, report in zip(staged, reports):
        diff = item['diff']
        report.update(content_hash=bt._content_hash(item['new_text']) if item['action'] != 'delete' else None,
                      lines_added=diff['lines_added'], lines_removed=diff['lines_removed'],
                      diff_skipped=diff['diff_skipped'], diff_truncated=diff['diff_truncated'])
        diffs.append(diff)
    batch = _batch_diff(diffs)
    stats = f"+{batch['lines_added']} -{batch['lines_removed']} 行"
    if not batch['stats_available_files']:
        stats = '增删行数不可用'
    elif not batch['stats_complete']:
        stats = '已统计 ' + stats + '（部分文件统计不可用）'
    return {'success': True, 'message': f'补丁已修改 {len(reports)} 个文件，{stats}', 'files': reports,
            '_file_diff': batch,
            '_file_history': [{'path': str(item['path']), 'old_text': item['old_text'],
                               'new_text': item['new_text'], 'encoding': item['encoding'],
                               'old_exists': item['old_bytes'] is not None,
                               'new_exists': item['action'] != 'delete'} for item in staged]}
