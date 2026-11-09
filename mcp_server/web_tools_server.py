# coding: utf-8
"""
MCP 网页工具服务（web_tools_server）。

注册工具（5 个）：
    fetch_url       抓取网页/HTTP 接口（纯文本提取、标签/正则抽取、gzip/deflate 自动解压、截断）
    web_search      网页搜索（Bing 主 + DuckDuckGo 回退合并、重定向解包、返回标题/链接/摘要）
    web_open        打开静态页：标题 + 正文 + 分页偏移 + 编号链接（供 web_click 跟进）
    web_click       按 web_open 的链接编号打开目标页（无状态：每次重新读取来源页解析编号）
    web_find        在静态正文中查找字面文本，只返回匹配行、字符偏移与上下文

实现说明：
    - 阻塞网络请求统一通过 asyncio.to_thread
      放到工作线程执行，避免卡死 stdio 事件循环；
    - 所有大输出统一截断保护，避免撑爆模型上下文；
    - 文本解码采用统一 BOM/严格 UTF-8/声明与本地编码回退，避免汉字评分误判。
"""
# 标准库
import asyncio
import base64
import json
import re
import sys
import zlib
from html import unescape
from html.parser import HTMLParser
from http.client import IncompleteRead
from pathlib import Path
from typing import Optional, Annotated, Literal, Union
from pydantic import Field
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, parse_qs, urljoin
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from util.text_encoding import decode_text
from util.tool_arguments import string_arg, int_arg, number_arg
from util.bounded_regex import compile_pattern
from factory.agent_runtime.tool_control import tool_deadline, remaining_seconds

# 第三方库
try:
    # mcp 2.x：FastMCP 更名为 MCPServer（API 兼容，平替改名）
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:
    # mcp 1.x：旧的 fastmcp 模块
    from mcp.server.fastmcp import FastMCP
# 创建 MCP 服务器实例
web_mcp_server = FastMCP("web-tools-server")


# ============================ 通用辅助 ============================

# 网页工具输出上限，防止超大结果撑爆模型上下文
_FETCH_URL_DEFAULT_CHARS = 8000
_WEB_SEARCH_MAX_RESULTS = 20

# 默认浏览器请求头：显著降低被目标站点直接拒绝的概率
_BROWSER_LIKE_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


# ============================ HTML 处理辅助 ============================
# 提取纯文本时整段跳过的标签
_SKIP_HTML_TAGS = {"script", "style", "noscript", "template", "svg", "iframe", "head", "select", "option"}
# 视为块级、转换行处理的标签
_BLOCK_HTML_TAGS = {
    "p", "div", "br", "hr", "li", "ul", "ol", "tr", "table", "thead", "tbody",
    "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "header", "footer",
    "pre", "blockquote", "dl", "dt", "dd", "form", "fieldset", "nav", "main",
    "aside", "figure", "figcaption", "address",
}


class _HtmlTextExtractor(HTMLParser):
    """把 HTML 抽成纯文本：跳过脚本/样式，块级标签转换行，保留图片 alt 说明。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._chunks: list = []

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_HTML_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "img":
            alt = dict(attrs).get("alt", "")
            if alt:
                self._chunks.append(f"[图片:{alt}]")
        if tag in _BLOCK_HTML_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_HTML_TAGS:
            if self._skip_depth:
                self._skip_depth -= 1
        elif not self._skip_depth and tag in _BLOCK_HTML_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data):
        if not self._skip_depth and data.strip():
            self._chunks.append(data)

    def get_text(self) -> str:
        text = "".join(self._chunks)
        lines = [re.sub(r"[ \t\u3000]+", " ", line).strip() for line in text.split("\n")]
        return "\n".join(line for line in lines if line)


class _LinkExtractor(HTMLParser):
    """按页面顺序提取可跟进的 HTTP(S) 链接及其可见文字。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self._current = None

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href", "").strip()
            if href and self._current is None:
                self._current = [href, []]

    def handle_data(self, data):
        if self._current:
            self._current[1].append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._current:
            href, chunks = self._current
            self.links.append((href, re.sub(r"\s+", " ", "".join(chunks)).strip()))
            self._current = None

    def close(self):
        super().close()
        # Malformed HTML may omit </a>; keep its link rather than silently losing it.
        if self._current:
            href, chunks = self._current
            self.links.append((href, re.sub(r"\s+", " ", "".join(chunks)).strip()))
            self._current = None


def _html_to_text(html_text: str) -> str:
    parser = _HtmlTextExtractor()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception:
        return html_text
    return parser.get_text()


def _extract_links(html_text: str, base_url: str, limit: int = 50) -> list:
    """Return deduplicated, safe-to-follow links as (absolute_url, label).

    仅保留 http(s) 链接；跳过带账号口令的链接（不让 urllib 隐式发送凭据）
    与指向本页自身的纯片段链接（跟进只会重新打开同一页）。
    """
    parser = _LinkExtractor()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception:
        return []
    try:
        base_nofrag = urlsplit(base_url)._replace(fragment="").geturl()
    except ValueError:
        base_nofrag = ""
    links, seen = [], set()
    for href, label in parser.links:
        try:
            url = urljoin(base_url, unescape(href).strip())
            parsed = urlsplit(url)
        except ValueError:
            continue
        if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
            continue
        # Avoid exposing credentials embedded in a page link or asking urllib to
        # send them implicitly. Such links are not useful for public page navigation.
        if parsed.username is not None or parsed.password is not None:
            continue
        normalized = parsed._replace(fragment="").geturl()
        if normalized in seen or (base_nofrag and normalized == base_nofrag):
            continue
        seen.add(normalized)
        links.append((url, label or parsed.hostname))
        if len(links) >= limit:
            break
    return links


# text 模式默认跳过的导航/页脚类标签：这些内容对"读网页正文"几乎没有信息量，
# 却经常占满 max_chars 截断预算，把真正有用的正文挤掉
_NOISE_TAG_NAMES = ("nav", "header", "footer", "aside", "noscript", "template")
# 常见正文容器 id/class 关键词（依次尝试，命中即整块抽取）
_MAIN_CONTENT_HINTS = (
    ("id", "content"), ("id", "main-content"), ("id", "main"),
    ("class", "main-content"), ("class", "post-content"), ("class", "article-content"),
    ("class", "markdown-body"), ("class", "article"), ("class", "content"),
)

# 疑似 JS 渲染应用的标记：静态正文为空且命中这些标记时，明确指引模型
# 改用浏览器工具（browser_navigate + browser_snapshot），而不是反复重试静态抓取。
_JS_APP_MARKERS = (
    "__NEXT_DATA__", "__NUXT__", "__APOLLO_STATE__", "react-root", "data-reactroot",
    "ng-app", "ng-version", "data-v-app", "/_next/static/", "/_nuxt/", "vite/client",
    "enable JavaScript", "启用JavaScript", "需要启用 JavaScript", "javascript required",
    "Please enable JS", "requires JavaScript",
)


def _detect_js_app(html_text: str) -> str:
    """Return the matched JS-app marker, or '' when no signal is found."""
    if not html_text:
        return ""
    lowered = html_text[:200000].lower()
    for marker in _JS_APP_MARKERS:
        if marker.lower() in lowered:
            return marker
    return ""


def _extract_main_text(html_text: str) -> tuple:
    """text 模式的正文优先提取：跳过导航噪音、优先取正文容器。

    返回 (文本, 提取模式说明)。策略：
      1) 剥掉 nav/header/footer/aside 等噪音块后整体转文本；
      2) 若存在常见正文容器（article / #content 等），改用容器内文本（更干净）；
      3) 容器文本过短（不足 200 字符且不超过全文）时回退方案 1，避免误伤小页面。
    """
    cleaned = html_text
    for tag_name in _NOISE_TAG_NAMES:
        cleaned = re.sub(
            rf"<{tag_name}\b[^>]*>.*?</{tag_name}\s*>",
            " ", cleaned, flags=re.IGNORECASE | re.DOTALL)
    full_text = _html_to_text(cleaned)
    container_blocks = _extract_tag_blocks(html_text, "article")
    used_hint = ""
    if not container_blocks:
        for key, hint in _MAIN_CONTENT_HINTS:
            spec = f"div#{hint}" if key == "id" else f"div.{hint}"
            try:
                candidate_blocks = _extract_tag_blocks(html_text, spec)
            except ValueError:
                continue
            if candidate_blocks:
                container_blocks = candidate_blocks
                used_hint = spec
                break
    if container_blocks:
        inner_text = "\n".join(_html_to_text(block) for block in container_blocks)
        if inner_text and len(inner_text) >= min(200, len(full_text)):
            label = f"正文容器 {used_hint}" if used_hint else "正文容器 article"
            return inner_text, label
    return full_text, "全文（已剥离导航噪音）"


def _strip_tags(fragment: str) -> str:
    """去掉片段内的 HTML 标签并压缩空白（用于标题/摘要）。"""
    text = re.sub(r"<[^>]+>", " ", fragment)
    return re.sub(r"\s+", " ", unescape(text)).strip()


_TAG_SPEC_PATTERN = re.compile(r"^([a-zA-Z][a-zA-Z0-9-]*)((?:[.#][\w-]+)*)$")


class _TagBlockParser(HTMLParser):
    """按真实元素边界抽取内部 HTML，脚本字符串不会成为伪元素。"""
    _void = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, text, selectors):
        super().__init__(convert_charrefs=False)
        self.text, self.selectors = text, selectors
        self.offsets = [0]
        for line in text.splitlines(keepends=True):
            self.offsets.append(self.offsets[-1] + len(line))
        self.stack, self.blocks = [], []

    def source_offset(self):
        line, column = self.getpos()
        return self.offsets[line - 1] + column

    def handle_starttag(self, tag, attrs):
        if tag in self._void:
            return
        values = dict(attrs)
        selected = any(tag == wanted_tag and all(
            expected in (values.get(key) or "").split() if key == "class"
            else expected == values.get(key) for key, expected in conditions)
            for wanted_tag, conditions in self.selectors)
        self.stack.append((tag, self.source_offset() + len(self.get_starttag_text()), selected))

    def handle_startendtag(self, tag, attrs):
        pass

    def close(self):
        super().close()
        for _, start, selected in self.stack:
            if selected:
                self.blocks.append((start, len(self.text), self.text[start:].strip()))
        self.stack.clear()

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                for _, start, selected in self.stack[index:]:
                    if selected:
                        end = self.source_offset()
                        self.blocks.append((start, end, self.text[start:end].strip()))
                del self.stack[index:]
                break


def _extract_tag_blocks(html_text: str, tag_spec: str) -> list:
    """Simple selector union in source order; overlapping selections appear once."""
    specs = tag_spec.split(',')
    if len(specs) > 20:
        raise ValueError("tag 最多接受 20 个逗号分隔的简单选择器")
    selectors = []
    for spec in specs:
        match = _TAG_SPEC_PATTERN.fullmatch(spec.strip())
        if not match:
            raise ValueError(f"tag 参数格式不合法: {spec}（示例：div、div#main、div.content、h2,p）")
        conditions = [("id" if part[0] == "#" else "class", part[1:])
                      for part in re.findall(r"[.#][\w-]+", match.group(2))]
        selectors.append((match.group(1).lower(), conditions))
    parser = _TagBlockParser(html_text, selectors)
    parser.feed(html_text)
    parser.close()
    blocks, covered_end = [], -1
    for start, end, block in sorted(parser.blocks, key=lambda item: (item[0], -item[1])):
        if start < covered_end:
            continue  # 父元素已含子元素正文，避免重叠选择重复输出。
        blocks.append(block)
        covered_end = end
    return blocks




_HTTP_MAX_TRANSFER_BYTES = 16 * 1024 * 1024
_HTTP_MAX_DECOMPRESSED_BYTES = 32 * 1024 * 1024


