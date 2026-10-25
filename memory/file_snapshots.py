"""Bounded atomic text snapshots. Existing plain-text histories remain readable."""
import gzip
import os
from pathlib import Path
import uuid
import zlib

MAX_BYTES = 64 * 1024 * 1024
SESSION_BYTES = 256 * 1024 * 1024


def encode(text, compressed):
    data = text.encode('utf-8')
    if len(data) > MAX_BYTES:
        raise ValueError('文件快照超过 64 MiB 上限')
    return gzip.compress(data, compresslevel=3, mtime=0) if compressed else data


def usage(root):
    return sum(p.stat().st_size for p in root.glob('*/g*/*.txt*') if p.is_file())


def check_quota(root, additional):
    if usage(root) + additional > SESSION_BYTES:
        raise ValueError('HISTORY_QUOTA_EXCEEDED：会话快照达到容量上限，请保留备份后清理历史')


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = encode(text, path.suffix == '.gz')
    check_quota(path.parents[2], len(data))
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('xb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        from security.resource_limits import storage_write
        # The temporary file is excluded from the metadata scan. A snapshot
        # reservation protects concurrent uploads and other session workers.
        with storage_write(len(data)):
            os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return len(data)


def read(path, *, normalize_legacy=True):
    try:
        opener = gzip.open if path.suffix == '.gz' else open
        with opener(path, 'rb') as handle:
            data = handle.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValueError('快照解压后超过大小上限')
        text = data.decode('utf-8')
        # Legacy .txt snapshots were read with universal-newline conversion.
        return text if path.suffix == '.gz' or not normalize_legacy else text.replace('\r\n','\n').replace('\r','\n')
    except (OSError, EOFError, UnicodeError, zlib.error) as exc:
        raise ValueError('历史快照缺失或损坏，已拒绝恢复；当前文件未改动') from exc
