# coding: utf-8
"""
系统的 mcp 服务模块：为聊天模型提供一组通用本地工具。

当前注册工具（4 个；read/write/edit/search 文件四件套已迁移为主项目后端内置工具）：
    list_items      目录浏览（通配符过滤、递归深度、文件/目录过滤）
    run_command     执行终端命令（多 shell；超时保护、输出截断、编码自适应、后台分离模式）
    fetch_url       抓取网页/HTTP 接口（纯文本提取、标签/正则抽取、gzip/deflate 自动解压、截断）
    web_search      网页搜索（Bing 主 + DuckDuckGo 回退合并、重定向解包、返回标题/链接/摘要）

实现说明：
    - 每次工具调用都会重新启动本文件的 MCP 服务器子进程；生成任务中子进程
      工作目录继承自会话 worker（已 chdir 到会话工作目录），因此工具内的
      相对路径默认落在会话工作目录内；修改本文件后下一次工具调用即生效；
    - 阻塞操作（子进程、网络请求、目录遍历）统一通过 asyncio.to_thread
      放到工作线程执行，避免卡死 stdio 事件循环；
    - 所有大输出统一截断保护，避免撑爆模型上下文；
    - 文本解码采用「BOM 嗅探 + 多候选严格解码评分」策略，降低 GBK/UTF-8 互串乱码概率。
"""
# 标准库
import asyncio
import base64
import difflib
import fnmatch
import gzip
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zlib
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlencode
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

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

# 各工具输出上限，防止超大结果撑爆模型上下文
_LIST_DIR_MAX_ENTRIES = 1000
_READ_FILE_MAX_LINES = 2000
_READ_FILE_MAX_CHARS = 60000
_RUN_COMMAND_MAX_CHARS = 12000
_FETCH_URL_DEFAULT_CHARS = 8000
_WEB_SEARCH_MAX_RESULTS = 20

# 跨文件搜索/遍历时默认跳过的目录名（含各语言依赖与构建产物目录）
_SKIP_DIR_NAMES = {
    ".git", ".idea", ".vscode", "__pycache__", "node_modules",
    ".venv", "venv", "env", "dist", "build",
}

# 默认浏览器请求头：显著降低被目标站点直接拒绝的概率
_BROWSER_LIKE_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


