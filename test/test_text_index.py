from unittest.mock import patch

from factory.agent_runtime import builtin_tools as bt, text_index


def test_large_file_next_page_reuses_index_and_hash(tmp_path):
    path = tmp_path / 'large.txt'
    path.write_text(''.join(f'row {i}\n' for i in range(12000)), encoding='utf-8')
    args = {'path': str(path), 'limit': 3}
    first = bt.execute_read_file(args)
    with patch.object(bt, '_read_bytes_with_limit', side_effect=AssertionError('must reuse snapshot')), \
         patch.object(bt, '_content_hash', side_effect=AssertionError('must reuse hash')):
        page = bt.execute_read_file({**args, 'start_line': 10001})
    assert page['total_lines'] == 12000
    assert 'row 10000' in page['content']
    assert first['content_hash'] == page['content_hash']


def test_changed_file_invalidates_snapshot_and_preserves_newlines(tmp_path):
    path = tmp_path / 'mixed.txt'
    original = ('甲\r\n\r乙\u2028丙\x85\n' * 1200)
    path.write_bytes(original.encode('utf-16'))
    first = bt.execute_read_file({'path': str(path)})
    assert first['total_lines'] == len(original.splitlines())
    with path.open('ab') as handle:
        handle.write('末行'.encode('utf-16-le'))
    second = bt.execute_read_file({'path': str(path), 'start_line': first['total_lines'] + 1})
    assert second['content_hash'] != first['content_hash']
    assert '末行' in second['content']


def test_quota_falls_back_without_changing_read_result(tmp_path):
    path = tmp_path / 'fallback.txt'
    path.write_text('line\n' * 5000, encoding='utf-8')
    with patch.object(text_index, '_MAX_BYTES', 32):
        result = bt.execute_read_file({'path': str(path), 'start_line': 4999})
    assert result['total_lines'] == 5000
    assert result['range_end'] == 5000
