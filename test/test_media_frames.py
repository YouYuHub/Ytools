"""视频帧拼图回退 + 音视频拒收分类型口径回归。

- 三协议统一：video/audio 绝不共用一条通知，带文件名与接口名；
- Messages/Responses 有视觉模型：视频转关键帧拼图（单图）真实发送；
- 无视觉/失败/音频：精确占位或拒收说明，不误导模型。
"""
import base64
import io
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image as _Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory import file_memory as fm
from factory.agent_runtime.builtin_tools import assemble_injected_media_parts
from factory.agent_runtime.builtin_tools import execute_read_media, load_any_media_model_part
from chat.protocols import messages_content, responses_content

TEST_SESSION = f"test_media_frames_{uuid.uuid4().hex[:8]}"


def _ffmpeg_exe():
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


requires_ffmpeg = pytest.mark.skipif(_ffmpeg_exe() is None, reason="需要 ffmpeg")


@pytest.fixture(scope="module", autouse=True)
def _session_dir():
    fm._get_session_dir(TEST_SESSION).mkdir(parents=True, exist_ok=True)
    yield
    shutil.rmtree(fm._get_session_dir(TEST_SESSION), ignore_errors=True)


def _make_test_mp4(name="clip.mp4", duration=2.0):
    ffmpeg = _ffmpeg_exe()
    assert ffmpeg, "需要 ffmpeg"
    path = fm._media_dir(TEST_SESSION) / name
    cmd = [ffmpeg, "-y", "-f", "lavfi", "-i",
           f"testsrc=duration={duration}:size=160x120:rate=10",
           "-pix_fmt", "yuv420p", str(path)]
    subprocess.run(cmd, capture_output=True, timeout=120, check=True)
    return path


# ---------- 统一拒收文案 ----------

def test_unsupported_notice_splits_video_audio():
    video = fm.media_unsupported_notice("video", "demo.mp4", "responses")
    assert "（视频）" in video and "demo.mp4" in video and "Responses" in video
    assert "（音频）" not in video
    audio = fm.media_unsupported_notice("audio", "note.mp3", "messages")
    assert "（音频）" in audio and "note.mp3" in audio and "Messages" in audio
    assert "（视频）" not in audio
    bare = fm.media_unsupported_notice("audio")
    assert bare == "[未发送附件（音频）。当前接口仅支持图片与文本输入。]"
    unknown = fm.media_unsupported_notice(None, "x.zzz", "responses")
    assert "（媒体）" in unknown


def test_display_name_extraction():
    assert fm.media_part_display_name(
        {"type": "video_url", "video_url": {"url": "media://demo_abc123.mp4"}}) == "demo_abc123.mp4"
    assert fm.media_part_display_name(
        {"type": "video_url", "video_url": {"url": "https://h/v/a.mov?x=1"}}) == "a.mov"
    assert fm.media_part_display_name(
        {"type": "video_url", "video_url": {"url": "data:video/mp4;base64,AAA"}}) is None
    assert fm.media_part_display_name(
        {"type": "input_audio", "input_audio": {"data": "media://n.mp3", "format": "mp3"}}) == "n.mp3"
    assert fm.media_part_display_name(
        {"type": "input_audio", "input_audio": {"data": "AAAA", "format": "mp3"}}) is None
    assert fm.media_part_display_name({"type": "text"}) is None
    assert fm.media_part_display_name("nope") is None


# ---------- 帧拼图内核 ----------

@requires_ffmpeg
def test_frames_sheet_real_video():
    path = _make_test_mp4()
    info = fm._media_model_part_from_path(
        path, api_type="messages",
        thumb_cache_dir=fm._thumb_dir(TEST_SESSION),
        transcode_cache_dir=fm._transcode_cache_dir(TEST_SESSION),
    )
    assert info.get("error") is None
    assert info["kind"] == "image"
    url = info["part"]["image_url"]["url"]
    assert url.startswith("data:image/jpeg;base64,")
    raw = base64.b64decode(url.split(",", 1)[1])
    with _Image.open(io.BytesIO(raw)) as img:
        assert img.format == "JPEG"
        assert img.size == (504, 304)  # 3 列×2 行，格子 160x120 + 22px 时间戳条
    assert info["converted"]["from_video"] is True
    assert info["converted"]["frames"] == 6
    assert "关键帧" in info["caption"] and "无音频内容" in info["caption"]
    assert "clip.mp4" in info["caption"]
    assert 1.5 < info["video"]["duration"] < 2.5
    assert info["video"]["read"]["truncated"] is False