# BOM 前缀 → 编码名（嗅探优先级最高，字节级无歧义）
_BOM_TABLE = (
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe\x00\x00", "utf-32-le"),
    (b"\x00\x00\xfe\xff", "utf-32-be"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
)

# 乱码特征：CJK 区字符与常见替换符。GBK 误按 UTF-8 解码会大量产生 CJK 扩展区生僻字，
# UTF-8 误按 GBK 解码则表现为成对"锟斤拷/烫烫烫"类噪声，两者都可用比例识别。
_REPLACEMENT_CHARS = ("\ufffd", "�")


def _mojibake_score(text: str) -> float:
    """估计一段文本的乱码程度（0=干净，越高越乱）。

    规则：
      - 替换符 � 计重罚；
      - CJK 兼容/扩展区生僻字（U+3400-4DBF、U+E000-F8FF、U+FE30-FE4F 等）计轻罚；
      - 正常常用汉字（U+4E00-9FFF）不算乱码。
    """
    if not text:
        return 0.0
    sample = text[:20000]
    penalty = 0
    for ch in sample:
        code = ord(ch)
        if ch in _REPLACEMENT_CHARS:
            penalty += 2
        elif 0x3400 <= code <= 0x4DBF or 0xE000 <= code <= 0xF8FF or 0xFE30 <= code <= 0xFE4F:
            penalty += 1
        elif 0x9FA6 <= code <= 0x9FFF:
            # U+9FA6-9FFF 属于 GBK 有映射但 Unicode 主区少用的字，GBK 串被 utf-8 硬解时高频出现
            penalty += 1
    return penalty / len(sample)


def _decode_bytes_used(data: bytes, encoding: str = "", candidates: tuple = ("utf-8", "gbk")) -> tuple:
    """把字节解码为文本，返回 (文本, 实际使用的编码)。

    优先级：显式 encoding > BOM 嗅探 > 多候选严格解码评分。
    多候选都能严格解码时（如 GBK 中文恰好构成合法 UTF-8 的情形），按乱码特征
    打分取最优，避免固定顺序导致的高概率互串乱码。
    显式指定 encoding 时直接按该编码宽松解码（保证不抛异常）。
    """
    if encoding:
        return data.decode(encoding, errors="replace"), encoding
    for bom, name in _BOM_TABLE:
        if data.startswith(bom):
            try:
                return data.decode(name), name
            except (UnicodeDecodeError, LookupError):
                break
    best_text, best_encoding, best_score = None, None, None
    for candidate in candidates:
        try:
            decoded = data.decode(candidate)
        except (UnicodeDecodeError, LookupError):
            continue
        score = _mojibake_score(decoded)
        if best_score is None or score < best_score:
            best_text, best_encoding, best_score = decoded, candidate, score
        if score == 0.0:
            break  # 已找到零乱码候选，无需继续比较
    if best_text is None:
        return data.decode(candidates[-1], errors="replace"), candidates[-1]
    return best_text, best_encoding


def _decode_bytes(data: bytes, encoding: str = "", candidates: tuple = ("utf-8", "gbk")) -> str:
    """同 _decode_bytes_used，但只返回文本。"""
    return _decode_bytes_used(data, encoding, candidates)[0]


def _command_output_candidates() -> tuple:
    """终端命令输出的候选编码：中文 Windows 的 cmd 默认代码页为 GBK，故优先尝试。"""
    return ("gbk", "utf-8") if os.name == "nt" else ("utf-8", "gbk")


def _is_probably_binary(data: bytes) -> bool:
    """前 8KB 中出现空字节即视为二进制文件。"""
    return b"\x00" in data[:8192]


def _truncate_text(text: str, limit: int, note: str = "内容过长已截断") -> str:
    """超限时保留头部 2/3 + 尾部 1/3，中间用提示行衔接。"""
    if len(text) <= limit:
        return text
    head = int(limit * 2 / 3)
    tail = max(0, limit - head)
    omitted = len(text) - head - tail
    return f"{text[:head]}\n...[{note}，中间约 {omitted} 字符已省略]...\n{text[-tail:]}"


def _resolve_dir_path(dir_path) -> Path:
    """解析目录参数：空串回退当前目录（生成任务中即会话工作目录）。"""
    text = str(dir_path).strip() if dir_path else ""
    candidate = Path(text).expanduser() if text else Path(".")
    if not candidate.exists():
        raise FileNotFoundError(f"路径不存在: {candidate}")
    if not candidate.is_dir():
        raise NotADirectoryError(f"不是目录: {candidate}")
    return candidate.resolve()


def _display_path(path: Path) -> str:
    """统一用 / 作为路径分隔符展示，避免模型转义反斜杠出错。"""
    return str(path).replace("\\", "/")


def _walk_entries(root: Path, max_depth: int):
    """遍历目录树，yield (路径, "file"|"dir")。
    跳过隐藏目录与 _SKIP_DIR_NAMES 中的目录；max_depth 为相对 root 的深度上限（0=仅当前目录）。"""
    root_depth = len(root.parts)
    for current, dir_names, file_names in os.walk(root):
        current_path = Path(current)
        depth = len(current_path.parts) - root_depth
        keep_dirs = sorted(
            name for name in dir_names
            if name not in _SKIP_DIR_NAMES and not name.startswith(".")
        )
        for name in keep_dirs:
            yield current_path / name, "dir"
        # 深度用尽时剪枝，不再下钻（目录本身已在上面列出）
        dir_names[:] = keep_dirs if depth < max_depth else []
        for name in sorted(file_names):
            yield current_path / name, "file"


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


def _strip_tags(fragment: str) -> str:
    """去掉片段内的 HTML 标签并压缩空白（用于标题/摘要）。"""
    text = re.sub(r"<[^>]+>", " ", fragment)
    return re.sub(r"\s+", " ", unescape(text)).strip()


_TAG_SPEC_PATTERN = re.compile(r"^([a-zA-Z][a-zA-Z0-9-]*)((?:[.#][\w-]+)*)$")
_ATTR_PATTERN = re.compile(r"([\w:-]+)\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)")


def _extract_tag_blocks(html_text: str, tag_spec: str) -> list:
    """按 "div"、"div#main"、"div.content" 形式的标签选择器抽取标签内部内容。
    说明：同名标签嵌套时按非贪婪最短块匹配，不做严格配对。"""
    match = _TAG_SPEC_PATTERN.match(tag_spec.strip())
    if not match:
        raise ValueError(f"tag 参数格式不合法: {tag_spec}（示例：div、div#main、div.content）")
    tag_name, selector_part = match.group(1).lower(), match.group(2)
    conditions = []
    for selector in re.findall(r"[.#][\w-]+", selector_part):
        conditions.append(("id", selector[1:]) if selector[0] == "#" else ("class", selector[1:]))
    block_pattern = re.compile(rf"<{tag_name}\b([^>]*)>(.*?)</{tag_name}\s*>", re.IGNORECASE | re.DOTALL)
    blocks = []
    for matched in block_pattern.finditer(html_text):
        attrs_text, inner = matched.group(1), matched.group(2)
        if conditions and not _attrs_match(attrs_text, conditions):
            continue
        blocks.append(inner.strip())
    return blocks


def _attrs_match(attrs_text: str, conditions: list) -> bool:
    attrs = {name.lower(): value.strip("\"'") for name, value in _ATTR_PATTERN.findall(attrs_text)}
    for key, expected in conditions:
        value = attrs.get(key, "")
        values = value.split() if key == "class" else [value]
        if expected not in values:
            return False
    return True


def _http_request(url: str, method: str = "GET", body: str = "", headers: Optional[dict] = None,
                  timeout_seconds: float = 30.0) -> tuple:
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
        with urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read()
            content_type = response.headers.get("Content-Type", "")
            # 按响应头/魔数解压：gzip(1f 8b)、deflate(zlib 头 78 xx 或裸 deflate)
            encoding_header = (response.headers.get("Content-Encoding") or "").lower().strip()
            if encoding_header in ("gzip", "deflate", "x-gzip", "zlib") or (
                    not encoding_header and (raw[:2] == b"\x1f\x8b" or raw[:1] == b"\x78")):
                try:
                    if raw[:2] == b"\x1f\x8b":
                        raw = gzip.decompress(raw)
                    else:
                        try:
                            raw = zlib.decompress(raw)          # zlib 包装的 deflate
                        except zlib.error:
                            raw = zlib.decompress(raw, -15)     # 裸 deflate 流
                except (OSError, zlib.error):
                    pass  # 解压失败则按原样返回，交由上层按文本处理
            return response.geturl(), response.status, content_type, raw
    except HTTPError as exc:
        detail = ""
        try:
            detail = exc.read(2000).decode("utf-8", errors="replace")
        except Exception:
            pass
        raise ValueError(f"HTTP {exc.code} {exc.reason}: {url}" + (f"\n{detail}" if detail else "")) from exc
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


# ============================ 工具实现 ============================

def _list_items_impl(dir_path, pattern, search_mode, max_depth) -> str:
    root = _resolve_dir_path(dir_path)
    mode = (search_mode or "all").lower()
    if mode not in ("file", "dir", "all"):
        raise ValueError(f"search_mode 仅支持 file/dir/all，当前为 {search_mode}")
    name_pattern = (pattern or "").strip()
    entries = []
    truncated = False
    for path, kind in _walk_entries(root, max(0, int(max_depth))):
        if mode == "file" and kind != "file":
            continue
        if mode == "dir" and kind != "dir":
            continue
        if name_pattern and not fnmatch.fnmatch(path.name, name_pattern):
            continue
        if len(entries) >= _LIST_DIR_MAX_ENTRIES:
            truncated = True
            break
        item = {"name": path.name, "type": kind, "path": _display_path(path)}
        if kind == "file":
            try:
                item["size"] = path.stat().st_size
            except OSError:
                item["size"] = None
        entries.append(item)
    payload = {
        "dir": _display_path(root),
        "search_mode": mode,
        "pattern": name_pattern or None,
        "total": len(entries),
        "truncated": truncated,
        "entries": entries,
    }
    text = json.dumps(payload, ensure_ascii=False)
    if truncated:
        text += f"\n[列表已达上限 {_LIST_DIR_MAX_ENTRIES} 条被截断；请用 pattern 过滤或缩小目录范围]"
    return text


def _read_file_impl(full_file_name, start_line, end_line, encoding, show_line_numbers, char_offset=0) -> str:
    path = Path(str(full_file_name)).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"{path} 不存在")
    if not path.is_file():
        raise IsADirectoryError(f"{path} 不是文件")
    data = path.read_bytes()
    if _is_probably_binary(data):
        return f"[read_file] {_display_path(path)} 疑似二进制文件（{len(data)} 字节），拒绝按文本读取"
    text, used_encoding = _decode_bytes_used(data, encoding)
    lines = text.splitlines()
    total = len(lines)
    start = max(1, int(start_line))
    end = total if not end_line or int(end_line) <= 0 else min(int(end_line), total)
    if start > total:
        return f"[read_file] start_line={start} 超出文件总行数 {total}，无内容可读"
    if end < start:
        return f"[read_file] end_line={end_line} 小于 start_line={start}，无内容可读"
    selected = lines[start - 1:end]
    limited = False
    if len(selected) > _READ_FILE_MAX_LINES:
        selected = selected[:_READ_FILE_MAX_LINES]
        limited = True
    # 单行超长（如压缩过的大 JSON/日志）时按字符分块返回，避免头尾截断丢失中间内容
    if len(selected) == 1 and len(selected[0]) > _READ_FILE_MAX_CHARS:
        line_text = selected[0]
        offset = max(0, int(char_offset or 0))
        chunk = line_text[offset:offset + _READ_FILE_MAX_CHARS]
        if not chunk:
            return f"[read_file] char_offset={offset} 已超出该行长度 {len(line_text)}，无内容可读"
        next_offset = offset + len(chunk)
        footer = (f"[read_file] {_display_path(path)} | 单行超长（共 {len(line_text)} 字符，编码 {used_encoding}）"
                  f" | 本次返回第 {offset + 1}-{next_offset} 字符")
        if next_offset < len(line_text):
            footer += f" | 未读完，请用 char_offset={next_offset} 继续读取"
        return chunk + "\n" + footer
    if show_line_numbers:
        width = len(str(start + len(selected) - 1))
        body = "\n".join(f"{no:>{width}}| {line}" for no, line in enumerate(selected, start=start))
    else:
        body = "\n".join(selected)
    char_limited = len(body) > _READ_FILE_MAX_CHARS
    if char_limited:
        body = _truncate_text(body, _READ_FILE_MAX_CHARS, "文件内容过长已截断")
    last_line = start + len(selected) - 1
    footer = f"[read_file] {_display_path(path)} | 共 {total} 行 | 本次第 {start}-{last_line} 行 | 编码 {used_encoding}"
    if last_line < total or limited or char_limited:
        footer += " | 未读完，可调整 start_line/end_line 继续读取"
    return body + "\n" + footer if body else footer


