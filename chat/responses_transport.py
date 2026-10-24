"""Responses HTTP/1.1 + JSON/SSE transport using only the standard library.

Retries and durable state belong to responses_client, never this layer.
"""
import asyncio
import codecs
import json
import queue
import re
import select
import socket
import ssl
import threading
import time
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
import zlib

from .protocols.errors import ResponsesProtocolError

MAX_DATA = 64 * 1024 * 1024
MAX_HEADERS = 65536


class HTTPFailure(RuntimeError):
    def __init__(self, status, detail, headers):
        super().__init__(f"HTTP {status}: {detail[:1800]}")
        self.status_code = status
        self.response_headers = headers


class Stopped(RuntimeError):
    pass


def _head_plan():
    while True:
        line = yield ("line", MAX_HEADERS)
        if not line:
            raise ConnectionError("连接在 HTTP 状态行前关闭")
        parts = line.split(None, 2)
        if len(parts) < 2 or parts[0] not in {b"HTTP/1.0", b"HTTP/1.1"} or not parts[1].isdigit():
            raise ResponsesProtocolError("Responses HTTP 状态行无效")
        status = int(parts[1])
        if not 100 <= status <= 599 or status == 101:
            raise ResponsesProtocolError("Responses 不支持该 HTTP 响应状态")
        headers, size = {}, len(line)
        while True:
            line = yield ("line", MAX_HEADERS)
            if not line:
                raise ConnectionError("连接在 HTTP 响应头中途关闭")
            size += len(line)
            if size > MAX_HEADERS:
                raise ResponsesProtocolError("Responses HTTP 响应头过大")
            if line in {b"\r\n", b"\n"}:
                break
            name, colon, value = line.decode("latin-1").partition(":")
            if not colon:
                raise ResponsesProtocolError("Responses HTTP 响应头无效")
            name, value = name.strip().lower(), value.strip()
            if name == "content-length" and name in headers and headers[name] != value:
                raise ResponsesProtocolError("Responses HTTP Content-Length 冲突")
            headers[name] = value
        if status >= 200:
            return status, headers


def _body_plan(headers):
    encoding = headers.get("transfer-encoding", "").lower()
    if encoding:
        if encoding != "chunked":
            raise ResponsesProtocolError("Responses 不支持该 Transfer-Encoding")
        while True:
            line = yield ("line", MAX_HEADERS)
            if not line:
                raise ConnectionError("Responses chunked 响应提前结束")
            try:
                size = int(line.strip().split(b";", 1)[0], 16)
                if size < 0:
                    raise ValueError()
            except ValueError as exc:
                raise ResponsesProtocolError("Responses HTTP chunk 大小无效") from exc
            if size == 0:
                total = 0
                while True:
                    trailer = yield ("line", MAX_HEADERS)
                    if not trailer:
                        raise ConnectionError("Responses HTTP trailer 提前结束")
                    total += len(trailer)
                    if total > MAX_HEADERS:
                        raise ResponsesProtocolError("Responses HTTP trailer 过大")
                    if trailer in {b"\r\n", b"\n"}:
                        return
            while size:
                data = yield ("read", min(size, 65536))
                if not data:
                    raise ConnectionError("Responses HTTP chunk 内容被截断")
                size -= len(data)
                yield ("emit", data)
            ending = b""
            while len(ending) < 2:
                data = yield ("read", 2 - len(ending))
                if not data:
                    raise ConnectionError("Responses HTTP chunk 结尾被截断")
                ending += data
            if ending != b"\r\n":
                raise ResponsesProtocolError("Responses HTTP chunk 缺少 CRLF")
    else:
        length = headers.get("content-length")
        if length is not None and not length.isdecimal():
            raise ResponsesProtocolError("Responses HTTP Content-Length 无效")
        remaining = int(length) if length is not None else None
        while remaining is None or remaining > 0:
            data = yield ("read", min(remaining, 65536) if remaining is not None else 65536)
            if not data:
                if remaining:
                    raise ConnectionError("Responses HTTP 响应体被截断")
                return
            if remaining is not None:
                remaining -= len(data)
            yield ("emit", data)


