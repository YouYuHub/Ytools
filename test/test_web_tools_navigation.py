"""web_open / web_click / web_find 回归；响应固定，不依赖外网。"""
import unittest
from unittest.mock import patch
from mcp_server import web_tools_server as s

try:
    import fitz
except Exception:
    try:
        import pymupdf as fitz
    except Exception:
        fitz = None


PAGE = """<html><head><title>测试页面</title></head><body>
<nav><a href="/nav">导航链接</a></nav>
<main>
<h1>主标题</h1>
<p>第一段正文，包含关键词苹果。</p>
<p>第二段正文，包含关键词香蕉。</p>
<p><a href="/docs/guide">使用指南</a> 与 <a href="https://other.example/page">外部页面</a></p>
<p><a href="#section1">本页锚点</a></p>
<p><a href="mailto:hi@example.com">邮件联系</a></p>
<p><a href="https://user:pass@example.com/secret">带凭据链接</a></p>
</main>
</body></html>"""

TARGET = """<html><head><title>指南页</title></head><body>
<main><p>指南正文内容。</p><p><a href="/docs/next">下一页</a></p></main>
</body></html>"""


def _response(html, url="https://test.example/article"):
    return (url, 200, "text/html; charset=utf-8", html.encode())


class WebOpenTests(unittest.TestCase):
    def test_open_returns_title_text_and_numbered_links(self):
        with patch.object(s, "_http_request", return_value=_response(PAGE)):
            result = s._web_open_impl("https://test.example/article")
        self.assertIn("标题: 测试页面", result)
        self.assertIn("主标题", result)
        self.assertIn("第一段正文", result)
        self.assertIn("可跟进链接", result)
        self.assertIn("1. ", result)
        self.assertIn("https://test.example/docs/guide", result)
        self.assertIn("https://other.example/page", result)

    def test_open_skips_unsafe_and_self_links(self):
        with patch.object(s, "_http_request", return_value=_response(PAGE)):
            result = s._web_open_impl("https://test.example/article")
        self.assertNotIn("mailto:", result)
        self.assertNotIn("user:pass@", result)
        self.assertNotIn("#section1", result)

    def test_open_dedupes_links_and_respects_max_links(self):
        html = "<p>" + "".join(f'<a href="/same">重复{i}</a>' for i in range(5)) + "</p>"
        with patch.object(s, "_http_request", return_value=_response(html)):
            result = s._web_open_impl("https://test.example/", max_links=50)
        self.assertEqual(result.count("https://test.example/same"), 1)
        with patch.object(s, "_http_request", return_value=_response(PAGE)):
            result = s._web_open_impl("https://test.example/article", max_links=1)
        self.assertIn("  1. ", result)
        self.assertNotIn("  2. ", result)

    def test_open_pagination_uses_char_offsets(self):
        html = "<main><p>" + "字" * 600 + "</p></main>"
        with patch.object(s, "_http_request", return_value=_response(html)):
            first = s._web_open_impl("https://test.example/", max_chars=200)
            second = s._web_open_impl("https://test.example/", max_chars=200, offset=200)
        self.assertIn("offset=200", first)
        self.assertIn("本次 [200, 400)", second)

    def test_open_rejects_bad_arguments(self):
        with self.assertRaises(ValueError):
            s._web_open_impl("ftp://example.com/file")
        with self.assertRaises(ValueError):
            s._web_open_impl("https://test.example/", offset=-1)


class WebFindTests(unittest.TestCase):
    def test_find_reports_matches_with_offsets_and_context(self):
        with patch.object(s, "_http_request", return_value=_response(PAGE)):
            result = s._web_find_impl("https://test.example/article", "香蕉")
        self.assertIn("1 处匹配", result)
        self.assertIn("第二段正文", result)
        self.assertIn("offset=", result)
        # 偏移可用 web_open 跳转：取出第一处 offset 并验证落在正文内。
        offset = int(result.split("offset=")[1].split("）")[0].split(")")[0])
        with patch.object(s, "_http_request", return_value=_response(PAGE)):
            opened = s._web_open_impl("https://test.example/article", offset=offset)
        self.assertIn("香蕉", opened)

    def test_find_is_case_insensitive_and_reports_miss(self):
        html = "<main><p>Hello World</p></main>"
        with patch.object(s, "_http_request", return_value=_response(html)):
            self.assertIn("1 处匹配", s._web_find_impl("https://test.example/", "hello"))
            missed = s._web_find_impl("https://test.example/", "不存在的词")
        self.assertIn("未找到字面文本", missed)
        self.assertIn("不代表", missed)

    def test_find_rejects_empty_query(self):
        with self.assertRaises(ValueError):
            s._web_find_impl("https://test.example/", "   ")


class WebClickTests(unittest.TestCase):
    def test_click_follows_numbered_link(self):
        pages = {"https://test.example/article": PAGE, "https://test.example/docs/guide": TARGET}
        calls = []

        def routing_request(url, **kwargs):
            calls.append(url)
            key = url if url in pages else "https://test.example/article"
            return _response(pages[key], key)

        with patch.object(s, "_http_request", side_effect=routing_request):
            result = s._web_click_impl("https://test.example/article", 2, max_links=0)
        self.assertIn("跟进第 2 个链接", result)
        self.assertIn("指南正文内容", result)
        self.assertEqual(len(calls), 2)

    def test_click_rejects_unknown_number(self):
        with patch.object(s, "_http_request", return_value=_response(PAGE)):
            with self.assertRaises(ValueError) as ctx:
                s._web_click_impl("https://test.example/article", 50)
        self.assertIn("不存在", str(ctx.exception))
        with self.assertRaises(ValueError):
            with patch.object(s, "_http_request", return_value=_response(PAGE)):
                s._web_click_impl("https://test.example/article", 0)

    def test_click_survives_tiny_remaining_budget(self):
        # 来源页几乎耗尽总预算时，嵌套打开仍应通过参数校验（钳制到 1 秒），
        # 实际超时由外层 deadline 决定，不应报“timeout_seconds 必须为 …”。
        with patch.object(s, "_http_request", return_value=_response(PAGE)):
            with patch.object(s, "remaining_seconds", return_value=0.001):
                result = s._web_click_impl("https://test.example/article", 1, max_links=0)
        self.assertIn("跟进第 1 个链接", result)


