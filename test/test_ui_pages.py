import copy
import json
from pathlib import Path

import pytest

from util.ui_pages import MANIFEST, PAGES, PRIVATE_PAGES, PUBLIC_PAGES, load_pages


def test_manifest_covers_all_entry_pages_and_auth_bootstrap():
    root = MANIFEST.parent
    assert {page.file for page in PAGES} == {page.name for page in root.glob('*.html')}
    assert PUBLIC_PAGES == {'/ui/login.html'}
    for page in PAGES:
        if page.authenticated:
            html = (root / page.file).read_text(encoding='utf-8')
            assert page.path in PRIVATE_PAGES
            assert 'data-ytools-auth-required' in html and 'js/security_client.js' in html


@pytest.mark.parametrize('mutation', ['path', 'duplicate', 'public_index', 'public_account', 'boolean', 'unknown', 'missing'])
def test_invalid_manifest_fails_closed(tmp_path, mutation):
    data = json.loads(MANIFEST.read_text(encoding='utf-8'))
    first = data['pages'][0]
    if mutation == 'path': first['file'] = '../index.html'
    elif mutation == 'duplicate': data['pages'].append(copy.deepcopy(first))
    elif mutation == 'public_index': first['authenticated'] = False
    elif mutation == 'public_account': data['pages'][1]['authenticated'] = False
    elif mutation == 'boolean': first['authenticated'] = 'true'
    elif mutation == 'unknown': first['allow_anything'] = True
    elif mutation == 'missing': data['pages'].pop()
    path = tmp_path / 'pages.json'
    path.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(ValueError): load_pages(path)