class _ContentDecoder:
    def __init__(self, headers):
        encoding = headers.get("content-encoding", "identity").lower()
        if encoding not in {"", "identity", "gzip", "deflate"}:
            raise ResponsesProtocolError("Responses 不支持该 Content-Encoding")
        self.decoder = zlib.decompressobj(31 if encoding == "gzip" else 15) if encoding in {"gzip", "deflate"} else None

    def feed(self, data):
        if not self.decoder:
            return data
        try:
            decoded = self.decoder.decompress(data, MAX_DATA + 1)
        except zlib.error as exc:
            raise ResponsesProtocolError("Responses 压缩响应数据无效") from exc
        if self.decoder.unconsumed_tail or len(decoded) > MAX_DATA:
            raise ResponsesProtocolError("Responses 解压后的数据过大")
        return decoded

    def finish(self):
        if not self.decoder:
            return b""
        if not self.decoder.eof:
            raise ConnectionError("Responses 压缩响应被截断")
        if self.decoder.unused_data:
            raise ResponsesProtocolError("Responses 压缩响应含多余数据")
        return self.decoder.flush()


class _SSEDecoder:
    def __init__(self):
        self.utf8 = codecs.getincrementaldecoder("utf-8-sig")()
        self.buffer, self.event, self.data = "", "", []
        self.size, self.done = 0, False

    def _line(self, line, *, final=False):
        if not line:
            if not self.data:
                self.event = ""
                return None
            content, event = "\n".join(self.data), self.event
            self.event, self.data, self.size = "", [], 0
            if content.strip() == "[DONE]":
                self.done = True
                return None
            try:
                value = json.loads(content)
            except ValueError as exc:
                if final:
                    raise ConnectionError("Responses SSE 最后一帧被截断") from exc
                raise ResponsesProtocolError("Responses SSE 帧不是完整 JSON") from exc
            if not isinstance(value, dict):
                raise ResponsesProtocolError("Responses SSE 数据必须是 JSON 对象")
            if not value.get("type") and event:
                value["type"] = event
            return value
        name, colon, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if colon and name == "event":
            self.event = value
        elif colon and name == "data":
            self.size += len(value)
            if self.size > MAX_DATA:
                raise ResponsesProtocolError("Responses SSE 帧过大")
            self.data.append(value)
        return None

    def feed(self, raw=b"", *, final=False):
        try:
            self.buffer += self.utf8.decode(raw, final=final)
        except UnicodeError as exc:
            if final:
                raise ConnectionError("Responses SSE UTF-8 尾部被截断") from exc
            raise ResponsesProtocolError("Responses SSE UTF-8 数据无效") from exc
        if len(self.buffer) > MAX_DATA:
            raise ResponsesProtocolError("Responses SSE 数据行过大")
        events = []
        start, index = 0, 0
        while index < len(self.buffer):
            char = self.buffer[index]
            if char in "\r\n":
                if char == "\r" and index + 1 == len(self.buffer) and not final:
                    break
                event = self._line(self.buffer[start:index])
                if event is not None:
                    events.append(event)
                index += 2 if char == "\r" and self.buffer[index:index + 2] == "\r\n" else 1
                start = index
                if self.done:
                    break
            else:
                index += 1
        self.buffer = self.buffer[start:]
        if final and not self.done:
            if self.buffer:
                event = self._line(self.buffer)
                if event is not None:
                    events.append(event)
            event = self._line("", final=True)
            if event is not None:
                events.append(event)
            self.buffer = ""
        return events


def _json(data):
    if not data:
        raise ConnectionError("Responses 返回空响应体")
    try:
        value = json.loads(data.decode("utf-8-sig"))
    except (ValueError, UnicodeError) as exc:
        if data.lstrip().lower().startswith((b"<html", b"<!doctype")):
            raise ResponsesProtocolError("平台返回网页，未提供兼容的 Responses API 端点") from exc
        raise ConnectionError("Responses 未返回完整 JSON 响应") from exc
    if not isinstance(value, dict):
        raise ResponsesProtocolError("Responses 响应必须是 JSON 对象")
    return value


