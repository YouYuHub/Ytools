import gzip
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from factory.agent_runtime import builtin_tools as bt
from factory.agent_runtime.file_history_result import record_result
from memory import file_history as store, file_snapshots


@pytest.fixture
def area(tmp_path,monkeypatch):
    monkeypatch.setattr(store,'HISTORY_DIFF_ROOT',tmp_path/'history')
    return tmp_path


def change(area, old, new, **kwargs):
    target = area/'file.txt'
    target.write_bytes(new.encode(kwargs.get('encoding','utf-8')))
    result = store.record_change('large',path=str(target),display_path='file.txt',
        old_text=old,new_text=new,**kwargs)
    return target,result


def test_large_tool_edit_records_compressed_snapshots_and_reverts_exact_bytes(area):
    target = area/'mixed.txt'
    original = ('中文行\r\nsecond\nthird\r'*16000) + 'replace-me\n'
    target.write_bytes(original.encode('utf-8'))
    result = bt.execute_edit_file({'path':str(target),'old_string':'replace-me','new_string':'changed'})
    record_result(result,'large','edit_file',1)
    assert result['history_recorded']
    key = store.file_key(str(target))
    versions = store.versions_of('large',key)
    assert len(versions)==2
    meta = store._load_meta('large',key)
    assert all(v['compression']=='gzip' for v in meta['versions'])
    assert file_snapshots.usage(store._root_dir('large')) < len(original.encode('utf-8'))//10
    view = store.full_view('large',key)
    assert view['snapshot_only'] and view['rows']==[] and view['truncated']
    parts,offset = [],0
    while True:
        page = store.read_content('large',key,offset=offset,limit=100000)
        parts.append(page['content']); offset=page['next_offset']
        if not page['has_more']:break
    assert ''.join(parts)==original.replace('replace-me','changed')
    store.rollback('large',key)
    assert target.read_bytes()==original.encode('utf-8')


def test_created_empty_file_remains_visible_and_reverts_all(area):
    target,result=change(area,'','',old_exists=False,new_exists=True)
    assert store.list_files('large',hide_clean=True)[0]['changed']
    assert store.cleanup_file_histories('large')['removed_count']==0
    store.revert_all('large')
    assert not target.exists()


def test_keep_existing_empty_file_preserves_existence(area):
    target,result=change(area,'text','',old_exists=True,new_exists=True)
    store.keep('large',result['key'])
    meta=store._load_meta('large',result['key'])
    assert meta['versions'][-1]['exists'] is True
    assert not store.list_files('large',hide_clean=True)
    store.rollback('large',result['key'])
    assert target.exists()


def test_version_limit_accounts_for_external_snapshot_before_any_write(area,monkeypatch):
    target,result=change(area,'a','b')
    size=file_snapshots.usage(store._root_dir('large'))
    monkeypatch.setattr(store,'_FILE_HISTORY_MAX_VERSIONS',3)
    _,second=change(area,'external','new')
    assert second['skipped']=='history_version_limit'
    assert len(store.versions_of('large',result['key']))==2
    assert file_snapshots.usage(store._root_dir('large'))==size


def test_large_overwrite_preserves_old_encoding_for_rollback(area):
    target=area/'encoded.txt'
    original=('旧内容\r\n'*50000).encode('utf-16')
    target.write_bytes(original)
    result=bt.execute_write_file({'path':str(target),'content':'new text','mode':'overwrite','encoding':'utf-8'})
    record_result(result,'large','write_file',1)
    assert result['history_recorded']
    store.rollback('large',store.file_key(str(target)))
    assert target.read_bytes()==original


def test_snapshot_quota_rejects_recording_before_writing_any_snapshot(area,monkeypatch):
    monkeypatch.setattr(file_snapshots,'SESSION_BYTES',32)
    _,result=change(area,'a'*300000,'b'*300000)
    assert result['skipped']=='history_quota_exceeded'
    assert not list(store._root_dir('large').glob('*/g*/*'))