def _decompress_http_body(raw: bytes, wbits: int) -> bytes:
    decoder = zlib.decompressobj(wbits)
    result = decoder.decompress(raw, _HTTP_MAX_DECOMPRESSED_BYTES + 1)
    if len(result) > _HTTP_MAX_DECOMPRESSED_BYTES or decoder.unconsumed_tail:
        raise ValueError("网页解压后超过 32 MiB 上限")
    tail = decoder.flush(_HTTP_MAX_DECOMPRESSED_BYTES + 1 - len(result))
    if len(result) + len(tail) > _HTTP_MAX_DECOMPRESSED_BYTES:
        raise ValueError("网页解压后超过 32 MiB 上限")
    return result + tail


class _HttpResult(tuple):
    def __new__(cls, final_url, status, content_type, raw, headers, partial_note=""):
        result = super().__new__(cls, (final_url, status, content_type, raw))
        result.headers = headers
        result.partial_note = partial_note
        return result


def _dns_resolves(hostname: str) -> bool:
    """DNS 存活探针（3 秒上限，任何异常视为不可达）。

    某些网络会劫持 DNS 并在 TLS 阶段失败，错误表面是 SSL，根因是域名
    问题；SSL 失败时反查一次 DNS 以正确归类。仅失败路径调用。
    """
    import concurrent.futures as _futures
    import socket as _socket
    try:
        with _futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_socket.getaddrinfo, hostname, 443, 0, _socket.SOCK_STREAM)
            future.result(timeout=3)
        return True
    except Exception:
        return False


def _classify_url_error(exc: BaseException, url: str) -> str:
    """把 urlopen 异常归类为 DNS / 连接 / TLS / 超时四类中文说明。"""
    import socket as _socket
    import ssl as _ssl
    host = urlsplit(url).hostname or url
    root = getattr(exc, "reason", exc)
    if isinstance(root, _ssl.SSLCertVerificationError):
        return f"TLS 证书验证失败：{host}（{root}）；请检查系统时间/证书或更换来源"
    if isinstance(root, _ssl.SSLError):
        if not _dns_resolves(host):
            return f"DNS 解析失败：{host}；请检查域名拼写与网络（TLS 报错为表象）"
        return f"TLS 握手失败：{host}（{root}）"
    if isinstance(root, _socket.gaierror):
        return f"DNS 解析失败：{host}（{root}）；请检查域名拼写与网络"
    if isinstance(root, (_socket.timeout, TimeoutError)):
        return f"连接超时：{host}；可调大 timeout_seconds 重试"
    if isinstance(root, ConnectionRefusedError):
        return f"连接被拒绝：{host}；目标端口可能未监听"
    if isinstance(root, ConnectionResetError):
        return f"连接被重置：{host}；可能被中间设备拦截，可稍后重试"
    return f"请求失败：{url}（{root}）"


def _http_request(url: str, method: str = "GET", body: str = "", headers: Optional[dict] = None,
                  timeout_seconds: float = 30.0, allow_partial: bool = False) -> tuple:
    """执行 HTTP 请求，返回 (最终 URL, 状态码, Content-Type, 原始字节)。
    统一带浏览器头降低被站点拒绝的概率；显式 headers 合并到默认头之上。"""
    url = str(url).strip()
    if not re.match(r"^https?://", url, re.IGNORECASE):
        raise ValueError(f"仅支持 http/https 链接: {url}")
    merged_headers = dict(_BROWSER_LIKE_HEADERS)
    if headers:
        merged_headers.update({str(k): str(v) for k, v in headers.items()})
    data = body.encode("utf-8") if body else None
    if data is not None and method.upper() != "GET":
        merged_headers.setdefault("Content-Type", "application/json")
    # 主动协商压缩传输（仅当调用方未自带 Accept-Encoding 时），减少大页面流量与耗时
    if headers is None or not any(k.lower() == "accept-encoding" for k in (headers or {})):
        merged_headers.setdefault("Accept-Encoding", "gzip, deflate")
    request = Request(url, data=data, headers=merged_headers, method=method.upper())
    try:
        try:
            response = urlopen(request, timeout=remaining_seconds(timeout_seconds))
        except HTTPError as exc:
            response = exc  # HTTP 错误仍有可读正文，使用同样的大小与解压保护。
        except TimeoutError:
            raise  # 工具时限，不归类为网络错误
        with response:
            chunks = []
            transferred = 0
            partial_note = ""
            while transferred <= _HTTP_MAX_TRANSFER_BYTES:
                try:
                    remaining = remaining_seconds(timeout_seconds)
                    # urllib's timeout is otherwise an inactivity timeout. Refresh
                    # the socket budget and use read1 to check a slow stream promptly.
                    stream = getattr(response, "fp", None)
                    if stream is not None and hasattr(stream, "fp"):
                        stream = stream.fp
                    sock = getattr(getattr(stream, "raw", None), "_sock", None)
                    if sock is not None:
                        sock.settimeout(remaining)
                    read = getattr(response, "read1", response.read)
                    chunk = read(min(65536, _HTTP_MAX_TRANSFER_BYTES + 1 - transferred))
                except IncompleteRead as exc:
                    chunks.append(exc.partial)
                    transferred += len(exc.partial)
                    partial_note = "传输中断（IncompleteRead）；仅保留已收到内容，不能据此判断网页无匹配"
                    break
                except (OSError, TimeoutError) as exc:
                    if not transferred:
                        raise
                    partial_note = f"传输中断（{type(exc).__name__}）；仅保留已收到内容，不能据此判断网页无匹配"
                    break
                if not chunk:
                    break
                chunks.append(chunk)
                transferred += len(chunk)
            raw = b"".join(chunks)
            if len(raw) > _HTTP_MAX_TRANSFER_BYTES:
                raise ValueError("网页响应超过 16 MiB 下载上限")
            declared_length = response.headers.get("Content-Length", "")
            if not partial_note and str(declared_length).isdigit() and len(raw) < int(declared_length):
                partial_note = "响应短于 Content-Length；仅保留已收到内容，不能据此判断网页无匹配"
            if partial_note and not allow_partial:
                # Search engines must not silently treat an incomplete page as
                # complete. Only fetch_url opts into explicitly marked partials.
                raise ValueError("网页响应未完整接收：" + partial_note)
            content_type = response.headers.get("Content-Type", "")
            # 按响应头/魔数解压：gzip(1f 8b)、deflate(zlib 头 78 xx 或裸 deflate)
            encoding_header = (response.headers.get("Content-Encoding") or "").lower().strip()
            if encoding_header in ("gzip", "deflate", "x-gzip", "zlib") or (
                    not encoding_header and (raw[:2] == b"\x1f\x8b" or raw[:1] == b"\x78")):
                try:
                    if raw[:2] == b"\x1f\x8b":
                        raw = _decompress_http_body(raw, 16 + zlib.MAX_WBITS)
                    else:
                        try:
                            raw = _decompress_http_body(raw, zlib.MAX_WBITS)
                        except zlib.error:
                            raw = _decompress_http_body(raw, -zlib.MAX_WBITS)
                except (OSError, zlib.error):
                    pass  # 解压失败则按原样返回，交由上层按文本处理
            return _HttpResult(response.geturl(), response.status, content_type, raw, dict(response.headers), partial_note)
    except URLError as exc:
        raise ValueError(_classify_url_error(exc, url)) from exc
    except TimeoutError as exc:
        raise ValueError(f"请求超时: {url}") from exc


def _pick_web_charset(content_type: str, raw: bytes, encoding: str) -> str:
    """网页编码优先级：显式参数 > Content-Type > meta 标签 > utf-8。"""
    if encoding:
        return encoding
    match = re.search(r"charset=([\w-]+)", content_type or "", re.IGNORECASE)
    if match:
        return match.group(1)
    head = raw[:4096].decode("ascii", errors="ignore")
    meta = re.search(r"<meta[^>]+charset=[\"']?([\w-]+)", head, re.IGNORECASE)
    return meta.group(1) if meta else "utf-8"


# ============================ 网页工具实现 ============================
def _fetch_url_impl(url, mode, pattern, tag, max_chars, timeout_seconds,
                    encoding, method, body, headers, offset=0) -> str:
    # body 接受对象/数组：内部转 JSON（自然写法不再被 schema 拒绝）
    if isinstance(body, (dict, list)):
        try:
            body = json.dumps(body, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"body 无法序列化为 JSON：{exc}；请传字符串或可序列化对象")
    elif body is not None and not isinstance(body, str):
        raise ValueError("body 须为字符串或可 JSON 序列化的对象/数组")
    values = {"url": url, "mode": mode, "pattern": pattern, "tag": tag, "max_chars": max_chars,
              "timeout_seconds": timeout_seconds, "encoding": encoding, "method": method,
              "body": body or "", "offset": offset}
    for key, maximum in (("url", 8192), ("mode", 20), ("pattern", 8192), ("tag", 4096),
                         ("encoding", 64), ("method", 20), ("body", 2 * 1024 * 1024)):
        string_arg(values, key, max_length=maximum)
    timeout = number_arg(values, "timeout_seconds", 30, minimum=1, maximum=120)
    int_arg(values, "offset", 0)
    int_arg(values, "max_chars", 8000, maximum=100000)
    if offset and method.upper() not in {"GET", "HEAD"}:
        raise ValueError("offset 续读仅支持 GET/HEAD；本次请求未发送")
    if headers is not None and (not isinstance(headers, dict) or len(headers) > 64
            or any(not isinstance(k, str) or not isinstance(v, str) for k, v in headers.items())):
        raise ValueError("headers 必须是最多 64 项的字符串键值对象")
    with tool_deadline(timeout):
        return _fetch_url_checked(url, mode, pattern, tag, max_chars, timeout,
                                  encoding, method, body, headers, offset)


def _fetch_url_pdf_result(response, raw, max_chars, offset, method):
    """PDF 响应按逐页文本提取并参与常规字符分页；tag/regex/html 对 PDF 无意义。"""
    content, extraction, pdf_title = _extract_pdf_text(raw)
    limit = min(max(int(max_chars) if max_chars else _FETCH_URL_DEFAULT_CHARS, 200), 100000)
    original_len = len(content)
    start = min(int(offset), original_len)
    end = min(start + limit, original_len)
    final_url, status, content_type, _raw = response
    header = (f"[fetch_url] HTTP {status} | {str(final_url)}\n"
              f"[fetch_url] Content-Type: {content_type or '未知'} | 提取: {extraction}"
              f" | 原始 {len(raw)} 字节 | 提取后 {original_len} 字符 | 本次字符 [{start}, {end})")
    if pdf_title:
        header += f" | 标题: {pdf_title}"
    notes = []
    if getattr(response, "partial_note", ""):
        notes.append("部分响应：" + response.partial_note)
    if status >= 400:
        notes.append("HTTP 错误响应，以下为站点返回的错误正文，不代表抓取成功")
    if not original_len:
        notes.append("PDF 无可提取文本（可能是扫描版图片型 PDF）；图片型 PDF 暂不支持 OCR")
    if end < original_len:
        if method.upper() in {"GET", "HEAD"}:
            notes.append(f"内容超过 max_chars={limit} 已截断；next_offset={end}，用同一 URL 和 offset={end} 继续读取（每次重新请求）")
        else:
            notes.append(f"内容超过 max_chars={limit} 已截断；当前方法不支持 offset 续读")
    if offset >= original_len and offset:
        notes.append("offset 已达到或超过提取文本末尾；没有后续内容")
    if notes:
        header += "\n[fetch_url] " + "；".join(notes)
    return header + "\n" + (content[start:end] if content[start:end] else "（提取结果为空）")


