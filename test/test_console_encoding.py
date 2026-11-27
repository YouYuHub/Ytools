"""终端编码跟随系统控制台代码页（多地区 Windows 兼容），而非写死。"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from factory.agent_runtime import command_pty
from util import text_encoding as te


class FakeKernel32:
    def __init__(self, oem=936, console=936, out=936, locale_oem=936):
        self._oem, self._console, self._out = oem, console, out
        self._locale_oem = locale_oem

    def GetOEMCP(self):
        return self._oem

    def GetConsoleCP(self):
        if self._console == 0:
            raise OSError('no console')
        return self._console

    def GetConsoleOutputCP(self):
        return self._out

    def GetLocaleInfoW(self, locale, lctype, buf, size):
        buf.value = str(self._locale_oem)
        return len(str(self._locale_oem)) + 1


class ConsoleCodecDetectionTests(unittest.TestCase):
    def test_oem_codec_maps_system_oem_page(self):
        with patch.object(te, '_kernel32', return_value=FakeKernel32(oem=866)):
            self.assertEqual(te.oem_console_codec(), 'cp866')
        with patch.object(te, '_kernel32', return_value=FakeKernel32(oem=936)):
            self.assertEqual(te.oem_console_codec(), 'gbk')
        with patch.object(te, '_kernel32', return_value=FakeKernel32(oem=65001)):
            self.assertEqual(te.oem_console_codec(), 'utf-8')

    def test_oem_codec_unknown_page_falls_back_to_mbcs(self):
        with patch.object(te, '_kernel32', return_value=FakeKernel32(oem=9999)):
            self.assertEqual(te.oem_console_codec(), 'mbcs')

    def test_shared_codec_uses_live_console_page(self):
        with patch.object(te, '_kernel32', return_value=FakeKernel32(oem=936, console=65001)):
            codec, cp = te.shared_console_codec()
            self.assertEqual((codec, cp), ('utf-8', 65001))

    def test_shared_codec_without_console_falls_back_to_oem(self):
        with patch.object(te, '_kernel32', return_value=FakeKernel32(oem=437, console=0)):
            codec, cp = te.shared_console_codec()
            self.assertEqual(codec, 'cp437')
            self.assertIsNone(cp)

    @unittest.skipUnless(os.name == 'nt', '真实系统代码页仅 Windows 可读')
    def test_real_detection_returns_usable_values(self):
        self.assertTrue(te.oem_console_codec())
        codec, cp = te.shared_console_codec()
        self.assertTrue(codec)
        self.assertTrue(cp is None or cp > 0)
        # 地区传统页与独立查询一致（Beta 机器上与 GetOEMCP 不同，不可混用）
        import ctypes
        buf = ctypes.create_unicode_buffer(16)
        ctypes.windll.kernel32.GetLocaleInfoW(0x800, 11, buf, 16)
        self.assertEqual(te.traditional_oem_cp(), int(buf.value))
        self.assertTrue(te.traditional_oem_codec())

    def test_traditional_oem_ignores_beta_utf8(self):
        # Beta 机器：GetOEMCP()=65001，但地区传统页仍按 locale 取 936
        with patch.object(te, '_kernel32', return_value=FakeKernel32(oem=65001, locale_oem=936)):
            self.assertEqual(te.traditional_oem_cp(), 936)
            self.assertEqual(te.traditional_oem_codec(), 'gbk')

    def test_traditional_oem_falls_back_to_oemcp(self):
        class _NoLocale(FakeKernel32):
            def GetLocaleInfoW(self, *args):
                raise OSError('no locale api')

        with patch.object(te, '_kernel32', return_value=_NoLocale(oem=866)):
            self.assertEqual(te.traditional_oem_cp(), 866)
            self.assertEqual(te.traditional_oem_codec(), 'cp866')


class CmdScriptConsoleEncodingTests(unittest.TestCase):
    def test_russian_oem_console_uses_cp866_not_mbcs(self):
        # 俄文机 ANSI=1251、控制台 OEM=866：.cmd 按控制台页落盘才不会乱码
        ru = '@echo off\r\necho Привет\r\n'
        with patch.object(te, 'oem_console_codec', return_value='cp866'):
            self.assertEqual(te.cmd_script_bytes(ru), ru.encode('cp866'))

    def test_unencodable_falls_back_to_utf8_chcp(self):
        with patch.object(te, 'oem_console_codec', return_value='cp437'):
            out = te.cmd_script_bytes('@echo off\r\necho 中文\r\n')
        self.assertTrue(out.startswith(b'@chcp 65001 >nul\r\n'))
        self.assertIn('echo 中文'.encode('utf-8'), out)
        self.assertNotIn(b'chcp 437', out)

    def test_shared_console_fallback_restores_original_page(self):
        out = te.cmd_script_bytes('@echo off\r\necho 中文\r\n', ansi='cp437', restore_cp=437)
        self.assertTrue(out.startswith(b'@chcp 65001 >nul\r\n'))
        self.assertTrue(out.endswith(b'@chcp 437 >nul\r\n'))

    def test_direct_encode_never_adds_chcp_lines(self):
        out = te.cmd_script_bytes('@echo off\r\necho hi\r\n', ansi='cp437', restore_cp=437)
        self.assertNotIn(b'chcp', out)


class StdinCodecTests(unittest.TestCase):
    def test_cmd_always_uses_traditional_oem_codec(self):
        # cmd 在 65001 下读管道 stdin 会把非 ASCII 字节逐个变成 U+FFFD（实测硬伤），
        # 因此 cmd 会话永远走传统 OEM 口径，不看强制标志
        with patch.object(te, 'traditional_oem_codec', return_value='gbk'):
            self.assertEqual(
                command_pty.resolve_stdin_codec(shell_kind='cmd', console_forced_utf8=True,
                                                child_self_forces_utf8=True), 'gbk')
            self.assertEqual(
                command_pty.resolve_stdin_codec(shell_kind='cmd', console_forced_utf8=False,
                                                child_self_forces_utf8=False), 'gbk')

    def test_forced_or_self_forced_console_uses_utf8(self):
        self.assertEqual(command_pty.resolve_stdin_codec(
            shell_kind='powershell', console_forced_utf8=True, child_self_forces_utf8=False), 'utf-8')
        self.assertEqual(command_pty.resolve_stdin_codec(
            shell_kind='powershell', console_forced_utf8=False, child_self_forces_utf8=True), 'utf-8')

    def test_unforced_console_falls_back_to_oem_codec(self):
        with patch.object(te, 'oem_console_codec', return_value='cp437'):
            self.assertEqual(
                command_pty.resolve_stdin_codec(shell_kind='powershell', console_forced_utf8=False,
                                                child_self_forces_utf8=False), 'cp437')

    def test_pipe_write_honest_error_when_unencodable(self):
        pipe = command_pty.Pipe.__new__(command_pty.Pipe)
        pipe.stdin_codec = 'cp437'

        class _Sink:
            def __init__(self):
                self.data = bytearray()

            def write(self, chunk):
                self.data += bytes(chunk)
                return len(chunk)

            def flush(self):
                pass

        class _Child:
            def __init__(self):
                self.stdin = _Sink()

        pipe.child = _Child()
        pipe.write('dir\n')
        self.assertEqual(bytes(pipe.child.stdin.data), b'dir\n')
        with self.assertRaises(ValueError) as ctx:
            pipe.write('中文\n')
        self.assertIn('cp437', str(ctx.exception))

    def test_stdin_env_follows_writer_codec_not_hardcoded_utf8(self):
        # 跟随 stdin 字节编码（cmd/GBK 会话声明 GBK），只设 IOENCODING 不设
        # PYTHONUTF8（后者会破坏 cmd 会话的一致性）
        self.assertEqual(command_pty.stdin_env_vars('gbk'), {'PYTHONIOENCODING': 'gbk'})
        self.assertEqual(command_pty.stdin_env_vars('utf-8'), {'PYTHONIOENCODING': 'utf-8'})


class PsParseDecodeTests(unittest.TestCase):
    def test_gbk_checker_output_decodes_by_detection_not_hardcoded_utf8(self):
        from factory.agent_runtime import builtin_tools as bt
        payload = '["缺少右引号"]'.encode('gbk')
        fake = subprocess.CompletedProcess(args=['powershell'], returncode=0, stdout=payload, stderr=b'')
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(bt, '_BG_LOG_DIR', Path(folder)), \
                patch.object(subprocess, 'run', return_value=fake) as mocked:
            errors = bt._ps_parse_errors('powershell.exe', "Write-Output 'x'")
        mocked.assert_called_once()
        self.assertEqual(errors, ['缺少右引号'])
