# coding: utf-8
"""
系统的 MCP 网页工具服务。

当前注册工具（2 个；read/write/edit/search 文件四件套与 run_command 已迁移为主项目
后端内置工具 factory/agent_runtime/builtin_tools.py，list_items 已停用）：
    fetch_url       抓取网页/HTTP 接口（纯文本提取、标签/正则抽取、gzip/deflate 自动解压、截断）
    web_search      网页搜索（Bing 主 + DuckDuckGo 回退合并、重定向解包、返回标题/链接/摘要）

实现说明：
    - 阻塞网络请求统一通过 asyncio.to_thread
      放到工作线程执行，避免卡死 stdio 事件循环；
    - 所有大输出统一截断保护，避免撑爆模型上下文；
    - 文本解码采用统一 BOM/严格 UTF-8/声明与本地编码回退，避免汉字评分误判。
"""
# 标准库
import asyncio
import base64
import re
import sys
import zlib
from html import unescape
from html.parser import HTMLParser
from http.client import IncompleteRead
from pathlib import Path
from typing import Optional, Annotated, Literal
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
sys_mcp_server = FastMCP("sys-mcp-server")


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


def _html_to_text(html_text: str) -> str:
    parser = _HtmlTextExtractor()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception:
        return html_text
    return parser.get_text()


# text 模式默认跳过的导航/页脚类标签：这些内容对"读网页正文"几乎没有信息量，
# 却经常占满 max_chars 截断预算，把真正有用的正文挤掉
_NOISE_TAG_NAMES = ("nav", "header", "footer", "aside", "noscript", "template")
# 常见正文容器 id/class 关键词（依次尝试，命中即整块抽取）
_MAIN_CONTENT_HINTS = (
    ("id", "content"), ("id", "main-content"), ("id", "main"),
    ("class", "main-content"), ("class", "post-content"), ("class", "article-content"),
    ("class", "markdown-body"), ("class", "article"), ("class", "content"),
)


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
_ATTR_PATTERN = re.compile(r"([\w:-]+)\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)")


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


def _attrs_match(attrs_text: str, conditions: list) -> bool:
    attrs = {name.lower(): value.strip("\"'") for name, value in _ATTR_PATTERN.findall(attrs_text)}
    for key, expected in conditions:
        value = attrs.get(key, "")
        values = value.split() if key == "class" else [value]
        if expected not in values:
            return False
    return True


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
        raise ValueError(f"请求失败: {url}（{exc.reason}）") from exc
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
    values = {"url": url, "mode": mode, "pattern": pattern, "tag": tag, "max_chars": max_chars,
              "timeout_seconds": timeout_seconds, "encoding": encoding, "method": method,
              "body": body, "offset": offset}
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
        notes.append("没有提取到静态正文；页面可能依赖 JavaScript、需要登录或被站点拦截。此工具不执行 JavaScript，请用 mode=html/tag 核对或改用正文/下载链接")
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
    return bool(re.search(r"(?<![\d.])" + re.escape(entity) + r"(?![\d.])", text))


def _has_weak_relevance(results: list, query: str) -> bool:
    """判断候选结果是否与查询的「特征关键词」脱节（用于触发多引擎合并召回）。

    特征关键词 = 含数字或非 ASCII 字符的词（如版本号 3.14、中文词 新特性），
    这类词最能区分结果相关性；纯英文通用词（如语言名 python）几乎每条结果
    都会命中，不参与判断。没有任何一条结果覆盖全部特征词即视为弱相关。
    查询没有特征词时恒返回 False（无法判断时不折腾）。
    """
    specific = [
        keyword for keyword in _search_keywords(query)
        if re.search(r"[0-9]", keyword) or any(ord(ch) > 127 for ch in keyword)
    ]
    keywords = _search_keywords(query)
    if keywords and not any(any(word in f"{item[0]} {item[1]} {item[2]}".lower()
                                for word in keywords) for item in results):
        return True
    if not specific:
        return False
    for item in results:
        haystack = f"{item[0]} {item[2]} {item[1]}".lower()
        if all(keyword in haystack for keyword in specific):
            return False
    return True


