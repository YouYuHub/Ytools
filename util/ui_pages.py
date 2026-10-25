"""Shared entry-page policy for authentication, bootstrap and asset versioning."""
from dataclasses import dataclass
import json
from pathlib import Path
import re
from types import MappingProxyType

MANIFEST = Path(__file__).resolve().parents[1] / 'H5' / 'pages.json'


@dataclass(frozen=True)
class Page:
    file: str
    authenticated: bool
    defer_scripts: bool = False
    password_change: bool = False

    @property
    def path(self):
        return '/ui/' + self.file


def load_pages(path=MANIFEST):
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(data, dict) or set(data) != {'pages'} or not isinstance(data['pages'], list):
        raise ValueError('Invalid UI page manifest')
    pages, names = [], set()
    for item in data['pages']:
        if (not isinstance(item, dict) or set(item) - {'file', 'authenticated', 'defer_scripts', 'password_change'}
                or not isinstance(item.get('file'), str) or not re.fullmatch(r'[a-z][a-z0-9_-]*\.html', item['file'])
                or item['file'] in names or type(item.get('authenticated')) is not bool
                or any(type(item.get(key, False)) is not bool for key in ('defer_scripts', 'password_change'))
                or (item.get('password_change') and not item['authenticated'])):
            raise ValueError('Invalid or duplicate UI page entry')
        names.add(item['file'])
        pages.append(Page(**item))
    indexed = {page.file: page for page in pages}
    if (not {'index.html', 'login.html', 'account.html'} <= names or indexed['login.html'].authenticated
            or not indexed['index.html'].authenticated or not indexed['account.html'].authenticated
            or not indexed['account.html'].password_change):
        raise ValueError('Required login/account page policy is missing')
    return tuple(pages)


PAGES = load_pages()
PAGE_BY_FILE = MappingProxyType({page.file: page for page in PAGES})
PRIVATE_PAGES = frozenset(page.path for page in PAGES if page.authenticated) | {'/ui/'}
PUBLIC_PAGES = frozenset(page.path for page in PAGES if not page.authenticated)
PASSWORD_CHANGE_PAGES = frozenset(page.path for page in PAGES if page.password_change)