@requires_ffmpeg
def test_frames_sheet_cache_reuse():
    path = _make_test_mp4("cached.mp4")
    kwargs = dict(api_type="messages", thumb_cache_dir=fm._thumb_dir(TEST_SESSION),
                  transcode_cache_dir=fm._transcode_cache_dir(TEST_SESSION))
    first = fm._media_model_part_from_path(path, **kwargs)
    sheets = list(fm._transcode_cache_dir(TEST_SESSION).glob("cached.mp4.frames_*.jpg"))
    assert len(sheets) == 1
    mtime = sheets[0].stat().st_mtime_ns
    second = fm._media_model_part_from_path(path, **kwargs)
    assert sheets[0].stat().st_mtime_ns == mtime
    assert second["part"]["image_url"]["url"] == first["part"]["image_url"]["url"]


@requires_ffmpeg
def test_frames_sheet_range_and_probe():
    path = _make_test_mp4("ranged.mp4")
    kwargs = dict(thumb_cache_dir=fm._thumb_dir(TEST_SESSION),
                  transcode_cache_dir=fm._transcode_cache_dir(TEST_SESSION))
    ranged = fm._media_model_part_from_path(path, api_type="responses",
                                            start_time=0.5, end_time=1.5, **kwargs)
    assert ranged.get("error") is None
    assert "0.5s~1.5s" in ranged["caption"]
    assert "Responses" in ranged["caption"]
    assert ranged["video"]["read"]["start"] == pytest.approx(0.5)
    probed = fm._media_model_part_from_path(path, api_type="messages",
                                            start_time=1.0, end_time=1.0, **kwargs)
    assert probed["part"]["type"] == "text"  # start==end 只探测元数据
    assert "元数据" in probed["part"]["text"]
    assert probed["video"].get("probe") is True


def test_frames_no_ffmpeg_precise_error():
    path = fm._media_dir(TEST_SESSION) / "nobin.mp4"
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42fake")
    with patch.object(fm, "_locate_ffmpeg", return_value=None):
        info = fm._media_model_part_from_path(
            path, api_type="messages",
            thumb_cache_dir=fm._thumb_dir(TEST_SESSION),
            transcode_cache_dir=fm._transcode_cache_dir(TEST_SESSION),
        )
    assert "part" not in info
    assert "视频" in info["error"] and "ffmpeg" in info["error"]
    assert "音频" not in info["error"]


def test_frames_corrupt_video_precise_error():
    path = fm._media_dir(TEST_SESSION) / "broken.mp4"
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42fake")
    info = fm._media_model_part_from_path(
        path, api_type="responses",
        thumb_cache_dir=fm._thumb_dir(TEST_SESSION),
        transcode_cache_dir=fm._transcode_cache_dir(TEST_SESSION),
    )
    assert "part" not in info
    assert "视频" in info["error"]
    assert "音频" not in info["error"]


def test_kernel_audio_messages_precise_error():
    path = fm._media_dir(TEST_SESSION) / "note.mp3"
    path.write_bytes(b"ID3" + b"\x00" * 64)
    info = fm._media_model_part_from_path(
        path, api_type="messages",
        thumb_cache_dir=fm._thumb_dir(TEST_SESSION),
        transcode_cache_dir=fm._transcode_cache_dir(TEST_SESSION),
    )
    assert "part" not in info
    assert "（音频）" in info["error"] and "note.mp3" in info["error"]
    assert "（视频）" not in info["error"]


def test_size_gate_messages_split_by_kind():
    with patch.object(fm, "media_send_size_limit_bytes", return_value=10):
        for name, want, avoid in (("a.png", "图片", "音频"),
                                  ("b.mp3", "音频", "视频"),
                                  ("c.mp4", "动图/视频", "图片")):
            path = fm._media_dir(TEST_SESSION) / name
            path.write_bytes(b"x" * 64)
            info = fm._media_model_part_from_path(
                path, thumb_cache_dir=fm._thumb_dir(TEST_SESSION),
                transcode_cache_dir=fm._transcode_cache_dir(TEST_SESSION),
            )
            assert "part" not in info
            assert want in info["error"], (name, info["error"])
            assert avoid not in info["error"], (name, info["error"])