def _fetch_url_checked(url, mode, pattern, tag, max_chars, timeout_seconds,
                       encoding, method, body, headers, offset):
    mode = (mode or "text").lower()
    if mode not in ("text", "html", "tag", "regex"):
        raise ValueError("mode 仅支持 text/html/tag/regex")
    if mode == "tag" and not (tag or "").strip():
        raise ValueError("mode=tag 时必须提供 tag 参数（示例：div、div#main、div.content）")
    if mode == "regex" and not (pattern or "").strip():
        raise ValueError("mode=regex 时必须提供 pattern 参数")
    regex = None
    offset = int(offset)
    if offset < 0:
        raise ValueError("offset 必须为非负字符偏移")
    if mode == "regex":
        regex = compile_pattern(pattern)
    timeout = timeout_seconds
    response = _http_request(
        url, method=method or "GET", body=body or "", headers=headers, timeout_seconds=timeout, allow_partial=True)
    final_url, status, content_type, raw = response
    if _is_pdf_response(content_type, raw):
        if mode != "text":
            raise ValueError("PDF 仅支持 mode=text 逐页文本提取")
        return _fetch_url_pdf_result(response, raw, max_chars, offset, method)
    declared = _pick_web_charset(content_type, raw, "")
    html_text, charset = decode_text(raw, encoding, candidates=(declared, 'gb18030', 'big5'))
    title = ""
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html_text, re.IGNORECASE | re.DOTALL)
    if title_match:
        title = _strip_tags(title_match.group(1))
    extract_note = ""
    regex_truncated = False
    if mode == "text":
        content, extract_note = _extract_main_text(html_text)
    elif mode == "html":
        content = html_text
    elif mode == "tag":
        blocks = _extract_tag_blocks(html_text, str(tag))
        content = "\n----\n".join(_html_to_text(block) for block in blocks)
    else:
        # regex 模式：单条匹配展示上限 = max_chars/4，钳制在 [200, 2000]（默认 8000 → 每条 2000 字符）
        per_match_cap = max(200, min(2000, (int(max_chars) if max_chars else _FETCH_URL_DEFAULT_CHARS) // 4))
        matches = []
        try:
            for found in regex.finditer(html_text, timeout=remaining_seconds(2)):
                piece = (" | ".join(g if g is not None else "" for g in found.groups())
                         if found.groups() else found.group(0))
                if len(piece) > per_match_cap:
                    regex_truncated = True
                matches.append(piece[:per_match_cap])
                if len(matches) >= 200:
                    regex_truncated = True
                    break
        except TimeoutError:
            regex_truncated = True
            extract_note = "正则执行超时，结果不完整"
        content = "\n".join(matches)
    limit = min(max(int(max_chars) if max_chars else _FETCH_URL_DEFAULT_CHARS, 200), 100000)
    original_len = len(content)
    start = min(offset, original_len)
    end = min(start + limit, original_len)
    content = content[start:end]
    header = (f"[fetch_url] HTTP {status} | {str(final_url)}\n"
              f"[fetch_url] Content-Type: {content_type or '未知'} | 声明/推断编码: {declared} | 实际编码: {charset}"
              f" | 原始 {len(raw)} 字节 | 提取后 {original_len} 字符 | 本次字符 [{start}, {end})")
    if extract_note:
        header += f" | 提取模式: {extract_note}"
    if title:
        header += f" | 标题: {title}"
    notes = []
    if getattr(response, "partial_note", ""):
        notes.append("部分响应：" + response.partial_note)
    if regex_truncated:
        notes.append(f"正则结果已截断：每条最多 {per_match_cap} 字符，最多 200 条；请收窄 pattern 或使用 tag/text")
    if status >= 400:
        notes.append("HTTP 错误响应，以下为站点返回的错误正文，不代表抓取成功")
    for key, value in getattr(response, "headers", {}).items():
        if key.lower() in ("retry-after", "location"):
            notes.append(f"{key}: {value}")
    if mode == "tag" and not original_len:
        notes.append("目标标签未匹配或内容为空；可用 mode=html 核对真实标签与 id/class（不执行 JavaScript）")
    if mode == "regex" and not original_len:
        notes.append("正则没有匹配内容；匹配对象为原始响应文本，可用 mode=html 核对")
    if mode == "text" and not original_len:
        hint = "没有提取到静态正文；页面可能依赖 JavaScript、需要登录或被站点拦截。此工具不执行 JavaScript，请用 mode=html/tag 核对或改用正文/下载链接"
        marker = _detect_js_app(html_text)
        if marker:
            hint += f"；检测到 JS 应用特征 {marker}，可改用 browser_navigate 打开 {final_url} 后用 browser_snapshot 读取"
        notes.append(hint)
    if offset >= original_len and offset:
        notes.append("offset 已达到或超过提取文本末尾；没有后续内容")
    if end < original_len:
        if method.upper() in {"GET", "HEAD"}:
            notes.append(f"内容超过 max_chars={limit} 已截断；next_offset={end}，用同一 URL/模式和 offset={end} 继续读取（每次重新请求）；也可用 pattern/tag 缩小范围")
        else:
            notes.append(f"内容超过 max_chars={limit} 已截断；当前方法不支持 offset 续读")
    if notes:
        header += "\n[fetch_url] " + "；".join(notes)
    return header + "\n" + (content if content else "（提取结果为空）")


def _extract_pdf_text(raw: bytes):
    """Extract per-page text from PDF bytes; returns (text, note, title).

    自包含实现（不依赖 factory.file_factory，保持 MCP 层轻量）。
    最多处理前 200 页；扫描版/无文本页返回空正文并附注原因。
    """
    try:
        import fitz
    except Exception:
        try:
            import pymupdf as fitz
        except Exception:
            return "", "PyMuPDF 未安装，无法解析 PDF", ""
    try:
        doc = fitz.open(stream=bytes(raw), filetype="pdf")
    except Exception as exc:
        return "", f"PDF 解析失败：{exc}", ""
    try:
        try:
            meta_title = ((doc.metadata or {}).get("title", "") or "").strip()
        except Exception:
            meta_title = ""
        parts = []
        for number, page in enumerate(doc, 1):
            if number > 200:
                parts.append(f"\n[第 {number} 页起共 {len(doc)} 页已省略；PDF 过长]")
                break
            try:
                text = (page.get_text() or "").strip()
            except Exception:
                text = ""
            if text:
                parts.append(f"--- 第 {number} 页 ---\n{text}")
        if not parts:
            return "", "PDF 无可提取文本（可能是扫描版图片型 PDF）", meta_title
        return "\n\n".join(parts), f"PDF逐页文本（{len(doc)} 页）", meta_title
    finally:
        try:
            doc.close()
        except Exception:
            pass


def _extract_pdf_links(raw: bytes, limit: int = 50) -> list:
    """Extract http(s) URI links embedded in PDF pages as (url, label)."""
    try:
        import fitz
    except Exception:
        try:
            import pymupdf as fitz
        except Exception:
            return []
    try:
        doc = fitz.open(stream=bytes(raw), filetype="pdf")
    except Exception:
        return []
    try:
        uri_kind = getattr(fitz, "LINK_URI", 1)
        links, seen = [], set()
        for number, page in enumerate(doc, 1):
            try:
                page_links = page.get_links() or []
            except Exception:
                continue
            for item in page_links:
                if not isinstance(item, dict) or item.get("kind") != uri_kind:
                    continue
                url = str(item.get("uri", "") or "").strip()
                try:
                    parsed = urlsplit(url)
                except ValueError:
                    continue
                if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
                    continue
                if parsed.username is not None or parsed.password is not None:
                    continue
                normalized = parsed._replace(fragment="").geturl()
                if normalized in seen:
                    continue
                seen.add(normalized)
                links.append((url, f"PDF第{number}页链接"))
                if len(links) >= limit:
                    return links
        return links
    finally:
        try:
            doc.close()
        except Exception:
            pass


def _is_pdf_response(content_type: str, raw: bytes) -> bool:
    return "pdf" in (content_type or "").lower() or bytes(raw[:5]) == b"%PDF-"


def _read_static_page(url: str, timeout_seconds: float = 30.0):
    """Fetch one static page/PDF and return response metadata, text and links."""
    response = _http_request(url, timeout_seconds=timeout_seconds, allow_partial=True)
    final_url, status, content_type, raw = response
    if _is_pdf_response(content_type, raw):
        content, extraction, pdf_title = _extract_pdf_text(raw)
        links = _extract_pdf_links(raw)
        return response, "", pdf_title, content, "application/pdf", "pdf-text", extraction, links
    declared = _pick_web_charset(content_type, raw, "")
    html_text, charset = decode_text(raw, candidates=(declared, "gb18030", "big5"))
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html_text, re.I | re.S)
    title = _strip_tags(title_match.group(1)) if title_match else ""
    content, extraction = _extract_main_text(html_text)
    links = _extract_links(html_text, str(final_url), 50)
    return response, html_text, title, content, declared, charset, extraction, links


def _web_open_impl(url, max_chars=8000, offset=0, max_links=20, timeout_seconds=30):
    values = {"url": url, "max_chars": max_chars, "offset": offset,
              "max_links": max_links, "timeout_seconds": timeout_seconds}
    string_arg(values, "url", required=True, max_length=8192)
    limit = int_arg(values, "max_chars", 8000, maximum=100000)
    start = int_arg(values, "offset", 0)
    link_limit = int_arg(values, "max_links", 20, minimum=0, maximum=50)
    timeout = number_arg(values, "timeout_seconds", 30, minimum=1, maximum=120)
    if start < 0:
        raise ValueError("offset 必须为非负字符偏移")
    with tool_deadline(timeout):
        response, html_text, title, content, declared, charset, extraction, page_links = _read_static_page(url, timeout)
    limit = min(max(limit or _FETCH_URL_DEFAULT_CHARS, 200), 100000)
    start = min(start, len(content))
    end = min(start + limit, len(content))
    final_url, status, content_type, raw = response
    heading = f"[web_open] HTTP {status} | {final_url}"
    if title:
        heading += f"\n[web_open] 标题: {title}"
    heading += (f"\n[web_open] Content-Type: {content_type or '未知'} | 编码: {charset}"
                f" | 提取: {extraction} | 正文 {len(content)} 字符 | 本次 [{start}, {end})")
    notes = []
    if getattr(response, "partial_note", ""):
        notes.append("部分响应：" + response.partial_note)
    if status >= 400:
        notes.append("HTTP 错误响应；正文不代表抓取成功")
    if end < len(content):
        notes.append(f"正文已截断；使用相同 URL 和 offset={end} 继续读取")
    if notes:
        heading += "\n[web_open] 提示: " + "；".join(notes)
    body = content[start:end]
    if not body:
        marker = _detect_js_app(html_text)
        if marker:
            body = (f"（没有提取到静态正文；检测到 JS 应用特征 {marker}，"
                    f"请用 browser_navigate 打开 {final_url} 后用 browser_snapshot 读取渲染后内容）")
        else:
            body = "（没有提取到静态正文；页面可能依赖 JavaScript 或登录）"
    lines = [heading, body]
    links = page_links[:link_limit]
    if links:
        lines.append(f"[web_open] 可跟进链接（编号只对本页有效，最多显示 {link_limit} 条）：")
        lines.extend(f"  {index}. {label[:160]} — {link}" for index, (link, label) in enumerate(links, 1))
    elif link_limit:
        lines.append("[web_open] 未提取到可跟进的 HTTP(S) 链接")
    return "\n".join(lines)


def _web_find_impl(url, query, context_lines=2, max_matches=10, timeout_seconds=30):
    values = {"url": url, "query": query, "context_lines": context_lines,
              "max_matches": max_matches, "timeout_seconds": timeout_seconds}
    string_arg(values, "url", required=True, max_length=8192)
    needle = string_arg(values, "query", required=True, max_length=1024).strip()
    if not needle:
        raise ValueError("query 不能为空")
    context = int_arg(values, "context_lines", 2, minimum=0, maximum=10)
    match_limit = int_arg(values, "max_matches", 10, minimum=1, maximum=50)
    timeout = number_arg(values, "timeout_seconds", 30, minimum=1, maximum=120)
    with tool_deadline(timeout):
        response, _html, title, content, _declared, _charset, _extraction, _links = _read_static_page(url, timeout)
    lines = content.splitlines()
    # 每行起始字符偏移：与 web_open 的 offset 同口径，模型可直接跳转续读。
    line_starts, position = [], 0
    for raw_line in content.splitlines(keepends=True):
        line_starts.append(position)
        position += len(raw_line)
    folded = needle.casefold()
    matches = [index for index, line in enumerate(lines) if folded in line.casefold()]
    if not matches:
        return (f"[web_find] 未找到字面文本 {needle!r} | HTTP {response[1]} | {response[0]}\n"
                "这只表示提取到的静态文本中没有匹配；不代表动态渲染内容或完整网页中不存在。")
    output = [f"[web_find] {len(matches)} 处匹配（显示最多 {match_limit}） | HTTP {response[1]} | {response[0]}"]
    if title:
        output.append(f"标题: {title}")
    shown = matches[:match_limit]
    emitted = set()
    for match_number, index in enumerate(shown, 1):
        lo, hi = max(0, index - context), min(len(lines), index + context + 1)
        output.append(f"\n匹配 {match_number}（第 {index + 1} 行，offset={line_starts[index]}）：")
        for line_index in range(lo, hi):
            if line_index in emitted:
                continue
            emitted.add(line_index)
            line = lines[line_index]
            output.append(f"{line_index + 1}: {line[:1000]}" + (" …[行截断]" if len(line) > 1000 else ""))
    if len(matches) > match_limit:
        output.append(f"\n还有 {len(matches) - match_limit} 处未显示；可缩小查询词或改用 web_open 分页查看。")
    result = "\n".join(output)
    if len(result) > 16000:
        result = result[:16000].rstrip() + "\n[web_find] 输出到 16000 字符上限；请缩小 query 或减少 context_lines。"
    return result


def _web_click_impl(url, link_id, max_chars=8000, offset=0, max_links=20, timeout_seconds=45):
    values = {"url": url, "link_id": link_id, "max_chars": max_chars,
              "offset": offset, "max_links": max_links, "timeout_seconds": timeout_seconds}
    string_arg(values, "url", required=True, max_length=8192)
    index = int_arg(values, "link_id", None, minimum=1, maximum=50)
    limit = int_arg(values, "max_chars", 8000, maximum=100000)
    start = int_arg(values, "offset", 0)
    link_limit = int_arg(values, "max_links", 20, minimum=0, maximum=50)
    timeout = number_arg(values, "timeout_seconds", 45, minimum=1, maximum=120)
    if start < 0:
        raise ValueError("offset 必须为非负字符偏移")
    with tool_deadline(timeout):
        response, _html, _title, _content, _declared, _charset, _extraction, links = _read_static_page(url, timeout)
        if index > len(links):
            raise ValueError(f"链接编号 {index} 不存在；页面当前只有 {len(links)} 个可跟进链接，请重新 web_open")
        target, label = links[index - 1]
        # 剩余预算可能不足 1 秒；钳制到 1 秒以通过参数校验，嵌套 deadline 仍取
        # 外层最小值，实际超时行为不变（到期由 check_tool_control 抛出）。
        opened = _web_open_impl(target, limit, start, link_limit, max(1.0, remaining_seconds(timeout)))
    return f"[web_click] 跟进第 {index} 个链接：{label}\n{opened}"


def _unwrap_bing_redirect(url: str) -> str:
    """还原被 Bing 点击跟踪重定向（bing.com/ck/a）包装的真实目标链接。

    重定向形如 https://www.bing.com/ck/a?!&&p=...&u=a1<base64url编码的目标地址>&ntb=1，
    其中 u 参数去掉 a1 前缀后是 URL 安全 base64；解不开时退回剥掉查询串的原始链接。
    """
    if not re.search(r"bing\.com/ck/a", url, re.IGNORECASE):
        return url
    token_match = re.search(r"[?&]u=a1([^&]+)", url)
    if token_match:
        token = token_match.group(1)
        try:
            decoded = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode(
                "utf-8", errors="replace")
            if decoded.startswith("http"):
                return decoded
        except Exception:
            pass
    return url.split("&", 1)[0].split("?", 1)[0]


def _split_bing_blocks(html_text: str) -> list:
    """切出每个结果块（<li class="b_algo">）的内部 HTML。

    用前瞻定位每个 b_algo 起点、以下一个起点为界，避免经典非贪婪
    .*?</li> 写法在块内嵌套列表时提前截断的问题。
    """
    starts = [found.start() for found in re.finditer(r'<li class="b_algo', html_text, re.IGNORECASE)]
    blocks = []
    for index, start in enumerate(starts):
        seg_end = starts[index + 1] if index + 1 < len(starts) else len(html_text)
        blocks.append(html_text[start:seg_end])
    return blocks


def _first_p_snippet(block: str) -> str:
    """从结果块提取摘要：优先官方摘要容器 .b_caption p，回退到块内任意 p。"""
    match = re.search(r'<div class="b_caption[^"]*"[^>]*>.*?<p[^>]*>(.*?)</p>',
                      block, re.IGNORECASE | re.DOTALL)
    if not match:
        match = re.search(r'<p[^>]*>(.*?)</p>', block, re.IGNORECASE | re.DOTALL)
    return _strip_tags(match.group(1))[:300] if match else ""


def _bing_block_result(block: str) -> Optional[tuple]:
    """从单个 b_algo 块解析 (标题, 链接, 摘要)；广告位或无有效链接时返回 None。"""
    head = block[:2000].lower()
    if 'class="b_ad' in head or '"b_prom"' in head or "data-partnertag" in head:
        return None  # 跳过广告/推广卡片
    link = None
    match = re.search(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
                      block, re.IGNORECASE | re.DOTALL)
    if match:
        link = (match.group(1), match.group(2))
    else:
        # 视频卡/知识卡等无 h2 的特殊块：回退到第一个带可见文字的链接
        for anchor in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
                                  block, re.IGNORECASE | re.DOTALL):
            if _strip_tags(anchor.group(2)):
                link = (anchor.group(1), anchor.group(2))
                break
    if not link:
        return None
    url = _unwrap_bing_redirect(unescape(link[0]).strip())
    title = _strip_tags(link[1])
    if not url.startswith("http") or not title:
        return None
    if _is_junk_title(title):
        return None
    return title, url, _first_p_snippet(block)


