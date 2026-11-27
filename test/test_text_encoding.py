import base64
import codecs
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from util.text_encoding import (decode_text, powershell_text_prelude,
                                cmd_script_bytes, wscript_text_bytes, order_decode_candidates)
from factory import file_factory as ff
from factory.agent_runtime import builtin_tools as bt
from mcp_server import web_tools_server as web


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
        source = '名称,内容\n工具,中文说明\n'
        for encoding in ('gb18030', 'utf-16', 'utf-8-sig'):
            self.assertEqual(ff._parse_csv(source.encode(encoding)), source)

    def test_csv_preserves_headers_quoted_fields_and_headerless_first_row(self):
        # file_factory 已移除 pandas 依赖，CSV 解析恒走无损文本路径（无需再 patch ff.pd）。
        for source in ('name,score,comment\r\nAlice,95,"优秀,稳定"\r\nBob,,"两行\n评价"\r\n',
                       'Alice,95\nBob,87\n', ''):
            self.assertEqual(ff._parse_csv(source.encode()), source)
            self.assertEqual(ff._parse_csv_with_pages(source.encode())[0]['content'], source)

    def test_local_csv_document_pagination_includes_header(self):
        source = 'name,score,comment\nAlice,95,优秀\nBob,87,良好\n'
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'data.csv'
            path.write_bytes(source.encode())
            with patch.object(bt.file_memory, 'find_file_memory_record', return_value=None):
                first = bt.execute_read_document({'filename': str(path), 'mode': 'text', 'limit': 18}, 'csv-test')
                remaining = bt.execute_read_document({'filename': str(path), 'mode': 'text', 'offset': first['next_offset']}, 'csv-test')
        self.assertEqual(first['content'] + remaining['content'], source)

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