def _write_file_impl(full_file_name, content, encoding, append) -> str:
    path = Path(str(full_file_name)).expanduser()
    dir_path = path.parent if str(path.parent).strip() else Path(".")
    if not dir_path.exists():
        dir_path.mkdir(parents=True, exist_ok=True)
    mode = "a" if append else "w"
    with open(path, mode, encoding=encoding or "utf-8", newline="") as file:
        file.write(content)
    action = "追加" if append else "写入"
    return f"[write_file] 已{action} {len(content)} 字符 → {_display_path(path.resolve())}"


def _edit_file_impl(full_file_name, old_string, new_string, replace_all, encoding) -> str:
    if not str(old_string):
        raise ValueError("old_string 不能为空；如需清空文件请使用 write_file 写入空内容")
    path = Path(str(full_file_name)).expanduser()
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"{path} 不存在或不是文件")
    data = path.read_bytes()
    if _is_probably_binary(data):
        raise ValueError(f"{path} 疑似二进制文件，不支持文本编辑")
    original, used_encoding = _decode_bytes_used(data, encoding)
    eol = "\r\n" if "\r\n" in original else "\n"
    # 匹配在换行归一化文本上进行，写回时还原原文件的换行风格
    text = original.replace("\r\n", "\n").replace("\r", "\n")
    old_norm = str(old_string).replace("\r\n", "\n")
    new_norm = str(new_string).replace("\r\n", "\n")
    count = text.count(old_norm)
    if count == 0:
        # 0 匹配时给出最接近的候选行，帮助模型快速定位"凭记忆写错"的差异
        hint = ""
        close = difflib.get_close_matches(old_norm.strip(), [line.strip() for line in text.split("\n")], n=2, cutoff=0.6)
        if close:
            shown = " / ".join(f"「{c[:120]}」" for c in close)
            hint = f"；文件中最相似的行：{shown}（注意空格/缩进/全角差异）"
        raise ValueError(f"old_string 在文件中 0 处匹配；请先用 read_file 核对内容（空格/缩进/换行必须完全一致）{hint}")
    if count > 1 and not replace_all:
        raise ValueError(f"old_string 在文件中匹配到 {count} 处，存在歧义；请提供更长的上下文使其唯一，或传 replace_all=true 全部替换")
    updated = text.replace(old_norm, new_norm) if replace_all else text.replace(old_norm, new_norm, 1)
    with open(path, "w", encoding=used_encoding, newline="") as file:
        file.write(updated.replace("\n", eol))
    replaced = count if replace_all else 1
    style = "CRLF" if eol == "\r\n" else "LF"
    return f"[edit_file] 已在 {_display_path(path)} 中替换 {replaced} 处（换行风格 {style}，编码 {used_encoding}）"


def _format_match_block(path: Path, lines: list, hit_indexes: list, context: int) -> list:
    """把（相邻+上下文合并后的）命中区间格式化为 "路径:行号" 区块。"""
    ranges = []
    for idx in hit_indexes:
        if ranges and idx <= ranges[-1][1] + context + 1:
            ranges[-1][1] = idx
        else:
            ranges.append([idx, idx])
    blocks = []
    for start, end in ranges:
        low = max(0, start - context)
        high = min(len(lines) - 1, end + context)
        header = f"{_display_path(path)}:{start + 1}" + (f"-{end + 1}" if end != start else "")
        body = []
        for no in range(low, high + 1):
            marker = ">" if start <= no <= end else " "
            body.append(f"{marker}{no + 1}| {lines[no][:300]}")
        blocks.append(header + "\n" + "\n".join(body))
    return blocks


def _search_files_impl(pattern, dir_path, file_pattern, ignore_case, is_regex,
                       context_lines, max_results, max_depth, max_file_mb=2) -> str:
    pattern = (pattern or "").strip()
    if not pattern:
        raise ValueError("pattern 不能为空")
    root = _resolve_dir_path(dir_path)
    flags = re.IGNORECASE if ignore_case else 0
    if is_regex:
        try:
            regex = re.compile(pattern, flags)
        except re.error as exc:
            raise ValueError(f"正则表达式不合法: {exc}")
    else:
        regex = re.compile(re.escape(pattern), flags)
    context = max(0, min(int(context_lines), 5))
    limit = max(1, min(int(max_results), 200))
    name_filter = (file_pattern or "").strip()
    try:
        size_limit = float(max_file_mb) if max_file_mb else 0.0
    except (TypeError, ValueError):
        size_limit = 2.0
    if size_limit <= 0:
        size_limit = 0.0
    blocks = []
    total_matches = 0
    files_scanned = 0
    files_matched = 0
    skipped_large = 0
    for path, kind in _walk_entries(root, max(0, int(max_depth))):
        if kind != "file":
            continue
        if name_filter and not fnmatch.fnmatch(path.name, name_filter):
            continue
        try:
            file_size = path.stat().st_size
            if size_limit and file_size > size_limit * 1024 * 1024:
                skipped_large += 1
                continue  # 跳过超过大小上限的文件
            data = path.read_bytes()
        except OSError:
            continue
        if _is_probably_binary(data):
            continue
        files_scanned += 1
        lines = _decode_bytes(data).splitlines()
        hit_indexes = [idx for idx, line in enumerate(lines) if regex.search(line)]
        if not hit_indexes:
            continue
        files_matched += 1
        total_matches += len(hit_indexes)
        if len(blocks) < limit:
            room = limit - len(blocks)
            block_group = _format_match_block(path, lines, hit_indexes, context)
            blocks.extend(block_group[:room])
    header = (f"[search_files] 正则: {regex.pattern} | 目录: {_display_path(root)}"
              f" | 扫描 {files_scanned} 个文本文件，命中 {files_matched} 个文件 / {total_matches} 处")
    if skipped_large:
        header += f" | 跳过 {skipped_large} 个超过 {size_limit:g}MB 的文件（可用 max_file_mb 调整）"
    if not blocks:
        return header + "\n（无匹配结果）"
    result = [header] + blocks
    if len(blocks) >= limit:
        result.append(f"[已达到 max_results={limit} 上限；可缩小目录/加 file_pattern 过滤或提高 max_results]")
    return "\n".join(result)


