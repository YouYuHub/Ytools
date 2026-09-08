""" 记录当前轮次对话上传的文件信息，持久化到项目 history 文件夹中 """
# from __future__ import annotations
# 标准库
import base64
import json
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
import io
# 第三方库
from PIL import Image
# 自定义模块
from util.timestamp_utils import DEFAULT_TIMESTAMP_FORMAT, now_str as _timestamp_now

HISTORY_ROOT = Path(__file__).resolve().parents[1] / "history_files" / "upload"
HISTORY_ROOT.mkdir(parents=True, exist_ok=True)


# ---------- 多媒体附件（图片/音频/视频） ----------
# 聊天多模态消息的二进制文件存储：原始字节保存在
# history_files/upload/<session>/media/ 下，聊天消息 content 列表里用
# "media://<stored_name>" 引用，发送上游前由 resolve_media_content_parts
# 解析为 OpenAI 兼容格式（image_url.url = data URL；input_audio.data = base64）。

MEDIA_URL_SCHEME = "media://"
MEDIA_DIRECTORY_NAME = "media"
# 单文件大小上限按媒体类别区分：视频文件普遍较大（500MB），图片/音频 20MB
MEDIA_SIZE_LIMITS = {
    "image": 20 * 1024 * 1024,
    "audio": 20 * 1024 * 1024,
    "video": 500 * 1024 * 1024,
}
MEDIA_MAX_FILE_SIZE = MEDIA_SIZE_LIMITS["image"]  # 兼容旧引用：图片/音频默认上限

# ---------- 大图缩略图 ----------
# 超过该字节数的图片在发送上游前自动降采样为 JPEG 缩略图（原图仍完整落盘，
# 预览/下载不受影响）：视觉 API 按分辨率计费，全屏截图动辄数 MB，直接发
# base64 会显著浪费 token；长边 1568 是主流视觉模型的高清档分辨率。
IMAGE_THUMBNAIL_THRESHOLD_BYTES = 2 * 1024 * 1024
IMAGE_THUMBNAIL_MAX_EDGE = 1568
IMAGE_THUMBNAIL_JPEG_QUALITY = 85
# 动图转 JPEG 会丢帧，跳过缩略图按原图发送
_IMAGE_THUMBNAIL_SKIP_EXTENSIONS = {".gif"}
_THUMB_DIRECTORY_NAME = "thumbs"

# 主流视觉模型原生接受的图片格式：PNG/JPEG/GIF/WebP（OpenAI 系），
# Qwen-VL/GLM-4V/Gemini 等另普遍支持 BMP/TIFF
_IMAGE_MEDIA_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
                           ".ico", ".tif", ".tiff"}
_AUDIO_MEDIA_EXTENSIONS = {".wav", ".mp3", ".m4a", ".ogg", ".flac"}
_VIDEO_MEDIA_EXTENSIONS = {".mp4", ".webm", ".mov", ".mkv"}

# 视觉模型不原生接受、落盘前自动转为 PNG 的图片格式（需 Pillow）：
# ICO（模型基本不支持）、TIFF（部分模型支持，转 PNG 通用性最好）
_PNG_CONVERSION_EXTENSIONS = {".ico", ".tif", ".tiff"}

# content 部件类型 -> 历史回放/压缩时的占位文案
MEDIA_PART_LABELS = {
    "image_url": "[图片]",
    "input_audio": "[音频]",
    "video_url": "[视频]",
}

# ---------- 可解析文档的原始字节（用于点击预览/下载） ----------
# 上传解析流程历来只保存解析后的文本 JSON；原始文件（如 PDF）字节另存一份在
# history_files/upload/<session>/files/ 下，前端消息/附件区的文档可点击预览。

DOC_DIRECTORY_NAME = "files"

def _safe_session_id(session_id: str) -> str:
    """与 memory.chat_memory._safe_session_id 保持一致：无损保留中文等合法文件名字符。

    仅把路径分隔符等危险字符替换为下划线，并拦截 `..` 防止路径穿越。
    """
    value = re.sub(r"[^\w .-]+", "_", session_id)
    value = value.replace("..", "_")
    return value.strip(" ._-")


def _get_session_dir(session_id: str) -> Path:
    safe_id = _safe_session_id(session_id)
    return HISTORY_ROOT / safe_id


