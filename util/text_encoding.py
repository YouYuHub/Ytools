"""Byte-to-text decoding shared by tools; never reinterpret valid UTF-8 by scoring Han characters."""
import codecs
import locale
import os

BOMS = ((codecs.BOM_UTF32_LE, 'utf-32'), (codecs.BOM_UTF32_BE, 'utf-32'),
        (codecs.BOM_UTF8, 'utf-8-sig'), (codecs.BOM_UTF16_LE, 'utf-16'),
        (codecs.BOM_UTF16_BE, 'utf-16'))


def unicode_bom(data: bytes) -> str:
    return next((name for bom, name in BOMS if data.startswith(bom)), '')


# Windows 代码页 → Python 编解码器名（控制台 OEM 页 + 系统 ANSI 页全覆盖：
# 各国默认不同——中文 936、俄文 OEM866/ANSI1251、西文 OEM437/850/ANSI1252 等）。
# 读 .cmd 的是控制台代码页（OEM），不是 ANSI：写死 mbcs 会在 ANSI≠OEM 的
# 地区乱码（如俄文机 1251 字节被 866 控制台误读）。
CONSOLE_CP_TO_CODEC = {
    65001: 'utf-8',
    936: 'gbk', 950: 'big5', 932: 'shift_jis', 949: 'cp949',
    437: 'cp437', 720: 'cp720', 737: 'cp737', 775: 'cp775', 850: 'cp850',
    852: 'cp852', 855: 'cp855', 857: 'cp857', 858: 'cp858', 860: 'cp860',
    861: 'cp861', 862: 'cp862', 863: 'cp863', 865: 'cp865', 866: 'cp866',
    869: 'cp869', 874: 'cp874',
    1250: 'cp1250', 1251: 'cp1251', 1252: 'cp1252', 1253: 'cp1253',
    1254: 'cp1254', 1255: 'cp1255', 1256: 'cp1256', 1257: 'cp1257', 1258: 'cp1258',
}


def _kernel32():
    if os.name != 'nt':
        return None
    try:
        import ctypes
        return ctypes.windll.kernel32
    except Exception:
        return None


def oem_console_codec() -> str:
    """新建隐藏控制台的默认编码（系统 OEM 代码页映射）。

    中转链子进程运行在新建隐藏控制台下，代码页恒为系统 OEM 默认值，
    与本进程控制台当前值无关。未知代码页回退 mbcs（=旧行为，不回归）。
    """
    kernel32 = _kernel32()
    if kernel32 is not None:
        try:
            codec = CONSOLE_CP_TO_CODEC.get(int(kernel32.GetOEMCP()))
            if codec:
                return codec
        except Exception:
            pass
    return 'mbcs'


def shared_console_codec() -> tuple:
    """当前进程控制台的（编码，代码页）：写直连子进程共享的脚本用。

    无控制台时返回 (OEM 编码, None)——直连子进程将新建 OEM 控制台。
    """
    kernel32 = _kernel32()
    if kernel32 is not None:
        try:
            cp = int(kernel32.GetConsoleCP())
            if cp:
                return CONSOLE_CP_TO_CODEC.get(cp, 'mbcs'), cp
        except Exception:
            pass
    return oem_console_codec(), None


# GetLocaleInfoW 常量：系统默认地区（“非 Unicode 程序语言”）的默认 OEM 代码页
_LOCALE_SYSTEM_DEFAULT = 0x800
_LOCALE_IDEFAULTCODEPAGE = 11


def traditional_oem_cp() -> int:
    """地区传统 OEM 代码页（不受 Beta UTF-8 影响：中文机恒为 936）。

    cmd.exe 在 65001 控制台下读管道 stdin 会把每个非 ASCII 字节变成 U+FFFD
    （实测），因此 cmd 交互会话的控制台必须设为传统 OEM 页、stdin 按其编码。
    取不到时回退 GetOEMCP()，再失败返回 0（调用方再回退 mbcs）。
    """
    kernel32 = _kernel32()
    if kernel32 is not None:
        try:
            import ctypes
            buf = ctypes.create_unicode_buffer(16)
            if kernel32.GetLocaleInfoW(_LOCALE_SYSTEM_DEFAULT, _LOCALE_IDEFAULTCODEPAGE, buf, 16):
                return int(buf.value)
        except Exception:
            pass
        try:
            return int(kernel32.GetOEMCP())
        except Exception:
            pass
    return 0


def traditional_oem_codec() -> str:
    """地区传统 OEM 代码页对应的 Python 编解码器名；未知回退 OEM 检测。"""
    codec = CONSOLE_CP_TO_CODEC.get(traditional_oem_cp())
    return codec or oem_console_codec()


# 单字节西文代码页（ANSI/OEM）：几乎任何字节序列都能"成功"解码，若排在
# CJK 候选之前，英文代码页服务器（cp1252 等）会把 GBK 中文静默解成西文乱码
# （中文机器 locale=cp936 掩盖了该问题）。自动识别时统一降权到 CJK 之后。
_SINGLE_BYTE_WESTERN = {
    'ascii', 'cp437', 'cp850', 'cp852', 'cp866', 'mac-roman',
    'cp1250', 'cp1251', 'cp1252', 'cp1253', 'cp1254', 'cp1255', 'cp1256', 'cp1257', 'cp1258',
}


