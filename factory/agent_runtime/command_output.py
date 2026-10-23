"""Byte cursors for append-only command logs, with bounded reads and decoding."""
import base64
import codecs
import hashlib
import json
from pathlib import Path


def _decode(data, *, final, encoding, candidates):
    names = (encoding,) if encoding else tuple(dict.fromkeys(('utf-8', *candidates)))
    for name in names:
        try:
            decoder = codecs.getincrementaldecoder(name)(errors='strict')
            text = decoder.decode(data, final=final)
            pending = decoder.getstate()[0]
            return text, len(data) - len(pending), name
        except UnicodeDecodeError:
            continue
    name = encoding or 'utf-8'
    decoder = codecs.getincrementaldecoder(name)(errors='replace')
    text = decoder.decode(data, final=final)
    return text, len(data) - len(decoder.getstate()[0]), name


def read(meta, cursor, max_chars, *, finished=False, encoding=''):
    from .builtin_tools import _command_output_candidates
    offset, identity, prefix = 0, None, None
    if cursor:
        try:
            if len(cursor) > 512:
                raise ValueError()
            value = json.loads(base64.urlsafe_b64decode(cursor.encode()))
            if value['job'] != meta['job_id'] or type(value['offset']) is not int or value['offset'] < 0:
                raise ValueError()
            offset, identity, prefix = value['offset'], value['identity'], value['prefix']
        except (ValueError, KeyError, TypeError, UnicodeError) as exc:
            raise ValueError('cursor 无效或不属于该任务；从头读取请传 cursor=""') from exc
    path = Path(meta['output_path'])
    try:
        handle = path.open('rb')
    except FileNotFoundError:
        if offset:
            raise ValueError('OUTPUT_RESET：日志已移走，从头读取请传 cursor=""')
        return {'output': '', 'next_cursor': '', 'has_more': False, 'encoding': encoding or None}
    with handle:
        import os
        stat = os.fstat(handle.fileno())
        current_identity = [stat.st_dev, stat.st_ino]
        head = handle.read(min(offset, 64))
        if identity is not None and (identity != current_identity or stat.st_size < offset or
                                      prefix != hashlib.sha256(head).hexdigest()):
            raise ValueError('OUTPUT_RESET：日志被替换或截断，从头读取请传 cursor=""')
        handle.seek(0)
        bom = handle.read(4)
        # Use an endian-specific codec after the BOM, so resumed chunks need no BOM.
        detected, skip = '', 0
        for marker, codec in ((codecs.BOM_UTF32_LE, 'utf-32-le'), (codecs.BOM_UTF32_BE, 'utf-32-be'),
                              (codecs.BOM_UTF8, 'utf-8'), (codecs.BOM_UTF16_LE, 'utf-16-le'),
                              (codecs.BOM_UTF16_BE, 'utf-16-be')):
            if bom.startswith(marker):
                detected, skip = codec, len(marker)
                break
        encoding = encoding or detected
        if encoding in {'utf-16', 'utf-32'} and detected.startswith(encoding):
            encoding = detected
        offset = max(offset, skip)
        handle.seek(offset)
        # Reading at most max_chars bytes also bounds decoded characters.
        data = handle.read(max_chars)
        end = offset + len(data)
        final = finished and end >= stat.st_size
        if encoding:
            output, consumed, used = _decode(data, final=final, encoding=encoding, candidates=())
        else:
            # Detect complete lines separately: Windows tools may mix UTF-8 and OEM output.
            output, consumed, used_encodings = [], 0, set()
            pieces = data.splitlines(keepends=True)
            for i, piece in enumerate(pieces):
                text, size, used = _decode(piece, final=i < len(pieces) - 1 or piece.endswith(b'\n') or final,
                                           encoding='', candidates=_command_output_candidates())
                output.append(text)
                consumed += size
                used_encodings.add(used)
                if size < len(piece):
                    break
            output = ''.join(output)
            used = next(iter(used_encodings)) if len(used_encodings) == 1 else 'mixed' if used_encodings else None
        offset += consumed
        handle.seek(0)
        prefix = hashlib.sha256(handle.read(min(offset, 64))).hexdigest()
        token = base64.urlsafe_b64encode(json.dumps({'job': meta['job_id'], 'offset': offset,
                'identity': current_identity, 'prefix': prefix}, separators=(',', ':')).encode()).decode()
        return {'output': output, 'next_cursor': token,
                'has_more': offset < stat.st_size, 'encoding': used,
                'decoding_errors': '\ufffd' in output}