def _make_pdf():
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Hello PDF page one")
    page.insert_link({"kind": fitz.LINK_URI, "from": fitz.Rect(72, 72, 220, 100),
                      "uri": "https://example.com/target"})
    second = doc.new_page()
    second.insert_text((72, 72), "Second page content here")
    doc.set_metadata({"title": "Test Doc"})
    try:
        return doc.tobytes()
    finally:
        doc.close()


@unittest.skipUnless(fitz, "PyMuPDF 未安装时跳过 PDF 用例")
class PdfTests(unittest.TestCase):
    def test_open_pdf_extracts_pages_and_links(self):
        raw = _make_pdf()
        with patch.object(s, "_http_request",
                          return_value=("https://test.example/doc.pdf", 200, "application/pdf", raw)):
            result = s._web_open_impl("https://test.example/doc.pdf")
        self.assertIn("PDF逐页文本", result)
        self.assertIn("第 1 页", result)
        self.assertIn("Hello PDF page one", result)
        self.assertIn("第 2 页", result)
        self.assertIn("Second page content", result)
        self.assertIn("标题: Test Doc", result)
        self.assertIn("https://example.com/target", result)

    def test_fetch_pdf_text_mode_and_reject_other_modes(self):
        raw = _make_pdf()
        with patch.object(s, "_http_request",
                          return_value=("https://test.example/doc.pdf", 200, "application/pdf", raw)):
            result = s._fetch_url_impl("https://test.example/doc.pdf", "text", "", "", 8000, 5, "", "GET", "", None)
        self.assertIn("PDF逐页文本", result)
        self.assertIn("Hello PDF page one", result)
        with patch.object(s, "_http_request",
                          return_value=("https://test.example/doc.pdf", 200, "application/pdf", raw)):
            with self.assertRaisesRegex(ValueError, "仅支持 mode=text"):
                s._fetch_url_impl("https://test.example/doc.pdf", "tag", "", "div", 8000, 5, "", "GET", "", None)

    def test_scanned_pdf_reports_no_text(self):
        doc = fitz.open()
        doc.new_page()
        try:
            raw = doc.tobytes()
        finally:
            doc.close()
        with patch.object(s, "_http_request",
                          return_value=("https://test.example/scan.pdf", 200, "application/pdf", raw)):
            result = s._web_open_impl("https://test.example/scan.pdf")
        self.assertIn("扫描版", result)


class JsHandoffTests(unittest.TestCase):
    def test_detect_js_app_markers(self):
        self.assertTrue(s._detect_js_app('<div id="react-root"></div>'))
        self.assertTrue(s._detect_js_app('<script>window.__NEXT_DATA__={}</script>'))
        self.assertEqual(s._detect_js_app("<main><p>普通正文</p></main>"), "")

    def test_empty_text_with_js_marker_points_to_browser_tools(self):
        html = ('<html><body><div id="react-root"></div>'
                '<script src="/_next/static/app.js"></script></body></html>')
        with patch.object(s, "_http_request", return_value=_response(html)):
            result = s._web_open_impl("https://test.example/app")
        self.assertIn("browser_navigate", result)
        self.assertIn("browser_snapshot", result)
        self.assertIn("react-root", result)


class SearchFallbackTests(unittest.TestCase):
    def test_bing_redesign_falls_back_to_links(self):
        html = ('<html><body><div class="new-layout">'
                '<a href="https://docs.python.org/3.13/">Python 3.13 文档</a>'
                '<a href="https://www.bing.com/search?q=next">下一页</a>'
                '</div></body></html>')
        with patch.object(s, "_http_request", return_value=("", 200, "text/html", html.encode())):
            items, fallback = s._fetch_bing_results("Python 3.13", 20)
        self.assertTrue(fallback)
        self.assertTrue(any("docs.python.org" in item[1] for item in items))
        self.assertFalse(any("bing.com" in item[1] for item in items))

    def test_bing_fallback_empty_without_keyword_hits(self):
        html = ('<html><body><a href="https://unrelated.example/x">完全无关</a></body></html>')
        with patch.object(s, "_http_request", return_value=("", 200, "text/html", html.encode())):
            items, fallback = s._fetch_bing_results("Python 3.13", 20)
        self.assertEqual((items, fallback), ([], False))

    def test_ddg_redesign_falls_back_before_giving_up(self):
        html = ('<html><body><a href="https://docs.python.org/3.13/">Python 3.13 文档</a></body></html>')
        with patch.object(s, "_http_request", return_value=("", 200, "text/html", html.encode())):
            items, fallback = s._fetch_ddg_results("Python 3.13", 20)
        self.assertTrue(fallback)
        self.assertTrue(any("docs.python.org" in item[1] for item in items))

    def test_search_output_marks_fallback_engine(self):
        pool = [("Python 3.13", "https://docs.python.org/3.13/", "docs")]
        with patch.object(s, "_fetch_bing_results", return_value=(pool, True)), \
                patch.object(s, "_fetch_ddg_results", return_value=([], False)):
            result = s._web_search_impl("Python 3.13", 5)
        self.assertIn("非结构化兜底", result)
        self.assertIn("docs.python.org", result)


if __name__ == "__main__":
    unittest.main()