def _wire(url, headers, method, payload, *, keep_alive=False):
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise ResponsesProtocolError("Responses API 地址无效")
    try:
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8") if payload is not None else b""
    except (ValueError, TypeError) as exc:
        raise ResponsesProtocolError("Responses 请求包含不能编码为 JSON 的参数") from exc
    host = parts.hostname.encode("idna").decode()
    authority = ("[" + host + "]" if ":" in host else host) + (":" + str(parts.port) if parts.port else "")
    path = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~") + ("?" + parts.query if parts.query else "")
    values = {"content-type": ("Content-Type", "application/json"),
              "accept": ("Accept", "text/event-stream" if payload and payload.get("stream") else "application/json"),
              "accept-encoding": ("Accept-Encoding", "identity")}
    values.update({key.lower(): (key, value) for key, value in headers.items()})
    for name, value in values.values():
        if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name) or any(char in value for char in "\r\n"):
            raise ResponsesProtocolError("Responses 请求头无效，不能包含换行")
    lines = [f"{method} {path} HTTP/1.1", f"Host: {authority}", "Connection: " + ("keep-alive" if keep_alive else "close"), f"Content-Length: {len(body)}"]
    lines.extend(name + ": " + value for name, value in values.values())
    try:
        packet = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body
    except UnicodeError as exc:
        raise ResponsesProtocolError("Responses 请求头或地址包含无效字符") from exc
    return parts, host, packet


class _SyncIO:
    def __init__(self, sock, request, stop):
        self.sock, self.request, self.stop, self.buffer = sock, request, stop, bytearray()
        sock.setblocking(False)

    def check(self):
        if self.stop and self.stop():
            raise Stopped("Responses 请求已停止")

    def _ready(self, deadline, write=False):
        self.check()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Responses 发送超时" if write else "Responses 读取超时")
        select.select([] if write else [self.sock], [self.sock] if write else [], [], min(remaining, 0.1))

    def recv(self, size):
        deadline = time.monotonic() + self.request.timeout_read
        while True:
            self.check()
            try:
                return self.sock.recv(size)
            except (ssl.SSLWantReadError, BlockingIOError):
                self._ready(deadline)
            except ssl.SSLWantWriteError:
                self._ready(deadline, True)

    def read(self, size):
        if self.buffer:
            data = bytes(self.buffer[:size])
            del self.buffer[:size]
            return data
        return self.recv(size)

    def line(self, limit):
        while True:
            position = self.buffer.find(b"\n")
            if position >= 0:
                if position + 1 > limit:
                    raise ResponsesProtocolError("Responses HTTP 数据行过长")
                data = bytes(self.buffer[:position + 1])
                del self.buffer[:position + 1]
                return data
            if len(self.buffer) >= limit:
                raise ResponsesProtocolError("Responses HTTP 数据行过长")
            data = self.recv(65536)
            if not data:
                if self.buffer:
                    raise ConnectionError("Responses HTTP 数据行被截断")
                return b""
            self.buffer.extend(data)

    def send(self, packet):
        deadline = time.monotonic() + self.request.timeout_drain
        view = memoryview(packet)
        while view:
            self.check()
            try:
                count = self.sock.send(view)
                if not count:
                    raise ConnectionError("Responses 发送期间连接关闭")
                view = view[count:]
            except (ssl.SSLWantWriteError, BlockingIOError):
                self._ready(deadline, True)
            except ssl.SSLWantReadError:
                self._ready(deadline)

    def close(self):
        self.sock.close()


def _sync_plan(plan, io):
    value = None
    while True:
        try:
            operation, argument = plan.send(value)
        except StopIteration as done:
            return done.value
        value = io.line(argument) if operation == "line" else io.read(argument)


def _sync_body(headers, io):
    plan, value, decoder = _body_plan(headers), None, _ContentDecoder(headers)
    while True:
        try:
            operation, argument = plan.send(value)
        except StopIteration:
            tail = decoder.finish()
            if tail:
                yield tail
            return
        if operation == "emit":
            decoded = decoder.feed(argument)
            if decoded:
                yield decoded
            value = None
        else:
            value = io.line(argument) if operation == "line" else io.read(argument)


class _ClientBase:
    def __init__(self, url, headers, request):
        self.url, self.headers, self.request = url, headers, request
        self.responses, self.stop, self.active = self, None, set()

    def set_stop(self, stop):
        self.stop = stop

    def _url(self, suffix, extra_query):
        parts = urlsplit(self.url)
        query = parse_qsl(parts.query, keep_blank_values=True)
        if extra_query:
            query = [(key, value) for key, value in query if key not in extra_query]
            query.extend(extra_query.items())
        return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/") + suffix, urlencode(query, doseq=True), ""))