def _keyword_score(item: tuple, keywords: set) -> float:
    """单条结果的相关性得分：标题命中权重最高，摘要次之，URL 再次；
    文档/参考类路径（docs、changelog、whatsnew 等）小幅加权。"""
    title, url, snippet = str(item[0]), str(item[1]), str(item[2])
    hay_title, hay_snippet, hay_url = title.lower(), snippet.lower(), url.lower()
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
    keywords = _search_keywords(query)
    entities = _search_numeric_entities(query)
    def score(item):
        # 精确版本/年份覆盖优先于通用语言名反复出现的页面；相同覆盖度再按
        # 标题/摘要/URL 关键词评分，仍保留搜索引擎顺序作为平手依据。
        text = ' '.join(map(str, item)).lower()
        return (sum(_entity_in_text(entity, text) for entity in entities),
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


def _fetch_bing_results(query: str, limit: int) -> list:
    """请求并解析 Bing 网页版搜索结果，返回 [(标题, 链接, 摘要)]。

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
    # 返回完整排序池（≤3×limit），由调用方决定展示条数，便于多引擎合并
    return _relevance_rank(_dedupe_results(collected, limit * 3), query, limit * 3)


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


def _fetch_ddg_results(query: str, limit: int) -> list:
    failures = []
    # HTML 无法解析/访问失败时仅追加一次 Lite 尝试，不绕过验证码，也不无限重试。
    for endpoint in ("https://html.duckduckgo.com/html/", "https://lite.duckduckgo.com/lite/"):
        try:
            _, status, content_type, raw = _http_request(endpoint + "?" + urlencode({"q": query}), timeout_seconds=15)
            if status >= 400:
                raise ValueError(f"HTTP {status}")
            html_text, _charset = decode_text(raw, candidates=(_pick_web_charset(content_type, raw, ""),))
            parser = _DdgResultParser()
            parser.feed(html_text)
            if parser.current:
                parser.results.append(tuple(parser.current))
            if parser.results:
                return _relevance_rank(_dedupe_results(parser.results, limit * 3), query, limit * 3)
            if re.search(r"no results|no-results|没有找到", html_text, re.I):
                return []
            raise ValueError("未解析到结果：可能验证码、访问受限或页面结构变化")
        except (ValueError, OSError) as exc:
            failures.append(f"{urlsplit(endpoint).hostname}: {exc}")
    raise ValueError("DuckDuckGo HTML/Lite 均失败：" + "；".join(failures))


def _web_search_impl(query, max_results, engine="auto") -> str:
    query = string_arg({"query": query}, "query", required=True, max_length=4096).strip()
    limit = int_arg({"max_results": max_results}, "max_results", 8, minimum=1, maximum=_WEB_SEARCH_MAX_RESULTS)
    with tool_deadline(45):
        return _web_search_checked(query, limit, engine)


def _web_search_checked(query, limit, engine):
    engine_key = str(engine or "auto").strip().lower()
    if engine_key in ("ddg", "duckduckgo"):
        engine_key = "ddg"
    elif engine_key not in ("auto", "bing"):
        raise ValueError("engine 仅支持 auto/bing/ddg（duckduckgo）")
    results, engine_name = [], ""
    diagnostics = []
    candidate_limit = _WEB_SEARCH_MAX_RESULTS
    if engine_key in ("auto", "bing"):
        try:
            results = _fetch_bing_results(query, candidate_limit)
            engine_name = "Bing 网页版解析"
        except (ValueError, OSError) as exc:
            diagnostics.append(f"Bing: {exc}")
            results = []  # Bing 异常时自动走 DuckDuckGo 回退，而不是直接失败
        if not results and engine_key == "bing":
            raise ValueError(
                "Bing 未解析到结果（站点改版或访问受限）。可改用 engine=ddg 重试，"
                "或直接用 fetch_url 打开 https://www.bing.com/search?q=关键词 自行提取。")
        # auto 模式下结果与查询特征词脱节或候选池偏薄时，并入 DuckDuckGo 候选
        # 统一重排扩大召回面；DDG 失败或无增量不影响已有结果
        if engine_key == "auto" and results and (
                _has_weak_relevance(results, query) or len(results) < 8):
            try:
                ddg_pool = _fetch_ddg_results(query, candidate_limit)
                # 两个候选池先合并再排序，避免满额 Bing 池在排序前挤掉 DDG 新结果。
                merged = _dedupe_results(list(results) + list(ddg_pool), candidate_limit * 6)
                if len(merged) > len(results):
                    results = _relevance_rank(merged, query, candidate_limit * 3)
                    engine_name = "Bing+DuckDuckGo 合并重排"
            except (ValueError, OSError) as exc:
                diagnostics.append(f"DuckDuckGo: {exc}")
    if not results:
        try:
            results = _fetch_ddg_results(query, candidate_limit)
            engine_name = "DuckDuckGo（Bing 无结果/不可达或显式指定）"
        except (ValueError, OSError) as exc:
            detail = "；".join(diagnostics + [f"DuckDuckGo: {exc}"])
            providers = "DuckDuckGo" if engine_key == "ddg" else "Bing 与 DuckDuckGo"
            raise ValueError(
                providers + " 未获得搜索结果（站点改版或访问受限）：" + detail +
                "。可直接用 fetch_url 打开 https://www.bing.com/search?q=关键词 ，"
                "用 mode=text/regex 自行提取。") from exc
    # site: 是明确的域名/路径约束，不能返回约束外的结果。
    sites = re.findall(r"\bsite:([^\s]+)", query, re.I)
    if sites:
        def in_site(url, site):
            wanted = urlsplit(site if "://" in site else "https://" + site)
            actual = urlsplit(url)
            host, domain = (actual.hostname or "").lower(), (wanted.hostname or "").lower()
            return bool(domain) and (host == domain or host.endswith("." + domain)) and actual.path.startswith(wanted.path)
        filtered = [item for item in results if any(in_site(item[1], site) for site in sites)]
        if len(filtered) != len(results):
            diagnostics.append("已剔除不符合 site: 域名/路径约束的结果")
        results = filtered
    keywords = _search_keywords(query)
    if results and keywords and not any(
            any(word in f"{item[0]} {item[1]} {item[2]}".lower() for word in keywords)
            for item in results):
        results = []
        diagnostics.append("候选结果均未命中查询关键词，已丢弃；不能据此推断网页不存在")
    shown = results[:limit]
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
    for index, (title, url, snippet) in enumerate(shown, start=1):
        lines.append(f"{index}. {title}\n   链接: {url}" + (f"\n   摘要: {snippet}" if snippet else ""))
    return "\n".join(lines)


# 仅注册网页抓取与搜索；本地文件和命令由后端内置工具提供。
@sys_mcp_server.tool()
async def fetch_url(
    url: Annotated[str, Field(min_length=1, max_length=8192, description="完整 HTTP(S) 地址")],
    mode: Literal["text", "html", "tag", "regex"] = "text",
    pattern: Annotated[str, Field(max_length=8192, description="regex 模式的原始响应正则，可用 (?s) 跨行")] = "",
    tag: Annotated[str, Field(max_length=4096, description="tag 模式：div、div#id、div.class，逗号分隔最多 20 个")] = "",
    max_chars: Annotated[int, Field(strict=True, ge=0, le=100000, description="正文字符预算，默认 8000；最少返回预算 200，0 使用默认")] = _FETCH_URL_DEFAULT_CHARS,
    timeout_seconds: Annotated[float, Field(strict=True, ge=1, le=120, allow_inf_nan=False, description="请求总时限秒数，默认 30")] = 30.0,
    encoding: Annotated[str, Field(max_length=64, description="响应编码；省略自动判断")] = "",
    method: Annotated[str, Field(min_length=1, max_length=20, description="HTTP 方法，默认 GET")] = "GET",
    body: Annotated[str, Field(max_length=2 * 1024 * 1024, description="请求体；默认 Content-Type 为 application/json")] = "",
    headers: Annotated[Optional[dict[str, str]], Field(max_length=64, description="附加请求头字符串键值对象")] = None,
    offset: Annotated[int, Field(strict=True, ge=0, description="GET/HEAD 文本字符偏移；按 next_offset 续读，每次重新请求")] = 0,
) -> str:
    '抓取网页或 HTTP 接口，不执行 JavaScript。text 提取正文，html 返回原文，tag 简单选择器，regex 匹配原始响应。按 next_offset 续读仅支持 GET/HEAD；4xx/5xx 或部分响应会明确标记，不能当作完整成功结果。'
    return await asyncio.to_thread(
        _fetch_url_impl, url, mode, pattern, tag, max_chars, timeout_seconds,
        encoding, method, body, headers, offset)


@sys_mcp_server.tool()
async def web_search(
    query: Annotated[str, Field(min_length=1, max_length=4096, description="搜索词，可用 site: 限定来源")],
    max_results: Annotated[int, Field(strict=True, ge=1, le=20, description="展示条数，默认 8；不改变候选池策略")] = 8,
    engine: Literal["auto", "bing", "ddg"] = "auto",
) -> str:
    '搜索候选网页，返回标题、链接和摘要；用 fetch_url 核验正文。auto 在 Bing 候选不足或失败时尝试 DDG；总预算 45 秒。空结果不证明网页不存在。'
    return await asyncio.to_thread(_web_search_impl, query, max_results, engine)


# 运行服务器
if __name__ == "__main__":
    # 添加错误处理来诊断问题
    try:
        # 使用 run() 方法启动 MCP 服务器,指定 stdio 传输方式
        sys_mcp_server.run(transport="stdio")
    except Exception as e:
        import sys
        import traceback
        print(f"❌ MCP 服务器启动失败: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)