def _is_single_byte_western(name: str) -> bool:
    try:
        canonical = codecs.lookup(name).name
    except (LookupError, TypeError):
        return False
    return canonical in _SINGLE_BYTE_WESTERN or canonical.startswith('iso8859')


def order_decode_candidates(names, ansi: str = '') -> list:
    """自动解码候选排序：utf-8 恒最前；单字节西文代码页降到 CJK 候选之后。

    西文组内部系统 ANSI 优先（比通用 latin-1 兜底更可能是真实编码）。
    显式 encoding 不走此排序（用户指定即权威）。双字节 CJK（gbk/big5/shift_jis
    等）与 utf-8 保持原有相对顺序。
    """
    ordered = list(dict.fromkeys(n for n in names if n))
    western = [n for n in ordered if _is_single_byte_western(n)]
    if not western:
        return ordered
    primary = [n for n in ordered if n not in western]
    tail = ([ansi] if ansi in western else []) + [n for n in western if n != ansi]
    return primary + tail


def decode_text(data: bytes, encoding: str = '', candidates=(), *, strict=False):
    """Explicit encoding wins; otherwise BOM, strict UTF-8, supplied/local codecs.

    Detection without a BOM is ambiguous. Explicit encoding is the escape hatch.
    Strict mode prevents lossy edits; replacement fallback is for display only.
    """
    if encoding:
        return data.decode(encoding, errors='strict' if strict else 'replace'), encoding
    bom = unicode_bom(data)
    if bom:
        return data.decode(bom, errors='strict' if strict else 'replace'), bom
    locale_name = locale.getpreferredencoding(False)
    ordered = order_decode_candidates(
        ('utf-8', *candidates, locale_name, 'gb18030', 'big5'), ansi=locale_name)
    for name in ordered:
        if not name:
            continue
        try:
            return data.decode(name), name
        except (UnicodeDecodeError, LookupError):
            continue
    if strict:
        raise UnicodeError('无法可靠解码文本，请显式指定 encoding')
    # 严格全失败的 replace 兜底：优先系统 ANSI（西文单字节文本的真实归属，
    # 排序降权只影响"严格成功"的优先级，不应把兜底也交给 CJK 产生乱码）。
    fallback = next((name for name in (locale_name, *ordered)
                     if name and name.lower() not in ('utf-8', 'utf8')), 'utf-8')
    try:
        return data.decode(fallback, errors='replace'), fallback
    except LookupError:
        return data.decode('utf-8', errors='replace'), 'utf-8'


def cmd_script_bytes(text: str, ansi: str = None, restore_cp=None) -> bytes:
    """cmd 家族脚本（.cmd 启动器/runner/探针）的落盘字节。

    ansi=None（默认）按**新建隐藏控制台**的系统 OEM 代码页编码：中转链子进程
    不共享本进程控制台，cmd 读脚本用的是 OEM 页——写死 mbcs(ANSI) 会在
    ANSI≠OEM 的地区乱码（如俄文机 1251 字节被 866 控制台误读；中文机两者
    都是 936 才碰巧没事）。探针等共享本进程控制台的脚本，调用方传
    shared_console_codec() 的结果。
    目标编码写不下时回退 UTF-8 并首行切代码页（cmd 执行 chcp 后按新代码页
    重读后续行）；restore_cp 非空时末尾恢复原代码页（共享控制台探针用，
    避免污染宿主控制台；隔离的新控制台不需要恢复）。
    ansi 参数显式传值时保持旧语义（测试可模拟其它代码页）。
    """
    if ansi is None:
        ansi = oem_console_codec()
    try:
        return text.encode(ansi)
    except (UnicodeEncodeError, LookupError):
        body = text.replace('\r\n', '\n').replace('\r', '\n').replace('\n', '\r\n')
        out = b'@chcp 65001 >nul\r\n' + body.encode('utf-8')
        if restore_cp:
            if not out.endswith(b'\n'):
                out += b'\r\n'
            out += ('@chcp %d >nul\r\n' % int(restore_cp)).encode('ascii')
        return out


def wscript_text_bytes(text: str, ansi: str = 'mbcs') -> bytes:
    """VBS 包装器落盘字节：mbcs 优先；无法编码时回退 UTF-16 LE BOM
    （Windows Script Host 原生支持带 BOM 的 Unicode 脚本），不再丢字。"""
    try:
        return text.encode(ansi)
    except (UnicodeEncodeError, LookupError):
        return codecs.BOM_UTF16_LE + text.encode('utf-16-le')


def powershell_text_prelude() -> str:
    """Only change this child session; callers can override -Encoding explicitly."""
    return (
        '$__ytoolsUtf8 = New-Object System.Text.UTF8Encoding($false)\n'
        '[Console]::InputEncoding = $__ytoolsUtf8\n'
        '[Console]::OutputEncoding = $__ytoolsUtf8\n'
        '$OutputEncoding = $__ytoolsUtf8\n'
        "$PSDefaultParameterValues['Select-String:Encoding'] = 'utf8'\n"
        "$PSDefaultParameterValues['Get-Content:Encoding'] = 'utf8'\n"
        "$PSDefaultParameterValues['Set-Content:Encoding'] = 'utf8'\n"
        "$PSDefaultParameterValues['Add-Content:Encoding'] = 'utf8'\n"
        "$PSDefaultParameterValues['Out-File:Encoding'] = 'utf8'\n"
    )