class SyncClient(_ClientBase):
    def _connect(self, parts, host):
        # DNS and TLS can block inside system libraries; quarantine their result
        # on timeout/stop so a late connection can never send the HTTP request.
        result, abandoned, handoff = queue.Queue(), threading.Event(), threading.Lock()
        def connect():
            sock = None
            try:
                sock = socket.create_connection((host, parts.port or (443 if parts.scheme == "https" else 80)), self.request.timeout_connect)
                if parts.scheme == "https":
                    from .http_pool import default_ssl_context
                    sock = default_ssl_context().wrap_socket(sock, server_hostname=host)
                with handoff:
                    if abandoned.is_set():
                        sock.close()
                    else:
                        result.put((sock, None))
            except Exception as exc:
                if sock is not None:
                    sock.close()
                result.put((None, exc))
        threading.Thread(target=connect, daemon=True).start()
        deadline = time.monotonic() + self.request.timeout_connect
        try:
            while True:
                if self.stop and self.stop():
                    raise Stopped("Responses 连接已停止")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Responses 连接超时")
                try:
                    sock, error = result.get(timeout=min(remaining, 0.1))
                except queue.Empty:
                    continue
                if error:
                    raise error
                return _SyncIO(sock, self.request, self.stop)
        except BaseException:
            with handoff:
                abandoned.set()
                try:
                    sock, _ = result.get_nowait()
                    if sock is not None:
                        sock.close()
                except queue.Empty:
                    pass
            raise

    def _call(self, method, suffix, payload, extra_query):
        parts, host, packet = _wire(self._url(suffix, extra_query), self.headers, method, payload)
        io = self._connect(parts, host)
        self.active.add(io)
        handed_off = False
        try:
            io.send(packet)
            status, headers = _sync_plan(_head_plan(), io)
            chunks = _sync_body(headers, io)
            if not 200 <= status < 300 or not (payload and payload.get("stream")) or "application/json" in headers.get("content-type", ""):
                data = bytearray()
                for chunk in chunks:
                    data.extend(chunk)
                    if len(data) > MAX_DATA:
                        raise ResponsesProtocolError("Responses 响应体过大")
                if not 200 <= status < 300:
                    raise HTTPFailure(status, data.decode("utf-8", errors="replace"), headers)
                raw = _json(data)
                if payload and payload.get("stream"):
                    return iter([{"type": "response.created", "response": {"id": raw.get("id")}},
                                 {"type": "response.completed", "response": raw}])
                return raw
            stream = _SyncStream(chunks, io, self.active)
            handed_off = True
            return stream
        finally:
            if not handed_off:
                io.close()
                self.active.discard(io)

    def create(self, *, extra_query=None, **payload):
        return self._call("POST", "", payload, extra_query)

    def retrieve(self, response_id, *, extra_query=None):
        return self._call("GET", "/" + quote(str(response_id), safe=""), None, extra_query)

    def compact(self, *, extra_query=None, **payload):
        return self._call("POST", "/compact", payload, extra_query)

    def close(self):
        for io in list(self.active):
            io.close()
        self.active.clear()


class _SyncStream:
    def __init__(self, chunks, io, active):
        self.chunks, self.io, self.active, self.decoder = chunks, io, active, _SSEDecoder()
        self.pending, self.ended = [], False

    def __iter__(self):
        return self

    def __next__(self):
        while not self.pending:
            if self.ended or self.decoder.done:
                raise StopIteration
            try:
                chunk = next(self.chunks)
            except StopIteration:
                self.ended = True
                self.pending.extend(self.decoder.feed(final=True))
            else:
                self.pending.extend(self.decoder.feed(chunk))
        return self.pending.pop(0)

    def close(self):
        self.io.close()
        self.active.discard(self.io)


class _AsyncIO:
    def __init__(self, reader, writer, request, stop):
        self.reader, self.writer, self.request, self.stop = reader, writer, request, stop

    async def read(self, size):
        from .chat_llm import ChatLLM
        return await ChatLLM._wait_with_stop(self.reader.read(size), self.request.timeout_read, self.stop)

    async def line(self, limit):
        from .chat_llm import ChatLLM
        try:
            line = await ChatLLM._wait_with_stop(self.reader.readline(), self.request.timeout_read, self.stop)
        except ValueError as exc:
            raise ResponsesProtocolError("Responses HTTP 数据行过长") from exc
        if len(line) > limit:
            raise ResponsesProtocolError("Responses HTTP 数据行过长")
        if line and not line.endswith(b"\n"):
            raise ConnectionError("Responses HTTP 数据行被截断")
        return line

    async def close(self):
        self.writer.close()
        try:
            await asyncio.wait_for(self.writer.wait_closed(), 1)
        except (Exception, asyncio.CancelledError):
            pass


