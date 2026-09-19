""" 记录当前轮次对话上传的文件信息，持久化到项目 history 文件夹中 """
# from __future__ import annotations
# 标准库
import base64
import json
import re
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
import io
# 第三方库
from PIL import Image
# 自定义模块
from util.timestamp_utils import DEFAULT_TIMESTAMP_FORMAT, now_str as _timestamp_now

# 会话上传数据的存储根目录（由 history_files/upload/ 改名而来，
# 目录名保留 session 语义；_meta.upload_id 字段名沿用不变）
HISTORY_ROOT = Path(__file__).resolve().parents[1] / "history_files" / "session_files"
HISTORY_ROOT.mkdir(parents=True, exist_ok=True)


# ---------- 多媒体附件（图片/音频/视频） ----------
# 聊天多模态消息的二进制文件存储：原始字节保存在
# history_files/session_files/<session>/media/ 下，聊天消息 content 列表里用
# "media://<stored_name>" 引用，发送上游前由 resolve_media_content_parts
# 解析为 OpenAI 兼容格式（image_url.url = data URL；input_audio.data = base64）。

MEDIA_URL_SCHEME = "media://"
MEDIA_DIRECTORY_NAME = "media"
# 单文件大小上限按媒体类别区分（上传入库口径）：视频文件普遍较大（600MB），
# 图片/音频 20MB；发送/读取口径按内容判定（动图判定后分流，见 _media_send_size_limit）
MEDIA_SIZE_LIMITS = {
    "image": 20 * 1024 * 1024,
    "audio": 20 * 1024 * 1024,
    "video": 600 * 1024 * 1024,
}
MEDIA_MAX_FILE_SIZE = MEDIA_SIZE_LIMITS["image"]  # 兼容旧引用：图片/音频默认上限

# ---------- 发送/读取大小上限（按内容判定，区分静图与动图） ----------
# 视觉 API 的请求体上限普遍在数百 MB 量级：超大原始文件直接注入必然被上游
# 拒绝（实测 526MB gif 以 ~700MB base64 载荷发送导致连接中止 WinError 10053）。
# 判定时机在动图/静图分流之后（content-based）：静图类收得紧（30MB，超大静图
# 本就应走缩略图链路）；动图 gif 与视频类放宽到 600MB（转码前先缩放，耗时可控）。
MEDIA_SEND_IMAGE_LIMIT_BYTES = 30 * 1024 * 1024
MEDIA_SEND_ANIMATED_OR_VIDEO_LIMIT_BYTES = 600 * 1024 * 1024


class MediaSendTooLargeError(RuntimeError):
    """媒体超过发送/读取上限（按内容判定）——不可回退原图注入，必须拒绝。"""

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

# 主流视觉模型原生接受的图片格式：PNG/JPEG/WebP（OpenAI 系），
# Qwen-VL/GLM-4V/Gemini 等另普遍支持 BMP/TIFF
_IMAGE_MEDIA_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
                           ".ico", ".tif", ".tiff"}
_AUDIO_MEDIA_EXTENSIONS = {".wav", ".mp3", ".m4a", ".ogg", ".flac"}
_VIDEO_MEDIA_EXTENSIONS = {".mp4", ".webm", ".mov", ".mkv"}

# 视觉模型不原生接受、落盘前自动转为 PNG 的图片格式（需 Pillow）：
# ICO（模型基本不支持）、TIFF（部分模型支持，转 PNG 通用性最好）
_PNG_CONVERSION_EXTENSIONS = {".ico", ".tif", ".tiff"}
# 视觉 API 通常拒绝的图片格式集合（发送上游前需转换为受支持格式）：
# ICO（模型基本不支持）、TIFF（部分模型支持）、BMP（多数网关拒绝）
_VISION_UNSAFE_IMAGE_EXTENSIONS = {".bmp", ".ico", ".tif", ".tiff"}

# content 部件类型 -> 历史回放/压缩时的占位文案
MEDIA_PART_LABELS = {
    "image_url": "[图片]",
    "input_audio": "[音频]",
    "video_url": "[视频]",
}