def test_messages_image_error_splits_kind():
    video_err = fm._messages_image_model_part(Path("x.mp4"), api_type="responses")
    assert "（视频）" in video_err["error"] and "Responses" in video_err["error"]
    audio_err = fm._messages_image_model_part(Path("x.mp3"), api_type="messages")
    assert "（音频）" in audio_err["error"] and "（视频）" not in audio_err["error"]


# ---------- 附件解析层 ----------

@requires_ffmpeg
def test_resolve_messages_video_with_vision_sends_frames():
    src = _make_test_mp4("send.mp4")
    saved = fm.save_session_media(TEST_SESSION, "send.mp4", src.read_bytes())
    content = [{"type": "video_url", "video_url": {"url": saved["media_ref"]}}]
    resolved, unresolved = fm.resolve_media_content_parts(
        TEST_SESSION, content, vision_enabled=True, api_type="messages")
    assert unresolved == []
    assert len(resolved) == 2  # caption + 拼图
    assert resolved[0]["type"] == "text" and "关键帧" in resolved[0]["text"]
    assert resolved[1]["type"] == "image_url"
    assert resolved[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_resolve_messages_video_without_vision_keeps_placeholder():
    saved = fm.save_session_media(TEST_SESSION, "novis.mp4", b"\x00" * 64)
    content = [{"type": "video_url", "video_url": {"url": saved["media_ref"]}}]
    resolved, unresolved = fm.resolve_media_content_parts(
        TEST_SESSION, content, vision_enabled=False, api_type="messages")
    assert unresolved == []
    assert resolved == [{"type": "text", "text": f"[视频 {saved['media_ref']}]"}]


def test_resolve_messages_audio_precise_notice():
    saved = fm.save_session_media(TEST_SESSION, "note.mp3", b"ID3" + b"\x00" * 64)
    content = [{"type": "input_audio", "input_audio": {"data": saved["media_ref"], "format": "mp3"}}]
    resolved, unresolved = fm.resolve_media_content_parts(
        TEST_SESSION, content, vision_enabled=True, api_type="messages")
    assert unresolved == []
    assert len(resolved) == 1
    text = resolved[0]["text"]
    assert "（音频）" in text and saved["stored_name"] in text and "Messages" in text
    assert "（视频）" not in text


def test_resolve_responses_video_frames_label():
    if _ffmpeg_exe() is None:
        pytest.skip("需要 ffmpeg")
    src = _make_test_mp4("rs.mp4")
    saved = fm.save_session_media(TEST_SESSION, "rs.mp4", src.read_bytes())
    content = [{"type": "video_url", "video_url": {"url": saved["media_ref"]}}]
    resolved, _ = fm.resolve_media_content_parts(
        TEST_SESSION, content, vision_enabled=True, api_type="responses")
    assert "Responses" in resolved[0]["text"]


def test_resolve_video_zero_quota_never_converts_to_frames():
    saved = fm.save_session_media(TEST_SESSION, "banned.mp4", b"\x00" * 64)
    content = [{"type": "video_url", "video_url": {"url": saved["media_ref"]}}]
    resolved, unresolved = fm.resolve_media_content_parts(
        TEST_SESSION, content, vision_enabled=True, api_type="messages",
        media_limits={"video": 0})
    assert unresolved == []
    assert len(resolved) == 1
    assert "（视频）" in resolved[0]["text"] and "video: 0" in resolved[0]["text"]
    assert saved["stored_name"] in resolved[0]["text"]


def test_resolve_completions_video_passthrough_unchanged():
    saved = fm.save_session_media(TEST_SESSION, "raw.mp4", b"\x00" * 64)
    content = [{"type": "video_url", "video_url": {"url": saved["media_ref"]}}]
    resolved, unresolved = fm.resolve_media_content_parts(TEST_SESSION, content)
    assert unresolved == []
    assert resolved[0]["type"] == "video_url"
    assert resolved[0]["video_url"]["url"].startswith("data:video/mp4;base64,")


def test_resolve_messages_http_video_precise_notice():
    content = [{"type": "video_url", "video_url": {"url": "https://h/v/demo.mp4"}}]
    resolved, unresolved = fm.resolve_media_content_parts(
        TEST_SESSION, content, vision_enabled=True, api_type="messages")
    assert unresolved == []
    assert len(resolved) == 1
    assert "（视频）" in resolved[0]["text"] and "demo.mp4" in resolved[0]["text"]
    assert "（音频）" not in resolved[0]["text"]


# ---------- 协议转换层 ----------

def test_messages_content_splits_video_audio():
    video = messages_content.content_blocks(
        [{"type": "video_url", "video_url": {"url": "media://a.mp4"}}])[0]
    assert video["type"] == "text"
    assert "（视频）" in video["text"] and "a.mp4" in video["text"]
    assert "（音频）" not in video["text"]
    audio = messages_content.content_blocks(
        [{"type": "input_audio", "input_audio": {"data": "media://b.mp3", "format": "mp3"}}])[0]
    assert "（音频）" in audio["text"] and "b.mp3" in audio["text"]
    assert "（视频）" not in audio["text"]


def test_responses_content_degrades_video_audio_without_raise():
    parts = responses_content.content_parts([
        {"type": "video_url", "video_url": {"url": "media://a.mp4"}},
        {"type": "input_audio", "input_audio": {"data": "media://b.mp3", "format": "mp3"}},
    ])
    assert len(parts) == 2
    assert all(p["type"] == "input_text" for p in parts)
    assert "（视频）" in parts[0]["text"] and "a.mp4" in parts[0]["text"]
    assert "（音频）" in parts[1]["text"] and "b.mp3" in parts[1]["text"]


# ---------- 注入组装 ----------

def test_assemble_pairs_caption_with_media_key():
    img = {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AA"}}
    parts, keys = assemble_injected_media_parts([
        ("k1", "media://a.mp4", {"part": img, "caption": "[视频关键帧：a.mp4]"}),
        ("k2", "media://b.png", {"part": dict(img)}),
        ("k3", "media://c.mp4", {"error": "boom"}),
        ("k4", "media://d.mp4", None),
    ])
    assert [p.get("type") for p in parts] == ["text", "image_url", "image_url", "text", "text"]
    assert parts[0]["text"] == "[视频关键帧：a.mp4]"
    assert keys == ["k1", "k1", "k2", None, None]
    retained = [k if isinstance(p, dict) and p.get("type") in {"image_url", "input_audio"} else None
                for k, p in zip(keys, parts)]
    assert retained == [None, "k1", "k2", None, None]


def test_assemble_caps_video_and_maps_call_ids():
    v1 = {"type": "video_url", "video_url": {"url": "data:video/mp4;base64,AA"}}
    v2 = {"type": "video_url", "video_url": {"url": "data:video/mp4;base64,BB"}}
    parts, keys = assemble_injected_media_parts(
        [("k1", "media://a.mp4", {"part": v1}), ("k2", "media://b.mp4", {"part": v2})],
        responses_calls={"k1": "call-1", "k2": "call-2"},
    )
    assert parts[0]["type"] == "video_url" and parts[1]["type"] == "text"
    assert keys == ["k1", "k2"]
    assert parts[0]["_responses_call_id"] == "call-1"
    assert parts[1]["_responses_call_id"] == "call-2"


@requires_ffmpeg
def test_read_media_tool_video_frames_end_to_end():
    src = fm._media_dir(TEST_SESSION) / "tool_e2e_src.mp4"
    ffmpeg = _ffmpeg_exe()
    subprocess.run([ffmpeg, "-y", "-f", "lavfi", "-i", "testsrc=duration=3:size=160x120:rate=10",
                    "-pix_fmt", "yuv420p", str(src)],
                   capture_output=True, timeout=120, check=True)
    saved = fm.save_session_media(TEST_SESSION, "tool_e2e.mp4", src.read_bytes())
    res = execute_read_media({"references": [saved["media_ref"]]}, TEST_SESSION,
                             vision_enabled=True, api_type="messages")
    assert res["ok"] is True
    assert res["loaded"][0]["kind"] == "image"
    assert res["loaded"][0]["converted"]["from_video"] is True
    info = load_any_media_model_part(TEST_SESSION, saved["media_ref"], api_type="messages")
    parts, keys = assemble_injected_media_parts([("k1", saved["media_ref"], info)])
    assert [p.get("type") for p in parts] == ["text", "image_url"]
    assert keys == ["k1", "k1"]
    assert "关键帧" in parts[0]["text"]
