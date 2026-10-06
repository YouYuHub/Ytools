"""Byte-to-text decoding shared by tools; never reinterpret valid UTF-8 by scoring Han characters."""
import codecs
import locale

BOMS = ((codecs.BOM_UTF32_LE, 'utf-32'), (codecs.BOM_UTF32_BE, 'utf-32'),
        (codecs.BOM_UTF8, 'utf-8-sig'), (codecs.BOM_UTF16_LE, 'utf-16'),
        (codecs.BOM_UTF16_BE, 'utf-16'))


def unicode_bom(data: bytes) -> str:
    return next((name for bom, name in BOMS if data.startswith(bom)), '')


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
    ordered = list(dict.fromkeys(('utf-8', *candidates, locale.getpreferredencoding(False), 'gb18030', 'big5')))
    for name in ordered:
        if not name:
            continue
        try:
            return data.decode(name), name
        except (UnicodeDecodeError, LookupError):
            continue
    if strict:
        raise UnicodeError('无法可靠解码文本，请显式指定 encoding')
    fallback = next((name for name in ordered if name and name.lower() not in ('utf-8', 'utf8')), 'utf-8')
    try:
        return data.decode(fallback, errors='replace'), fallback
    except LookupError:
        return data.decode('utf-8', errors='replace'), 'utf-8'


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