def get_upload_dir_name(session_id: str) -> str:
    """返回该会话上传文件所在目录名（history_files/upload/ 下的文件夹名）。

    上传目录按上传时前端传入的 session_id 命名（`_safe_session_id` 后的结果），
    可能与聊天会话文件名不一致，因此调用方会把它记录到会话 _meta.upload_id，
    供删除会话时连带清理上传目录。
    """
    return _safe_session_id(session_id)


def _safe_filename(filename: str) -> str:
    filename = Path(filename).name
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", filename)
    return safe_name.strip("_.-") or "uploaded_file"


def _get_history_path(session_id: str, filename: str) -> Path:
    session_dir = _get_session_dir(session_id)
    session_dir.mkdir(parents=True, exist_ok=True)
    safe_name = _safe_filename(filename)
    return session_dir / f"{safe_name}.json"


def _write_json_file(file_path: Path, data: dict[str, Any]) -> None:
    with file_path.open("w", encoding="utf-8") as fp:
        fp.write(json.dumps(data, ensure_ascii=False, indent=2))


def _read_json_file(file_path: Path) -> dict[str, Any] | None:
    if not file_path.exists():
        return None
    try:
        with file_path.open("r", encoding="utf-8") as fp:
            return json.load(fp)
    except (json.JSONDecodeError, OSError):
        return None


def _get_record_timestamp(file_path: Path) -> float:
    data = _read_json_file(file_path)
    if not data:
        try:
            return float(file_path.stat().st_mtime)
        except OSError:
            return 0.0
    timestamp = data.get("timestamp")
    if isinstance(timestamp, str):
        try:
            return datetime.strptime(timestamp, DEFAULT_TIMESTAMP_FORMAT).timestamp()
        except ValueError:
            pass
    try:
        return float(file_path.stat().st_mtime)
    except OSError:
        return 0.0


def _list_session_files(session_id: str) -> list[Path]:
    session_dir = _get_session_dir(session_id)
    if not session_dir.exists():
        return []
    files = list(session_dir.glob("*.json"))
    files.sort(key=_get_record_timestamp)
    return files


def _convert_image_bytes_to_png(data: bytes) -> bytes:
    """把 ICO/TIFF 等视觉模型不原生接受的图片字节转为 PNG。

    需要 Pillow（requirements.txt）；透明通道保留，CMYK 等少数模式
    先转 RGBA 再存。转换失败由调用方转为明确的用户提示。
    """
    with Image.open(io.BytesIO(data)) as img:
        img.load()
        out = io.BytesIO()
        try:
            img.save(out, format="PNG")
        except OSError:
            # PNG 不支持的模式（如 CMYK）：转 RGBA 后重试
            converted = img.convert("RGBA")
            converted.save(out, format="PNG")
    return out.getvalue()


def _media_dir(session_id: str) -> Path:
    directory = _get_session_dir(session_id) / MEDIA_DIRECTORY_NAME
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _thumb_dir(session_id: str) -> Path:
    directory = _get_session_dir(session_id) / _THUMB_DIRECTORY_NAME
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _load_image_thumbnail_base64(session_id: str, path: Path) -> tuple[str, str] | None:
    """为大图生成/复用 JPEG 缩略图，返回 (mime, base64)；失败返回 None（回退原图）。

    - 触发条件由调用方判断（文件字节数超过 IMAGE_THUMBNAIL_THRESHOLD_BYTES）；
    - 缩略图缓存到 <session>/thumbs/<原名>.thumb.jpg，同图多次解析只算一次，
      原图更新（字节变化）时按 mtime+size 失效重建；
    - 长边压到 IMAGE_THUMBNAIL_MAX_EDGE、JPEG 质量 IMAGE_THUMBNAIL_JPEG_QUALITY；
    - 任何失败（Pillow 不支持/磁盘异常）都返回 None，调用方回退原图发送。
    """
    thumb_path = _thumb_dir(session_id) / f"{path.name}.thumb.jpg"
    try:
        stat = path.stat()
        fingerprint = f"{stat.st_size}-{int(stat.st_mtime)}"
        if thumb_path.is_file():
            cached = thumb_path.read_text(encoding="ascii", errors="ignore").split("\n", 1)
            if len(cached) == 2 and cached[0] == fingerprint and cached[1]:
                return "image/jpeg", cached[1]
        with Image.open(path) as img:
            img.load()
            width, height = img.size
            max_edge = max(width, height)
            if max_edge > IMAGE_THUMBNAIL_MAX_EDGE:
                scale = IMAGE_THUMBNAIL_MAX_EDGE / max_edge
                new_size = (max(1, round(width * scale)), max(1, round(height * scale)))
                img = img.resize(new_size, Image.LANCZOS)
            if img.mode not in ("RGB", "L"):
                # JPEG 无透明通道：透明区域铺白底，避免黑底突兀
                rgba = img.convert("RGBA")
                background = Image.new("RGB", rgba.size, (255, 255, 255))
                background.paste(rgba, mask=rgba.split()[3])
                img = background
            out = io.BytesIO()
            img.save(out, format="JPEG", quality=IMAGE_THUMBNAIL_JPEG_QUALITY)
        encoded = base64.b64encode(out.getvalue()).decode("ascii")
        # 指纹与数据同行存储：命中校验 + 缓存内容一次读取
        thumb_path.write_text(f"{fingerprint}\n{encoded}", encoding="ascii")
        return "image/jpeg", encoded
    except Exception:
        return None