def _search_keywords(query: str) -> set:
    """从查询中提取关键词集合：整词 + 英文/数字分词（≥2 字符）。"""
    query = re.sub(r"\bsite:[^\s]+", "", str(query or ""), flags=re.IGNORECASE)
    keywords = set()
    for token in re.split(r"[\s,，、]+", str(query or "").strip()):
        if not token:
            continue
        keywords.add(token.lower())
        keywords.update(
            part.lower() for part in re.findall(r"[A-Za-z0-9]+", token) if len(part) >= 2
        )
    return keywords


def _search_numeric_entities(query: str) -> set:
    """Keep versions/years intact instead of treating 3.13 as the common token 13."""
    query = re.sub(r"\bsite:[^\s]+", "", str(query or ""), flags=re.I)
    return set(re.findall(r"(?<![\d.])(?:\d+(?:\.\d+)+|(?:19|20)\d{2})(?![\d.])", query))


def _entity_in_text(entity: str, text: str) -> bool:
    # 右边界只禁数字不禁点：查询 3.14 应当命中页面里的 "3.14.0"（版本细版号），
    # 但仍不应命中 "3.145" 或 "3.1415"。左边界保持禁数字+点，避免 13.14 误命中。
    return bool(re.search(r"(?<![\d.])" + re.escape(entity) + r"(?!\d)", text))


def _query_terms(query: str) -> set:
    """细粒度查询词项：整词 + 中文 2/3 字滑窗 + 英文数字分词。

    搜索引擎会对中文分词：「小学数学教师资格证面试」这种整串在结果页里
    几乎不会逐字出现，只用整词匹配会把「小学（中国传统语文学）」这种
    泛化页当成唯一命中，或干脆把整池候选判成无关。补上 2/3 字窗口后，
    「小学数学」「教师资格」「面试」「真题」这类真实片段才能参与打分。
    """
    text = re.sub(r"\bsite:[^\s]+", " ", str(query or ""), flags=re.IGNORECASE)
    terms = set()
    for token in re.split(r"[\s,，、;；|/]+", text.strip()):
        if not token:
            continue
        terms.add(token.lower())
        # 中文连续片段：2 字窗口（3 字以上片段再补 3 字窗口，提高区分度）
        for run in re.findall(r"[\u4e00-\u9fff]+", token):
            for size in ((2, 3) if len(run) >= 3 else (2,)):
                if len(run) < size:
                    continue
                for index in range(len(run) - size + 1):
                    terms.add(run[index:index + size])
        terms.update(part.lower() for part in re.findall(r"[A-Za-z0-9]+", token) if len(part) >= 2)
    return {term for term in terms if term}


def _result_matches_query(item: tuple, query: str, min_hits: Optional[int] = None) -> bool:
    """判断单条候选是否与查询相关：整词命中直接通过，否则要求细粒度词项命中数达标。

    只要求「任意整词出现」会把 2 字以上的中文整串当成唯一合法证据；
    只用细粒度词项又会被「小学」这类高频泛词淹没。两者结合：整词命中
    直接收下，否则按词项命中计数设阈值。
    """
    # 匹配必须用 _match_text（去掉 URL 查询串）：搜索页会把查询词回显进链接
    # 参数（`?word=关键词`），直接用完整 URL 会让任何站内导航链接都「命中」。
    haystack = _match_text(item)
    whole_words = [word for word in _search_keywords(query) if len(word) >= 2]
    if any(word in haystack for word in whole_words):
        return True
    terms = _query_terms(query)
    if not terms:
        return True
    hits = sum(1 for term in terms if term in haystack)
    threshold = min_hits if min_hits is not None else (2 if len(terms) <= 6 else 3)
    return hits >= threshold


def _is_cjk_query(query: str) -> bool:
    """中文为主的查询：这类查询在 Bing 网页版上常被拆成单字泛词，需换中文引擎。"""
    text = re.sub(r"\bsite:[^\s]+", " ", str(query or ""))
    compact = re.sub(r"\s+", "", text)
    if not compact:
        return False
    cjk = len(re.findall(r"[\u4e00-\u9fff]", compact))
    return cjk >= 2 and cjk * 3 >= len(compact)


# 英文里几乎不携带区分度的词：参与「内容词命中数」判断会把泛化页面判成相关
_EN_STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "you", "your", "are", "was",
    "how", "what", "why", "when", "where", "which", "who", "can", "could", "should",
    "about", "into", "than", "then", "there", "their", "them", "but", "not", "all",
    "new", "news", "best", "top", "guide", "tutorial", "example", "examples", "vs",
    # URL 结构性词：几乎每个链接都含，命中它们毫无区分度
    "http", "https", "www", "html", "htm", "index", "com", "cn", "org", "net",
}