_SHELL_ALIASES = {
    "auto": "auto", "": "auto",
    "cmd": "cmd", "cmd.exe": "cmd", "batch": "cmd",
    "powershell": "powershell", "powershell.exe": "powershell", "ps": "powershell", "ps1": "powershell",
    "pwsh": "pwsh", "pwsh.exe": "pwsh", "powershell7": "pwsh",
    "bash": "bash", "sh": "sh", "zsh": "zsh", "dash": "sh",
}


def _resolve_shell(shell: str, is_nt: bool) -> tuple:
    """把 shell 名称解析为 (exe, shell 标识, 额外 argv 前缀)。

    - auto: Windows → cmd.exe；非 Windows → bash 可用则 bash，否则 sh；
    - 返回 exe 为绝对路径时走 Popen 列表形式，否则仅作为提示由 shell=True 使用。
    - 找不到可执行文件时抛 ValueError。
    """
    key = str(shell or "auto").strip().lower()
    kind = _SHELL_ALIASES.get(key)
    if kind is None:
        raise ValueError(
            f"shell 仅支持 auto/cmd/powershell/pwsh/bash/sh/zsh，当前为 {shell!r}")
    if kind == "auto":
        if is_nt:
            return os.environ.get("COMSPEC") or "cmd.exe", "cmd", []
        bash_path = shutil.which("bash")
        return (bash_path or "/bin/sh"), ("bash" if bash_path else "sh"), []
    if kind == "cmd":
        if not is_nt:
            raise ValueError("shell=cmd 仅在 Windows 上可用；Linux/macOS 请使用 bash/sh/zsh")
        return os.environ.get("COMSPEC") or "cmd.exe", "cmd", []
    if kind == "powershell":
        if not is_nt:
            raise ValueError("shell=powershell 仅在 Windows 上可用（非 Windows 请使用 pwsh/bash/sh）")
        exe = shutil.which("powershell") or "powershell.exe"
        return exe, "powershell", ["-NoProfile", "-NonInteractive", "-Command"]
    if kind == "pwsh":
        exe = shutil.which("pwsh") or ("pwsh.exe" if is_nt else None)
        if not exe or not shutil.which(exe) and not Path(exe).exists():
            raise ValueError("未找到 PowerShell 7 (pwsh)；可安装 https://aka.ms/powershell 或改用 shell=powershell/bash")
        return exe, "pwsh", ["-NoProfile", "-NonInteractive", "-Command"]
    # bash / sh / zsh
    exe = shutil.which(kind)
    if not exe:
        exe = f"/bin/{kind}" if Path(f"/bin/{kind}").exists() else None
    if not exe:
        hint = "Windows 上可通过 Git for Windows / WSL 获取 bash" if is_nt else f"系统未安装 {kind}"
        raise ValueError(f"未找到 {kind} 可执行文件（{hint}）")
    if is_nt:
        # Windows 侧的 bash（Git Bash/WSL 登录 shell）：-c 形式最稳，避免 MSYS 路径转换干扰
        return exe, kind, ["-c"]
    return exe, kind, ["-c"]


_BG_LOG_DIR = Path.home() / ".mcp_bg_logs"
# 后台日志文件名序号：同一进程同一秒内多次调用时保证文件名唯一
_BG_NAME_SEQ = itertools.count(1)


def _prune_old_bg_files() -> None:
    """清理后台日志目录中超过 24 小时的旧文件（忽略一切错误）。"""
    try:
        cutoff = time.time() - 24 * 3600
        for item in _BG_LOG_DIR.glob("bg_*"):
            try:
                if item.stat().st_mtime < cutoff:
                    item.unlink()
            except OSError:
                continue
    except OSError:
        pass


def _pid_alive_windows(pid: int) -> bool:
    """用 OpenProcess+GetExitCodeProcess 判断进程是否仍在运行。"""
    import ctypes
    from ctypes import wintypes
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    exit_code = wintypes.DWORD()
    ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
    kernel32.CloseHandle(handle)
    return bool(ok) and exit_code.value == STILL_ACTIVE


def _wmi_create_process(command_line: str) -> int:
    """通过 WMI Win32_Process.Create 启动进程，返回新进程 PID。

    新进程的父进程是系统服务（WmiPrvSE），完全脱离本工具的进程树，
    因此不受宿主环境"命令结束后清理整棵进程树"的影响。
    优先用 pywin32 COM（无额外进程开销）；未安装时回退 PowerShell Invoke-CimMethod。
    """
    try:
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        try:
            wmi = win32com.client.GetObject(
                "winmgmts:{impersonationLevel=impersonate}!//./root/cimv2")
            proc_class = wmi.Get("Win32_Process")
            in_params = proc_class.Methods_("Create").InParameters.SpawnInstance_()
            in_params.Properties_("CommandLine").Value = command_line
            result = wmi.ExecMethod("Win32_Process", "Create", in_params)
            code = result.Properties_("ReturnValue").Value
            if code != 0:
                raise ValueError(f"WMI 创建进程失败（Win32_Process.Create 返回码 {code}）")
            pid = int(result.Properties_("ProcessId").Value)
            # 先释放全部 COM 引用再 CoUninitialize，避免"IUnknown 释放异常"噪音
            del result, in_params, proc_class, wmi
            return pid
        finally:
            pythoncom.CoUninitialize()
    except ImportError:
        pass
    # 回退：PowerShell（-EncodedCommand 避免引号转义问题）
    ps_script = (
        "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
        f"-Arguments @{{CommandLine='{command_line.replace(chr(39), chr(39) * 2)}'}}; "
        "if ($r.ReturnValue -ne 0) { Write-Error ('WMI错误码 ' + $r.ReturnValue); exit 1 } "
        "else { Write-Output $r.ProcessId }")
    encoded = base64.b64encode(ps_script.encode("utf-16-le")).decode("ascii")
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        capture_output=True, timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    text = completed.stdout.decode(_command_output_candidates()[0], errors="replace").strip()
    if completed.returncode != 0 or not text.isdigit():
        detail = completed.stderr.decode(errors="replace").strip()[:300]
        raise ValueError(f"WMI 回退启动失败（exit={completed.returncode}）: {detail or text}")
    return int(text)