def _doc_dir(session_id: str) -> Path:
    directory = _get_session_dir(session_id) / DOC_DIRECTORY_NAME
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _media_data_url(mime: str, base64_data: str) -> str:
    return f"data:{mime};base64,{base64_data}"


def _is_media_reference(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(MEDIA_URL_SCHEME)



def media_kind(filename: str) -> str | None:
    """按扩展名判断媒体类别：image / audio / video；非媒体返回 None。"""
    extension = Path(filename).suffix.lower()
    if extension in _IMAGE_MEDIA_EXTENSIONS:
        return "image"
    if extension in _AUDIO_MEDIA_EXTENSIONS:
        return "audio"
    if extension in _VIDEO_MEDIA_EXTENSIONS:
        return "video"
    return None


def media_mime_type(filename: str) -> str:
    """按扩展名推断 MIME 类型；未知类型回退 application/octet-stream。"""
    import mimetypes

    extension = Path(filename).suffix.lower()
    if extension == ".webp":
        return "image/webp"
    if extension == ".mp3":
        return "audio/mpeg"
    if extension == ".m4a":
        return "audio/mp4"
    if extension == ".ico":
        return "image/x-icon"
    if extension in (".tif", ".tiff"):
        return "image/tiff"
    guessed = mimetypes.guess_type("x" + extension)[0]
    return guessed or "application/octet-stream"


def media_size_limit(kind: str | None) -> int:
    """按媒体类别返回单文件大小上限；未知类别按图片（最小）处理。"""
    return MEDIA_SIZE_LIMITS.get(kind or "", MEDIA_MAX_FILE_SIZE)


def save_session_document(session_id: str, filename: str, data: bytes) -> dict[str, Any]:
    """保存可解析文档的原始字节，返回 stored_name 供前端预览/下载引用。"""
    suffix = Path(filename).suffix.lower()
    base = _safe_filename(Path(filename).stem) or "document"
    stored_name = f"{base}_{uuid.uuid4().hex[:8]}{suffix}"
    path = _doc_dir(session_id) / stored_name
    path.write_bytes(data)
    return {
        "stored_name": stored_name,
        "mime": media_mime_type(stored_name),
        "size": len(data),
    }


def resolve_document_path(session_id: str, stored_name: str) -> Path | None:
    """把文档 stored_name 解析为原始文件路径；非法引用或不存在返回 None。"""
    if not isinstance(stored_name, str):
        return None
    name = stored_name.strip()
    if not name or "/" in name or "\\" in name or ".." in name:
        return None
    path = _doc_dir(session_id) / name
    if not path.is_file():
        return None
    return path


def resolve_media_path(session_id: str, reference: str) -> Path | None:
    """把 media:// 引用解析为媒体文件路径；非法引用或文件不存在返回 None。

    引用只允许纯文件名（会话内 media/ 目录下），拒绝任何路径分隔与穿越。
    """
    if not isinstance(reference, str):
        return None
    name = reference[len(MEDIA_URL_SCHEME):].strip() if reference.startswith(MEDIA_URL_SCHEME) else reference.strip()
    if not name or "/" in name or "\\" in name or ".." in name:
        return None
    path = _media_dir(session_id) / _safe_filename(name)
    if not path.is_file():
        return None
    return path


def save_session_media(session_id: str, filename: str, data: bytes) -> dict[str, Any]:
    """保存一个多媒体附件的原始字节，返回供前端构造消息部件的引用信息。

    ICO/TIFF 等视觉模型不原生接受的图片格式会先自动转为 PNG 再落盘，
    上游模型始终收到它支持的格式；转换失败时给出明确错误。
    """
    kind = media_kind(filename)
    if kind is None:
        raise ValueError(f"不支持的媒体类型: {filename}")
    size_limit = media_size_limit(kind)
    if len(data) > size_limit:
        raise ValueError(f"文件大小超过限制（{kind} 最大 {size_limit // (1024 * 1024)}MB）")
    # 扩展名取自原始文件名（_safe_filename 仅保留 ASCII，中文文件名的扩展名会丢）
    suffix = Path(filename).suffix.lower()
    if kind == "image" and suffix in _PNG_CONVERSION_EXTENSIONS:
        try:
            data = _convert_image_bytes_to_png(data)
        except Exception as convert_error:
            raise ValueError(
                f"{filename} 为视觉模型不原生支持的格式（{suffix}），"
                f"自动转换为 PNG 失败: {convert_error}"
            ) from convert_error
        suffix = ".png"
    base = _safe_filename(Path(filename).stem) or "media"
    # 同名覆盖会串图：加短随机后缀保证唯一
    stored_name = f"{base}_{uuid.uuid4().hex[:8]}{suffix}"
    path = _media_dir(session_id) / stored_name
    path.write_bytes(data)
    return {
        "filename": filename,
        "stored_name": stored_name,
        "media_ref": MEDIA_URL_SCHEME + stored_name,
        "kind": kind,
        "mime": media_mime_type(stored_name),
        "size": len(data),
    }


def load_session_media_base64(session_id: str, reference: str) -> tuple[str, str] | None:
    """读取媒体文件，返回 (mime, base64)；文件不存在返回 None。"""
    path = resolve_media_path(session_id, reference)
    if path is None:
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return media_mime_type(path.name), base64.b64encode(data).decode("ascii")


def save_session_media_stream(session_id: str, filename: str, file_obj: Any) -> dict[str, Any]:
    """流式保存大媒体文件（如 500MB 视频），边拷贝边计数、超限即中止。

    与 save_session_media 的区别：不把整个文件读入内存，也不做格式转换
    （视频无需转码，直接按原始格式存储）。`file_obj` 为二进制文件对象。
    """
    kind = media_kind(filename)
    if kind is None:
        raise ValueError(f"不支持的媒体类型: {filename}")
    size_limit = media_size_limit(kind)
    suffix = Path(filename).suffix.lower()
    base = _safe_filename(Path(filename).stem) or "media"
    stored_name = f"{base}_{uuid.uuid4().hex[:8]}{suffix}"
    path = _media_dir(session_id) / stored_name
    written = 0
    try:
        with path.open("wb") as out:
            while True:
                chunk = file_obj.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > size_limit:
                    raise ValueError(
                        f"文件大小超过限制（{kind} 最大 {size_limit // (1024 * 1024)}MB）"
                    )
                out.write(chunk)
    except Exception:
        # 中止时清理半成品，避免残留损坏文件
        try:
            path.unlink()
        except OSError:
            pass
        raise
    return {
        "filename": filename,
        "stored_name": stored_name,
        "media_ref": MEDIA_URL_SCHEME + stored_name,
        "kind": kind,
        "mime": media_mime_type(stored_name),
        "size": written,
    }


def resolve_media_content_parts(session_id: str, content: Any) -> tuple[Any, list[str]]:
    """把消息 content 列表里的 media:// 引用解析为上游 API 兼容格式。

    - ``image_url.url`` / ``video_url.url`` 等 URL 字段：解析为
      ``data:<mime>;base64,<b64>``（url 与 base64 双格式中的 base64 形态）；
    - ``input_audio.data``：解析为纯 base64（OpenAI 音频格式不要 data: 前缀）；
    - 其余部件（纯文本、http(s) URL、已内联 data:）原样保留。

    返回 (解析后的 content, 未成功解析的引用列表)。content 为字符串时原样返回。
    """
    if not isinstance(content, list):
        return content, []
    resolved_parts: list[Any] = []
    unresolved: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            resolved_parts.append(part)
            continue
        new_part = dict(part)
        # URL 类部件：image_url / video_url 等以 _url 结尾的键
        for key, value in list(new_part.items()):
            if not (key.endswith("_url") and isinstance(value, dict)):
                continue
            url = value.get("url")
            if _is_media_reference(url):
                loaded = load_session_media_base64(session_id, url)
                if loaded is None:
                    unresolved.append(url)
                else:
                    mime, base64_data = loaded
                    # 大图降采样：超过阈值的图片改发缩略图（原图完整落盘不受影响），
                    # 缩略图生成失败或动图（GIF）按原图发送
                    try:
                        media_path = resolve_media_path(session_id, url)
                        oversized = (
                            media_path is not None
                            and media_path.stat().st_size > IMAGE_THUMBNAIL_THRESHOLD_BYTES
                            and Path(media_path.name).suffix.lower() not in _IMAGE_THUMBNAIL_SKIP_EXTENSIONS
                        )
                    except OSError:
                        oversized = False
                    if oversized and media_path is not None:
                        thumb = _load_image_thumbnail_base64(session_id, media_path)
                        if thumb is not None:
                            mime, base64_data = thumb
                    new_part[key] = {**value, "url": _media_data_url(mime, base64_data)}
        # 音频部件：input_audio.data 需要纯 base64
        audio = new_part.get("input_audio")
        if isinstance(audio, dict) and _is_media_reference(audio.get("data")):
            loaded = load_session_media_base64(session_id, audio["data"])
            if loaded is None:
                unresolved.append(audio["data"])
            else:
                mime, base64_data = loaded
                new_part["input_audio"] = {**audio, "data": base64_data, "format": audio.get("format") or mime}
        resolved_parts.append(new_part)
    return resolved_parts, unresolved


def resolve_message_media_refs(session_id: str, messages: list[Any]) -> list[str]:
    """就地解析一组消息里的媒体引用（仅当前轮消息调用，历史轮次不解析）。

    返回未成功解析的引用列表；单个解析异常不影响其余部件。
    """
    unresolved_all: list[str] = []
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        resolved, unresolved = resolve_media_content_parts(session_id, content)
        message["content"] = resolved
        unresolved_all.extend(unresolved)
    return unresolved_all


def content_part_to_text(content: Any, include_media_labels: bool = True) -> str:
    """把消息 content 规整为纯文本（历史回放/压缩/标题等文本口径统一入口）。

    - 字符串原样返回；
    - 多部件列表抽取 text 部件；媒体部件默认替换为 [图片]/[音频]/[视频] 占位
      （历史回放用，提示模型该轮发过媒体），`include_media_labels=False`
      时跳过媒体部件（轮次问题/会话标题用，只要纯文本）；
    - None 返回空串。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
                continue
            if not isinstance(part, dict):
                continue
            part_type = str(part.get("type") or "")
            if part_type == "text" and isinstance(part.get("text"), str):
                parts.append(part["text"])
                continue
            if include_media_labels:
                label = MEDIA_PART_LABELS.get(part_type)
                if label:
                    parts.append(label)
        return "\n".join(part for part in parts if part.strip())
    return str(content)


class FileMemoryManager:
    """文件上传历史管理器 - 持久化到文件，支持多会话隔离"""

    def __init__(self, session_id: str):
        self._lock = threading.Lock()
        self._session_id = session_id
        self._session_dir = _get_session_dir(session_id)
        self._session_dir.mkdir(parents=True, exist_ok=True)

    def add_file_memory(self, file_info: dict[str, Any], max_items: int = 10) -> str:
        """
        将解析后的文件信息保存为单独 JSON 文件，并可按数量保留历史
        Args:
            file_info: 文件信息字典，包含 filename, type, content, size 等字段
            max_items: 最多保存的历史文件数，超过时删除最旧文件
        Returns:
            操作结果字符串
        """
        if not file_info.get("filename"):
            raise ValueError("file_info 必须包含 filename")
        record = {
            "timestamp": _timestamp_now()
        }
        record.update(file_info)
        with self._lock:
            file_path = _get_history_path(self._session_id, file_info["filename"])
            _write_json_file(file_path, record)
            if max_items and max_items > 0:
                all_files = _list_session_files(self._session_id)
                if len(all_files) > max_items:
                    for old_file in all_files[: len(all_files) - max_items]:
                        old_file.unlink(missing_ok=True)
        return f"保存成功: {file_path.name}"

    def get_file_memory_all(self, number: int = -1) -> list[dict[str, Any]]:
        """
        获取最近文件上传历史列表
        Args:
            number: 获取最后的指定数量，-1 表示所有
        Returns:
            文件历史记录列表
        """
        if not hasattr(number, "__int__"):
            raise ValueError("number 参数必须为整数")
        with self._lock:
            files = _list_session_files(self._session_id)
            entries: list[dict[str, Any]] = []
            for file_path in reversed(files):
                data = _read_json_file(file_path)
                if data is not None:
                    entries.append(data)
        return entries[:number] if number > 0 else entries

    def get_file_memory_chat(self, number: int = -1) -> list[dict[str, Any]]:
        """
        获取最近文件上传历史列表
        Args:
            number: 获取最后的指定数量，-1 表示所有
        Returns:
            文件历史记录列表
        """
        if not hasattr(number, "__int__"):
            raise ValueError("number 参数必须为整数")
        with self._lock:
            files = _list_session_files(self._session_id)
            entries: list[dict[str, Any]] = []
            for file_path in reversed(files):
                data = _read_json_file(file_path)
                if data is not None:
                    entries.append({
                        "filename": data.get("filename", "unknown"),
                        "content": data.get("content", ""),
                    })
        return entries[:number] if number > 0 else entries

    def get_file_memory_text(self, number: int = -1, max_total_chars: int = 3000) -> str:
        """
        获取最近文件历史的纯文本摘要，用于LLM上下文
        Args:
            number: 获取的记录数量，-1 表示全部
            max_total_chars: 最大返回字符数
        Returns:
            纯文本格式的文件历史摘要
        """
        records = self.get_file_memory_all(number)
        summary_parts = []
        total_chars = 0
        for record in records:
            content = record.get("content", "")
            if not content:
                continue
            part = (
                f"文件: {record.get('filename', '')} | "
                f"类型: {record.get('type', '')} | "
                f"大小: {record.get('size', '')} | "
                f"内容: {content}"
            )
            if total_chars + len(part) > max_total_chars:
                remaining = max_total_chars - total_chars
                if remaining <= 0:
                    break
                summary_parts.append(part[:remaining])
                break
            summary_parts.append(part)
            total_chars += len(part)
        return "\n".join(summary_parts)

    def clear_file_memory(self) -> str:
        """
        清空当前会话的所有文件上传历史
        Returns:
            操作结果字符串
        """
        with self._lock:
            for file_path in _list_session_files(self._session_id):
                file_path.unlink(missing_ok=True)
        return "清空成功"

    def delete_file_memory(self, filename: str) -> int:
        """
        删除当前会话中指定文件名的历史文件记录
        Args:
            filename: 原始上传文件名
        Returns:
            删除的文件数量
        """
        safe_name = _safe_filename(filename)
        file_path = _get_history_path(self._session_id, filename)
        deleted_count = 0
        with self._lock:
            if file_path.exists():
                file_path.unlink(missing_ok=True)
                deleted_count = 1
            else:
                for existing_path in _list_session_files(self._session_id):
                    if existing_path.stem.endswith(f"_{safe_name}"):
                        existing_path.unlink(missing_ok=True)
                        deleted_count += 1
        return deleted_count


# 会话管理器注册表（用于缓存不同session_id的管理器实例）
_session_managers: dict[str, FileMemoryManager] = {}
_session_lock = threading.Lock()


async def get_file_memory_manager(session_id: str) -> FileMemoryManager:
    """
    根据 session_id 获取或创建文件记忆管理器实例
    Args:
        session_id: 会话ID
    Returns:
        FileMemoryManager实例
    """
    with _session_lock:
        if session_id not in _session_managers:
            _session_managers[session_id] = FileMemoryManager(session_id)
        return _session_managers[session_id]


async def cleanup_file_memory_manager(session_id: str) -> None:
    """
    清理指定会话的记忆管理器（释放实例引用）
    Args:
        session_id: 会话ID
    """
    with _session_lock:
        if session_id in _session_managers:
            del _session_managers[session_id]


if __name__ == "__main__":
    print("=" * 60)
    print("FileMemoryManager 并发安全测试")
    print("=" * 60)

    # 测试1：基本功能
    print("\n【测试1】基本功能测试")

    # 为不同session创建独立的管理器
    manager_1 = get_file_memory_manager("session_1")
    manager_2 = get_file_memory_manager("session_2")

    # 测试会话1
    manager_1.add_file_memory({"filename": "test1.pdf", "type": "pdf", "size": 1024})
    manager_1.add_file_memory({"filename": "test2.docx", "type": "docx", "size": 2048})
    print(f"Session 1 文件: {manager_1.get_file_memory_all()}")

    # 测试会话2
    manager_2.add_file_memory({"filename": "another.txt", "type": "txt", "size": 512})
    print(f"Session 2 文件: {manager_2.get_file_memory_all()}")

    # 验证隔离
    print(f"\n验证隔离 - Session 1: {len(manager_1.get_file_memory_all())} 个文件")
    print(f"验证隔离 - Session 2: {len(manager_2.get_file_memory_all())} 个文件")

    # 测试2：数量限制
    print("\n【测试2】数量限制测试（最多10个文件）")
    manager_limit = get_file_memory_manager("session_limit")
    for i in range(12):
        manager_limit.add_file_memory({
            "filename": f"file_{i+1}.txt",
            "type": "txt",
            "size": 100 * (i + 1)
        })
    files = manager_limit.get_file_memory_all(10)
    print(f"添加12个文件后保留: {len(files)} 个文件")
    print(f"文件名: {[f['filename'] for f in files]}")

    # 测试3：文本摘要
    print("\n【测试3】文本摘要测试")
    manager_text = get_file_memory_manager("session_text")
    manager_text.add_file_memory({"filename": "文档1.pdf", "type": "pdf", "size": 1024})
    manager_text.add_file_memory({"filename": "文档2.docx", "type": "docx", "size": 2048})
    manager_text.add_file_memory({"filename": "文档3.txt", "type": "txt", "size": 512})
    text_summary = manager_text.get_file_memory_text(max_total_chars=100)
    print(f"文本摘要（限制100字符）:\n{text_summary}")

    # 测试4：清理功能
    print("\n【测试4】清理功能测试")
    print(f"清理前 Session 1: {len(manager_1.get_file_memory_all())} 个文件")
    manager_1.clear_file_memory()
    print(f"清理后 Session 1: {len(manager_1.get_file_memory_all())} 个文件")
    print(f"Session 2 未受影响: {len(manager_2.get_file_memory_all())} 个文件")

    # 测试5：并发安全测试
    print("\n【测试5】并发安全测试")
    import time

    errors = []

    def worker(session_id, num_files):
        try:
            manager = get_file_memory_manager(session_id)
            for i in range(num_files):
                manager.add_file_memory({
                    "filename": f"Thread-{session_id}-File-{i}.txt",
                    "type": "txt",
                    "size": 100 * i
                })
                time.sleep(0.001)  # 模拟一些工作
        except Exception as e:
            errors.append(str(e))

    # 创建多个线程同时操作不同的session
    threads = []
    for i in range(5):
        t = threading.Thread(target=worker, args=(f"concurrent_session_{i}", 15))
        threads.append(t)
        t.start()

    # 等待所有线程完成
    for t in threads:
        t.join()

    print(f"并发测试完成，错误数: {len(errors)}")
    if errors:
        print(f"错误详情: {errors[:3]}")

    # 验证每个会话的数据完整性
    for i in range(5):
        session_id = f"concurrent_session_{i}"
        manager = get_file_memory_manager(session_id)
        files = manager.get_file_memory_all(10)
        print(f"  {session_id}: {len(files)} 个文件")

    # 清理测试数据
    for i in range(5):
        cleanup_file_memory_manager(f"concurrent_session_{i}")
    cleanup_file_memory_manager("session_1")
    cleanup_file_memory_manager("session_2")
    cleanup_file_memory_manager("session_limit")
    cleanup_file_memory_manager("session_text")

    print("\n" + "=" * 60)
    print("所有测试完成！")
    print("=" * 60)
