import socket
from urllib.request import Request

import pytest
from security import network


@pytest.mark.parametrize("url", ["http://127.0.0.1", "http://10.0.0.1", "http://169.254.169.254/latest/meta-data",
    "http://[::1]", "http://[::ffff:127.0.0.1]", "file:///etc/passwd", "http://user:pass@example.com/", "http://localhost/#fragment"])
def test_private_and_ambiguous_urls_are_rejected(url):
    with pytest.raises(ValueError):
        network.resolve_target(url)


def test_dns_mixed_public_private_fails_and_allowlist_is_exact_origin(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args: [(2, 1, 6, "", (ip, 80)) for ip in ("8.8.8.8", "127.0.0.1")])
    with pytest.raises(ValueError): network.resolve_target("http://example.com/")
    with pytest.raises(ValueError): network.resolve_target("http://127.0.0.1:81", allowed_origins=("http://127.0.0.1:80",))
    assert network.resolve_target("http://127.0.0.1:80", allowed_origins=("http://127.0.0.1",))[3] == "127.0.0.1"


def test_connection_uses_validated_address_and_redirect_is_not_followed(monkeypatch):
    connected, closed = [], []
    monkeypatch.setattr(network, "resolve_target", lambda *a, **kw: (__import__('urllib.parse', fromlist=['urlsplit']).urlsplit("http://example.com/path"), "example.com", 80, "8.8.8.8"))
    monkeypatch.setattr(network.http.client.HTTPConnection, "request", lambda self, *a, **kw: self.connect())
    monkeypatch.setattr(network.socket, "create_connection", lambda address, **kw: connected.append(address))
    class Response:
        status = 302
        def close(self): closed.append(True)
    monkeypatch.setattr(network.http.client.HTTPConnection, "getresponse", lambda _: Response())
    with pytest.raises(ValueError, match="跳转"):
        network.safe_urlopen(Request("http://example.com/path"))
    assert connected == [("8.8.8.8", 80)] and closed
