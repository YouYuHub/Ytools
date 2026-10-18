"""页面引用的静态资源版本必须与文件内容一致。"""
import hashlib
import re
from pathlib import Path


H5_ROOT = Path(__file__).resolve().parents[1] / "H5"


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
