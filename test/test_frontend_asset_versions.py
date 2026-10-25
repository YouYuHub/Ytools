"""页面引用的静态资源版本必须与文件内容一致。"""
import hashlib
import re
from pathlib import Path

import pytest
from util.ui_pages import PAGES


H5_ROOT = Path(__file__).resolve().parents[1] / "H5"


def test_worker_dependencies_use_current_content_hashes():
    for owner, dependency, relative in (
        ('js/history_parse_worker.js', 'js/history_parser.js', 'history_parser.js'),
        ('js/history_parse_async.js', 'js/history_parse_worker.js', 'js/history_parse_worker.js'),
    ):
        digest = hashlib.sha256((H5_ROOT / dependency).read_bytes()).hexdigest()[:12]
        assert relative + '?v=' + digest in (H5_ROOT / owner).read_text(encoding='utf-8')


def test_frontend_asset_hashes_and_script_order():
    html = (H5_ROOT / "index.html").read_text(encoding="utf-8")
    scripts = re.findall(r'<script\b[^>]*\bsrc="(js/[^\"]+)"[^>]*></script>', html)
    assert scripts
    styles = re.findall(r'<link\b[^>]*\bhref="((?:style/|js/)[^\"]+\.css\?v=[^\"]+)"[^>]*>', html)
    assert styles
    for url in scripts + styles:
        path, version = url.split("?v=", 1)
        digest = hashlib.sha256((H5_ROOT / path).read_bytes()).hexdigest()[:12]
        assert version == digest, path
    bottom = html.split("</head>", 1)[1]
    assert all("defer" in tag for tag in re.findall(r'<script\b[^>]*></script>', bottom))
    assert html.index("js/vendor/dompurify/") < html.index("js/markdown.js")
    status_scripts = ["js/app/core.js", "js/task_state.js", "js/app/session_activity.js", "js/app/task_status.js", "js/app/stream_pipeline.js", "js/app/chat.js", "js/app.js"]
    positions = [html.index(path + "?v=") for path in status_scripts]
    assert positions == sorted(positions)
    render_scripts = ['js/app/core.js', 'js/app/message_diff.js', 'js/app/messages.js', 'js/app/stream_pipeline.js']
    positions = [html.index(path + '?v=') for path in render_scripts]
    assert positions == sorted(positions)


@pytest.mark.parametrize('page', [page.file for page in PAGES if page.file != 'index.html'])
def test_secondary_pages_version_all_local_scripts_and_styles(page):
    html = (H5_ROOT / page).read_text(encoding='utf-8')
    urls = re.findall(r'(?:src|href)="((?:js/[^"?]+\.js|(?:style/|js/)[^"?]+\.css|login\.(?:js|css))(?:\?[^\"]*)?)"', html)
    assert urls
    for url in urls:
        path, version = url.split('?v=', 1)
        assert version == hashlib.sha256((H5_ROOT / path).read_bytes()).hexdigest()[:12], path
