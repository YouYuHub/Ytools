"""Messages image/document conversion and request limits.

Public history keeps the application's attachment format. Conversion happens on a
request copy, including native tool replay, without modifying signed prefixes.
"""
import base64
import binascii
import copy
import io
import json
from pathlib import Path
from urllib.parse import urlsplit

from PIL import Image, ImageOps

from .errors import MessagesProtocolError

IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".xml", ".html", ".log"}
DOCUMENT_EXTENSIONS = {".pdf", *TEXT_EXTENSIONS}
MAX_REQUEST_BYTES = 32 * 1024 * 1024
MAX_IMAGE_BYTES = 10 * 1024 * 1024  # Base64-encoded size, not decoded byte size.


def supported_document_types(declared):
    """Never advertise arbitrary binary Office formats as native documents."""
    return [item for item in (declared or []) if str(item).lower() in DOCUMENT_EXTENSIONS]


def _url(value):
    if not isinstance(value, str):
        raise MessagesProtocolError("附件 URL 必须为字符串")
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise MessagesProtocolError("Messages 附件需要有效的 http(s) URL 或 base64 数据")
    return value


def _decode(data):
    if not isinstance(data, str) or not data:
        raise MessagesProtocolError("附件 base64 数据为空或格式无效")
    if len(data) > MAX_REQUEST_BYTES:
        raise MessagesProtocolError("附件 base64 已超过 Messages 32MB 请求体上限")
    try:
        return base64.b64decode(data, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise MessagesProtocolError("附件 base64 数据格式无效") from exc


def data_source(value):
    if not isinstance(value, str) or not value.startswith("data:"):
        raise MessagesProtocolError("文档 file_data 需要包含 MIME 类型的 base64 data URL")
    header, separator, data = value.partition(",")
    if not separator or not header.lower().endswith(";base64"):
        raise MessagesProtocolError("附件 data URL 仅支持 base64 编码")
    media_type = header[5:].split(";", 1)[0].lower()
    return {"type": "base64", "media_type": media_type, "data": data}


def prepare_image(data, media_type, *, max_edge=None, quality=85):
    """Validate pixels; convert unsupported formats and optionally shrink uploads.

    GIF is a still image for Messages. Decode only its first frame and send PNG,
    rather than running the Chat Completions GIF-to-video pipeline.
    """
    try:
        with Image.open(io.BytesIO(data)) as image:
            original_format = image.format
            image.seek(0)
            image.load()
            image = ImageOps.exif_transpose(image)
            resized = max_edge is not None and max(image.size) > max_edge
            if resized:
                image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
            actual = Image.MIME.get(original_format, "")
            has_alpha = "A" in image.getbands() or "transparency" in image.info
            if actual in IMAGE_TYPES and actual != "image/gif" and not resized:
                encoded = base64.b64encode(data).decode("ascii")
                return {"type": "base64", "media_type": actual, "data": encoded}
            output = io.BytesIO()
            if resized and not has_alpha:
                image.convert("RGB").save(output, format="JPEG", quality=quality)
                actual = "image/jpeg"
            else:
                image.convert("RGBA" if has_alpha else "RGB").save(output, format="PNG")
                actual = "image/png"
            return {"type": "base64", "media_type": actual,
                    "data": base64.b64encode(output.getvalue()).decode("ascii")}
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise MessagesProtocolError("附件无法解码为完整图片：" + str(exc)) from exc


def _source(source, kind):
    if not isinstance(source, dict):
        raise MessagesProtocolError(kind + " 缺少 source 对象")
    source = copy.deepcopy(source)
    source_type = source.get("type")
    if source_type == "url":
        return {"type": "url", "url": _url(source.get("url"))}
    if source_type == "file":
        file_id = source.get("file_id")
        if not isinstance(file_id, str) or not file_id.strip():
            raise MessagesProtocolError("Files API 引用缺少 file_id")
        return {"type": "file", "file_id": file_id}
    if kind == "document" and source_type == "text":
        if source.get("media_type") != "text/plain" or not isinstance(source.get("data"), str):
            raise MessagesProtocolError("纯文本文档需要 text/plain 和字符串 data")
        return {"type": "text", "media_type": "text/plain", "data": source["data"]}
    if kind == "document" and source_type == "content":
        content = source.get("content")
        if isinstance(content, str):
            return {"type": "content", "content": content}
        blocks = content_blocks(content)
        if not blocks or any(block["type"] not in {"text", "image"} for block in blocks):
            raise MessagesProtocolError("自定义文档 content 仅支持文本和图片")
        return {"type": "content", "content": blocks}
    if source_type != "base64":
        raise MessagesProtocolError("不支持的 Messages 附件 source: " + str(source_type))
    media_type = str(source.get("media_type") or "").lower()
    if kind == "image":
        if len(source.get("data") or "") > MAX_IMAGE_BYTES:
            raise MessagesProtocolError("单张图片 base64 超过 Messages 10MB 上限；请缩小图片后重试")
        data = _decode(source.get("data"))
        # Convert BMP/TIFF etc. supplied through the application's image_url.
        return prepare_image(data, media_type)
    if media_type != "application/pdf":
        raise MessagesProtocolError("Messages 原生二进制文档仅支持 PDF；Word/Excel 请使用文本解析")
    data = _decode(source.get("data"))
    if not data.startswith(b"%PDF-"):
        raise MessagesProtocolError("PDF 内容缺少有效的 PDF 文件头")
    return {"type": "base64", "media_type": media_type, "data": source["data"]}


def _metadata(part, keys):
    result = {}
    for key in keys:
        if part.get(key) is not None:
            result[key] = copy.deepcopy(part[key])
    return result


def _file_block(part):
    file = part.get("file") or part
    name = str(file.get("filename") or "")
    if file.get("file_id"):
        source = {"type": "file", "file_id": file["file_id"]}
    elif file.get("file_url") or file.get("url"):
        source = {"type": "url", "url": file.get("file_url") or file["url"]}
    else:
        source = data_source(file.get("file_data"))
        if source["media_type"] != "application/pdf":
            suffix = Path(name).suffix.lower()
            if not source["media_type"].startswith("text/") and suffix not in TEXT_EXTENSIONS:
                raise MessagesProtocolError("Messages 不支持该原生文档格式：" + (name or source["media_type"]))
            from util.text_encoding import decode_text
            source = {"type": "text", "media_type": "text/plain", "data": decode_text(_decode(source["data"]))[0]}
    return {"type": "document", "source": _source(source, "document"),
            **({"title": name} if name else {}),
            **_metadata(part, ("title", "context", "citations", "cache_control"))}


def content_blocks(content, *, role="user", session_id=None):
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if content is None:
        return []
    if not isinstance(content, list):
        raise MessagesProtocolError("消息 content 必须为字符串或内容块数组")
    result = []
    for part in content:
        if isinstance(part, str):
            if part:
                result.append({"type": "text", "text": part})
            continue
        if not isinstance(part, dict):
            raise MessagesProtocolError("消息内容块必须为 JSON 对象")
        kind = part.get("type")
        if kind == "text":
            if not isinstance(part.get("text"), str):
                raise MessagesProtocolError("text 内容块需要字符串 text")
            if part["text"]:
                result.append({"type": "text", "text": part["text"],
                               **_metadata(part, ("cache_control", "citations"))})
            continue
        if kind in {"input_audio", "video_url"}:
            result.append({"type": "text", "text": "[未发送附件：Messages API 不支持原生音频/视频输入。]"})
            continue
        if role != "user":
            raise MessagesProtocolError("图片和文档只能放在 Messages user 消息或工具结果内")
        if kind == "image_url":
            image = part.get("image_url")
            url = image.get("url") if isinstance(image, dict) else image
            if isinstance(url, str) and url.startswith("media://") and session_id:
                from memory.file_memory import load_session_media_model_part
                info = load_session_media_model_part(session_id, url, api_type="messages")
                if not info or info.get("error"):
                    raise MessagesProtocolError("图片引用读取失败：" + str((info or {}).get("error") or url))
                url = info["part"]["image_url"]["url"]
            source = data_source(url) if isinstance(url, str) and url.startswith("data:") else {"type": "url", "url": url}
            result.append({"type": "image", "source": _source(source, "image"),
                           **_metadata(part, ("cache_control",))})
        elif kind in {"image", "document"}:
            keys = ("cache_control", "transformations") if kind == "image" else ("title", "context", "citations", "cache_control")
            result.append({"type": kind, "source": _source(part.get("source"), kind), **_metadata(part, keys)})
        elif kind in {"file", "input_file"}:
            result.append(_file_block(part))
        else:
            raise MessagesProtocolError("不支持的 Messages 输入内容块：" + str(kind))
    return result


def tool_result_content(content, *, session_id=None):
    if isinstance(content, list):
        return content_blocks(content, session_id=session_id)
    return content if isinstance(content, str) else json.dumps(content, ensure_ascii=False) if content is not None else ""


def walk_blocks(messages):
    """Include attachments nested in tool results and custom documents."""
    for message in messages:
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            yield block
            nested = block.get("content") if block.get("type") == "tool_result" else None
            if block.get("type") == "document" and block.get("source", {}).get("type") == "content":
                nested = block["source"]["content"]
            if isinstance(nested, list):
                yield from walk_blocks([{"content": nested}])


def validate_payload(payload, config):
    """Check the actual replay payload. Never trim a signature-bound prefix."""
    window = config.get("maxInputTokens") or config.get("max_input_tokens") or 200000
    limits = config.get("messagesLimits") or {}
    if not isinstance(limits, dict):
        raise MessagesProtocolError("messagesLimits 必须为对象")

    def cap(key, default):
        value = limits.get(key, default)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise MessagesProtocolError("messagesLimits." + key + " 必须为正整数")
        return min(value, default)

    image_limit = cap("max_images", 100 if int(window) <= 200000 else 600)
    page_limit = cap("max_pdf_pages", 100 if int(window) < 1000000 else 600)
    blocks = list(walk_blocks(payload["messages"]))
    images = [block for block in blocks if block["type"] == "image"]
    from util.media_limits import get_media_limits
    quotas = get_media_limits(config)
    counts = {"image": len(images), "document": sum(block["type"] == "document" for block in blocks)}
    counts["total"] = counts["image"] + counts["document"]
    for kind, count in counts.items():
        if kind in quotas and count > quotas[kind]:
            raise MessagesProtocolError(f"Messages 实际请求 {kind} 数量 {count} 超过配置配额 {quotas[kind]}；请减少附件或重建上下文")
    if len(images) > image_limit:
        raise MessagesProtocolError(f"Messages 图片数量 {len(images)} 超过单次请求上限 {image_limit}")
    pages = 0
    for block in blocks:
        kind, source = block.get("type"), block.get("source") or {}
        if kind == "image" and source.get("type") == "base64":
            if len(source["data"]) > cap("max_image_bytes", MAX_IMAGE_BYTES):
                raise MessagesProtocolError("Messages 单张图片 base64 超过大小上限")
            data = _decode(source["data"])
            with Image.open(io.BytesIO(data)) as image:
                edge = cap("max_image_dimension", 2000 if len(images) > 20 else 8000)
                if max(image.size) > edge:
                    raise MessagesProtocolError(f"Messages 图片尺寸超过 {edge}px；请缩小图片后重试")
        if kind == "document":
            if source.get("type") == "base64":
                data = _decode(source["data"])
                try:
                    import fitz
                except ImportError:
                    if b"/Encrypt" in data:
                        raise MessagesProtocolError("Messages 不支持加密 PDF")
                else:
                    try:
                        with fitz.open(stream=data, filetype="pdf") as document:
                            if document.is_encrypted:
                                raise MessagesProtocolError("Messages 不支持加密 PDF")
                            pages += document.page_count
                    except MessagesProtocolError:
                        raise
                    except Exception as exc:
                        raise MessagesProtocolError("PDF 无法解析：" + str(exc)) from exc
            citations = block.get("citations")
            if citations is not None and (not isinstance(citations, dict) or not isinstance(citations.get("enabled"), bool)):
                raise MessagesProtocolError("document.citations.enabled 必须为布尔值")
    if pages > page_limit:
        raise MessagesProtocolError(f"Messages PDF 总页数 {pages} 超过单次请求上限 {page_limit}；请使用 read_document 分页读取")
    documents = [block for block in blocks if block["type"] == "document"]
    enabled = [bool((block.get("citations") or {}).get("enabled")) for block in documents]
    if any(enabled) and not all(enabled):
        raise MessagesProtocolError("同一次请求中的所有文档必须同时启用或关闭 citations")
    if any(enabled) and ((payload.get("output_config") or {}).get("format") or payload.get("output_format")):
        raise MessagesProtocolError("文档 citations 与结构化输出 format 不能同时启用")
    size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    maximum = cap("max_request_bytes", MAX_REQUEST_BYTES)
    if size > maximum:
        raise MessagesProtocolError(f"Messages 请求体 {size / 1048576:.1f}MB 超过 {maximum / 1048576:g}MB 上限；请减少附件或分批读取")
