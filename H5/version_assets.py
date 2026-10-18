"""按静态文件内容更新 index.html 的版本参数，并并行加载底部脚本。

修改 H5/js 或 CSS 后执行 ``python H5/version_assets.py``；脚本可重复运行。
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent
INDEX = ROOT / "index.html"


def versioned_url(url: str) -> str:
    relative = url.split("?", 1)[0]
    asset = ROOT / relative
    if not asset.is_file():
        raise FileNotFoundError(asset)
    digest = hashlib.sha256(asset.read_bytes()).hexdigest()[:12]
    return f"{relative}?v={digest}"


def update_index() -> None:
    html = INDEX.read_text(encoding="utf-8")

    def replace_script(match: re.Match[str]) -> str:
        tag = match.group(0)
        url = match.group(1)
        tag = tag.replace(url, versioned_url(url), 1)
        if url.split("?", 1)[0] != "js/theme.js" and not re.search(r"\bdefer\b", tag):
            tag = tag.replace("<script ", "<script defer ", 1)
        return tag

    html = re.sub(r'<script\b[^>]*\bsrc="(js/[^\"]+)"[^>]*></script>', replace_script, html)

    def replace_css(match: re.Match[str]) -> str:
        return match.group(0).replace(match.group(1), versioned_url(match.group(1)), 1)

    html = re.sub(r'<link\b[^>]*\bhref="((?:style/|js/)[^\"]+\.css(?:\?[^\"]*)?)"[^>]*>', replace_css, html)
    INDEX.write_text(html, encoding="utf-8")


if __name__ == "__main__":
    update_index()
