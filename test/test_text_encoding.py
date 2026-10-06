import base64
import codecs
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from util.text_encoding import decode_text, powershell_text_prelude
from factory import file_factory as ff
from factory.agent_runtime import builtin_tools as bt
from mcp_server import sys_tools_server as web


class TextEncodingTests(unittest.TestCase):
    def test_valid_utf8_is_never_rescored_as_gbk(self):
        text = '涓锛鐨璁 中文路径与工具输出'
        self.assertEqual(decode_text(text.encode(), candidates=('gbk',))[0], text)
        self.assertEqual(bt._decode_command_bytes(text.encode(), candidates=('gbk', 'utf-8')), text)

    def test_document_boms_and_chinese_encodings(self):
        text = '中文内容\n下一行'
        for encoding in ('utf-8-sig', 'utf-16', 'utf-32', 'gb18030'):
            with self.subTest(encoding=encoding):
                data = text.encode(encoding)
                self.assertEqual(ff._parse_txt(data), text)
                self.assertEqual(ff._parse_txt_with_pages(data)[0]['content'], text)
                self.assertEqual(ff.parse_text_file_bytes('unknown.extension', data), text)
        for bom, encoding in ((codecs.BOM_UTF16_BE, 'utf-16-be'), (codecs.BOM_UTF32_BE, 'utf-32-be')):
            self.assertEqual(ff.decode_text_bytes(bom + text.encode(encoding))[0], text)

    def test_csv_legacy_encoding_no_lossy_skip(self):
        if ff.pd is None:
            self.skipTest('pandas unavailable')
        for encoding in ('gb18030', 'utf-16', 'utf-8-sig'):
            result = ff._parse_csv('名称,内容\n工具,中文说明\n'.encode(encoding))
            self.assertIn('工具 中文说明', result)

    def test_doc_converter_output_is_decoded_not_ignored(self):
        result = subprocess.CompletedProcess([], 0, '中文转换结果'.encode('gbk'), b'')
        with patch.object(ff.subprocess, 'run', return_value=result) as run:
            self.assertEqual(ff._parse_doc(b'fake doc'), '中文转换结果')
            self.assertNotIn('errors', run.call_args.kwargs)

    def test_web_charset_conflict_bom_and_gb18030(self):
        for raw, declared in [('<p>中文页面</p>'.encode(), 'latin-1'),
                              ('<p>中文页面</p>'.encode('utf-16'), 'utf-8'),
                              ('<p>中文页面</p>'.encode('gb18030'), 'utf-8')]:
            with patch.object(web, '_http_request', return_value=('https://test.example', 200, 'text/html; charset=' + declared, raw)):
                result = web._fetch_url_impl('https://test.example', 'text', '', '', 8000, 5, '', 'GET', '', None)
                self.assertIn('中文页面', result)

    def test_explicit_encoding_still_wins_and_edit_is_strict(self):
        raw = '中文'.encode('gbk')
        self.assertEqual(decode_text(raw, 'gbk')[0], '中文')
        with self.assertRaises(ValueError):
            bt._decode_bytes_used(raw, 'utf-8')

    def test_bom_edit_uses_unicode_not_gbk(self):
        for encoding in ('utf-8-sig', 'utf-16', 'utf-32'):
            raw = '中文 TARGET'.encode(encoding)
            text, used = bt._decode_for_edit(raw, '')
            self.assertEqual(text, '中文 TARGET')
            self.assertEqual(used, encoding)
            self.assertEqual(text.encode(used), raw)

    @unittest.skipUnless(os.name == 'nt' and shutil.which('powershell'), 'requires Windows PowerShell')
    def test_real_powershell_select_string_utf8_path_and_legacy_override(self):
        with tempfile.TemporaryDirectory(prefix='工具编码_') as folder:
            utf8 = Path(folder, '中文.txt'); utf8.write_bytes('WARN 中文工具说明'.encode())
            legacy = Path(folder, '旧编码.txt'); legacy.write_bytes('WARN 本地旧编码'.encode('mbcs'))
            script = powershell_text_prelude() + (
                "Select-String -LiteralPath '" + str(utf8).replace("'", "''") + "' -Pattern WARN | ForEach-Object { $_.Path + ':' + $_.Line }\n"
                "Select-String -LiteralPath '" + str(legacy).replace("'", "''") + "' -Pattern WARN -Encoding Default | ForEach-Object { $_.Line }"
            )
            encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
            proc = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded], capture_output=True, timeout=20)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            output = proc.stdout.decode('utf-8')
            self.assertIn(str(utf8), output)
            self.assertIn('WARN 中文工具说明', output)
            self.assertIn('WARN 本地旧编码', output)

    def test_relay_powershell_script_contains_encoding_defaults(self):
        with tempfile.TemporaryDirectory() as folder:
            runner = Path(folder, 'run.ps1')
            bt._write_runner_script(runner, 'powershell', 'Write-Output "中文"')
            script = runner.read_text(encoding='utf-8-sig')
            self.assertIn(powershell_text_prelude(), script)
            self.assertIn('Write-Output "中文"', script)
