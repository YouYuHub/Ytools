"""H5/version_assets.py 自测：用合成目录验证打戳、幂等与 worker 依赖链。

改完 H5/js 或 CSS 后的正规流程是跑该脚本同步 ?v=（见
docs/project_maintenance.md「前端资源版本」），不要手工改哈希。
"""
import hashlib
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from util.ui_pages import Page


def load_stamper():
    spec = importlib.util.spec_from_file_location("version_assets", ROOT / "H5" / "version_assets.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def test_stamp_updates_pages_and_worker_chain_idempotently(tmp_path, monkeypatch):
    (tmp_path / "js").mkdir()
    (tmp_path / "style").mkdir()
    (tmp_path / "js" / "a.js").write_text("console.log(1);\n", encoding="utf-8")
    (tmp_path / "style" / "main.css").write_text(".a{color:red}\n", encoding="utf-8")
    (tmp_path / "js" / "history_parser.js").write_text("/* parser */\n", encoding="utf-8")
    (tmp_path / "js" / "history_parse_worker.js").write_text(
        "importScripts('history_parser.js?v=000000000000');\n", encoding="utf-8")
    (tmp_path / "js" / "history_parse_async.js").write_text(
        "new Worker('js/history_parse_worker.js?v=000000000000');\n", encoding="utf-8")
    (tmp_path / "index.html").write_text(
        "<head>\n"
        '<script src="js/a.js?v=000000000000"></script>\n'
        '<link rel="stylesheet" href="style/main.css?v=000000000000">\n'
        "</head>\n",
        encoding="utf-8")
    stamper = load_stamper()
    monkeypatch.setattr(stamper, "ROOT", tmp_path)
    monkeypatch.setattr(stamper, "PAGES", (Page(file="index.html", authenticated=True, defer_scripts=True),))

    stamper.update_index()
    html = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert f"js/a.js?v={digest(tmp_path / 'js' / 'a.js')}" in html
    assert f"style/main.css?v={digest(tmp_path / 'style' / 'main.css')}" in html
    assert "defer" in html  # defer_scripts 页面补全 defer
    worker = (tmp_path / "js" / "history_parse_worker.js").read_text(encoding="utf-8")
    assert f"history_parser.js?v={digest(tmp_path / 'js' / 'history_parser.js')}" in worker
    async_js = (tmp_path / "js" / "history_parse_async.js").read_text(encoding="utf-8")
    assert f"js/history_parse_worker.js?v={digest(tmp_path / 'js' / 'history_parse_worker.js')}" in async_js

    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    stamper.update_index()  # 幂等：第二次运行零改动
    assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before
