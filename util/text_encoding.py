"""Byte-to-text decoding shared by tools; never reinterpret valid UTF-8 by scoring Han characters."""
import codecs
import locale

BOMS = ((codecs.BOM_UTF32_LE, 'utf-32'), (codecs.BOM_UTF32_BE, 'utf-32'),
        (codecs.BOM_UTF8, 'utf-8-sig'), (codecs.BOM_UTF16_LE, 'utf-16'),
        (codecs.BOM_UTF16_BE, 'utf-16'))


def unicode_bom(data: bytes) -> str:
    return next((name for bom, name in BOMS if data.startswith(bom)), '')


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


def cmd_script_bytes(text: str, ansi: str = 'mbcs') -> bytes:
    """cmd 家族脚本（.cmd/.bat 启动器与 runner）的落盘字节。

    优先按系统 ANSI 代码页（mbcs）无损编码：中文机器（cp936）与控制台默认
    代码页一致，行为零变化。非中文代码页服务器（如英文 cp1252）上，环境块
    （PATH 等）、命令或路径一旦含中文，strict mbcs 会抛 UnicodeEncodeError
    ——这是终端工具在该类服务器上所有模式全失败的根因（启动器要注入整份
    环境变量，纯 ASCII 命令也躲不开）。此时回退 UTF-8 并在首行切代码页：
    cmd 执行 chcp 后会按新代码页重新读取后续行，中文得以原样保留；首行
    自身纯 ASCII，不受旧代码页影响。ansi 参数仅供测试模拟其它代码页。
    """
    try:
        return text.encode(ansi)
    except (UnicodeEncodeError, LookupError):
        body = text.replace('\r\n', '\n').replace('\r', '\n').replace('\n', '\r\n')
        return b'@chcp 65001 >nul\r\n' + body.encode('utf-8')


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
