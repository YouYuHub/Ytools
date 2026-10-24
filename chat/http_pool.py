"""Bounded, event-loop-local HTTP/1.1 connections, with conservative reuse.

No automatic request replay: retries and model-call accounting stay with callers.
Only fully framed responses can return a connection to the pool. Closing an SSE
early drains bytes already buffered, never waits for a supplier's trailing data.
"""
import asyncio
import os
import ssl
import threading
import time

_SSL_LOCK = threading.Lock()
_SSL_CONTEXT = None
_SSL_KEY = None
_SSL_CREATED = 0.0
IDLE_SECONDS = 20
MAX_IDLE = 8
MAX_ORIGIN_IDLE = 2


def _stopped(stop):
    try:
        return bool(stop and stop())
    except Exception:
        return False


def default_ssl_context():
    """Keep certificate/hostname verification; reload on CA path changes or expiry."""
    global _SSL_CONTEXT, _SSL_KEY, _SSL_CREATED
    paths = ssl.get_default_verify_paths()
    def stamp(path):
        try:
            stat = os.stat(path) if path else None
            return (path, stat.st_mtime_ns, stat.st_size) if stat else (path,)
        except OSError:
            return (path, None)
    key = (stamp(paths.cafile), stamp(paths.capath), os.getenv("SSL_CERT_FILE"), os.getenv("SSL_CERT_DIR"))
    with _SSL_LOCK:
        now = time.monotonic()
        if _SSL_CONTEXT is None or key != _SSL_KEY or now - _SSL_CREATED > 300:
            _SSL_CONTEXT = ssl.create_default_context()
            _SSL_KEY, _SSL_CREATED = key, now
        return _SSL_CONTEXT


class _Boundary:
    def __init__(self, headers, http11):
        self.done = False
        self.valid = True
        self.reusable = http11 and "close" not in headers.get("connection", "").lower()
        self.line = bytearray()
        self.remaining = 0
        encoding = headers.get("transfer-encoding", "").lower()
        length = headers.get("content-length")
        if encoding == "chunked" and length is None:
            self.state = "size"
        elif not encoding and length is not None and length.isdecimal():
            self.state, self.remaining = "fixed", int(length)
            self.done = self.remaining == 0
        else:
            self.state, self.valid = "eof", False

    def feed(self, data):
        position = 0
        while position < len(data) and self.valid:
            if self.done:
                self.valid = False  # Unexpected bytes after the response boundary.
                break
            if self.state in {"fixed", "data"}:
                count = min(self.remaining, len(data) - position)
                self.remaining -= count
                position += count
                if not self.remaining:
                    if self.state == "fixed":
                        self.done = True
                    else:
                        self.state = "ending"
                continue
            end = data.find(b"\n", position)
            until = len(data) if end < 0 else end + 1
            self.line.extend(data[position:until])
            position = until
            if len(self.line) > 65536:
                self.valid = False
            if end < 0:
                break
            line = bytes(self.line)
            self.line.clear()
            if self.state == "ending":
                self.valid = line == b"\r\n"
                self.state = "size"
            elif self.state == "trailers":
                self.done = line == b"\r\n"
            else:
                try:
                    self.remaining = int(line.strip().split(b";", 1)[0], 16)
                    if self.remaining < 0:
                        raise ValueError()
                except ValueError:
                    self.valid = False
                self.state = "data" if self.remaining else "trailers"


class _Reader:
    def __init__(self, raw):
        self.raw, self.boundary, self.http11 = raw, None, False

    def configure(self, headers):
        self.boundary = _Boundary(headers, self.http11)

    def _seen(self, data):
        if self.boundary is not None:
            self.boundary.feed(data)
        elif data.startswith(b"HTTP/"):
            self.http11 = data.startswith(b"HTTP/1.1 ")
        return data

    async def read(self, n=-1):
        boundary = self.boundary
        if boundary is not None and boundary.done:
            return b""
        if boundary is not None and boundary.state == "fixed":
            size = boundary.remaining if n < 0 else min(n, boundary.remaining)
            # read() promises the entire body, read(n) only the available prefix.
            data = await (self.raw.readexactly(size) if n < 0 else self.raw.read(size))
        else:
            data = await self.raw.read(n)
        return self._seen(data)

    async def readline(self):
        if self.boundary is not None and self.boundary.done:
            return b""
        return self._seen(await self.raw.readline())

    async def readexactly(self, n):
        return self._seen(await self.raw.readexactly(n))

    def __getattr__(self, name):
        return getattr(self.raw, name)


