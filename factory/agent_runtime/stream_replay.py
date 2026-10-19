"""Transient full-task SSE replay; hot frames remain in the bounded stream deque."""
import tempfile


class StreamReplayJournal:
    def __init__(self):
        # Small tasks stay in memory; large ones spill to an automatically
        # removed private temp file instead of growing the hot deque forever.
        self.file = tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b")
        self.end = 0

    def append(self, chunk: str) -> int:
        data = chunk.encode("utf-8")
        self.file.seek(self.end)
        self.file.write(len(data).to_bytes(8, "big") + data)
        self.end += 8 + len(data)
        return self.end

    def read(self, offset: int) -> tuple[str, int]:
        self.file.seek(offset)
        header = self.file.read(8)
        if len(header) != 8:
            raise OSError("任务回放记录不完整")
        size = int.from_bytes(header, "big")
        data = self.file.read(size)
        if len(data) != size:
            raise OSError("任务回放内容不完整")
        return data.decode("utf-8"), offset + 8 + size

    def close(self):
        try:
            self.file.close()
        except OSError:
            # A failed flush must not fail the real generation or its teardown.
            pass