def test_rollback_reuses_existing_snapshot_even_when_quota_is_full(area,monkeypatch):
    target,result=change(area,'old\n'*100000,'new\n'*100000)
    key=result['key']
    size=file_snapshots.usage(store._root_dir('large'))
    monkeypatch.setattr(file_snapshots,'SESSION_BYTES',size)
    store.rollback('large',key)
    assert target.read_text()=='old\n'*100000
    assert file_snapshots.usage(store._root_dir('large'))==size


@pytest.mark.parametrize('corruption',['missing','gzip','hash'])
def test_corrupt_snapshot_never_deletes_or_overwrites_current_file(area,corruption):
    target,result=change(area,'old\n'*100000,'new\n'*100000)
    meta=store._load_meta('large',result['key'])
    snapshot=store._version_file(meta,meta['versions'][0])
    if corruption=='missing':snapshot.unlink()
    elif corruption=='gzip':snapshot.write_bytes(b'broken')
    else:snapshot.write_bytes(gzip.compress(b'valid gzip, wrong content'))
    current=target.read_bytes()
    with pytest.raises(ValueError,match='快照'):
        store.rollback('large',result['key'])
    assert target.read_bytes()==current
    assert len(store.versions_of('large',result['key']))==2


def test_disk_edits_are_protected_and_can_be_imported_before_rollback(area):
    target,result=change(area,'old\n'*100000,'new\n'*100000)
    target.write_text('external change',encoding='utf-8')
    with pytest.raises(PermissionError,match='FILE_MODIFIED_EXTERNALLY'):
        store.rollback('large',result['key'])
    assert target.read_text()=='external change'
    assert store.sync_from_disk('large',result['key'])['synced']
    store.rollback('large',result['key'])
    assert target.read_text()=='old\n'*100000


def test_small_crlf_roundtrip_does_not_double_carriage_returns(area):
    target,result=change(area,'a\r\nb\r\n','A\r\nb\r\n',eol='CRLF')
    store.rollback('large',result['key'])
    assert target.read_bytes()==b'a\r\nb\r\n'


def test_existing_empty_file_and_nonexistent_file_have_distinct_baselines(area):
    target,result=change(area,'','new',old_exists=True,new_exists=True)
    store.rollback('large',result['key'])
    assert target.exists() and target.read_bytes()==b''
    target2=area/'created-empty.txt'
    target2.write_text('',encoding='utf-8')
    created=store.record_change('large',path=str(target2),display_path='created-empty.txt',
        old_text='',new_text='',old_exists=False,new_exists=True)
    assert created['recorded']
    store.rollback('large',created['key'])
    assert not target2.exists()


def test_legacy_plain_text_history_remains_readable(area):
    target,result=change(area,'old','new')
    meta=store._load_meta('large',result['key'])
    for version in meta['versions']:
        version.pop('newline_exact',None);version.pop('exists',None)
    store._save_meta(meta)
    assert store.read_content('large',result['key'])['content']=='new'
    store.rollback('large',result['key'])
    assert target.read_text()=='old'


def test_snapshot_size_version_limit_and_paging_parameters_are_bounded(area,monkeypatch):
    target,result=change(area,'old','new')
    monkeypatch.setattr(store,'_FILE_HISTORY_MAX_VERSIONS',2)
    target.write_text('again',encoding='utf-8')
    skipped=store.record_change('large',path=str(target),display_path='file.txt',old_text='new',new_text='again')
    assert skipped['skipped']=='history_version_limit'
    with pytest.raises(ValueError,match='offset/limit'):
        store.read_content('large',result['key'],offset=-1)
    with pytest.raises(ValueError,match='key'):
        store.read_content('large','../outside')


def test_large_hunk_operations_are_rejected_before_alignment(area):
    _,result=change(area,'old\n'*100000,'new\n'*100000)
    with patch('difflib.SequenceMatcher',side_effect=AssertionError('must not align huge files')):
        assert store.full_view('large',result['key'])['snapshot_only']
        with pytest.raises(ValueError,match='完整版本回退'):
            store.hunk_undo('large',result['key'],0)