class _Writer:
    def __init__(self, raw, reader, pool, key, stop):
        self.raw, self.reader, self.pool, self.key = raw, reader, pool, key
        self.stop, self.closing, self.finalized = stop, False, False

    def close(self):
        self.closing = True

    async def wait_closed(self):
        if self.finalized:
            return
        self.finalized = True
        reader, raw = self.reader, self.reader.raw
        boundary = reader.boundary
        stopped = _stopped(self.stop)
        # Only inspect the StreamReader's already buffered bytes. Never await a
        # remote trailer here; an unfinished stream is discarded immediately.
        if not stopped and boundary is not None and boundary.valid and not boundary.done:
            available = len(raw._buffer)
            if 0 < available <= 65536:
                boundary.feed(await raw.read(available))
        if (not stopped and boundary is not None and boundary.valid and boundary.reusable and boundary.done
                and not raw._buffer and not raw.at_eof() and raw.exception() is None
                and not self.raw.is_closing()):
            self.pool.put(self.key, raw, self.raw)
            return
        self.raw.close()
        try:
            await asyncio.wait_for(self.raw.wait_closed(), 1)
        except (Exception, asyncio.CancelledError):
            pass

    def is_closing(self):
        return self.closing or self.raw.is_closing()

    def __getattr__(self, name):
        return getattr(self.raw, name)


class _Pool:
    def __init__(self, loop):
        self.loop, self.idle = loop, []

    def _drop(self, entry):
        if entry in self.idle:
            self.idle.remove(entry)
        entry[3].cancel()
        entry[2].close()

    def take(self, key):
        for entry in list(reversed(self.idle)):
            if entry[0] != key:
                continue
            self.idle.remove(entry)
            entry[3].cancel()
            _, reader, writer, _ = entry
            if not reader.at_eof() and not reader._buffer and reader.exception() is None and not writer.is_closing():
                return reader, writer
            writer.close()
        return None

    def put(self, key, reader, writer):
        while len(self.idle) >= MAX_IDLE:
            self._drop(self.idle[0])
        same = [entry for entry in self.idle if entry[0] == key]
        if len(same) >= MAX_ORIGIN_IDLE:
            self._drop(same[0])
        entry = [key, reader, writer, None]
        entry[3] = self.loop.call_later(IDLE_SECONDS, self._drop, entry)
        self.idle.append(entry)

    def close(self):
        for entry in list(self.idle):
            self._drop(entry)


def _pool():
    loop = asyncio.get_running_loop()
    pool = getattr(loop, "_ytools_http_pool", None)
    if pool is None:
        pool = loop._ytools_http_pool = _Pool(loop)
    return pool


async def open_connection(host, port, use_ssl, timeout, stop, wait):
    # Pool hits bypass the cancellable socket-opening await. Preserve the same
    # stop boundary before returning an already connected writer to the caller.
    if _stopped(stop):
        raise asyncio.CancelledError("stopped by client")
    pool = _pool()
    context = default_ssl_context() if use_ssl else None
    key = (host.casefold(), port, context)
    connection = pool.take(key)
    reused = connection is not None
    if connection is None:
        kwargs = {"ssl": context, "server_hostname": host, "ssl_handshake_timeout": timeout} if use_ssl else {}
        connection = await wait(asyncio.open_connection(host, port, **kwargs), timeout, stop)
    raw_reader, raw_writer = connection
    reader = _Reader(raw_reader)
    writer = _Writer(raw_writer, reader, pool, key, stop)
    writer.reused = reused
    return reader, writer


def configure_response(reader, headers):
    from util.api_timing import mark
    mark("headers_received")
    if isinstance(reader, _Reader):
        reader.configure(headers)


def close_idle_connections():
    pool = getattr(asyncio.get_running_loop(), "_ytools_http_pool", None)
    if pool is not None:
        pool.close()
