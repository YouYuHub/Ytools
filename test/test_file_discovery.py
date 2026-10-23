from pathlib import Path
from unittest.mock import patch

import pytest

from factory.agent_runtime import builtin_tools as bt
from factory.agent_runtime import query_pages
from factory.agent_runtime.tool_validation import argument_error
from factory.agent_runtime.permission_policy import plan_tool


def test_list_files_does_not_read_contents_and_paginates(tmp_path):
    for name in ('a.py', 'b.py', 'c.txt'):
        (tmp_path / name).write_text('text', encoding='utf-8')
    args = {'dir_path': str(tmp_path), 'pattern': '*.py', 'max_results': 1}
    with patch.object(Path, 'read_bytes', side_effect=AssertionError('must not read')):
        first = bt.execute_list_files(args)
        second = bt.execute_list_files({**args, 'cursor': first['next_cursor']})
    assert first['has_more'] and not second['has_more']
    assert set(first['files'] + second['files']) == {bt._display_path(tmp_path / name) for name in ('a.py', 'b.py')}
    assert first['scan_complete'] and first['truncated']


def test_relative_globs_and_exclusions(tmp_path):
    (tmp_path / 'src' / 'deep').mkdir(parents=True)
    for name in ('src/a.py', 'src/deep/b.py', 'root.py', '.secret'):
        (tmp_path / name).write_text('target', encoding='utf-8')
    args = {'dir_path': str(tmp_path), 'pattern': 'src/**/*.py'}
    files = bt.execute_list_files(args)['files']
    assert len(files) == 2
    content = bt.execute_search_files({'dir_path': str(tmp_path), 'pattern': 'target', 'file_pattern': 'src/**/*.py'})
    assert content['total_matches'] == 2
    assert all('.secret' not in path for path in bt.execute_list_files({'dir_path': str(tmp_path)})['files'])


def test_content_cursor_reuses_snapshot_and_preserves_every_match(tmp_path):
    for i in range(4):
        (tmp_path / f'{i}.txt').write_text(f'target {i}\n', encoding='utf-8')
    args = {'dir_path': str(tmp_path), 'pattern': 'target', 'max_results': 1, 'count_total': True}
    first = bt.execute_search_files(args)
    messages = [first['message']]
    page = first
    with patch.object(bt, '_read_bytes_with_limit', side_effect=AssertionError('must reuse')):
        while page['has_more']:
            page = bt.execute_search_files({**args, 'cursor': page['next_cursor']})
            messages.append(page['message'])
    assert len(messages) == 4
    for i in range(4):
        assert f'target {i}' in '\n'.join(messages)


def test_cursor_rejects_changed_files_query_and_expiration(tmp_path):
    for i in range(2):
        (tmp_path / f'{i}.txt').write_text('hit', encoding='utf-8')
    args = {'dir_path': str(tmp_path), 'pattern': 'hit', 'max_results': 1}
    first = bt.execute_search_files(args)
    with pytest.raises(ValueError, match='CURSOR_EXPIRED'):
        bt.execute_search_files({**args, 'pattern': 'other', 'cursor': first['next_cursor']})
    (tmp_path / '0.txt').write_text('changed', encoding='utf-8')
    with pytest.raises(ValueError, match='FILE_QUERY_CHANGED'):
        bt.execute_search_files({**args, 'cursor': first['next_cursor']})
    with query_pages._lock:
        query_pages._cache.clear()
    with pytest.raises(ValueError, match='CURSOR_EXPIRED'):
        bt.execute_search_files({**args, 'cursor': first['next_cursor']})


def test_tiny_pages_make_progress_inside_large_block(tmp_path):
    (tmp_path / 'file.txt').write_text(''.join(f'hit {i} ' + 'x' * 110 + '\n' for i in range(30)), encoding='utf-8')
    args = {'dir_path': str(tmp_path), 'pattern': 'hit', '_max_chars': 512}
    result = bt.execute_search_files(args)
    cursors, messages = set(), []
    for _ in range(80):
        assert len(result['message']) <= 512
        messages.append(result['message'])
        if not result['has_more']:
            break
        assert result['next_cursor'] not in cursors
        cursors.add(result['next_cursor'])
        result = bt.execute_search_files({**args, 'cursor': result['next_cursor']})
    else:
        pytest.fail('cursor did not finish')
    for i in range(30):
        assert f'hit {i} ' in '\n'.join(messages)


def test_injection_schema_and_permission_plan(tmp_path):
    definitions, servers = bt.inject_builtin_tools([], {}, include_search_files=True)
    assert servers['list_files'] == '__builtin__'
    assert argument_error('list_files', {}) is None
    assert argument_error('list_files', {'cursor': 5})
    plan = plan_tool('list_files', {}, {'root': str(tmp_path), 'mode': 'workspace', 'session': 's'})
    assert plan['paths'] == [str(tmp_path.resolve())]
    assert not plan['reason']


def test_skipped_protected_file_does_not_invalidate_cursor(tmp_path, monkeypatch):
    import contextvars
    from types import SimpleNamespace
    from factory.agent_runtime import permission_runtime as runtime
    for name in ('a', 'b', 'secret'):
        (tmp_path / name).write_text('hit', encoding='utf-8')
    monkeypatch.setattr(runtime, 'protected', lambda path: Path(path).name == 'secret')
    current = SimpleNamespace(session='s', context={'mode': 'workspace', 'root': str(tmp_path.resolve())},
                              active_plan=contextvars.ContextVar('plan', default=None), media_grants=set())
    token = runtime.CURRENT.set(current)
    try:
        args = {'dir_path': str(tmp_path), 'pattern': 'hit', 'max_results': 1, 'count_total': True}
        first = bt.execute_search_files(args)
        second = bt.execute_search_files({**args, 'cursor': first['next_cursor']})
        assert first['has_more'] and not second['has_more']
        assert not first['scan_complete']
        assert first['skipped_permission'] == 1
    finally:
        runtime.CURRENT.reset(token)
