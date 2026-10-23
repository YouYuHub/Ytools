from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from factory.agent_runtime import builtin_tools as bt, query_pages
from factory.agent_runtime.tool_validation import argument_error


def collect(args):
    pages = [bt.execute_search_files(args)]
    for _ in range(200):
        if not pages[-1]['has_more']:
            return pages
        pages.append(bt.execute_search_files({**args, 'cursor': pages[-1]['next_cursor']}))
    pytest.fail('search did not finish')


def test_first_page_only_reads_the_first_matching_file(tmp_path):
    for i in range(100):
        (tmp_path / f'{i:03}.txt').write_text(f'hit {i}\n', encoding='utf-8')
    args = dict(dir_path=str(tmp_path), pattern='hit', file_pattern='*.txt', max_results=1)
    with patch.object(bt, '_read_bytes_with_limit', wraps=bt._read_bytes_with_limit) as read:
        first = bt.execute_search_files(args)
        assert read.call_count == 1
        assert first['scan_pending'] and first['has_more']
        assert not first['total_matches_exact'] and not first['scan_complete']
        assert first['total_matches'] == 1
        second = bt.execute_search_files({**args, 'cursor': first['next_cursor']})
        assert read.call_count == 2
        assert 'hit 1' in second['message']
    with patch.object(bt, '_read_bytes_with_limit', wraps=bt._read_bytes_with_limit) as read:
        complete = bt.execute_search_files({**args,'count_total':True})
        assert read.call_count == 100 and complete['total_matches'] == 100


def test_all_pages_preserve_matches_and_final_exact_count(tmp_path):
    for i in range(7):
        (tmp_path / f'{i}.txt').write_text(f'hit {i}\n', encoding='utf-8')
    pages = collect(dict(dir_path=str(tmp_path), pattern='hit', max_results=2))
    body = '\n'.join(page['message'] for page in pages)
    for i in range(7):
        assert body.count(f'| hit {i}') == 1
    assert pages[-1]['scan_complete'] and pages[-1]['total_matches_exact']
    assert pages[-1]['total_matches'] == 7
    assert not pages[-1]['scan_pending']


def test_dense_file_is_scanned_in_bounded_windows(tmp_path):
    (tmp_path / 'big.txt').write_text('hit\n' * 5000, encoding='utf-8')
    args = dict(dir_path=str(tmp_path), pattern='hit', max_results=1)
    first = bt.execute_search_files(args)
    assert first['total_matches'] == 200
    pages = collect({**args, 'cursor': first['next_cursor']})
    assert pages[-1]['total_matches'] == 5000
    assert pages[-1]['scan_complete']


def test_context_across_windows_keeps_original_line_numbers(tmp_path):
    lines = ['context'] * 410
    for i in (0,198,199,200,201,399,400,409):
        lines[i] = f'hit-{i+1}'
    (tmp_path / 'lines.txt').write_text('\n'.join(lines), encoding='utf-8')
    pages = collect(dict(dir_path=str(tmp_path), pattern='hit-', context_lines=2,max_results=1))
    body = '\n'.join(page['message'] for page in pages)
    for i in (0,198,199,200,201,399,400,409):
        assert body.count(f'>{i+1}| hit-{i+1}') == 1
    assert pages[-1]['total_matches'] == 8


def test_repeating_a_cursor_is_idempotent_and_thread_safe(tmp_path):
    for i in range(8):
        (tmp_path / f'{i}.txt').write_text('hit', encoding='utf-8')
    args = dict(dir_path=str(tmp_path),pattern='hit',max_results=1)
    first = bt.execute_search_files(args)
    query = {**args,'cursor':first['next_cursor']}
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: bt.execute_search_files(query),range(8)))
    assert all(result == results[0] for result in results)