class NonChineseCodepageTests(unittest.TestCase):
    """英文代码页服务器（cp1252）视角回归。

    根因两类：① 写启动器/runner 时 Unicode→ANSI 严格编码，环境块/命令含中文
    即抛 UnicodeEncodeError（所有模式全失败）；② 自动解码时单字节西文页排在
    CJK 之前，GBK 字节被"成功"解成西文乱码（文档/终端输出静默损坏）。
    """

    def test_cmd_script_bytes_ansi_first_then_utf8_chcp(self):
        ascii_text = '@echo off\r\necho hi\r\n'
        self.assertEqual(cmd_script_bytes(ascii_text, ansi='cp1252'), ascii_text.encode('cp1252'))
        zh = '@echo off\r\nset "PATH=D:\\软件;C:\\x"\r\necho 中文\r\n'
        out = cmd_script_bytes(zh, ansi='cp1252')
        self.assertTrue(out.startswith(b'@chcp 65001 >nul\r\n'))
        self.assertIn('D:\\软件'.encode('utf-8'), out)
        self.assertIn('echo 中文'.encode('utf-8'), out)
        # 中文代码页（gbk）零变化：仍按 ANSI 原样落盘，不加 chcp
        self.assertEqual(cmd_script_bytes(zh, ansi='gbk'), zh.encode('gbk'))

    def test_wscript_bytes_utf16_bom_fallback(self):
        vbs = wscript_text_bytes('f.Write "中文"', ansi='cp1252')
        self.assertTrue(vbs.startswith(codecs.BOM_UTF16_LE))
        self.assertEqual(vbs[len(codecs.BOM_UTF16_LE):].decode('utf-16-le'), 'f.Write "中文"')
        self.assertEqual(wscript_text_bytes('f.Write 1', ansi='cp1252'), b'f.Write 1')

    def test_relay_launcher_survives_non_ansi_env(self):
        # 模拟英文代码页：环境块含中文（PATH=D:\软件\bin），strict mbcs 必失败；
        # 启动器/runner 应回退 UTF-8+chcp，VBS 回退 UTF-16 BOM，全程不抛错。
        with tempfile.TemporaryDirectory(prefix='工具目录_') as folder:
            with patch.object(bt, '_BG_LOG_DIR', Path(folder)), \
                 patch.object(bt, '_merged_child_env', return_value={'PATH': 'D:\\软件\\bin'}), \
                 patch.object(bt, 'cmd_script_bytes', lambda text, ansi='mbcs', **kw: cmd_script_bytes(text, 'cp1252')), \
                 patch.object(bt, 'wscript_text_bytes', lambda text, ansi='mbcs': wscript_text_bytes(text, 'cp1252')), \
                 patch.object(bt, '_wmi_create_process', return_value=4321):
                info = bt._write_relay_script('echo 你好', folder, 'cmd.exe', 'cmd', None, 'fg')
            launcher = Path(info['launcher']).read_bytes()
            self.assertTrue(launcher.startswith(b'@chcp 65001 >nul\r\n'))
            self.assertIn('D:\\软件\\bin'.encode('utf-8'), launcher)
            runner = Path(info['runner']).read_bytes()
            self.assertTrue(runner.startswith(b'@chcp 65001 >nul\r\n'))
            self.assertIn('echo 你好'.encode('utf-8'), runner)
            self.assertTrue(Path(info['vbs']).read_bytes().startswith(codecs.BOM_UTF16_LE))

    def test_runner_interactive_cmd_uses_traditional_oem_console(self):
        # 交互式 cmd 会话控制台固定地区传统 OEM 页（stdin/批处理/输出全跟输出
        # 口径；cmd 在 65001 下读管道 stdin 会逐字节变 U+FFFD，实测硬伤）：
        # 脚本自带 chcp 自校正 + 按传统 OEM 编码落盘。
        with tempfile.TemporaryDirectory() as folder:
            runner = Path(folder, 'run.cmd')
            with patch.object(bt, 'traditional_oem_cp', return_value=936), \
                    patch.object(bt, 'traditional_oem_codec', return_value='gbk'):
                bt._write_runner_script(runner, 'cmd', 'echo 中文', interactive_cmd=True)
            data = runner.read_bytes()
            self.assertTrue(data.startswith('@chcp 936 >nul\r\n'.encode('ascii')))
            self.assertIn('echo 中文'.encode('gbk'), data)

    def test_runner_interactive_cmd_rejects_unencodable_chars_loudly(self):
        # 超集字符（emoji）在 OEM 页写不下：大声报错并指引，不静默建出必乱码的会话
        with tempfile.TemporaryDirectory() as folder:
            runner = Path(folder, 'run.cmd')
            with patch.object(bt, 'traditional_oem_cp', return_value=936), \
                    patch.object(bt, 'traditional_oem_codec', return_value='gbk'):
                with self.assertRaises(ValueError) as ctx:
                    bt._write_runner_script(runner, 'cmd', 'echo 🎉', interactive_cmd=True)
        self.assertIn('powershell', str(ctx.exception).lower())

    def test_gbk_not_mojibake_under_western_locale(self):
        text = '名称,内容\n工具,中文说明\n'
        raw = text.encode('gbk')
        with patch('util.text_encoding.locale.getpreferredencoding', return_value='cp1252'):
            self.assertEqual(decode_text(raw)[0], text)
            self.assertEqual(ff.decode_text_bytes(raw)[0], text)
            self.assertEqual(ff._parse_txt(raw), text)
            self.assertEqual(ff._parse_csv(raw), text)
            self.assertEqual(bt._decode_command_bytes(raw), text)

    def test_western_text_still_ansi_under_western_locale(self):
        raw = 'café naïve'.encode('cp1252')
        with patch('util.text_encoding.locale.getpreferredencoding', return_value='cp1252'):
            out, used = decode_text(raw)
        self.assertEqual(out, 'café naïve')
        self.assertEqual(used, 'cp1252')

    def test_candidate_order_demotes_single_byte_western(self):
        ordered = order_decode_candidates(('utf-8', 'cp1252', 'gbk', 'cp437', 'gb18030'), ansi='cp1252')
        self.assertEqual(ordered, ['utf-8', 'gbk', 'gb18030', 'cp1252', 'cp437'])
        # 中文代码页不降权（双字节 CJK 保持原位）
        self.assertEqual(order_decode_candidates(('utf-8', 'gbk', 'gb18030'), ansi='gbk'),
                         ['utf-8', 'gbk', 'gb18030'])

    def test_poll_output_gbk_before_western(self):
        from factory.agent_runtime import command_output as co
        raw = '中文输出\n'.encode('gbk')
        with patch.object(bt, '_command_output_candidates', return_value=('cp1252', 'cp437', 'utf-8', 'gbk')):
            out, _consumed, used = co._auto_decode(raw, True, None)
        self.assertEqual(out, '中文输出\n')
        self.assertEqual(used, 'gbk')
