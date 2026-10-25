"""Pinned-address HTTP reads for untrusted preview/media URLs.

Proxy environment variables are deliberately not used. DNS validation and the
actual socket share the selected address; HTTPS still verifies the original host.
"""
import concurrent.futures
import http.client
import ipaddress
import socket
import threading
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

_DNS_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="safe-dns")
_DNS_SLOTS = threading.BoundedSemaphore(8)


def _public(address):
    address = ipaddress.ip_address(address)
    if getattr(address, "ipv4_mapped", None):
        address = address.ipv4_mapped
    return address.is_global and not (address.is_multicast or address.is_reserved or address.is_unspecified)


def _origin(parts):
    return (parts.scheme, (parts.hostname or "").casefold().rstrip("."), parts.port or (443 if parts.scheme == "https" else 80))


def resolve_target(url, *, allowed_origins=(), timeout=5):
    parts = urlsplit(url)
    if (parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password
            or parts.fragment or "%" in parts.hostname or any(ord(char) < 33 for char in url)):
        raise ValueError("媒体 URL 格式不受支持")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    if not 1 <= port <= 65535:
        raise ValueError("目标端口无效")
    allowed = {_origin(urlsplit(value)) for value in allowed_origins}
    private_allowed = _origin(parts) in allowed
    host = parts.hostname.encode("idna").decode()
    try:
        addresses = [str(ipaddress.ip_address(host))]
    except ValueError:
        if not _DNS_SLOTS.acquire(blocking=False):
            raise ValueError("网络解析繁忙，请稍后重试")
        future = _DNS_POOL.submit(socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM)
        future.add_done_callback(lambda _: _DNS_SLOTS.release())
        try:
            addresses = list(dict.fromkeys(item[4][0] for item in future.result(timeout=min(5, timeout))))
        except (OSError, concurrent.futures.TimeoutError) as exc:
            future.cancel()
            raise ValueError("目标地址解析失败或超时") from exc
    if not addresses or (not private_allowed and any(not _public(address) for address in addresses)):
        raise ValueError("不允许访问回环、私网、链路本地或云元数据地址")
    return parts, host, port, addresses[0]


class _Response:
    def __init__(self, response, connection, url):
        self.response, self.connection, self.url = response, connection, url
        self.headers, self.status = response.headers, response.status

    def read(self, *args):
        return self.response.read(*args)

    def getheader(self, *args):
        return self.response.getheader(*args)

    def geturl(self):
        return self.url

    def info(self):
        return self.headers

    def close(self):
        self.response.close()
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def safe_urlopen(request, *, timeout=30, allowed_origins=()):
    """No redirects: changed URLs require a fresh permission decision."""
    url = request.full_url if hasattr(request, "full_url") else str(request)
    parts, host, port, address = resolve_target(url, allowed_origins=allowed_origins, timeout=timeout)
    use_tls = parts.scheme == "https"
    class PinnedConnection(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.create_connection((address, port), timeout=self.timeout)
            if use_tls:
                from chat.http_pool import default_ssl_context
                self.sock = default_ssl_context().wrap_socket(self.sock, server_hostname=host)
    connection = PinnedConnection(host, port, timeout=timeout)
    headers = dict(request.header_items()) if hasattr(request, "header_items") else {}
    headers["Connection"] = "close"
    method = request.get_method() if hasattr(request, "get_method") else "GET"
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    try:
        connection.request(method, path, body=getattr(request, "data", None), headers=headers)
        response = connection.getresponse()
        if 300 <= response.status < 400:
            response.close()
            raise ValueError("媒体跳转需要对新地址重新授权")
        if response.status >= 400:
            status, reason = response.status, response.reason
            response.close()
            raise HTTPError(url, status, reason, {}, None)
        return _Response(response, connection, url)
    except BaseException:
        connection.close()
        raise


def account_urlopen(request, *, timeout=30):
    from security.principal import current
    principal = current()
    allowed = principal.store.settings.allowed_media_origins if principal else ()
    return safe_urlopen(request, timeout=timeout, allowed_origins=allowed)