def _run_command_background(command, work_dir, shell_exe, shell_kind, shell_prefix, is_nt) -> str:
    """后台分离模式：以脱离主进程的方式启动命令，立即返回 PID 与日志路径。

    Windows：把命令写入启动器批处理（含 cd 与输出重定向），再用 WMI
    Win32_Process.Create 启动 —— 新进程父进程是系统服务 WmiPrvSE，
    彻底脱离本 MCP 进程树，不受"MCP 结束即清理子进程树"的影响；
    POSIX：start_new_session=True（setsid）建立新会话，摆脱父进程组与控制终端。
    stdout/stderr 合并写入 ~/.mcp_bg_logs/bg_时间戳.log，之后可用 read_file 查看输出。
    """
    try:
        _BG_LOG_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ValueError(f"无法创建后台日志目录 {_BG_LOG_DIR}: {exc}")
    _prune_old_bg_files()
    stamp = time.strftime("%Y%m%d_%H%M%S")
    seq = next(_BG_NAME_SEQ) % 10000
    log_path = _BG_LOG_DIR / f"bg_{stamp}_{seq:04d}_{os.getpid()}.log"
    posix_prefix = " ".join(shell_prefix) if shell_prefix else ""
    if is_nt:
        # 启动器批处理：切目录 + 执行命令并合并重定向输出；mbcs(ANSI) 编码保证中文路径可被 cmd 解析
        effective_dir = work_dir or os.getcwd()
        launcher_path = _BG_LOG_DIR / f"bg_{stamp}_{seq:04d}_{os.getpid()}.cmd"
        launcher_text = (
            "@echo off\r\n"
            f'cd /d "{effective_dir}"\r\n'
            f"{command} > \"{log_path}\" 2>&1\r\n"
        )
        try:
            launcher_path.write_text(launcher_text, encoding="mbcs")
        except (OSError, LookupError):
            launcher_path.write_text(launcher_text, encoding="utf-8", errors="replace")
        # 启动器路径由我们生成（无空格），直接传路径即可，避免引号被 list2cmdline 二次转义
        popen_args = [shell_exe, "/c", str(launcher_path)]
        use_shell = False
    else:
        log_file = open(log_path, "wb")
        try:
            proc = subprocess.Popen(
                [*([shell_exe, posix_prefix, command] if posix_prefix else [command])],
                shell=not posix_prefix, cwd=work_dir,
                stdin=subprocess.DEVNULL,  # 防交互：等待输入的命令立即得到 EOF 而非挂起
                stdout=log_file, stderr=subprocess.STDOUT,
                start_new_session=True,  # 新会话：脱离父进程组与控制终端
            )
        finally:
            # 子进程已继承自己的句柄副本，父进程随即关闭不影响其继续写日志
            log_file.close()
        pid = proc.pid
    if is_nt:
        try:
            pid = _wmi_create_process(
                subprocess.list2cmdline(popen_args) if isinstance(popen_args, list) else popen_args)
        except Exception as exc:
            raise ValueError(f"后台命令启动失败: {exc}")
    time.sleep(0.5)  # 短暂观察，捕捉"秒退"的命令（如命令拼写错误）
    alive = (_pid_alive_windows(pid) if is_nt else proc.poll() is None)
    parts = [
        "[run_command] 已以后台分离模式启动（独立于本 MCP 进程运行，服务结束/超时不会被关闭）",
        f"PID: {pid}" + ("（外层 shell 的进程 id）" if is_nt else ""),
        f"shell: {shell_kind} | 工作目录: {work_dir or os.getcwd()}",
        f"启动时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"命令: {command}",
        f"日志文件: {_display_path(log_path)}（stdout+stderr 合并写入原始字节）",
    ]
    if alive:
        parts.append("状态: 运行中（启动 0.5s 后仍存活）；稍后用 read_file 读取日志查看输出与结果")
    else:
        parts.append("状态: 进程已结束（快速命令可能已正常完成）；请用 read_file 读日志查看结果或报错")
    parts.append(
        '管理: Windows 用 tasklist /FI "PID eq <PID>" 查存活、taskkill /F /T /PID <PID> 结束；'
        "Linux/macOS 用 ps -p <PID> / kill <PID>；后台模式忽略 timeout_seconds 与 encoding 参数")
    return "\n".join(parts)


def _run_command_impl(command, timeout_seconds, cwd, encoding, shell="auto", background=False) -> str:
    command = str(command or "").strip()
    if not command:
        raise ValueError("command 不能为空")
    try:
        timeout = min(max(float(timeout_seconds), 1.0), 240.0)
    except (TypeError, ValueError):
        timeout = 45.0
    work_dir = None
    if cwd and str(cwd).strip():
        candidate = Path(str(cwd).strip()).expanduser()
        if not candidate.is_dir():
            raise NotADirectoryError(f"cwd 不是有效目录: {candidate}")
        work_dir = str(candidate)
    is_nt = os.name == "nt"
    shell_exe, shell_kind, shell_prefix = _resolve_shell(shell, is_nt)
    if background:
        return _run_command_background(command, work_dir, shell_exe, shell_kind, shell_prefix, is_nt)
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if is_nt else 0
    if shell_prefix:
        # 直接启动指定 shell：[exe, "-c", command] / powershell [-Command] 形式
        popen_args = [shell_exe, *shell_prefix, command]
        use_shell = False
    else:
        popen_args = command
        use_shell = True
    try:
        proc = subprocess.Popen(
            popen_args, shell=use_shell, cwd=work_dir,
            stdin=subprocess.DEVNULL,  # 防交互：等待输入的命令立即得到 EOF 而非挂起
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=creationflags,
        )
    except OSError as exc:
        raise ValueError(f"命令启动失败: {exc}")
    started = time.perf_counter()
    timed_out = False
    try:
        out_bytes, err_bytes = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        if os.name == "nt":
            # 杀掉整个进程树（shell=True 时 cmd 及其子进程都要清理）
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, creationflags=creationflags)
        proc.kill()
        out_bytes, err_bytes = proc.communicate()
    elapsed = time.perf_counter() - started
    candidates = _command_output_candidates()
    stdout_text = _decode_bytes(out_bytes or b"", encoding, candidates)
    stderr_text = _decode_bytes(err_bytes or b"", encoding, candidates)
    parts = [f"[run_command] exit_code={proc.returncode} | 耗时 {elapsed:.1f}s | shell={shell_kind}"
             + (" | 命令超时被强制终止" if timed_out else "")]
    if stdout_text.strip():
        parts.append("[stdout]\n" + _truncate_text(stdout_text.rstrip(), _RUN_COMMAND_MAX_CHARS, "命令输出过长已截断"))
    if stderr_text.strip():
        parts.append("[stderr]\n" + _truncate_text(stderr_text.rstrip(), 4000, "stderr 过长已截断"))
    if not stdout_text.strip() and not stderr_text.strip():
        parts.append("（命令无输出）")
    return "\n".join(parts)


