"""按 pages.json 更新入口页资源版本，保留各页脚本执行策略。

修改 H5/js 或 CSS 后执行 ``python H5/version_assets.py``；脚本可重复运行。
"""
from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
INDEX = ROOT / "index.html"
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))
from util.ui_pages import PAGES


def versioned_url(url: str) -> str:
    relative = url.split("?", 1)[0]
    asset = ROOT / relative
    if not asset.is_file():
        raise FileNotFoundError(asset)
    digest = hashlib.sha256(asset.read_bytes()).hexdigest()[:12]
    return f"{relative}?v={digest}"


def update_index() -> None:
    # Version the worker's parser first, then the worker URL, then entry pages.
    # This also prevents stale nested resources after a cached-page upgrade.
    for owner, dependency, relative in (
        ('js/history_parse_worker.js', 'js/history_parser.js', 'history_parser.js'),
        ('js/history_parse_async.js', 'js/history_parse_worker.js', 'js/history_parse_worker.js'),
    ):
        target = ROOT / owner
        source = target.read_text(encoding='utf-8')
        digest = hashlib.sha256((ROOT / dependency).read_bytes()).hexdigest()[:12]
        updated, count = re.subn(r'(["\'])' + re.escape(relative) + r'(?:\?v=[^"\']*)?\1',
                                 lambda match: match.group(1) + relative + '?v=' + digest + match.group(1), source)
        if count != 1:
            raise ValueError('Missing/ambiguous runtime dependency: ' + owner)
        if updated != source:
            target.write_text(updated, encoding='utf-8')
    for page in PAGES:
        def replace_script(match: re.Match[str]) -> str:
            tag, url = match.group(0), match.group(1)
            tag = tag.replace(url, versioned_url(url), 1)
            if (page.defer_scripts and url.split('?', 1)[0] not in {'js/theme.js', 'js/security_client.js', 'js/account_preferences.js', 'js/account_panel_client.js'}
                    and not re.search(r'\bdefer\b', tag)):
                tag = tag.replace('<script ', '<script defer ', 1)
            return tag
        target = ROOT / page.file
        html = target.read_text(encoding='utf-8')
        html = re.sub(r'<script\b[^>]*\bsrc="((?:js/[^\"]+|login\.js(?:\?[^\"]*)?))"[^>]*></script>', replace_script, html)
        html = re.sub(r'<link\b[^>]*\bhref="((?:(?:style/|js/)[^\"]+\.css|login\.css)(?:\?[^\"]*)?)"[^>]*>',
                      lambda match: match.group(0).replace(match.group(1), versioned_url(match.group(1)), 1), html)
        target.write_text(html, encoding='utf-8')


if __name__ == "__main__":
    update_index()
