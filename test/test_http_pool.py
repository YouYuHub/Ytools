"""Loopback-only keep-alive, framing and cancellation regressions."""
import asyncio
import json

import pytest

from chat.chat_llm import ChatLLM
from chat import http_pool
from chat.responses_transport import AsyncClient
from config import ChatLLMRequest


async def _exercise(callback, *, chunked=False, close=False, messages=False, tls=None):
    accepted, tasks = [], set()
    body = b'data: {"choices":[{"delta":{"content":"OK"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
    if messages:
        from test_messages_api import frames, sse
        body = sse(frames())

    async def handle(reader, writer):
        tasks.add(asyncio.current_task())
        accepted.append(writer)
        try:
            while True:
                header = await reader.readuntil(b"\r\n\r\n")
                size = next(int(line.split(b":", 1)[1]) for line in header.split(b"\r\n") if line.lower().startswith(b"content-length:"))
                await reader.readexactly(size)
                framing = b"Transfer-Encoding: chunked\r\n" if chunked else f"Content-Length: {len(body)}\r\n".encode()
                packet = f"{len(body):x};ext=yes\r\n".encode() + body + b"\r\n0\r\nX-Trailer: yes\r\n\r\n" if chunked else body
                writer.write(b"HTTP/1.1 200 OK\r\n" + framing + (b"Connection: close\r\n" if close else b"") + b"\r\n" + packet)
                await writer.drain()
                if close:
                    break
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()
            tasks.discard(asyncio.current_task())

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=tls)
    port = server.sockets[0].getsockname()[1]
    try:
        await callback(port, accepted)
    finally:
        http_pool.close_idle_connections()
        server.close()
        await server.wait_closed()
        for task in list(tasks):
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("chunked", [False, True])
def test_chat_stream_reuses_only_complete_connections(chunked):
    async def check(port, accepted):
        config = {"url": f"http://127.0.0.1:{port}/v1", "selected_model_id": "test", "apiType": "chat-completions"}
        for _ in range(3):
            frames = [frame async for frame in ChatLLM.async_std_completions_sse(
                ChatLLMRequest(messages=[{"role": "user", "content": "hi"}], timeout_read=1), model_config=config)]
            assert any('"content": "OK"' in frame for frame in frames)
            assert frames[-1] == "data: [DONE]\n\n"
        assert len(accepted) == 1
    asyncio.run(_exercise(check, chunked=chunked))