def _fetch_url_impl(url, mode, pattern, tag, max_chars, timeout_seconds,
                    encoding, method, body, headers) -> str:
    mode = (mode or "text").lower()
    if mode not in ("text", "html", "tag", "regex"):
        raise ValueError("mode 仅支持 text/html/tag/regex")
    if mode == "tag" and not (tag or "").strip():
        raise ValueError("mode=tag 时必须提供 tag 参数（示例：div、div#main、div.content）")
    if mode == "regex" and not (pattern or "").strip():
        raise ValueError("mode=regex 时必须提供 pattern 参数")
    regex = None
    if mode == "regex":
        try:
            regex = re.compile(str(pattern))
        except re.error as exc:
            raise ValueError(f"正则表达式不合法: {exc}")
    try:
        timeout = min(max(float(timeout_seconds), 1.0), 120.0)
    except (TypeError, ValueError):
        timeout = 30.0
    final_url, status, content_type, raw = _http_request(
        url, method=method or "GET", body=body or "", headers=headers, timeout_seconds=timeout)
    if encoding:
        charset = encoding
        html_text = raw.decode(charset, errors="replace")
    else:
        # 声明编码与实际内容不符的站点兜底：按乱码评分在候选编码中择优
        declared = _pick_web_charset(content_type, raw, "")
        candidates = [declared] + [alt for alt in ("utf-8", "gbk") if alt.lower() != declared.lower()]
        best_text, best_charset, best_score = None, declared, None
        for candidate in candidates:
            try:
                decoded = raw.decode(candidate)
            except (UnicodeDecodeError, LookupError):
                continue
            score = _mojibake_score(decoded)
            if best_score is None or score < best_score:
                best_text, best_charset, best_score = decoded, candidate, score
            if score == 0.0:
                break
        html_text = best_text if best_text is not None else raw.decode(declared, errors="replace")
        charset = best_charset
    title = ""
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html_text, re.IGNORECASE | re.DOTALL)
    if title_match:
        title = _strip_tags(title_match.group(1))
    if mode == "text":
        content = _html_to_text(html_text)
    elif mode == "html":
        content = html_text
    elif mode == "tag":
        blocks = _extract_tag_blocks(html_text, str(tag))
        content = "\n----\n".join(blocks)
    else:
        # regex 模式：单条匹配展示上限 = max_chars/4，钳制在 [200, 2000]（默认 8000 → 每条 2000 字符）
        per_match_cap = max(200, min(2000, (int(max_chars) if max_chars else _FETCH_URL_DEFAULT_CHARS) // 4))
        matches = []
        for found in regex.finditer(html_text):
            piece = (" | ".join(g if g is not None else "" for g in found.groups())
                     if found.groups() else found.group(0))
            matches.append(piece[:per_match_cap])
            if len(matches) >= 200:
                break
        content = "\n".join(matches)
    limit = min(max(int(max_chars) if max_chars else _FETCH_URL_DEFAULT_CHARS, 200), 100000)
    original_len = len(content)
    content = _truncate_text(content, limit, "网页内容过长已截断")
    header = (f"[fetch_url] HTTP {status} | {str(final_url)}\n"
              f"[fetch_url] Content-Type: {content_type or '未知'} | 编码: {charset}"
              f" | 原始 {len(raw)} 字节 | 提取后 {original_len} 字符")
    if title:
        header += f" | 标题: {title}"
    notes = []
    if mode == "tag" and not content:
        notes.append("未匹配到目标标签，可改用 mode=regex 或调整 tag 选择器")
    if original_len > limit:
        notes.append(f"内容超过 max_chars={limit} 已截断；可用 pattern/tag 抽取更小范围或调大 max_chars")
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
    """请求并解析 Bing 网页版搜索结果，返回 [(标题, 链接, 摘要)]。"""
    search_url = "https://www.bing.com/search?" + urlencode(
        {"q": query, "count": min(limit * 3, 30), "mkt": "zh-CN"})
    _, _, content_type, raw = _http_request(search_url, timeout_seconds=15)
    charset = _pick_web_charset(content_type, raw, "")
    html_text = raw.decode(charset, errors="replace")
    collected = []
    for block in _split_bing_blocks(html_text):
        item = _bing_block_result(block)
        if item:
            collected.append(item)
        if len(collected) >= limit * 2:
            break
    return _dedupe_results(collected, limit)


def _fetch_ddg_results(query: str, limit: int) -> list:
    """请求并解析 DuckDuckGo HTML 版搜索结果，返回 [(标题, 链接, 摘要)]。

    按文档顺序扫描 result__a（标题/链接）与 result__snippet（摘要）锚点，
    摘要归属其前面最近的一个标题，避免可选分组跨结果错配。
    """
    ddg_url = "https://html.duckduckgo.com/html/?" + urlencode({"q": query})
    _, _, content_type, raw = _http_request(ddg_url, timeout_seconds=15)
    charset = _pick_web_charset(content_type, raw, "")
    html_text = raw.decode(charset, errors="replace")
    collected = []
    current = None
    for anchor in re.finditer(
            r'<a[^>]+class="result__(a|snippet)"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
            html_text, re.IGNORECASE | re.DOTALL):
        kind, href, inner = anchor.group(1), anchor.group(2), anchor.group(3)
        if kind == "a":
            if current:
                collected.append(tuple(current))
            url = unquote(href)
            # DDG 重定向链接 //duckduckgo.com/l/?uddg=<真实URL>
            uddg = re.search(r"uddg=([^&]+)", url)
            if uddg:
                url = unquote(uddg.group(1))
            current = [_strip_tags(inner), url, ""] if url.startswith("http") else None
        elif current is not None:
            current[2] = _strip_tags(inner)[:300]
    if current:
        collected.append(tuple(current))
    return _dedupe_results(collected, limit)


def _web_search_impl(query, max_results, engine="auto") -> str:
    query = str(query or "").strip()
    if not query:
        raise ValueError("query 不能为空")
    limit = max(1, min(int(max_results) if max_results else 8, _WEB_SEARCH_MAX_RESULTS))
    engine_key = str(engine or "auto").strip().lower()
    if engine_key in ("ddg", "duckduckgo"):
        engine_key = "ddg"
    elif engine_key not in ("auto", "bing"):
        raise ValueError("engine 仅支持 auto/bing/ddg（duckduckgo）")

    results, engine_name = [], ""
    if engine_key in ("auto", "bing"):
        try:
            results = _fetch_bing_results(query, limit)
            engine_name = "Bing 网页版解析"
        except (ValueError, OSError):
            results = []  # Bing 异常时自动走 DuckDuckGo 回退，而不是直接失败
        if not results and engine_key == "bing":
            raise ValueError(
                "Bing 未解析到结果（站点改版或访问受限）。可改用 engine=ddg 重试，"
                "或直接用 fetch_url 打开 https://www.bing.com/search?q=关键词 自行提取。")
    if not results:
        try:
            results = _fetch_ddg_results(query, limit)
            engine_name = "DuckDuckGo（Bing 无结果/不可达或显式指定）"
        except (ValueError, OSError) as exc:
            detail = f"（{exc}）" if engine_key == "ddg" else ""
            raise ValueError(
                "Bing 与 DuckDuckGo 均未解析到结果（站点改版或访问受限）" + detail +
                "。可直接用 fetch_url 打开 https://www.bing.com/search?q=关键词 ，"
                "用 mode=text/regex 自行提取。") from exc
    lines = [f"[web_search] 查询: {query} | 共 {len(results)} 条结果（{engine_name}，链接正文可用 fetch_url 抓取）"]
    for index, (title, url, snippet) in enumerate(results, start=1):
        lines.append(f"{index}. {title}\n   链接: {url}" + (f"\n   摘要: {snippet}" if snippet else ""))
    return "\n".join(lines)


# ============================ 工具注册 ============================

@sys_mcp_server.tool()
async def list_items(dir_path: str = ".", pattern: str = "", search_mode: str = "all", max_depth: int = 1) -> str:
    """列出目录下的文件与子目录，返回 JSON 列表
    参数：
        dir_path:    目录路径，默认为当前目录（生成任务中即会话工作目录）；相对/绝对路径均可
        pattern:     文件名通配符过滤（fnmatch 语法），例如 *.py、data_??.json；空串表示不过滤
        search_mode: 过滤类型，all=文件+目录（默认）、file=仅文件、dir=仅目录
        max_depth:   递归深度，0=仅当前目录，1=含一级子目录，以此类推；默认 1
    返回：
        JSON 字符串：{"dir", "search_mode", "pattern", "total", "truncated", "entries"}；
        每个 entry 含 name/type（file|dir）/path（绝对路径，分隔符为 /）/size（仅文件）。
        超过 1000 条时截断并附提示；隐藏目录与 node_modules/__pycache__/.git 等目录自动跳过
    """
    return await asyncio.to_thread(_list_items_impl, dir_path, pattern, search_mode, max_depth)


# ---------------- 已迁移为后端内置工具（factory/agent_runtime/builtin_tools.py） ----------------
# read_file / write_file / edit_file / search_files 四个文件读写检索工具已转为
# 主项目内置可选工具（服务端本地执行，结果结构化、为文件 diff 预留），不再由
# 本 MCP 服务注册暴露；下方注册块整体注释保留，需要回滚时取消注释即可。
# 注意：_read_file_impl 等辅助函数仍被保留（仅本文件历史引用，无运行时调用）。

# @sys_mcp_server.tool()
# async def read_file(full_file_name: str, start_line: int = 1, end_line: int = 0,
#                     encoding: str = "", show_line_numbers: bool = True, char_offset: int = 0) -> str:
#     """读取文本文件内容（自动尝试 utf-8/gbk 编码，带行号与读取进度提示）
#     参数：
#         full_file_name:    文件路径，相对路径基于会话工作目录；传入目录会报错
#         start_line:        起始行（从 1 开始），默认 1
#         end_line:          结束行（包含该行），0 或负数表示读到文件末尾；默认 0
#         encoding:          指定文件编码（如 utf-8、gbk）；留空自动尝试 utf-8 → gbk
#         show_line_numbers: 是否带 "行号| 内容" 前缀显示，默认 true；行号可用于后续 edit_file 定位
#         char_offset:       单行超长时的字符偏移（按返回的 char_offset 提示续读）；普通文件忽略该参数
#     返回：
#         文件内容；末尾附 [read_file] 摘要（总行数/本次范围/编码），未读完会提示分段读取。
#         单次最多返回 2000 行；二进制文件拒绝读取；
#         单行超过 6 万字符（如压缩大 JSON）时自动转为字符分块模式，按提示的 char_offset 续读
#     """
#     return await asyncio.to_thread(
#         _read_file_impl, full_file_name, start_line, end_line, encoding, show_line_numbers, char_offset)


# @sys_mcp_server.tool()
# async def write_file(full_file_name: str, content: str, encoding: str = "utf-8", append: bool = False) -> str:
#     """写入文本文件（整文件覆盖或追加，自动创建多级目录）
#     参数：
#         full_file_name: 文件路径；不存在则创建，父目录不存在自动创建；相对路径基于会话工作目录
#         content:        要写入的完整内容（覆盖模式会清空原内容）
#         encoding:       文件编码，默认 utf-8
#         append:         true=在文件末尾追加；false（默认）=整文件覆盖
#     返回：
#         写入结果说明（含绝对路径与字符数）；只改文件局部内容时优先用 edit_file
#     """
#     return await asyncio.to_thread(_write_file_impl, full_file_name, content, encoding, append)


# @sys_mcp_server.tool()
# async def edit_file(full_file_name: str, old_string: str, new_string: str,
#                     replace_all: bool = False, encoding: str = "") -> str:
#     """按精确字符串替换编辑文件（类似查找替换，比按行号改写更安全）
#     参数：
#         full_file_name: 文件路径，相对路径基于会话工作目录
#         old_string:     要被替换的原文，必须与文件内容完全一致（建议从 read_file 的输出复制）
#         new_string:     替换后的新内容；传空串表示删除 old_string
#         replace_all:    old_string 出现多次时是否全部替换；默认 false（多处匹配会报错并提示加长上下文）
#         encoding:       指定文件编码（如 gbk）；留空自动尝试 utf-8 → gbk，并按读到的编码写回
#     返回：
#         替换结果说明（替换处数/换行风格/编码）；old_string 为 0 处或多处歧义时报错
#     """
#     return await asyncio.to_thread(
#         _edit_file_impl, full_file_name, old_string, new_string, replace_all, encoding)


# @sys_mcp_server.tool()
# async def search_files(pattern: str, dir_path: str = ".", file_pattern: str = "",
#                        ignore_case: bool = False, is_regex: bool = True,
#                        context_lines: int = 0, max_results: int = 50, max_depth: int = 6,
#                        max_file_mb: float = 2) -> str:
#     """跨文件正则搜索（类似 grep）：在目录下的文本文件中逐行匹配
#     参数：
#         pattern:       正则表达式（is_regex=false 时按普通文本查找）
#         dir_path:      搜索根目录，默认当前目录（会话工作目录）
#         file_pattern:  文件名通配符过滤，例如 *.py、*.md；空串表示所有文件
#         ignore_case:   是否忽略大小写，默认 false
#         is_regex:      pattern 是否按正则解析，默认 true；false 时按字面文本匹配
#         context_lines: 每处匹配附带上下文行数（0-5），默认 0
#         max_results:   最多返回的匹配区块数（1-200），默认 50
#         max_depth:     递归深度，0=仅当前目录；默认 6
#         max_file_mb:   单文件大小上限 MB（默认 2；设 0 表示不限制，可搜索大 JSON/日志）
#     返回：
#         "文件路径:行号" 区块列表，> 前缀标记命中行；头部附扫描/命中统计。
#         自动跳过二进制文件、隐藏目录及 node_modules/__pycache__/.git 等目录；
#         超过 max_file_mb 的文件默认跳过并在头部统计中提示
#     """
#     return await asyncio.to_thread(
#         _search_files_impl, pattern, dir_path, file_pattern, ignore_case, is_regex,
#         context_lines, max_results, max_depth, max_file_mb)
# ---------------- 内置工具迁移注释结束 ----------------


@sys_mcp_server.tool()
async def run_command(command: str, timeout_seconds: float = 60.0, cwd: str = "", encoding: str = "",
                      shell: str = "auto", background: bool = False) -> str:
    """执行一条终端命令并返回退出码与输出（支持多 shell：cmd/powershell/pwsh/bash/sh/zsh）
    参数：
        command:         命令字符串；支持管道与重定向（由所选 shell 解释）
        timeout_seconds: 超时秒数（1-240，默认 60）；超时强制杀掉整个进程树
        cwd:             命令工作目录；留空使用当前目录（会话工作目录）
        encoding:        指定输出编码（如 gbk、utf-8）；留空自动尝试（Windows 先 gbk 后 utf-8），乱码时请显式指定
        shell:           解释器（默认 auto=Windows 用 cmd，Linux/macOS 优先 bash，无则 sh）；
                         可选 cmd / powershell（Win 自带 5.1）/ pwsh（PowerShell 7）/ bash / sh / zsh
        background:      后台分离模式（默认 false）；true 时命令在完全脱离本 MCP 进程的子进程中运行，
                         不受 MCP 结束/超时影响，立即返回进程 PID 与日志文件路径（stdout+stderr 合并写入该日志）；
                         之后用 read_file 查看输出；适合服务器/监听器/长任务等长期驻留程序。
                         该模式忽略 timeout_seconds 与 encoding 参数
    返回：
         "[run_command] exit_code=... | 耗时 ... | shell=..." 头部 + [stdout]/[stderr] 分段（超长自动截断）；
         background=true 时返回 "[run_command] 已以后台分离模式启动" + PID/启动时间/日志路径/状态/管理命令提示
    注意：
        非零退出码不算工具错误，会作为正常结果返回，请根据 exit_code 判断成败；
        stdin 已重定向为空（DEVNULL），等待交互输入的命令会立即得到 EOF 而非挂起；
        powershell/pwsh 以 -NoProfile -NonInteractive 运行；bash/sh/zsh 以 -c 运行；
        非后台模式下仍建议避免交互式或长期驻留命令（如进入 REPL、启动服务器），它们会等到超时被杀
    选型指引（与持久终端工具的关系）：
        需要跨调用保留 shell 状态（cd/环境变量）、实时观察输出、交互式确认 → 用持久终端三件套；
        跑完即走的一次性脚本/构建/查询 → 用本工具；长期驻留的服务器/监听器 → background=true
    """
    return await asyncio.to_thread(
        _run_command_impl, command, timeout_seconds, cwd, encoding, shell, background)


@sys_mcp_server.tool()
async def fetch_url(url: str, mode: str = "text", pattern: str = "", tag: str = "",
                    max_chars: int = 0, timeout_seconds: float = 30.0, encoding: str = "",
                    method: str = "GET", body: str = "", headers: Optional[dict] = None) -> str:
    """抓取网页或 HTTP 接口内容：支持纯文本提取、按标签/正则抽取，超长自动截断
    参数：
        url:             完整链接，仅支持 http/https
        mode:            提取模式：text=正文纯文本（默认）、html=原始 HTML、tag=按标签抽取、regex=按正则抽取
        pattern:         mode=regex 时必填，在原始 HTML 上执行的正则（可用 (?s) 跨行匹配）；
                         每条匹配展示长度与 max_chars 联动（不再固定 500 字符）
        tag:             mode=tag 时必填，标签选择器，支持 div、div#id、div.class（同名标签嵌套时按最短块匹配）
        max_chars:       返回内容最大字符数（200-100000）；默认 8000
        timeout_seconds: 请求超时秒数（1-120），默认 30
        encoding:        指定响应编码（如 gbk）；留空按 BOM/响应头/meta 自动判断
        method:          HTTP 方法，默认 GET；POST 时配合 body 使用
        body:            请求体（POST 等使用），默认按 application/json 发送
        headers:         额外请求头字典，例如 {"Authorization": "Bearer xxx"}；覆盖默认浏览器头中的同名项
    返回：
        头部（状态码/最终 URL/Content-Type/标题/长度）+ 提取的内容；
        内容过长会截断并提示，网页太大时建议用 tag/regex 只抽取需要的部分
    """
    return await asyncio.to_thread(
        _fetch_url_impl, url, mode, pattern, tag, max_chars, timeout_seconds,
        encoding, method, body, headers)


@sys_mcp_server.tool()
async def web_search(query: str, max_results: int = 8, engine: str = "auto") -> str:
    """网页搜索：解析 Bing 网页版结果（DuckDuckGo 自动回退），返回标题/链接/摘要
    参数：
        query:       搜索关键词
        max_results: 最多返回条数（1-20），默认 8
        engine:      搜索引擎：auto=Bing 失败/无结果时自动回退 DuckDuckGo（默认）、
                     bing=仅用 Bing（失败即报错）、ddg=仅用 DuckDuckGo
    返回：
        编号结果列表，每条含标题、链接、摘要；头部标注实际使用的引擎；
        需要正文时把链接交给 fetch_url 抓取
    说明：
        无 API Key，通过解析搜索引擎网页版实现；已做 Bing 重定向解包、广告过滤与 URL 去重；
        站点改版或访问受限时可能失败并给出替代建议
    """
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
