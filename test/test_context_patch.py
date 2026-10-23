import os
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from factory.agent_runtime import builtin_tools as bt, context_patch
from factory.agent_runtime.file_history_result import record_result
from factory.agent_runtime.permission_policy import plan_tool
from factory.agent_runtime.sub_agent_files import SubAgentFileAccess
from memory import file_history
from factory.chat_factory import _format_tool_result
from factory.agent_runtime.sub_agent import _format_result_text


def make_patch(*parts):
    return '\n'.join(['*** Begin Patch', *parts, '*** End Patch'])


def test_add_update_delete_and_history(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(file_history, 'HISTORY_DIFF_ROOT', tmp_path / 'versions')
    (tmp_path / 'old.txt').write_text('remove\n', encoding='utf-8')
    (tmp_path / 'edit.py').write_bytes(b'a\r\nb\r\nc')
    text = make_patch('*** Add File: sub/new.txt', '+hello', '*** Update File: edit.py', '@@',
                      ' a', '-b', '+changed', ' c', '*** Delete File: old.txt')
    result = bt.try_execute_builtin_file_tool('apply_patch', {'patch': text})
    assert result['success'], result
    assert (tmp_path / 'edit.py').read_bytes() == b'a\r\nchanged\r\nc'
    assert (tmp_path / 'sub/new.txt').read_text(encoding='utf-8') == 'hello\n'
    assert not (tmp_path / 'old.txt').exists()
    record_result(result, 'patch-test', 'apply_patch', 1)
    assert result['history_recorded']
    assert all(item['history_recorded'] for item in result['files'])
    assert not any('old_text' in item for item in result['files'])
    assert [(r['lines_added'],r['lines_removed']) for r in result['files']] == [(1,0),(1,1),(0,1)]
    assert result['_file_diff']['file_count'] == 3
    assert len(result['_file_diff']['files']) == 3
    assert all(r['history_recorded'] for r in result['_file_diff']['files'])
    for format_result in (_format_tool_result, _format_result_text):
        model=json.loads(format_result(result))
        assert model['files'][1]['lines_added'] == 1
        assert '_file_diff' not in model and '_file_history' not in model
        assert all('diff' not in item and 'old_text' not in item for item in model['files'])


def test_prevalidation_failure_does_not_write_any_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'a').write_text('old\n', encoding='utf-8')
    result = bt.try_execute_builtin_file_tool('apply_patch', {'patch': make_patch(
        '*** Add File: new', '+created', '*** Update File: a', '@@', '-missing', '+new')})
    assert 'PATCH_CONTEXT_MISMATCH' in result['error']
    assert not (tmp_path / 'new').exists()
    assert (tmp_path / 'a').read_text(encoding='utf-8') == 'old\n'


def test_ambiguous_context_and_existing_create_are_rejected(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'a').write_text('x\nx\n', encoding='utf-8')
    for body in [('*** Update File: a', '@@', '-x', '+y'), ('*** Add File: a', '+x')]:
        result = bt.try_execute_builtin_file_tool('apply_patch', {'patch': make_patch(*body)})
        assert result['success'] is False
    assert (tmp_path / 'a').read_text(encoding='utf-8') == 'x\nx\n'


@pytest.mark.parametrize('external', [False, True])
def test_commit_failure_rolls_back_or_reports_external_conflict(tmp_path, monkeypatch, external):
    monkeypatch.chdir(tmp_path)
    for name in ('a', 'b'):
        (tmp_path / name).write_text('old\n', encoding='utf-8')
    original = context_patch._replace
    def replace(path, *args, **kwargs):
        if path.name == 'b':
            if external:
                (tmp_path / 'a').write_text('external\n', encoding='utf-8')
            raise OSError('simulated commit failure')
        return original(path, *args, **kwargs)
    text = make_patch('*** Update File: a', '@@', '-old', '+new', '*** Update File: b', '@@', '-old', '+new')
    with patch.object(context_patch, '_replace', side_effect=replace):
        result = context_patch.execute({'patch': text})
    assert result['rollback_complete'] is (not external)
    assert (tmp_path / 'a').read_text(encoding='utf-8') == ('external\n' if external else 'old\n')
    assert (tmp_path / 'b').read_text(encoding='utf-8') == 'old\n'
    assert len(result['files']) == 2
    assert result['files'][1]['status'] == 'commit_failed'
    assert result['files'][0]['needs_attention'] is external
    assert '_file_diff' not in result and '_file_history' not in result