def test_server_close_is_not_reused():
    async def check(port, accepted):
        for _ in range(2):
            reader, writer = await ChatLLM._open_connection("127.0.0.1", port, False, 1)
            writer.write(b"POST / HTTP/1.1\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            await reader.readline()
            headers = {}
            while (line := await reader.readline()) != b"\r\n":
                key, value = line.decode().split(":", 1)
                headers[key.lower()] = value.strip()
            http_pool.configure_response(reader, headers)
            await reader.read()
            writer.close()
            await writer.wait_closed()
        assert len(accepted) == 2
    asyncio.run(_exercise(check, close=True))


@pytest.mark.parametrize("stop", [False, True])
def test_early_unread_response_and_user_stop_are_discarded(stop):
    async def check(port, accepted):
        stopped = [False]
        for _ in range(2):
            reader, writer = await ChatLLM._open_connection("127.0.0.1", port, False, 1, lambda: stopped[0])
            writer.write(b"POST / HTTP/1.1\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            # Closing before headers/body are validated cannot pool the socket.
            await reader.readline()
            stopped[0] = stop
            writer.close()
            await writer.wait_closed()
            stopped[0] = False
        assert len(accepted) == 2
    asyncio.run(_exercise(check))


@pytest.mark.parametrize("headers,body", [
    ({"transfer-encoding": "chunked"}, b"3\r\nabc\r\n0\r\n\r\nextra"),
    ({"transfer-encoding": "chunked"}, b"3\r\nabcXX\n0\r\n\r\n"),
    ({"transfer-encoding": "chunked", "content-length": "3"}, b"abc"),
    ({"content-length": "3"}, b"abcEXTRA"),
    ({}, b"unframed"),
])
def test_ambiguous_boundaries_are_never_reused(headers, body):
    boundary = http_pool._Boundary(headers, True)
    for byte in body:
        boundary.feed(bytes([byte]))
    assert not boundary.valid


def test_responses_json_reuses_connection_and_body_is_unchanged():
    async def check(port, accepted):
        client = AsyncClient(f"http://127.0.0.1:{port}", {}, ChatLLMRequest(messages=[], timeout_read=1))
        # The loopback returns SSE bytes; exercise its non-stream body path with
        # a patched JSON decoder, retaining exact body bytes for both calls.
        from unittest.mock import patch
        with patch("chat.responses_transport._json", lambda data: bytes(data)):
            first = await client.create(model="m", stream=False)
            second = await client.create(model="m", stream=False)
        await client.close()
        assert first == second and first.startswith(b"data:")
        assert len(accepted) == 1
    asyncio.run(_exercise(check))


def test_messages_stream_reuses_connection(tmp_path, monkeypatch):
    from chat import messages_client
    from chat.protocols import native_store
    monkeypatch.setattr(native_store, "_root", lambda: tmp_path)
    async def check(port, accepted):
        config = {"url": f"http://127.0.0.1:{port}/v1", "selected_model_id": "m", "apiType": "messages"}
        for _ in range(2):
            request = ChatLLMRequest(messages=[{"role": "user", "content": "hi"}], timeout_read=1)
            result = [frame async for frame in messages_client.async_stream(ChatLLM, request, config)]
            assert result[-1] == "data: [DONE]\n\n"
            assert any("你好" in frame for frame in result)
        assert len(accepted) == 1
    asyncio.run(_exercise(check, chunked=True, messages=True))


def test_ssl_context_reuse_keeps_verification_and_reloads_environment(monkeypatch):
    import ssl
    monkeypatch.setattr(http_pool, "_SSL_CONTEXT", None)
    monkeypatch.setattr(http_pool, "_SSL_KEY", None)
    monkeypatch.setattr(http_pool, "_SSL_CREATED", 0)
    first = http_pool.default_ssl_context()
    assert first.check_hostname and first.verify_mode == ssl.CERT_REQUIRED
    assert http_pool.default_ssl_context() is first
    monkeypatch.setenv("SSL_CERT_DIR", "nonexistent-test-ca-directory")
    second = http_pool.default_ssl_context()
    assert second is not first
    assert second.check_hostname and second.verify_mode == ssl.CERT_REQUIRED


def test_pool_has_bounded_idle_entries_and_expiry(monkeypatch):
    async def run():
        class Writer:
            closed = False
            def close(self):
                self.closed = True
        pool = http_pool._Pool(asyncio.get_running_loop())
        writers = []
        monkeypatch.setattr(http_pool, "IDLE_SECONDS", 0.01)
        for _ in range(20):
            writer = Writer()
            writers.append(writer)
            pool.put("one-origin", object(), writer)
        assert len(pool.idle) == http_pool.MAX_ORIGIN_IDLE
        await asyncio.sleep(0.03)
        assert not pool.idle and all(writer.closed for writer in writers)
    asyncio.run(run())


def test_stop_is_checked_even_before_pool_hit(monkeypatch):
    monkeypatch.setattr(http_pool, "_pool", lambda: pytest.fail("must not acquire after stop"))
    async def run():
        with pytest.raises(asyncio.CancelledError):
            await ChatLLM._open_connection("127.0.0.1", 1, False, 1, lambda: True)
    asyncio.run(run())


def test_https_reuse_requires_trusted_certificate_and_matching_host(tmp_path, monkeypatch):
    import datetime
    import ipaddress
    import ssl
    pytest.importorskip("cryptography")
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "local-only")])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False)
        .sign(key, hashes.SHA256()))
    cert_file, key_file = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()))
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert_file, key_file)
    monkeypatch.setattr(http_pool, "_SSL_CONTEXT", None)
    monkeypatch.setattr(http_pool, "_SSL_KEY", None)
    monkeypatch.setattr(http_pool, "_SSL_CREATED", 0)
    async def check(port, accepted):
        with pytest.raises(ssl.SSLCertVerificationError):
            await ChatLLM._open_connection("127.0.0.1", port, True, 2)
        monkeypatch.setenv("SSL_CERT_FILE", str(cert_file))
        with pytest.raises(ssl.SSLCertVerificationError):
            # Keep transport on the numeric loopback address; no DNS/IPv6
            # fallback timing should affect the certificate-name assertion.
            await asyncio.open_connection("127.0.0.1", port, ssl=http_pool.default_ssl_context(),
                                          server_hostname="mismatch.invalid", ssl_handshake_timeout=2)
        config = {"url": f"https://127.0.0.1:{port}/v1", "selected_model_id": "m"}
        for _ in range(2):
            result = [frame async for frame in ChatLLM.async_std_completions_sse(
                ChatLLMRequest(messages=[], timeout_read=1), model_config=config)]
            assert any('"content": "OK"' in frame for frame in result)
        assert len(accepted) == 1
    asyncio.run(_exercise(check, tls=tls))
