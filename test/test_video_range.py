# -*- coding: utf-8 -*-
"""视频区间读取回归：元数据探测、帧吸附、区间切片、探测约定与分段去重。

依赖：ffmpeg（缺失时依赖真实转码的用例自动跳过）；Pillow 生成动图样例。
"""
import base64
import io
import shutil
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))

from memory import file_memory as fm
from factory.agent_runtime import builtin_tools as bt

from PIL import Image, ImageDraw

TEST_SESSION = f"test_video_range_{uuid.uuid4().hex[:8]}"


def _animated_gif_bytes(size=(96, 64), frames=12) -> bytes:
    """多帧动图（帧间图形显著差异，避免调色板判重）。"""
    imgs = []
    for i in range(max(2, frames)):
        frame = Image.new("RGB", size, (i * 20 % 256, 60, 160))
        draw = ImageDraw.Draw(frame)
        draw.rectangle([i * 6 % size[0], 0, (i * 6 % size[0]) + 30, size[1]], fill=(255, 220, 0))
        imgs.append(frame)
    out = io.BytesIO()
    imgs[0].save(out, format="GIF", save_all=True, append_images=imgs[1:], duration=100, loop=0)
    return out.getvalue()


class VideoRangeKernelTests(unittest.TestCase):
    """区间读取内核：探测 / 吸附 / 切片 / 元数据约定（依赖 ffmpeg）。"""

    @classmethod
    def setUpClass(cls):
        cls.has_ffmpeg = fm._locate_ffmpeg() is not None
        cls.gif_bytes = _animated_gif_bytes()

    def setUp(self):
        fm._get_session_dir(TEST_SESSION).mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        try:
            shutil.rmtree(fm._get_session_dir(TEST_SESSION), ignore_errors=True)
        except Exception:
            pass

    def _gif_path(self, name="anim.gif"):
        path = fm._media_dir(TEST_SESSION) / name
        path.write_bytes(self.gif_bytes)
        return path

    def test_snap_frame_time(self):
        self.assertAlmostEqual(fm._snap_frame_time(1.234, 10.0), 1.2)
        self.assertAlmostEqual(fm._snap_frame_time(0.05, 10.0), 0.1, places=3)

    def test_parse_ffmpeg_time_seconds(self):
        self.assertAlmostEqual(fm._parse_ffmpeg_time_seconds("Duration: 00:01:30.25,"), 90.25)
        self.assertAlmostEqual(fm._parse_ffmpeg_time_seconds("00:00:05.000"), 5.0)
        self.assertIsNone(fm._parse_ffmpeg_time_seconds("N/A"))

    def test_probe_gif_metadata_estimated(self):
        path = self._gif_path("probe.gif")
        cache = fm._transcode_cache_dir(TEST_SESSION)
        meta = fm._probe_video_metadata(path, cache)
        self.assertIsNotNone(meta)
        self.assertGreater(meta["duration"], 0)
        self.assertGreater(meta["fps"], 0)
        self.assertEqual((meta["width"], meta["height"]), (96, 64))
        # ffmpeg 的 gif demuxer 会给出真实 Duration（无需估算分支）
        self.assertFalse(meta["estimated_duration"])
        # 缓存命中：二次读取返回同数据
        again = fm._probe_video_metadata(path, cache)
        self.assertEqual(again["duration"], meta["duration"])

    def test_probe_returns_none_for_non_video(self):
        path = fm._media_dir(TEST_SESSION) / "pic.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        self.assertIsNone(fm._probe_video_metadata_uncached(path, ".png"))

    def test_probe_only_returns_metadata_text_without_video_data(self):
        # 约定：start_time == end_time → 只返回元数据文本部件
        path = self._gif_path("probe_only.gif")
        cache = fm._transcode_cache_dir(TEST_SESSION)
        built = fm._build_video_range_part(path, cache, 0, 0, True)
        self.assertIsNone(built.get("error"))
        self.assertEqual(built["part"]["type"], "text")
        self.assertIn("视频元数据", built["part"]["text"])
        self.assertIn("区间读取约定", built["part"]["text"])
        self.assertTrue(built["video"].get("probe"))
        self.assertIsNone(built.get("size") or None)

    @unittest.skipUnless(shutil.which("ffmpeg") or _imageio_ok(), "需要 ffmpeg")
    def test_default_range_reads_first_60s(self):
        path = self._gif_path("range_default.gif")
        cache = fm._transcode_cache_dir(TEST_SESSION)
        built = fm._build_video_range_part(path, cache, None, None, True)
        self.assertIsNone(built.get("error"))
        read = built["video"]["read"]
        self.assertEqual(read["start"], 0.0)
        self.assertGreaterEqual(read["end"], read["start"])
        self.assertFalse(read["truncated"])
        self.assertEqual(built["part"]["type"], "video_url")
        raw = base64.b64decode(built["part"]["video_url"]["url"].split(",", 1)[1])
        self.assertEqual(raw[4:8], b"ftyp")
        self.assertGreater(built["size"], 0)

    @unittest.skipUnless(shutil.which("ffmpeg") or _imageio_ok(), "需要 ffmpeg")
    def test_range_clipped_and_start_gt_duration_error(self):
        path = self._gif_path("range_clip.gif")
        cache = fm._transcode_cache_dir(TEST_SESSION)
        # 超长区间：截断到上限并标记 truncated
        built = fm._build_video_range_part(path, cache, 0, 10_000.0, True)
        self.assertIsNone(built.get("error"))
        self.assertTrue(built["video"]["read"]["truncated"])
        # start 超过时长：错误
        built2 = fm._build_video_range_part(path, cache, 99_999.0, None, True)
        self.assertIn("超出视频时长", built2.get("error", ""))
        # end < start：错误（内核层兜底，normalize 层已先行拦截）
        built3 = fm._build_video_range_part(path, cache, 2.0, 1.0, True)
        self.assertIn("小于", built3.get("error", ""))

    @unittest.skipUnless(shutil.which("ffmpeg") or _imageio_ok(), "需要 ffmpeg")
    def test_media_model_part_video_pipeline_returns_video_meta(self):
        # 加载内核集成：动图 gif 未指定区间 → 默认读前 N 秒并带 video 块
        path = self._gif_path("kernel_meta.gif")
        info = fm._media_model_part_from_path(
            path, transcode_cache_dir=fm._transcode_cache_dir(TEST_SESSION),
        )
        self.assertEqual(info["kind"], "video")
        self.assertIn("video", info)
        self.assertGreater(info["video"]["duration"], 0)
        self.assertIn("read", info["video"])
        self.assertEqual(info["part"]["type"], "video_url")
        # 工具结果元信息（execute_read_media 的 loaded 项）应携带 video 块

    @unittest.skipUnless(shutil.which("ffmpeg") or _imageio_ok(), "需要 ffmpeg")
    def test_injected_key_distinguishes_ranges(self):
        # 同一引用不同区间是新的读取（分段续读不被 already_injected 拦截）
        self.assertEqual(
            bt._media_injected_key("media://v.mp4", None, None), "media://v.mp4"
        )
        self.assertEqual(
            bt._media_injected_key("media://v.mp4", 0.0, 60.0), "media://v.mp4#0.000-60.000"
        )
        self.assertNotEqual(
            bt._media_injected_key("media://v.mp4", 0.0, 60.0),
            bt._media_injected_key("media://v.mp4", 60.0, 120.0),
        )
        # 相同区间重复读取仍被去重（键一致）
        first = bt.execute_read_media(
            {"references": ["media://v.mp4"], "start_time": 0, "end_time": 60},
            "session", set(), None,
            load_media=lambda *a, **k: {"part": {"type": "text", "text": "x"}, "kind": "video"},
        )
        self.assertTrue(first["ok"])
        second = bt.execute_read_media(
            {"references": ["media://v.mp4"], "start_time": 0, "end_time": 60},
            "session", set(), set(first["injected_references"]),
            load_media=lambda *a, **k: {"part": {"type": "text", "text": "x"}, "kind": "video"},
        )
        self.assertFalse(second["ok"])
        # 不同区间：允许（新读取）
        third = bt.execute_read_media(
            {"references": ["media://v.mp4"], "start_time": 60, "end_time": 120},
            "session", set(), set(first["injected_references"]),
            load_media=lambda *a, **k: {"part": {"type": "text", "text": "x"}, "kind": "video"},
        )
        self.assertTrue(third["ok"])


def _imageio_ok():
    try:
        import imageio_ffmpeg  # noqa: F401
        return True
    except ImportError:
        return False


if __name__ == "__main__":
    unittest.main()