def test_patch_diff_limits_keep_stats_and_paths(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(context_patch,'_PATCH_DIFF_MAX_CHARS',80)
    result=context_patch.execute({'patch':make_patch('*** Add File: one.py', '+'+('x'*90),
                                                   '*** Add File: two.py','+a','+b')})
    assert result['success']
    view=result['_file_diff']
    assert sum(len(item['diff']) for item in view['files']) <= 80
    assert view['diff_truncated']
    assert [(r['lines_added'],r['lines_removed']) for r in result['files']] == [(1,0),(2,0)]
    assert all(Path(item['path']).is_absolute() for item in view['files'])


def test_large_patch_file_does_not_claim_zero_changes(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path/'big.txt').write_text('x'*300000+'\n',encoding='utf-8')
    result=context_patch.execute({'patch':make_patch('*** Update File: big.txt','@@',' '+'x'*300000,'+new')})
    assert result['success']
    assert result['files'][0]['lines_added'] is None
    assert result['_file_diff']['stats_complete'] is False
    assert result['_file_diff']['files'][0]['path']
    assert '不可用' in result['message']


def test_failed_operation_that_altered_disk_is_explicitly_flagged(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in ('a','b'):
        (tmp_path/name).write_text('old\n',encoding='utf-8')
    original=context_patch._replace
    def replace(path,*args,**kwargs):
        if path.name=='b':
            path.write_text('partial\n',encoding='utf-8')
            raise OSError('failed after disk mutation')
        return original(path,*args,**kwargs)
    monkeypatch.setattr(context_patch,'_replace',replace)
    result=context_patch.execute({'patch':make_patch('*** Update File: a','@@','-old','+new',
                                                    '*** Update File: b','@@','-old','+new')})
    assert result['needs_attention'] and not result['rollback_complete']
    assert result['files'][0]['status']=='rolled_back'
    assert result['files'][1]['status']=='commit_failed' and result['files'][1]['needs_attention']
    assert (tmp_path/'a').read_text(encoding='utf-8')=='old\n'
    assert (tmp_path/'b').read_text(encoding='utf-8')=='partial\n'
    assert '_file_history' not in result


def test_write_followed_by_unreadable_version_does_not_hide_modified_file(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path)
    original=bt._file_version
    def version(path):
        if path.name=='new' and path.exists():
            raise PermissionError('cannot inspect version')
        return original(path)
    monkeypatch.setattr(bt,'_file_version',version)
    result=context_patch.execute({'patch':make_patch('*** Add File: new','+created')})
    assert result['files'][0]['status']=='rollback_failed'
    assert result['files'][0]['needs_attention'] and result['needs_attention']
    assert (tmp_path/'new').read_text(encoding='utf-8')=='created\n'


def test_patch_paths_are_planned_and_schema_injected(tmp_path):
    text = make_patch(f'*** Add File: {tmp_path / "a"}', '+x', f'*** Delete File: {tmp_path.parent / "outside"}')
    plan = plan_tool('apply_patch', {'patch': text}, {'root': str(tmp_path), 'mode': 'ask'})
    assert len(plan['paths']) == 2
    assert plan['needs_approval']
    definitions, servers = bt.inject_builtin_tools([], {}, include_apply_patch=True)
    assert servers == {'apply_patch': '__builtin__'}
    assert bt.is_builtin_tool('apply_patch')


def test_child_patch_obeys_shared_file_ownership(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'a').write_text('old\n', encoding='utf-8')
    access = SubAgentFileAccess()
    run = lambda agent, name, args: access.execute(agent, name, args, bt.try_execute_builtin_file_tool)
    assert run('one', 'edit_file', {'path': 'a', 'old_string': 'old', 'new_string': 'new'})['success']
    text = make_patch('*** Update File: a', '@@', '-new', '+merged')
    refused = run('two', 'apply_patch', {'patch': text})
    assert refused['code'] == 'SUB_AGENT_FILE_CONFLICT'
    assert run('two', 'read_file', {'path': 'a'})['success']
    assert run('two', 'apply_patch', {'patch': text})['success']


def test_malformed_patch_duplicate_paths_and_unsupported_move():
    for text in ('wrong', make_patch('*** Add File: a', '+x', '*** Add File: ./a', '+y')):
        with pytest.raises(ValueError):
            context_patch.parse(text)


def test_patch_preserves_big_endian_bom(tmp_path, monkeypatch):
    import codecs
    monkeypatch.chdir(tmp_path)
    target = tmp_path / 'be.txt'
    target.write_bytes(codecs.BOM_UTF16_BE + '甲\r\n乙\r\n'.encode('utf-16-be'))
    result = bt.try_execute_builtin_file_tool('apply_patch', {'patch': make_patch('*** Update File: be.txt', '@@', '-甲', '+丙')})
    assert result['success'], result
    assert target.read_bytes() == codecs.BOM_UTF16_BE + '丙\r\n乙\r\n'.encode('utf-16-be')


def test_indented_anchor_and_repetitive_context():
    source = 'class Demo:\n    def call(self):\n        return 1\n'
    changed = context_patch._update(source, ['@@     def call(self):', '-        return 1', '+        return 2'])
    assert changed == source.replace('return 1', 'return 2')
    assert context_patch._positions(['x'] * 50000 + ['y'], ['x'] * 1000 + ['y'], 0) == [49000]


def test_oversized_line_count_rejected_before_allocation():
    with pytest.raises(ValueError, match='200000'):
        context_patch._update('x\n' * 200001, ['@@', '-x', '+y'])
