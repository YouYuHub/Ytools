"""Archive scope must reduce history reads and document scans, not only hits."""
import json
import sqlite3
from unittest.mock import patch

import pytest

from memory import history_search, session_archives, session_listing
from test_session_archives import history, api, root  # noqa: F401


def mark_archived(root, ids):
    path=root/'sidecars'/'session_archives.json'
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({'version':1,'sessions':{sid:{'archived_at':'2026-10-22T08:00:00+00:00'} for sid in ids}}),encoding='utf-8')


def test_500_sessions_450_archived_only_50_histories_read_for_normal_listing_and_search(root):
    for i in range(500): history(root,'s'+str(i))
    mark_archived(root,['s'+str(i) for i in range(450)])
    with patch.object(session_listing,'_summary',wraps=session_listing._summary) as summary:
        assert len(session_listing.list_session_summaries())==50
        assert summary.call_count==50
    with patch.object(history_search,'_read_file',wraps=history_search._read_file) as read:
        result=history_search.search_history('common',root=root)
        assert result['archive_scope']=='active' and result['session_count']==50
        assert result['checked_files']==result['indexed_files']==read.call_count==50
        assert result['excluded_files']==450
        assert all(int(call.args[0].stem[1:].split('_')[0])>=450 for call in read.call_args_list)


def test_warm_all_index_still_only_scans_selected_session_texts(root):
    for sid in ('active','archived'): history(root,sid,title=sid+' marker')
    mark_archived(root,['archived'])
    assert history_search.search_history('marker',root=root,archive_scope='all')['session_count']==2
    inspected=[]
    connect=sqlite3.connect
    def tracked(*args,**kwargs):
        db=connect(*args,**kwargs)
        def instr(text,needle):
            inspected.append(text)
            return str(text).find(needle)+1
        db.create_function('instr',2,instr)
        return db
    with patch.object(history_search.sqlite3,'connect',tracked):
        result=history_search.search_history('marker',root=root)
    assert result['session_count']==1 and result['indexed_files']==0
    assert 'active marker' in inspected and 'archived marker' not in inspected


def test_changed_archived_history_is_deferred_until_selected_or_unarchived(root):
    history(root,'active'); path=history(root,'archived')
    history_search.search_history('common',root=root,archive_scope='all')
    session_archives.set_archived('archived',True)
    path.write_text(path.read_text(encoding='utf-8').replace('common question','new changed text'),encoding='utf-8')
    with patch.object(history_search,'_read_file',side_effect=AssertionError('must not read archived content')):
        assert history_search.search_history('changed',root=root)['total']==0
    selected=history_search.search_history('changed',root=root,archive_scope='archived')
    assert selected['session_count']==selected['indexed_files']==1
    session_archives.set_archived('archived',False)
    assert history_search.search_history('changed',root=root)['session_count']==1


def test_deletion_cleanup_still_removes_deferred_cached_documents(root):
    history(root,'active'); archived=history(root,'archived')
    history_search.search_history('common',root=root,archive_scope='all')
    session_archives.set_archived('archived',True); archived.unlink()
    history_search.search_history('common',root=root)
    with sqlite3.connect(root/'sidecars'/'history_search.sqlite3') as db:
        assert not db.execute("SELECT 1 FROM sessions WHERE session_id='archived'").fetchone()
        assert not db.execute("SELECT 1 FROM documents WHERE filename='archived_chat.jsonl'").fetchone()


def test_sidebar_snapshot_returns_active_rows_and_lightweight_archive_membership(root,api):
    history(root,'active'); history(root,'archived')
    session_archives.set_archived('archived',True)
    with patch.object(session_listing,'_summary',wraps=session_listing._summary) as summary:
        result=api.get('/chat_history/summaries',params={'with_archive_state':True}).json()
        assert [row['id'] for row in result['sessions']]==['active']
        assert result['archived_ids']==['archived'] and summary.call_count==1
    normal=api.get('/chat_history/search',params={'q':'common'}).json()
    assert normal['archive_scope']=='active' and normal['session_count']==1
    assert api.get('/chat_history/search',params={'q':'common','archive_scope':'archived'}).json()['session_count']==1
    assert api.get('/chat_history/search',params={'q':'common','archive_scope':'all'}).json()['session_count']==2


def test_archive_change_during_indexing_never_serves_incomplete_scope(root):
    history(root,'one')
    read=history_search._read_file
    def changed(path,cancel):
        result=read(path,cancel); session_archives.set_archived('one',True); return result
    with patch.object(history_search,'_read_file',changed),pytest.raises(history_search.SearchSnapshotChanged):
        history_search.search_history('common',root=root)
    assert history_search.search_history('common',root=root)['total']==0
