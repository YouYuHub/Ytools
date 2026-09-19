"""read_media 格式转换链路回归：静图 gif→PNG、动图 gif→MP4(H.264)、bmp→PNG、
原生格式原样、降采样互斥、缓存命中、URL 直链转换回退。

依赖：Pillow（测试生成样例）；ffmpeg 仅在动图用例需要（缺失时该用例自动跳过）。
"""
import base64
import io
import os
import shutil
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory import file_memory as fm
from factory.agent_runtime import builtin_tools as bt

from PIL import Image as _Image

TEST_SESSION = f"test_media_convert_{uuid.uuid4().hex[:8]}"


def _static_gif_bytes(size=(64, 48)) -> bytes:
    out = io.BytesIO()
    _Image.new("P", size, color=3).save(out, format="GIF")
    return out.getvalue()


def _animated_gif_bytes(size=(64, 48), frames=2) -> bytes:
    """构造多帧动图 GIF：纯色帧会被 Pillow 调色板量化合并，需含不同图形。"""
    from PIL import ImageDraw

    imgs = []
    for i in range(max(2, frames)):
        base_color = [(255, 0, 0), (0, 200, 0), (0, 0, 255)][i % 3]
        frame = _Image.new("RGB", size, base_color)
        draw = ImageDraw.Draw(frame)
        # 帧间图形差异足够大，避免调色板量化后相邻帧被判重
        if i % 2 == 0:
            draw.rectangle([0, 0, size[0] // 2, size[1]], fill=(0, 0, 255))
        else:
            draw.ellipse([size[0] // 4, 0, size[0] * 3 // 4, size[1]], fill=(255, 255, 0))
        imgs.append(frame)
    out = io.BytesIO()
    imgs[0].save(
        out, format="GIF", save_all=True,
        append_images=imgs[1:], duration=120, loop=0,
    )
    return out.getvalue()


def _bmp_bytes(size=(64, 48)) -> bytes:
    out = io.BytesIO()
    _Image.new("RGB", size, (200, 30, 30)).save(out, format="BMP")
    return out.getvalue()


def _png_bytes(size=(64, 48)) -> bytes:
    out = io.BytesIO()
    _Image.new("RGB", size, (10, 120, 240)).save(out, format="PNG")
    return out.getvalue()


class MediaConvertTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 动图样例：确认 Pillow 生成多帧
        cls.animated_gif = _animated_gif_bytes()
        with _Image.open(io.BytesIO(cls.animated_gif)) as img:
            cls.animated_frames = getattr(img, "n_frames", 1)

    def setUp(self):
        fm._get_session_dir(TEST_SESSION).mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        try:
            shutil.rmtree(fm._get_session_dir(TEST_SESSION), ignore_errors=True)
        except Exception:
            pass

    # ---------- 动图判定 ----------
    def test_image_is_animated_true_for_multi_frame_gif(self):
        self.assertGreater(self.animated_frames, 1, "测试样例应为多帧")
        path = fm._media_dir(TEST_SESSION) / "anim.gif"
        path.write_bytes(self.animated_gif)
        self.assertTrue(fm.image_is_animated(path))

    def test_image_is_animated_false_for_single_frame(self):
        path = fm._media_dir(TEST_SESSION) / "static.gif"
        path.write_bytes(_static_gif_bytes())
        self.assertFalse(fm.image_is_animated(path))

    def test_image_is_animated_corrupt_returns_false(self):
        path = fm._media_dir(TEST_SESSION) / "junk.gif"
        path.write_bytes(b"GIF89a" + b"corrupt")
        self.assertFalse(fm.image_is_animated(path))

    # ---------- 静图 gif → PNG ----------
    def test_static_gif_converts_to_png(self):
        path = fm._media_dir(TEST_SESSION) / "static.gif"
        path.write_bytes(_static_gif_bytes())
        info = fm._media_model_part_from_path(
            path, thumb_cache_dir=fm._thumb_dir(TEST_SESSION),
            transcode_cache_dir=fm._transcode_cache_dir(TEST_SESSION),
        )
        self.assertIsNotNone(info["converted"])
        self.assertEqual(info["converted"]["original_format"], "gif")
        self.assertEqual(info["converted"]["converted_format"], "png")
        self.assertEqual(info["converted"]["animated"], False)
        self.assertEqual(info["converted"]["converted_kind"], "image")
        self.assertEqual(info["kind"], "image")
        self.assertEqual(info["mime"], "image/png")
        url = info["part"]["image_url"]["url"]
        self.assertTrue(url.startswith("data:image/png;base64,"))
        # 转换结果是合法 PNG
        raw = base64.b64decode(url.split(",", 1)[1])
        with _Image.open(io.BytesIO(raw)) as img:
            self.assertEqual(img.format, "PNG")

    # ---------- 动图 gif → MP4 ----------
    def test_animated_gif_converts_to_mp4(self):
        if shutil.which("ffmpeg") is None:
            self.skipTest("系统未安装 ffmpeg")
        self._assert_animated_gif_to_mp4()

    def test_animated_gif_to_mp4_via_imageio_fallback(self):
        # 模拟用户机器未装系统 ffmpeg（patch shutil.which 恒 None）：
        # 回退 imageio-ffmpeg 内置二进制应完成同样转码（任何环境可验证）。
        # 独立文件名避开同会话其他用例的缓存，确保真实走回退转码
        try:
            import imageio_ffmpeg  # noqa: F401
        except ImportError:
            self.skipTest("未安装 imageio-ffmpeg")
        with patch("shutil.which", return_value=None):
            located = fm._locate_ffmpeg()
            self.assertIsNotNone(located, "应回退到 imageio-ffmpeg 内置 ffmpeg")
            path = fm._media_dir(TEST_SESSION) / "anim_fb.gif"
            path.write_bytes(self.animated_gif)
            cache = fm._transcode_cache_dir(TEST_SESSION)
            info = fm._media_model_part_from_path(
                path, thumb_cache_dir=fm._thumb_dir(TEST_SESSION),
                transcode_cache_dir=cache,
            )
        self.assertIsNotNone(info["converted"])
        self.assertEqual(info["converted"]["converted_format"], "mp4")
        self.assertEqual(info["kind"], "video")
        # 缓存文件单点命名：<原名>.conv.mp4
        self.assertTrue((cache / "anim_fb.gif.conv.mp4").is_file())

    def _assert_animated_gif_to_mp4(self):
        path = fm._media_dir(TEST_SESSION) / "anim.gif"
        path.write_bytes(self.animated_gif)
        info = fm._media_model_part_from_path(
            path, thumb_cache_dir=fm._thumb_dir(TEST_SESSION),
            transcode_cache_dir=fm._transcode_cache_dir(TEST_SESSION),
        )
        self.assertIsNotNone(info["converted"])
        self.assertEqual(info["converted"]["converted_format"], "mp4")
        self.assertEqual(info["converted"]["animated"], True)
        self.assertEqual(info["converted"]["converted_kind"], "video")
        self.assertEqual(info["kind"], "video")
        self.assertEqual(info["mime"], "video/mp4")
        # 部件类型转为 video_url
        url = info["part"]["video_url"]["url"]
        self.assertTrue(url.startswith("data:video/mp4;base64,"))
        raw = base64.b64decode(url.split(",", 1)[1])
        # H.264 魔数：ftyp box（isom/mp42 等品牌）
        self.assertEqual(raw[4:8], b"ftyp")
        self.assertGreater(info["size"], 0)

    # ---------- bmp → PNG ----------
    def test_bmp_converts_to_png(self):
        path = fm._media_dir(TEST_SESSION) / "pic.bmp"
        path.write_bytes(_bmp_bytes())
        info = fm._media_model_part_from_path(
            path, transcode_cache_dir=fm._transcode_cache_dir(TEST_SESSION),
        )
        self.assertIsNotNone(info["converted"])
        self.assertEqual(info["converted"]["converted_format"], "png")
        self.assertEqual(info["mime"], "image/png")
        self.assertEqual(info["kind"], "image")

    # ---------- 原生格式原样 ----------
    def test_native_png_no_conversion(self):
        path = fm._media_dir(TEST_SESSION) / "native.png"
        path.write_bytes(_png_bytes())
        info = fm._media_model_part_from_path(
            path, transcode_cache_dir=fm._transcode_cache_dir(TEST_SESSION),
        )
        self.assertIsNone(info["converted"])
        self.assertEqual(info["kind"], "image")
        self.assertTrue(info["part"]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_video_kind_untouched(self):
        # 视频扩展名不进入图像转换分支；损坏视频经区间读取管线给出明确
        # 错误占位（不再原样注入假数据）
        path = fm._media_dir(TEST_SESSION) / "clip.mp4"
        path.write_bytes(b"\x00\x00\x00\x18ftypmp42fake")
        info = fm._media_model_part_from_path(
            path, transcode_cache_dir=fm._transcode_cache_dir(TEST_SESSION),
        )
        self.assertIn("error", info)
        self.assertNotIn("part", info)

    # ---------- 缓存命中 ----------
    def test_conversion_cache_reuse(self):
        path = fm._media_dir(TEST_SESSION) / "cached.bmp"
        path.write_bytes(_bmp_bytes())
        cache = fm._transcode_cache_dir(TEST_SESSION)
        first = fm._load_converted_media_base64(cache, path)
        self.assertIsNotNone(first)
        cached_files = list(cache.glob("cached.bmp.conv.png"))
        self.assertEqual(len(cached_files), 1)
        first_mtime = cached_files[0].stat().st_mtime_ns
        second = fm._load_converted_media_base64(cache, path)
        self.assertIsNotNone(second)
        self.assertEqual(first[1], second[1])
        # 缓存命中不重写转换文件
        self.assertEqual(cached_files[0].stat().st_mtime_ns, first_mtime)
        # 源文件变化 → 指纹失效重建
        os.utime(path, (0, 0))
        path.write_bytes(_bmp_bytes(size=(80, 60)))
        third = fm._load_converted_media_base64(cache, path)
        self.assertIsNotNone(third)
        self.assertNotEqual(first[1], third[1])

    # ---------- 转换失败回退原图 ----------
    def test_corrupt_unsafe_image_falls_back(self):
        path = fm._media_dir(TEST_SESSION) / "broken.bmp"
        path.write_bytes(b"BM" + b"junk" * 32)
        info = fm._media_model_part_from_path(
            path, transcode_cache_dir=fm._transcode_cache_dir(TEST_SESSION),
        )
        # 转换失败：converted=None，按原图 gif 口径原样回传
        self.assertIsNone(info["converted"])
        self.assertEqual(info["kind"], "image")
        self.assertTrue(info["part"]["image_url"]["url"].startswith("data:image/bmp;base64,"))

    def test_corrupt_gif_falls_back_to_original(self):
        # 动图判定失败也按 False（静图）处理，但 PNG 转换失败 → 回退原图
        path = fm._media_dir(TEST_SESSION) / "broken.gif"
        path.write_bytes(b"GIF89a" + b"junk" * 32)
        info = fm._media_model_part_from_path(
            path, transcode_cache_dir=fm._transcode_cache_dir(TEST_SESSION),
        )
        self.assertIsNone(info["converted"])
        self.assertEqual(info["kind"], "image")
        self.assertTrue(info["part"]["image_url"]["url"].startswith("data:image/gif;base64,"))

    # ---------- 附件注入路径（resolve_media_content_parts）----------
    def test_resolve_parts_static_gif_becomes_png(self):
        saved = fm.save_session_media(TEST_SESSION, "上传.gif", _static_gif_bytes())
        content = [{"type": "image_url", "image_url": {"url": saved["media_ref"]}}]
        resolved, unresolved = fm.resolve_media_content_parts(TEST_SESSION, content)
        self.assertEqual(unresolved, [])
        url = resolved[0]["image_url"]["url"]
        self.assertTrue(url.startswith("data:image/png;base64,"))

    @patch.object(fm, "_VISION_UNSAFE_IMAGE_EXTENSIONS", {".bmp"})
    def test_resolve_parts_native_png_unchanged(self):
        saved = fm.save_session_media(TEST_SESSION, "原图.png", _png_bytes())
        content = [{"type": "image_url", "image_url": {"url": saved["media_ref"]}}]
        resolved, unresolved = fm.resolve_media_content_parts(TEST_SESSION, content)
        self.assertEqual(unresolved, [])
        self.assertTrue(resolved[0]["image_url"]["url"].startswith("data:image/png;base64,"))


class LoadAnyConvertTests(unittest.TestCase):
    """load_any_media_model_part 的本地路径转换（网络路径仅测分流函数）。"""

    @classmethod
    def tearDownClass(cls):
        transcode = fm._transcode_cache_dir("network_media")
        for f in transcode.glob("dl_*"):
            try:
                f.unlink()
            except OSError:
                pass

    def test_local_bmp_via_load_any(self):
        tmp = Path(tempfile.mkdtemp()) / "local.bmp"
        tmp.write_bytes(_bmp_bytes())
        try:
            info = bt_load(tmp)
            self.assertIsNotNone(info)
            self.assertEqual(info["source_type"], "local")
            self.assertIsNotNone(info["converted"])
            self.assertEqual(info["mime"], "image/png")
        finally:
            try:
                tmp.unlink()
                tmp.parent.rmdir()
            except OSError:
                pass

    def test_local_static_gif_via_load_any(self):
        tmp = Path(tempfile.mkdtemp()) / "local.gif"
        tmp.write_bytes(_static_gif_bytes())
        try:
            info = bt_load(tmp)
            self.assertIsNotNone(info)
            self.assertEqual(info["mime"], "image/png")
            self.assertEqual(info["converted"]["converted_format"], "png")
        finally:
            try:
                tmp.unlink()
                tmp.parent.rmdir()
            except OSError:
                pass


class SendLimitTests(unittest.TestCase):
    """发送/读取大小门控与动图回退兜底（按内容判定）。"""

    @classmethod
    def tearDownClass(cls):
        try:
            shutil.rmtree(fm._get_session_dir(TEST_SESSION), ignore_errors=True)
        except Exception:
            pass

    def _write(self, name: str, data: bytes) -> Path:
        path = fm._media_dir(TEST_SESSION) / name
        path.write_bytes(data)
        return path

    def _info(self, path: Path):
        return fm._media_model_part_from_path(
            path, thumb_cache_dir=fm._thumb_dir(TEST_SESSION),
            transcode_cache_dir=fm._transcode_cache_dir(TEST_SESSION),
        )

    def test_media_send_size_limit_by_content(self):
        self.assertEqual(
            fm.media_send_size_limit_bytes(Path("x.png")),
            fm.MEDIA_SEND_IMAGE_LIMIT_BYTES,
        )
        self.assertEqual(
            fm.media_send_size_limit_bytes(Path("x.gif"), animated=False),
            fm.MEDIA_SEND_IMAGE_LIMIT_BYTES,
        )
        self.assertEqual(
            fm.media_send_size_limit_bytes(Path("x.gif"), animated=True),
            fm.MEDIA_SEND_ANIMATED_OR_VIDEO_LIMIT_BYTES,
        )
        self.assertEqual(
            fm.media_send_size_limit_bytes(Path("x.mp4")),
            fm.MEDIA_SEND_ANIMATED_OR_VIDEO_LIMIT_BYTES,
        )
        self.assertEqual(
            fm.media_send_size_limit_bytes(Path("x.wav")),
            fm.MEDIA_SIZE_LIMITS["audio"],
        )

    def test_oversized_static_image_rejected_with_error_placeholder(self):
        big = b"\x89PNG\r\n\x1a\n" + b"x" * (fm.MEDIA_SEND_IMAGE_LIMIT_BYTES + 1)
        info = self._info(self._write("big_over.png", big))
        self.assertIn("error", info)
        self.assertNotIn("part", info)
        self.assertIn("30MB", info["error"])

    def test_oversized_video_rejected_before_read(self):
        fake = b"\x00\x00\x00\x18ftypmp42" + b"x" * (
            fm.MEDIA_SEND_ANIMATED_OR_VIDEO_LIMIT_BYTES + 1
        )
        info = self._info(self._write("big_over.mp4", fake))
        self.assertIn("error", info)
        self.assertNotIn("part", info)
        self.assertIn("600MB", info["error"])

    def test_oversized_static_gif_rejected(self):
        # 全零尾部：动图扫描走未知块 → Pillow 兜底失败 → 按静图 30MB 档拒绝
        big = b"GIF89a" + b"\x00" * (fm.MEDIA_SEND_IMAGE_LIMIT_BYTES + 1024 * 1024)
        info = self._info(self._write("big_static_over.gif", big))
        self.assertIn("error", info)
        self.assertNotIn("part", info)
        self.assertIn("30MB", info["error"])

    def test_animated_gif_kernel_mp4_failure_falls_back_first_frame_png(self):
        # 内核级：动图 mp4 转码失败 → 回退首帧 PNG（converted_kind 变 image）
        path = self._write("kern_fb.gif", _animated_gif_bytes())
        with patch.object(fm, "_convert_gif_to_mp4_file", side_effect=RuntimeError("ffmpeg 失败")):
            result = fm._load_converted_media_base64(
                fm._transcode_cache_dir(TEST_SESSION), path
            )
        self.assertIsNotNone(result)
        mime, b64, meta = result
        self.assertEqual(mime, "image/png")
        self.assertEqual(meta["converted_kind"], "image")
        self.assertEqual(meta["converted_format"], "png")
        raw = base64.b64decode(b64)
        with _Image.open(io.BytesIO(raw)) as img:
            self.assertEqual(img.format, "PNG")

    def test_animated_gif_all_conversion_failures_kernel_returns_none(self):
        # 内核级：mp4 与首帧 PNG 都失败 → 返回 None（调用方按原图口径处理，
        # 但 _media_model_part_from_path 的兜底门控会拦超大原图）
        path = self._write("kern_fail.gif", _animated_gif_bytes())
        with patch.object(fm, "_convert_gif_to_mp4_file", side_effect=RuntimeError("ffmpeg 失败")), \
             patch.object(fm, "_convert_image_file_to_png_file", side_effect=RuntimeError("转换失败")):
            result = fm._load_converted_media_base64(
                fm._transcode_cache_dir(TEST_SESSION), path
            )
        self.assertIsNone(result)

    def test_animated_gif_kernel_failure_final_gate_blocks_oversized_original(self):
        # 集成：35MB 动图（超过 30MB 图片档）转码链路全失败时，兜底门控
        # 拦截原图注入（绝不发送原始 gif）；文件内容为头部+零块（转码必失败）
        with patch.object(fm, "gif_is_animated", return_value=True):
            big = b"GIF89a" + b"\x00" * (35 * 1024 * 1024)
            info = self._info(self._write("mid_anim_over.gif", big))
        self.assertIn("error", info)
        self.assertNotIn("part", info)
        self.assertIn("30MB", info["error"])

    def test_oversized_animated_gif_gate_before_conversion(self):
        # 动图判定 True + 超过 600MB：读取门控直接拒绝（转换前拦截）
        with patch.object(fm, "gif_is_animated", return_value=True):
            big = b"GIF89a" + b"\x00" * (
                fm.MEDIA_SEND_ANIMATED_OR_VIDEO_LIMIT_BYTES + 1
            )
            info = self._info(self._write("big_anim_over.gif", big))
        self.assertIn("error", info)
        self.assertNotIn("part", info)
        self.assertIn("600MB", info["error"])

    def test_gif_is_animated_true_and_false(self):
        # 流式字节扫描与 Pillow 判定口径一致
        animated_path = self._write("scan_anim.gif", _animated_gif_bytes())
        static_path = self._write("scan_static.gif", _static_gif_bytes())
        self.assertTrue(fm.gif_is_animated(animated_path))
        self.assertFalse(fm.gif_is_animated(static_path))
        self.assertFalse(fm.gif_is_animated(self._write("scan_junk.gif", b"GIF89ajunk")))


def bt_load(path):
    return bt.load_any_media_model_part(TEST_SESSION, str(path))


if __name__ == "__main__":
    unittest.main()