# ---------- 可解析文档的原始字节（用于点击预览/下载） ----------
# 上传解析流程历来只保存解析后的文本 JSON；原始文件（如 PDF）字节另存一份在
# history_files/session_files/<session>/files/ 下，前端消息/附件区的文档可点击预览。

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
    """返回该会话上传文件所在目录名（history_files/session_files/ 下的文件夹名）。

    上传目录按上传时前端传入的 session_id 命名（`_safe_session_id` 后的结果），
    可能与聊天会话文件名不一致，因此调用方会把它记录到会话 _meta.upload_id，
    供删除会话时连带清理上传目录。字段名沿用 upload_id 不变（仅指目录名，
    与物理路径无关）。
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


# gif→mp4 转换缓存目录名（与会话 thumbs/ 同级，独立管理）
_GIF_VIDEO_DIRECTORY_NAME = "media_transcode"
_GIF_VIDEO_MAX_EDGE = 768
_GIF_VIDEO_TIMEOUT_SECONDS = 120


def _locate_ffmpeg() -> str | None:
    """定位 ffmpeg 可执行文件；系统未安装时回退 imageio-ffmpeg 自带二进制。

    优先级：系统 PATH 的 ffmpeg → imageio-ffmpeg 包内置的静态可执行文件
    （pip install imageio-ffmpeg 即得，无需管理员权限或系统级安装，自带
    libx264/minterpolate）→ None（调用方按 FileNotFoundError 提示）。
    """
    import shutil

    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def image_is_animated(path: Path) -> bool:
    """判断一张图片是否为动图（Pillow 口径，异常按 False 静态处理）。

    动图判定：is_animated 标记或 n_frames>1。仅覆盖 Pillow 支持解析的
    图片格式（gif/webp/png 等），非图片文件调用方先按扩展名分流。
    gif 大文件建议用 gif_is_animated（流式扫描，不整读内存）。
    """
    try:
        with Image.open(path) as img:
            return bool(getattr(img, "is_animated", False)) or getattr(img, "n_frames", 1) > 1
    except Exception:
        return False


# gif 动图字节扫描的单块大小（256KB）：GIF 图像帧由若干 data sub-block 组成，
# 扫描块级即可定位图像描述符（0x2C）与块结束符（0x00），无需整读文件
_GIF_SCAN_CHUNK_BYTES = 256 * 1024


def gif_is_animated(path: Path) -> bool:
    """流式判断 gif 是否为动图：数图像描述符(0x2C)到第 2 个即提前退出。

    GIF 结构：文件头(13B) → 逻辑屏幕描述符 → [全局色表] → 多个
    (扩展块 0x21 / 图像描述符 0x2C)，图像描述符后跟 LZW 数据子块序列，
    以 0x00 长度的子块结束。跳帧数据量通常远小于整文件，超大 gif（数百 MB）
    一般在前几 MB 内即可判定，全程 O(1) 内存。

    判定失败（非 gif 魔数/IO 异常/扫描到 EOF 只见 1 帧）返回 False，
    与 image_is_animated 的异常口径一致（调用方按静图处理）。
    """
    try:
        with path.open("rb") as fp:
            head = fp.read(13)
            if len(head) < 13 or not head.startswith((b"GIF87a", b"GIF89a")):
                return False
            flags = head[10]
            pos = 13
            if flags & 0x80:  # 全局色表：3 * 2^(N+1) 字节
                pos += 3 * (2 ** ((flags & 0x07) + 1))
            descriptors = 0
            block_type = 0
            while descriptors < 2:
                fp.seek(pos)
                marker = fp.read(1)
                if not marker:
                    break
                block_type = marker[0]
                pos += 1
                if block_type == 0x2C:  # 图像描述符
                    descriptors += 1
                    if descriptors >= 2:
                        return True
                    desc_head = fp.read(9)
                    pos += 9
                    local_flags = desc_head[8] if len(desc_head) == 9 else 0
                    if local_flags & 0x80:  # 局部色表
                        pos += 3 * (2 ** ((local_flags & 0x07) + 1))
                    # 跳过 LZW 数据子块序列（min code size 字节 + 子块流）
                    fp.seek(pos)
                    lzw_head = fp.read(1)
                    if not lzw_head:
                        break
                    pos += 1 + lzw_head[0]
                    while True:
                        fp.seek(pos)
                        size_byte = fp.read(1)
                        if not size_byte:
                            pos = -1
                            break
                        pos += 1
                        if size_byte[0] == 0:  # 子块流结束
                            break
                        pos += size_byte[0]
                    if pos < 0:
                        break
                elif block_type == 0x21:  # 扩展块：跳过子块流
                    fp.seek(pos)
                    label = fp.read(1)
                    pos += 1
                    while True:
                        fp.seek(pos)
                        size_byte = fp.read(1)
                        if not size_byte:
                            pos = -1
                            break
                        pos += 1
                        if size_byte[0] == 0:
                            break
                        pos += size_byte[0]
                    if pos < 0:
                        break
                elif block_type == 0x3B:  # 文件结束
                    break
                else:
                    # 未知块类型：结构不可信，退回 Pillow 判定（小文件可接受）
                    return image_is_animated(path)
            return False
    except OSError:
        return False


def media_send_size_limit_bytes(path: Path, animated: bool | None = None) -> int:
    """发送/读取上游的按内容判定上限（字节数）。

    - 静图类（kind=image 且非动图）：30MB——超大静图本就走缩略图链路，
      原始字节注入没有意义且必然撑爆请求体；
    - 动图 gif 与视频类：600MB——转码前先缩放，耗时与输出体积可控；
    - 音频：20MB（与上传口径一致）。
    """
    kind = media_kind(path.name)
    if kind == "audio":
        return MEDIA_SIZE_LIMITS["audio"]
    if kind == "video" or animated:
        return MEDIA_SEND_ANIMATED_OR_VIDEO_LIMIT_BYTES
    return MEDIA_SEND_IMAGE_LIMIT_BYTES


# ---------- 视频区间读取（read_media 视频/动图按帧精读） ----------
# 单次注入的视频时长上限（秒）：供应商请求体有硬上限，实测 1 分钟以上的
# 全量视频会被上游拒绝；请求区间超上限自动截断为 [start, start+上限]，
# 模型可按区间继续读取后续内容。上限可由项目 .env 的 VIDEO_MAX_READ_SECONDS
# 配置（配置热重载线程监视 .env 指纹，变化后内存 env_vars 已同步，此处
# 每次调用动态读取，改完 .env 约一个轮询间隔内生效、无需重启）。
DEFAULT_VIDEO_MAX_READ_SECONDS = 60
VIDEO_MAX_READ_SECONDS_ENV = "VIDEO_MAX_READ_SECONDS"
_VIDEO_PROBE_TIMEOUT_SECONDS = 30


def video_max_read_seconds() -> int:
    """视频单次读取上限（秒）：动态读取 .env 配置，钳制在 5-3600 之间。

    非法值（非数字/空）回退默认 60；下限 5 防误配成 0 导致区间为空。
    """
    from env_manager import load_var

    try:
        value = int(float(load_var(VIDEO_MAX_READ_SECONDS_ENV, DEFAULT_VIDEO_MAX_READ_SECONDS)))
    except (TypeError, ValueError):
        return DEFAULT_VIDEO_MAX_READ_SECONDS
    return max(5, min(3600, value))


def _parse_ffmpeg_time_seconds(text: str) -> float | None:
    """解析 ffmpeg 的 HH:MM:SS.ms 时长文本为秒。"""
    match = re.search(r"(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if not match:
        return None
    return int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))


def _count_gif_image_descriptors(path: Path) -> int:
    """流式统计 gif 图像描述符（0x2C）总数（估算无时长元数据的 gif 帧数）。"""
    try:
        with path.open("rb") as fp:
            head = fp.read(13)
            if len(head) < 13 or not head.startswith((b"GIF87a", b"GIF89a")):
                return 0
            pos = 13
            if head[10] & 0x80:  # 全局色表
                pos += 3 * (2 ** ((head[10] & 0x07) + 1))
            descriptors = 0
            while True:
                fp.seek(pos)
                marker = fp.read(1)
                if not marker:
                    break
                block_type = marker[0]
                pos += 1
                if block_type == 0x2C:  # 图像描述符：跳过其 LZW 子块流
                    descriptors += 1
                    desc_head = fp.read(9)
                    pos += 9
                    local_flags = desc_head[8] if len(desc_head) == 9 else 0
                    if local_flags & 0x80:
                        pos += 3 * (2 ** ((local_flags & 0x07) + 1))
                    fp.seek(pos)
                    lzw_head = fp.read(1)
                    if not lzw_head:
                        break
                    pos += 1 + lzw_head[0]
                    while True:
                        fp.seek(pos)
                        size_byte = fp.read(1)
                        if not size_byte:
                            return descriptors
                        pos += 1
                        if size_byte[0] == 0:
                            break
                        pos += size_byte[0]
                elif block_type == 0x21:  # 扩展块：跳过子块流
                    while True:
                        fp.seek(pos)
                        size_byte = fp.read(1)
                        if not size_byte:
                            return descriptors
                        pos += 1
                        if size_byte[0] == 0:
                            break
                        pos += size_byte[0]
                elif block_type == 0x3B:
                    break
                else:
                    break
            return descriptors
    except OSError:
        return 0


def _probe_video_metadata_uncached(path: Path, suffix: str) -> dict[str, Any] | None:
    """用 ffmpeg 解析视频流元信息（无 ffprobe 依赖：ffmpeg -i 的 stderr 摘要）。

    返回 {duration, fps, width, height, estimated_duration}；探测失败
    （非视频流/ffmpeg 缺失/超时）返回 None。gif 的 Duration 缺失时用
    图像描述符计数估算（frames/fps，标注 estimated_duration）。
    """
    ffmpeg = _locate_ffmpeg()
    if not ffmpeg:
        return None
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", str(path)],
            capture_output=True, timeout=_VIDEO_PROBE_TIMEOUT_SECONDS,
        )
    except Exception:
        return None
    stderr = proc.stderr.decode("utf-8", errors="replace")
    if "Video:" not in stderr:
        return None
    duration = None
    dur_match = re.search(r"Duration:\s*([0-9:.+\-N/A]+)", stderr)
    if dur_match and "N/A" not in dur_match.group(1):
        duration = _parse_ffmpeg_time_seconds(dur_match.group(1))
    fps = None
    fps_match = re.search(r"([\d.]+)\s*fps", stderr)
    if fps_match:
        fps = float(fps_match.group(1))
    else:
        tbr_match = re.search(r"([\d.]+)\s*tbr", stderr)
        if tbr_match:
            fps = float(tbr_match.group(1))
    video_line = stderr.split("Video:", 1)[-1]
    size_match = re.search(r"(\d{2,6})x(\d{2,6})", video_line)
    meta = {
        "duration": duration,
        "fps": fps,
        "width": int(size_match.group(1)) if size_match else None,
        "height": int(size_match.group(2)) if size_match else None,
        "estimated_duration": False,
    }
    if duration is None and Path(path.name).suffix.lower() == ".gif":
        frames = _count_gif_image_descriptors(path)
        if frames:
            meta["duration"] = frames / (fps or 10.0)
            meta["fps"] = fps or 10.0
            meta["estimated_duration"] = True
    # 有效性校验：时长与分辨率都没有信号视为非视频流（如 png_pipe 单帧序列，
    # 只有 fps 默认值、无真实视频流信息）
    if duration is None and size_match is None:
        return None
    meta["fps"] = fps if fps is not None else 25.0
    return meta


def _probe_video_metadata(path: Path, cache_dir: Path | None = None) -> dict[str, Any] | None:
    """视频元信息探测（带缓存）：{duration, fps, width, height, estimated_duration}。

    基于 ffmpeg -i 的 stderr 摘要解析（不整解码）；缓存 <原名>.probe.json
    按 mtime+size 指纹失效；探测失败返回 None（调用方降级处理）。
    """
    try:
        stat = path.stat()
        fingerprint = f"{stat.st_size}-{int(stat.st_mtime)}"
    except OSError:
        return None
    cache_file = Path(cache_dir) / f"{path.name}.probe.json" if cache_dir else None
    if cache_file is not None:
        try:
            if cache_file.is_file():
                data = json.loads(cache_file.read_text(encoding="utf-8"))
                if data.get("_fp") == fingerprint:
                    data.pop("_fp", None)
                    return data
        except (OSError, ValueError):
            pass
    meta = _probe_video_metadata_uncached(path, Path(path.name).suffix.lower())
    if meta is not None and cache_file is not None:
        try:
            Path(cache_dir).mkdir(parents=True, exist_ok=True)
            cache_file.write_text(
                json.dumps({**meta, "_fp": fingerprint}, ensure_ascii=False),
                encoding="utf-8",
            )
        except (OSError, TypeError):
            pass
    return meta


def _snap_frame_time(seconds: float, fps: float) -> float:
    """把秒吸附到最近帧边界（帧号 = int(t*fps + 0.5)，避免银行家舍入把 0.5 吞掉）。"""
    fps = max(fps, 1e-6)
    return round(int(seconds * fps + 0.5) / fps, 6)


def _cut_video_clip_file(
    cut_source: Path,
    out_path: Path,
    start: float,
    duration: float,
    fps: float,
) -> None:
    """从视频源切出 [start, start+duration] 的 H.264 MP4 片段（区间读取内核）。

    - -ss 放在 -i 前（重编码模式下 ffmpeg 默认 accurate_seek，定位帧精确）；
    - 短边压到 _GIF_VIDEO_MAX_EDGE（gif 转码源已 ≤768，scale 为无害直通）；
    - 禁音频；帧数上限按区间帧数+2 兜底，防 fps 探测偏差导致超发。
    失败（ffmpeg 缺失/超时/退出码非零）抛出异常，调用方转为错误占位。
    """
    vf = (
        f"scale=trunc(min(iw\\,{_GIF_VIDEO_MAX_EDGE})/2)*2:"
        f"trunc(min(ih\\,{_GIF_VIDEO_MAX_EDGE})/2)*2"
    )
    cmd = [
        _locate_ffmpeg() or "ffmpeg",
        "-hide_banner", "-loglevel", "error",
        "-y",
        "-ss", f"{start:.6f}",
        "-i", str(cut_source),
        "-t", f"{duration:.6f}",
        "-vf", vf,
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "22",
        "-pix_fmt", "yuv420p",
        "-an",
        "-movflags", "+faststart",
        "-frames:v", str(max(2, int(duration * fps) + 2)),
        str(out_path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=_GIF_VIDEO_TIMEOUT_SECONDS)
    except FileNotFoundError as exc:
        raise RuntimeError(
            "未找到 ffmpeg（视频区间读取需要可用的 ffmpeg：系统安装，或 pip install imageio-ffmpeg）"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"ffmpeg 切片超时（>{_GIF_VIDEO_TIMEOUT_SECONDS}s）") from exc
    if proc.returncode != 0 or not out_path.is_file() or out_path.stat().st_size == 0:
        stderr_tail = (proc.stderr or b"")[-300:].decode("utf-8", errors="replace")
        raise RuntimeError(f"ffmpeg 切片失败: {stderr_tail or proc.returncode}")


def _build_video_range_part(
    source_path: Path,
    cache_dir: Path,
    start_time: float | None,
    end_time: float | None,
    is_animated_gif: bool,
) -> dict[str, Any]:
    """视频（含动图 gif 转码源）区间读取：构建 video_url 部件与 video 元信息。

    语义（与 read_media 工具约定一致）：
    - start/end 以秒为单位（毫秒精度），内部按源帧率吸附到帧边界；
    - start_time == end_time（吸附后同帧）：仅返回元数据文本部件（探测），
      不注入视频数据；
    - end 缺省 → min(时长, start + VIDEO_MAX_READ_SECONDS)；
    - 区间超过 VIDEO_MAX_READ_SECONDS → 截断为 [start, start+上限]，标记 truncated；
    - start > end / start ≥ 时长 / 区间为空 → 返回 {"error": ...}。

    返回 {"part", "video": {duration, fps, width, height, read:{start,end,
    seconds,truncated} 或 probe 标记}, "size", "converted"} 或 {"error"}。
    """
    try:
        stat = source_path.stat()
        fingerprint = f"{stat.st_size}-{int(stat.st_mtime)}"
    except OSError as exc:
        return {"error": f"读取失败: {exc}"}
    converted_meta: dict[str, Any] | None = None
    if is_animated_gif:
        # gif 先确保转码 mp4 存在（复用转换内核的缓存命名与指纹）
        cut_source = Path(cache_dir) / f"{source_path.name}.conv.mp4"
        try:
            conv_fp = cut_source.with_name(cut_source.name + ".fp")
            if not (cut_source.is_file() and cut_source.stat().st_size > 0):
                _convert_gif_to_mp4_file(source_path, cut_source)
                conv_fp.write_text(fingerprint, encoding="ascii")
            elif conv_fp.is_file() and conv_fp.read_text(encoding="ascii", errors="ignore") != fingerprint:
                _convert_gif_to_mp4_file(source_path, cut_source)
                conv_fp.write_text(fingerprint, encoding="ascii")
            converted_meta = {
                "original_format": "gif",
                "converted_format": "mp4",
                "animated": True,
                "converted_kind": "video",
            }
        except Exception as conv_error:
            return {"error": f"动图 gif 转码失败，无法区间读取: {conv_error}"}
    else:
        cut_source = source_path
    probed = _probe_video_metadata(source_path, cache_dir)
    if probed is None:
        return {"error": f"视频元信息探测失败（需可用的 ffmpeg）: {source_path.name}"}
    fps = float(probed.get("fps") or 25.0)
    duration = probed.get("duration")
    video_meta: dict[str, Any] = {
        "duration": duration,
        "fps": fps,
        "width": probed.get("width"),
        "height": probed.get("height"),
        "estimated_duration": bool(probed.get("estimated_duration")),
    }
    s = _snap_frame_time(max(0.0, float(start_time)), fps) if start_time is not None else 0.0
    e = _snap_frame_time(float(end_time), fps) if end_time is not None else None
    # 元数据探测约定：显式给的 start_time == end_time（吸附后同帧）→ 只回元数据
    if start_time is not None and end_time is not None and abs(e - s) < 1e-9:
        dur_text = f"{duration:.2f}s" if duration is not None else "未知"
        if video_meta.get("estimated_duration"):
            dur_text += "（估算）"
        video_meta["probe"] = True
        text = (
            f"[视频元数据] {source_path.name}：时长 {dur_text}，"
            f"{fps:.3g}fps，分辨率 {video_meta['width']}x{video_meta['height']}，"
            f"大小 {stat.st_size / (1024 * 1024):.1f}MB。"
            f"区间读取约定：start_time == end_time 仅返回元数据；"
            f"单次最多读取 {video_max_read_seconds()} 秒（超出自动截断）。"
        )
        return {"part": {"type": "text", "text": text}, "video": video_meta, "size": None}
    if end_time is not None and e < s:
        return {
            "error": (
                f"end_time({end_time}) 小于 start_time({start_time})：请保证 "
                f"start_time <= end_time（相等表示仅探测元数据）"
            )
        }
    if duration is not None and s >= duration:
        return {"error": f"start_time({start_time}) 超出视频时长（{duration:.2f}s）"}
    truncated = False
    if e is None:
        e = s + video_max_read_seconds()
        if duration is not None:
            e = min(e, duration)
        e = _snap_frame_time(e, fps)
        # 默认读取（未显式给 end）：还有剩余未读即标记截断，提示模型续读
        if duration is not None and e < duration - 1e-6:
            truncated = True
    if e - s > video_max_read_seconds() + 1e-6:
        e = _snap_frame_time(s + video_max_read_seconds(), fps)
        truncated = True
    if duration is not None and e > duration:
        e = _snap_frame_time(duration, fps)
    if e <= s:
        return {"error": f"读取区间为空（{s:.3f}s ~ {e:.3f}s）：请调整区间"}
    # 切片缓存：<原名>.clip_<start>_<end>.mp4，按源文件 mtime+size 指纹失效
    clip_path = Path(cache_dir) / f"{source_path.name}.clip_{s:.3f}_{e:.3f}.mp4"
    try:
        Path(cache_dir).mkdir(parents=True, exist_ok=True)
        fp_file = clip_path.with_name(clip_path.name + ".fp")
        if (
            clip_path.is_file()
            and clip_path.stat().st_size > 0
            and fp_file.is_file()
            and fp_file.read_text(encoding="ascii", errors="ignore") == fingerprint
        ):
            clip_size = clip_path.stat().st_size
        else:
            _cut_video_clip_file(cut_source, clip_path, s, e - s, fps)
            fp_file.write_text(fingerprint, encoding="ascii")
            clip_size = clip_path.stat().st_size
        clip_b64 = base64.b64encode(clip_path.read_bytes()).decode("ascii")
    except RuntimeError as exc:
        return {"error": str(exc)}
    except OSError as exc:
        return {"error": f"切片缓存读写失败: {exc}"}
    video_meta["read"] = {
        "start": s,
        "end": e,
        "seconds": round(e - s, 3),
        "truncated": truncated,
    }
    return {
        "part": {"type": "video_url", "video_url": {"url": _media_data_url("video/mp4", clip_b64)}},
        "video": video_meta,
        "size": clip_size,
        "converted": converted_meta,
    }


def _convert_image_file_to_png_file(path: Path, out_path: Path) -> None:
    """把一张图片文件转为 PNG 文件（转换内核，缓存与即时转换共用）。

    GIF 静图取第一帧；透明通道保留，CMYK 等少数模式先转 RGBA 再存。
    """
    with Image.open(path) as img:
        img.load()
        try:
            img.save(out_path, format="PNG")
        except OSError:
            img.convert("RGBA").save(out_path, format="PNG")


def _convert_gif_to_mp4_file(gif_path: Path, out_path: Path) -> None:
    """把 GIF 动图转为 H.264 MP4（供 video_url 部件发送视觉模型）。

    参数口径：
    - 源按 10fps 采样（多数 GIF 帧率量级），输出时基 1/fps；
    - minterpolate 运动补偿插值补帧到 30fps：视觉模型按帧采样时动作
      连续性更好（插值失败时 ffmpeg 自行回退，不影响主流程）；
    - 滤镜链顺序 fps → scale → minterpolate：先把短边压到
      _GIF_VIDEO_MAX_EDGE 再做运动插值——插值是逐像素运动估计的极重
      滤镜，在全分辨率（如 3600×2546）上会慢到超时；先缩放可将插值
      工作量降约一个数量级（实测 526MB 大 gif 全分辨率插值 120s 超时、
      先缩后插秒级完成）；
    - 禁用音频流（GIF 无音轨，避免部分供应商拒绝空音频流）；
    - preset fast + CRF 22 兼顾转码速度与体积；
    - 帧数上限 750（75 秒 @10fps）防超长 GIF 卡住转码。
    转码失败（ffmpeg 缺失/损坏文件/超时）抛出异常，调用方回退静图链路。
    """
    fps = 10
    frame = 1.0 / fps
    vf = (
        f"fps={fps},"
        f"scale=trunc(min(iw\\,{_GIF_VIDEO_MAX_EDGE})/2)*2:"
        f"trunc(min(ih\\,{_GIF_VIDEO_MAX_EDGE})/2)*2,"
        "minterpolate=fps=30:mi_mode=mci:mc_mode=aobmc:me_mode=bidir"
    )
    cmd = [
        _locate_ffmpeg() or "ffmpeg",
        "-hide_banner", "-loglevel", "error",
        "-y",
        "-i", str(gif_path),
        "-vf", vf,
        "-r", str(fps),
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "22",
        "-pix_fmt", "yuv420p",
        "-an",
        "-movflags", "+faststart",
        "-frames:v", "750",
        str(out_path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=_GIF_VIDEO_TIMEOUT_SECONDS)
    except FileNotFoundError as exc:
        raise RuntimeError(
            "未找到 ffmpeg（gif→mp4 需要可用的 ffmpeg：系统安装，或 pip install imageio-ffmpeg）"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"ffmpeg 转码超时（>{_GIF_VIDEO_TIMEOUT_SECONDS}s）") from exc
    if proc.returncode != 0 or not out_path.is_file() or out_path.stat().st_size == 0:
        stderr_tail = (proc.stderr or b"")[-300:].decode("utf-8", errors="replace")
        raise RuntimeError(f"ffmpeg 转码失败: {stderr_tail or proc.returncode}")


def _load_converted_media_base64(
    cache_dir: Path,
    source_path: Path,
    quality: int | None = None,
) -> tuple[str, str, dict[str, Any]] | None:
    """把视觉 API 拒绝的媒体文件转换为受支持格式（转换内核，带磁盘缓存）。

    分流规则（按扩展名 + 流式字节扫描动图判定，gif 大文件不再让 Pillow
    硬扫整文件）：
    - .gif 动图 → H.264 MP4（ffmpeg，kind 转 video）；转码失败回退
      首帧 PNG（kind 仍为 image）——绝不把数百 MB 原始 gif 发给上游；
    - .gif 静图 → PNG（Pillow 取第一帧，kind 仍为 image）；
    - .bmp/.ico/.tif/.tiff → PNG（Pillow）；
    - 其余格式返回 None（调用方按原生格式走）。

    大小门控（超限抛 MediaSendTooLargeError，调用方必须拒绝注入而不是
    回退原图）：按内容判定——静图类 30MB（MEDIA_SEND_IMAGE_LIMIT_BYTES）、
    动图 gif 与视频类 600MB（MEDIA_SEND_ANIMATED_OR_VIDEO_LIMIT_BYTES）、
    音频 20MB。门控对无缓存的实时转换生效；缓存命中（同指纹）直接复用，
    不再重复校验。

    返回 (mime, base64, convert_info)；convert_info 携带 {original_format,
    converted_format, animated, converted_kind} 供工具结果展示；转换失败
    返回 None（调用方回退原图/原错误）。
    缓存文件 <原名>.conv.<ext>，按 mtime+size 指纹失效（<原名>.conv.<ext>.fp
    存指纹），与缩略图同款机制；cache_dir 由调用方传入（会话链路传
    <session>/media_transcode/，本地/网络链路按第一个可用会话目录隔离）。
    """
    suffix = Path(source_path.name).suffix.lower()
    if suffix not in _VISION_UNSAFE_IMAGE_EXTENSIONS and suffix != ".gif":
        return None
    try:
        stat = source_path.stat()
        fingerprint = f"{stat.st_size}-{int(stat.st_mtime)}"
    except OSError:
        return None

    # GIF 按动图判定分流 mp4/PNG；其余扩展名固定 PNG（此处不感知动图差异）。
    # 动图判定用流式字节扫描（gif_is_animated）：数到第 2 个图像描述符即
    # 提前退出，超大 gif（数百 MB）不再触发 Pillow 的 is_animated 全量解码
    animated: bool | None = None
    if suffix == ".gif":
        animated = gif_is_animated(source_path)
        cache_target = cache_dir / (
            f"{source_path.name}.conv" + (".mp4" if animated else ".png")
        )
    else:
        cache_target = cache_dir / f"{source_path.name}.conv.png"

    conv_kind = "video" if cache_target.suffix == ".mp4" else "image"
    mime = "video/mp4" if cache_target.suffix == ".mp4" else "image/png"
    meta = {
        "original_format": suffix.lstrip("."),
        "converted_format": "mp4" if cache_target.suffix == ".mp4" else "png",
        "animated": bool(animated),
        "converted_kind": conv_kind,
    }
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        if cache_target.is_file() and cache_target.stat().st_size > 0:
            cached_fp = cache_target.with_name(cache_target.name + ".fp")
            cached_text = cached_fp.read_text(encoding="ascii", errors="ignore") if cached_fp.is_file() else ""
            if cached_text == fingerprint:
                return mime, base64.b64encode(cache_target.read_bytes()).decode("ascii"), meta
    except OSError:
        return None

    # 大小门控（缓存未命中才走到这里）：超限直接拒绝，绝不注入原始大文件
    limit_bytes = media_send_size_limit_bytes(source_path, animated)
    if stat.st_size > limit_bytes:
        raise MediaSendTooLargeError(
            f"{source_path.name}（{stat.st_size / (1024 * 1024):.1f}MB）超过"
            f"{'动图/视频' if (kind_is_animated_or_video(animated, suffix)) else '静图'}"
            f"读取上限 {limit_bytes // (1024 * 1024)}MB，已拒绝注入"
        )

    def _run_convert(target: Path) -> None:
        if target.suffix == ".mp4":
            _convert_gif_to_mp4_file(source_path, target)
        else:
            _convert_image_file_to_png_file(source_path, target)

    # 转码链路：动图 gif 的 mp4 转换失败时回退首帧 PNG（kind 仍为 image），
    # 避免调用方按原图口径回退导致巨型 gif 原样注入；静图转换失败维持
    # 原语义返回 None（调用方按原图发送，单帧图体积通常可控）
    try:
        try:
            _run_convert(cache_target)
        except Exception:
            if cache_target.suffix != ".mp4":
                raise
            # 动图 gif → mp4 失败：回退首帧 PNG（转换内核内部兜底，不再上抛）
            fallback_target = cache_dir / f"{source_path.name}.conv.png"
            _convert_image_file_to_png_file(source_path, fallback_target)
            cache_target = fallback_target
            mime = "image/png"
            conv_kind = "image"
            meta["converted_format"] = "png"
            meta["converted_kind"] = "image"
            # 首帧 PNG 仍超静图上限（极高分辨率原图）：再降质为 JPEG 缩略图
            if fallback_target.stat().st_size > MEDIA_SEND_IMAGE_LIMIT_BYTES:
                thumb = _load_image_thumbnail_base64(
                    cache_dir, source_path,
                    max_edge=_GIF_VIDEO_MAX_EDGE, quality=85,
                )
                if thumb is not None:
                    thumb_mime, thumb_b64 = thumb
                    thumb_target = cache_dir / f"{source_path.name}.conv.jpg"
                    thumb_target.write_bytes(base64.b64decode(thumb_b64))
                    cache_target = thumb_target
                    mime = thumb_mime
                    meta["converted_format"] = "jpg"
        cache_target.with_name(cache_target.name + ".fp").write_text(fingerprint, encoding="ascii")
        return mime, base64.b64encode(cache_target.read_bytes()).decode("ascii"), meta
    except MediaSendTooLargeError:
        raise
    except Exception:
        return None


def kind_is_animated_or_video(animated: bool | None, suffix: str) -> bool:
    """大小门控的类别文案辅助：动图或视频扩展名返回 True。"""
    return bool(animated) or suffix in _VIDEO_MEDIA_EXTENSIONS


def _transcode_cache_dir(session_id: str) -> Path:
    """gif→mp4 等转换缓存的会话目录（<session>/media_transcode/）。"""
    directory = _get_session_dir(session_id) / _GIF_VIDEO_DIRECTORY_NAME
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _media_dir(session_id: str) -> Path:
    directory = _get_session_dir(session_id) / MEDIA_DIRECTORY_NAME
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _thumb_dir(session_id: str) -> Path:
    directory = _get_session_dir(session_id) / _THUMB_DIRECTORY_NAME
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _load_image_thumbnail_base64(
    cache_dir: Path,
    path: Path,
    max_edge: int = IMAGE_THUMBNAIL_MAX_EDGE,
    quality: int = IMAGE_THUMBNAIL_JPEG_QUALITY,
) -> tuple[str, str] | None:
    """为大图生成/复用 JPEG 缩略图，返回 (mime, base64)；失败返回 None（回退原图）。

    - 触发条件由调用方判断（文件字节数超过 IMAGE_THUMBNAIL_THRESHOLD_BYTES）；
    - 缓存目录由调用方传入（会话链路传 <session>/thumbs/，本地/网络链路传
      _thumb_dir(session_id) 同目录——按来源文件名隔离，互不覆盖）；
      缓存文件为 <原名>.thumb.<max_edge>.<quality>.jpg，同图同参数多次解析
      只算一次，原图更新（字节变化）时按 mtime+size 失效重建，
      不同 max_edge/quality 参数互相独立缓存、互不覆盖；
    - 长边压到 max_edge、JPEG 质量 quality；
    - 任何失败（Pillow 不支持/磁盘异常）都返回 None，调用方回退原图发送。
    """
    thumb_path = Path(cache_dir) / f"{path.name}.thumb.{max_edge}.{quality}.jpg"
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
            longest = max(width, height)
            if longest > max_edge:
                scale = max_edge / longest
                new_size = (max(1, round(width * scale)), max(1, round(height * scale)))
                img = img.resize(new_size, Image.LANCZOS)
            if img.mode not in ("RGB", "L"):
                # JPEG 无透明通道：透明区域铺白底，避免黑底突兀
                rgba = img.convert("RGBA")
                background = Image.new("RGB", rgba.size, (255, 255, 255))
                background.paste(rgba, mask=rgba.split()[3])
                img = background
            out = io.BytesIO()
            img.save(out, format="JPEG", quality=quality)
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


def _delete_media_file_with_retry(path: Path, attempts: int = 6) -> None:
    """删除媒体文件，带短重试（Windows 上刚写入的文件可能被索引/杀软短暂占用）。

    文件不存在视为已删除直接返回；仍失败抛出 OSError 交由上层记录。
    """
    for attempt in range(attempts):
        try:
            path.unlink()
            return
        except FileNotFoundError:
            return
        except OSError:
            if attempt >= attempts - 1:
                raise
            time.sleep(0.01 * (attempt + 1))


def _delete_doc_file_with_retry(session_id: str, stored_name: str, attempts: int = 6) -> None:
    """按 stored_name 删除 files/ 目录下的原始字节文件；不存在视为已删除。"""
    path = resolve_document_path(session_id, stored_name)
    if path is None:
        return
    for attempt in range(attempts):
        try:
            path.unlink()
            return
        except FileNotFoundError:
            return
        except OSError:
            if attempt >= attempts - 1:
                raise
            time.sleep(0.01 * (attempt + 1))


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


def _sniff_media_filename(data: bytes) -> str | None:
    """按魔数嗅探媒体类型，返回可用占位文件名；无法识别返回 None。

    网络下载/本地文件入库统一按扩展名走 media_kind/media_mime_type 链路，
    因此来源扩展名缺失或错误时用魔数校准文件名（入 sessionStorage 前调用）。
    """
    head = bytes(data[:16])
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "sniffed.png"
    if head.startswith(b"\xff\xd8\xff"):
        return "sniffed.jpg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "sniffed.gif"
    if head.startswith(b"RIFF") and len(data) >= 12:
        if data[8:12] == b"WEBP":
            return "sniffed.webp"
        if data[8:12] == b"WAVE":
            return "sniffed.wav"
    if head.startswith(b"BM"):
        return "sniffed.bmp"
    if head.startswith(b"ID3"):
        return "sniffed.mp3"
    if head.startswith(b"fLaC"):
        return "sniffed.flac"
    if head.startswith(b"OggS"):
        return "sniffed.ogg"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return "sniffed.mp4"
    return None


def register_media_source(
    session_id: str,
    filename: str,
    data: bytes,
    source_type: str,
) -> dict[str, Any]:
    """把模型侧获取的媒体字节（网络下载/本地文件读取）注册进会话媒体库。

    存储规则与用户上传完全一致（save_session_media：类型校验、大小上限、
    ICO/TIFF 自动转 PNG、唯一 stored_name），返回信息含 media:// 引用；
    注册后该媒体即走统一的会话媒体加载/降采样链路。

    source_type 备注来源（"network" / "local"），供后续用户授权审计使用；
    授权确认逻辑就位前不做任何拦截（当前策略：工具由用户提供，无安全边界）。
    """
    saved = save_session_media(session_id, filename, data)
    return {**saved, "source_type": source_type}


# read_media 内置工具：给模型读取媒体文件用的降采样/描述参数档位。
# >2MB 图片默认按此档位降采样（长边/质量），与用户上传链路的固定档位解耦，
# 支持模型通过 quality 参数（50%-100%）调节回传清晰度。
MODEL_MEDIA_MAX_EDGE = 1568
MODEL_MEDIA_DEFAULT_QUALITY = 85


def _media_model_part_from_path(
    path: Path,
    quality: int | None = None,
    thumb_cache_dir: Path | None = None,
    transcode_cache_dir: Path | None = None,
    start_time: float | None = None,
    end_time: float | None = None,
) -> dict[str, Any] | None:
    """按文件路径构建 read_media 模型部件（会话引用与 load_any 共用的加载内核）。

    返回 dict：{part, kind, mime, size, downsampled, quality, stored_name,
    filename, converted}；读取失败返回 None。

    发送口径（按扩展名 + 流式动图判定统一处理，读取前先过大小门控）：
    - 视觉 API 通常拒绝的格式（bmp/ico/tif/tiff；静图 gif）先转 PNG；
    - 动图 gif 转 H.264 MP4（ffmpeg，kind 转 video、部件转 video_url），
      ffmpeg 缺失/转码失败回退首帧 PNG（仍失败且超过图片读取上限时
      整体拒绝注入，绝不回退原始 gif）；
    - 原生支持的格式（png/jpg/jpeg/webp）不变，图片 >2MB（或显式 quality）
      仍走 JPEG 缩略图降采样（长边 MODEL_MEDIA_MAX_EDGE，默认质量 85）；
      gif 无论动静跳过降采样（转换后的小体积无需再压，静图首帧同理）；
    - 视频（含动图 gif 转码源）走区间读取管线：默认读
      [0, min(时长, VIDEO_MAX_READ_SECONDS)]，start_time/end_time 指定
      区间（帧吸附），start==end 仅探测元数据；返回值携带 video 元信息块。
    thumb_cache_dir / transcode_cache_dir：缩略图与格式转换的缓存目录；
    缺省时按会话媒体目录口径用第一个可用会话目录（避免缓存落进当前进程
    cwd），仅影响缓存位置、不影响数据。
    """
    kind = media_kind(path.name)
    mime = media_mime_type(path.name)
    suffix_lower = Path(path.name).suffix.lower()
    try:
        stat = path.stat()
        size = stat.st_size
    except OSError:
        return None

    # 发送/读取大小门控（读取字节前拦截，按内容判定）：动图 gif 与视频
    # 600MB、静图 30MB、音频 20MB——超限直接拒绝注入（错误占位告知模型），
    # 不再整读进内存，也绝不回退原图发送
    animated_probe = (
        gif_is_animated(path) if (kind == "image" and suffix_lower == ".gif") else None
    )
    limit_bytes = media_send_size_limit_bytes(path, animated_probe)
    if size > limit_bytes:
        return {
            "error": (
                f"{path.name}（{size / (1024 * 1024):.1f}MB）超过"
                f"{'动图/视频' if (animated_probe or kind == 'video') else '图片/音频'}"
                f"读取上限 {limit_bytes // (1024 * 1024)}MB，未注入；请压缩后再读取"
            ),
        }

    # ---------- 视频/动图统一区间读取管线 ----------
    transcode_dir = (
        transcode_cache_dir
        if transcode_cache_dir is not None
        else _transcode_cache_dir("media_model_part")
    )
    # 原生视频与动图 gif（转码后）都不再全量注入：默认读 [0, min(时长, 上限)]，
    # start_time/end_time 指定区间（帧吸附），start==end 仅探测元数据。
    # 失败语义：视频/显式区间 → 错误占位；动图 gif 未指定区间 → 回退静图管线
    is_video_kind = kind == "video"
    is_animated_gif = kind == "image" and suffix_lower == ".gif" and bool(animated_probe)
    if is_video_kind or is_animated_gif:
        range_built = _build_video_range_part(
            path, transcode_dir, start_time, end_time, is_animated_gif
        )
        if range_built.get("error") is None:
            return {
                "part": range_built["part"],
                "kind": "video",
                "mime": "video/mp4",
                "size": range_built.get("size") or size,
                "downsampled": False,
                "quality": None,
                "stored_name": path.name,
                "filename": path.name,
                "converted": range_built.get("converted"),
                "video": range_built.get("video"),
            }
        if is_video_kind or start_time is not None or end_time is not None:
            # 原生视频管线失败（ffmpeg 缺失/探测失败等）或显式区间请求失败：
            # 直接返回错误说明（全量读取同样不可行，静默回退只会原样报错）
            return {"error": range_built["error"]}
        # 动图 gif 管线失败且未指定区间：落到下方静图内核（内核内部先重试
        # mp4 再兜底首帧 PNG/JPEG 缩略图），维持既有兜底语义

    # ---------- 图像/静图路径（原生或转换后的 image） ----------
    converted: dict[str, Any] | None = None
    effective_kind = kind
    try:
        data = path.read_bytes()
    except OSError:
        return None

    # 统一转换内核：视觉 API 拒绝的格式在发送前转换；成功时 kind/mime/
    # 数据以转换结果为准，失败静默回退原图（与缩略图失败回退同口径）
    try:
        converted_result = _load_converted_media_base64(transcode_dir, path)
    except MediaSendTooLargeError as gate_error:
        return {"error": str(gate_error)}
    if converted_result is not None:
        conv_mime, conv_b64, conv_meta = converted_result
        mime, data_b64 = conv_mime, conv_b64
        effective_kind = conv_meta["converted_kind"]
        converted = conv_meta
    else:
        data_b64 = base64.b64encode(data).decode("ascii")

    needs_downsample = (
        effective_kind == "image"
        and (size > IMAGE_THUMBNAIL_THRESHOLD_BYTES or quality is not None)
        and Path(path.name).suffix.lower() not in _IMAGE_THUMBNAIL_SKIP_EXTENSIONS
        and converted is None
    )
    downsampled = False
    if needs_downsample:
        thumb = _load_image_thumbnail_base64(
            thumb_cache_dir if thumb_cache_dir is not None else _thumb_dir("media_model_part"),
            path,
            max_edge=MODEL_MEDIA_MAX_EDGE,
            quality=MODEL_MEDIA_DEFAULT_QUALITY if quality is None else quality,
        )
        if thumb is not None:
            mime, data_b64 = thumb
            downsampled = True
    # 缩略图失败的兜底门控：静图（未转换）无法降质时，超过图片读取上限的
    # 原始字节绝不注入（≤30MB 的原图保持既有回退语义不变）
    if (
        not downsampled
        and effective_kind == "image"
        and converted is None
        and size > MEDIA_SEND_IMAGE_LIMIT_BYTES
    ):
        return {
            "error": (
                f"{path.name}（{size / (1024 * 1024):.1f}MB）缩略图生成失败且超过"
                f"图片读取上限 {MEDIA_SEND_IMAGE_LIMIT_BYTES // (1024 * 1024)}MB，未注入"
            ),
        }
    if effective_kind == "image":
        part = {
            "type": "image_url",
            "image_url": {"url": _media_data_url(mime, data_b64)},
        }
    elif effective_kind == "video":
        part = {
            "type": "video_url",
            "video_url": {"url": _media_data_url(mime, data_b64)},
        }
    else:
        part = {
            "type": "input_audio",
            "input_audio": {"data": data_b64, "format": Path(path.name).suffix.lower().lstrip(".")},
        }
    return {
        "part": part,
        "kind": effective_kind,
        "mime": mime,
        "size": size,
        "downsampled": downsampled,
        "quality": (
            MODEL_MEDIA_DEFAULT_QUALITY if quality is None else quality
        ) if downsampled else None,
        "stored_name": path.name,
        "filename": path.name,
        "converted": converted,
    }


def load_session_media_model_part(
    session_id: str,
    reference: str,
    quality: int | None = None,
    start_time: float | None = None,
    end_time: float | None = None,
) -> dict[str, Any] | None:
    """为 read_media 内置工具加载一个会话媒体文件，返回可直接放入消息 content 的部件。

    返回 dict：{kind, mime, base64|data_url, stored_name, filename, size,
    downsampled(bool), quality(实际生效的缩略图质量，未降采样为 None)}；
    视频/动图附带 video 元信息块（时长/帧率/分辨率/实际读取区间）；
    引用非法/文件不存在/读取失败返回 None（调用方转为模型可见错误）。
    """
    path = resolve_media_path(session_id, reference)
    if path is None:
        return None
    return _media_model_part_from_path(
        path,
        quality,
        thumb_cache_dir=_thumb_dir(session_id),
        transcode_cache_dir=_transcode_cache_dir(session_id),
        start_time=start_time,
        end_time=end_time,
    )


def describe_session_media_file(session_id: str, reference: str) -> dict[str, Any] | None:
    """读取媒体文件元信息（不入内存完整字节前可用的轻量探测）。"""
    path = resolve_media_path(session_id, reference)
    if path is None:
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    return {
        "filename": path.name,
        "stored_name": path.name,
        "kind": media_kind(path.name) or "other",
        "mime": media_mime_type(path.name),
        "size": stat.st_size,
    }


def save_session_media_stream(session_id: str, filename: str, file_obj: Any) -> dict[str, Any]:
    """流式保存大媒体文件（如 600MB 视频或大 gif 动图），边拷贝边计数、超限即中止。

    与 save_session_media 的区别：不把整个文件读入内存，也不做格式转换
    （视频无需转码，直接按原始格式存储）。`file_obj` 为二进制文件对象。
    动图 gif（可能数百 MB，转码前先缩放）按视频档上限（600MB）接受，
    与读取/发送侧的按内容判定口径一致；其余类别沿用上传口径。
    """
    kind = media_kind(filename)
    if kind is None:
        raise ValueError(f"不支持的媒体类型: {filename}")
    suffix = Path(filename).suffix.lower()
    is_animated_gif = suffix == ".gif"
    size_limit = (
        MEDIA_SEND_ANIMATED_OR_VIDEO_LIMIT_BYTES
        if is_animated_gif
        else media_size_limit(kind)
    )
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
                        f"文件大小超过限制（"
                        f"{'动图/视频' if is_animated_gif else kind}"
                        f" 最大 {size_limit // (1024 * 1024)}MB）"
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


def resolve_media_content_parts(
    session_id: str,
    content: Any,
    vision_enabled: bool | None = None,
) -> tuple[Any, list[str]]:
    """把消息 content 列表里的 media:// 引用解析为上游 API 兼容格式。

    - ``image_url.url`` / ``video_url.url`` 等 URL 字段：解析为
      ``data:<mime>;base64,<b64>``（url 与 base64 双格式中的 base64 形态）；
    - ``input_audio.data``：解析为纯 base64（OpenAI 音频格式不要 data: 前缀）；
    - 其余部件（纯文本、http(s) URL、已内联 data:）原样保留。

    vision_enabled=False（当前模型不支持视觉）时媒体部件不再解析为 base64，
    就地替换为带引用的文本占位（「[图片 media://x.png]」等，与历史回放口径
    一致）——图片仅存档（media:// 引用已随消息落盘），不发图片数据给不支持
    视觉的模型；None 视为 True（保持向后兼容，默认按支持视觉解析）。

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
        # 视觉降级：模型不支持视觉时媒体部件转文本占位（audio 不受影响），
        # 引用保留在占位文本中，模型可切支持视觉的模型后用 read_media 回读
        if vision_enabled is False:
            media_label = _media_reference_label(part)
            if media_label is not None and str(part.get("type") or "") != "input_audio":
                resolved_parts.append({"type": "text", "text": media_label})
                continue
        new_part = dict(part)
        media_replaced = False
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
                    # 统一发送口径（与 _media_model_part_from_path 一致）：
                    # 视觉 API 拒绝的格式（静图 gif/bmp/ico/tif/tiff→PNG、
                    # 动图 gif→mp4）转换后发送；原图字节完整落盘不受影响
                    media_path = resolve_media_path(session_id, url)
                    try:
                        converted_result = (
                            _load_converted_media_base64(
                                _transcode_cache_dir(session_id), media_path
                            )
                            if media_path is not None
                            else None
                        )
                    except MediaSendTooLargeError as gate_error:
                        # 超过发送/读取上限：不再解析原图（避免超大 base64
                        # 撑爆请求体导致上游拒绝），就地替换为带原因的文本
                        # 占位（原图落盘不受影响）
                        resolved_parts.append({
                            "type": "text",
                            "text": f"[媒体 {url} 未注入：{gate_error}]",
                        })
                        media_replaced = True
                        break
                    if converted_result is not None:
                        conv_mime, conv_b64, _conv_meta = converted_result
                        new_part[key] = {**value, "url": _media_data_url(conv_mime, conv_b64)}
                        continue
                    # 大图降采样：超过阈值的图片改发缩略图（原图完整落盘不受影响），
                    # 缩略图生成失败或动图（GIF）按原图发送
                    try:
                        oversized = (
                            media_path is not None
                            and media_path.stat().st_size > IMAGE_THUMBNAIL_THRESHOLD_BYTES
                            and Path(media_path.name).suffix.lower() not in _IMAGE_THUMBNAIL_SKIP_EXTENSIONS
                        )
                    except OSError:
                        oversized = False
                    if oversized and media_path is not None:
                        thumb = _load_image_thumbnail_base64(
                            _thumb_dir(session_id), media_path
                        )
                        if thumb is not None:
                            mime, base64_data = thumb
                        elif media_path.stat().st_size > MEDIA_SEND_IMAGE_LIMIT_BYTES:
                            # 缩略图失败且超过图片读取上限：原图绝不上行
                            # （防御兜底；正常情况下读取/转换层已先行拦截）
                            resolved_parts.append({
                                "type": "text",
                                "text": (
                                    f"[媒体 {url} 未注入：{media_path.name}"
                                    f"（{media_path.stat().st_size / (1024 * 1024):.1f}MB）"
                                    f"超过图片读取上限 "
                                    f"{MEDIA_SEND_IMAGE_LIMIT_BYTES // (1024 * 1024)}MB"
                                    f"，未注入]"
                                ),
                            })
                            media_replaced = True
                            break
                    new_part[key] = {**value, "url": _media_data_url(mime, base64_data)}
        # 超限占位已整体替换：不再走后续解析（含音频分支）
        if media_replaced:
            continue
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


def resolve_message_media_refs(
    session_id: str,
    messages: list[Any],
    vision_enabled: bool | None = None,
) -> list[str]:
    """就地解析一组消息里的媒体引用（仅当前轮消息调用，历史轮次不解析）。

    vision_enabled=False 时媒体部件不解析为 base64，替换为文本占位（见
    resolve_media_content_parts）；vision_enabled=None 视为 True（兼容默认）。
    返回未成功解析的引用列表；单个解析异常不影响其余部件。
    """
    unresolved_all: list[str] = []
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        resolved, unresolved = resolve_media_content_parts(
            session_id, content, vision_enabled=vision_enabled
        )
        message["content"] = resolved
        unresolved_all.extend(unresolved)
    return unresolved_all


def _media_reference_label(part: dict[str, Any]) -> str | None:
    """把单个媒体部件转为文本口径的占位标注（历史回放/压缩用）。

    - 带可定位引用（media:// 会话相对引用 / http(s) URL / 本地路径）时输出
      「[图片 media://x.png]」形态——模型可据此调用 read_media 重新查看，
      不再是纯视觉占位；
    - base64 内嵌数据（data: 前缀）无引用可用，退回纯类型占位；
    - 非媒体部件返回 None。
    """
    part_type = str(part.get("type") or "")
    label = MEDIA_PART_LABELS.get(part_type)
    if not label:
        return None
    reference: Any = None
    if part_type == "image_url":
        value = part.get("image_url")
        reference = value.get("url") if isinstance(value, dict) else None
    elif part_type == "input_audio":
        value = part.get("input_audio")
        reference = value.get("data") if isinstance(value, dict) else None
    elif part_type == "video_url":
        value = part.get("video_url")
        reference = value.get("url") if isinstance(value, dict) else None
    text = str(reference or "").strip()
    if not text or text.startswith("data:"):
        return label
    # 「[图片 media://x.png]」：引用放括号内，便于模型一眼识别为可回读引用
    return f"[{label.strip('[]')} {text}]"


def content_part_to_text(content: Any, include_media_labels: bool = True) -> str:
    """把消息 content 规整为纯文本（历史回放/压缩/标题等文本口径统一入口）。

    - 字符串原样返回；
    - 多部件列表抽取 text 部件；媒体部件默认替换为带引用的可回溯占位
      （如「[图片 media://x.png]」，模型可用 read_media 重新查看；base64
      内嵌数据退化为「[图片]」纯占位），`include_media_labels=False` 时
      跳过媒体部件（轮次问题/会话标题用，只要纯文本）；
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
                label = _media_reference_label(part)
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
