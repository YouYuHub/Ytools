"""Release gates distinguish focused checks from full, mandatory browser validation."""
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest
from scripts import release_check


def test_release_plan_keeps_required_stages_and_full_backend(tmp_path):
    stages=dict(release_check.plan('release',tmp_path,python='python-fixture',npm='npm-fixture'))
    assert list(stages)==['backend','frontend','browser','storage-benchmark']
    assert 'test' in stages['backend'] and '-k' not in stages['browser']
    assert stages['frontend'][0]=='npm-fixture'
    quick=dict(release_check.plan('quick',tmp_path))
    assert 'test/test_history_pages.py' in quick['backend'] and '-k' in quick['browser']


def test_missing_browser_is_a_failure_not_a_skipped_release(monkeypatch):
    monkeypatch.setattr(release_check.shutil,'which',lambda _:None)
    with pytest.raises(RuntimeError,match='cannot be skipped'):
        release_check.prerequisites()


def test_source_manifest_excludes_real_data_and_detects_code_change(tmp_path):
    (tmp_path/'main.py').write_text('source',encoding='utf8')
    (tmp_path/'history_files').mkdir();(tmp_path/'history_files/secret.json').write_text('private')
    first=release_check.source_digest(tmp_path)
    (tmp_path/'history_files/secret.json').write_text('changed private')
    assert release_check.source_digest(tmp_path)==first
    (tmp_path/'main.py').write_text('changed source',encoding='utf8')
    assert release_check.source_digest(tmp_path)!=first


def test_junit_does_not_treat_skipped_restoration_as_verified(tmp_path):
    path=tmp_path/'result.xml'
    name='test_restored_fixture_account_can_login_and_read_history_without_source'
    path.write_text(f'<testsuites><testsuite><testcase name="{name}"><skipped/></testcase></testsuite></testsuites>')
    result=release_check.test_summary(path)
    assert result['tests']==1 and result['skipped']==1 and not result['backup_restore_verified']
    path.write_text(f'<testsuites><testsuite><testcase name="{name}"/></testsuite></testsuites>')
    assert release_check.test_summary(path)['backup_restore_verified']


def test_deployment_check_verifies_anonymous_boundaries_and_all_assets():
    import json
    from scripts import verify_deployment as deploy
    def fetch(origin,path,method='GET'):
        if path=='/auth/status':return 200,json.dumps(dict.fromkeys(['initialized','account_panel','account_sharing','history_paging'],True)).encode(),{}
        if path.startswith('/ui/js/') or path.startswith('/ui/style/') or path.startswith('/ui/login.'):
            relative=path.split('?',1)[0].removeprefix('/ui/')
            return 200,(deploy.ROOT/'H5'/relative).read_bytes(),{}
        if path.startswith('/ui/'):return 303,b'',{'Location':'/ui/login.html'}
        return 401,b'',{'Cache-Control':'private, no-store'}
    result=deploy.verify('http://127.0.0.1:12345',fetch=fetch)
    assert result['passed'] and result['assets_checked']>100
    def leaked(origin,path,method='GET'):
        if path.startswith('/chat_history/page'):return 200,b'private data',{}
        return fetch(origin,path,method)
    with pytest.raises(ValueError,match='boundary'):
        deploy.verify('http://127.0.0.1:12345',fetch=leaked)
    with pytest.raises(ValueError,match='HTTPS'):
        deploy.validate_origin('http://remote.example.test')