def test_ignore_rules_nested_negation_and_directory_pruning(tmp_path):
    (tmp_path / 'src').mkdir()
    (tmp_path / 'ignored').mkdir()
    (tmp_path / '.gitignore').write_text('*.log\n!keep.log\nignored/\n!ignored/keep.txt\n\\#literal\n',encoding='utf-8')
    (tmp_path / 'src' / '.gitignore').write_text('!inside.log\n',encoding='utf-8')
    for name in ('drop.log','keep.log','src/drop.log','src/inside.log','ignored/keep.txt','#literal','normal.txt'):
        (tmp_path / name).write_text('needle',encoding='utf-8')
    args = dict(dir_path=str(tmp_path),respect_gitignore=True,exclude_dirs=['permissions'])
    paths = bt.execute_list_files(args)['files']
    assert {name for name in ('keep.log','inside.log','normal.txt') if any(path.endswith(name) for path in paths)} == {'keep.log','inside.log','normal.txt'}
    assert len(paths) == 3
    pages = collect({**args,'pattern':'needle','max_results':1})
    assert pages[-1]['total_matches'] == 3
    assert pages[-1]['scan_complete']
    assert bt.execute_search_files({**args,'pattern':'needle','count_total':True})['total_matches'] == 3


def test_ignore_file_changes_invalidate_continuation(tmp_path):
    ignore = tmp_path / '.gitignore'
    ignore.write_text('*.log',encoding='utf-8')
    for i in range(2):
        (tmp_path / f'{i}.txt').write_text('hit',encoding='utf-8')
    args = dict(dir_path=str(tmp_path),pattern='hit',max_results=1,respect_gitignore=True)
    first = bt.execute_search_files(args)
    ignore.write_text('*.txt',encoding='utf-8')
    with pytest.raises(ValueError,match='FILE_QUERY_CHANGED'):
        bt.execute_search_files({**args,'cursor':first['next_cursor']})


def test_ignore_same_stat_content_change_still_invalidates_cursor(tmp_path,monkeypatch):
    ignore=tmp_path/'.gitignore'
    ignore.write_text('*.log',encoding='utf-8')
    for i in range(2):
        (tmp_path/f'{i}.txt').write_text('hit',encoding='utf-8')
    original=bt._file_version
    fixed=original(ignore)
    monkeypatch.setattr(bt,'_file_version',lambda p:fixed if p==ignore else original(p))
    args=dict(dir_path=str(tmp_path),pattern='hit',max_results=1,respect_gitignore=True,no_default_excludes=True,
              exclude_dirs=['permissions'])
    first=bt.execute_search_files(args)
    ignore.write_text('*.txt',encoding='utf-8')
    with pytest.raises(ValueError,match='FILE_QUERY_CHANGED'):
        bt.execute_search_files({**args,'cursor':first['next_cursor']})


def test_ignore_rule_budget_fails_closed_for_that_directory(tmp_path):
    (tmp_path / '.gitignore').write_text('#' * 65537,encoding='utf-8')
    (tmp_path / 'secret.txt').write_text('hit',encoding='utf-8')
    result = bt.execute_search_files(dict(dir_path=str(tmp_path),pattern='hit',respect_gitignore=True))
    assert result['total_matches'] == 0 and not result['scan_complete']
    assert result['skipped_unreadable'] == 1


def test_total_scan_budget_and_candidate_limit_remain_explicit(tmp_path):
    for i in range(5):
        (tmp_path / f'{i}.txt').write_text('hit',encoding='utf-8')
    pages = collect(dict(dir_path=str(tmp_path),pattern='hit',max_results=1,max_scanned_files=2))
    assert pages[-1]['total_matches'] == 2
    assert pages[-1]['scan_truncated'] and not pages[-1]['scan_complete']


@pytest.mark.parametrize('field',['count_total','respect_gitignore'])
def test_new_flags_are_strict_booleans(field):
    assert argument_error('search_files',{'pattern':'x',field:True}) is None
    assert argument_error('search_files',{'pattern':'x',field:'true'})
