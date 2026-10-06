"""网页工具正确性回归；响应固定，不依赖搜索引擎实时内容。"""
import io
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from email.message import Message
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from mcp_server import sys_tools_server as s


def fetch(raw, mode='text', **kwargs):
    with patch.object(s, '_http_request', return_value=('https://test.example/', 200, 'text/html; charset=utf-8', raw.encode())):
        return s._fetch_url_impl('https://test.example/', mode, kwargs.get('pattern', ''), kwargs.get('tag', ''), kwargs.get('max_chars', 8000), 5, '', 'GET', '', None)


class FetchTests(unittest.TestCase):
    def test_real_nested_element_not_script_string(self):
        html = '<script>"<div id=main>假内容</div>"</script><div id=main class="one two">前<div>嵌套</div>后<script>假正文</script><style>假样式</style></div>'
        result = fetch(html, 'tag', tag='div#main.two')
        self.assertIn('嵌套', result)
        self.assertIn('后', result)
        self.assertNotIn('假内容', result)
        self.assertNotIn('假正文', result)
        self.assertNotIn('假样式', result)

    def test_attribute_case_quotes_and_multiple_elements(self):
        blocks = s._extract_tag_blocks("<DIV CLASS='a b'>一</DIV><div class=b>二</div>", 'div.b')
        self.assertEqual(blocks, ['一', '二'])

    def test_missing_closing_tag_keeps_remaining_body(self):
        self.assertEqual(s._extract_tag_blocks('<div id=main>完整正文', 'div#main'), ['完整正文'])

    def test_empty_head_is_not_selector_failure(self):
        result = fetch('<html><head></head><body><h1>正文</h1></body></html>', 'tag', tag='head')
        self.assertIn('内容为空', result)
        self.assertNotIn('正文\n', result)

    def test_regex_intentionally_reads_original_script(self):
        result = fetch('<script>const value=42;</script>', 'regex', pattern=r'value=(\d+)')
        self.assertTrue(result.endswith('42'))

    def test_regex_truncation_is_explicit(self):
        self.assertIn('正则结果已截断', fetch('x' * 3000, 'regex', pattern='(.+)', max_chars=800))

    def test_used_and_declared_encoding_are_reported(self):
        self.assertIn('实际编码: utf-8', fetch('<p>中文正文</p>'))
        self.assertIn('声明/推断编码: utf-8', fetch('<p>中文正文</p>'))

    def test_http_error_retains_status_headers_and_body(self):
        headers = Message(); headers['Content-Type'] = 'text/html; charset=utf-8'; headers['Retry-After'] = '30'
        error = HTTPError('https://test.example/', 429, 'Too Many Requests', headers, io.BytesIO('<p>请稍后</p>'.encode()))
        with patch.object(s, 'urlopen', side_effect=error):
            result = s._fetch_url_impl('https://test.example/', 'text', '', '', 8000, 5, '', 'GET', '', None)
        self.assertIn('HTTP 429', result); self.assertIn('Retry-After: 30', result); self.assertIn('请稍后', result)

    def test_http_error_body_remains_bounded(self):
        error = HTTPError('https://test.example/', 404, 'Not Found', {}, io.BytesIO(b'x' * 65))
        with patch.object(s, '_HTTP_MAX_TRANSFER_BYTES', 64), patch.object(s, 'urlopen', side_effect=error):
            with self.assertRaisesRegex(ValueError, '下载上限'):
                s._http_request('https://test.example/')


    def test_real_local_http_request_and_error_response(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                status = 429 if self.path == '/error' else 200
                body = '<div id=main>开始<div>内部</div>完整结尾</div>'.encode()
                self.send_response(status)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                if status == 429:
                    self.send_header('Retry-After', '60')
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f'http://127.0.0.1:{server.server_port}'
            text = s._fetch_url_impl(url, 'tag', '', 'div#main', 8000, 5, '', 'GET', '', None)
            self.assertIn('完整结尾', text)
            error = s._fetch_url_impl(url+'/error', 'text', '', '', 8000, 5, '', 'GET', '', None)
            self.assertIn('HTTP 429', error)
            self.assertIn('Retry-After: 60', error)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class SearchTests(unittest.TestCase):
    def test_ddg_attribute_order_classes_and_redirect_decoding(self):
        html = '<a href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2F%3Fq%3Da%2526b&amp;rut=x" class="other result__a"><b>Python</b> 文档</a><a href="https://example.org" class="result__snippet extra">Python 示例</a>'
        with patch.object(s, '_http_request', return_value=('', 200, 'text/html', html.encode())):
            items = s._fetch_ddg_results('python', 20)
        self.assertEqual(items, [('Python 文档', 'https://example.org/?q=a%26b', 'Python 示例')])

    def test_challenge_is_failure_not_zero_results(self):
        with patch.object(s, '_http_request', return_value=('', 202, 'text/html', b'<h1>Verify you are human</h1>')):
            with self.assertRaisesRegex(ValueError, '验证码'):
                s._fetch_ddg_results('python', 20)

    def test_recognized_no_results_is_empty(self):
        with patch.object(s, '_http_request', return_value=('', 200, 'text/html', b'<div class="no-results">No results</div>')):
            self.assertEqual(s._fetch_ddg_results('nonsense', 20), [])

    def test_query_sent_without_rewriting(self):
        seen = []
        def response(url, **kwargs):
            seen.append(url); return ('', 200, 'text/html', b'<div class="no-results">No results</div>')
        with patch.object(s, '_http_request', side_effect=response):
            s._fetch_bing_results('beautifulsoup4 site:github.com', 20)
            s._fetch_ddg_results('beautifulsoup4 site:github.com', 20)
        self.assertTrue(all(parse_qs(urlsplit(url).query)['q'] == ['beautifulsoup4 site:github.com'] for url in seen))

    def test_site_constraint_excludes_wrong_host(self):
        pool = [('Python', 'https://github.com/python/a', ''), ('Python', 'https://github.com.evil.test/python', ''), ('Python', 'https://other.test', '')]
        with patch.object(s, '_fetch_bing_results', return_value=pool):
            result = s._web_search_impl('python site:github.com', 8, 'bing')
        self.assertIn('https://github.com/python/a', result)
        self.assertNotIn('evil.test', result); self.assertNotIn('other.test', result)

    def test_unrelated_english_pool_triggers_fallback_and_is_discarded(self):
        pool = [('Falkland Islands', 'https://example.org/islands', '旅游')]
        with patch.object(s, '_fetch_bing_results', return_value=pool), patch.object(s, '_fetch_ddg_results', side_effect=ValueError('验证码')) as ddg:
            result = s._web_search_impl('beautifulsoup4', 8)
        ddg.assert_called_once(); self.assertIn('未获得可靠结果', result); self.assertNotIn('Falkland Islands', result)

    def test_max_results_only_changes_display_count(self):
        pool = [('Python '+str(i), 'https://python.org/'+str(i), '') for i in range(20)]
        with patch.object(s, '_fetch_bing_results', return_value=pool) as bing, patch.object(s, '_fetch_ddg_results') as ddg:
            small = s._web_search_impl('python', 3)
            large = s._web_search_impl('python', 20)
        self.assertEqual(bing.call_args_list[0], bing.call_args_list[1]); ddg.assert_not_called()
        self.assertIn('共 3 条', small); self.assertIn('共 20 条', large)

    def test_explicit_ddg_error_does_not_claim_bing_failed(self):
        with patch.object(s, '_fetch_ddg_results', side_effect=ValueError('验证码')):
            with self.assertRaisesRegex(ValueError, 'DuckDuckGo 未获得搜索结果'):
                s._web_search_impl('python', 8, 'ddg')


if __name__ == '__main__':
    unittest.main()
