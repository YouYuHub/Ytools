"""Disk-backed line snapshots: bounded memory, version-keyed reuse, automatic cleanup."""
from array import array
from collections import OrderedDict
import tempfile
import threading

_cache = OrderedDict()
_lock = threading.Lock()
_MAX_BYTES = 64 * 1024 * 1024
_STRIDE = 128


class TextIndex:
    def __init__(self, text, lines):
        from .tool_control import check_tool_control
        self.file = tempfile.TemporaryFile(mode='w+b')
        self.lock = threading.Lock()
        self.offsets = array('Q')
        self.total = self.size = 0
        for no, line in enumerate(lines(text)):
            if no % _STRIDE == 0:
                check_tool_control()
                self.offsets.append(self.size)
            data = line.encode('utf-8') + b'\n'
            if self.size + len(data) > _MAX_BYTES:
                self.file.close()
                raise OverflowError('文本快照超过缓存配额')
            self.file.write(data)
            self.size += len(data)
            self.total += 1
        self.file.flush()

    def select(self, start, end, limit):
        from .tool_control import check_tool_control
        if start > self.total:
            return []
        group = (start - 1) // _STRIDE
        with self.lock:
            self.file.seek(self.offsets[group])
            for _ in range(start - 1 - group * _STRIDE):
                self.file.readline()
            selected = []
            for _ in range(min(limit, self.total - start + 1, end - start + 1)):
                check_tool_control()
                selected.append(self.file.readline().decode('utf-8')[:-1])
            return selected


def get(key):
    with _lock:
        value = _cache.get(key)
        if value:
            _cache.move_to_end(key)
        return value


def store(key, text, encoding, content_hash, lines):
    try:
        index = TextIndex(text, lines)
    except OverflowError:
        return None
    value = (index, encoding, content_hash, None, len(index.offsets) * 8)
    with _lock:
        existing = _cache.get(key)
        if existing:
            return existing
        _cache[key] = value
        while len(_cache) > 8 or sum(item[0].size for item in _cache.values()) > _MAX_BYTES:
            _cache.popitem(last=False)
    # TemporaryFile is released when both the cache and in-flight readers drop
    # their references; eviction never closes a snapshot another call is reading.
    return value