async def _async_plan(plan, io):
    value = None
    while True:
        try:
            operation, argument = plan.send(value)
        except StopIteration as done:
            return done.value
        value = await (io.line(argument) if operation == "line" else io.read(argument))


async def _async_body(headers, io):
    plan, value, decoder = _body_plan(headers), None, _ContentDecoder(headers)
    while True:
        try:
            operation, argument = plan.send(value)
        except StopIteration:
            tail = decoder.finish()
            if tail:
                yield tail
            return
        if operation == "emit":
            decoded = decoder.feed(argument)
            if decoded:
                yield decoded
            value = None
        else:
            value = await (io.line(argument) if operation == "line" else io.read(argument))


class AsyncClient(_ClientBase):
    async def _call(self, method, suffix, payload, extra_query):
        from .chat_llm import ChatLLM
        from .http_pool import configure_response
        from util import api_timing
        api_timing.attempt("responses")
        parts, host, packet = _wire(self._url(suffix, extra_query), self.headers, method, payload, keep_alive=True)
        api_timing.mark("request_ready", payload_bytes=len(packet) - packet.index(b"\r\n\r\n") - 4, method=method)
        # Responses 的生成/压缩尝试已在协议客户端登记；底层连接不再登记一遍。
        # GET 查询恢复结果也不是一次新的模型生成。
        reader, writer = await ChatLLM._open_connection(host, parts.port or (443 if parts.scheme == "https" else 80),
            parts.scheme == "https", self.request.timeout_connect, self.stop, meter_attempt=False)
        io = _AsyncIO(reader, writer, self.request, self.stop)
        self.active.add(io)
        handed_off = False
        try:
            writer.write(packet)
            await ChatLLM._wait_with_stop(writer.drain(), self.request.timeout_drain, self.stop)
            api_timing.mark("request_sent")
            status, headers = await _async_plan(_head_plan(), io)
            configure_response(reader, headers)
            chunks = _async_body(headers, io)
            if not 200 <= status < 300 or not (payload and payload.get("stream")) or "application/json" in headers.get("content-type", ""):
                data = bytearray()
                async for chunk in chunks:
                    data.extend(chunk)
                    if len(data) > MAX_DATA:
                        raise ResponsesProtocolError("Responses 响应体过大")
                if not 200 <= status < 300:
                    raise HTTPFailure(status, data.decode("utf-8", errors="replace"), headers)
                raw = _json(data)
                if payload and payload.get("stream"):
                    return _AsyncEvents([{"type": "response.created", "response": {"id": raw.get("id")}},
                                         {"type": "response.completed", "response": raw}])
                return raw
            handed_off = True
            return _AsyncStream(chunks, io, self.active)
        finally:
            if not handed_off:
                await io.close()
                self.active.discard(io)

    async def create(self, *, extra_query=None, **payload):
        return await self._call("POST", "", payload, extra_query)

    async def retrieve(self, response_id, *, extra_query=None):
        return await self._call("GET", "/" + quote(str(response_id), safe=""), None, extra_query)

    async def compact(self, *, extra_query=None, **payload):
        return await self._call("POST", "/compact", payload, extra_query)

    async def close(self):
        for io in list(self.active):
            await io.close()
        self.active.clear()


class _AsyncEvents:
    def __init__(self, events):
        self.events = iter(events)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self.events)
        except StopIteration:
            raise StopAsyncIteration from None

    async def close(self):
        pass


class _AsyncStream:
    def __init__(self, chunks, io, active):
        self.chunks, self.io, self.active, self.decoder = chunks, io, active, _SSEDecoder()
        self.pending, self.ended = [], False

    def __aiter__(self):
        return self

    async def __anext__(self):
        while not self.pending:
            if self.ended or self.decoder.done:
                raise StopAsyncIteration
            try:
                chunk = await self.chunks.__anext__()
            except StopAsyncIteration:
                self.ended = True
                self.pending.extend(self.decoder.feed(final=True))
            else:
                self.pending.extend(self.decoder.feed(chunk))
                if self.pending:
                    from util.api_timing import mark
                    mark("first_upstream_event")
        return self.pending.pop(0)

    async def close(self):
        await self.io.close()
        self.active.discard(self.io)