def _en_content_words(query: str) -> list:
    """查询里的英文/数字内容词（去停用词、去重、保持原顺序）。"""
    cleaned = re.sub(r"\bsite:[^\s]+", " ", str(query or ""), flags=re.IGNORECASE)
    words = [w.lower() for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9.+#-]*", cleaned)]
    ordered = []
    for word in words:
        if len(word) >= 2 and word not in _EN_STOPWORDS and word not in ordered:
            ordered.append(word)
    return ordered


def _match_text(item: tuple) -> str:
    """用于相关性判断的匹配文本：标题 + 摘要 + 干净 URL。

    为什么不能直接用完整 URL：搜索引擎会把查询词回显进链接参数（360 的
    `?word=关键词`、bing 的 `?q=`），URL 里的 "https" 也会命中查询词 "http"。
    实测这会让「登录/注册/图片」这类站内导航链接凭 URL 冒充相关结果。
    因此只保留主机名（去掉 www.）与路径。
    """
    try:
        parts = urlsplit(str(item[1]))
        host = (parts.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        clean_url = host + parts.path
    except ValueError:
        clean_url = str(item[1])
    return f"{item[0]} {item[2]} {clean_url}".lower()


def _query_cjk_runs(query: str) -> list:
    """查询里的中文连续片段（已去掉 site: 与引号），按长度降序。"""
    text = re.sub(r"\bsite:[^\s]+", " ", str(query or ""), flags=re.IGNORECASE)
    runs = [run for run in re.findall(r"[\u4e00-\u9fff]+", text) if len(run) >= 2]
    return sorted(set(runs), key=len, reverse=True)


def _cjk_overlap_len(query: str, text: str) -> int:
    """查询与文本之间最长的「中文连续片段命中长度」。

    为什么不用整词包含：搜索引擎会给中文做分词和同义扩写，结果标题常写成
    「小学数学教资面试历年真题」。「小学数学教师资格证面试」整串永不相等，
    但共享 6 字长的「面试历年真题」这类片段，足以证明相关；而「中小学教材
    电子版」只共享「小学」2 字，应当被判定为弱相关。用最长公共连续片段长度
    正好能区分这两类，且不必引入分词词典。
    """
    if not text:
        return 0
    for run in _query_cjk_runs(query):
        # 从长到短探测窗口；上限 8 字已足以判定「强相关」，避免无意义的大窗口开销
        for size in range(min(len(run), 8), 1, -1):
            for index in range(len(run) - size + 1):
                if run[index:index + size] in text:
                    return size
    return 0


def _en_content_hits(query: str, text: str) -> int:
    """查询中命中的英文/数字内容词个数（去掉停用词与 1 字符词）。"""
    return sum(1 for word in _en_content_words(query) if word in text)


def _cjk_gram_hits(query: str, text: str) -> int:
    """命中查询中「不同中文 2 字窗口」的个数。

    「小学」这类两字高频词几乎出现在所有教育类页面（百科词条、教材站、
    杂志社首页），单独命中它不足以证明相关；而「数学」+「面试」或
    「小学」+「数学」两个不同窗口同时出现，才说明内容确实围绕该主题。
    """
    if not text:
        return 0
    grams = set()
    for run in _query_cjk_runs(query):
        for index in range(len(run) - 1):
            grams.add(run[index:index + 2])
    return sum(1 for gram in grams if gram in text)


def _relevance_grade(item: tuple, query: str) -> int:
    """单条结果与查询的相关度：2=强相关，1=弱相关（泛化/部分命中），0=不相关。

    规则（按优先级）：
      1) 查询含版本号/年份等数字约束时，结果必须包含数字约束才可能强相关；
      2) 中文最长命中 >=4 字 → 强相关；=3 字 → 弱相关；=2 字（多半就是
         「小学」这类泛词）要求同时命中 2 个不同的 2 字窗口才算弱相关；
      3) 英文/数字内容词命中 >=2 个 → 强相关；只有 1 个内容词的查询命中即
         强相关（单词查询本身就是精确匹配）；否则 1 个 → 弱相关。

    第 2 条里的「2 字窗口计数」是本次故障的关键修正：原实现只要整词或
    任意关键词出现就算命中，于是「小学（中国传统语文学）_百度百科」这种
    只含「小学」的页面被判定为有效结果，挤掉了真正的真题页面。
    """
    text = _match_text(item)
    entities = _search_numeric_entities(query)
    entity_ok = all(_entity_in_text(entity, text) for entity in entities) if entities else True
    cjk_len = _cjk_overlap_len(query, text)
    gram_hits = _cjk_gram_hits(query, text)
    en_words = _en_content_words(query)
    en_hits = sum(1 for word in en_words if word in text)
    # 单一英文内容词且无中文词：命中即强相关（"asdfghjkl"、"taskgroup" 这类
    # 精确查询不该被降级成弱相关）
    single_term_exact = len(en_words) == 1 and en_hits == 1 and not _query_cjk_runs(query)
    if not entity_ok:
        # 数字约束不满足：最多算弱相关，绝不能伪装成命中
        return 1 if (cjk_len >= 3 or gram_hits >= 2 or en_hits >= 1) else 0
    if cjk_len >= 4:
        return 2
    if single_term_exact:
        return 2
    if en_hits >= 2:
        return 2
    if cjk_len >= 3 or gram_hits >= 2 or en_hits >= 1:
        return 1
    return 0


def _pool_is_good(pool: list, query: str, want: int = 5) -> bool:
    """候选池是否已足够好：强相关结果达到 want 条即可停止补引擎。

    判据必须用「强相关」而不是「命中任意词」：Bing 对
    "python 3.14 new features" 会返回一堆只含 "Python" 的入门教程页，按任意词
    判定就成了「够好」，多引擎补充会被直接跳过，用户永远拿不到含 3.14 的
    release 页——这正是本次故障的另一半原因。

    也不用「强相关占比过半」：合并两三个引擎后池子里必然混入大量弱相关候选，
    按比例判定会永远不达标，于是每次都把 DDG 的两个超时窗口拖满。
    """
    if not pool:
        return False
    good = sum(1 for item in pool if _relevance_grade(item, query) >= 2)
    return good >= want


def _keyword_score(item: tuple, keywords: set) -> float:
    """单条结果的相关性得分：标题命中权重最高，摘要次之，URL 再次；
    文档/参考类路径（docs、changelog、whatsnew 等）小幅加权。

    关键词除了整词，还会展开成中文 2/3 字窗口（见 _query_terms）：中文查询的
    整串几乎不可能在结果里原样出现，只按整词打分会让所有结果同为 0 分。
    """
    title, url, snippet = str(item[0]), str(item[1]), str(item[2])
    # URL 只取主机名+路径：查询串里的回显词不是网页内容，参与打分会让
    # 「登录/注册」这类站内链接凭参数冒充高分结果。
    clean_url = _match_text(("", url, ""))
    hay_title, hay_snippet, hay_url = title.lower(), snippet.lower(), clean_url
    s = 0.0
    for keyword in keywords:
        if keyword in hay_title:
            s += 3.0
        if keyword in hay_snippet:
            s += 1.5
        if keyword in hay_url:
            s += 1.0
    if re.search(r"(docs?|developer|guide|reference|changelog|whatsnew|release)", hay_url):
        s += 0.8
    return s


def _relevance_rank(results: list, query: str, limit: int) -> list:
    """把去重后的候选结果按与 query 的相关性重排后截断到 limit 条。

    以原始顺序作为稳定平手依据，避免无关键词时乱序。
    """
    keywords = _query_terms(query) or _search_keywords(query)
    entities = _search_numeric_entities(query)
    def score(item):
        # 相关度等级（强/弱/不相关）优先，其次精确版本/年份覆盖，再按标题/摘要/
        # URL 关键词评分，最后保留搜索引擎顺序作为平手依据。
        text = _match_text(item)
        return (_relevance_grade(item, query),
                sum(_entity_in_text(entity, text) for entity in entities),
                _keyword_score(item, keywords))
    ranked = sorted(
        enumerate(results),
        key=lambda pair: (*(-value for value in score(pair[1])), pair[0]))
    return [item for _, item in ranked[:limit]]


def _dedupe_results(results: list, limit: int) -> list:
    """按 URL 去重（忽略结尾斜杠），保持原有顺序，截断到 limit 条。"""
    seen = set()
    unique = []
    for item in results:
        key = item[1].rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
        if len(unique) >= limit:
            break
    return unique


def _merge_pools(pools: list, cap: int) -> list:
    """把多个引擎的候选池按尝试顺序拼接后去重（不看引擎内部评分，统一交给重排）。"""
    merged = []
    for _name, pool in pools:
        merged.extend(pool)
    return _dedupe_results(merged, max(1, cap))


def _generic_link_fallback(html_text: str, base_url: str, query: str, limit: int) -> list:
    """搜索引擎改版导致结构化解析失效时的最后兜底：从页面提取全部外部链接。

    只保留命中查询关键词的站外链接并按相关性重排；无命中时返回空列表，
    不伪造结果。调用方必须标注“非结构化兜底”以示可靠性较低。
    """
    try:
        base_host = (urlsplit(base_url).hostname or "").lower()
    except ValueError:
        base_host = ""
    keywords = _search_keywords(query)
    scored = []
    for url, label in _extract_links(html_text, base_url, 50):
        try:
            host = (urlsplit(url).hostname or "").lower()
        except ValueError:
            continue
        if _is_engine_own_host(host, base_host):
            # 翻页/导航/登录注册/图片/百科 tab 等引擎站内链接对搜索无用。
            # 实测兜底路径会把「登录」「注册」「图片」当结果返回（它们的 URL
            # 参数里带着查询词），这类假结果比空结果误导性更强。
            continue
        if _is_nav_link(label):
            continue
        # 兜底场景下链接只有锚文字，放宽到命中 1 个细粒度词项即可
        candidate = (label or url, url, "")
        if not _result_matches_query(candidate, query, min_hits=1):
            continue
        scored.append(candidate)
    if not scored:
        return []
    return _relevance_rank(_dedupe_results(scored, limit * 3), query, limit)


# 搜索页上的站内导航/工具入口（登录、注册、图片、视频等 tab）。兜底提取时
# 这些链接的文字很短、URL 参数里又回显查询词，极易被当成结果；但 wenku.so.com
# 这类「引擎自营内容页」是真实结果，不能按域名一刀切，所以只按锚文字排除。
_NAV_LINK_LABELS = {
    "百科", "登录", "注册", "图片", "视频", "地图", "新闻", "音乐", "翻译",
    "更多", "首页", "设置", "反馈", "帮助", "ai问答", "ai 问答", "文库首页",
}


def _is_engine_own_host(host: str, base_host: str = "") -> bool:
    """链接主机是否就是搜索页自身（同站点即视为站内导航）。"""
    host = (host or "").lower()
    if not base_host:
        return False
    base = base_host.lower()
    if base.startswith("www."):
        base = base[4:]
    return host == base or host.endswith("." + base)


def _is_nav_link(label: str) -> bool:
    """锚文字是否是纯导航入口（登录/注册/图片/AI问答……）。"""
    text = re.sub(r"[\s_\-|·]+", "", str(label or "")).lower()
    return text in _NAV_LINK_LABELS or (len(text) <= 4 and text in _NAV_LINK_LABELS)


def _is_junk_title(title: str) -> bool:
    """标题是否是不含信息的残片（"The"、"阅读"、"首页"、"…" 这类）。

    搜索引擎的侧栏/相关推荐块常被误当成结果块，切出来的标题只有停用词或
    单个短词。这类条目的链接通常是站内页，返回给模型只会浪费一次抓取。
    """
    text = re.sub(r"\s+", " ", str(title or "")).strip()
    if len(text) < 2:
        return True
    # 纯标点/省略号
    if not re.search(r"[\w\u4e00-\u9fff]", text):
        return True
    lowered = text.lower()
    if _is_nav_link(text):
        return True
    # 只有 1 个英文词且是停用词（"The" / "Read more"）
    words = re.findall(r"[A-Za-z]+", lowered)
    if len(words) == 1 and len(re.findall(r"[\u4e00-\u9fff]", text)) == 0:
        if words[0] in _EN_STOPWORDS or len(words[0]) <= 3:
            return True
    # 英文停用词开头且总长很短（"The"、"Read more"、"Click here"）
    if len(text) <= 12 and words and words[0] in _EN_STOPWORDS and not re.search(r"[\u4e00-\u9fff]", text):
        return True
    return False


def _fetch_bing_results(query: str, limit: int) -> tuple:
    """请求并解析 Bing 网页版搜索结果，返回 ([(标题, 链接, 摘要)], 是否走兜底)。

    多抓 3 倍候选后按相关性重排截断：网页版结果常混入首页/下载页等
    泛化条目，直接取前 N 条会挤掉真正相关的文档链接。
    """
    search_url = "https://www.bing.com/search?" + urlencode(
        {"q": query, "count": min(limit * 3, 30), "mkt": "zh-CN"})
    _, status, content_type, raw = _http_request(search_url, timeout_seconds=15)
    if status >= 400:
        raise ValueError(f"Bing HTTP {status}")
    charset = _pick_web_charset(content_type, raw, "")
    html_text, charset = decode_text(raw, candidates=(charset,))
    collected = []
    for block in _split_bing_blocks(html_text):
        item = _bing_block_result(block)
        if item:
            collected.append(item)
        if len(collected) >= limit * 3:
            break
    if not collected:
        # 结构化块一个都切不出来：页面很可能已改版，用非结构化兜底代替直接判空。
        rescued = _generic_link_fallback(html_text, search_url, query, limit * 3)
        return rescued, bool(rescued)
    # 返回完整排序池（≤3×limit），由调用方决定展示条数，便于多引擎合并
    return _relevance_rank(_dedupe_results(collected, limit * 3), query, limit * 3), False


class _DdgResultParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results, self.current, self.anchor = [], None, None

    def handle_starttag(self, tag, attrs):
        if tag not in ("a", "td"):
            return
        attrs = dict(attrs)
        classes = (attrs.get("class") or "").split()
        kind = ("title" if tag == "a" and ("result__a" in classes or "result-link" in classes)
                else "snippet" if "result__snippet" in classes or "result-snippet" in classes else None)
        if kind:
            self.anchor = [kind, attrs.get("href") or "", [], tag]

    def handle_data(self, data):
        if self.anchor:
            self.anchor[2].append(data)

    def handle_endtag(self, tag):
        if not self.anchor or tag != self.anchor[3]:
            return
        kind, href, chunks, _tag = self.anchor
        self.anchor = None
        if kind == "title":
            if self.current:
                self.results.append(tuple(self.current))
            url = urljoin("https://duckduckgo.com", href)
            target = parse_qs(urlsplit(url).query).get("uddg")
            if target:
                url = target[0]
            self.current = ["".join(chunks).strip(), url, ""] if urlsplit(url).scheme in ("http", "https") else None
        elif self.current:
            self.current[2] = "".join(chunks).strip()[:300]


# DuckDuckGo 熔断器：连续失败后一段时间内跳过尝试（每次尝试最多浪费
# 2×15s），auto 模式直接用 Bing 结果并在诊断里说明；成功一次即复位。
_DDG_BREAKER_FAILS = 0
_DDG_BREAKER_SKIP_UNTIL = 0.0
_DDG_BREAKER_THRESHOLD = 2
_DDG_BREAKER_COOLDOWN_SECONDS = 1800
# 网络级失败（连不上/超时）单独计：本机实测 DDG 两个端点都连不通，
# 每次尝试白等 2×8s，还会让 web_search 逼近 45s 总预算。
_DDG_NET_FAILS = 0
_DDG_NET_SKIP_UNTIL = 0.0


def _is_network_level_failure(exc: Exception) -> bool:
    """是否为网络层失败（DNS/连接/超时），而非页面结构或解析问题。

    用于决定要不要继续换下一个 DDG 端点：DNS 解析不了、连不上、超时都属于
    「这台机器到该站点的链路不通」，换同一站点的另一个域名只会再白等一次超时。
    判定依据是 _http_request 统一包装后的中文错误前缀（见 _classify_url_error），
    不依赖具体异常类型，避免 urllib 各版本异常链差异。
    """
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    message = str(exc)
    return any(marker in message for marker in (
        "连接超时", "请求超时", "连接被拒绝", "连接被重置", "DNS 解析失败",
        "TLS 握手失败", "timed out", "Connection refused", "Connection reset",
    ))


def _fetch_ddg_results(query: str, limit: int, *, ignore_breaker: bool = False) -> tuple:
    global _DDG_BREAKER_FAILS, _DDG_BREAKER_SKIP_UNTIL, _DDG_NET_FAILS, _DDG_NET_SKIP_UNTIL
    import time as _time
    if not ignore_breaker and _time.monotonic() < _DDG_BREAKER_SKIP_UNTIL:
        raise ValueError("DuckDuckGo 近期连续失败，熔断跳过中（仅 Bing 结果）")
    if not ignore_breaker and _time.monotonic() < _DDG_NET_SKIP_UNTIL:
        raise ValueError("DuckDuckGo 近期网络不可达（DNS/连接/超时），已临时跳过；如需强制重试请用 engine=ddg")
    failures = []
    last_page = ("", "")
    network_down = True  # 只要有一次请求真的拿到页面就置 False
    # HTML 无法解析/访问失败时仅追加一次 Lite 尝试，不绕过验证码，也不无限重试。
    # 本机（国内网络）实测两个端点都是「连不上→超时」，长超时纯属白等；
    # 5s 足够区分「慢」和「不可达」，把 45s 总预算留给真正能用的引擎。
    for endpoint in ("https://html.duckduckgo.com/html/", "https://lite.duckduckgo.com/lite/"):
        try:
            _, status, content_type, raw = _http_request(endpoint + "?" + urlencode({"q": query}), timeout_seconds=5)
            if status >= 400:
                raise ValueError(f"HTTP {status}")
            html_text, _charset = decode_text(raw, candidates=(_pick_web_charset(content_type, raw, ""),))
            last_page = (endpoint, html_text)
            network_down = False
            parser = _DdgResultParser()
            parser.feed(html_text)
            if parser.current:
                parser.results.append(tuple(parser.current))
            if parser.results:
                _DDG_BREAKER_FAILS = 0
                _DDG_NET_FAILS = 0
                return _relevance_rank(_dedupe_results(parser.results, limit * 3), query, limit * 3), False
            if re.search(r"no results|no-results|没有找到", html_text, re.I):
                _DDG_BREAKER_FAILS = 0
                _DDG_NET_FAILS = 0
                return [], False
            raise ValueError("未解析到结果：可能验证码、访问受限或页面结构变化")
        except (ValueError, OSError) as exc:
            failures.append(f"{urlsplit(endpoint).hostname}: {exc}")
            if _is_network_level_failure(exc):
                # HTML 端点是网络层失败（DNS/连接/超时）时，lite 端点同域名同网络，
                # 必然同样失败，没必要再等一个超时窗口。
                break
    if network_down:
        # 两个端点都没拿到页面 = 网络层不可达，与「页面结构改版」完全不同，
        # 冷却更久并给出明确原因，避免每次搜索都白等 16 秒。
        _DDG_NET_FAILS += 1
        _DDG_NET_SKIP_UNTIL = _time.monotonic() + _DDG_BREAKER_COOLDOWN_SECONDS
    if last_page[1]:
        rescued = _generic_link_fallback(last_page[1], last_page[0], query, limit * 3)
        if rescued:
            _DDG_BREAKER_FAILS = 0
            return rescued, True
    _DDG_BREAKER_FAILS += 1
    if _DDG_BREAKER_FAILS >= _DDG_BREAKER_THRESHOLD:
        _DDG_BREAKER_SKIP_UNTIL = _time.monotonic() + _DDG_BREAKER_COOLDOWN_SECONDS
    raise ValueError("DuckDuckGo HTML/Lite 均失败：" + "；".join(failures))


def _fetch_so360_results(query: str, limit: int, timeout_seconds: float = 10.0) -> tuple:
    """请求并解析 360 搜索（so.com）结果，返回 ([(标题, 链接, 摘要)], 是否走兜底)。

    为什么需要它：Bing 网页版对中文长查询会做激进切词，「小学数学教师资格证
    面试 历年真题 试讲题目」会被降级成「小学」单字泛词，返回百科全书条目；
    DuckDuckGo（html/lite）在国内网络下通常直接超时。360 搜索不需要额外跳转
    就能拿到真实目标地址（data-mdurl），适合作为中文查询的第二来源。
    """
    collected = []
    search_url = "https://www.so.com/s?" + urlencode({"q": query, "pn": 1})
    html_text = ""
    # 默认只抓第 1 页：360 对连续/频繁请求会跳验证码页（qcaptcha.so.com），
    # 多抓一页反而更容易把自己关进限流。只有第 1 页成功但条数明显不足时才补第 2 页。
    for page in (1, 2):
        page_url = "https://www.so.com/s?" + urlencode({"q": query, "pn": page})
        final_url, status, content_type, raw = _http_request(page_url, timeout_seconds=timeout_seconds)
        if status >= 400:
            if page == 1:
                raise ValueError(f"360 搜索 HTTP {status}")
            break
        if "qcaptcha" in (final_url or "").lower() or "/noresult" in (final_url or "").lower():
            # 360 把「疑似机器访问」和「确实无结果」都重定向到这个页面
            if page == 1:
                raise ValueError(
                    "360 搜索返回验证码/无结果页（疑似触发反爬限流，或该查询确实无结果）；"
                    "稍后重试或改用 engine=bing")
            break
        charset = _pick_web_charset(content_type, raw, "")
        html_text, _charset = decode_text(raw, candidates=(charset,))
        page_items = _parse_so360_page(html_text, page_url)
        collected.extend(page_items)
        search_url = page_url
        if len(collected) >= limit * 2 or not page_items:
            # 已够用，或该页压根没解析出结果（翻页也不会更好）
            break
    if not collected:
        rescued = _generic_link_fallback(html_text, search_url, query, limit * 3)
        return rescued, bool(rescued)
    return _relevance_rank(_dedupe_results(collected, limit * 3), query, limit * 3), False


def _parse_so360_page(html_text: str, page_url: str) -> list:
    """解析 360 搜索单页结果，返回 [(标题, 链接, 摘要)]。

    360 的每个自然结果在 <li class="res-list ...">，标题链接同时带 href（跳转
    链接）和 data-mdurl（真实目标地址）。优先 data-mdurl，避免抓取端还要
    跟随一次 so.com/link 跳转。
    """
    starts = [found.start() for found in
              re.finditer(r'<li[^>]+class="[^"]*\bres-list\b', html_text, re.IGNORECASE)]
    skip_titles = {"其他人还搜了", "相关搜索", "相关推荐", "大家还在搜", "猜你喜欢"}
    # ai.so.com 是 360 的 AI 摘要卡片（站内页），不是可打开的真实网页；
    # 作为搜索结果返回会让模型误以为抓到了原文。
    skip_hosts = {"ai.so.com", "www.so.com", "so.com"}
    collected = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else min(len(html_text), start + 20000)
        block = html_text[start:end]
        anchor = re.search(
            r'<h3[^>]*class="[^"]*res-title[^"]*"[^>]*>\s*<a\b([^>]*)>(.*?)</a>',
            block, re.IGNORECASE | re.DOTALL)
        if not anchor:
            continue
        attrs, title_html = anchor.group(1), anchor.group(2)
        title = _strip_tags(title_html)
        if not title or title in skip_titles:
            continue
        href_match = re.search(r'data-mdurl="([^"]+)"', attrs) or re.search(r'href="([^"]+)"', attrs)
        if not href_match:
            continue
        url = unescape(href_match.group(1)).strip()
        url = urljoin("https://www.so.com", url) if url.startswith("/") else url
        if not url.lower().startswith("http"):
            continue
        if (urlsplit(url).hostname or "").lower() in skip_hosts:
            continue
        snippet_match = (re.search(r'class="res-list-summary"[^>]*>(.*?)</span>', block, re.DOTALL)
                         or re.search(r'class="[^"]*res-desc[^"]*"[^>]*>(.*?)</(?:p|div|span)>', block, re.DOTALL))
        snippet = _strip_tags(snippet_match.group(1))[:300] if snippet_match else ""
        collected.append((title, url, snippet))
    return collected


# 各引擎池「够不够好」的门槛：过了就不再请求下一个引擎，控制总耗时
def _budget_ok(minimum: float = 6.0) -> bool:
    """剩余预算是否够再发起一次引擎请求（web_search 总预算 45s）。"""
    try:
        return remaining_seconds(45.0) >= minimum
    except Exception:
        return True


def _fetch_sogou_results(query: str, limit: int, timeout_seconds: float = 10.0) -> tuple:
    """请求并解析搜狗（sogou.com）网页搜索，返回 ([(标题, 链接, 摘要)], 是否走兜底)。

    为什么需要它：实测 Bing 网页版对中文长查询会把整句退化成单字泛词
    （「小学数学教师资格证面试 历年真题 试讲题目」→ 只搜「小学」，返回
    朱熹《小学》、中小学教材电子版），360 搜索对频繁请求会跳到验证码页
    （qcaptcha.so.com）。搜狗对中文长查询的分词与排序最贴近预期，实测同一
    查询命中的全是「小学数学教师资格证面试试讲真题」这类真实页面。

    搜狗结果是 `/link?url=...` 跳转链接，需要一次 HTTP 请求解析出真实地址
    （跳转页用 `window.location.replace("...")` 前端跳转，不是 3xx 重定向）。
    因此先用标题/摘要判断相关度，只对最终展示所需的最相关结果做解析，避免
    N 次额外请求。
    """
    search_url = "https://www.sogou.com/web?" + urlencode({"query": query})
    final_url, status, content_type, raw = _http_request(search_url, timeout_seconds=timeout_seconds)
    if status >= 400:
        raise ValueError(f"搜狗 HTTP {status}")
    # 反爬重定向判定必须看「最终 URL」，查请求 URL 永远不命中（这里原本写错了，
    # 导致被拦截时只能靠下面的 HTML 文本特征兜底）。
    if "antispider" in (final_url or "").lower():
        raise ValueError("搜狗反爬拦截（antispider），请稍后重试或用 engine=bing")
    charset = _pick_web_charset(content_type, raw, "")
    html_text, _charset = decode_text(raw, candidates=(charset,))
    if "antispider" in html_text[:4000].lower() or "请输入验证码" in html_text[:4000]:
        raise ValueError("搜狗返回验证码/反爬页面，请稍后重试")
    # 用最终 URL（可能被重定向）作基准解析相对链接，避免 /link?url= 被解析到错站点
    collected = _parse_sogou_page(html_text, final_url or search_url)
    if not collected:
        rescued = _generic_link_fallback(html_text, final_url or search_url, query, limit * 3)
        return rescued, bool(rescued)
    ranked = _relevance_rank(_dedupe_results(collected, limit * 4), query, limit * 4)
    return ranked, False


def _parse_sogou_page(html_text: str, page_url: str) -> list:
    """解析搜狗搜索结果页，返回 [(标题, 链接, 摘要)]。"""
    blocks = re.split(r'<div class="vrwrap"', html_text)[1:]
    skip_titles = {"其他人还搜了", "相关搜索", "相关推荐", "大家还在搜"}
    collected = []
    for block in blocks:
        anchor = re.search(r'<h3[^>]*>.*?<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, re.IGNORECASE | re.DOTALL)
        if not anchor:
            continue
        href, title_html = anchor.group(1), anchor.group(2)
        title = _strip_tags(title_html)
        # 搜狗高亮会把汉字拆进 <em data-feedid=...>，_strip_tags 后可能出现多余空格
        title = re.sub(r"\s+", " ", title).strip()
        if not title or title in skip_titles or len(title) < 2:
            continue
        if _is_junk_title(title):
            continue
        url = urljoin(page_url, unescape(href).strip())
        if not url.lower().startswith("http"):
            continue
        # 摘要：搜狗常见容器 class（text-layout / star-wiki / fz-mid / space-txt）
        snippet_match = re.search(
            r'<(?:p|div)[^>]*class="[^"]*(?:text-layout|star-wiki|fz-mid|space-txt)[^"]*"[^>]*>(.*?)</(?:p|div)>',
            block, re.IGNORECASE | re.DOTALL)
        snippet = _strip_tags(snippet_match.group(1))[:300] if snippet_match else ""
        collected.append((title, url, snippet))
    return collected


def _resolve_sogou_link(url: str, timeout_seconds: float = 6.0) -> str:
    """把搜狗 /link?url=... 跳转链接解析成真实目标地址。

    跳转页是 226 字节的 HTML，用 `window.location.replace("...")` 做前端跳转，
    所以不能靠 HTTP 3xx 拿到目标。解析失败时返回原链接（fetch_url 仍可跟随）。
    """
    if "sogou.com/link?" not in str(url):
        return url
    # 同一跳转链接在一次搜索里会重复出现（多条结果指向同一页面），
    # 用进程内小缓存避免重复请求。
    cached = _SOGOU_LINK_CACHE.get(url)
    if cached:
        return cached
    resolved = url
    try:
        _, status, content_type, raw = _http_request(url, timeout_seconds=timeout_seconds)
        if status < 400:
            body, _charset = decode_text(raw, candidates=(_pick_web_charset(content_type, raw, ""),))
            for pattern in (r'location\.replace\(["\']([^"\']+)', r'location\.href\s*=\s*["\']([^"\']+)',
                            r'URL=([^"\'>]+)'):
                match = re.search(pattern, body, re.IGNORECASE)
                if match and match.group(1).lower().startswith("http"):
                    resolved = match.group(1)
                    break
    except (ValueError, OSError):
        resolved = url
    if resolved != url:
        if len(_SOGOU_LINK_CACHE) > 256:
            _SOGOU_LINK_CACHE.clear()
        _SOGOU_LINK_CACHE[url] = resolved
    return resolved


# 搜狗跳转链接 → 真实地址的进程内缓存（跳转内容是稳定的）
_SOGOU_LINK_CACHE: dict = {}


def _web_search_impl(query, max_results, engine="auto") -> str:
    query = string_arg({"query": query}, "query", required=True, max_length=4096).strip()
    limit = int_arg({"max_results": max_results}, "max_results", 8, minimum=1, maximum=_WEB_SEARCH_MAX_RESULTS)
    with tool_deadline(45):
        return _web_search_checked(query, limit, engine)


def _web_search_checked(query, limit, engine):
    engine_key = str(engine or "auto").strip().lower()
    if engine_key in ("ddg", "duckduckgo"):
        engine_key = "ddg"
    elif engine_key in ("so", "so360", "360"):
        engine_key = "so360"
    elif engine_key in ("sogou", "sg"):
        engine_key = "sogou"
    elif engine_key not in ("auto", "bing"):
        raise ValueError("engine 仅支持 auto/bing/sogou/ddg（duckduckgo）/so360（360搜索）")
    candidate_limit = _WEB_SEARCH_MAX_RESULTS
    diagnostics = []
    pools = []  # [(引擎名, [(标题, 链接, 摘要)])]，按尝试顺序收集，最后统一合并重排

    def try_engine(name, fetcher):
        """跑一个引擎（fetcher(query, limit)）；单个引擎失败只记诊断，不让整体搜索失败。"""
        if not _budget_ok():
            diagnostics.append(f"{name}: 剩余时间预算不足，已跳过")
            return []
        try:
            pool, fallback_used = fetcher(query, candidate_limit)
        except (ValueError, OSError) as exc:
            diagnostics.append(f"{name}: {exc}")
            return []
        if fallback_used:
            diagnostics.append(
                f"{name} 页面结构可能已改版：结构化解析无结果，已回退为链接提取，可靠性较低，请用 fetch_url 核验")
        pool = list(pool or [])
        if pool:
            pools.append((name, pool))
        else:
            # 引擎返回空池必须留痕：静默跳过会让模型以为「只用了 Bing 且结果不够」，
            # 却无从知道另一个来源是被限流、改版还是确实无结果（实测 360 对英文
            # 查询会因限流返回空页，不提示就等于丢了一次重试机会）。
            diagnostics.append(f"{name}: 未返回可用结果（可能确实无匹配、被限流或页面结构变化）")
        return pool

    if engine_key == "bing":
        # 显式指定引擎必须照做：调用方要 Bing 就不能偷偷换成搜狗。
        # 之前这里复用了 auto 的「中文优先搜狗」路由，engine=bing 实际走的是搜狗，
        # 失败时还报「Bing 未解析到结果」，报错与行为自相矛盾（实测踩过）。
        if not try_engine("Bing", _fetch_bing_results):
            raise ValueError(
                "Bing 未解析到结果（站点改版或访问受限）。可改用 engine=sogou 重试，"
                "或直接用 fetch_url 打开 https://www.bing.com/search?q=关键词 自行提取。")
    elif engine_key == "auto":
        # 中文查询先用搜狗：实测 Bing 网页版会把中文长查询退化成单字泛词
        # （「小学数学教师资格证面试 历年真题 试讲题目」只按「小学」检索，
        # 返回朱熹《小学》、中小学教材电子版），搜狗的分词与排序最贴合原意。
        # 英文查询仍以 Bing 为主（它更懂英文）。
        cjk_query = _is_cjk_query(query)
        primary_name, primary_fetcher = (
            ("搜狗", _fetch_sogou_results) if cjk_query else ("Bing", _fetch_bing_results))
        primary_pool = try_engine(primary_name, primary_fetcher)
        # 主引擎池不够好（弱相关或偏薄）时补第二个来源。
        if not _pool_is_good(primary_pool, query):
            if cjk_query:
                # 中文：搜狗 → Bing → 360，都是 1s 级请求
                try_engine("Bing", _fetch_bing_results)
                if not _pool_is_good(_merge_pools(pools, candidate_limit), query):
                    try_engine("360搜索", _fetch_so360_results)
            else:
                # 英文：Bing 退化时补搜狗，仍然不够好才考虑 DDG
                try_engine("搜狗", _fetch_sogou_results)
                if not _pool_is_good(_merge_pools(pools, candidate_limit), query):
                    try_engine("DuckDuckGo", _fetch_ddg_results)
    elif engine_key == "so360":
        try_engine("360搜索", _fetch_so360_results)
        if not pools:
            raise ValueError(
                "360 搜索未解析到结果（站点改版或访问受限）。可改用 engine=bing 重试，"
                "或直接用 fetch_url 打开 https://www.so.com/s?q=关键词 自行提取。")
    elif engine_key == "sogou":
        try_engine("搜狗", _fetch_sogou_results)
        if not pools:
            raise ValueError(
                "搜狗未解析到结果（站点改版、反爬拦截或访问受限）。可改用 engine=bing 重试，"
                "或直接用 fetch_url 打开 https://www.sogou.com/web?query=关键词 自行提取。")
    if not pools:
        # 显式 engine=ddg 绕过熔断（用户明确要求就试一次）；auto 回退遵守熔断
        try_engine("DuckDuckGo", lambda q, limit: _fetch_ddg_results(
            q, limit, ignore_breaker=(engine_key == "ddg")))
        if not pools:
            detail = "；".join(diagnostics) or "无可用引擎"
            providers = "DuckDuckGo" if engine_key == "ddg" else "搜狗 / Bing / 360搜索 / DuckDuckGo"
            raise ValueError(
                providers + " 未获得搜索结果（站点改版、网络受限或解析失败）：" + detail +
                "。可直接用 fetch_url 打开 https://www.bing.com/search?q=关键词 ，"
                "用 mode=text/regex 自行提取。")
    engine_name = "+".join(name for name, _pool in pools) + (" 合并重排" if len(pools) > 1 else "")
    ranked = _relevance_rank(_merge_pools(pools, candidate_limit * 6), query, candidate_limit * 6)
    # site: 是明确的域名/路径约束，必须最先过滤（在任何条数裁剪之前）。
    # 实测踩过的坑：`site:docs.python.org asyncio taskgroup` 的唯一命中结果相关度
    # 只有「弱」，若先按「弱相关只用于补位」裁剪、再过滤域名，就会两边都落空，
    # 最终返回 0 条。
    sites = re.findall(r"\bsite:([^\s]+)", query, re.I)

    def in_site(url, site):
        wanted = urlsplit(site if "://" in site else "https://" + site)
        actual = urlsplit(url)
        host, domain = (actual.hostname or "").lower(), (wanted.hostname or "").lower()
        return bool(domain) and (host == domain or host.endswith("." + domain)) and actual.path.startswith(wanted.path)

    graded = [(item, _relevance_grade(item, query)) for item in ranked]
    if sites:
        # 搜狗给的是 sogou.com/link?url=... 跳转链接，其主机名是 sogou.com 而不是
        # 目标站点。不先解出真实地址，site: 过滤会把搜狗的命中结果全部误剔
        # （实测 `site:docs.python.org asyncio taskgroup` 因此返回 0 条）。
        # 上限 20 条 + 单条 4s，避免最坏情况拖爆 45s 总预算。
        graded = [
            ((title, _resolve_sogou_link(url, timeout_seconds=4.0), snippet)
             if index < 20 and "sogou.com/link?" in str(url) else (title, url, snippet), grade)
            for index, ((title, url, snippet), grade) in enumerate(graded)
        ]
        graded = [(item, grade) for item, grade in graded
                  if any(in_site(item[1], site) for site in sites)]
        if len(graded) != len(ranked):
            diagnostics.append("已剔除不符合 site: 域名/路径约束的结果")
    strong = [item for item, grade in graded if grade >= 2]
    weak = [item for item, grade in graded if grade == 1]
    if strong:
        if sites:
            # 域名是调用方显式约束：此时弱相关就是「域内唯一可用结果」，
            # 不能再按补位规则裁掉，否则约束与召回会互相抵消。
            results = strong + weak
        else:
            # 强相关排前面；弱相关只在强相关不足展示条数时补齐，绝不主动把泛化
            # 结果混进前排（原故障里正是百科「小学」条目被当成命中结果）。
            results = strong + weak[:max(0, limit - len(strong))]
        if weak:
            diagnostics.append(
                f"强相关 {len(strong)} 条，另有 {len(weak)} 条弱相关（只部分命中查询词，"
                "标题已标注 [弱相关]）；判断相关性时请看摘要，必要时改写关键词或用 site: 限定来源")
    else:
        # 一条强相关都没有：与其返回空结果让调用方无法判断「没结果」还是「解析失败」，
        # 不如把弱相关候选交出去并明确标注，由模型决定是否采信。
        results = weak
        if weak:
            diagnostics.append(
                "未找到强相关结果：以下候选只部分命中查询词，很可能是泛化结果"
                "（搜索引擎把长查询拆成了泛化词），请务必核验后再引用")
        else:
            diagnostics.append("候选结果与查询无明显词面重叠，已全部丢弃；不能据此推断网页不存在")
    shown = results[:limit]
    # 搜狗结果是 /link?url= 跳转链接（前端 JS 跳转，不是 HTTP 3xx），直接返回
    # 会让模型拿到毫无意义的 sogou.com 地址。这里只对最终展示的少量结果做
    # 解析（每条 1 次 ~200 字节请求），失败则保留原链接供 fetch_url 跟随。
    if any("sogou.com/link?" in str(item[1]) for item in shown):
        shown = [(title, _resolve_sogou_link(url), snippet) for title, url, snippet in shown]
    # 注意：链接解析会重建 tuple（id 变化），相关度必须在解析后再算，不能复用
    # 之前 strong 列表里的对象身份，否则 [弱相关] 标记会整体错位。
    strong_flags = [_relevance_grade(item, query) >= 2 for item in shown]
    entities = _search_numeric_entities(query)
    if shown and entities and not any(all(_entity_in_text(entity, ' '.join(map(str, item)))
                                          for entity in entities) for item in shown):
        diagnostics.append("结果未完整匹配查询中的版本号/年份（" + ', '.join(sorted(entities)) +
                           "），可能是泛化结果；请调整关键词或用 site: 指定来源，并用 fetch_url 核验")
    lines = [f"[web_search] 查询: {query} | 共 {len(shown)} 条结果（{engine_name}，链接正文可用 fetch_url 抓取）"]
    if diagnostics:
        lines.append("[web_search] 提示: " + "；".join(diagnostics))
    if not shown:
        lines.append("[web_search] 未获得可靠结果；可能确实无结果、访问受限或解析失败，请更换关键词/引擎并核验")
    for index, item in enumerate(shown, start=1):
        title, url, snippet = item
        # 逐条标注相关性等级，避免模型把泛化结果当成权威来源直接引用
        flag = "" if strong_flags[index - 1] else " [弱相关]"
        lines.append(f"{index}. {title}{flag}\n   链接: {url}" + (f"\n   摘要: {snippet}" if snippet else ""))
    return "\n".join(lines)


# 仅注册网页抓取与搜索；本地文件和命令由后端内置工具提供。
@web_mcp_server.tool()
async def fetch_url(
    url: Annotated[str, Field(min_length=1, max_length=8192, description="完整 HTTP(S) 地址")],
    mode: Literal["text", "html", "tag", "regex"] = "text",
    pattern: Annotated[str, Field(max_length=8192, description="regex 模式的原始响应正则，可用 (?s) 跨行")] = "",
    tag: Annotated[str, Field(max_length=4096, description="tag 模式：div、div#id、div.class，逗号分隔最多 20 个")] = "",
    max_chars: Annotated[int, Field(strict=True, ge=0, le=100000, description="正文字符预算，默认 8000；最少返回预算 200，0 使用默认")] = _FETCH_URL_DEFAULT_CHARS,
    timeout_seconds: Annotated[float, Field(strict=True, ge=1, le=120, allow_inf_nan=False, description="请求总时限秒数，默认 30")] = 30.0,
    encoding: Annotated[str, Field(max_length=64, description="响应编码；省略自动判断")] = "",
    method: Annotated[str, Field(min_length=1, max_length=20, description="HTTP 方法，默认 GET")] = "GET",
    body: Annotated[Union[str, dict, list], Field(description="请求体；可传对象/数组（自动转 JSON）或字符串；默认 Content-Type 为 application/json")] = "",
    headers: Annotated[Optional[dict[str, str]], Field(max_length=64, description="附加请求头字符串键值对象")] = None,
    offset: Annotated[int, Field(strict=True, ge=0, description="GET/HEAD 文本字符偏移；按 next_offset 续读，每次重新请求")] = 0,
) -> str:
    '抓取网页或 HTTP 接口，不执行 JavaScript。text 提取正文，html 返回原文，tag 简单选择器，regex 匹配原始响应。按 next_offset 续读仅支持 GET/HEAD；4xx/5xx 或部分响应会明确标记，不能当作完整成功结果。'
    return await asyncio.to_thread(
        _fetch_url_impl, url, mode, pattern, tag, max_chars, timeout_seconds,
        encoding, method, body, headers, offset)


@web_mcp_server.tool()
async def web_open(
    url: Annotated[str, Field(min_length=1, max_length=8192, description="要打开的 HTTP(S) 页面 URL")],
    max_chars: Annotated[int, Field(strict=True, ge=0, le=100000, description="正文字符数，默认 8000；0 使用默认值")] = _FETCH_URL_DEFAULT_CHARS,
    offset: Annotated[int, Field(strict=True, ge=0, description="续读偏移；按上次返回的 next offset 传入")] = 0,
    max_links: Annotated[int, Field(strict=True, ge=0, le=50, description="同时返回的可跟进链接数，默认 20")] = 20,
    timeout_seconds: Annotated[float, Field(strict=True, ge=1, le=120, allow_inf_nan=False, description="整次操作时限秒数，默认 30")] = 30.0,
) -> str:
    """打开静态网页，返回标题、正文、分页偏移和编号链接；不执行 JavaScript。用 web_click 跟进链接编号。"""
    return await asyncio.to_thread(_web_open_impl, url, max_chars, offset, max_links, timeout_seconds)


@web_mcp_server.tool()
async def web_click(
    url: Annotated[str, Field(min_length=1, max_length=8192, description="之前 web_open 的页面 URL")],
    link_id: Annotated[int, Field(strict=True, ge=1, le=50, description="web_open 显示的 1-based 链接编号")],
    max_chars: Annotated[int, Field(strict=True, ge=0, le=100000, description="目标页正文字符数，默认 8000")] = _FETCH_URL_DEFAULT_CHARS,
    offset: Annotated[int, Field(strict=True, ge=0, description="目标页续读偏移")] = 0,
    max_links: Annotated[int, Field(strict=True, ge=0, le=50, description="目标页同时返回的可跟进链接数，默认 20")] = 20,
    timeout_seconds: Annotated[float, Field(strict=True, ge=1, le=120, allow_inf_nan=False, description="查找并打开链接的总时限秒数，默认 45")] = 45.0,
) -> str:
    """按 web_open 的链接编号打开目标页；会重新读取来源页，因此页面变动时请重新 web_open。"""
    return await asyncio.to_thread(_web_click_impl, url, link_id, max_chars, offset, max_links, timeout_seconds)


@web_mcp_server.tool()
async def web_find(
    url: Annotated[str, Field(min_length=1, max_length=8192, description="要查找的 HTTP(S) 页面 URL")],
    query: Annotated[str, Field(min_length=1, max_length=1024, description="静态正文中的字面文本，忽略大小写")],
    context_lines: Annotated[int, Field(strict=True, ge=0, le=10, description="每处匹配前后附带的行数，默认 2")] = 2,
    max_matches: Annotated[int, Field(strict=True, ge=1, le=50, description="最多返回匹配处数，默认 10")] = 10,
    timeout_seconds: Annotated[float, Field(strict=True, ge=1, le=120, allow_inf_nan=False, description="请求总时限秒数，默认 30")] = 30.0,
) -> str:
    """在静态网页正文中查找字面文本，只返回匹配行及上下文；动态页面可能需要 web_open 检查 HTML。"""
    return await asyncio.to_thread(_web_find_impl, url, query, context_lines, max_matches, timeout_seconds)


@web_mcp_server.tool()
async def web_search(
    query: Annotated[str, Field(min_length=1, max_length=4096, description="搜索词，可用 site: 限定来源")],
    max_results: Annotated[int, Field(strict=True, ge=1, le=20, description="展示条数，默认 8；不改变候选池策略")] = 8,
    engine: Literal["auto", "sogou", "bing", "ddg", "so360"] = "auto",
) -> str:
    '搜索候选网页，返回标题、链接和摘要；用 web_open 打开并跟进链接、web_find 定位正文。auto 中文查询以搜狗为主、英文以 Bing 为主，弱相关/偏薄时补其他引擎（Bing / 360 / DDG）；总预算 45 秒。[弱相关] 标记表示只部分命中查询词，引用前需核验。网页解析可能随搜索站点改版失效，空结果不证明网页不存在。'
    return await asyncio.to_thread(_web_search_impl, query, max_results, engine)


# 运行服务器
if __name__ == "__main__":
    # 添加错误处理来诊断问题
    try:
        # 使用 run() 方法启动 MCP 服务器,指定 stdio 传输方式
        web_mcp_server.run(transport="stdio")
    except Exception as e:
        import traceback
        print(f"❌ MCP 服务器启动失败: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)
